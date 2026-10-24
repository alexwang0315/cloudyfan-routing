"""Bounded independent HTTP workers; product assembly is supplied explicitly."""
from contextlib import contextmanager, ExitStack
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import sqlite3
import threading
import time
from uuid import uuid4

import httpx
import pytest

from capacity_admission_process_fixture import SharedClock, Worker


def assemble_queue_host(root, index, clock, origin, options, before_response):
    from capacity_queue_fixture import build_queue_host
    return build_queue_host(root, clock=clock, origin=origin, options=options,
        before_response=before_response)


class QueueProcessPair:
    """Run the real shared queue in separately spawned HTTP worker processes.

    The assembly function must reopen the published product fixture. The parent
    controls synthetic upstream completion and calls the actual scheduler through
    a private pipe; queue submission, observation and cancellation use real HTTP.
    """

    def __init__(self, root, network, assemble, *, first_mode="normal", **options):
        self.root, self.network, self.assemble = Path(root), network, assemble
        self.root.mkdir(parents=True, mode=0o700)
        self.context = multiprocessing.get_context("spawn")
        self.clock = SharedClock(self.context.Value("d", time.time()))
        self.options = {"configuration_at": self.clock(), **options}
        if options.get("overlap_keys"):
            from capacity_sources_fixture import limit
            def group(name, kind, ref):
                return {"id": name, "scope": {"kind": kind, "ref": ref},
                    "source": {"kind": "controlled_import", "version": "synthetic-process-reviewed-v1"},
                    "supplier_limits": [], "local_limits": [limit("rpm", options.get("rpm", 10), self.clock(), expires=7200),
                        limit("tpm", None, self.clock(), status="not_applicable", expires=7200),
                        limit("concurrency", 1, self.clock(), expires=7200)]}
            configuration = {"schema_version": 1, "version": "synthetic-admission-config-v1",
                "groups": [group("shared", "account", "synthetic-shared-account"),
                    group("key-a", "credential", "synthetic-upstream-key"),
                    group("key-b", "credential", "synthetic-other-key")],
                "bindings": [{"id": name, "provider": "openrouter", "account_ref": "synthetic-shared-account",
                    "credential_ref": key, "endpoint_ref": "openrouter-chat", "groups": ["shared", group_id]}
                    for name, key, group_id in (("synthetic-binding", "synthetic-upstream-key", "key-a"),
                        ("synthetic-disjoint-binding", "synthetic-other-key", "key-b"))]}
            path = self.root / "synthetic-queue-configuration.json"
            path.write_text(json.dumps(configuration))
            path.chmod(0o600)
        self.first_mode = first_mode
        self.workers, self.receipts, self.http_records, self.observations = [], [], [], []
        self.pending, self.send_locks = {}, {}
        self.stack = ExitStack()

    def _record(self, index, value):
        self.receipts.append({"worker": index, **{key: item for key, item in value.items()
            if key not in {"credential", "credentials", "operator_token"}}})

    def start(self, index, mode="normal"):
        from capacity_queue_process_worker import serve
        parent, child = self.context.Pipe()
        stop, release = self.context.Event(), self.context.Event()
        process = self.context.Process(target=serve, args=(self.assemble, str(self.root), index,
            self.clock, self.options, mode, child, stop, release))
        process.start()
        child.close()
        try:
            assert parent.poll(15), "Independent queue HTTP worker did not start"
            ready = parent.recv()
            self._record(index, ready)
            assert ready.get("event") == "ready", ready
            for existing in self.workers:
                assert ready["queue_path"] == existing.ready["queue_path"]
                assert ready["service_root"] == existing.ready["service_root"]
                assert ready["pid"] != existing.ready["pid"]
                if not existing.connection.closed:
                    assert ready["origin"] != existing.ready["origin"]
        except BaseException:
            process.join(2)
            if process.is_alive():
                process.terminate()
                process.join(5)
            self.receipts.append({"phase": "setup_failed", "pid": process.pid,
                "exitcode": process.exitcode})
            (self.root / "setup-failed.json").write_text(json.dumps(self.receipts, indent=2))
            parent.close()
            raise
        self.network.ports.add(int(ready["origin"].rsplit(":", 1)[1]))
        worker = Worker(process, parent, stop, release, ready, mode)
        if index < len(self.workers):
            assert self.workers[index].connection.closed, "Cannot replace a live owned worker"
            self.workers[index] = worker
        else:
            assert index == len(self.workers)
            self.workers.append(worker)
        self.pending[index], self.send_locks[index] = [], threading.Lock()
        return worker

    def __enter__(self):
        try:
            self.start(0, self.first_mode)
            self.start(1)
        except BaseException as error:
            self.__exit__(type(error), error, error.__traceback__)
            raise
        return self

    def receive(self, index, event, *, command_id=None, timeout=10):
        worker = self.workers[index]
        deadline = time.monotonic() + timeout

        def matches(value):
            return value.get("event") == event and (command_id is None
                or value.get("command_id") == command_id)

        for position, value in enumerate(self.pending[index]):
            if matches(value):
                return self.pending[index].pop(position)
        while time.monotonic() < deadline:
            assert worker.connection.poll(max(0, deadline - time.monotonic())), (
                "Queue worker did not reach " + event)
            value = worker.connection.recv()
            self._record(index, value)
            assert value.get("event") not in {"worker_failed", "dispatch_failed"}, value
            if matches(value):
                return value
            self.pending[index].append(value)
        raise AssertionError("Queue worker did not reach " + event)

    def dispatch(self, index):
        command_id = str(uuid4())
        with self.send_locks[index]:
            self.workers[index].connection.send({"command": "dispatch", "command_id": command_id})
        self.receipts.append({"worker": index, "event": "dispatch_requested", "command_id": command_id})
        return command_id

    def dispatched(self, index, command_id):
        return self.receive(index, "dispatched", command_id=command_id)["result"]

    def entered(self, index):
        return self.receive(index, "entered")

    def release(self, index):
        worker = self.workers[index]
        if worker.process.is_alive() and worker.mode != "kill":
            worker.release_event.set()

    def client(self, index=0, *, operator=False, principal="studio", timeout=10):
        ready = self.workers[index].ready
        token = ready["operator_token"] if operator else ready["credentials"][principal]

        def record(response):
            response.read()
            request = response.request
            self.http_records.append({"worker": index, "method": request.method,
                "path": request.url.path, "query": request.url.query.decode(), "port": request.url.port,
                "request": request.content.decode(),
                "request_sha256": hashlib.sha256(request.content).hexdigest(),
                "status": response.status_code, "response": response.text,
                "response_sha256": hashlib.sha256(response.content).hexdigest()})

        return self.stack.enter_context(httpx.Client(base_url=ready["origin"],
            headers={"Authorization": "Bearer " + token}, timeout=timeout, trust_env=False,
            event_hooks={"response": [record]}))

    def activate(self):
        response = self.client(operator=True).post("/monitor/capacity/activate",
            json={"revision": 0, "version": "synthetic-queued-admission-v1"})
        assert response.status_code == 200, response.text
        return response.json()

    def view(self, path, index=0):
        response = self.client(index, operator=True).get(path)
        assert response.status_code == 200, response.text
        self.observe(path, response.json())
        return response.json()

    def capacity(self, index=0):
        return self.view("/monitor/capacity", index)["dispatch"]

    def mode(self, principal, mode):
        revision = self.view("/monitor/snapshot")["control"]["revision"]
        response = self.client(operator=True).post("/monitor/mode",
            json={"revision": revision, "client": principal, "mode": mode})
        assert response.status_code == 200, response.text
        return response.json()

    @staticmethod
    def group(capacity, identifier):
        return next((group for group in capacity["groups"] if group["group"] == identifier),
            {"rpm_used": 0, "tpm_reserved": 0, "concurrency_used": 0})

    def ledgers(self):
        rows = {}
        for path in sorted(self.root.glob("budget*.sqlite")):
            with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
                db.row_factory = sqlite3.Row
                rows[path.name] = {table: [dict(row) for row in db.execute("SELECT * FROM " + table + " ORDER BY 1")]
                    for table in ("reservations", "events", "violations")}
        self.observe("raw_original_fee_ledgers", rows)
        return rows

    def receipt_hashes(self):
        return {str(path.relative_to(self.root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(self.root.rglob("*.json")) if path.name in {"intent.json", "response.json"}}

    def retention_tasks(self):
        path = Path(self.workers[0].ready["service_root"]) / ".retention/lifecycle.sqlite3"
        with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
            db.row_factory = sqlite3.Row
            rows = [dict(row) for row in db.execute("SELECT * FROM tasks ORDER BY id")]
        self.observe("original_retention_tasks", rows)
        return rows

    def calls(self, index=None):
        rows = []
        for worker_index in range(len(self.workers)) if index is None else (index,):
            path = self.root / ("queue-wire-" + str(worker_index) + ".jsonl")
            if path.exists():
                rows.extend(json.loads(line) for line in path.read_text().splitlines())
        return rows

    def protected_hashes(self):
        paths = {Path(path) for worker in self.workers for path in worker.ready["protected_paths"]}
        files = {child for path in paths for child in (path.rglob("*") if path.is_dir() else (path,))
            if child.is_file() and ".retention" not in child.parts}
        # Original retention guards record a monotonic clock even on replay.
        # Its immutable task rows are compared separately by retention_tasks().
        return {str(path.relative_to(self.root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(files)}

    def observe(self, name, value):
        self.observations.append({"name": name, "clock": self.clock(), "value": value})

    def finish(self, index, *, kill=False):
        worker = self.workers[index]
        if not kill:
            self.release(index)
            worker.stop.set()
            worker.process.join(5)
        if kill or worker.process.is_alive():
            worker.process.terminate()
            worker.process.join(5)
        assert not worker.process.is_alive(), "Owned queue worker did not exit"
        while worker.connection.poll():
            try:
                self._record(index, worker.connection.recv())
            except EOFError:
                break
        self.receipts.append({"worker": index, "pid": worker.process.pid,
            "exitcode": worker.process.exitcode, "deliberate_kill": kill})
        worker.connection.close()
        assert kill or worker.process.exitcode == 0, "Queue worker required unexpected termination"
        assert all(row.get("blocked_socket_attempts", []) == [] for row in self.receipts)

    def __exit__(self, kind, value, traceback):
        cleanup_errors = []
        for index, worker in enumerate(self.workers):
            if not worker.connection.closed:
                try:
                    deliberate_cleanup = kind is not None and worker.mode == "kill"
                    if deliberate_cleanup:
                        self.receipts.append({"worker": index, "event": "failed_test_kill_worker_cleanup"})
                    self.finish(index, kill=deliberate_cleanup)
                except AssertionError as error:
                    cleanup_errors.append(str(error))
        self.stack.close()
        inventory = [{"path": str(path.relative_to(self.root)), "size": path.stat().st_size,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
            for path in sorted(self.root.rglob("*")) if path.is_file()]
        receipt = {"scope": "Synthetic independent HTTP processes; real shared queue and ledgers; MockTransport upstream",
            "options": self.options, "workers": self.receipts, "http_responses": self.http_records,
            "observations": self.observations, "cleanup_errors": cleanup_errors,
            "actual_wire_requests": self.calls(),
            "connected_socket_count": len(self.network.connected),
            "blocked_socket_attempts": [str(address) for address in self.network.blocked],
            "failure_type": None if kind is None else kind.__name__, "artifacts": inventory}
        (self.root / "process-evidence.json").write_text(json.dumps(receipt, indent=2, allow_nan=False))
        if cleanup_errors and kind is None:
            raise AssertionError("; ".join(cleanup_errors))


def publish_test_receipt(request, pairs, network):
    """Index raw process evidence without copying credentials into test output."""
    destination = os.environ.get("CLOUDYFAN_MONITOR_EVIDENCE_DIR")
    if destination:
        output = Path(destination)
        output.mkdir(parents=True, exist_ok=True)
        (output / (request.node.name + ".json")).write_text(json.dumps({
            "test": request.node.name, "pairs": [str(pair.root / "process-evidence.json") for pair in pairs],
            "allowed_loopback_ports": sorted(network.ports),
            "connected_socket_count": len(network.connected),
            "blocked_socket_attempts": [str(address) for address in network.blocked]}, indent=2))
    assert not network.blocked
    if any(pair.workers for pair in pairs):
        assert network.connected


@pytest.fixture
def queue_process_pair(tmp_path, loopback_network, request):
    pairs = []

    @contextmanager
    def start(**options):
        pair = QueueProcessPair(tmp_path / ("pair-" + str(len(pairs))), loopback_network,
            assemble_queue_host, **options)
        pairs.append(pair)
        with pair:
            yield pair

    yield start
    publish_test_receipt(request, pairs, loopback_network)

"""Actual independent HTTP hosts sharing response windows, with synthetic upstreams."""
from contextlib import contextmanager
from datetime import datetime, timezone
from email.utils import format_datetime
import hashlib
import json
import os
from pathlib import Path

import httpx
import pytest

from cloudyfan_jev.capacity_dispatch import CapacityController
from cloudyfan_jev.capacity_windows import CapacityWindows
from cloudyfan_monitor.capacity import CapacityObserver
from cloudyfan_monitor.integration import assemble_monitor
from capacity_admission_process_fixture import (
    CONFIGURATION, POLICY as ADMISSION_POLICY, ProcessPair, Worker, build_process_bundle,
)

POLICY = "synthetic-process-windows-admission-v1"
WINDOWS = "synthetic-process-windows-v1"


def build_window_process_bundle(root, index, clock, *, origin, mode, before_post, **options):
    bundle, clients = build_process_bundle(root, index, clock, origin=origin,
        fee_post_guard=before_post if mode == "prepared" else None, **options)
    calls = Path(root) / ("wire-" + str(index) + ".jsonl")

    def upstream(request):
        observed = {"pid": os.getpid(), "index": index, "method": request.method,
            "url": str(request.url), "synthetic_clock_at_mock_transport": clock(),
            "wire_sha256": hashlib.sha256(request.content).hexdigest(),
            "content": request.content.decode(), "timeout": request.extensions.get("timeout")}
        with calls.open("a") as stream:
            stream.write(json.dumps(observed) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        response = bundle.upstream(request)
        body = response.json()
        body["id"] = "gen-synthetic-window-" + str(index) + "-" + str(os.getpid()) + "-" + str(len(bundle.upstream.calls))
        if mode in {"rate", "rate-date", "invalid"} and len(bundle.upstream.calls) == 1:
            body["choices"] = []
            body["error"] = {"code": 429, "message": "SYNTHETIC_PRIVATE_PROCESS_WINDOW_ERROR"}
            now = datetime.fromtimestamp(clock(), timezone.utc).replace(microsecond=0)
            value = (format_datetime(datetime.fromtimestamp(int(clock()) + 30, timezone.utc), usegmt=True)
                if mode == "rate-date" else "not-a-time" if mode == "invalid" else "30")
            return httpx.Response(429, json=body, headers={"Retry-After": value,
                "Date": format_datetime(now, usegmt=True),
                "X-Private-Window": "SYNTHETIC_PRIVATE_PROCESS_WINDOW_HEADER"})
        return httpx.Response(response.status_code, json=body, headers=response.headers)

    for client in clients.values():
        client.http.close()
        client.http = httpx.Client(transport=httpx.MockTransport(upstream), trust_env=False)
    old = bundle.app
    original = old.capacity_controller
    disjoint = options.get("disjoint", False)
    windows = CapacityWindows(original.store, version=WINDOWS, rules=[
        {"binding": "binding-" + key, "group": "account-" + (key if disjoint else "shared"),
         "header": "retry-after", "format": "retry_after", "constraint": "requests"}
        for key in ("a", "b")])
    controller = CapacityController(original.store, credential_active=original.credential_active,
        window_backend=windows)
    controller.register(POLICY, configuration_version=CONFIGURATION,
        routes=original.routes[ADMISSION_POLICY], max_seconds=1800, windows_version=WINDOWS)
    bundle.app = assemble_monitor(old.store, old.gateway.service, old.gateway.registry, old.identity_store,
        operator_token=bundle.operator_token, source=old.source, origin=origin, demo=True,
        capacity_observer=CapacityObserver(original.store, sources={}), capacity_controller=controller)
    return bundle, clients


class WindowProcessPair(ProcessPair):
    def __init__(self, root, network, *, second_mode="normal", **options):
        super().__init__(root, network, first_mode=options.pop("first_mode", "rate"),
            concurrency=2, **options)
        self.second_mode = second_mode

    def start(self, index, mode):
        from capacity_windows_process_worker import serve
        parent, child = self.context.Pipe()
        stop, release = self.context.Event(), self.context.Event()
        process = self.context.Process(target=serve,
            args=(str(self.root), index, self.clock, self.options, mode, child, stop, release))
        process.start()
        child.close()
        try:
            assert parent.poll(15), "Independent window HTTP worker did not start"
            ready = parent.recv()
            assert ready.get("ready"), ready
        except BaseException:
            process.join(2)
            if process.is_alive():
                process.terminate()
                process.join(5)
            self.receipts.append({"phase": "setup_failed", "pid": process.pid, "exitcode": process.exitcode})
            (self.root / "setup-failed.json").write_text(json.dumps(self.receipts))
            parent.close()
            raise
        self.network.ports.add(int(ready["origin"].rsplit(":", 1)[1]))
        worker = Worker(process, parent, stop, release, ready, mode)
        if index < len(self.workers):
            self.workers[index] = worker
        else:
            self.workers.append(worker)
        return worker

    def __enter__(self):
        try:
            self.start(0, self.first_mode)
            self.start(1, self.second_mode)
        except BaseException as error:
            self.__exit__(type(error), error, error.__traceback__)
            raise
        return self

    def activate(self):
        response = self.client(operator=True).post("/monitor/capacity/activate",
            json={"revision": 0, "version": POLICY})
        assert response.status_code == 200, response.text
        assert self.capacity(index=1)["active_version"] == POLICY

    def durable_hashes(self):
        paths = [self.root / "capacity.sqlite", *(self.root / "budgets").glob("*.sqlite")]
        paths += [path for path in (self.root / "receipts").rglob("*.json")]
        paths += [path for path in self.root.glob("frontend-*/health*.sqlite")]
        return {str(path.relative_to(self.root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(paths) if path.is_file()}

    def __exit__(self, kind, value, traceback):
        try:
            super().__exit__(kind, value, traceback)
        finally:
            path = self.root / "process-evidence.json"
            if path.exists():
                record = json.loads(path.read_bytes())
                record["registration"]["policy_version"] = POLICY
                record["registration"]["windows_version"] = WINDOWS
                record["scope"] = "synthetic response windows; actual independent same-host HTTP; MockTransport only"
                path.write_text(json.dumps(record, indent=2))


@pytest.fixture
def window_process_pair(tmp_path, loopback_network, request):
    pairs = []

    @contextmanager
    def start(**options):
        pair = WindowProcessPair(tmp_path / ("window-pair-" + str(len(pairs))), loopback_network, **options)
        pairs.append(pair)
        with pair:
            yield pair

    yield start
    destination = os.environ.get("CLOUDYFAN_MONITOR_EVIDENCE_DIR")
    if destination:
        output = Path(destination)
        output.mkdir(parents=True, exist_ok=True)
        (output / (request.node.name + ".json")).write_text(json.dumps({
            "test": request.node.name, "pairs": [str(pair.root / "process-evidence.json") for pair in pairs],
            "allowed_loopback_ports": sorted(loopback_network.ports),
            "connected_socket_count": len(loopback_network.connected),
            "blocked_socket_attempts": [str(address) for address in loopback_network.blocked]}, indent=2))
    assert not loopback_network.blocked
    if any(pair.workers for pair in pairs):
        assert loopback_network.connected

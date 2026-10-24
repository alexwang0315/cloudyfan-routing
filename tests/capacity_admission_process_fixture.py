"""Synthetic same-host multi-key fixture; no production token or supplier claims."""
from contextlib import contextmanager, ExitStack
from copy import copy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import time

import httpx
import pytest

from cloudyfan_jev.budget import Authorization, BudgetedCore, BudgetStore
from cloudyfan_jev.capacity import CapacityStore
from cloudyfan_jev.capacity_dispatch import CapacityController, CapacityRoute, TokenUpperBound
from cloudyfan_jev.client import CHAT, DECISIONS, Client
from cloudyfan_monitor.capacity import CapacityObserver
from cloudyfan_monitor.integration import assemble_monitor, build_local_integration
from capacity_sources_fixture import limit

POLICY = "synthetic-process-admission-v1"
CONFIGURATION = "synthetic-process-capacity-v1"


@dataclass
class SharedClock:
    value: object

    def __call__(self):
        return self.value.value

    def advance(self, seconds):
        with self.value.get_lock():
            self.value.value += seconds


class SyntheticTokenEnvelope:
    """The injected MockTransport returns at most 1000 tokens per request."""
    version = "synthetic-process-envelope-v1"

    def __init__(self, clock):
        self.clock = clock

    def __call__(self, content, scope):
        return TokenUpperBound(1000, hashlib.sha256(content).hexdigest(), scope["scope_sha256"],
            self.version, "synthetic-process-envelope-not-production", self.clock() + 3600)


class DeniedAtPost:
    """Server-registered synthetic fee contract withdraws before physical POST."""
    def __call__(self, *args):
        return True

    def verify_dispatch(self, *args):
        return True

    def verify_post(self, *args):
        return False


class HeldAtPost(DeniedAtPost):
    """Hold the injected original fee guard after capacity preparation."""
    def __init__(self, callback):
        self.callback = callback

    def verify_post(self, *args):
        self.callback()
        return True


def build_process_bundle(root, index, clock, *, origin, disjoint=False, shared_budget=False,
                         deny_before_post=False, before_response=None, configuration_at=None,
                         fee_post_guard=None, rpm=100, concurrency=1):
    """Every worker registers the same two transports, bindings and host policy.

    Frontend stores/locks and keys/receipt roots differ deliberately: those locks
    must not impersonate capacity admission. Shared fee mode retains BudgetStore's
    original global unknown-cost stop; independent mode uses separately authorized
    accounts rather than weakening that stop.
    """
    root = Path(root)
    configured = clock() if configuration_at is None else configuration_at
    bundle = build_local_integration(root / ("frontend-" + str(index)), origin=origin, clock=clock)
    bundle.upstream.fail_tiers = set()
    bundle.upstream.before_response = before_response
    calls = root / ("wire-" + str(index) + ".jsonl")

    def upstream(request):
        observed = {"pid": os.getpid(), "method": request.method, "url": str(request.url),
            "synthetic_clock_at_mock_transport": clock(),
            "wire_sha256": hashlib.sha256(request.content).hexdigest(),
            "content": request.content.decode(), "timeout": request.extensions.get("timeout")}
        with calls.open("a") as stream:
            stream.write(json.dumps(observed) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        return bundle.upstream(request)

    clients = {suffix: Client(httpx.Client(transport=httpx.MockTransport(upstream), trust_env=False),
        "synthetic-process-upstream-" + suffix + "-not-real", root / "receipts" / suffix)
        for suffix in ("a", "b")}
    suffix = ("a", "b")[index]
    old = bundle.app
    template = next(iter(old.gateway.service.contracts.values())).runner
    core = copy(template.core)
    core.client = clients[suffix]
    budget = BudgetStore(root / "budgets" / ("shared.sqlite" if shared_budget else suffix + ".sqlite"))
    if budget.report()["authorization"] is None:
        budget.authorize(Authorization("synthetic-process-fee-account-" + ("shared" if shared_budget else suffix),
            Decimal("100"), (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()))
    verifier = (HeldAtPost(fee_post_guard) if fee_post_guard is not None else
        DeniedAtPost() if deny_before_post else template.verify_bound)
    runner = BudgetedCore(core, budget, model_bounds=template.model_bounds,
        tool_bounds=template.tool_bounds, verify_bound=verifier)
    from dataclasses import replace
    old.gateway.service.contracts = {key: replace(contract, runner=runner)
        for key, contract in old.gateway.service.contracts.items()}

    group_ids = ["account-a", "account-b"] if disjoint else ["account-shared"]
    group_ids += ["key-a", "key-b"]
    groups = [{"id": identifier, "scope": {"kind": "credential" if identifier.startswith("key-") else "account",
            "ref": "synthetic-process-" + identifier},
        "source": {"kind": "controlled_import", "version": "synthetic-process-reviewed-v1"},
        "supplier_limits": [], "local_limits": [limit("rpm", rpm, configured, expires=7200),
            limit("tpm", 10000, configured, expires=7200), limit("concurrency", concurrency, configured, expires=7200)]}
        for identifier in group_ids]
    bindings = [{"id": "binding-" + key, "provider": "openrouter",
        "account_ref": "synthetic-process-account-" + (key if disjoint else "shared"),
        "credential_ref": "synthetic-process-key-" + key,
        "endpoint_ref": "openrouter-chat-and-decisions",
        "groups": ["key-" + key, "account-" + (key if disjoint else "shared")]}
        for key in ("a", "b")]
    store = CapacityStore(root / "capacity.sqlite", clock=clock)
    store.configure({"schema_version": 1, "version": CONFIGURATION, "bindings": bindings, "groups": groups})
    identity = old.identity_store

    def credential_active(principal, credential, scope, now):
        return identity.is_active(principal) and any(row["id"] == credential and row["revoked_at"] is None
            and row["created_at"] <= now < row["expires_at"] and scope in row["scopes"]
            for row in identity.credentials(principal))

    controller = CapacityController(store, credential_active=credential_active)
    routes = []
    for key, client in clients.items():
        routes.extend(CapacityRoute(client, CHAT, profile["model_version"], "binding-" + key,
            token_adapter=SyntheticTokenEnvelope(clock), provider_name=profile.get("provider_name"))
            for profile in core.profiles.values())
        routes.append(CapacityRoute(client, DECISIONS, core.decision["version"], "binding-" + key,
            token_adapter=SyntheticTokenEnvelope(clock), provider_name=core.decision.get("provider_name")))
    controller.register(POLICY, configuration_version=CONFIGURATION, routes=routes, max_seconds=1800)
    bundle.app = assemble_monitor(old.store, old.gateway.service, old.gateway.registry, identity,
        operator_token=bundle.operator_token, source=old.source, origin=origin, demo=True,
        capacity_observer=CapacityObserver(store, sources={}), capacity_controller=controller)
    return bundle, clients


@dataclass
class Worker:
    process: object
    connection: object
    stop: object
    release_event: object
    ready: dict
    mode: str


class ProcessPair:
    def __init__(self, root, network, *, disjoint=False, shared_budget=False, first_mode="hold",
                 rpm=100, concurrency=1):
        self.root, self.network = Path(root), network
        self.root.mkdir(parents=True, mode=0o700)
        self.context = multiprocessing.get_context("spawn")
        self.clock = SharedClock(self.context.Value("d", time.time()))
        self.options = {"disjoint": disjoint, "shared_budget": shared_budget, "configuration_at": self.clock(),
            "rpm": rpm, "concurrency": concurrency}
        self.workers, self.receipts, self.http_records, self.observations = [], [], [], []
        self.stack = ExitStack()
        self.first_mode = first_mode

    def start(self, index, mode):
        from capacity_admission_process_worker import serve
        parent, child = self.context.Pipe()
        stop, release = self.context.Event(), self.context.Event()
        process = self.context.Process(target=serve,
            args=(str(self.root), index, self.clock, self.options, mode, child, stop, release))
        process.start()
        child.close()
        try:
            assert parent.poll(15), "Independent HTTP worker did not start"
            ready = parent.recv()
            assert ready.get("ready"), ready
        except BaseException:
            process.join(2)
            if process.is_alive():
                process.terminate()
                process.join(5)
            self.receipts.append({"phase": "setup_failed", "pid": process.pid,
                "exitcode": process.exitcode})
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
            self.start(1, "normal")
        except BaseException as error:
            self.__exit__(type(error), error, error.__traceback__)
            raise
        return self

    def client(self, index=0, *, operator=False, timeout=5):
        ready = self.workers[index].ready
        token = ready["operator_token"] if operator else ready["credential"]

        def record(response):
            self.http_records.append({"method": response.request.method, "path": response.request.url.path,
                "port": response.request.url.port, "status": response.status_code})

        return self.stack.enter_context(httpx.Client(base_url=ready["origin"],
            headers={"Authorization": "Bearer " + token}, timeout=timeout, trust_env=False,
            event_hooks={"response": [record]}))

    def activate(self):
        response = self.client(operator=True).post("/monitor/capacity/activate",
            json={"revision": 0, "version": POLICY})
        assert response.status_code == 200, response.text
        assert self.capacity(index=1)["active_version"] == POLICY

    def capacity(self, index=0):
        response = self.client(index, operator=True).get("/monitor/capacity")
        assert response.status_code == 200, response.text
        self.observations.append({"kind": "capacity", "index": index, "response": response.json()})
        return response.json()["dispatch"]

    def snapshot(self, index=0):
        response = self.client(index, operator=True).get("/monitor/snapshot")
        assert response.status_code == 200, response.text
        self.observations.append({"kind": "monitor_snapshot", "index": index, "response": response.json()})
        return response.json()

    @staticmethod
    def group(capacity, identifier):
        return next((group for group in capacity["groups"] if group["group"] == identifier),
            {"rpm_used": 0, "tpm_reserved": 0, "concurrency_used": 0})

    def entered(self, index):
        worker = self.workers[index]
        assert worker.connection.poll(10), "Controlled request phase was not reached"
        receipt = worker.connection.recv()
        assert receipt.get("entered"), receipt
        self.receipts.append(receipt)
        return receipt

    def release(self, index):
        worker = self.workers[index]
        if worker.process.is_alive() and worker.mode != "kill":
            worker.release_event.set()

    def calls(self, index):
        path = self.root / ("wire-" + str(index) + ".jsonl")
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def receipt_hashes(self, index):
        root = self.root / "receipts" / ("a", "b")[index] / "calls"
        return {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(root.glob("*/*.json")) if path.name in {"intent.json", "response.json"}}

    def finish(self, index, *, kill=False):
        worker = self.workers[index]
        if not kill:
            self.release(index)
            worker.stop.set()
            worker.process.join(5)
        if kill or worker.process.is_alive():
            worker.process.terminate()
            worker.process.join(5)
        assert not worker.process.is_alive(), "Owned synthetic worker did not exit"
        while worker.connection.poll():
            try:
                self.receipts.append(worker.connection.recv())
            except EOFError:
                break
        self.receipts.append({"pid": worker.process.pid, "exitcode": worker.process.exitcode,
            "deliberate_kill": kill})
        worker.connection.close()
        assert kill or worker.process.exitcode == 0, "Synthetic worker required unexpected termination"
        assert all(row.get("blocked_socket_attempts", []) == [] for row in self.receipts), "Worker attempted forbidden network"

    def __exit__(self, kind, value, traceback):
        cleanup_errors = []
        for index, worker in enumerate(self.workers):
            if not worker.connection.closed:
                try:
                    self.finish(index)
                except AssertionError as error:
                    cleanup_errors.append(str(error))
        self.stack.close()
        inventory = [{"path": str(path.relative_to(self.root)), "size": path.stat().st_size,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
            for path in sorted(self.root.rglob("*")) if path.is_file()]
        receipt = {"scope": "synthetic same-host independent HTTP processes; models MockTransport only",
            "registration": {"configuration_version": CONFIGURATION, "policy_version": POLICY, **self.options},
            "workers": self.receipts, "http_responses": self.http_records,
            "observations": self.observations,
            "cleanup_errors": cleanup_errors,
            "actual_wire_requests": [row for index in (0, 1) for row in self.calls(index)],
            "blocked_socket_attempts": [str(address) for address in self.network.blocked], "artifacts": inventory}
        (self.root / "process-evidence.json").write_text(json.dumps(receipt, indent=2))
        if cleanup_errors and kind is None:
            raise AssertionError("; ".join(cleanup_errors))


@pytest.fixture
def process_pair(tmp_path, loopback_network, request):
    pairs = []

    @contextmanager
    def start(**options):
        pair = ProcessPair(tmp_path / ("pair-" + str(len(pairs))), loopback_network, **options)
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

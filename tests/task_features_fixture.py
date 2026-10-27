"""Real HTTP creation-feature tracer; only upstreams and host policy are synthetic."""
from contextlib import ExitStack
from dataclasses import dataclass, field
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import platform
import sqlite3
import threading
from types import MappingProxyType

import httpx
import pytest

from capacity_resilience_fixture import build_resilience_health_host
from test_local_http_integration import Clock, QuietHandler, ThreadedServer


POLICY_BUNDLE = MappingProxyType({
    "feature_version": "local-deterministic-v1",
    "data_policy_version": "creation-feature-data-v1",
    "max_input_utf8_bytes": 32768,
    "max_feature_items": 16,
    "max_reasons_per_item": 4,
    "max_reason_code_characters": 80,
    "max_record_bytes": 16384,
    "max_compute_milliseconds": 50,
})


def _save(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")
    path.chmod(0o600)


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@dataclass(frozen=True)
class StoreFeatureBindingPort:
    """Proposed PUBLIC host port: lazily call the real Store, never fabricate features.

    The baseline does not consume creation_features and therefore never calls
    this port. The future implementation must supply these Store capabilities;
    no missing-method fallback or fake recorder is installed by this fixture.
    """
    store: object

    def snapshot(self, principal, run_id):
        return self.store.feature_binding(principal, run_id)

    def seal(self, principal, run_id, adoption_sha, registered_sha, record_sha):
        return self.store.seal_features(principal, run_id, adoption_sha, registered_sha, record_sha)


@dataclass
class TaskFeaturesHTTP:
    controlled: object
    origin: str
    clock: Clock
    stack: ExitStack
    artifacts: Path
    evidence: dict
    clients: dict = field(default_factory=dict)
    server: object = None

    def dispatch(self):
        self.evidence['worker_dispatch_called'] = True
        return self.controlled.queue.dispatch_next()

    def reopen_disabled(self):
        """Actual new host objects on the same original stores, capture disabled."""
        self.controlled.close()
        controlled = build_resilience_health_host(self.controlled.host.root, clock=self.clock,
            origin=self.origin, held_timeout_seconds=600)
        self.controlled = controlled
        self.server.set_app(controlled.app)
        def close_reopened():
            controlled.close()
            self.evidence['reopened_fixture_close_returned'] = True
        self.stack.callback(close_reopened)
        self.evidence['actual_host_reopened_with_capture_disabled'] = controlled.host.service.creation_features is None

    def client(self, principal):
        if principal not in self.clients:
            credential = (self.controlled.operator_token if principal == "operator"
                          else self.controlled.credentials[principal].token)

            def record(response):
                response.read()
                number = len(self.evidence["http_responses"]) + 1
                request_path = self.artifacts / f"http-{number:02d}-request.bin"
                response_path = self.artifacts / f"http-{number:02d}-response.bin"
                request_path.write_bytes(response.request.content)
                response_path.write_bytes(response.content)
                request_path.chmod(0o600)
                response_path.chmod(0o600)
                self.evidence["http_responses"].append({
                    "method": response.request.method,
                    "path": response.request.url.path,
                    "port": response.request.url.port,
                    "status": response.status_code,
                    "request_body": request_path.name,
                    "request_sha256": _sha(request_path),
                    "response_body": response_path.name,
                    "response_sha256": _sha(response_path),
                    "authorization_recorded": False,
                })

            self.clients[principal] = self.stack.enter_context(httpx.Client(
                base_url=self.origin, trust_env=False, timeout=5,
                headers={"Authorization": "Bearer " + credential},
                event_hooks={"response": [record]}))
        return self.clients[principal]

    def snapshot(self, name):
        controlled = self.controlled
        root = controlled.host.root
        budgets = {lane: store.report() for lane, store in controlled.budgets.items()}
        _save(self.artifacts / (name + "-budget-reports.json"), budgets)
        result = {
            "model_requests": list(controlled.requests),
            "source_requests": list(controlled.side_requests["source"]),
            "accounting_requests": list(controlled.side_requests["accounting"]),
            "budget": {lane: {key: report.get(key) for key in
                       ("known_usd", "reserved_usd", "unknown_events")}
                       for lane, report in budgets.items()},
            "health_path": str(controlled.host.health.path.relative_to(root)),
            "health_sha256": _sha(controlled.host.health.path),
            "budget_sqlite_sha256": {lane: _sha(store.path)
                                     for lane, store in controlled.budgets.items()},
            "capacity_sha256": _sha(controlled.controller.store.path),
            "queue_sha256": _sha(controlled.queue.path),
            "mock_held_response_started": controlled.held.is_set(),
            "raw_inventory_scope": "JSON/BIN evidence; original SQLite hashes are recorded separately",
            "raw_files_sha256": {str(path.relative_to(root)): _sha(path)
                                 for path in sorted(root.rglob("*"))
                                 if path.suffix in {".json", ".bin"} and path.is_file()},
        }
        _save(self.artifacts / (name + "-observation.json"), result)
        return result


@pytest.fixture
def task_features_http(tmp_path, loopback_network, request):
    destination = os.environ.get("CLOUDYFAN_MONITOR_EVIDENCE_DIR")
    evidence_root = Path(destination) if destination else tmp_path / "http-evidence"
    evidence_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    artifacts = evidence_root / (request.node.name + "-artifacts")
    artifacts.mkdir(mode=0o700)
    evidence = {
        "schema_version": 1,
        "test": request.node.name,
        "scope": "Real loopback HTTP/Core/Budget/Identity/Health/Queue; MockTransport only; no worker dispatched",
        "phase": "setup",
        "http_responses": [],
        "upstream_wire": [],
        "pytest_pid": os.getpid(),
        "child_processes_created": [],
        "python": platform.python_version(),
        "sqlite": sqlite3.sqlite_version,
        "host_configuration": {"policy_bundle": dict(POLICY_BUNDLE),
                               "interface": "PUBLIC TaskService.creation_features",
                               "baseline_product_support_claimed": False},
    }
    controlled = server = thread = tracer = None
    stack = ExitStack()
    try:
        server = ThreadedServer(("127.0.0.1", 0), QuietHandler)
        stack.callback(server.server_close)
        loopback_network.ports.add(server.server_port)
        origin = f"http://127.0.0.1:{server.server_port}"
        clock = Clock(datetime.fromisoformat("2026-10-21T00:00:00+00:00").timestamp())
        def record_model(wire):
            number = len(evidence['upstream_wire']) + 1
            body, headers = artifacts / f'upstream-{number:02d}-body.bin', artifacts / f'upstream-{number:02d}-headers.bin'
            body.write_bytes(wire.content)
            headers.write_bytes(b'\r\n'.join(key + b': ' + value for key, value in wire.headers.raw))
            evidence['upstream_wire'].append({'method': wire.method, 'url': str(wire.url),
                'body': body.name, 'body_sha256': _sha(body), 'headers': headers.name, 'headers_sha256': _sha(headers)})
        controlled = build_resilience_health_host(artifacts / "service", clock=clock,
                                                 origin=origin, held_timeout_seconds=600, before_response=record_model)
        def close_fixture():
            controlled.close()
            evidence["fixture_close_returned"] = True
        stack.callback(close_fixture)
        # This is the declared new host configuration contract, not a recorder
        # mock. On the baseline it is simply unused, leaving real HTTP behavior.
        policy_bundle = dict(POLICY_BUNDLE)
        if getattr(request, 'param', None):
            policy_bundle.update(request.param)
        evidence['host_configuration']['policy_bundle'] = policy_bundle
        controlled.host.service.creation_features = {
            "policy_bundle": MappingProxyType(policy_bundle),
            "binding_port": StoreFeatureBindingPort(controlled.app.store),
        }
        server.set_app(controlled.app)
        tracer = TaskFeaturesHTTP(controlled, origin, clock, stack, artifacts, evidence)
        tracer.server = server
        evidence["before"] = tracer.snapshot("before-http")
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01},
                                  daemon=True, name="creation-feature-loopback-http")
        thread.start()
        stack.callback(thread.join, 2)
        stack.callback(server.shutdown)
        evidence["server_thread"] = {"ident": thread.ident, "native_id": thread.native_id}
        evidence["phase"] = "actual_http_ready"
        yield tracer
    except BaseException as error:
        if evidence["phase"] == "setup":
            evidence["setup_failure_type"] = type(error).__name__
        raise
    finally:
        try:
            stack.close()
        finally:
            if tracer is not None:
                evidence["after"] = tracer.snapshot("after-http")
            evidence["allowed_loopback_ports"] = sorted(loopback_network.ports)
            evidence["socket_attempts"] = [{"host": address[0], "port": address[1]}
                                           for address in loopback_network.connected]
            evidence["blocked_socket_attempts"] = [str(address) for address in loopback_network.blocked]
            evidence["cleanup"] = {
                "server_thread_exited": thread is None or not thread.is_alive(),
                "server_socket_closed": server is None or server.socket.fileno() == -1,
                "worker_dispatch_called": evidence.get('worker_dispatch_called', False),
                "fixture_close_completed": evidence.get("fixture_close_returned", False),
            }
            _save(artifacts / "fixture-receipt.json", evidence)
            _save(evidence_root / (request.node.name + ".json"), {
                "schema_version": 1, "test": request.node.name,
                "fixture_receipt": str((artifacts / "fixture-receipt.json").relative_to(evidence_root)),
                "fixture_receipt_sha256": _sha(artifacts / "fixture-receipt.json"),
                "actual_http_statuses": [row["status"] for row in evidence["http_responses"]],
                "allowed_loopback_ports": evidence["allowed_loopback_ports"],
                "blocked_socket_attempts": evidence["blocked_socket_attempts"],
                "mock_upstream_call_count": len(controlled.requests) if controlled is not None else None,
                "cleanup": evidence["cleanup"],
            })
        assert thread is None or not thread.is_alive(), "Owned HTTP server thread did not exit"
        assert not loopback_network.blocked, "A connection escaped the test loopback allowlist"


@pytest.fixture
def task_features_pair(task_features_http, loopback_network):
    """Two actual isolated hosts with same client/run/clock, original Mock models."""
    first = task_features_http
    artifacts = first.artifacts / 'off-host-artifacts'
    artifacts.mkdir(mode=0o700)
    evidence = {'http_responses': [], 'upstream_wire': [], 'worker_dispatch_called': False}
    stack = ExitStack()
    server = ThreadedServer(('127.0.0.1', 0), QuietHandler)
    stack.callback(server.server_close)
    loopback_network.ports.add(server.server_port)
    origin = f'http://127.0.0.1:{server.server_port}'
    clock = Clock(first.clock.now)
    def record_model(wire):
        number = len(evidence['upstream_wire']) + 1
        body, headers = artifacts / f'upstream-{number:02d}-body.bin', artifacts / f'upstream-{number:02d}-headers.bin'
        body.write_bytes(wire.content)
        headers.write_bytes(b'\r\n'.join(key + b': ' + value for key, value in wire.headers.raw))
        evidence['upstream_wire'].append({'method': wire.method, 'url': str(wire.url),
            'body': body.name, 'body_sha256': _sha(body), 'headers': headers.name, 'headers_sha256': _sha(headers)})
    controlled = build_resilience_health_host(artifacts / 'service', clock=clock, origin=origin,
                                             held_timeout_seconds=600, before_response=record_model)
    stack.callback(controlled.close)
    server.set_app(controlled.app)
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
    thread.start()
    stack.callback(thread.join, 2)
    stack.callback(server.shutdown)
    second = TaskFeaturesHTTP(controlled, origin, clock, stack, artifacts, evidence)
    second.server = server
    evidence['before'] = second.snapshot('before-off-http')
    try:
        yield first, second
    finally:
        stack.close()
        evidence['after'] = second.snapshot('after-off-http')
        evidence['cleanup'] = {'server_thread_exited': not thread.is_alive(),
            'server_socket_closed': server.socket.fileno() == -1, 'fixture_close_completed': True}
        _save(artifacts / 'pair-fixture-receipt.json', evidence)
        assert not thread.is_alive()

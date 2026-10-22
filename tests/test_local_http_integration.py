"""Approved local HTTP behavior with real sockets and a mocked model upstream."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from dataclasses import dataclass, field, replace
import json
import multiprocessing
import os
from pathlib import Path
import re
import socket
from socketserver import ThreadingMixIn
import threading
import time
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer

import httpx
import pytest

from cloudyfan_jev.demo import ArithmeticEnvironment
from cloudyfan_monitor.demo import CATEGORY


# Captured at collection, before the autouse fixture blocks all socket connects.
_SOCKET_CONNECT = socket.socket.connect
_SOCKET_CONNECT_EX = socket.socket.connect_ex
TASK = {"run_id": "local-http-one", "category": CATEGORY,
        "prompt": "Return only the integer sum of 1 and 1."}


@dataclass
class LoopbackEvidence:
    ports: set = field(default_factory=set)
    connected: list = field(default_factory=list)
    blocked: list = field(default_factory=list)

    def check(self, address):
        allowed = (isinstance(address, tuple) and len(address) == 2
                   and address[0] == "127.0.0.1" and address[1] in self.ports)
        if not allowed:
            self.blocked.append(address)
            raise AssertionError("Only this test's loopback HTTP service may receive socket connections")


def configure_loopback_network(evidence, patch):
    def connect(stream, address):
        evidence.check(address)
        evidence.connected.append(address)
        return _SOCKET_CONNECT(stream, address)

    def connect_ex(stream, address):
        evidence.check(address)
        evidence.connected.append(address)
        return _SOCKET_CONNECT_EX(stream, address)

    def create_connection(address, timeout=socket._GLOBAL_DEFAULT_TIMEOUT,
                          source_address=None, *, all_errors=False):
        evidence.check(address)
        stream = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            if timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:
                stream.settimeout(timeout)
            if source_address is not None:
                if source_address != ("127.0.0.1", 0):
                    raise AssertionError("Unexpected test source address")
                stream.bind(source_address)
            stream.connect(address)
            return stream
        except BaseException:
            stream.close()
            raise

    patch(socket.socket, "connect", connect)
    patch(socket.socket, "connect_ex", connect_ex)
    patch(socket, "create_connection", create_connection)


@pytest.fixture
def loopback_network(monkeypatch, block_external_network):
    evidence = LoopbackEvidence()
    configure_loopback_network(evidence, monkeypatch.setattr)
    return evidence


class QuietHandler(WSGIRequestHandler):
    def log_message(self, format, *args):
        pass


class ThreadedServer(ThreadingMixIn, WSGIServer):
    daemon_threads = True


@dataclass
class Clock:
    now: float = field(default_factory=time.time)

    def __call__(self):
        return self.now


@dataclass
class LocalService:
    bundle: object
    origin: str
    stack: ExitStack
    server: ThreadedServer
    clock: Clock
    http_records: list = field(default_factory=list)
    additional_workers: list = field(default_factory=list)

    def client(self, principal="studio", *, timeout=2, origin=None):
        token = (self.bundle.operator_token if principal == "operator"
                 else self.bundle.credentials[principal].token)

        def record(response):
            observed = {"method": response.request.method, "path": response.request.url.path,
                        "port": response.request.url.port, "status": response.status_code}
            if response.status_code >= 400:
                response.read()
                try:
                    code = response.json().get("error", {}).get("code")
                except (ValueError, AttributeError):
                    code = None
                # Only the bounded public error code is exported, never headers,
                # request payloads, result bodies or provider exception messages.
                if isinstance(code, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,79}", code):
                    observed["error_code"] = code
            self.http_records.append(observed)

        return self.stack.enter_context(httpx.Client(
            base_url=origin or self.origin, headers={"Authorization": "Bearer " + token},
            timeout=timeout, trust_env=False, event_hooks={"response": [record]}))


@pytest.fixture
def local_service(tmp_path, loopback_network, request):
    from cloudyfan_monitor.integration import build_local_integration

    with ExitStack() as stack:
        server = ThreadedServer(("127.0.0.1", 0), QuietHandler)
        stack.callback(server.server_close)
        port = server.server_port
        loopback_network.ports.add(port)
        origin = f"http://127.0.0.1:{port}"
        clock = Clock()
        bundle = build_local_integration(tmp_path / "service", origin=origin, clock=clock, seed=False)
        server.set_app(bundle.app)
        thread = threading.Thread(target=server.serve_forever,
                                  kwargs={"poll_interval": .01}, daemon=True)
        thread.start()
        stack.callback(thread.join, 2)
        stack.callback(server.shutdown)
        service = LocalService(bundle, origin, stack, server, clock)
        try:
            yield service
        finally:
            destination = os.environ.get("CLOUDYFAN_MONITOR_EVIDENCE_DIR")
            if destination:
                directory = Path(destination)
                directory.mkdir(parents=True, exist_ok=True)
                receipt = {
                    "schema_version": 1, "test": request.node.name,
                    "allowed_loopback_ports": sorted(loopback_network.ports),
                    "socket_attempts": [{"host": address[0], "port": address[1]}
                                        for address in loopback_network.connected],
                    "blocked_socket_attempts": [str(address) for address in loopback_network.blocked],
                    "http_responses": service.http_records,
                    "mock_upstream_call_count": len(bundle.upstream.calls),
                    "mock_upstream_calls": [{"model": payload.get("model"),
                                             "kind": "classification" if "questions" in payload else "generation"}
                                            for payload in bundle.upstream.calls],
                    "additional_workers": service.additional_workers,
                    "scope": "isolated synthetic fixture; actual loopback HTTP; mocked model transport",
                }
                (directory / (request.node.name + ".json")).write_text(json.dumps(receipt, indent=2))
            assert not loopback_network.blocked
            assert loopback_network.connected


def test_running_http_task_keeps_reads_responsive_and_rejects_another_client(local_service):
    bundle = local_service.bundle
    bundle.upstream.fail_tiers = set()
    entered, release = threading.Event(), threading.Event()

    def hold_response(request):
        entered.set()
        if not release.wait(10):
            raise AssertionError("Synthetic upstream release timed out")

    bundle.upstream.before_response = hold_response
    first_client = local_service.client(timeout=12)
    observer = local_service.client()
    second_client = local_service.client("agent")
    operator = local_service.client("operator")
    with ThreadPoolExecutor(1) as workers:
        running = workers.submit(first_client.post, "/v1/tasks", json=TASK)
        try:
            assert entered.wait(3), "Task never reached the mocked upstream"
            status = observer.get("/v1/tasks/" + TASK["run_id"])
            assert status.status_code == 200
            assert status.json()["status"] == "running"
            view = operator.get("/monitor/snapshot")
            assert view.status_code == 200
            task = next(row for row in view.json()["tasks"] if row["run"] == TASK["run_id"])
            assert task["status"] == "running"
            changed = operator.post("/monitor/mode", json={
                "revision": view.json()["control"]["revision"], "mode": "fixed_high"})
            assert changed.status_code == 200
            duplicate = observer.post("/v1/tasks", json=TASK)
            assert duplicate.status_code == 200
            assert duplicate.json()["status"] == "running"
            rejected = second_client.post("/v1/tasks", json={**TASK, "run_id": "rejected-while-busy"})
            assert rejected.status_code == 429
            assert rejected.json()["error"]["code"] == "service_busy"
            assert not release.is_set()
        finally:
            release.set()
        assert running.result(timeout=5).json()["status"] == "passed"

    assert len(bundle.upstream.calls) == 1
    repeated = observer.post("/v1/tasks", json=TASK)
    assert repeated.status_code == 200
    assert repeated.json()["status"] == "passed"
    assert len(bundle.upstream.calls) == 1
    result = observer.get("/v1/tasks/" + TASK["run_id"] + "/result")
    assert result.status_code == 200 and result.json()["answer"] == "2"
    own = second_client.get("/v1/monitor")
    assert own.status_code == 200 and own.json()["tasks"] == []


def test_disconnected_http_client_does_not_cancel_task_or_release_execution_slot(local_service):
    bundle = local_service.bundle
    bundle.upstream.fail_tiers = set()
    entered, release = threading.Event(), threading.Event()

    def hold_response(request):
        entered.set()
        if not release.wait(10):
            raise AssertionError("Synthetic upstream release timed out")

    bundle.upstream.before_response = hold_response
    observer = local_service.client()
    second_client = local_service.client("agent")
    body = httpx.Request("POST", local_service.origin + "/v1/tasks", json=TASK).content
    port = int(local_service.origin.rsplit(":", 1)[1])
    request = (f"POST /v1/tasks HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
               f"Authorization: Bearer {bundle.credentials['studio'].token}\r\n"
               f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n"
               "Connection: close\r\n\r\n").encode() + body
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as disconnected:
        disconnected.settimeout(2)
        disconnected.connect(("127.0.0.1", port))
        disconnected.sendall(request)
        try:
            assert entered.wait(3), "Task was not accepted before the client disconnected"
            disconnected.shutdown(socket.SHUT_RDWR)
            disconnected.close()
            status = observer.get("/v1/tasks/" + TASK["run_id"])
            assert status.status_code == 200 and status.json()["status"] == "running"
            rejected = second_client.post("/v1/tasks", json={**TASK, "run_id": "after-disconnect"})
            assert rejected.status_code == 429
            assert len(bundle.upstream.calls) == 1
            assert not release.is_set()
        finally:
            release.set()

    deadline = time.monotonic() + 5
    while True:
        status = observer.get("/v1/tasks/" + TASK["run_id"])
        assert status.status_code == 200
        if status.json()["status"] == "passed":
            break
        assert time.monotonic() < deadline, "Disconnected task did not finish on the server"
        time.sleep(.01)
    # Completed status can be visible while a concurrent final capture owns the
    # nonblocking read guard. Retry only its explicit public task_busy response.
    deadline = time.monotonic() + 5
    while True:
        result = observer.get("/v1/tasks/" + TASK["run_id"] + "/result")
        if result.status_code != 409:
            break
        assert result.json() == {"error": {"code": "task_busy"}}, result.json()
        assert time.monotonic() < deadline, "Completed result remained busy"
        time.sleep(.01)
    assert result.status_code == 200 and result.json()["answer"] == "2"
    assert observer.post("/v1/tasks", json=TASK).json()["status"] == "passed"
    assert len(bundle.upstream.calls) == 1
    assert second_client.get("/v1/monitor").json()["tasks"] == []


def test_completed_http_task_remains_read_only_while_another_result_reader_is_busy(local_service):
    bundle = local_service.bundle
    bundle.upstream.fail_tiers = set()
    client = local_service.client()
    assert client.post("/v1/tasks", json=TASK).json()["status"] == "passed"
    entered, release = threading.Event(), threading.Event()
    service = bundle.app.gateway.service

    def overlapping_result_reader():
        # The fixture owns a real guard used by an overlapping reader/finalizer.
        # Observations and assertions below remain at the actual HTTP boundary.
        with service.retention.guard(service.internal_id("studio", TASK["run_id"])):
            entered.set()
            assert release.wait(5), "Overlapping reader was not released"

    with ThreadPoolExecutor(1) as workers:
        reader = workers.submit(overlapping_result_reader)
        try:
            assert entered.wait(3)
            status = client.get("/v1/tasks/" + TASK["run_id"])
            assert status.status_code == 200 and status.json()["status"] == "passed"
            assert status.json()["observation"] == "busy"
            busy = client.get("/v1/tasks/" + TASK["run_id"] + "/result")
            assert busy.status_code == 409 and busy.json()["error"]["code"] == "task_busy"
            repeated = client.post("/v1/tasks", json=TASK)
            assert repeated.status_code == 200 and repeated.json()["status"] == "passed"
            assert len(bundle.upstream.calls) == 1
        finally:
            release.set()
        reader.result(timeout=5)

    result = client.get("/v1/tasks/" + TASK["run_id"] + "/result")
    assert result.status_code == 200 and result.json()["answer"] == "2"
    assert len(bundle.upstream.calls) == 1


def test_http_expiry_boundary_and_reopened_service_preserve_history_without_resending(local_service):
    from cloudyfan_monitor.integration import build_local_integration

    bundle = local_service.bundle
    bundle.upstream.fail_tiers = set()
    client = local_service.client()
    operator = local_service.client("operator")
    created = local_service.clock.now
    assert client.post("/v1/tasks", json=TASK).json()["status"] == "passed"
    before = operator.get("/monitor/snapshot").json()
    known = before["summary"]["known_api_usd"]
    assert before["summary"]["passed"] == 1
    assert len(bundle.upstream.calls) == 1

    local_service.clock.now = created + 30 * 86400 - 1
    assert client.get("/v1/tasks/" + TASK["run_id"] + "/result").status_code == 200
    local_service.clock.now += 1
    for method, path, payload in (
        ("GET", "/v1/tasks/" + TASK["run_id"], None),
        ("GET", "/v1/tasks/" + TASK["run_id"] + "/result", None),
        ("POST", "/v1/tasks", TASK),
        ("POST", "/v1/tasks/" + TASK["run_id"] + "/resume", {}),
    ):
        expired = client.request(method, path, json=payload)
        assert expired.status_code == 410
        assert expired.json()["error"]["code"] == "task_expired"
    after = operator.get("/monitor/snapshot").json()
    assert after["summary"]["passed"] == 1
    assert after["summary"]["completed"] == 1
    assert after["summary"]["expired"] == 1
    assert after["summary"]["known_api_usd"] == known
    assert after["tasks"][0]["status"] == "expired"
    assert len(bundle.upstream.calls) == 1

    reopened = build_local_integration(bundle.root, origin=local_service.origin,
                                       clock=local_service.clock, seed=False)
    local_service.server.set_app(reopened.app)
    assert client.post("/v1/tasks", json=TASK).status_code == 410
    retained = operator.get("/monitor/snapshot").json()
    assert retained["summary"]["passed"] == 1
    assert retained["summary"]["known_api_usd"] == known
    assert retained["tasks"][0]["status"] == "expired"
    assert reopened.upstream.calls == []
    local_service.additional_workers.append({"kind": "reopened_application", "pid": os.getpid(),
                                             "mock_upstream_call_count": len(reopened.upstream.calls)})


def worker_service(root, now, connection, stop, hold_response=False):
    """Separate process with its own app, model transport, and closed network boundary."""
    from cloudyfan_monitor.integration import build_local_integration

    evidence = LoopbackEvidence()
    configure_loopback_network(evidence, setattr)
    try:
        with ThreadedServer(("127.0.0.1", 0), QuietHandler) as server:
            evidence.ports.add(server.server_port)
            origin = f"http://127.0.0.1:{server.server_port}"
            bundle = build_local_integration(root, origin=origin, clock=Clock(now), seed=False)
            if hold_response:
                def held_response(request):
                    connection.send({"entered": True, "pid": os.getpid(), "port": server.server_port,
                                     "phase": "mock_request_before_response",
                                     "mock_upstream_call_count": len(bundle.upstream.calls),
                                     "blocked_socket_attempts": [str(address) for address in evidence.blocked]})
                    # A killed process cannot acknowledge multiprocessing.Event
                    # notifications; keep this deliberate crash wait process-local.
                    threading.Event().wait(20)
                    raise AssertionError("Synthetic worker response was never released")
                bundle.upstream.before_response = held_response
            server.set_app(bundle.app)
            server.timeout = .05
            connection.send({"ready": True, "origin": origin, "pid": os.getpid()})
            while not stop.is_set():
                server.handle_request()
            connection.send({"pid": os.getpid(), "port": server.server_port,
                             "mock_upstream_call_count": len(bundle.upstream.calls),
                             "blocked_socket_attempts": [str(address) for address in evidence.blocked],
                             "socket_attempt_count": len(evidence.connected)})
    except BaseException as error:
        connection.send({"error": type(error).__name__})
        raise
    finally:
        connection.close()


def test_separate_http_worker_shares_one_service_slot_and_does_not_queue(local_service, loopback_network):
    bundle = local_service.bundle
    bundle.upstream.fail_tiers = set()
    entered, release = threading.Event(), threading.Event()

    def hold_response(request):
        entered.set()
        if not release.wait(10):
            raise AssertionError("Synthetic upstream release timed out")

    bundle.upstream.before_response = hold_response
    context = multiprocessing.get_context("spawn")
    parent_connection, child_connection = context.Pipe()
    stop = context.Event()
    process = context.Process(target=worker_service,
                              args=(bundle.root, local_service.clock.now, child_connection, stop))
    process.start()
    child_connection.close()
    try:
        assert parent_connection.poll(15), "Second worker did not start"
        ready = parent_connection.recv()
        assert ready.get("ready"), ready
        assert ready["pid"] != os.getpid()
        worker_port = int(ready["origin"].rsplit(":", 1)[1])
        loopback_network.ports.add(worker_port)
        first_client = local_service.client(timeout=12)
        second_client = local_service.client("agent", origin=ready["origin"])
        with ThreadPoolExecutor(1) as workers:
            running = workers.submit(first_client.post, "/v1/tasks", json=TASK)
            try:
                assert entered.wait(3)
                response = second_client.post("/v1/tasks", json={**TASK, "run_id": "different-worker"})
                assert response.status_code == 429
                assert response.json()["error"]["code"] == "service_busy"
                assert not release.is_set()
            finally:
                release.set()
            assert running.result(timeout=5).json()["status"] == "passed"
        assert second_client.get("/v1/monitor").json()["tasks"] == []
        assert len(bundle.upstream.calls) == 1
    finally:
        release.set()
        stop.set()
        process.join(5)
        if process.is_alive():
            process.terminate()
            process.join(2)
        try:
            if parent_connection.poll(1):
                measured = parent_connection.recv()
                local_service.additional_workers.append(measured)
            else:
                measured = {"error": "worker_receipt_missing"}
        finally:
            parent_connection.close()
        assert process.exitcode == 0, "Second worker failed or required forced termination"
        assert measured.get("mock_upstream_call_count") == 0, measured
        assert measured.get("blocked_socket_attempts") == [], measured


def test_unknown_model_fee_blocks_other_client_over_real_http(local_service):
    bundle = local_service.bundle
    bundle.upstream.unknown_cost = True
    first_client = local_service.client()
    second_client = local_service.client("agent")
    operator = local_service.client("operator")

    interrupted = first_client.post("/v1/tasks", json=TASK)
    assert interrupted.status_code == 409
    assert interrupted.json()["status"] == "accounting_interrupted"
    assert len(bundle.upstream.calls) == 1
    blocked = second_client.post("/v1/tasks", json={**TASK, "run_id": "different-client-blocked"})
    assert blocked.status_code == 409
    assert blocked.json()["status"] == "accounting_interrupted"
    assert len(bundle.upstream.calls) == 1

    snapshot = operator.get("/monitor/snapshot")
    assert snapshot.status_code == 200
    ledger = snapshot.json()["ledger"]
    assert len(ledger["accounts"]) == 1
    assert ledger["unknown_events"] == 1
    assert ledger["total_usd"] is None
    assert ledger["accounts"][0]["dispatch_blocked"] is True
    assert second_client.get("/v1/tasks/" + TASK["run_id"] + "/result").status_code == 404
    own = second_client.get("/v1/monitor").json()
    assert all(task["client"] == "agent" for task in own["tasks"])
    assert "ledger" not in own


def test_interrupted_http_submission_only_continues_after_explicit_resume(local_service):
    bundle = local_service.bundle
    bundle.upstream.fail_tiers = set()

    class InterruptedValidation(ArithmeticEnvironment):
        interrupted = False

        def validate(self, text):
            if not type(self).interrupted:
                type(self).interrupted = True
                raise RuntimeError("Synthetic public-validation interruption")
            return super().validate(text)

    # The validator factory is the registered public task seam, fixed before submission.
    service = bundle.app.gateway.service
    service.contracts["demo-cloudyfan"] = replace(
        service.contracts["demo-cloudyfan"], version="local-http-interruption-v1",
        environment_factory=lambda workspace: InterruptedValidation())
    client = local_service.client()
    interrupted = client.post("/v1/tasks", json=TASK)
    assert interrupted.status_code == 503
    assert interrupted.json()["status"] == "engineering_interrupted"
    assert len(bundle.upstream.calls) == 1

    repeated = client.post("/v1/tasks", json=TASK)
    assert repeated.status_code == 503
    assert repeated.json()["status"] == "engineering_interrupted"
    assert len(bundle.upstream.calls) == 1
    assert client.get("/v1/tasks/" + TASK["run_id"] + "/result").status_code == 409

    resumed = client.post("/v1/tasks/" + TASK["run_id"] + "/resume", json={})
    assert resumed.status_code == 200 and resumed.json()["status"] == "passed"
    assert len(bundle.upstream.calls) == 1
    result = client.get("/v1/tasks/" + TASK["run_id"] + "/result")
    assert result.status_code == 200 and result.json()["answer"] == "2"


def test_killed_worker_is_not_automatically_resumed_or_rebilled_after_reopen(local_service, loopback_network):
    from cloudyfan_monitor.integration import build_local_integration

    bundle = local_service.bundle
    context = multiprocessing.get_context("spawn")
    parent_connection, child_connection = context.Pipe()
    stop = context.Event()
    process = context.Process(target=worker_service,
                              args=(bundle.root, local_service.clock.now, child_connection, stop, True))
    process.start()
    child_connection.close()
    try:
        assert parent_connection.poll(15), "Worker did not start"
        ready = parent_connection.recv()
        assert ready.get("ready"), ready
        assert ready["pid"] != os.getpid()
        loopback_network.ports.add(int(ready["origin"].rsplit(":", 1)[1]))
        client = local_service.client(origin=ready["origin"], timeout=12)
        with ThreadPoolExecutor(1) as workers:
            request = workers.submit(client.post, "/v1/tasks", json=TASK)
            try:
                assert parent_connection.poll(5), "Worker did not reach mocked model transport"
                observed = parent_connection.recv()
                assert observed.get("entered"), observed
                assert observed["mock_upstream_call_count"] == 1
                process.terminate()
                process.join(5)
                assert not process.is_alive()
                observed["exitcode"] = process.exitcode
                local_service.additional_workers.append(observed)
                with pytest.raises(httpx.TransportError):
                    request.result(timeout=5)
            finally:
                if process.is_alive():
                    process.terminate()
                    process.join(5)

        reopened = build_local_integration(bundle.root, origin=local_service.origin,
                                           clock=local_service.clock, seed=False)
        local_service.server.set_app(reopened.app)
        observer = local_service.client()
        status = observer.get("/v1/tasks/" + TASK["run_id"])
        assert status.status_code == 200
        assert status.json()["status"] == "running"
        assert observer.post("/v1/tasks", json=TASK).json()["status"] == "running"
        assert reopened.upstream.calls == []
        resumed = observer.post("/v1/tasks/" + TASK["run_id"] + "/resume", json={})
        assert resumed.status_code == 409
        assert resumed.json()["status"] == "accounting_interrupted"
        second = local_service.client("agent")
        blocked = second.post("/v1/tasks", json={**TASK, "run_id": "blocked-after-worker-crash"})
        assert blocked.status_code == 409
        assert reopened.upstream.calls == []
        assert bundle.upstream.calls == []
        local_service.additional_workers.append({"kind": "reopened_application", "pid": os.getpid(),
                                                 "mock_upstream_call_count": len(reopened.upstream.calls)})
    finally:
        if process.is_alive():
            process.terminate()
            process.join(5)
        parent_connection.close()

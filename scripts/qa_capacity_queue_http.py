#!/usr/bin/env python3
"""Private real-HTTP queue fixture with synthetic upstream and explicit worker control."""
import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import threading
from uuid import uuid4


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    output = parser.parse_args().output.resolve()
    package = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(package / "tests"))
    import httpx
    from cloudyfan_monitor.demo import CATEGORY
    from cloudyfan_monitor.openrouter_accounting import OpenRouterAccounting
    from cloudyfan_monitor.pools import OpenRouterPoolSource
    from capacity_queue_fixture import build_queue_host
    from test_sources import fixtures
    from test_local_http_integration import (
        Clock, LoopbackEvidence, QuietHandler, ThreadedServer, configure_loopback_network,
    )

    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    boundary, responses, requests, commands = LoopbackEvidence(), [], [], []
    configure_loopback_network(boundary, setattr)
    held, released = threading.Event(), threading.Event()
    flags = {"hold": False, "read_failure": False}
    workers, worker_results = [], []
    lock = threading.RLock()
    with ThreadedServer(("127.0.0.1", 0), QuietHandler) as server:
        boundary.ports.add(server.server_port)
        origin = f"http://127.0.0.1:{server.server_port}"
        clock = Clock(datetime.fromisoformat("2026-10-25T00:00:00+00:00").timestamp())
        def observe_model(request):
            body = request.content
            payload = json.loads(body)
            with lock:
                requests.append({"method": request.method, "path": request.url.path,
                    "wire_sha256": hashlib.sha256(body).hexdigest(), "model": payload.get("model")})
                (output / f"private-wire-{len(requests):03d}.bin").write_bytes(body)
            if flags["hold"]:
                held.set()
                if not released.wait(45):
                    raise httpx.ReadTimeout("Synthetic queue completion gate timed out")
        controlled = build_queue_host(output / "service", clock=clock, origin=origin,
            options={"limits": {"capacity": 4, "concurrency": 1, "per_principal": 2, "max_wait_seconds": 120},
                "rpm": 10, "seconds": 300}, before_response=observe_model)
        host, queue = controlled.bundle, controlled.queue
        controller = host.app.capacity_controller
        budget = next(iter(host.app.gateway.service.contracts.values())).runner.store
        side_requests = {"source": [], "accounting": []}
        observation_transports = []
        for kind in side_requests:
            def unexpected(request, source=kind):
                side_requests[source].append({"method": request.method, "path": request.url.path})
                return httpx.Response(503, json={"error": "PRIVATE_QUEUE_UPSTREAM_FAILURE"})
            observation_transports.append(httpx.Client(transport=httpx.MockTransport(unexpected), trust_env=False))
        host.app.source = OpenRouterPoolSource(observation_transports[0], fixtures()[2])
        host.app.gateway.billing.source = OpenRouterAccounting(observation_transports[1], api_key=uuid4().hex)

        def receipt():
            original = {str(path.relative_to(host.root)): hashlib.sha256(path.read_bytes()).hexdigest()
                for folder in (host.root / "receipts", host.root / "tasks")
                for path in sorted(folder.rglob("*.json"))}
            return {"schema_version": 1, "pid": os.getpid(), "origin": origin,
                "scope": "actual original Core/BudgetedCore/TaskService/IdentityStore/Store/QueueScheduler/CapacityStore; loopback HTTP; MockTransport only",
                "http_responses": list(responses), "fixture_commands": list(commands),
                "model_requests": list(requests), "source_requests": list(side_requests["source"]),
                "accounting_requests": list(side_requests["accounting"]),
                "budget": budget.report(), "billing": host.app.gateway.billing.snapshot(),
                "control": host.app.store.control(),
                "queue_sha256": hashlib.sha256(queue.path.read_bytes()).hexdigest(),
                "capacity_sha256": hashlib.sha256(controller.store.path.read_bytes()).hexdigest(),
                "original_evidence_sha256": original, "worker_results": list(worker_results),
                "worker_running": any(worker.is_alive() for worker in workers),
                "model_response_held": held.is_set() and not released.is_set(),
                "blocked_server_socket_attempts": [str(item) for item in boundary.blocked],
                "server_socket_attempts": [str(item) for item in boundary.connected]}

        def save():
            with lock:
                pending = output / "http-receipt.pending"
                pending.write_text(json.dumps(receipt(), indent=2))
                pending.replace(output / "http-receipt.json")

        def app(environ, start_response):
            response = {"method": environ.get("REQUEST_METHOD"), "path": environ.get("PATH_INFO")}

            def start(status, headers, exc_info=None):
                response["status"] = int(status.split()[0])
                return start_response(status, headers, exc_info)

            if flags["read_failure"] and response == {"method": "GET", "path": "/monitor/queue"}:
                body = b'{"error":{"code":"monitor_or_source_unavailable"}}'
                start("503 Service Unavailable", [("Content-Type", "application/json"), ("Content-Length", str(len(body)))])
                result = [body]
            else:
                result = host.app(environ, start)
            responses.append(response)
            save()
            return result

        def dispatch():
            try:
                result = queue.dispatch_next()
                worker_results.append({"status": "returned", "claimed": result is not None})
            except Exception as error:
                code = getattr(error, "code", "fixture_worker_failed")
                worker_results.append({"status": "failed", "code": code if re.fullmatch(r"[a-z_]{1,80}", code) else "fixture_worker_failed"})
            finally:
                save()

        server.set_app(app)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        thread.start()
        print(json.dumps({"type": "ready", "origin": origin, "operator_token": host.operator_token,
            "category": CATEGORY, "policy_version": "synthetic-queued-admission-v1",
            "queue_version": "synthetic-pure-queue-v1",
            "credentials": {key: value.token for key, value in host.credentials.items()}}), flush=True)
        try:
            for line in sys.stdin:
                command = json.loads(line)
                name = command["command"]
                if name == "stop":
                    break
                if name == "hold":
                    if any(worker.is_alive() for worker in workers):
                        raise ValueError("Cannot replace an active completion gate")
                    flags["hold"] = True
                    held.clear()
                    released.clear()
                elif name == "release":
                    released.set()
                    flags["hold"] = False
                elif name == "dispatch":
                    worker = threading.Thread(target=dispatch, daemon=True)
                    workers.append(worker)
                    worker.start()
                elif name == "advance":
                    seconds = command.get("seconds")
                    if type(seconds) is not int or not 0 <= seconds <= 31 * 86400:
                        raise ValueError("Bounded fixture clock movement required")
                    clock.now += seconds
                elif name == "revoke_agent":
                    host.identity.revoke(host.credentials["agent"].id)
                elif name == "fail_first_tier":
                    host.upstream.fail_tiers = {"open_source"}
                elif name == "restore_model":
                    host.upstream.fail_tiers = set()
                elif name == "rotate_operator":
                    host.app.admin_token = uuid4().hex
                elif name == "fail_read":
                    flags["read_failure"] = True
                elif name == "restore_read":
                    flags["read_failure"] = False
                elif name != "inspect":
                    raise ValueError("Unknown private fixture command")
                commands.append({"command": name})
                save()
                message = {"type": "inspected", **receipt()}
                if name == "rotate_operator":
                    message["reauth_token"] = host.app.admin_token
                print(json.dumps(message), flush=True)
        finally:
            released.set()
            for worker in workers:
                worker.join(3)
            server.shutdown()
            thread.join(2)
            save()
            for transport in observation_transports:
                transport.close()
            controlled.close()
            if boundary.blocked or any(worker.is_alive() for worker in workers):
                raise AssertionError("Fixture network or worker cleanup failed")


if __name__ == "__main__":
    main()

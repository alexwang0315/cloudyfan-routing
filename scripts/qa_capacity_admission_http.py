#!/usr/bin/env python3
"""Private actual-HTTP capacity fixture; synthetic upstream only, no product controls."""
import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
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
    from cloudyfan_jev.capacity_dispatch import TokenUpperBound
    from cloudyfan_monitor.openrouter_accounting import OpenRouterAccounting
    from cloudyfan_monitor.pools import OpenRouterPoolSource
    from candidate_health_fixture import assemble_health_host
    from capacity_admission_fixture import attach_admission
    from test_sources import fixtures
    from test_local_http_integration import (
        Clock, LoopbackEvidence, QuietHandler, ThreadedServer, configure_loopback_network,
    )

    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    boundary, responses, requests, commands = LoopbackEvidence(), [], [], []
    configure_loopback_network(boundary, setattr)
    held, released = threading.Event(), threading.Event()
    flags = {"hold": False, "read_failure": False}
    with ThreadedServer(("127.0.0.1", 0), QuietHandler) as server:
        boundary.ports.add(server.server_port)
        origin = f"http://127.0.0.1:{server.server_port}"
        clock = Clock(datetime.fromisoformat("2026-10-21T00:00:00+00:00").timestamp())
        host = assemble_health_host(output / "service", origin, clock=clock)

        def upstream(request):
            payload = json.loads(request.content)
            requests.append({"method": request.method, "path": request.url.path,
                "wire_sha256": hashlib.sha256(request.content).hexdigest(), "model": payload.get("model")})
            (output / f"private-wire-{len(requests):03d}.bin").write_bytes(request.content)
            host.calls.append({"method": request.method, "path": request.url.path, "payload": payload})
            if flags["hold"]:
                held.set()
                if not released.wait(45):
                    raise httpx.ReadTimeout("Synthetic completion gate timed out")
            return host.values["module"].response(payload, len(requests))

        for contract in host.service.contracts.values():
            contract.runner.client.http = httpx.Client(transport=httpx.MockTransport(upstream), trust_env=False)

        class SyntheticTokenContract:
            version = "synthetic-desktop-token-envelope-v1"

            def __call__(self, body, scope):
                return TokenUpperBound(1000, scope["wire_sha256"], scope["scope_sha256"], self.version,
                    "MockTransport-fixture-only-not-production", clock() + 300)

        controller = attach_admission(host, clock, rpm=1, tpm=1000, token_adapter=SyntheticTokenContract())
        controller.register("synthetic-admission-v2", configuration_version="synthetic-admission-config-v1",
            routes=controller.routes["synthetic-admission-v1"], max_seconds=300)
        side_requests = {"source": [], "accounting": []}
        observation_transports = []
        for kind in side_requests:
            def unexpected(request, source=kind):
                side_requests[source].append({"method": request.method, "path": request.url.path})
                return httpx.Response(503, json={"error": "PRIVATE_CAPACITY_UPSTREAM_FAILURE"})
            observation_transports.append(httpx.Client(transport=httpx.MockTransport(unexpected), trust_env=False))
        host.app.source = OpenRouterPoolSource(observation_transports[0], fixtures()[2])
        host.app.gateway.billing.source = OpenRouterAccounting(observation_transports[1], api_key=uuid4().hex)
        lock = threading.RLock()

        def receipt():
            files = {str(path.relative_to(host.root)): hashlib.sha256(path.read_bytes()).hexdigest()
                for folder in (host.root / "receipts", host.root / "tasks") for path in sorted(folder.rglob("*.json"))}
            return {"schema_version": 1, "pid": os.getpid(), "origin": origin,
                "scope": "synthetic real BackupCategoryCore/BudgetedCore/TaskService/IdentityStore/HealthStore/CapacityStore; actual loopback HTTP; MockTransport only",
                "http_responses": list(responses), "fixture_commands": list(commands),
                "model_requests": list(requests), "source_requests": list(side_requests["source"]),
                "accounting_requests": list(side_requests["accounting"]),
                "budget": host.budget.report(), "control": host.app.store.control(),
                "capacity_sha256": hashlib.sha256(controller.store.path.read_bytes()).hexdigest(),
                "health_sha256": hashlib.sha256(host.health.path.read_bytes()).hexdigest(),
                "original_evidence_sha256": files,
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

            if flags["read_failure"] and response == {"method": "GET", "path": "/monitor/capacity"}:
                body = b'{"error":{"code":"monitor_or_source_unavailable"}}'
                start("503 Service Unavailable", [("Content-Type", "application/json"), ("Content-Length", str(len(body)))])
                result = [body]
            else:
                result = host.app(environ, start)
            responses.append(response)
            save()
            return result

        server.set_app(app)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        thread.start()
        print(json.dumps({"type": "ready", "origin": origin, "operator_token": host.operator_token,
            "category": host.category, "credentials": {key: value.token for key, value in host.credentials.items()}}), flush=True)
        try:
            for line in sys.stdin:
                command = json.loads(line)
                name = command["command"]
                if name == "stop":
                    break
                if name == "hold":
                    flags["hold"] = True
                    held.clear()
                    released.clear()
                elif name == "release":
                    released.set()
                    flags["hold"] = False
                elif name == "advance":
                    seconds = command["seconds"]
                    if type(seconds) is not int or not 0 <= seconds <= 31 * 86400:
                        raise ValueError("Bounded fixture clock movement required")
                    clock.now += seconds
                elif name == "revoke_agent":
                    host.app.identity_store.revoke(host.credentials["agent"].id)
                elif name == "fail_read":
                    flags["read_failure"] = True
                elif name == "restore_read":
                    flags["read_failure"] = False
                elif name != "inspect":
                    raise ValueError("Unknown private fixture command")
                commands.append({"command": name})
                save()
                print(json.dumps({"type": "inspected", **receipt()}), flush=True)
        finally:
            released.set()
            server.shutdown()
            thread.join(2)
            save()
            for transport in observation_transports:
                transport.close()
            if boundary.blocked:
                raise AssertionError("Unexpected non-loopback socket attempt")


if __name__ == "__main__":
    main()

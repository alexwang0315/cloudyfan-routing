#!/usr/bin/env python3
"""Private real HTTP resilience fixture; original stores and failures stay local."""
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
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    package = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(package / "tests"))
    import httpx
    from cloudyfan_monitor.demo import CATEGORY
    from cloudyfan_monitor.openrouter_accounting import OpenRouterAccounting
    from cloudyfan_monitor.pools import OpenRouterPoolSource
    from capacity_resilience_fixture import build_resilience_host, copied_database_fault
    from test_local_http_integration import (
        Clock, LoopbackEvidence, QuietHandler, ThreadedServer, configure_loopback_network,
    )
    from test_sources import fixtures

    boundary, responses, requests, commands = LoopbackEvidence(), [], [], []
    configure_loopback_network(boundary, setattr)
    lock = threading.RLock()
    workers, results = [], []
    with ThreadedServer(("127.0.0.1", 0), QuietHandler) as server:
        boundary.ports.add(server.server_port)
        origin = f"http://127.0.0.1:{server.server_port}"
        clock = Clock(datetime.fromisoformat("2026-10-25T00:00:00+00:00").timestamp())

        def observe_model(request):
            body = request.content
            payload = json.loads(body) if body else {}
            with lock:
                number = len(requests) + 1
                requests.append({"method": request.method, "path": request.url.path,
                    "wire_sha256": hashlib.sha256(body).hexdigest(), "model": payload.get("model")})
                (output / f"private-wire-{number:04d}.bin").write_bytes(body)

        controlled = build_resilience_host(output / "service", clock=clock, origin=origin,
            options={"limits": {"capacity": 4, "concurrency": 1, "per_principal": 2, "max_wait_seconds": 120},
                "rpm": 200, "seconds": 3600, "retry_after_seconds": 3600, "shared_budget": True},
            before_response=observe_model)
        host, queue, controller = controlled.bundle, controlled.queue, controlled.controller
        side_requests = {"source": [], "accounting": []}
        transports = []
        for kind in side_requests:
            def unexpected(request, source=kind):
                side_requests[source].append({"method": request.method, "path": request.url.path})
                return httpx.Response(503, json={"error": "PRIVATE_RESILIENCE_SOURCE_CANARY"})
            transports.append(httpx.Client(transport=httpx.MockTransport(unexpected), trust_env=False))
        host.app.source = OpenRouterPoolSource(transports[0], fixtures()[2])
        host.app.gateway.billing.source = OpenRouterAccounting(transports[1], api_key=uuid4().hex)

        def inventory():
            folders = {host.app.gateway.service.root}
            folders.update(contract.runner.client.root for contract in host.app.gateway.service.contracts.values())
            return {str(path.relative_to(host.root)): hashlib.sha256(path.read_bytes()).hexdigest()
                for folder in folders for path in sorted(folder.rglob("*.json")) if path.is_file()}

        def receipt():
            budgets = {str(contract.runner.store.path.relative_to(host.root)): contract.runner.store.report()
                for contract in host.app.gateway.service.contracts.values()}
            health = {str(path.relative_to(host.root)): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in host.root.rglob("health.sqlite3")}
            return {"schema_version": 1, "pid": os.getpid(), "origin": origin, "clock": clock(),
                "scope": "initial real Queue/Core/BudgetedCore/TaskService/Capacity/Windows preservation slice; MockTransport only; Health factory pending",
                "http_responses": list(responses), "fixture_commands": list(commands),
                "model_requests": list(requests), "source_requests": list(side_requests["source"]),
                "accounting_requests": list(side_requests["accounting"]), "budget": budgets,
                "capacity_sha256": hashlib.sha256(controller.store.path.read_bytes()).hexdigest(),
                "queue_sha256": hashlib.sha256(queue.path.read_bytes()).hexdigest(),
                "health_sqlite_sha256": health,
                "health_evidence_status": "present" if health else "not_in_initial_factory",
                "original_evidence_sha256": inventory(), "worker_running": any(w.is_alive() for w in workers),
                "worker_results": list(results),
                "blocked_server_socket_attempts": [str(item) for item in boundary.blocked],
                "server_socket_attempts": [str(item) for item in boundary.connected]}

        def save():
            with lock:
                temporary = output / "http-receipt.pending"
                temporary.write_text(json.dumps(receipt(), indent=2))
                temporary.replace(output / "http-receipt.json")

        def app(environ, start_response):
            row = {"method": environ.get("REQUEST_METHOD"), "path": environ.get("PATH_INFO")}
            def start(status, headers, exc_info=None):
                row["status"] = int(status.split()[0])
                return start_response(status, headers, exc_info)
            value = host.app(environ, start)
            responses.append(row)
            save()
            return value

        server.set_app(app)
        serving = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        serving.start()
        print(json.dumps({"type": "ready", "origin": origin, "category": CATEGORY,
            "policy_version": "synthetic-queued-admission-v1", "operator_token": host.operator_token,
            "credentials": {name: value.token for name, value in host.credentials.items()}}), flush=True)
        try:
            for line in sys.stdin:
                request = json.loads(line)
                name = request["command"]
                if name == "stop":
                    break
                if name == "work":
                    if any(worker.is_alive() for worker in workers):
                        raise ValueError("A private worker is already running")
                    def work():
                        try:
                            result = queue.dispatch_next()
                            results.append({key: result[key] for key in ("status", "run_id", "reason") if key in result}
                                if isinstance(result, dict) else {"status": "returned"})
                        except Exception as error:
                            import traceback
                            with (output / f"private-worker-{len(results):04d}-failure.txt").open("x") as stream:
                                stream.write(traceback.format_exc())
                            results.append({"status": "exception", "type": type(error).__name__})
                        finally:
                            save()
                    worker = threading.Thread(target=work, daemon=True)
                    workers.append(worker)
                    worker.start()
                elif name == "advance":
                    seconds = request.get("seconds")
                    if type(seconds) not in (int, float) or not 0 <= seconds <= 366 * 86400:
                        raise ValueError("Invalid controlled time advance")
                    clock.now += seconds
                elif name == "fault_missing_window_table":
                    if any(worker.is_alive() for worker in workers):
                        raise ValueError("Fault copies require no active worker")
                    with (output / "fault-before.json").open("x") as stream:
                        json.dump(receipt(), stream, indent=2)
                    copied_database_fault(controller.store.path,
                        output / "PRIVATE_RESILIENCE_PATH_CANARY-preserved-original",
                        omit=("capacity_window_observations",))
                elif name != "inspect":
                    raise ValueError("Unknown private fixture command")
                commands.append({"command": name})
                save()
                print(json.dumps({"type": "inspected", **receipt()}), flush=True)
        finally:
            server.shutdown()
            serving.join(3)
            for worker in workers:
                worker.join(3)
            save()
            controlled.close()
            for transport in transports:
                transport.close()


if __name__ == "__main__":
    try:
        main()
    except BaseException:
        import traceback
        if "--output" in sys.argv:
            output = Path(sys.argv[sys.argv.index("--output") + 1]).resolve()
            output.mkdir(parents=True, exist_ok=True)
            (output / "private-fixture-failure.txt").write_text(traceback.format_exc())
        sys.stderr.write("Private resilience fixture failed; local evidence retained.\n")
        raise SystemExit(1) from None

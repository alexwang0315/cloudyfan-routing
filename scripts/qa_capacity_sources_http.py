#!/usr/bin/env python3
"""Private synthetic capacity-source desktop fixture; all supplier traffic mocked."""
import argparse
from contextlib import suppress
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import sys
import threading


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    package = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(package / "tests"))
    from candidate_health_fixture import assemble_health_host
    from capacity_sources_fixture import MockCapacitySources, attach_capacity
    from test_local_http_integration import (
        Clock, LoopbackEvidence, QuietHandler, ThreadedServer, configure_loopback_network,
    )

    args.output.mkdir(parents=True, exist_ok=True)
    boundary, responses, commands = LoopbackEvidence(), [], []
    configure_loopback_network(boundary, setattr)
    with ThreadedServer(("127.0.0.1", 0), QuietHandler) as server:
        boundary.ports.add(server.server_port)
        origin = f"http://127.0.0.1:{server.server_port}"
        clock = Clock(datetime.fromisoformat("2026-10-21T00:00:00+00:00").timestamp())
        host = assemble_health_host(args.output / "service", origin, clock=clock)
        upstream = MockCapacitySources()
        observer = attach_capacity(host, clock, sources=upstream.configured())
        receipt_lock = threading.Lock()

        def receipt():
            report = host.budget.report()
            return {"schema_version": 1, "pid": os.getpid(), "origin": origin,
                "http_responses": list(responses), "fixture_commands": list(commands),
                "mock_source_requests": list(upstream.requests), "mock_model_request_count": len(host.calls),
                "known_api_usd": report["known_usd"], "reserved_api_usd": report["reserved_usd"],
                "unknown_events": report["unknown_events"], "control": host.app.store.control(),
                "capacity_sha256": hashlib.sha256(observer.store.path.read_bytes()).hexdigest(),
                "health_sha256": hashlib.sha256(host.health.path.read_bytes()).hexdigest(),
                "blocked_server_socket_attempts": [str(item) for item in boundary.blocked],
                "server_socket_attempts": [str(item) for item in boundary.connected],
                "scope": "synthetic BackupCategoryCore/BudgetedCore/IdentityStore/HealthStore/CapacityStore; actual HTTP; MockTransport only"}

        def save():
            with receipt_lock:
                pending = args.output / "http-receipt.pending"
                pending.write_text(json.dumps(receipt(), indent=2))
                pending.replace(args.output / "http-receipt.json")

        def app(environ, start_response):
            observed = {"method": environ.get("REQUEST_METHOD"), "path": environ.get("PATH_INFO"),
                        "remote_addr": environ.get("REMOTE_ADDR"), "port": server.server_port}
            def status(value, headers, exc_info=None):
                observed["status"] = int(value.split()[0])
                return start_response(value, headers, exc_info)
            result = host.app(environ, status)
            responses.append(observed)
            save()
            return result

        server.set_app(app)

        def input_commands():
            for line in sys.stdin:
                command = json.loads(line)
                if command["command"] == "inspect":
                    commands.append({"command": "inspect"})
                elif command["command"] == "fail_a":
                    upstream.failure.add("credential-a")
                    commands.append({"command": "fail_a"})
                elif command["command"] == "advance":
                    seconds = command["seconds"]
                    if type(seconds) is not int or not 0 <= seconds <= 31 * 86400:
                        raise ValueError("Bounded fixture clock movement required")
                    clock.now += seconds
                    commands.append({"command": "advance", "seconds": seconds})
                elif command["command"] == "stop":
                    commands.append({"command": "stop"})
                    server.shutdown()
                    return
                else:
                    raise ValueError("Unsupported private fixture command")
                save()
                print(json.dumps({"type": "inspected", **receipt()}), flush=True)
            server.shutdown()

        thread = threading.Thread(target=input_commands, daemon=True)
        thread.start()
        print(json.dumps({"type": "ready", "origin": origin, "category": host.category,
            "credentials": {key: value.token for key, value in host.credentials.items()}}), flush=True)
        try:
            server.serve_forever(poll_interval=.01)
        finally:
            save()
            with suppress(Exception):
                thread.join(1)
            if boundary.blocked:
                raise AssertionError("Unexpected fixture network attempt")


if __name__ == "__main__":
    main()

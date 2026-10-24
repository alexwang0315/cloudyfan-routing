#!/usr/bin/env python3
"""Private controlled-clock health desktop fixture; mutations never are HTTP routes."""
import argparse
from contextlib import suppress
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sys
import threading


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    output = parser.parse_args().output.resolve()
    package = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(package / "tests"))
    from candidate_health_fixture import assemble_health_host
    from test_local_http_integration import (
        Clock, LoopbackEvidence, QuietHandler, ThreadedServer, configure_loopback_network,
    )

    output.mkdir(parents=True, exist_ok=True)
    boundary, responses, commands = LoopbackEvidence(), [], []
    configure_loopback_network(boundary, setattr)
    with ThreadedServer(("127.0.0.1", 0), QuietHandler) as server:
        boundary.ports.add(server.server_port)
        origin = f"http://127.0.0.1:{server.server_port}"
        clock = Clock(datetime.fromisoformat("2026-10-21T00:00:00+00:00").timestamp())
        host = assemble_health_host(output / "service", origin, clock=clock)
        receipt_lock = threading.Lock()

        def receipt():
            budget = host.budget.report()
            originals = {str(path.relative_to(host.root)): hashlib.sha256(path.read_bytes()).hexdigest()
                for parent in (host.root / "receipts" / "runs", host.service.root)
                for path in parent.rglob("*") if path.is_file() and (
                    parent.name == "runs" or path.name == "job.json")}
            return {"schema_version": 1, "scope": "synthetic BackupCategoryCore/HealthStore; actual HTTP; MockTransport",
                "http_responses": list(responses), "fixture_commands": list(commands),
                "mock_upstream_calls": {"total": len(host.calls),
                    "POST": sum(call["method"] == "POST" for call in host.calls),
                    "GET": sum(call["method"] == "GET" for call in host.calls),
                    "classifier_POST": sum("questions" in call["payload"] for call in host.calls)},
                "health_store_sha256": hashlib.sha256(host.health.path.read_bytes()).hexdigest(),
                "original_evidence_sha256": originals, "clock": clock(),
                "known_api_usd": budget["known_usd"], "reserved_api_usd": budget["reserved_usd"],
                "unknown_events": budget["unknown_events"], "control": host.app.store.control(),
                "blocked_server_socket_attempts": [str(item) for item in boundary.blocked],
                "server_socket_attempts": [str(item) for item in boundary.connected]}

        def save():
            with receipt_lock:
                pending = output / "http-receipt.pending"
                pending.write_text(json.dumps(receipt(), indent=2))
                pending.replace(output / "http-receipt.json")

        def app(environ, start_response):
            observed = {"method": environ.get("REQUEST_METHOD"), "path": environ.get("PATH_INFO")}
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
                command = json.loads(line)["command"]
                if command == "stop":
                    server.shutdown()
                    return
                if command == "expire_health":
                    clock.now += 300
                elif command == "publish_healthy":
                    # A genuinely new observation has a new timestamp. Reusing
                    # an immutable batch hash does not supersede later evidence.
                    clock.now += 1
                    host.health.publish([host.observation(tier, position)
                        for tier, rows in host.values["plan"]["pools"].items() for position in range(len(rows))])
                elif command == "availability_failures":
                    row = host.observation()
                    for number in (1, 2):
                        host.health.record(f"desktop-availability-{number}", row["configuration_sha256"], row["provider"])
                elif command == "elapse_cooldown":
                    clock.now += 60
                elif command == "recover_endpoint":
                    row = host.observation()
                    current = host.health.assess(row["configuration_sha256"], row["provider"])
                    host.health.recover(row["configuration_sha256"], row["provider"], current["snapshot_id"])
                elif command == "degrade_endpoint":
                    row = host.observation()
                    row["health"].update(status=4, status_meaning="degraded", uptime_last_5m=98)
                    row["speed"]["latency_last_30m"]["p99"] = 10001
                    row["speed"]["throughput_last_30m"]["p50"] = 1
                    host.health.publish([row])
                else:
                    assert command == "inspect"
                commands.append({"command": command, "clock": clock(), "mock_call_count": len(host.calls)})
                save()
                print(json.dumps({"type": "inspected", **receipt()}), flush=True)
            server.shutdown()

        thread = threading.Thread(target=input_commands, daemon=True)
        thread.start()
        # Disposable synthetic credentials stay only on the private child pipe.
        print(json.dumps({"type": "ready", "origin": origin, "pools": host.pools, "category": host.category,
            "credentials": {key: value.token for key, value in host.credentials.items()}}), flush=True)
        try:
            server.serve_forever(poll_interval=.01)
        finally:
            save()
            with suppress(Exception):
                thread.join(1)
            assert not boundary.blocked


if __name__ == "__main__":
    main()

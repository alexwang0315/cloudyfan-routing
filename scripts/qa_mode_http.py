#!/usr/bin/env python3
"""Disposable four-mode desktop host. Only synthetic credentials travel over its private pipe."""
import argparse
from contextlib import suppress
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
    from test_category_host_integration import category_values
    from test_http_mode_acceptance import assemble_mode_host
    from test_local_http_integration import (
        LoopbackEvidence, QuietHandler, ThreadedServer, configure_loopback_network,
    )

    args.output.mkdir(parents=True, exist_ok=True)
    calls, responses, commands = [], [], []
    receipt_lock = threading.Lock()
    boundary = LoopbackEvidence()
    configure_loopback_network(boundary, setattr)
    values = category_values()
    with ThreadedServer(("127.0.0.1", 0), QuietHandler) as server:
        boundary.ports.add(server.server_port)
        origin = f"http://127.0.0.1:{server.server_port}"
        host = assemble_mode_host(args.output / "service", values, calls, origin)

        def receipt():
            return {"schema_version": 1, "pid": os.getpid(), "origin": origin,
                "http_responses": responses, "fixture_commands": commands,
                "mock_upstream_call_count": len(calls),
                "mock_upstream_calls": [{"model": call.get("model"),
                    "kind": "classification" if "questions" in call else "generation"} for call in calls],
                "server_socket_attempts": [str(item) for item in boundary.connected],
                "blocked_server_socket_attempts": [str(item) for item in boundary.blocked],
                "network_policy": "Python deny all outbound sockets except its loopback port; model API uses MockTransport",
                "scope": "isolated synthetic CategoryCore and Core baseline fixture, no real model qualification"}

        def save():
            # No credentials, prompts, answers, or raw model payloads are exported.
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

            response = host.app(environ, status)
            responses.append(observed)
            save()
            return response

        server.set_app(app)

        def input_commands():
            nonlocal host
            for line in sys.stdin:
                command = json.loads(line)
                if command["command"] == "reopen":
                    host = assemble_mode_host(host.root, host.values, calls, origin, clock=host.clock)
                    commands.append({"command": "reopen", "mock_call_count": len(calls)})
                    save()
                    print(json.dumps({"type": "reopened", "mock_call_count": len(calls)}), flush=True)
                elif command["command"] == "stop":
                    commands.append({"command": "stop", "mock_call_count": len(calls)})
                    server.shutdown()
                    return
                else:
                    raise ValueError("Unsupported fixture command")
            server.shutdown()

        thread = threading.Thread(target=input_commands, daemon=True)
        thread.start()
        # Captured by qa_mode_desktop's pipe, never printed or persisted there.
        print(json.dumps({"type": "ready", "origin": origin, "pools": host.pools,
            "category": values["category"], "credentials": {key: value.token for key, value in host.credentials.items()},
            "operator": host.operator_token}), flush=True)
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

"""Spawned real HTTP host; credentials remain private in its local IPC channel."""
import json
import os
from pathlib import Path

from capacity_windows_process_fixture import build_window_process_bundle
from test_local_http_integration import (
    LoopbackEvidence, QuietHandler, ThreadedServer, configure_loopback_network,
)


def serve(root, index, clock, options, mode, connection, stop, release):
    evidence = LoopbackEvidence()
    configure_loopback_network(evidence, setattr)
    try:
        with ThreadedServer(("127.0.0.1", 0), QuietHandler) as server:
            evidence.ports.add(server.server_port)
            origin = "http://127.0.0.1:" + str(server.server_port)

            def before_post():
                connection.send({"entered": True, "pid": os.getpid(), "port": server.server_port,
                    "phase": "fee_guard_after_prepare_before_physical_post", "synthetic_clock": clock(),
                    "blocked_socket_attempts": [str(address) for address in evidence.blocked]})
                if not release.wait(25):
                    raise AssertionError("Explicit synthetic before-POST release was not received")

            bundle, clients = build_window_process_bundle(root, index, clock, origin=origin,
                mode=mode, before_post=before_post, **options)
            server.set_app(bundle.app)
            server.timeout = .02
            connection.send({"ready": True, "origin": origin, "pid": os.getpid(),
                "credential": bundle.credentials["studio"].token, "operator_token": bundle.operator_token})
            while not stop.is_set():
                server.handle_request()
            receipt = {"pid": os.getpid(), "port": server.server_port,
                "mock_upstream_call_count": len(bundle.upstream.calls),
                "blocked_socket_attempts": [str(address) for address in evidence.blocked],
                "socket_attempt_count": len(evidence.connected)}
            (Path(root) / ("worker-" + str(index) + "-" + str(os.getpid()) + ".json")).write_text(json.dumps(receipt))
            connection.send(receipt)
            for client in clients.values():
                client.http.close()
    except BaseException as error:
        connection.send({"error": type(error).__name__, "pid": os.getpid()})
        raise
    finally:
        connection.close()

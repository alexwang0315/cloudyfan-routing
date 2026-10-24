"""Owned process target: real HTTP, real queue, and explicit upstream signals.

The assembly callable belongs to the process fixture. It must consume the real
published queue host; this runner neither implements nor substitutes a queue.
"""
import hashlib
import json
import os
from pathlib import Path
import threading

import httpx

from cloudyfan_jev.scheduler import QueueScheduler
from test_local_http_integration import (
    LoopbackEvidence, QuietHandler, ThreadedServer, configure_loopback_network,
)


def serve(assemble, root, index, clock, options, mode, connection, stop, release):
    root = Path(root)
    evidence = LoopbackEvidence()
    configure_loopback_network(evidence, setattr)
    send_lock = threading.Lock()
    jobs = []
    host = None

    def send(value):
        with send_lock:
            connection.send(value)

    def append(name, value):
        path = root / name
        descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(descriptor, "a") as stream:
            stream.write(json.dumps(value, allow_nan=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    try:
        with ThreadedServer(("127.0.0.1", 0), QuietHandler) as server:
            evidence.ports.add(server.server_port)
            origin = "http://127.0.0.1:" + str(server.server_port)

            def upstream_signal(request):
                row = {"pid": os.getpid(), "method": request.method, "url": str(request.url),
                    "wire_sha256": hashlib.sha256(request.content).hexdigest(),
                    "content": request.content.decode(), "clock": clock()}
                append("queue-wire-" + str(index) + ".jsonl", row)
                if mode not in {"hold", "kill", "uncertain"}:
                    return
                event = {"event": "entered", "pid": os.getpid(), "port": server.server_port,
                    "phase": "physical_mock_post_before_response", "clock": clock(),
                    "wire_sha256": row["wire_sha256"]}
                append("queue-worker-" + str(index) + "-events.jsonl", event)
                send(event)
                if mode == "kill":
                    # A deliberately killed child must not die holding the
                    # process-shared completion Event's condition lock.
                    threading.Event().wait(30)
                    raise AssertionError("Deliberate crash worker was not terminated")
                if not release.wait(30):
                    raise AssertionError("Explicit synthetic upstream completion was not received")
                if mode == "uncertain":
                    raise httpx.ReadError("synthetic unknown queue response", request=request)

            host = assemble(root, index, clock, origin, options, upstream_signal)
            if not isinstance(host.queue, QueueScheduler) or host.queue.service is not host.app.gateway.service:
                raise AssertionError("Independent workers must expose the actual controlled QueueScheduler")
            server.set_app(host.app)
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
            thread.start()
            send({"event": "ready", "pid": os.getpid(), "origin": origin,
                "credential": host.credential, "operator_token": host.operator_token,
                "credentials": {name: value.token for name, value in host.credentials.items()},
                "queue_path": str(host.queue.path), "service_root": str(host.queue.service.root),
                "protected_paths": [str(path) for path in host.protected_paths]})

            def dispatch(command_id):
                try:
                    value = host.queue.dispatch_next()
                    send({"event": "dispatched", "command_id": command_id, "result": value})
                except BaseException as error:
                    send({"event": "dispatch_failed", "command_id": command_id,
                        "error_type": type(error).__name__})

            try:
                while not stop.is_set():
                    if not connection.poll(.02):
                        continue
                    command = connection.recv()
                    if command.get("command") != "dispatch" or set(command) != {"command", "command_id"}:
                        raise AssertionError("Unsupported private process command")
                    job = threading.Thread(target=dispatch, args=(command["command_id"],), daemon=True)
                    jobs.append(job)
                    job.start()
            finally:
                server.shutdown()
                thread.join(3)
                for job in jobs:
                    job.join(3)
                receipt = {"pid": os.getpid(), "port": server.server_port,
                    "active_dispatch_threads": sum(job.is_alive() for job in jobs),
                    "server_thread_alive": thread.is_alive(),
                    "blocked_socket_attempts": [str(address) for address in evidence.blocked],
                    "socket_attempt_count": len(evidence.connected)}
                append("queue-worker-" + str(index) + "-events.jsonl", {"event": "stopped", **receipt})
                send({"event": "stopped", **receipt})
                if receipt["active_dispatch_threads"] or receipt["server_thread_alive"]:
                    raise AssertionError("Owned queue worker did not finish its threads")
                if evidence.blocked:
                    raise AssertionError("Queue worker attempted forbidden network")
    except BaseException as error:
        send({"event": "worker_failed", "error_type": type(error).__name__, "pid": os.getpid()})
        raise
    finally:
        if host is not None:
            host.close()
        connection.close()

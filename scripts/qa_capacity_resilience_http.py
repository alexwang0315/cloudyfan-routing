#!/usr/bin/env python3
"""Private real HTTP resilience fixture; original stores and failures stay local."""
import argparse
from datetime import datetime
import faulthandler
import hashlib
import json
import os
from pathlib import Path
import sys
import threading
import time
from uuid import uuid4


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--service-root", type=Path)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--minimal", action="store_true")
    parser.add_argument("--full-workflow", action="store_true")
    args = parser.parse_args()
    if args.minimal and args.full_workflow:
        parser.error("The full-workflow held fixture cannot be combined with the minimal slice")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    package = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(package / "tests"))
    import httpx
    from cloudyfan_monitor.demo import CATEGORY
    from cloudyfan_monitor.openrouter_accounting import OpenRouterAccounting
    from cloudyfan_monitor.pools import OpenRouterPoolSource
    from capacity_resilience_fixture import (
        build_resilience_host, build_resilience_health_host, copied_database_fault,
    )
    from test_local_http_integration import (
        Clock, LoopbackEvidence, QuietHandler, ThreadedServer, configure_loopback_network,
    )
    from test_sources import fixtures

    boundary, responses, requests, commands = LoopbackEvidence(), [], [], []
    configure_loopback_network(boundary, setattr)
    lock = threading.RLock()
    phase_lock = threading.Lock()
    workers, results = [], []
    thread_dump_written = False
    started = time.monotonic()

    def worker_phase(worker_id, phase, outcome=None):
        observed = time.monotonic()
        row = {"worker_id": worker_id, "thread_id": threading.get_ident(),
            "native_thread_id": threading.get_native_id(), "phase": phase,
            "monotonic_seconds": observed, "elapsed_seconds": observed - started}
        if outcome is not None:
            row["outcome"] = outcome
        with phase_lock:
            descriptor = os.open(output / "private-worker-phases.jsonl",
                os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
            with os.fdopen(descriptor, "a") as stream:
                stream.write(json.dumps(row) + "\n")
    service_root = (args.service_root or output / "service").resolve()
    service_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    clock_file = service_root / "private-fixture-clock.json"
    initial_time = datetime.fromisoformat("2026-10-21T00:00:00+00:00").timestamp()
    if clock_file.exists():
        initial_time = json.loads(clock_file.read_text())["now"]
    with ThreadedServer(("127.0.0.1", args.port), QuietHandler) as server:
        boundary.ports.add(server.server_port)
        origin = f"http://127.0.0.1:{server.server_port}"
        clock = Clock(initial_time)

        def observe_model(request):
            body = request.content
            payload = json.loads(body) if body else {}
            with lock:
                number = len(requests) + 1
                requests.append({"method": request.method, "path": request.url.path,
                    "wire_sha256": hashlib.sha256(body).hexdigest(), "model": payload.get("model")})
                (output / f"private-wire-{number:04d}.bin").write_bytes(body)

        if args.minimal:
            controlled = build_resilience_host(service_root, clock=clock, origin=origin,
                options={"limits": {"capacity": 4, "concurrency": 1, "per_principal": 2, "max_wait_seconds": 120},
                    "rpm": 200, "seconds": 3600, "retry_after_seconds": 3600, "shared_budget": True},
                before_response=observe_model)
            host = controlled.bundle
            side_requests = {"source": [], "accounting": []}
        else:
            controlled = build_resilience_health_host(service_root, clock=clock, origin=origin,
                before_response=observe_model, held_timeout_seconds=3600 if args.full_workflow else 600)
            host = controlled.host
            side_requests = controlled.side_requests
        queue, controller = controlled.queue, controlled.controller
        flags = {"read_failure": None}
        transports = []
        if args.minimal:
            for kind in side_requests:
                def unexpected(request, source=kind):
                    side_requests[source].append({"method": request.method, "path": request.url.path})
                    return httpx.Response(503, json={"error": "PRIVATE_RESILIENCE_SOURCE_CANARY"})
                transports.append(httpx.Client(transport=httpx.MockTransport(unexpected), trust_env=False))
            host.app.source = OpenRouterPoolSource(transports[0], fixtures()[2])
            host.app.gateway.billing.source = OpenRouterAccounting(transports[1], api_key=uuid4().hex)

        def inventory(digest):
            folders = {host.app.gateway.service.root}
            folders.update(contract.runner.client.root for contract in host.app.gateway.service.contracts.values())
            return {str(path.relative_to(host.root)): digest(path)
                for folder in folders for path in sorted(folder.rglob("*.json")) if path.is_file()}

        def hashes(paths, digest):
            return {str(path.relative_to(host.root)): digest(path)
                for path in sorted(paths) if path.is_file()}

        def receipt():
            receipt_started = time.monotonic()
            digests = {}

            def digest(path):
                if path not in digests:
                    digests[path] = hashlib.sha256(path.read_bytes()).hexdigest()
                return digests[path]

            budgets = {str(contract.runner.store.path.relative_to(host.root)): contract.runner.store.report()
                for contract in host.app.gateway.service.contracts.values()}
            health = {} if args.minimal else hashes([host.health.path], digest)
            if not args.minimal and (not health or host.health.path.stat().st_size == 0):
                raise AssertionError("A real nonempty HealthStore SQLite is required")
            raw_status = "complete"
            try:
                raw_requests = [json.loads(path.read_text()) for path in sorted(host.root.glob("private-request-*.json"))]
                method_counts = {method: sum(row["method"] == method for row in raw_requests)
                    for method in sorted({row["method"] for row in raw_requests})}
            except (OSError, ValueError):
                # A still-writing controlled receipt is unknown, never zero hidden calls.
                raw_status, method_counts = "incomplete", None
            return {"schema_version": 1, "pid": os.getpid(), "origin": origin, "clock": clock(),
                "scope": "initial queue preservation slice; no Health" if args.minimal else
                    "actual BackupCategoryCore/BudgetedCore/TaskService/Identity/Health/Queue/Capacity/Windows; MockTransport only",
                "http_responses": list(responses), "fixture_commands": list(commands),
                "model_requests": list(requests), "source_requests": list(side_requests["source"]),
                "accounting_requests": list(side_requests["accounting"]), "budget": budgets,
                "capacity_sha256": hashlib.sha256(controller.store.path.read_bytes()).hexdigest(),
                "queue_sha256": hashlib.sha256(queue.path.read_bytes()).hexdigest(),
                "health_sqlite_sha256": health,
                "wire_files_sha256": hashes(host.root.glob("private-wire-*.bin"), digest),
                "raw_mock_request_sha256": hashes(host.root.glob("private-request-*.json"), digest),
                "raw_mock_method_counts": method_counts,
                "raw_mock_metadata_status": raw_status,
                "receipt_artifacts_sha256": hashes((path for folder in host.root.glob("receipts-*")
                    for path in folder.rglob("*.json")), digest),
                "task_jobs_sha256": hashes((host.root / "tasks").rglob("job.json"), digest),
                "preservation_sha256": hashes([controller.store.integrity.head, queue.integrity.head], digest),
                "health_evidence_status": "present" if health else "not_in_initial_factory",
                "original_evidence_sha256": inventory(digest), "worker_running": any(w.is_alive() for w in workers),
                "worker_running_count": sum(w.is_alive() for w in workers),
                "model_response_held": not args.minimal and controlled.held.is_set() and not controlled.released.is_set(),
                "held_timeout_seconds": None if args.minimal else controlled.held_timeout_seconds,
                "worker_results": list(results),
                "wall_elapsed_seconds": time.monotonic() - started,
                "blocked_server_socket_attempts": [str(item) for item in boundary.blocked],
                "server_socket_attempts": [str(item) for item in boundary.connected],
                "receipt_build_seconds": time.monotonic() - receipt_started}

        def save():
            with lock:
                snapshot = receipt()
                temporary = output / "http-receipt.pending"
                temporary.write_text(json.dumps(snapshot, indent=2))
                temporary.replace(output / "http-receipt.json")
                clock_file.write_text(json.dumps({"now": clock()}))
                return snapshot

        def app(environ, start_response):
            read_started = time.monotonic()
            row = {"method": environ.get("REQUEST_METHOD"), "path": environ.get("PATH_INFO"),
                "started_elapsed_seconds": read_started - started}
            def start(status, headers, exc_info=None):
                row["status"] = int(status.split()[0])
                return start_response(status, headers, exc_info)
            try:
                if flags["read_failure"] == row["path"] and row["method"] == "GET":
                    body = b'{"error":{"code":"monitor_or_source_unavailable"}}'
                    start("503 Service Unavailable", [("Content-Type", "application/json"), ("Content-Length", str(len(body)))])
                    return [body]
                return host.app(environ, start)
            except BaseException as error:
                row["exception_type"] = type(error).__name__
                raise
            finally:
                row["duration_seconds"] = time.monotonic() - read_started
                responses.append(row)
                with (output / "actual-http-durations.jsonl").open("a") as stream:
                    stream.write(json.dumps(row) + "\n")

        server.set_app(app)
        serving = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        serving.start()
        save()
        print(json.dumps({"type": "ready", "origin": origin, "pid": os.getpid(),
            "category": CATEGORY if args.minimal else host.category,
            "policy_version": "synthetic-queued-admission-v1" if args.minimal else controlled.policy_version,
            "held_timeout_seconds": None if args.minimal else controlled.held_timeout_seconds,
            "operator_token": host.operator_token,
            "credentials": {name: value.token for name, value in host.credentials.items()}}), flush=True)
        try:
            for line in sys.stdin:
                request = json.loads(line)
                name = request["command"]
                command_started = time.monotonic()
                if name == "progress":
                    progress = {"type": "progress",
                        "thread_running_count": sum(worker.is_alive() for worker in workers),
                        "held": not args.minimal and controlled.held.is_set() and not controlled.released.is_set(),
                        "requests_count": len(requests)}
                    timing = {"command": name, "started_elapsed_seconds": command_started - started,
                        "duration_seconds": time.monotonic() - command_started}
                    commands.append(timing)
                    with (output / "actual-private-progress.jsonl").open("a") as stream:
                        stream.write(json.dumps({**timing, **progress}) + "\n")
                    print(json.dumps(progress), flush=True)
                    continue
                if name == "stop":
                    break
                if name == "thread_dump":
                    if thread_dump_written:
                        raise ValueError("Only one private thread dump is permitted per boot")
                    thread_dump_written = True
                    reference = "private-all-thread-stacks.txt"
                    descriptor = os.open(output / reference, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                    with os.fdopen(descriptor, "w") as stream:
                        # CPython bounds this to 100 threads/100 frames; no source code or locals.
                        faulthandler.dump_traceback(file=stream, all_threads=True)
                    commands.append({"command": name, "started_elapsed_seconds": command_started - started,
                        "duration_seconds": time.monotonic() - command_started})
                    print(json.dumps({"type": "thread_dump", "ref": reference}), flush=True)
                    continue
                if name == "work":
                    if sum(worker.is_alive() for worker in workers) >= (1 if args.minimal else 4):
                        raise ValueError("Private worker bound reached")
                    worker_id = len(workers) + 1
                    def work(worker_id=worker_id):
                        try:
                            worker_phase(worker_id, "dispatch_enter")
                            result = queue.dispatch_next()
                            worker_phase(worker_id, "dispatch_return")
                            results.append({key: result[key] for key in ("status", "run_id", "reason") if key in result}
                                if isinstance(result, dict) else {"status": "returned"})
                        except Exception as error:
                            worker_phase(worker_id, "worker_exception")
                            import traceback
                            with (output / f"private-worker-{len(results):04d}-failure.txt").open("x") as stream:
                                stream.write(traceback.format_exc())
                            results.append({"status": "exception", "type": type(error).__name__})
                        finally:
                            worker_phase(worker_id, "finally_save_enter")
                            saved = False
                            try:
                                save()
                                saved = True
                            finally:
                                worker_phase(worker_id, "finally_save_end", "returned" if saved else "raised")
                    worker = threading.Thread(target=work, name=f"resilience-worker-{worker_id}", daemon=True)
                    workers.append(worker)
                    worker.start()
                    timing = {"command": name, "worker_id": worker_id,
                        "started_elapsed_seconds": command_started - started,
                        "duration_before_ack_seconds": time.monotonic() - command_started}
                    commands.append(timing)
                    acknowledgement = {"type": "work_started", "completion_confirmed": False,
                        "pid": os.getpid(), "worker_id": worker_id,
                        "thread_running_count": sum(item.is_alive() for item in workers),
                        "requests_count": len(requests),
                        "held": not args.minimal and controlled.held.is_set() and not controlled.released.is_set()}
                    descriptor = os.open(output / "private-work-starts.jsonl",
                        os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
                    with os.fdopen(descriptor, "a") as stream:
                        stream.write(json.dumps({**timing, **acknowledgement}) + "\n")
                        stream.flush()
                        os.fsync(stream.fileno())
                    print(json.dumps(acknowledgement), flush=True)
                    continue
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
                elif name == "fault_missing_queue_table":
                    if any(worker.is_alive() for worker in workers):
                        raise ValueError("Fault copies require no active worker")
                    copied_database_fault(queue.path,
                        output / "PRIVATE_RESILIENCE_PATH_CANARY-preserved-queue",
                        omit=("jobs",))
                elif name == "fault_companion_loss" and not args.minimal:
                    if any(worker.is_alive() for worker in workers):
                        raise ValueError("Fault copies require no active worker")
                    archive = output / "PRIVATE_RESILIENCE_PATH_CANARY-preserved-companion"
                    copied_database_fault(controller.store.path, archive)
                    original = controller.store.integrity.root
                    before = {str(path.relative_to(original)): hashlib.sha256(path.read_bytes()).hexdigest()
                        for path in original.rglob("*") if path.is_file()}
                    retained = archive / original.name
                    original.rename(retained)
                    original.mkdir(mode=0o700)
                    descriptor = os.open(original / "lock", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                    os.close(descriptor)
                    after = {str(path.relative_to(retained)): hashlib.sha256(path.read_bytes()).hexdigest()
                        for path in retained.rglob("*") if path.is_file()}
                    if before != after:
                        raise AssertionError("Original companion changed while retaining fault evidence")
                    (archive / "companion-receipt.json").write_text(json.dumps({
                        "original_unchanged": True, "original_files_sha256": before,
                        "replacement": "new empty companion; no saved witness copied"}, indent=2))
                elif name == "fault_allocation_index" and not args.minimal:
                    import sqlite3
                    if any(worker.is_alive() for worker in workers):
                        raise ValueError("Fault copies require no active worker")
                    if request.get("run_id") not in {"resilience-held", "resilience-fallback"}:
                        raise ValueError("Only an owned actual original allocation may be copied")
                    identity = host.service.internal_id("studio", request["run_id"])
                    with sqlite3.connect(controller.store.path) as db:
                        rows = [(key, json.loads(payload)) for key, payload in
                            db.execute("SELECT id,payload FROM capacity_allocations")]
                    matching = [(key, value) for key, value in rows if value["run"] == identity]
                    if not matching:
                        raise ValueError("An actual saved original allocation is required")
                    key, _ = matching[-1]
                    copied_database_fault(controller.store.path,
                        output / "PRIVATE_RESILIENCE_PATH_CANARY-preserved-scope",
                        updates=[("UPDATE capacity_allocations SET state='complete',horizon=0 WHERE id=?", (key,))])
                elif name == "scenario" and not args.minimal:
                    if request.get("name") not in {"success", "window", "unknown", "unknown_fee", "availability", "quality"}:
                        raise ValueError("Unknown bounded response scenario")
                    controlled.flags["scenario"] = request["name"]
                elif name == "hold" and not args.minimal:
                    if any(worker.is_alive() for worker in workers):
                        raise ValueError("Completion gate cannot replace an active request")
                    controlled.held.clear()
                    controlled.released.clear()
                    controlled.flags["hold"] = True
                elif name == "release" and not args.minimal:
                    controlled.released.set()
                    controlled.flags["hold"] = False
                elif name == "planned_crash" and not args.minimal:
                    if not controlled.held.is_set() or controlled.released.is_set():
                        raise ValueError("Planned crash requires an actual unreleased model POST")
                    commands.append({"command": name})
                    snapshot = save()
                    with (output / "planned-crash.json").open("x") as stream:
                        json.dump({"intentional_exit": 86, "released": False, **snapshot}, stream, indent=2)
                    (service_root / "private-planned-stop.json").write_text(json.dumps({"pid": os.getpid()}))
                    print(json.dumps({"type": "planned_crash", "pid": os.getpid(), "intentional_exit": 86}), flush=True)
                    os._exit(86)
                elif name == "mark_stopped_workers" and not args.minimal:
                    prior = json.loads((service_root / "private-planned-stop.json").read_text())["pid"]
                    if prior == os.getpid():
                        raise ValueError("Recovery marking requires a different stopped process")
                    try:
                        os.kill(prior, 0)
                    except ProcessLookupError:
                        pass
                    else:
                        raise ValueError("Prior process is not proven stopped")
                    results.append({"status": "explicit_stopped_worker_marking",
                        "changed": queue.recover_inflight(workers_stopped=True)})
                elif name == "rotate_operator":
                    host.app.admin_token = uuid4().hex
                elif name == "revoke_agent":
                    host.app.identity_store.revoke(host.credentials["agent"].id)
                elif name in {"fail_read", "restore_read"}:
                    if name == "fail_read" and request.get("path") not in {"/monitor/capacity", "/monitor/queue"}:
                        raise ValueError("Only existing monitor reads may fail")
                    flags["read_failure"] = request.get("path") if name == "fail_read" else None
                elif name != "inspect":
                    raise ValueError("Unknown private fixture command")
                commands.append({"command": name, "started_elapsed_seconds": command_started - started,
                    "duration_before_receipt_seconds": time.monotonic() - command_started})
                snapshot = save()
                print(json.dumps({"type": "inspected", **snapshot,
                    **({"reauth_token": host.app.admin_token} if name == "rotate_operator" else {})}), flush=True)
        finally:
            if not args.minimal and controlled.held.is_set() and not controlled.released.is_set():
                snapshot = save()
                with (output / "unreleased-failure-shutdown.json").open("x") as stream:
                    json.dump({"intentional_exit": 87, "released": False, **snapshot}, stream, indent=2)
                os._exit(87)  # Failed-run cleanup preserves uncertainty; never a passing restart proof.
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

#!/usr/bin/env python3
"""Private real-HTTP synthetic fixture; its controls are never product routes."""
import argparse
from copy import deepcopy
from datetime import timedelta
import json
from pathlib import Path
import sys
import threading

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
from resilience_fixture import assemble_resilience_host  # noqa: E402
from test_local_http_integration import (  # noqa: E402
    LoopbackEvidence, QuietHandler, ThreadedServer, configure_loopback_network,
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    output = parser.parse_args().output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    boundary, responses = LoopbackEvidence(), []
    configure_loopback_network(boundary, setattr)
    with ThreadedServer(("127.0.0.1", 0), QuietHandler) as server:
        boundary.ports.add(server.server_port)
        origin = f"http://127.0.0.1:{server.server_port}"
        host = assemble_resilience_host(output / "fixture", origin)
        host.scenario = "first_pass"
        flags = {"pool_failure": False, "delay_pool": False}
        delayed, release_pool, held, release = (threading.Event() for _ in range(4))
        core = host.service.contracts["history-backup-v1"].runner.core
        privacy = core.execution_policy
        original = deepcopy(core._backup["pools"]["open_source"][0]["evidence"])
        jobs = []
        save_lock = threading.RLock()

        def state():
            return {"model_requests": list(host.requests), "model_mock_calls": len(host.calls),
                "source_requests": host.side_requests["source"], "accounting_requests": host.side_requests["accounting"],
                "budget": host.budget.report(), "control": host.app.store.control(),
                "long_task_held": held.is_set() and not release.is_set(), "pool_response_held": delayed.is_set() and not release_pool.is_set()}

        def save():
            with save_lock:
                receipt = {"schema_version": 1, "scope": "actual HTTP; synthetic real backup host; MockTransport upstream only",
                    "http_responses": list(responses), **state(),
                    "socket_attempts": [{"host": item[0], "port": item[1]} for item in boundary.connected],
                    "blocked_socket_attempts": [str(item) for item in boundary.blocked]}
                pending = output / "http-receipt.pending"
                pending.write_text(json.dumps(receipt, indent=2))
                pending.replace(output / "http-receipt.json")

        def app(environ, start_response):
            response = {"method": environ.get("REQUEST_METHOD"), "path": environ.get("PATH_INFO")}
            def start(status, headers, exc_info=None):
                response["status"] = int(status.split()[0])
                return start_response(status, headers, exc_info)
            if response["path"] == "/monitor/pools" and flags["pool_failure"]:
                body = b'{"error":{"code":"monitor_or_source_unavailable"}}'
                start("503 Service Unavailable", [("Content-Type", "application/json"), ("Content-Length", str(len(body)))])
                result = [body]
            else:
                result = host.app(environ, start)
            if response["path"] == "/monitor/pools" and flags["delay_pool"]:
                flags["delay_pool"] = False
                delayed.set()
                assert release_pool.wait(30), "Synthetic HTTP response gate was not released"
            responses.append(response)
            save()
            return result

        server.set_app(app)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        thread.start()

        def submit(run, principal="studio"):
            with httpx.Client(base_url=origin, trust_env=False, timeout=90,
                    headers={"Authorization": "Bearer " + host.credentials[principal].token}) as client:
                result = client.post("/v1/tasks", json={"run_id": run, "category": host.category,
                    "prompt": "Return only 1+1. PRIVATE_RESILIENCE_BROWSER_PROMPT"})
                assert result.status_code == 200

        try:
            submit("resilience-completed")
            submit("resilience-other-client", "agent")
            print(json.dumps({"type": "ready", "origin": origin, "operator_token": host.operator_token,
                "client_token": host.credentials["studio"].token,
                "pools": host.pools, "category": host.category, "state": state()}), flush=True)
            for line in sys.stdin:
                command = json.loads(line)["command"]
                if command == "stop":
                    break
                if command == "hold":
                    def wait(_):
                        held.set()
                        assert release.wait(90), "Synthetic model response gate was not released"
                    host.before_response = wait
                    job = threading.Thread(target=submit, args=("resilience-long", "agent"), daemon=True)
                    jobs.append(job)
                    job.start()
                    assert held.wait(5)
                elif command == "release":
                    release.set()
                    for job in jobs:
                        job.join(15)
                        assert not job.is_alive()
                    host.before_response = None
                elif command == "advance_health":
                    host.values["now"] += timedelta(seconds=300)
                elif command == "damage_candidate":
                    core._backup["pools"]["open_source"][0]["evidence"] = ["PRIVATE_UPSTREAM_FAILURE_MARKER"]
                elif command == "restore_candidate":
                    core._backup["pools"]["open_source"][0]["evidence"] = deepcopy(original)
                elif command == "unavailable_contract":
                    core.execution_policy = None
                elif command == "restore_contract":
                    core.execution_policy = privacy
                elif command == "fail_pool":
                    flags["pool_failure"] = True
                elif command == "restore_pool":
                    flags["pool_failure"] = False
                elif command == "delay_pool":
                    delayed.clear()
                    release_pool.clear()
                    flags["delay_pool"] = True
                elif command == "release_pool":
                    release_pool.set()
                else:
                    assert command == "state"
                save()
                print(json.dumps({"type": "state", "state": state()}), flush=True)
        finally:
            release.set()
            release_pool.set()
            server.shutdown()
            thread.join(2)
            for job in jobs:
                job.join(2)
            for client in host.observation_http:
                client.close()
            save()
            assert not boundary.blocked


if __name__ == "__main__":
    main()

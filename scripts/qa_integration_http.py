#!/usr/bin/env python3
"""Disposable final desktop host; model and metadata upstreams use MockTransport.

Commands on stdin are fixture control only. This host neither creates product
endpoints nor reads real supplier credentials. Evidence exports public projections.
"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import threading

import httpx

from cloudyfan_monitor.demo import CATEGORY
from cloudyfan_monitor.integration import ACCESS_SECONDS, build_local_integration
from cloudyfan_monitor.pools import OpenRouterPoolSource

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from test_local_http_integration import (  # noqa: E402
    LoopbackEvidence, QuietHandler, ThreadedServer, configure_loopback_network,
)
from test_sources import NOW, fixtures  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    output = parser.parse_args().output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    boundary = LoopbackEvidence()
    configure_loopback_network(boundary, setattr)
    source_calls, responses, commands = [], [], []
    held, release = threading.Event(), threading.Event()
    lock = threading.RLock()
    clock = [datetime(2026, 10, 22, 5, 0, tzinfo=timezone.utc).timestamp() - ACCESS_SECONDS]
    models, benchmark, policies = fixtures()
    source_round = [0]

    def source_response(request):
        assert request.method == "GET"
        if request.url.path.endswith("/models"):
            source_round[0] += 1
            response = httpx.Response(503 if source_round[0] > 1 else 200, json={"data": models})
        elif request.url.path.endswith("/benchmarks"):
            response = httpx.Response(200, json=benchmark if not request.url.query else
                                      {"meta": {"version": "v1"}, "data": []})
        else:
            model = request.url.path.removeprefix("/api/v1/models/").removesuffix("/endpoints")
            response = httpx.Response(503, json={"error": "synthetic endpoint unavailable"}) if model == "example/a" else httpx.Response(200, json={"data": {
                "id": model, "endpoints": [{"tag": "provider/one", "model_id": model,
                "provider_name": "Provider", "pricing": {"prompt": "0.0000001", "completion": "0.0000001"},
                "latency_last_30m": {"p50": 200, "p90": 500}}]}})
        source_calls.append({"method": request.method, "path": request.url.path,
                             "query": str(request.url.query, "ascii"), "status": response.status_code})
        return response

    class BrowserSource(OpenRouterPoolSource):
        def refresh(self):
            view = super().refresh()
            view["source_kind"] = "offline_mock_source_partial_fixture"
            return view

    with ThreadedServer(("127.0.0.1", 0), QuietHandler) as server:
        boundary.ports.add(server.server_port)
        origin = f"http://127.0.0.1:{server.server_port}"
        bundle = build_local_integration(output / "fixture", clock=lambda: clock[0], origin=origin, seed=False)
        bundle.upstream.fail_tiers = set()
        payload = {"category": CATEGORY, "prompt": "Return only 1+1."}
        bundle.app.gateway.create("studio", {**payload, "run_id": "browser-expired-history"})
        clock[0] += ACCESS_SECONDS
        for index, mode in enumerate(("cloudyfan", "fixed_high", "low_first", "open_source_first")):
            principal = "studio" if index % 2 == 0 else "agent"
            bundle.app.gateway.switch(bundle.app.store.control()["revision"], mode, principal)
            bundle.app.gateway.create(principal, {**payload, "run_id": "browser-" + mode})
        for principal in ("studio", "agent"):
            bundle.app.gateway.switch(bundle.app.store.control()["revision"], None, principal)
        bundle.app.source = BrowserSource(httpx.Client(transport=httpx.MockTransport(source_response)),
                                         policies, endpoint_limit=2, clock=lambda: NOW)

        def state():
            snapshot = bundle.app.gateway.snapshot()
            return {"summary": snapshot["summary"], "control": snapshot["control"],
                    "tasks": [{key: item.get(key) for key in ("client", "run", "status", "mode", "pool")}
                              for item in snapshot["tasks"]],
                    "ledger_known_usd": snapshot["ledger"]["known_usd"],
                    "model_mock_calls": len(bundle.upstream.calls), "source_mock_calls": len(source_calls)}

        def save():
            with lock:
                receipt = {"schema_version": 1, "origin": origin, "http_responses": responses,
                    "fixture_commands": commands, "source_mock_requests": source_calls,
                    "mock_upstream_call_count": len(bundle.upstream.calls),
                    "mock_upstream_calls": [{"model": call.get("model"),
                        "kind": "classification" if "questions" in call else "generation"}
                        for call in bundle.upstream.calls],
                    "allowed_loopback_ports": sorted(boundary.ports),
                    "socket_attempts": [{"host": item[0], "port": item[1]} for item in boundary.connected],
                    "blocked_socket_attempts": [str(item) for item in boundary.blocked],
                    "network_policy": "deny Python outbound sockets except this loopback port; model and metadata use MockTransport",
                    "scope": "disposable synthetic final desktop fixture"}
                pending = output / "http-receipt.pending"
                pending.write_text(json.dumps(receipt, indent=2))
                pending.replace(output / "http-receipt.json")

        def app(environ, start_response):
            observed = {"method": environ.get("REQUEST_METHOD"), "path": environ.get("PATH_INFO"),
                        "remote_addr": environ.get("REMOTE_ADDR"), "port": server.server_port}

            def status(value, headers, exc_info=None):
                observed["status"] = int(value.split()[0])
                return start_response(value, headers, exc_info)

            response = bundle.app(environ, status)
            responses.append(observed)
            save()
            return response

        server.set_app(app)

        def hold_response(_):
            held.set()
            if not release.wait(120):
                raise AssertionError("Desktop fixture did not release accepted work")

        bundle.upstream.before_response = hold_response
        long_outcome = []

        def start_long_task():
            with httpx.Client(base_url=origin, timeout=130, trust_env=False,
                             headers={"Authorization": "Bearer " + bundle.credentials["agent"].token}) as client:
                response = client.post("/v1/tasks", json={**payload, "run_id": "browser-busy"})
                long_outcome.append({"status": response.status_code, "task_status": response.json()["status"]})
            save()

        def input_commands():
            for line in sys.stdin:
                command = json.loads(line)
                assert command["command"] in ("state", "release", "stop")
                commands.append({"command": command["command"], "mock_call_count": len(bundle.upstream.calls)})
                if command["command"] == "release":
                    release.set()
                    long_thread.join(15)
                    assert long_outcome == [{"status": 200, "task_status": "passed"}]
                    save()
                    print(json.dumps({"type": "released", "state": state()}), flush=True)
                elif command["command"] == "state":
                    save()
                    print(json.dumps({"type": "state", "state": state()}), flush=True)
                else:
                    release.set()
                    server.shutdown()
                    return
            release.set()
            server.shutdown()

        service_thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        service_thread.start()
        long_thread = threading.Thread(target=start_long_task, daemon=True)
        long_thread.start()
        try:
            assert held.wait(10), "Long task did not reach the mocked upstream"
            save()
            print(json.dumps({"type": "ready", "origin": origin}), flush=True)
            input_commands()
        finally:
            release.set()
            server.shutdown()
            service_thread.join(2)
            long_thread.join(2)
            save()
            assert not boundary.blocked, "Unexpected fixture network attempt"


if __name__ == "__main__":
    main()

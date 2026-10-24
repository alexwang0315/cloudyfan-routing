#!/usr/bin/env python3
"""Disposable historical desktop host: actual HTTP, synthetic frozen evidence."""
import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import sys
import threading

import httpx

from cloudyfan_monitor.integration import assemble_monitor, build_local_integration

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from history_fixture import OPERATOR, assemble_backup_host  # noqa: E402
from test_local_http_integration import (  # noqa: E402
    LoopbackEvidence, QuietHandler, ThreadedServer, configure_loopback_network,
)


@dataclass
class HistoricalFixture:
    app: object
    hosts: dict
    base: object
    legacy: object

    def state(self):
        return {"model_mock_calls": sum(len(host.calls) for host in self.hosts.values()) + len(self.legacy.upstream.calls),
                "budgets": {key: host.budget.report() for key, host in self.hosts.items()},
                "scope": "synthetic offline evidence; actual Core, BudgetedCore, TaskService and HealthStore"}


def assemble_history_fixture(root, origin):
    hosts = {name: assemble_backup_host(root / name, origin, **options) for name, options in (
        ("first-pass", {}), ("direct", {}), ("economic", {"classifiers": ("low_mid",), "economic": False}),
        ("classified", {"classifiers": ("low_mid",)}),
        ("quality-chain", {"classifiers": ("low_mid", "mid_high")}), ("availability", {}))}
    hosts["first-pass"].scenario = "first_pass"
    hosts["availability"].scenario = "availability"
    hosts["quality-chain"].scenario = "quality_chain"
    base, registry = hosts["first-pass"], {}
    service, store = base.service, base.app.store
    for name, host in hosts.items():
        contract_id = "history-" + name
        service.contracts[contract_id] = host.service.contracts["history-backup-v1"]
        view = host.app.store.pool(host.app.store.control()["routing_pool"])
        view.pop("id")
        view["source_kind"] = "synthetic_history_" + name
        pool = store.save_pool(view)
        registry[(pool, host.values["plan"]["category_id"], "cloudyfan")] = contract_id
    legacy = build_local_integration(root / "legacy", origin=origin, clock=lambda: base.values["now"].timestamp())
    legacy.upstream.fail_tiers = set()
    service.contracts["history-legacy"] = legacy.app.gateway.service.contracts["demo-cloudyfan"]
    view = legacy.app.store.pool(legacy.app.store.control()["routing_pool"])
    view.pop("id")
    view["source_kind"] = "synthetic_history_legacy"
    pool = store.save_pool(view)
    registry[(pool, base.values["plan"]["category_id"], "cloudyfan")] = "history-legacy"
    app = assemble_monitor(store, service, registry, base.app.identity_store,
        operator_token=OPERATOR, origin=origin, demo=True)
    return HistoricalFixture(app, hosts, base, legacy)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    output = parser.parse_args().output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    boundary, responses = LoopbackEvidence(), []
    configure_loopback_network(boundary, setattr)
    with ThreadedServer(("127.0.0.1", 0), QuietHandler) as server:
        boundary.ports.add(server.server_port)
        origin = f"http://127.0.0.1:{server.server_port}"
        fixture = assemble_history_fixture(output / "fixture", origin)

        def save():
            receipt = {"schema_version": 1, "scope": "synthetic history; actual loopback HTTP; MockTransport models",
                "http_responses": responses, "model_mock_calls": fixture.state()["model_mock_calls"],
                "socket_attempts": [{"host": item[0], "port": item[1]} for item in boundary.connected],
                "blocked_socket_attempts": [str(item) for item in boundary.blocked]}
            (output / "http-receipt.json").write_text(json.dumps(receipt, indent=2))

        def app(environ, start_response):
            observed = {"method": environ.get("REQUEST_METHOD"), "path": environ.get("PATH_INFO")}
            def status(value, headers, exc_info=None):
                observed["status"] = int(value.split()[0])
                return start_response(value, headers, exc_info)
            response = fixture.app(environ, status)
            responses.append(observed)
            return response

        server.set_app(app)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        thread.start()
        try:
            with httpx.Client(base_url=origin, trust_env=False, timeout=20) as client:
                operator = {"Authorization": "Bearer " + OPERATOR}
                customer = {"Authorization": "Bearer " + fixture.base.credentials["studio"].token}
                for (pool, category, _), contract_id in fixture.app.gateway.registry.items():
                    current = client.get("/monitor/snapshot", headers=operator).json()
                    changed = client.post("/monitor/pools/activate", headers=operator,
                        json={"pool": pool, "revision": current["control"]["revision"]})
                    assert changed.status_code == 200
                    name = contract_id.removeprefix("history-")
                    task = client.post("/v1/tasks", headers=customer, json={"run_id": "history-" + name,
                        "category": category, "prompt": "Return only 1+1. PRIVATE-HISTORY-PROMPT"})
                    assert task.status_code == 200 and task.json()["status"] == "passed"
            save()
            print(json.dumps({"type": "ready", "origin": origin, "state": fixture.state()}), flush=True)
            for line in sys.stdin:
                command = json.loads(line)["command"]
                if command == "stop":
                    break
                if command == "health":
                    host = fixture.hosts["availability"]
                    row = host.values["plan"]["pools"]["open_source"][0]
                    observed = host.values["module"].observation(now=host.values["now"],
                        provider=row["entry"]["profile"]["provider"], configuration=row["configuration_sha256"])
                    observed["health"].update(status=4, status_meaning="degraded")
                    host.health.publish([observed])
                else:
                    assert command == "state"
                save()
                print(json.dumps({"type": "state", "state": fixture.state()}), flush=True)
        finally:
            server.shutdown()
            thread.join(2)
            save()
            assert not boundary.blocked


if __name__ == "__main__":
    main()

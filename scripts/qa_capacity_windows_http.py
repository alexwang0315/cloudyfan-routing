#!/usr/bin/env python3
"""Private real HTTP host for window UI QA; all upstreams are MockTransport."""
import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import sys
import threading
from uuid import uuid4


def build_window_host(output, origin, clock):
    """Compose real stores/BackupCore plus controlled Mock responses; starts no server.

    Returned host/controller/windows and flags/clock/wire records are fixture-only.
    Callers own transport shutdown; no product endpoint exposes these controls.
    """
    from types import SimpleNamespace
    package = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(package / "tests"))
    import httpx
    from cloudyfan_jev.capacity import CapacityStore
    from cloudyfan_jev.capacity_dispatch import CapacityController, CapacityRoute
    from cloudyfan_jev.capacity_windows import CapacityWindows
    from cloudyfan_monitor.capacity import CapacityObserver
    from cloudyfan_monitor.integration import assemble_monitor
    from cloudyfan_monitor.openrouter_accounting import OpenRouterAccounting
    from cloudyfan_monitor.pools import OpenRouterPoolSource
    from candidate_health_fixture import assemble_health_host
    from capacity_sources_fixture import limit
    from test_sources import fixtures
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    held, released = threading.Event(), threading.Event()
    flags = {"hold": False, "read_failure": False, "scenario": "success"}
    requests = []
    host = assemble_health_host(output / "service", origin, clock=clock)

    def upstream(request):
        payload = json.loads(request.content)
        number = len(requests) + 1
        requests.append({"method": request.method, "path": request.url.path,
            "wire_sha256": hashlib.sha256(request.content).hexdigest(), "model": payload.get("model")})
        (output / f"private-wire-{number:03d}.bin").write_bytes(request.content)
        host.calls.append({"method": request.method, "path": request.url.path, "payload": payload})
        if flags["hold"]:
            held.set()
            if not released.wait(45):
                raise httpx.ReadTimeout("Synthetic window completion gate timed out")
        scenario = flags["scenario"]
        if scenario != "success":
            flags["scenario"] = "success"
        if scenario in {"window", "unknown", "unknown_fee"}:
            response = host.values["module"].response(payload, number, status=429,
                cost=None if scenario == "unknown_fee" else ".01")
            body = response.json()
            body["error"]["message"] = "PRIVATE_WINDOW_PROVIDER_ERROR"
            return httpx.Response(429, json=body, headers={
                "Retry-After": "not-a-time" if scenario == "unknown" else "30",
                "X-Private-Provider": "PRIVATE_WINDOW_HEADER_CANARY"})
        return host.values["module"].response(payload, number,
            answer="bad" if scenario == "quality" else "2")

    for contract in host.service.contracts.values():
        contract.runner.client.http = httpx.Client(transport=httpx.MockTransport(upstream), trust_env=False)
    profiles = [row["entry"]["profile"] for rows in host.values["plan"]["pools"].values() for row in rows]
    profiles.append(host.values["decision"])
    bindings, groups, rules = [], [], []
    for index in range(4):
        name = f"provider-{index}"
        scope = {"kind": "account", "ref": f"synthetic-window-account-{index}"}
        bindings.append({"id": name, "provider": "openrouter", "account_ref": scope["ref"],
            "credential_ref": f"synthetic-window-key-{index}", "endpoint_ref": f"synthetic-window-endpoint-{index}",
            "groups": [name]})
        groups.append({"id": name, "scope": scope,
            "source": {"kind": "controlled_import", "version": "synthetic-window-source-v1"},
            "supplier_limits": [], "local_limits": [limit("rpm", 100, clock()),
                limit("tpm", None, clock(), status="not_applicable"), limit("concurrency", 2, clock())]})
        rules.append({"binding": name, "group": name, "header": "retry-after",
            "format": "retry_after", "constraint": "requests"})
    config = {"schema_version": 1, "version": "synthetic-admission-config-v1", "bindings": bindings, "groups": groups}
    store = CapacityStore(host.root / "capacity.sqlite", clock=clock)
    store.configure(config)
    def credential_active(principal, credential, scope, now):
        return host.app.identity_store.is_active(principal) and any(
            row["id"] == credential and row["revoked_at"] is None and row["created_at"] <= now < row["expires_at"]
            and scope in row["scopes"] for row in host.app.identity_store.credentials(principal))
    windows = CapacityWindows(store, version="synthetic-windows-v1", rules=rules)
    controller = CapacityController(store, credential_active=credential_active, window_backend=windows)
    transport = host.service.contracts["health-backup-v1"].runner.client
    routes = []
    for profile in profiles:
        classifier = profile is host.values["decision"]
        model = profile.get("model_version", profile.get("version"))
        binding = "provider-3" if classifier else profile["provider"]
        endpoint = "https://openrouter.ai/api/alpha/decisions" if classifier else "https://openrouter.ai/api/v1/chat/completions"
        routes.append(CapacityRoute(transport, endpoint, model, binding,
            provider_name=profile["provider_name"], accepted_models=tuple(profile.get("accepted_models", (model,)))))
    for version, enabled in (("synthetic-window-admission-v1", True), ("synthetic-window-admission-v2", True),
                             ("synthetic-immediate-v1", False)):
        controller.register(version, configuration_version=config["version"], routes=routes, max_seconds=300,
            **({"windows_version": windows.version} if enabled else {}))
    old = host.app
    host.app = assemble_monitor(old.store, host.service, old.gateway.registry, old.identity_store,
        operator_token=host.operator_token, source=old.source, origin=origin, demo=True,
        capacity_observer=CapacityObserver(store, sources={}), capacity_controller=controller)
    side_requests = {"source": [], "accounting": []}
    observation_transports = []
    for kind in side_requests:
        def unexpected(request, source=kind):
            side_requests[source].append({"method": request.method, "path": request.url.path})
            return httpx.Response(503, json={"error": "PRIVATE_WINDOW_SOURCE_ERROR"})
        observation_transports.append(httpx.Client(transport=httpx.MockTransport(unexpected), trust_env=False))
    host.app.source = OpenRouterPoolSource(observation_transports[0], fixtures()[2])
    host.app.gateway.billing.source = OpenRouterAccounting(observation_transports[1], api_key=uuid4().hex)
    return SimpleNamespace(host=host, controller=controller, windows=windows, clock=clock, flags=flags,
        requests=requests, side_requests=side_requests, held=held, released=released,
        observation_transports=observation_transports)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    output = parser.parse_args().output.resolve()
    package = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(package / "tests"))
    from test_local_http_integration import (
        Clock, LoopbackEvidence, QuietHandler, ThreadedServer, configure_loopback_network,
    )

    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    boundary, responses, commands = LoopbackEvidence(), [], []
    configure_loopback_network(boundary, setattr)
    with ThreadedServer(("127.0.0.1", 0), QuietHandler) as server:
        boundary.ports.add(server.server_port)
        origin = f"http://127.0.0.1:{server.server_port}"
        clock = Clock(datetime.fromisoformat("2026-10-21T00:00:00+00:00").timestamp())
        fixture = build_window_host(output, origin, clock)
        host, controller, windows = fixture.host, fixture.controller, fixture.windows
        flags, held, released = fixture.flags, fixture.held, fixture.released
        requests, side_requests = fixture.requests, fixture.side_requests
        observation_transports = fixture.observation_transports
        lock = threading.RLock()

        def inventory(folder):
            return {str(p.relative_to(host.root)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted(folder.rglob("*.json")) if p.is_file()}

        def receipt():
            return {"schema_version": 1, "pid": os.getpid(), "origin": origin, "clock": clock(),
                "scope": "synthetic real BackupCategoryCore/BudgetedCore/TaskService/IdentityStore/HealthStore/CapacityStore/CapacityWindows; actual loopback HTTP; MockTransport only",
                "http_responses": list(responses), "fixture_commands": list(commands),
                "model_requests": list(requests), "source_requests": list(side_requests["source"]),
                "accounting_requests": list(side_requests["accounting"]), "budget": host.budget.report(),
                "capacity_sha256": hashlib.sha256(controller.store.path.read_bytes()).hexdigest(),
                "health_sha256": inventory(host.health.path),
                "original_evidence_sha256": {**inventory(host.root / "receipts"), **inventory(host.root / "tasks")},
                "window_snapshot": windows.snapshot(), "model_response_held": held.is_set() and not released.is_set(),
                "blocked_server_socket_attempts": [str(item) for item in boundary.blocked],
                "server_socket_attempts": [str(item) for item in boundary.connected]}

        def save():
            with lock:
                pending = output / "http-receipt.pending"
                pending.write_text(json.dumps(receipt(), indent=2))
                pending.replace(output / "http-receipt.json")

        def app(environ, start_response):
            response = {"method": environ.get("REQUEST_METHOD"), "path": environ.get("PATH_INFO")}

            def start(status, headers, exc_info=None):
                response["status"] = int(status.split()[0])
                return start_response(status, headers, exc_info)

            if flags["read_failure"] and response == {"method": "GET", "path": "/monitor/capacity"}:
                body = b'{"error":{"code":"monitor_or_source_unavailable"}}'
                start("503 Service Unavailable", [("Content-Type", "application/json"), ("Content-Length", str(len(body)))])
                result = [body]
            else:
                result = host.app(environ, start)
            responses.append(response)
            save()
            return result

        server.set_app(app)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        thread.start()
        print(json.dumps({"type": "ready", "origin": origin, "operator_token": host.operator_token,
            "category": host.category, "credentials": {key: value.token for key, value in host.credentials.items()}}), flush=True)
        try:
            for line in sys.stdin:
                command = json.loads(line)
                name = command["command"]
                if name == "stop":
                    break
                if name == "scenario":
                    if command["name"] not in {"success", "window", "quality", "unknown", "unknown_fee"}:
                        raise ValueError("Unknown synthetic response scenario")
                    flags["scenario"] = command["name"]
                elif name == "hold":
                    flags["hold"] = True
                    held.clear()
                    released.clear()
                elif name == "release":
                    released.set()
                    flags["hold"] = False
                elif name == "advance":
                    seconds = command["seconds"]
                    if type(seconds) is not int or not 0 <= seconds <= 3600:
                        raise ValueError("Bounded fixture clock movement required")
                    clock.now += seconds
                elif name == "revoke_agent":
                    host.app.identity_store.revoke(host.credentials["agent"].id)
                elif name in {"fail_read", "restore_read"}:
                    flags["read_failure"] = name == "fail_read"
                elif name != "inspect":
                    raise ValueError("Unknown private fixture command")
                commands.append({key: value for key, value in command.items() if key in {"command", "name", "seconds"}})
                save()
                print(json.dumps({"type": "inspected", **receipt()}), flush=True)
        finally:
            released.set()
            server.shutdown()
            thread.join(2)
            save()
            for item in observation_transports:
                item.close()
            if boundary.blocked:
                raise AssertionError("Unexpected non-loopback socket attempt")


if __name__ == "__main__":
    main()

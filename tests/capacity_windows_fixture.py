"""Real host composition with synthetic window authority, never production proof."""
from contextlib import ExitStack
from datetime import datetime
import importlib.util
import json
import os
from pathlib import Path
import threading

import httpx
import pytest

from cloudyfan_jev.capacity_dispatch import CapacityController
from cloudyfan_jev.capacity_windows import CapacityWindows
from cloudyfan_monitor.capacity import CapacityObserver
from cloudyfan_monitor.integration import assemble_monitor
from capacity_admission_fixture import attach_admission
from capacity_sources_fixture import limit


def window_configuration(now):
    return {"schema_version": 1, "version": "synthetic-admission-config-v1",
        "bindings": [{"id": "synthetic-binding", "provider": "openrouter", "account_ref": "synthetic-shared-account",
            "credential_ref": "synthetic-upstream-key", "endpoint_ref": "openrouter-chat", "groups": ["shared"]}],
        "groups": [{"id": "shared", "scope": {"kind": "account", "ref": "synthetic-shared-account"},
            "source": {"kind": "controlled_import", "version": "synthetic-reviewed-v1"}, "supplier_limits": [],
            "local_limits": [limit("rpm", 100, now), limit("tpm", None, now, status="not_applicable"), limit("concurrency", 2, now)]}]}


def attach_windows(bundle, clock, *, rules=None, controlled_sources=None):
    original = attach_admission(bundle, clock, config=window_configuration(clock()))
    windows = CapacityWindows(original.store, version="synthetic-windows-v1", rules=rules or [
        {"binding": "synthetic-binding", "group": "shared", "header": "retry-after",
         "format": "retry_after", "constraint": "requests"}],
        **({"controlled_sources": controlled_sources} if controlled_sources is not None else {}))
    controller = CapacityController(original.store, credential_active=original.credential_active,
        window_backend=windows)
    controller.register("synthetic-window-admission-v1", configuration_version="synthetic-admission-config-v1",
        routes=original.routes["synthetic-admission-v1"], max_seconds=300, windows_version=windows.version)
    old = bundle.app
    bundle.app = assemble_monitor(old.store, old.gateway.service, old.gateway.registry, old.identity_store,
        operator_token=bundle.operator_token, source=old.source, origin=old.origin, demo=True,
        capacity_observer=CapacityObserver(original.store, sources={},
            **({"window_backend": windows} if controlled_sources is not None else {})), capacity_controller=controller)
    return controller, windows


@pytest.fixture
def backup_windows_http(tmp_path, loopback_network, request):
    """The UI's same real BackupCore composition at an actual loopback socket."""
    from test_local_http_integration import Clock, QuietHandler, ThreadedServer
    source = Path(__file__).resolve().parents[1] / "scripts" / "qa_capacity_windows_http.py"
    spec = importlib.util.spec_from_file_location("capacity_windows_qa_host", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with ExitStack() as stack:
        server = ThreadedServer(("127.0.0.1", 0), QuietHandler)
        stack.callback(server.server_close)
        loopback_network.ports.add(server.server_port)
        origin = f"http://127.0.0.1:{server.server_port}"
        clock = Clock(datetime.fromisoformat("2026-10-21T00:00:00+00:00").timestamp())
        fixture = module.build_window_host(tmp_path / "window-host", origin, clock)
        server.set_app(fixture.host.app)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        thread.start()
        stack.callback(thread.join, 2)
        stack.callback(server.shutdown)
        fixture.operator = stack.enter_context(httpx.Client(base_url=origin, trust_env=False, timeout=10,
            headers={"Authorization": "Bearer " + fixture.host.operator_token}))
        fixture.client = stack.enter_context(httpx.Client(base_url=origin, trust_env=False, timeout=10,
            headers={"Authorization": "Bearer " + fixture.host.credentials["studio"].token}))
        try:
            yield fixture
        finally:
            fixture.released.set()
            for transport in fixture.observation_transports:
                transport.close()
            destination = os.environ.get("CLOUDYFAN_MONITOR_EVIDENCE_DIR")
            if destination:
                output = Path(destination)
                output.mkdir(parents=True, exist_ok=True)
                (output / (request.node.name + ".json")).write_text(json.dumps({
                    "test": request.node.name, "scope": "real BackupCore/BudgetedCore/TaskService/HealthStore/window HTTP; synthetic upstream only",
                    "model_requests": fixture.requests, "side_requests": fixture.side_requests,
                    "budget": fixture.host.budget.report(), "windows": fixture.windows.snapshot(),
                    "socket_attempts": [list(item) for item in loopback_network.connected],
                    "blocked_socket_attempts": [str(item) for item in loopback_network.blocked]}, indent=2))
        assert not loopback_network.blocked and loopback_network.connected

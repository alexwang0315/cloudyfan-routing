"""Real host composition with synthetic window authority, never production proof."""
from cloudyfan_jev.capacity_dispatch import CapacityController
from cloudyfan_jev.capacity_windows import CapacityWindows
from cloudyfan_monitor.capacity import CapacityObserver
from cloudyfan_monitor.integration import assemble_monitor
from capacity_admission_fixture import attach_admission


def attach_windows(bundle, clock, *, rules=None):
    original = attach_admission(bundle, clock, rpm=100, concurrency=2)
    windows = CapacityWindows(original.store, version="synthetic-windows-v1", rules=rules or [
        {"binding": "synthetic-binding", "group": "shared", "header": "retry-after",
         "format": "retry_after", "constraint": "requests"}])
    controller = CapacityController(original.store, credential_active=original.credential_active,
        window_backend=windows)
    controller.register("synthetic-window-admission-v1", configuration_version="synthetic-admission-config-v1",
        routes=original.routes["synthetic-admission-v1"], max_seconds=300, windows_version=windows.version)
    old = bundle.app
    bundle.app = assemble_monitor(old.store, old.gateway.service, old.gateway.registry, old.identity_store,
        operator_token=bundle.operator_token, source=old.source, origin=old.origin, demo=True,
        capacity_observer=CapacityObserver(original.store, sources={}), capacity_controller=controller)
    return controller, windows

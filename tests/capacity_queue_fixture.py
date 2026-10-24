"""Synthetic host fixtures for the queue/window injection seam; never qualification."""
from pathlib import Path
import hashlib
import json

from cloudyfan_jev.capacity_dispatch import CapacityController
from cloudyfan_monitor.capacity import CapacityObserver
from cloudyfan_monitor.integration import assemble_monitor


class WindowProtocolFixture:
    """Exercise the public backend SPI, not real response-window semantics."""
    protocol = "capacity-windows-v1"
    version = "synthetic-window-protocol-v1"

    def __init__(self, clock, *, status="open"):
        self.clock, self.status, self.responses = clock, status, []

    def check(self, db, scope, physical_groups, now):
        return {"status": self.status, "not_before": now + 30 if self.status == "wait" else None,
            "evidence_refs": ["synthetic-saved-window"], "revision": 1,
            "reason_code": "capacity_window_" + self.status}

    def record_response(self, receipt_ref, scope):
        path = Path(receipt_ref["root"]) / "calls" / receipt_ref["call_id"] / "response.json"
        assert hashlib.sha256(path.read_bytes()).hexdigest() == receipt_ref["response_sha256"]
        response = json.loads(path.read_text())
        assert scope["observed_at"] == response["window_headers"]["observed_at"]
        self.responses.append({"receipt": receipt_ref, "scope": scope, "response": response})

    def snapshot(self):
        return {"configured": True, "protocol": self.protocol, "version": self.version, "status": self.status}


def attach_window_protocol(bundle, previous, backend):
    old = bundle.app
    controller = CapacityController(previous.store, credential_active=previous.credential_active,
        window_backend=backend)
    controller.register("synthetic-window-enabled-v2", configuration_version="synthetic-admission-config-v1",
        routes=previous.routes["synthetic-admission-v1"], max_seconds=300, windows_version=backend.version)
    bundle.app = assemble_monitor(old.store, old.gateway.service, old.gateway.registry, old.identity_store,
        operator_token=bundle.operator_token, source=old.source, origin=old.origin, demo=True,
        capacity_observer=CapacityObserver(previous.store, sources={}), capacity_controller=controller)
    return controller


def attach_queue(bundle, clock, *, limits=None, rpm=10, tpm=None, concurrency=1,
                 token_adapter=None, seconds=300):
    from cloudyfan_jev.capacity_queue import QueuePreparation
    from cloudyfan_jev.scheduler import QueueScheduler, QueueLimits
    from capacity_admission_fixture import attach_admission
    previous = attach_admission(bundle, clock, rpm=rpm, tpm=tpm, concurrency=concurrency,
        token_adapter=token_adapter, seconds=seconds)
    old = bundle.app
    preparation = QueuePreparation("synthetic-pure-queue-v1", contracts=old.gateway.service.contracts,
        tools={name: [] for name in old.gateway.service.contracts})
    previous.queue_backend = preparation
    previous.register("synthetic-queued-admission-v1", configuration_version="synthetic-admission-config-v1",
        routes=previous.routes["synthetic-admission-v1"], max_seconds=seconds, queue_version=preparation.version)
    queue = QueueScheduler(old.gateway.service, bundle.root / "queue.sqlite", clock=clock,
        limits=limits or QueueLimits(8, 2, 4, 120), measurement_kind="synthetic", capacity_protocol=preparation)
    bundle.app = assemble_monitor(old.store, old.gateway.service, old.gateway.registry, old.identity_store,
        operator_token=bundle.operator_token, source=old.source, origin=old.origin, demo=True,
        capacity_observer=old.capacity_observer, capacity_controller=previous, queue=queue)
    return queue

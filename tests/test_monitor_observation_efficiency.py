"""Operator reads verify each original result once and keep identity guards."""
import httpx

from cloudyfan_jev.identity import IdentityStore

from cloudyfan_monitor.demo import CATEGORY
from cloudyfan_monitor.integration import build_local_integration


def test_snapshot_verifies_each_completed_core_result_once(tmp_path, monkeypatch):
    fixture = build_local_integration(tmp_path / "monitor")
    app = fixture.app
    headers = {"Authorization": "Bearer " + fixture.credentials["studio"].token}
    with httpx.Client(transport=httpx.WSGITransport(app), base_url=app.origin) as http:
        response = http.post("/v1/tasks", headers=headers,
            json={"run_id": "one-read", "category": CATEGORY, "prompt": "Return only the integer sum of 1 and 1."})
        assert response.status_code == 200
        binding = app.store.binding("studio", "one-read")
        core = app.gateway.service.contracts[binding["contract"]].runner.core
        original_read, reads = core.read_completed, []
        def counted_read(run_id):
            reads.append(run_id)
            return original_read(run_id)
        monkeypatch.setattr(core, "read_completed", counted_read)
        snapshot = http.get("/monitor/snapshot", headers={"Authorization": "Bearer " + app.admin_token})
    assert snapshot.status_code == 200
    task = snapshot.json()["tasks"][0]
    assert task["status"] == "passed"
    assert task["trace"]["basis"] == "verified_core_result"
    assert len(reads) == 1


def test_identical_host_and_registry_checks_run_once_per_service_authorization(tmp_path, monkeypatch):
    original, checks = IdentityStore.is_active, []
    def counted_active(identity, principal):
        checks.append(principal)
        return original(identity, principal)
    monkeypatch.setattr(IdentityStore, "is_active", counted_active)
    fixture = build_local_integration(tmp_path / "monitor")
    checks.clear()
    fixture.app.gateway.service.authorize("studio")
    assert checks == ["studio"]
    fixture.identity.set_active("studio", False)
    with httpx.Client(transport=httpx.WSGITransport(fixture.app), base_url=fixture.app.origin) as http:
        denied = http.get("/v1/monitor", headers={"Authorization": "Bearer " + fixture.credentials["studio"].token})
    assert denied.status_code == 401

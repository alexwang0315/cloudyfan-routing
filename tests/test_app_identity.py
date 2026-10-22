import time

import httpx
from cloudyfan_jev.identity import IdentityStore

from cloudyfan_monitor.app import App
from cloudyfan_monitor.demo import ADMIN_TOKEN, CATEGORY, build_demo


def identity_app(tmp_path):
    legacy, upstream = build_demo(tmp_path / "service", seed=False)
    identity = IdentityStore(tmp_path / "service" / "identities.sqlite",
                             pepper=b"synthetic-integration-pepper-only-000000")
    for principal in ("studio", "agent"):
        identity.create_principal(principal)
    app = App(legacy.gateway, admin_token=ADMIN_TOKEN, identity_store=identity)
    credential = identity.issue("studio", scopes={"tasks:read", "tasks:write"},
                                expires_at=time.time() + 86400)
    return app, upstream, identity, credential


def test_rotated_identity_keeps_task_and_revokes_old_access(tmp_path):
    app, upstream, identity, credential = identity_app(tmp_path)
    client = httpx.Client(transport=httpx.WSGITransport(app=app), base_url=app.origin,
                          headers={"Authorization": "Bearer " + credential.token})
    task = client.post("/v1/tasks", json={"run_id": "identity-one", "category": CATEGORY,
                                         "prompt": "Return only the integer sum of 1 and 1."})
    assert task.status_code == 200
    calls = len(upstream.calls)
    replacement = identity.rotate(credential.id, expires_at=time.time() + 86400)
    assert client.get("/v1/tasks/identity-one/result").status_code == 401
    client.headers["Authorization"] = "Bearer " + replacement.token
    result = client.get("/v1/tasks/identity-one/result")
    assert result.status_code == 200
    assert result.json()["answer"] == "2"
    assert len(upstream.calls) == calls
    assert client.post("/monitor/mode", json={"revision": 0, "mode": "fixed_high"}).status_code == 401


def test_identity_scope_and_transport_are_enforced(tmp_path):
    app, upstream, identity, credential = identity_app(tmp_path)
    write_only = identity.issue("studio", scopes={"tasks:write"}, expires_at=time.time() + 86400)
    client = httpx.Client(transport=httpx.WSGITransport(app=app), base_url=app.origin,
                          headers={"Authorization": "Bearer " + write_only.token})
    assert client.get("/v1/monitor").status_code == 401
    remote = httpx.Client(transport=httpx.WSGITransport(app=app, remote_addr="198.51.100.8"),
                          base_url=app.origin, headers={"Authorization": "Bearer " + credential.token})
    assert remote.get("/v1/monitor").status_code == 401
    identity.set_active("studio", False)
    client.headers["Authorization"] = "Bearer " + credential.token
    assert client.get("/v1/monitor").status_code == 401
    assert not upstream.calls

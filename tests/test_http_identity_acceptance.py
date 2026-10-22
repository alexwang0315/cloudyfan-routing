"""Stable client identity at the approved socket HTTP seam; synthetic upstream only."""
import json
import os
from pathlib import Path

import pytest

from cloudyfan_monitor.demo import CATEGORY
from test_local_http_integration import local_service as local_service
from test_local_http_integration import loopback_network as loopback_network


TASK = {"run_id": "identity-rotation", "category": CATEGORY,
        "prompt": "Return only the integer sum of 1 and 1."}


def public_projection(value):
    """Keep public counters in evidence, never prompts, answers, or credentials."""
    if "summary" in value:
        pending = [value]
        while pending:
            part = pending.pop()
            if isinstance(part, dict):
                assert not {"prompt", "answer", "token", "authorization", "api_key", "secret"}.intersection(part)
                pending.extend(part.values())
            elif isinstance(part, list):
                pending.extend(part)
        return {
            "summary": value["summary"],
            "clients": [{"id": row["id"], "stats": row["stats"]} for row in value["clients"]],
            "tasks": [{key: row.get(key) for key in
                       ("client", "run", "status", "observation", "known_api_usd", "final_pass")}
                      for row in value["tasks"]],
            "ledger": {key: value.get("ledger", {}).get(key) for key in
                       ("known_usd", "by_client_known_usd", "unattributed_known_usd", "unknown_events")},
        }
    return {key: value[key] for key in ("run_id", "status", "costs", "error") if key in value}


@pytest.fixture
def identity_http(local_service, request):
    responses = []

    def capture(response):
        response.read()
        responses.append({"method": response.request.method, "path": response.request.url.path,
                          "status": response.status_code, "body": public_projection(response.json())})

    def client(principal="studio", credential=None):
        connection = local_service.client(principal)
        if credential is not None:
            connection.headers["Authorization"] = "Bearer " + credential.token
        connection.event_hooks["response"].append(capture)
        return connection

    yield local_service, client
    destination = os.environ.get("CLOUDYFAN_MONITOR_EVIDENCE_DIR")
    if destination:
        path = Path(destination) / (request.node.name + "-identity.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"schema_version": 1, "test": request.node.name,
            "scope": "actual loopback HTTP; synthetic identity registry; MockTransport model upstream",
            "responses": responses, "mock_upstream_call_count": len(local_service.bundle.upstream.calls)},
            ensure_ascii=False, indent=2))


def test_http_rotated_credential_keeps_principal_task_statistics_and_cost(identity_http):
    service, connect = identity_http
    bundle = service.bundle
    bundle.upstream.fail_tiers = set()
    original = connect()
    operator = connect("operator")
    created = original.post("/v1/tasks", json=TASK)
    assert created.status_code == 200 and created.json()["status"] == "passed"
    before = operator.get("/monitor/snapshot").json()
    assert before["summary"]["tasks"] == before["summary"]["passed"] == 1
    assert before["ledger"]["by_client_known_usd"]["studio"] == "0.0001"
    assert len(bundle.upstream.calls) == 1

    replacement = bundle.identity.rotate(bundle.credentials["studio"].id,
                                        expires_at=service.clock.now + 86400)
    assert original.get("/v1/tasks/identity-rotation/result").status_code == 401
    assert original.post("/v1/tasks", json=TASK).status_code == 401
    current = connect(credential=replacement)
    result = current.get("/v1/tasks/identity-rotation/result")
    assert result.status_code == 200 and result.json()["answer"] == "2"
    status = current.get("/v1/tasks/identity-rotation")
    assert status.status_code == 200 and status.json()["status"] == "passed"
    assert current.post("/v1/tasks", json=TASK).json()["status"] == "passed"
    own = current.get("/v1/monitor").json()
    assert [row["id"] for row in own["clients"]] == ["studio"]
    assert [(row["client"], row["run"]) for row in own["tasks"]] == [("studio", "identity-rotation")]
    after = operator.get("/monitor/snapshot").json()
    assert after["summary"] == before["summary"]
    assert after["ledger"]["known_usd"] == before["ledger"]["known_usd"] == "0.0001"
    assert after["ledger"]["by_client_known_usd"] == before["ledger"]["by_client_known_usd"]
    assert next(row for row in after["clients"] if row["id"] == "studio")["stats"] == next(
        row for row in before["clients"] if row["id"] == "studio")["stats"]
    assert own["summary"]["tasks"] == own["summary"]["passed"] == 1
    assert own["summary"]["known_task_api_usd"] == "0.0001"
    assert len(bundle.upstream.calls) == 1


def test_http_clients_and_scopes_cannot_read_resume_or_manage_each_other(identity_http):
    service, connect = identity_http
    bundle = service.bundle
    bundle.upstream.fail_tiers = set()
    studio, agent, operator = connect(), connect("agent"), connect("operator")
    tasks = {"studio": {**TASK, "run_id": "studio-private"},
             "agent": {**TASK, "run_id": "agent-private"}}
    for principal, client in (("studio", studio), ("agent", agent)):
        task = tasks[principal]
        created = client.post("/v1/tasks", json=task)
        assert created.status_code == 200 and created.json()["status"] == "passed"
        status = client.get("/v1/tasks/" + task["run_id"])
        assert status.status_code == 200 and status.json()["status"] == "passed"
        result = client.get("/v1/tasks/" + task["run_id"] + "/result")
        assert result.status_code == 200 and result.json()["answer"] == "2"
        own = client.get("/v1/monitor").json()
        assert [row["id"] for row in own["clients"]] == [principal]
        assert [(row["client"], row["run"]) for row in own["tasks"]] == [(principal, task["run_id"])]

    for client, foreign in ((studio, "agent-private"), (agent, "studio-private")):
        for suffix in ("", "/result"):
            denied = client.get("/v1/tasks/" + foreign + suffix)
            assert denied.status_code == 404 and denied.json()["error"]["code"] == "task_not_found"
        denied = client.post("/v1/tasks/" + foreign + "/resume", json={})
        assert denied.status_code == 404 and denied.json()["error"]["code"] == "task_not_found"
        assert client.get("/monitor/snapshot").status_code == 401
        assert client.post("/monitor/mode", json={"revision": 0, "mode": "fixed_high"}).status_code == 401

    read_only = bundle.identity.issue("studio", scopes={"tasks:read"},
                                      expires_at=service.clock.now + 86400)
    reader = connect(credential=read_only)
    assert reader.get("/v1/tasks/studio-private/result").status_code == 200
    assert reader.post("/v1/tasks", json={**TASK, "run_id": "reader-cannot-create"}).status_code == 401
    assert reader.post("/v1/tasks/studio-private/resume", json={}).status_code == 401
    write_only = bundle.identity.issue("studio", scopes={"tasks:write"},
                                       expires_at=service.clock.now + 86400)
    writer = connect(credential=write_only)
    for path in ("/v1/monitor", "/v1/tasks/studio-private", "/v1/tasks/studio-private/result"):
        assert writer.get(path).status_code == 401

    view = operator.get("/monitor/snapshot").json()
    assert view["summary"]["tasks"] == view["summary"]["passed"] == 2
    assert view["ledger"]["by_client_known_usd"] == {"studio": "0.0001", "agent": "0.0001", "batch": "0"}
    assert len(bundle.upstream.calls) == 2


@pytest.mark.parametrize("original_evidence", ["result.json", "response.json"])
def test_http_disabled_client_keeps_operator_history_and_revalidates_originals(identity_http, original_evidence):
    service, connect = identity_http
    bundle = service.bundle
    bundle.upstream.fail_tiers = set()
    client, operator = connect(), connect("operator")
    task = {**TASK, "run_id": "identity-suspension"}
    created = client.post("/v1/tasks", json=task)
    assert created.status_code == 200 and created.json()["status"] == "passed"
    before = operator.get("/monitor/snapshot").json()
    assert before["summary"]["passed"] == 1
    assert before["ledger"]["by_client_known_usd"]["studio"] == "0.0001"

    bundle.identity.set_active("studio", False)
    assert client.get("/v1/tasks/identity-suspension/result").status_code == 401
    assert client.post("/v1/tasks/identity-suspension/resume", json={}).status_code == 401
    disabled = operator.get("/monitor/snapshot").json()
    assert disabled["summary"] == before["summary"]
    assert disabled["tasks"][0]["observation"] == "client_inactive"
    assert disabled["ledger"]["by_client_known_usd"] == before["ledger"]["by_client_known_usd"]
    assert disabled["ledger"]["unattributed_known_usd"] == "0"

    bundle.identity.set_active("studio", True)
    readable = client.get("/v1/tasks/identity-suspension/result")
    assert readable.status_code == 200 and readable.json()["answer"] == "2"
    restored = operator.get("/monitor/snapshot").json()
    assert restored["summary"] == before["summary"]
    assert restored["tasks"][0]["observation"] is None

    # Fault injection touches only this test's synthetic original evidence.
    # Behavior is observed through HTTP, never by querying an internal database.
    bundle.identity.set_active("studio", False)
    originals = list((bundle.root / "receipts").rglob(original_evidence))
    assert len(originals) == 1
    originals[0].write_text('{"synthetic_corruption":true}')
    still_disabled = operator.get("/monitor/snapshot").json()
    assert still_disabled["summary"]["passed"] == 1
    assert still_disabled["tasks"][0]["observation"] == "client_inactive"
    bundle.identity.set_active("studio", True)
    unavailable = client.get("/v1/tasks/identity-suspension/result")
    assert unavailable.status_code == 503
    revalidated = operator.get("/monitor/snapshot").json()
    assert revalidated["summary"]["passed"] == 0
    assert revalidated["summary"]["unknown"] == 1
    assert revalidated["tasks"][0]["status"] == "engineering_interrupted"
    assert revalidated["tasks"][0]["observation"] == "evidence_unavailable"
    assert revalidated["ledger"]["known_usd"] == "0.0001"
    assert revalidated["ledger"]["by_client_known_usd"] == before["ledger"]["by_client_known_usd"]
    assert len(bundle.upstream.calls) == 1

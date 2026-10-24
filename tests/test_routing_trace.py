import json

import httpx
import pytest

from cloudyfan_monitor.demo import ADMIN_TOKEN, CATEGORY, TOKENS, build_demo
from cloudyfan_monitor.store import MonitorError


def test_gateway_rejects_a_changed_ollama_contract_before_binding_or_dispatch(tmp_path):
    app, upstream = build_demo(tmp_path, seed=False)
    app.gateway.switch(app.store.control()["revision"], "fixed_high", "studio")
    with pytest.raises(MonitorError) as caught:
        app.gateway.create("studio", {"run_id": "changed-alias", "category": CATEGORY,
            "prompt": "Return only 2."}, expected_contract="demo-cloudyfan")
    assert caught.value.code == "ollama_contract_changed"
    assert caught.value.status == 409
    with httpx.Client(transport=httpx.WSGITransport(app=app), base_url=app.origin,
                      headers={"Authorization": "Bearer " + TOKENS["studio"]}) as customer:
        assert customer.get("/v1/monitor").json()["tasks"] == []
        assert customer.get("/v1/billing").json()["entries"] == []
    assert upstream.calls == []


def test_operator_sees_original_mrc_attempts_and_jev_stage_evidence(tmp_path):
    app, upstream = build_demo(tmp_path, seed=False)
    prompt = "Return only 2. Private request marker that must not enter the monitor."
    with httpx.Client(transport=httpx.WSGITransport(app=app), base_url=app.origin,
                      headers={"Authorization": "Bearer " + ADMIN_TOKEN}) as operator:
        created = operator.post("/v1/tasks", json={"run_id": "route-detail", "category": CATEGORY,
            "prompt": prompt}, headers={"Authorization": "Bearer " + TOKENS["studio"]})
        assert created.status_code == 200
        snapshot = operator.get("/monitor/snapshot").json()
        task = next(task for task in snapshot["tasks"] if task["run"] == "route-detail")
        trace = task["trace"]
        assert [attempt["tier"] for attempt in trace["attempts"]] == ["open_source", "mid", "high"]
        assert [attempt["passed"] for attempt in trace["attempts"]] == [False, False, True]
        assert [attempt["index"] for attempt in trace["attempts"]] == [1, 2, 3]
        assert trace["attempts"][0]["calls"][0]["model"] == "example/open_source"
        assert trace["attempts"][0]["calls"][0]["input_tokens"] == 20
        assert trace["attempts"][2]["calls"][0]["cost_usd"] == "0.004"
        assert [decision["stage"] for decision in trace["decisions"]] == ["low_mid", "mid_high"]
        assert [decision["previous_tier"] for decision in trace["decisions"]] == ["open_source", "mid"]
        assert [decision["selected_tier"] for decision in trace["decisions"]] == ["mid", "high"]
        assert all(decision["trigger"] == "public_validation_failed" for decision in trace["decisions"])
        assert all(decision["cost_usd"] == "0.0001" for decision in trace["decisions"])
        assert all(decision["fallback"] is False for decision in trace["decisions"])
        assert trace["decisions"][0]["probabilities"] == {"low": "0.2", "mid": "0.8"}
        assert prompt not in json.dumps(snapshot)
        assert "previous_answer" not in json.dumps(trace)
        assert len(upstream.calls) == 5
        operator.get("/monitor/snapshot").raise_for_status()
        assert len(upstream.calls) == 5

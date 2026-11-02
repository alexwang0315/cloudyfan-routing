from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from cloudyfan_monitor.demo import ADMIN_TOKEN, CATEGORY, SCENARIO, TOKENS, Upstream, build_demo
from cloudyfan_monitor.integration import build_local_integration
from cloudyfan_monitor.playground import SCENARIOS, Playground

QUESTION = "請用一句話說明什麼是模型路由"


def _operator(app):
    return httpx.Client(transport=httpx.WSGITransport(app), base_url=app.origin,
                        headers={"Authorization": "Bearer " + ADMIN_TOKEN})


@pytest.mark.parametrize("scenario,tiers,picks,status", [
    ("first_pass", ["open_source"], [], "passed"),
    ("low_pass", ["open_source", "low"], ["low"], "passed"),
    ("mid_pass", ["open_source", "mid"], ["mid"], "passed"),
    ("high_pass", ["open_source", "mid", "high"], ["mid", "high"], "passed"),
    ("all_fail", ["open_source", "mid", "high"], ["mid", "high"], "failed"),
])
def test_scenarios_drive_the_real_flow_and_return_a_trace(tmp_path, scenario, tiers, picks, status):
    app, upstream = build_demo(tmp_path / "demo", seed=False)
    with _operator(app) as client:
        response = client.post("/monitor/playground/ask", json={"prompt": QUESTION, "scenario": scenario})
    assert response.status_code == 200
    body = response.json()
    assert body["demo"] is True and "模擬上游" in body["notice"]
    assert body["status"] == status and body["task"]["client"] == "studio"
    assert [step["tier"] for step in body["task"]["path"]] == tiers
    assert [d["selected_tier"] for d in body["task"]["trace"]["decisions"]] == picks
    assert body["costs"]["complete_api_usd"] is not None
    chats = [call for call in upstream.calls if "messages" in call]
    assert len(chats) == len(tiers) and QUESTION in str(chats[0]["messages"])


def test_each_ask_is_a_new_task_visible_to_the_operator(tmp_path):
    app, upstream = build_demo(tmp_path / "demo", seed=False)
    with _operator(app) as client:
        runs = {client.post("/monitor/playground/ask", json={"prompt": QUESTION, "scenario": "first_pass"}).json()["run_id"]
                for _ in range(2)}
        tasks = client.get("/monitor/snapshot").json()["tasks"]
    assert len(runs) == 2 and runs <= {task["run"] for task in tasks}
    assert upstream.fail_tiers == {"open_source", "mid"}


def test_scenario_does_not_leak_to_other_threads_or_later_tasks(tmp_path):
    upstream = Upstream()
    token = SCENARIO.set(SCENARIOS["first_pass"])
    try:
        with ThreadPoolExecutor(1) as other:
            elsewhere = other.submit(lambda: upstream.tier_fails("open_source")).result()
        assert upstream.tier_fails("open_source") is False
    finally:
        SCENARIO.reset(token)
    assert elsewhere is True and upstream.tier_fails("open_source") is True


def test_descriptor_lists_scenarios_and_marks_simulation(tmp_path):
    fixture = build_local_integration(tmp_path / "integration")
    with _operator(fixture.app) as client:
        body = client.get("/monitor/playground").json()
    assert body["demo"] is True and body["category"] == CATEGORY and body["effective_mode"] == "cloudyfan"
    assert [item["id"] for item in body["scenarios"]] == list(SCENARIOS)


def test_identity_store_assembly_runs_a_scenario(tmp_path):
    fixture = build_local_integration(tmp_path / "integration")
    with _operator(fixture.app) as client:
        body = client.post("/monitor/playground/ask", json={"prompt": QUESTION, "scenario": "mid_pass"}).json()
    assert body["status"] == "passed" and [s["tier"] for s in body["task"]["path"]] == ["open_source", "mid"]


@pytest.mark.parametrize("payload,code", [
    ({"prompt": "  ", "scenario": "first_pass"}, "invalid_text_prompt"),
    ({"prompt": "x" * 32769, "scenario": "first_pass"}, "invalid_text_prompt"),
    ({"prompt": QUESTION, "scenario": "nope"}, "playground_scenario_not_supported"),
])
def test_rejects_bad_input_without_any_model_call(tmp_path, payload, code):
    app, upstream = build_demo(tmp_path / "demo", seed=False)
    with _operator(app) as client:
        response = client.post("/monitor/playground/ask", json=payload)
    assert response.status_code == 422 and response.json()["error"]["code"] == code
    assert upstream.calls == []


def test_extra_fields_cannot_choose_mode_or_model(tmp_path):
    app, upstream = build_demo(tmp_path / "demo", seed=False)
    with _operator(app) as client:
        response = client.post("/monitor/playground/ask",
                               json={"prompt": QUESTION, "scenario": "first_pass", "model": "real/model"})
    assert response.status_code >= 400 and upstream.calls == []


def test_requires_operator_token(tmp_path):
    app, upstream = build_demo(tmp_path / "demo", seed=False)
    for token in ("", TOKENS["studio"]):
        with httpx.Client(transport=httpx.WSGITransport(app), base_url=app.origin,
                          headers={"Authorization": "Bearer " + token}) as client:
            assert client.post("/monitor/playground/ask",
                               json={"prompt": QUESTION, "scenario": "first_pass"}).status_code == 401
    assert upstream.calls == []


def test_not_available_without_a_synthetic_playground(tmp_path):
    app, _ = build_demo(tmp_path / "demo", seed=False)
    app.playground = None
    with _operator(app) as client:
        assert client.get("/monitor/playground").json()["error"]["code"] == "playground_not_available"
        response = client.post("/monitor/playground/ask", json={"prompt": QUESTION, "scenario": "first_pass"})
    assert response.status_code == 409


def test_playground_refuses_non_synthetic_gateway(tmp_path):
    app, _ = build_demo(tmp_path / "demo", seed=False)
    app.gateway.demo = False
    with pytest.raises(ValueError):
        Playground(app.gateway)

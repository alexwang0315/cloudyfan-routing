import httpx
import pytest
import threading

from cloudyfan_monitor.simulation import CUSTOMER, SimulationController, build_simulation
from cloudyfan_monitor.customer_simulation import run_customer50


def test_customer_task_is_validated_from_its_frozen_public_question(tmp_path):
    fixture = build_simulation(tmp_path / "simulation")
    with httpx.Client(transport=httpx.WSGITransport(fixture.app), base_url=fixture.app.origin,
                      headers={"Authorization": "Bearer " + fixture.credentials[CUSTOMER].token}) as customer:
        # Different operands from the canned fifty: validator must read this
        # frozen request, rather than a mock-answer or scenario lookup table.
        request = {"run_id": "public-question", "category": "artificial-analysis:coding_index",
                   "prompt": "Simulation question 01. Return only the integer sum of 123 and -11."}
        assert customer.post("/v1/tasks", json=request).json()["status"] == "passed"
        result = customer.get("/v1/tasks/public-question/result").json()
        assert result["answer"] == "112"
        assert result["final_public_pass"] is True
        calls = len(fixture.upstream.calls)
        assert calls == 1
        assert customer.post("/v1/tasks", json=request).json()["status"] == "passed"
        assert customer.get("/v1/monitor").json()["summary"]["passed"] == 1
        assert len(fixture.upstream.calls) == calls


@pytest.mark.parametrize("number,prompt,status,answer,path", [
    (11, "Simulation question 11. Return only the integer sum of 177 and 167.",
     "passed", "344", ["open_source", "low"]),
    (21, "Simulation question 21. Return only the integer sum of 247 and 137.",
     "passed", "384", ["open_source", "mid"]),
    (31, "Simulation question 31. Return only the integer sum of 317 and 107.",
     "passed", "424", ["open_source", "mid", "high"]),
    (41, "Simulation question 41. Return only the integer sum of 387 and 77.",
     "failed", "465", ["open_source", "low", "high"]),
    (46, "Simulation question 46. Return only the integer sum of 422 and 62. "
         "Public verification is unavailable for this request.",
     "unverifiable", "484", ["open_source", "mid", "high"]),
])
def test_public_validation_controls_sequential_pool_repairs(tmp_path, number, prompt, status, answer, path):
    fixture = build_simulation(tmp_path / "simulation")
    with httpx.Client(transport=httpx.WSGITransport(fixture.app), base_url=fixture.app.origin,
                      headers={"Authorization": "Bearer " + fixture.credentials[CUSTOMER].token}) as customer:
        run_id = f"sequential-q{number}"
        posted = customer.post("/v1/tasks", json={"run_id": run_id,
            "category": "artificial-analysis:coding_index", "prompt": prompt})
        assert posted.status_code == 200
        assert posted.json()["status"] == status
        result = customer.get(f"/v1/tasks/{run_id}/result").json()
        assert result["answer"] == answer
        task = customer.get("/v1/monitor").json()["tasks"][0]
        assert [attempt["tier"] for attempt in task["path"]] == path
        assert task["classifier_calls"] == len(path) - 1
        assert task["unknown_events"] == 0


def test_official_ollama_customer_uses_http_and_replays_the_same_run_without_dispatch(tmp_path):
    fixture = build_simulation(tmp_path / "simulation")
    transport = httpx.WSGITransport(fixture.app)
    first = run_customer50(fixture.app.origin, fixture.credentials[CUSTOMER].token,
                          "sdk-test", question_numbers=(1, 41, 46), delay=0, transport=transport)
    assert first["outcomes"] == {"passed": 1, "failed": 1, "unverifiable": 1}
    assert first["completed"] == 3
    assert [record["ollama_http_status"] for record in first["questions"]] == [200, 422, 422]
    assert [record["answer"] for record in first["questions"]] == ["304", "465", "484"]
    before = len(fixture.upstream.calls)
    replay = run_customer50(fixture.app.origin, fixture.credentials[CUSTOMER].token,
                           "sdk-test", question_numbers=(1, 41, 46), delay=0, transport=transport)
    assert replay["outcomes"] == first["outcomes"]
    assert len(fixture.upstream.calls) == before


def test_simulation_controls_are_visible_without_asking_questions_or_debiting(tmp_path):
    fixture = build_simulation(tmp_path / "simulation")
    with httpx.Client(transport=httpx.WSGITransport(fixture.app), base_url=fixture.app.origin,
                      headers={"Authorization": "Bearer " + fixture.operator_token}) as operator:
        descriptor = operator.get("/monitor/snapshot").json()["simulation"]
        assert descriptor["status"] == "idle"
        assert descriptor["completed"] == 0
        assert descriptor["total"] == 50
        assert descriptor["customer"] == "ollama-customer"
        assert descriptor["alias"] == "cloudyfan/mrc:simulation"
        assert descriptor["can_start"] is True
        assert descriptor["model_upstream"] == "synthetic_mock_transport"
        snapshot = operator.get("/monitor/snapshot").json()
        assert snapshot["summary"]["tasks"] == 0
        assert fixture.credentials[CUSTOMER].token not in str(descriptor)
        assert fixture.upstream.calls == []
        assert [e["kind"] for e in operator.get("/monitor/billing").json()["entries"]] == ["credit"]


def test_zero_supplier_cost_open_source_answer_still_has_a_customer_charge(tmp_path):
    fixture = build_simulation(tmp_path / "simulation")
    with httpx.Client(transport=httpx.WSGITransport(fixture.app), base_url=fixture.app.origin,
                      headers={"Authorization": "Bearer " + fixture.operator_token}) as operator:
        response = operator.post("/v1/tasks", headers={"Authorization": "Bearer " + fixture.credentials[CUSTOMER].token},
            json={"run_id": "zero-cost-q01", "category": "artificial-analysis:coding_index",
                  "prompt": "Simulation question 01. Return only the integer sum of 107 and 197."})
        assert response.json()["status"] == "passed"
        assert operator.post("/monitor/billing/reconcile", json={}).json()["charged"] == 1
        assert operator.post("/monitor/billing/reconcile", json={}).json()["charged"] == 0
        billing = operator.get("/monitor/billing").json()
        debit = next(entry for entry in billing["entries"] if entry["kind"] == "debit")
        assert debit["supplier_cost_usd"] == "0"
        assert debit["amount_usd"] == "0.00001056"
        assert debit["demo"] is True
        assert billing["clients"][0]["balance_usd"] == "0.99998944"


def test_explicit_start_is_nonblocking_and_rejects_a_second_customer_while_running(tmp_path):
    fixture = build_simulation(tmp_path / "simulation")
    entered, release = threading.Event(), threading.Event()

    def blocked_customer_http(request):
        if request.method == "GET" and request.url.path == "/v1/monitor":
            return httpx.Response(200, json={"demo": True})
        if request.method == "GET" and request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "cloudyfan/mrc:simulation"}]})
        assert request.url.path == "/api/chat"
        entered.set()
        assert release.wait(5)
        return httpx.Response(503, json={"error": "controlled_customer_endpoint_unavailable"})

    fixture.controller = SimulationController(fixture, customer_transport=httpx.MockTransport(blocked_customer_http),
                                              delay=0)
    fixture.app.simulation = fixture.controller
    with httpx.Client(transport=httpx.WSGITransport(fixture.app), base_url=fixture.app.origin,
                      headers={"Authorization": "Bearer " + fixture.operator_token}) as operator:
        started = operator.post("/monitor/simulation/start", json={})
        assert started.status_code == 200
        assert started.json()["status"] == "running"
        try:
            assert entered.wait(5)
            assert operator.post("/monitor/simulation/start", json={}).status_code == 409
            assert operator.get("/monitor/snapshot").json()["simulation"]["completed"] == 0
            assert fixture.upstream.calls == []
        finally:
            release.set()
        terminal = fixture.controller.wait(5)
        assert terminal["status"] == "failed"
        assert terminal["completed"] == 0
        assert terminal["can_start"] is True


def test_simulation_customer_refuses_a_service_that_is_not_explicitly_synthetic():
    observed = []

    def service(request):
        observed.append((request.method, request.url.path))
        return httpx.Response(200, json={"demo": False})

    with pytest.raises(RuntimeError, match="synthetic_service_required"):
        run_customer50("http://127.0.0.1:18776", "mock-customer-credential-not-a-secret", "wrong-target",
                       question_numbers=(1,), delay=0, transport=httpx.MockTransport(service))
    assert observed == [("GET", "/v1/monitor")]


def test_fixture_authentication_failure_keeps_safe_diagnostics_and_never_dispatches(tmp_path):
    class Clock:
        now = 1792740000.0

        def __call__(self):
            return self.now

    clock = Clock()
    fixture = build_simulation(tmp_path / "simulation", clock=clock)
    with httpx.Client(transport=httpx.WSGITransport(fixture.app), base_url=fixture.app.origin,
                      headers={"Authorization": "Bearer " + fixture.credentials[CUSTOMER].token}) as customer:
        assert customer.get("/api/tags").status_code == 200
        # Public clock seam deliberately makes identity storage unavailable;
        # this is diagnostic coverage, not a claimed cause of the live Q23.
        clock.now -= 1
        denied = customer.get("/api/tags")
        assert denied.status_code == 503
        assert fixture.upstream.calls == []
        observed = customer.get("/monitor/snapshot", headers={
            "Authorization": "Bearer " + fixture.operator_token}).json()
        diagnostics = observed["simulation"]["identity_diagnostics"]
        assert diagnostics["failures"] == 1
        assert diagnostics["last"]["exception_class"] == "IdentityError"
        assert diagnostics["last"]["code"] == "identity_control_unavailable"
        assert "Identity clock moved backwards" not in str(diagnostics)
        assert fixture.credentials[CUSTOMER].token not in str(diagnostics)

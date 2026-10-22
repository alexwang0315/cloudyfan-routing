"""Approved Monitor behavior through Gateway and real TaskService/Core boundaries."""
from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from pathlib import Path
import threading
import time

import httpx
import pytest

from cloudyfan_jev.demo import ArithmeticEnvironment
from cloudyfan_jev.http_api import ServiceError, TaskService
from cloudyfan_jev.retention import RetentionPolicy

from cloudyfan_monitor.demo import build_demo, CATEGORY
from cloudyfan_monitor.gateway import Gateway
from cloudyfan_monitor.integration import build_local_integration
from cloudyfan_monitor.store import MonitorError, Store


TASK = {"run_id": "backend-example", "category": CATEGORY,
        "prompt": "Return only the integer sum of 1 and 1."}


def test_resubmitting_interrupted_task_only_reads_until_explicit_resume(tmp_path):
    app, upstream = build_demo(tmp_path, seed=False)
    upstream.fail_tiers = set()

    class InterruptedValidation(ArithmeticEnvironment):
        interrupted = False

        def validate(self, text):
            if not type(self).interrupted:
                type(self).interrupted = True
                raise RuntimeError("Synthetic validation interruption")
            return super().validate(text)

    contracts = app.gateway.service.contracts
    contracts["demo-cloudyfan"] = replace(
        contracts["demo-cloudyfan"], environment_factory=lambda workspace: InterruptedValidation())
    assert app.gateway.create("studio", TASK)["status"] == "engineering_interrupted"
    calls = len(upstream.calls)
    assert app.gateway.create("studio", TASK)["status"] == "engineering_interrupted"
    assert len(upstream.calls) == calls
    assert app.gateway.execute("studio", TASK["run_id"])["status"] == "passed"
    assert len(upstream.calls) == calls


def test_expiry_preserves_historical_public_statistics_and_cost(tmp_path):
    app, upstream = build_demo(tmp_path, seed=False)
    now = [time.time() + 1]
    store = Store(app.store.path, clock=lambda: now[0])
    service = TaskService(app.gateway.service.root, contracts=app.gateway.service.contracts,
        allowed_principals=app.gateway.service.allowed_principals,
        retention_policy=RetentionPolicy("monitor-access-v1", 30 * 86400), clock=lambda: now[0])
    gateway = Gateway(store, service, app.gateway.registry, demo=True)
    assert gateway.create("studio", TASK)["status"] == "passed"
    before = gateway.snapshot()
    assert before["summary"]["passed"] == 1
    known = before["summary"]["known_api_usd"]
    now[0] += 30 * 86400
    after = gateway.snapshot()
    assert after["tasks"][0]["status"] == "expired"
    assert after["summary"]["passed"] == 1
    assert after["summary"]["completed"] == 1
    assert after["summary"]["known_api_usd"] == known
    with pytest.raises(ServiceError) as expired:
        gateway.create("studio", TASK)
    assert expired.value.status == 410


def test_only_one_gateway_task_runs_and_new_busy_task_is_not_queued(tmp_path):
    app, upstream = build_demo(tmp_path, seed=False)
    entered, release = threading.Event(), threading.Event()

    def endpoint(request):
        entered.set()
        if not release.wait(5):
            raise AssertionError("Synthetic upstream release timed out")
        return upstream(request)

    runner = app.gateway.service.contracts["demo-cloudyfan"].runner
    runner.client.http = httpx.Client(transport=httpx.MockTransport(endpoint))
    second = Gateway(Store(app.store.path), app.gateway.service, app.gateway.registry, demo=True)
    with ThreadPoolExecutor(2) as workers:
        first = workers.submit(app.gateway.create, "studio", TASK)
        assert entered.wait(2)
        rejected = workers.submit(second.create, "agent", {**TASK, "run_id": "busy-new"})
        try:
            with pytest.raises(MonitorError) as caught:
                rejected.result(timeout=.5)
            assert caught.value.status == 429
            assert caught.value.code == "service_busy"
        except TimeoutError:
            pytest.fail("New task waited instead of returning immediate capacity error")
        finally:
            release.set()
        assert first.result(timeout=5)["status"] == "passed"
    calls = len(upstream.calls)
    assert second.snapshot("agent")["tasks"] == []
    assert len(upstream.calls) == calls


def test_same_id_request_racing_with_admission_reads_the_accepted_task(tmp_path):
    app, upstream = build_demo(tmp_path, seed=False)
    stale_read, entered, release = threading.Event(), threading.Event(), threading.Event()

    class ScheduledReadStore(Store):
        def existing_binding(self, client, run):
            result = super().existing_binding(client, run)
            if result is None:
                stale_read.set()
                if not entered.wait(2):
                    raise AssertionError("Accepted task did not reach upstream")
            return result

    second = Gateway(ScheduledReadStore(app.store.path), app.gateway.service,
                     app.gateway.registry, demo=True)

    def endpoint(request):
        entered.set()
        if not release.wait(5):
            raise AssertionError("Synthetic concurrent task was not released")
        return upstream(request)

    app.gateway.service.contracts["demo-cloudyfan"].runner.client.http = httpx.Client(
        transport=httpx.MockTransport(endpoint))
    with ThreadPoolExecutor(2) as workers:
        duplicate = workers.submit(second.create, "studio", TASK)
        assert stale_read.wait(2)
        accepted = workers.submit(app.gateway.create, "studio", TASK)
        assert entered.wait(2)
        try:
            assert duplicate.result(timeout=1)["status"] == "running"
        finally:
            release.set()
        assert accepted.result(timeout=5)["status"] == "passed"
    assert app.gateway.snapshot()["summary"]["tasks"] == 1


def test_contract_matrix_lists_all_categories_without_claiming_execution_authority(tmp_path):
    app, upstream = build_demo(tmp_path, seed=False)
    snapshot = app.gateway.snapshot()
    capabilities = snapshot["capabilities"]
    assert len(capabilities["categories"]) == 23
    assert capabilities["notice"] == "registration_not_execution_authorization"
    assert capabilities["categories"][CATEGORY]["cloudyfan"]["contract_registered"] is True
    other = next(category for category in capabilities["categories"] if category != CATEGORY)
    assert capabilities["categories"][other]["cloudyfan"] == {
        "contract_registered": False, "contract_id": None,
        "reason": "routing_configuration_unavailable"}
    assert upstream.calls == []


def test_retention_preview_retains_evidence_and_never_deletes_or_dispatches(tmp_path):
    app, upstream = build_demo(tmp_path, seed=False)
    now = [time.time() + 1]
    created = now[0]
    store = Store(app.store.path, clock=lambda: now[0])
    service = TaskService(app.gateway.service.root, contracts=app.gateway.service.contracts,
        allowed_principals=app.gateway.service.allowed_principals,
        retention_policy=RetentionPolicy("monitor-access-v1", 30 * 86400), clock=lambda: now[0])
    gateway = Gateway(store, service, app.gateway.registry, demo=True)
    gateway.create("studio", TASK)
    calls = len(upstream.calls)
    now[0] += 31 * 86400
    preview = gateway.retention_preview("studio", TASK["run_id"])
    assert preview["raw_review_due"] is True
    assert preview["deletion_authorized"] is False
    assert preview["eligible"] is False
    assert "minimum_evidence_period" in preview["blockers"]
    assert preview["evidence_retain_until"] == created + 31536000
    assert preview["core_preview"]["targets"]
    assert all(Path(target["path"]).exists() for target in preview["core_preview"]["targets"])
    assert len(upstream.calls) == calls


def test_explicit_resume_extends_used_version_evidence_from_last_execution(tmp_path):
    now = [time.time() + 1]
    app, upstream = build_demo(tmp_path, seed=False, clock=lambda: now[0],
        retention_policy=RetentionPolicy("monitor-access-v1", 30 * 86400))
    upstream.fail_tiers = set()

    class InterruptedValidation(ArithmeticEnvironment):
        interrupted = False

        def validate(self, text):
            if not type(self).interrupted:
                type(self).interrupted = True
                raise RuntimeError("Synthetic validation interruption")
            return super().validate(text)

    contracts = app.gateway.service.contracts
    contracts["demo-cloudyfan"] = replace(
        contracts["demo-cloudyfan"], environment_factory=lambda workspace: InterruptedValidation())
    assert app.gateway.create("studio", TASK)["status"] == "engineering_interrupted"
    now[0] += 29 * 86400
    last_execution = now[0]
    assert app.gateway.execute("studio", TASK["run_id"])["status"] == "passed"
    calls = len(upstream.calls)
    now[0] += 2 * 86400
    preview = app.gateway.retention_preview("studio", TASK["run_id"])
    assert preview["version_last_used"] == last_execution
    assert preview["evidence_retain_until"] == last_execution + 365 * 86400
    assert "minimum_evidence_period" in preview["blockers"]
    assert len(upstream.calls) == calls


def test_long_task_keeps_retained_monitor_readable_and_cost_incomplete(tmp_path):
    app, upstream = build_demo(tmp_path, seed=False)
    service = TaskService(app.gateway.service.root, contracts=app.gateway.service.contracts,
        allowed_principals=app.gateway.service.allowed_principals,
        retention_policy=RetentionPolicy("monitor-access-v1", 30 * 86400))
    gateway = Gateway(app.store, service, app.gateway.registry, demo=True)
    entered, release = threading.Event(), threading.Event()

    def endpoint(request):
        entered.set()
        if not release.wait(5):
            raise AssertionError("Synthetic long task was not released")
        return upstream(request)

    service.contracts["demo-cloudyfan"].runner.client.http = httpx.Client(
        transport=httpx.MockTransport(endpoint))
    with ThreadPoolExecutor(2) as workers:
        task = workers.submit(gateway.create, "studio", TASK)
        assert entered.wait(2)
        observation = workers.submit(gateway.snapshot)
        try:
            view = observation.result(timeout=1)
            assert view["tasks"][0]["status"] == "running"
            assert view["tasks"][0]["complete_api_usd"] is None
        finally:
            release.set()
        assert task.result(timeout=5)["status"] == "passed"


def test_account_total_keeps_unmonitored_history_unattributed(tmp_path):
    app, upstream = build_demo(tmp_path, seed=False)
    service = app.gateway.service
    service.create("studio", {"run_id": "before-monitor", "contract_id": "demo-cloudyfan",
                              "prompt": TASK["prompt"]})
    assert service.execute("studio", "before-monitor")["status"] == "passed"
    assert app.gateway.create("agent", TASK)["status"] == "passed"
    ledger = app.gateway.snapshot()["ledger"]
    assert ledger["known_usd"] == "0.0106"
    assert ledger["by_client_known_usd"]["agent"] == "0.0053"
    assert ledger["by_client_known_usd"]["studio"] == "0"
    assert ledger["unattributed_known_usd"] == "0.0053"


@pytest.mark.parametrize("lifecycle", ["active", "expired", "retired"])
def test_disabled_client_retains_operator_public_history_without_raw_access(tmp_path, monkeypatch, lifecycle):
    now = [time.time() + 1]
    bundle = build_local_integration(tmp_path, clock=lambda: now[0])
    bundle.upstream.fail_tiers = set()
    client = httpx.Client(transport=httpx.WSGITransport(bundle.app), base_url=bundle.app.origin,
        headers={"Authorization": "Bearer " + bundle.credentials["studio"].token})
    operator = httpx.Client(transport=httpx.WSGITransport(bundle.app), base_url=bundle.app.origin,
        headers={"Authorization": "Bearer " + bundle.operator_token})
    assert client.post("/v1/tasks", json=TASK).status_code == 200
    before = operator.get("/monitor/snapshot").json()
    assert before["summary"]["passed"] == 1
    internal = bundle.app.gateway.service.internal_id("studio", TASK["run_id"])
    bundle.identity.set_active("studio", False)
    if lifecycle == "expired":
        now[0] += 30 * 86400
    elif lifecycle == "retired":
        bundle.app.gateway.service.retention.retire(internal)
    raw_reads = []

    def unavailable_raw_result(*args):
        raw_reads.append(args)
        raise ValueError("Synthetic unavailable original result")

    core = bundle.app.gateway.service.contracts["demo-cloudyfan"].runner.core
    original_read = core.read_completed
    monkeypatch.setattr(core, "read_completed", unavailable_raw_result)
    after = operator.get("/monitor/snapshot").json()
    assert after["summary"]["passed"] == 1
    assert after["summary"]["completed"] == 1
    assert after["summary"]["known_api_usd"] == before["summary"]["known_api_usd"]
    assert after["tasks"][0]["status"] == ("passed" if lifecycle == "active" else "expired")
    assert after["tasks"][0]["observation"] == "client_inactive"
    if lifecycle != "active":
        assert after["tasks"][0]["error_code"] == "task_" + lifecycle
    assert raw_reads == []
    calls = len(bundle.upstream.calls)
    assert client.get("/v1/tasks/" + TASK["run_id"] + "/result").status_code == 401
    assert client.post("/v1/tasks/" + TASK["run_id"] + "/resume", json={}).status_code == 401
    assert len(bundle.upstream.calls) == calls
    if lifecycle == "active":
        bundle.identity.set_active("studio", True)
        monkeypatch.setattr(core, "read_completed", original_read)
        restored = operator.get("/monitor/snapshot").json()
        assert restored["summary"]["passed"] == 1
        assert restored["tasks"][0]["observation"] is None
        monkeypatch.setattr(core, "read_completed", unavailable_raw_result)
        revalidated = operator.get("/monitor/snapshot").json()
        assert revalidated["summary"]["passed"] == 0
        assert revalidated["summary"]["unknown"] == 1
        assert raw_reads


def test_disabled_client_keeps_historical_ledger_ownership(tmp_path):
    bundle = build_local_integration(tmp_path)
    bundle.upstream.fail_tiers = set()
    gateway = bundle.app.gateway
    assert gateway.create("studio", TASK)["status"] == "passed"
    before = gateway.ledger()
    assert before["by_client_known_usd"]["studio"] == "0.0001"
    bundle.identity.set_active("studio", False)
    after = gateway.ledger()
    assert after["by_client_known_usd"]["studio"] == before["by_client_known_usd"]["studio"]
    assert after["unattributed_known_usd"] == before["unattributed_known_usd"] == "0"
    assert after["known_usd"] == before["known_usd"]
    assert all(row["client"] == "studio" and row["task_run"] == TASK["run_id"]
               for row in after["rows"])


@pytest.mark.parametrize("retained", [False, True])
def test_completed_task_stays_readable_while_another_task_is_executing(tmp_path, retained):
    app, upstream = build_demo(tmp_path, seed=False,
        retention_policy=RetentionPolicy("monitor-access-v1", 30 * 86400) if retained else None)
    gateway = app.gateway
    assert gateway.create("studio", TASK)["status"] == "passed"
    before = gateway.snapshot()
    completed = before["tasks"][0]
    entered, release = threading.Event(), threading.Event()

    def endpoint(request):
        entered.set()
        if not release.wait(5):
            raise AssertionError("Synthetic second task was not released")
        return upstream(request)

    gateway.service.contracts["demo-cloudyfan"].runner.client.http = httpx.Client(
        transport=httpx.MockTransport(endpoint))
    with ThreadPoolExecutor(2) as workers:
        task = workers.submit(gateway.create, "agent", {**TASK, "run_id": "second-long-task"})
        assert entered.wait(2)
        observation = workers.submit(gateway.snapshot)
        try:
            view = observation.result(timeout=1)
            first = next(item for item in view["tasks"] if item["client"] == "studio")
            assert first["status"] == "passed"
            assert first["final_pass"] is True
            assert first["path"] == completed["path"]
            assert first["complete_api_usd"] == completed["complete_api_usd"]
            assert view["summary"]["completed"] == 1
            assert view["summary"]["in_progress"] == 1
        finally:
            release.set()
        assert task.result(timeout=5)["status"] == "passed"


def test_task_expiry_is_visible_even_while_its_execution_holds_retention_guard(tmp_path):
    app, upstream = build_demo(tmp_path, seed=False)
    now = [time.time() + 1]
    store = Store(app.store.path, clock=lambda: now[0])
    service = TaskService(app.gateway.service.root, contracts=app.gateway.service.contracts,
        allowed_principals=app.gateway.service.allowed_principals,
        retention_policy=RetentionPolicy("monitor-access-v1", 30 * 86400), clock=lambda: now[0])
    gateway = Gateway(store, service, app.gateway.registry, demo=True)
    entered, release = threading.Event(), threading.Event()

    def endpoint(request):
        entered.set()
        if not release.wait(5):
            raise AssertionError("Synthetic expiring task was not released")
        return upstream(request)

    service.contracts["demo-cloudyfan"].runner.client.http = httpx.Client(
        transport=httpx.MockTransport(endpoint))
    with ThreadPoolExecutor(1) as workers:
        task = workers.submit(gateway.create, "studio", TASK)
        assert entered.wait(2)
        now[0] += 30 * 86400
        try:
            with pytest.raises(ServiceError) as expired:
                gateway.status("studio", TASK["run_id"])
            assert expired.value.status == 410
            view = gateway.snapshot()
            assert view["tasks"][0]["status"] == "expired"
            assert view["tasks"][0]["complete_api_usd"] is None
        finally:
            release.set()
        with pytest.raises(ServiceError):
            task.result(timeout=5)
    assert gateway.snapshot()["tasks"][0]["status"] == "expired"

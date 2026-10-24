"""Versioned bounded capacity queue at the agreed real HTTP seam."""
import pytest
from test_local_http_integration import local_service as local_service, loopback_network as loopback_network, TASK
from capacity_admission_fixture import attach_admission
from capacity_queue_fixture import attach_window_protocol, WindowProtocolFixture


def test_queued_acceptance_is_202_idempotent_cancellable_and_never_starts_paid_work(local_service):
    from capacity_queue_fixture import attach_queue
    bundle = local_service.bundle
    attach_queue(bundle, local_service.clock)
    local_service.server.set_app(bundle.app)
    operator, client = local_service.client("operator"), local_service.client()
    assert operator.post("/monitor/capacity/activate", json={"revision": 0,
        "version": "synthetic-queued-admission-v1"}).status_code == 200
    payload = {**TASK, "run_id": "queued-cancelled"}
    accepted = client.post("/v1/tasks", json=payload)
    assert accepted.status_code == 202, accepted.text
    view = accepted.json()
    assert view["status"] == "queued" and view["reason"] == "queue_waiting_worker"
    assert view["queue"]["protocol"] == "capacity-queue-v1"
    assert view["queue"]["limits"] == {"capacity": 8, "concurrency": 2, "per_principal": 4, "max_wait_seconds": 120}
    assert client.post("/v1/tasks", json=payload).status_code == 202
    assert client.post("/v1/tasks", json={**payload, "prompt": "changed"}).status_code == 409
    assert client.post("/v1/tasks/queued-cancelled/cancel", json={}).json()["status"] == "cancelled"
    assert client.post("/v1/tasks", json=payload).json()["status"] == "cancelled"
    snapshot = operator.get("/monitor/queue").json()
    assert snapshot["jobs"][0]["status"] == "cancelled"
    assert local_service.client("agent").get("/v1/tasks/queued-cancelled").status_code == 404
    assert bundle.upstream.calls == []
    assert list((bundle.root / "receipts/calls").glob("*/intent.json")) == []
    ledger = operator.get("/monitor/snapshot").json()["ledger"]
    assert ledger["rows"] == [] and ledger["reserved_usd"] == "0"


def test_worker_claims_original_exact_request_before_execution_and_duplicate_is_read_only(local_service):
    from capacity_queue_fixture import attach_queue
    import hashlib
    import json
    bundle = local_service.bundle
    bundle.upstream.fail_tiers = set()
    queue = attach_queue(bundle, local_service.clock)
    local_service.server.set_app(bundle.app)
    operator, client = local_service.client("operator"), local_service.client()
    operator.post("/monitor/capacity/activate", json={"revision": 0, "version": "synthetic-queued-admission-v1"})
    payload = {**TASK, "run_id": "queued-completed"}
    assert client.post("/v1/tasks", json=payload).status_code == 202
    # A direct service execution is a lookup, never a queue bypass.
    assert queue.service.execute("studio", payload["run_id"])["status"] == "queued"
    assert bundle.upstream.calls == []
    completed = queue.dispatch_next()
    assert completed["status"] == "passed"
    assert len(bundle.upstream.calls) == 1
    saved = next((bundle.root / "receipts/calls").glob("*/intent.json"))
    intent = json.loads(saved.read_text())
    assert intent["dispatch"]["capacity_allocation"]
    allocation = operator.get("/monitor/capacity").json()["dispatch"]["allocations"][0]
    assert allocation["state"] == "complete"
    assert allocation["wire_sha256"] == intent["dispatch"]["wire_sha256"]
    before = hashlib.sha256(saved.read_bytes()).hexdigest()
    assert client.post("/v1/tasks", json=payload).json()["status"] == "passed"
    assert client.post("/v1/tasks/queued-completed/resume", json={}).json()["status"] == "passed"
    assert queue.dispatch_next() is None
    assert len(bundle.upstream.calls) == 1 and hashlib.sha256(saved.read_bytes()).hexdigest() == before
    assert client.get("/v1/tasks/queued-completed/result").json()["answer"] == "2"


def test_sequential_waits_before_next_intent_preserving_original_reserve_and_global_counts(local_service):
    from capacity_queue_fixture import attach_queue
    import hashlib
    bundle = local_service.bundle
    queue = attach_queue(bundle, local_service.clock, rpm=1)
    local_service.server.set_app(bundle.app)
    operator, client = local_service.client("operator"), local_service.client()
    operator.post("/monitor/capacity/activate", json={"revision": 0, "version": "synthetic-queued-admission-v1"})
    payload = {**TASK, "run_id": "queued-sequential"}
    assert client.post("/v1/tasks", json=payload).status_code == 202
    first = queue.dispatch_next()
    assert first["status"] == "queued" and len(bundle.upstream.calls) == 1
    assert list((bundle.root / "receipts/calls").glob("*/intent.json"))[0].parent.name.endswith("-a0-t0")
    assert queue.dispatch_next() is None
    waiting = client.get("/v1/tasks/queued-sequential").json()
    assert waiting["reason"] == "queue_waiting_capacity" and waiting["capacity_reason"] == "capacity_rpm_exhausted"
    report = queue.service.contracts["demo-cloudyfan"].runner.store.report()
    assert len(report["reservations"]) == 1 and len(report["events"]) == 1 and report["unknown_events"] == 0
    files = [queue.path, bundle.root / "capacity.sqlite", bundle.root / "budget.sqlite",
             *[path for path in (bundle.root / "receipts").rglob("*") if path.is_file()]]
    before = {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in files}
    for _ in range(3):
        client.get("/v1/tasks/queued-sequential")
        operator.get("/monitor/queue")
        client.post("/v1/tasks", json=payload)
        client.post("/v1/tasks/queued-sequential/resume", json={})
    assert before == {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in files}
    assert len(bundle.upstream.calls) == 1
    # Queue's original120s deadline, not a new per-call timeout, stays fixed.
    local_service.clock.now += 61
    second = queue.dispatch_next()
    assert second["status"] == "queued" and len(bundle.upstream.calls) == 2
    assert "questions" in bundle.upstream.calls[-1]
    assert second["queue"]["deadline"] == first["queue"]["deadline"]
    assert queue.dispatch_next() is None
    local_service.clock.now += 60
    assert client.get("/v1/tasks/queued-sequential").json()["status"] == "expired"
    assert queue.dispatch_next() is None and len(bundle.upstream.calls) == 2


def test_shared_host_reopen_retains_original_identity_configuration_and_next_request(local_service):
    from capacity_queue_fixture import build_queue_host
    host = build_queue_host(local_service.bundle.root / 'reopened', clock=local_service.clock,
                            origin=local_service.origin, options={'separate_budgets': True})
    local_service.bundle = host.bundle
    local_service.server.set_app(host.app)
    operator, client = local_service.client('operator'), local_service.client()
    operator.post('/monitor/capacity/activate', json={'revision': 0, 'version': 'synthetic-queued-admission-v1'})
    assert client.post('/v1/tasks', json={**TASK, 'run_id': 'reopened-queue'}).status_code == 202
    original = client.get('/v1/tasks/reopened-queue').json()
    local_service.clock.now += 10
    reopened = build_queue_host(host.bundle.root, clock=local_service.clock,
                                origin=local_service.origin, options={'separate_budgets': True})
    try:
        assert reopened.credential == host.credential
        assert reopened.queue.path == host.queue.path and reopened.queue.service.root == host.queue.service.root
        local_service.bundle = reopened.bundle
        local_service.server.set_app(reopened.app)
        assert client.get('/v1/tasks/reopened-queue').json()['queue']['accepted_at'] == original['queue']['accepted_at']
        assert reopened.queue.dispatch_next()['status'] == 'passed'
        assert len(reopened.upstream.calls) == 1 and len(host.upstream.calls) == 0
        assert client.get('/v1/tasks/reopened-queue/result').json()['answer'] == '2'
    finally:
        host.close()
        reopened.close()


@pytest.mark.parametrize('stop', ['cancel', 'expire'])
def test_stopping_waiting_continuation_preserves_paid_response_and_releases_only_unused_fee_reserve(local_service, stop):
    from capacity_queue_fixture import attach_queue
    import hashlib
    bundle = local_service.bundle
    queue = attach_queue(bundle, local_service.clock, rpm=1)
    local_service.server.set_app(bundle.app)
    operator, client = local_service.client('operator'), local_service.client()
    operator.post('/monitor/capacity/activate', json={'revision': 0, 'version': 'synthetic-queued-admission-v1'})
    payload = {**TASK, 'run_id': 'stop-continuation'}
    assert client.post('/v1/tasks', json=payload).status_code == 202
    assert queue.dispatch_next()['status'] == 'queued'
    paths = list((bundle.root / 'receipts/calls').glob('*/*.json'))
    before = {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}
    local_service.clock.now += 3 if stop == 'cancel' else 121
    if stop == 'cancel':
        state = client.post('/v1/tasks/stop-continuation/cancel', json={}).json()
        assert state['reason'] == 'cancelled_before_next_dispatch'
    else:
        assert client.get('/v1/tasks/stop-continuation').json()['status'] == 'expired'
        queue.dispatch_next()  # explicit mutation closes only the original unused monetary reserve
        state = client.get('/v1/tasks/stop-continuation').json()
    report = queue.service.contracts['demo-cloudyfan'].runner.store.report()
    assert report['known_usd'] == '0.0001' and report['unknown_events'] == 0 and report['reserved_usd'] == '0'
    assert state['provider_cancellation_confirmed'] is False
    assert before == {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}
    assert len(bundle.upstream.calls) == 1 and len(report['events']) == 1
    elapsed = state['queue']['waiting_seconds']
    local_service.clock.now += 5
    assert client.post('/v1/tasks', json=payload).json()['status'] == state['status']
    assert client.get('/v1/tasks/stop-continuation').json()['queue']['waiting_seconds'] == elapsed
    assert client.post('/v1/tasks/stop-continuation/resume', json={}).json()['status'] == state['status']
    assert len(bundle.upstream.calls) == 1


def test_actual_ollama_sdk_pending_is_honest_and_completed_duplicate_does_not_dispatch(local_service):
    from capacity_queue_fixture import build_queue_host
    from pydantic import ValidationError
    import ollama
    host = build_queue_host(local_service.bundle.root / 'native', clock=local_service.clock, origin=local_service.origin)
    local_service.bundle = host.bundle
    local_service.server.set_app(host.app)
    operator, client = local_service.client('operator'), local_service.client()
    operator.post('/monitor/capacity/activate', json={'revision': 0, 'version': 'synthetic-queued-admission-v1'})
    prompt = 'Return only 1+1.'
    for endpoint, run in (('chat', 'native-chat-raw'), ('generate', 'native-generate-raw')):
        payload = {'model': 'queue-mrc', 'stream': False,
            **({'messages': [{'role': 'user', 'content': prompt}]} if endpoint == 'chat' else {'prompt': prompt})}
        response = client.post('/api/' + endpoint, json=payload, headers={'X-CloudyFan-Run-ID': run})
        assert response.status_code == 202, response.text
        body = response.json()
        assert body['done'] is False and 'response' not in body and 'message' not in body
        assert body['cloudyfan']['pending'] is True
    sdk = ollama.Client(host=local_service.origin, headers={'Authorization': 'Bearer ' + host.credential,
        'X-CloudyFan-Run-ID': 'native-generate-sdk'}, timeout=5)
    try:
        generated = sdk.generate(model='queue-mrc', prompt=prompt, stream=False)
        assert generated.done is False and generated.response is None
    finally:
        sdk._client.close()
    chat = ollama.Client(host=local_service.origin, headers={'Authorization': 'Bearer ' + host.credential,
        'X-CloudyFan-Run-ID': 'native-chat-sdk'}, timeout=5)
    try:
        # SDK0.6.3 requires a ChatResponse.message and cannot represent honest
        # answerless pending chat. The original queued task remains queryable.
        with pytest.raises(ValidationError):
            chat.chat(model='queue-mrc', messages=[{'role': 'user', 'content': prompt}], stream=False)
    finally:
        chat._client.close()
    assert client.get('/v1/tasks/native-chat-sdk').json()['status'] == 'queued'
    assert host.upstream.calls == []
    assert host.queue.dispatch_next()['status'] == 'passed'
    completed = ollama.Client(host=local_service.origin, headers={'Authorization': 'Bearer ' + host.credential,
        'X-CloudyFan-Run-ID': 'native-chat-raw'}, timeout=5)
    try:
        answer = completed.chat(model='queue-mrc', messages=[{'role': 'user', 'content': prompt}], stream=False)
        assert answer.done is True and answer.message.content == '2'
    finally:
        completed._client.close()
        host.close()
    assert len(host.upstream.calls) == 1


def test_actual_environment_schema_must_match_host_preparation_before_accepted_queue_or_fees(local_service):
    from dataclasses import replace
    from capacity_queue_fixture import attach_queue
    from cloudyfan_jev.demo import ArithmeticEnvironment
    bundle = local_service.bundle
    queue = attach_queue(bundle, local_service.clock)
    class MismatchedEnvironment(ArithmeticEnvironment):
        def tools_schema(self):
            return [{'type': 'function', 'function': {'name': 'unregistered-effect', 'parameters': {}}}]
    original = queue.service.contracts['demo-cloudyfan']
    queue.service.contracts['demo-cloudyfan'] = replace(original, environment_factory=lambda workspace: MismatchedEnvironment())
    local_service.server.set_app(bundle.app)
    operator, client = local_service.client('operator'), local_service.client()
    operator.post('/monitor/capacity/activate', json={'revision': 0, 'version': 'synthetic-queued-admission-v1'})
    result = client.post('/v1/tasks', json={**TASK, 'run_id': 'unsupported-preparation'})
    assert result.status_code == 409 and result.json()['error']['code'] == 'queue_environment_contract_changed'
    assert bundle.upstream.calls == []
    assert queue.service.contracts['demo-cloudyfan'].runner.store.report()['reservations'] == []


def test_operator_queue_keeps_new_active_work_visible_after_one_hundred_terminal_jobs(local_service):
    from capacity_queue_fixture import attach_queue
    import hashlib
    queue = attach_queue(local_service.bundle, local_service.clock)
    local_service.server.set_app(local_service.bundle.app)
    operator, client = local_service.client('operator'), local_service.client()
    operator.post('/monitor/capacity/activate', json={'revision': 0, 'version': 'synthetic-queued-admission-v1'})
    for index in range(101):
        run = 'terminal-' + str(index)
        assert client.post('/v1/tasks', json={**TASK, 'run_id': run}).status_code == 202
        assert client.post('/v1/tasks/' + run + '/cancel', json={}).json()['status'] == 'cancelled'
        local_service.clock.now += 1
    assert client.post('/v1/tasks', json={**TASK, 'run_id': 'new-active'}).status_code == 202
    before = hashlib.sha256(queue.path.read_bytes()).hexdigest()
    view = operator.get('/monitor/queue', timeout=20).json()
    assert view['jobs'][0]['run_id'] == 'new-active'
    assert view['active_count'] == 1 and view['job_count'] == 102
    assert len(view['jobs']) == 100 and view['history_complete'] is False
    assert view['active_history_complete'] is True
    assert view['jobs'][1]['run_id'] == 'terminal-100'
    assert hashlib.sha256(queue.path.read_bytes()).hexdigest() == before
    assert local_service.bundle.upstream.calls == []


@pytest.mark.parametrize('limits,reason', [({'capacity': 1, 'concurrency': 1, 'per_principal': 4, 'max_wait_seconds': 120}, 'queue_full'),
    ({'capacity': 4, 'concurrency': 1, 'per_principal': 1, 'max_wait_seconds': 120}, 'principal_quota_full')])
def test_operator_sees_original_safe_bounded_queue_rejection_without_creating_paid_work(local_service, limits, reason):
    from capacity_queue_fixture import attach_queue
    from cloudyfan_jev.scheduler import QueueLimits
    import hashlib
    queue = attach_queue(local_service.bundle, local_service.clock, limits=QueueLimits(**limits))
    local_service.server.set_app(local_service.bundle.app)
    operator, client = local_service.client('operator'), local_service.client()
    operator.post('/monitor/capacity/activate', json={'revision': 0, 'version': 'synthetic-queued-admission-v1'})
    assert client.post('/v1/tasks', json={**TASK, 'run_id': 'accepted-original'}).status_code == 202
    denied = client.post('/v1/tasks', json={**TASK, 'run_id': 'rejected-original'})
    assert denied.status_code == 429 and denied.json()['error']['code'] == reason
    before = hashlib.sha256(queue.path.read_bytes()).hexdigest()
    view = operator.get('/monitor/queue').json()
    assert view['rejection_count'] == 1 and view['rejection_history_complete'] is True
    assert view['rejections'] == [{'client': 'studio', 'run_id': 'rejected-original', 'reason': reason, 'at': local_service.clock()}]
    assert len(view['jobs']) == 1 and view['jobs'][0]['run_id'] == 'accepted-original'
    assert hashlib.sha256(queue.path.read_bytes()).hexdigest() == before
    assert local_service.bundle.upstream.calls == []


def test_original_actual_payload_post_guard_denial_waits_without_a_slot_allocation_intent_or_fee_event(local_service):
    from capacity_queue_fixture import attach_queue
    bundle = local_service.bundle
    class ClosedPostGuard:
        def __call__(self, *args):
            return True
        def verify_dispatch(self, *args):
            return True
        def verify_post(self, *args):
            return False
    for contract in bundle.app.gateway.service.contracts.values():
        contract.runner.verify_bound = ClosedPostGuard()
    queue = attach_queue(bundle, local_service.clock)
    local_service.server.set_app(bundle.app)
    operator, client = local_service.client('operator'), local_service.client()
    operator.post('/monitor/capacity/activate', json={'revision': 0, 'version': 'synthetic-queued-admission-v1'})
    assert client.post('/v1/tasks', json={**TASK, 'run_id': 'original-guard-wait'}).status_code == 202
    assert queue.dispatch_next() is None
    view = client.get('/v1/tasks/original-guard-wait').json()
    assert view['status'] == 'queued' and view['reason'] == 'queue_waiting_budget'
    report = queue.service.contracts['demo-cloudyfan'].runner.store.report()
    assert report['events'] == [] and report['reservations'] == []
    assert operator.get('/monitor/capacity').json()['dispatch']['allocations'] == []
    assert bundle.upstream.calls == []


def test_queue_projects_saved_binding_and_tariff_and_captures_original_worker_completion(local_service):
    from capacity_queue_fixture import attach_queue
    bundle = local_service.bundle
    bundle.upstream.fail_tiers = set()
    queue = attach_queue(bundle, local_service.clock)
    local_service.server.set_app(bundle.app)
    operator, client = local_service.client('operator'), local_service.client()
    operator.post('/monitor/capacity/activate', json={'revision': 0, 'version': 'synthetic-queued-admission-v1'})
    original = client.post('/v1/tasks', json={**TASK, 'run_id': 'frozen-queue-binding'}).json()
    frozen = original['queue']['frozen']
    assert frozen['contract_id'] == 'demo-cloudyfan' and frozen['mode'] == 'cloudyfan'
    assert frozen['monitor']['mode'] == 'cloudyfan' and frozen['monitor']['category'] == TASK['category']
    assert frozen['monitor']['tariff']['revision'] == 0 and frozen['monitor']['tariff']['multiplier'] == '1'
    assert frozen['access_expires_at'] == original['queue']['accepted_at'] + 30 * 86400
    bundle.app.gateway.store.change(revision=bundle.app.gateway.store.control()['revision'], mode='fixed_high')
    assert operator.post('/monitor/billing/settings', json={'revision': 0, 'enabled': True,
        'multiplier': '3', 'client_multipliers': {}, 'open_source_prices': {}}).status_code == 200
    def advance(payload):
        local_service.clock.now += 3
    bundle.upstream.before_response = advance
    completed = queue.dispatch_next()
    assert completed['status'] == 'passed'
    projected = client.get('/v1/tasks/frozen-queue-binding').json()
    assert projected['queue']['frozen'] == frozen
    assert operator.get('/monitor/queue').json()['jobs'][0]['queue']['frozen'] == frozen
    saved = bundle.app.gateway.store.observation('studio', 'frozen-queue-binding')
    assert saved['status'] == 'passed' and saved['generation_attempts'] == 1 and saved['queue']['frozen'] == frozen
    assert saved['updated'] == local_service.clock()
    assert len(bundle.upstream.calls) == 1


def test_revoked_original_credential_stops_paid_continuation_even_with_fresh_same_principal_token(local_service):
    from capacity_queue_fixture import attach_queue
    import hashlib
    bundle = local_service.bundle
    queue = attach_queue(bundle, local_service.clock, rpm=10)
    local_service.server.set_app(bundle.app)
    operator, client = local_service.client('operator'), local_service.client()
    operator.post('/monitor/capacity/activate', json={'revision': 0, 'version': 'synthetic-queued-admission-v1'})
    payload = {**TASK, 'run_id': 'paid-then-original-credential-revoked'}
    accepted = client.post('/v1/tasks', json=payload)
    assert accepted.status_code == 202
    assert queue.dispatch_next()['status'] == 'queued'
    receipts = {str(path): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in (bundle.root / 'receipts').rglob('*.json')}
    identity = bundle.app.identity_store
    identity.revoke(bundle.credentials['studio'].id)
    fresh = identity.issue('studio', scopes={'tasks:read', 'tasks:write', 'models:read'},
        expires_at=local_service.clock() + 86400)
    fresh_headers = {'Authorization': 'Bearer ' + fresh.token}
    assert client.get('/v1/tasks/' + payload['run_id']).status_code == 401
    assert client.post('/v1/tasks/' + payload['run_id'] + '/resume', json={}, headers=fresh_headers).json()['status'] == 'queued'
    assert queue.dispatch_next() is None  # A rejection is not a claimed execution.
    observed = client.get('/v1/tasks/' + payload['run_id'], headers=fresh_headers).json()
    assert observed['status'] == 'rejected' and observed['capacity_reason'] == 'capacity_credential_inactive'
    assert observed['queue']['frozen'] == accepted.json()['queue']['frozen']
    report = queue.service.contracts['demo-cloudyfan'].runner.store.report()
    assert report['known_usd'] == '0.0001' and report['unknown_events'] == 0 and report['reserved_usd'] == '0'
    assert len(report['events']) == 1 and len(bundle.upstream.calls) == 1
    assert receipts == {str(path): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in (bundle.root / 'receipts').rglob('*.json')}


def test_claim_journal_counts_against_original_principal_quota_before_any_capacity_allocation(local_service, monkeypatch):
    from capacity_queue_fixture import attach_queue
    from cloudyfan_jev.scheduler import QueueLimits
    from cloudyfan_jev.capacity_dispatch import _CapacityDispatch
    from concurrent.futures import ThreadPoolExecutor
    import threading
    bundle = local_service.bundle
    bundle.upstream.fail_tiers = set()
    queue = attach_queue(bundle, local_service.clock, limits=QueueLimits(4, 2, 1, 120))
    local_service.server.set_app(bundle.app)
    operator, client = local_service.client('operator'), local_service.client()
    operator.post('/monitor/capacity/activate', json={'revision': 0, 'version': 'synthetic-queued-admission-v1'})
    assert client.post('/v1/tasks', json={**TASK, 'run_id': 'claim-quota-original'}).status_code == 202
    entered, release = threading.Event(), threading.Event()
    original = _CapacityDispatch.prepare
    def held(dispatch, cid, request, *, reserve=True):
        if reserve:
            entered.set()
            if not release.wait(5):
                raise AssertionError('bounded claim release missing')
        return original(dispatch, cid, request, reserve=reserve)
    monkeypatch.setattr(_CapacityDispatch, 'prepare', held)
    with ThreadPoolExecutor(1) as executor:
        future = executor.submit(queue.dispatch_next)
        try:
            assert entered.wait(5)
            assert client.get('/v1/tasks/claim-quota-original').json()['queue_state'] == 'claiming'
            assert operator.get('/monitor/capacity').json()['dispatch']['allocations'] == []
            denied = client.post('/v1/tasks', json={**TASK, 'run_id': 'claim-quota-later'})
            assert denied.status_code == 429 and denied.json()['error']['code'] == 'principal_quota_full'
            assert bundle.upstream.calls == []
        finally:
            release.set()
        assert future.result(timeout=10)['status'] == 'passed'
    assert len(bundle.upstream.calls) == 1


def test_original_saved_429_makes_queued_next_boundary_wait_for_real_window_without_an_extra_post(local_service):
    from capacity_queue_fixture import build_queue_host
    import hashlib
    host = build_queue_host(local_service.bundle.root / 'real-window', clock=local_service.clock,
        origin=local_service.origin, options={'retry_after_seconds': 30})
    local_service.bundle = host.bundle
    local_service.server.set_app(host.app)
    operator, client = local_service.client('operator'), local_service.client()
    operator.post('/monitor/capacity/activate', json={'revision': 0, 'version': 'synthetic-queued-admission-v1'})
    assert client.post('/v1/tasks', json={**TASK, 'run_id': 'queue-real-window'}).status_code == 202
    try:
        assert host.queue.dispatch_next()['status'] == 'queued'
        assert len(host.upstream.calls) == 1
        assert host.queue.dispatch_next() is None
        state = client.get('/v1/tasks/queue-real-window').json()
        assert state['reason'] == 'queue_waiting_window' and state['capacity_reason'] == 'capacity_window_wait'
        window = operator.get('/monitor/capacity').json()['dispatch']['windows']
        assert window['groups'][0]['not_before'] == local_service.clock() + 30
        paths = [host.queue.path, host.bundle.root / 'capacity.sqlite', *list((host.bundle.root / 'receipts').rglob('*.json'))]
        original = {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}
        assert operator.get('/monitor/queue').json()['jobs'][0]['reason'] == 'queue_waiting_window'
        assert client.post('/v1/tasks/queue-real-window/resume', json={}).json()['status'] == 'queued'
        assert {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths} == original
        assert len(host.upstream.calls) == 1
        local_service.clock.now += 30
        assert host.queue.dispatch_next()['status'] == 'queued'
        assert len(host.upstream.calls) == 2
        assert host.queue.dispatch_next()['status'] == 'passed'
        assert len(host.upstream.calls) == 3
    finally:
        host.close()


def test_window_protocol_is_explicitly_not_applicable_on_original_immediate_policy(local_service):
    bundle = local_service.bundle
    attach_admission(bundle, local_service.clock)
    local_service.server.set_app(bundle.app)
    response = local_service.client("operator").get("/monitor/capacity")
    assert response.status_code == 200
    assert response.json()["dispatch"]["windows"] == {
        "configured": False, "protocol": "capacity-windows-v1", "status": "not_applicable"}


def test_enabled_window_wait_and_missing_backend_refuse_before_paid_intent(local_service):
    bundle = local_service.bundle
    previous = attach_admission(bundle, local_service.clock, rpm=10)
    backend = WindowProtocolFixture(local_service.clock, status="wait")
    controller = attach_window_protocol(bundle, previous, backend)
    local_service.server.set_app(bundle.app)
    operator, client = local_service.client("operator"), local_service.client()
    assert operator.post("/monitor/capacity/activate", json={"revision": 0,
        "version": "synthetic-window-enabled-v2"}).status_code == 200
    waiting = client.post("/v1/tasks", json={**TASK, "run_id": "window-spi-wait"})
    assert waiting.status_code == 429 and waiting.json()["error"]["code"] == "capacity_window_wait"
    assert waiting.json()["costs"]["unknown_events"] == 0
    controller.window_backend = None
    denied = client.post("/v1/tasks", json={**TASK, "run_id": "window-spi-missing"})
    assert denied.status_code == 429 and denied.json()["error"]["code"] == "capacity_window_backend_unavailable"
    assert bundle.upstream.calls == []
    assert list((bundle.root / "receipts/calls").glob("*/intent.json")) == []
    view = operator.get("/monitor/capacity").json()["dispatch"]
    assert view["allocation_count"] == 0
    assert view["windows"]["status"] == "unknown"


def test_enabled_window_original_header_envelope_is_bounded_and_follows_durable_response(local_service):
    import httpx
    bundle = local_service.bundle
    bundle.upstream.fail_tiers = set()
    previous = attach_admission(bundle, local_service.clock, rpm=10)
    backend = WindowProtocolFixture(local_service.clock)
    def advancing_clock():
        local_service.clock.now += .001
        return local_service.clock()
    previous.store.clock = advancing_clock
    attach_window_protocol(bundle, previous, backend)
    for contract in bundle.app.gateway.service.contracts.values():
        def response(request):
            original = bundle.upstream(request)
            return httpx.Response(original.status_code, json=original.json(),
                headers=[("retry-after", "30"), ("Retry-After", "40"), ("date", "Wed, 21 Oct 2026 00:00:00 GMT")])
        contract.runner.client.http = httpx.Client(transport=httpx.MockTransport(response), trust_env=False)
    # Credentials and http transport are pinned at registration; re-register the
    # same already controlled transport objects before task acceptance.
    controller = bundle.app.capacity_controller
    controller.register("synthetic-window-enabled-v2", configuration_version="synthetic-admission-config-v1",
        routes=previous.routes["synthetic-admission-v1"], max_seconds=300, windows_version=backend.version)
    local_service.server.set_app(bundle.app)
    operator, client = local_service.client("operator"), local_service.client()
    assert operator.post("/monitor/capacity/activate", json={"revision": 0,
        "version": "synthetic-window-enabled-v2"}).status_code == 200
    completed = client.post("/v1/tasks", json={**TASK, "run_id": "window-spi-receipt"})
    assert completed.status_code == 200 and completed.json()["status"] == "passed", completed.text
    assert len(bundle.upstream.calls) == len(backend.responses) == 1
    observation = backend.responses[0]
    headers = observation["response"]["window_headers"]
    assert headers["protocol"] == "capacity-window-headers-v1" and headers["invalid"] is True
    assert len(headers["values"]) <= 8 and all(len(value) <= 160 for name, value in headers["values"])
    assert observation["scope"]["wire_sha256"] is not None
    assert observation["scope"]["physical_groups"][0]["group"] == "shared"

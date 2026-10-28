"""Rule adoption at the approved real operator/task HTTP seam."""
from task_features_fixture import task_features_http as task_features_http
from task_features_fixture import POLICY_BUNDLE, StoreFeatureBindingPort
from task_features_fixture import ArtifactRecorder
from test_local_http_integration import loopback_network as loopback_network
from test_local_http_integration import local_service as local_service
from test_local_http_integration import TASK
from cloudyfan_jev import task_recommendations as advice
from concurrent.futures import ThreadPoolExecutor
import threading
import sqlite3
import pytest
import tracemalloc
import json
import os
from pathlib import Path
import io
import hashlib


def test_operator_rule_activation_changes_new_bindings_without_recomputing_an_accepted_task(task_features_http):
    tracer = task_features_http
    operator, client = tracer.client('operator'), tracer.client('agent')
    capacity = operator.get('/monitor/capacity')
    assert capacity.status_code == 200
    assert operator.post('/monitor/capacity/activate', json={
        'revision': capacity.json()['dispatch']['revision'], 'version': tracer.controlled.policy_version,
    }).status_code == 200
    first_payload = {'run_id': 'before-rule-switch', 'category': tracer.controlled.category,
                     'prompt': '中文1+1 PRIVATE_RULE_SWITCH_CANARY'}
    first = client.post('/v1/tasks', json=first_payload)
    assert first.status_code == 202
    original = first.json()['task_features']
    assert original['status'] == 'available' and original['feature_version'] == 'local-deterministic-v1'
    control = operator.get('/monitor/observation-policy')
    tracer.evidence['phase'] = 'asserting_real_operator_rule_control'
    tracer.evidence['policy_control_http_status'] = control.status_code
    assert control.status_code == 200, 'The authenticated operator cannot inspect creation-observation policy'
    assert control.json()['schema_version'] == 'creation-observation-control-v1'
    changed = operator.post('/monitor/observation-policy/activate', json={
        'revision': control.json()['revision'], 'version': 'features-v2',
    })
    assert changed.status_code == 200
    assert changed.json()['active_version'] == 'features-v2'
    second = client.post('/v1/tasks', json={**first_payload, 'run_id': 'after-rule-switch'})
    assert second.status_code == 202
    assert second.json()['task_features']['feature_version'] == 'local-deterministic-v2'
    for response in (client.get('/v1/tasks/before-rule-switch'), client.post('/v1/tasks', json=first_payload)):
        assert response.status_code == 202
        assert response.json()['task_features'] == original
    observed = tracer.snapshot('after_operator_rule_switch_and_original_replay')
    assert observed['model_requests'] == observed['source_requests'] == observed['accounting_requests'] == []


def test_a_concurrent_disable_cannot_replace_an_already_accepted_tasks_original_recorder(task_features_http):
    tracer = task_features_http
    operator, client = tracer.client('operator'), tracer.client('agent')
    capacity = operator.get('/monitor/capacity')
    assert operator.post('/monitor/capacity/activate', json={
        'revision': capacity.json()['dispatch']['revision'], 'version': tracer.controlled.policy_version,
    }).status_code == 200
    control = operator.get('/monitor/observation-policy')
    selected = operator.post('/monitor/observation-policy/activate', json={
        'revision': control.json()['revision'], 'version': 'features-v2-advice-v1',
    })
    assert selected.status_code == 200
    reached, release = threading.Event(), threading.Event()

    class PausingRealStorePort(StoreFeatureBindingPort):
        def snapshot(self, principal, run_id):
            actual = super().snapshot(principal, run_id)
            if run_id == 'rule-switch-in-flight' and actual['seal'] is None and not reached.is_set():
                reached.set()
                assert release.wait(5), 'Test scheduling barrier was not released'
            return actual

    service = tracer.controlled.host.service
    service.creation_features = {'policy_bundle': {**POLICY_BUNDLE, 'feature_version': 'local-deterministic-v2'},
                                 'binding_port': PausingRealStorePort(tracer.controlled.app.store)}
    service.creation_recommendations = advice.enabled_policy()
    # Trusted host wiring pauses the real Store port used by the prepared
    # catalogue; task, binding, capture and response all remain real.
    from cloudyfan_monitor.observation_policy import ObservationPolicy
    tracer.controlled.app.observation_policy = ObservationPolicy(tracer.controlled.app.gateway,
        binding_port=PausingRealStorePort(tracer.controlled.app.store))
    payload = {'run_id': 'rule-switch-in-flight', 'category': tracer.controlled.category,
               'prompt': '中文1+1 PRIVATE_IN_FLIGHT_RULE_CANARY'}
    with ThreadPoolExecutor(max_workers=1) as threads:
        pending = threads.submit(client.post, '/v1/tasks', json=payload)
        try:
            assert reached.wait(5), 'Original binding did not reach the real Store observation port'
            disabled = operator.post('/monitor/observation-policy/activate', json={
                'revision': selected.json()['revision'], 'version': None,
            })
            assert disabled.status_code == 200 and disabled.json()['capture_enabled'] is False
        finally:
            release.set()
        accepted = pending.result(timeout=5)
    tracer.evidence['phase'] = 'asserting_original_recorder_after_concurrent_disable'
    tracer.snapshot('after_original_binding_and_concurrent_operator_disable')
    assert accepted.status_code == 202
    assert accepted.json()['task_features']['status'] == 'available', 'A later rule switch replaced the original accepted capture'
    assert accepted.json()['task_features']['feature_version'] == 'local-deterministic-v2'
    assert accepted.json()['routing_recommendation']['status'] == 'unknown'
    assert accepted.json()['routing_recommendation']['adopted'] is False


def test_new_tasks_follow_the_persisted_operator_rule_after_old_host_defaults_are_restored(task_features_http):
    tracer = task_features_http
    operator, client = tracer.client('operator'), tracer.client('agent')
    capacity = operator.get('/monitor/capacity')
    assert operator.post('/monitor/capacity/activate', json={
        'revision': capacity.json()['dispatch']['revision'], 'version': tracer.controlled.policy_version,
    }).status_code == 200
    control = operator.get('/monitor/observation-policy')
    changed = operator.post('/monitor/observation-policy/activate', json={
        'revision': control.json()['revision'], 'version': 'features-v2',
    })
    assert changed.status_code == 200 and changed.json()['active_version'] == 'features-v2'
    tracer.controlled.host.service.creation_features = {
        'policy_bundle': dict(POLICY_BUNDLE), 'binding_port': StoreFeatureBindingPort(tracer.controlled.app.store)}
    accepted = client.post('/v1/tasks', json={'run_id': 'durable-rule-adoption',
        'category': tracer.controlled.category, 'prompt': '中文1+1 PRIVATE_DURABLE_RULE_CANARY'})
    assert accepted.status_code == 202
    tracer.evidence['phase'] = 'asserting_durable_operator_rule_at_original_binding'
    assert accepted.json()['task_features']['feature_version'] == 'local-deterministic-v2', 'The new binding used stale host memory instead of the persisted operator selection'
    assert operator.get('/monitor/observation-policy').json()['revision'] == changed.json()['revision']
    observed = tracer.snapshot('after_persisted_rule_and_old_host_defaults')
    assert observed['model_requests'] == observed['source_requests'] == observed['accounting_requests'] == []


@pytest.mark.parametrize('clock_type', [int, float], ids=['integer-clock', 'float-clock'])
def test_explicit_operator_disable_survives_sqlite_time_storage_and_reopen(task_features_http, clock_type):
    tracer = task_features_http
    tracer.clock.now = clock_type(tracer.clock.now)
    operator, client = tracer.client('operator'), tracer.client('agent')
    capacity = operator.get('/monitor/capacity')
    assert operator.post('/monitor/capacity/activate', json={
        'revision': capacity.json()['dispatch']['revision'], 'version': tracer.controlled.policy_version,
    }).status_code == 200
    initial = operator.get('/monitor/observation-policy').json()
    off = operator.post('/monitor/observation-policy/activate', json={
        'revision': initial['revision'], 'version': None})
    assert off.status_code == 200 and off.json()['capture_enabled'] is False
    payload = {'run_id': 'numeric-clock-explicit-disable', 'category': tracer.controlled.category,
               'prompt': 'PRIVATE_NUMERIC_CLOCK_CANARY 中文1+1'}
    accepted = client.post('/v1/tasks', json=payload)
    assert accepted.status_code == 202
    assert accepted.json()['task_features']['status'] == 'not_provided'
    assert accepted.json()['routing_recommendation']['status'] == 'not_provided'
    binding = tracer.controlled.app.store.binding('agent', payload['run_id'])
    internal = tracer.controlled.host.service.internal_id('agent', payload['run_id'])
    original_expiry = tracer.controlled.host.service.retention.lookup(internal)['expires_at']
    tracer.reopen_disabled()
    assert operator.get('/monitor/observation-policy').json() == off.json()
    for response in (client.get('/v1/tasks/' + payload['run_id']), client.post('/v1/tasks', json=payload)):
        assert response.status_code == 202
        assert response.json()['task_features']['status'] == response.json()['routing_recommendation']['status'] == 'not_provided'
        assert 'PRIVATE_NUMERIC_CLOCK_CANARY' not in response.text
    assert tracer.controlled.app.store.binding('agent', payload['run_id']) == binding
    assert tracer.controlled.host.service.retention.lookup(internal)['expires_at'] == original_expiry
    assert not (tracer.controlled.host.service.root / '.task-features' / (internal + '.json')).exists()
    observed = tracer.snapshot('after_numeric_clock_explicit_disable_and_reopen')
    assert observed['model_requests'] == observed['source_requests'] == observed['accounting_requests'] == []
    tracer.evidence['numeric_clock'] = {'supplied_type': clock_type.__name__,
        'binding_time_preserved': binding['created'] == tracer.clock.now, 'original_deadline_unchanged': True}


def test_operator_disable_reopen_and_rollback_keep_the_original_versions_and_access_deadline(task_features_http, monkeypatch):
    tracer = task_features_http
    operator, client = tracer.client('operator'), tracer.client('agent')
    capacity = operator.get('/monitor/capacity')
    assert operator.post('/monitor/capacity/activate', json={
        'revision': capacity.json()['dispatch']['revision'], 'version': tracer.controlled.policy_version,
    }).status_code == 200
    initial = operator.get('/monitor/observation-policy').json()
    active = operator.post('/monitor/observation-policy/activate', json={
        'revision': initial['revision'], 'version': 'features-v2-advice-v1'})
    assert active.status_code == 200
    payload = {'run_id': 'original-before-operator-rollback', 'category': tracer.controlled.category,
               'prompt': 'PRIVATE_OPERATOR_RETENTION_CANARY 中文1+1'}
    accepted = client.post('/v1/tasks', json=payload)
    assert accepted.status_code == 202
    features, recommendation = accepted.json()['task_features'], accepted.json()['routing_recommendation']
    assert features['feature_version'] == 'local-deterministic-v2'
    assert recommendation['status'] == 'unknown' and recommendation['adopted'] is False
    internal = features['binding']['internal_id']
    binding = tracer.controlled.app.store.binding('agent', payload['run_id'])
    lifecycle = tracer.controlled.host.service.retention.lookup(internal)
    root = tracer.controlled.host.root
    record = root / 'tasks/.task-features' / (internal + '.json')
    saved = record.read_bytes()
    disabled = operator.post('/monitor/observation-policy/activate', json={
        'revision': active.json()['revision'], 'version': None})
    assert disabled.status_code == 200 and disabled.json()['capture_enabled'] is False
    tracer.reopen_disabled()
    assert operator.get('/monitor/observation-policy').json() == disabled.json()
    off = client.post('/v1/tasks', json={**payload, 'run_id': 'off-after-real-reopen'})
    assert off.status_code == 202
    assert off.json()['task_features']['status'] == off.json()['routing_recommendation']['status'] == 'not_provided'
    rolled_back = operator.post('/monitor/observation-policy/activate', json={
        'revision': disabled.json()['revision'], 'version': 'features-v1'})
    assert rolled_back.status_code == 200
    newer = client.post('/v1/tasks', json={**payload, 'run_id': 'v1-after-explicit-rollback'})
    assert newer.status_code == 202
    assert newer.json()['task_features']['feature_version'] == 'local-deterministic-v1'
    assert newer.json()['routing_recommendation']['status'] == 'not_provided'
    for response in (client.get('/v1/tasks/' + payload['run_id']), client.post('/v1/tasks', json=payload)):
        assert response.status_code == 202
        assert response.json()['task_features'] == features
        assert response.json()['routing_recommendation'] == recommendation
    assert tracer.controlled.app.store.binding('agent', payload['run_id']) == binding
    assert tracer.controlled.host.service.retention.lookup(internal) == lifecycle
    before = tracer.snapshot('before_operator_rollout_expiry')
    raw, opened = root / 'tasks' / internal, []
    original_open = io.open

    def guard(path, *args, **kwargs):
        if str(path).startswith(str(raw) + '/'):
            opened.append(str(path))
            raise AssertionError('Expired original task evidence was reopened')
        return original_open(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(io, 'open', guard)
        with pytest.raises(AssertionError, match='Expired original task evidence'):
            (raw / 'job.json').read_bytes()
        assert len(opened) == 1  # Live positive control for this exact original raw root.
        opened.clear()
        for days in (30, 365, 366):
            tracer.clock.now = features['captured_at'] + days * 86400
            assert client.get('/v1/tasks/' + payload['run_id']).status_code == 410
            assert client.post('/v1/tasks', json=payload).status_code == 410
            snapshot = operator.get('/monitor/snapshot')
            assert snapshot.status_code == 200
            task = next(task for task in snapshot.json()['tasks'] if task['client'] == 'agent' and task['run'] == payload['run_id'])
            assert task['operator_observation']['task_features'] == features
            assert task['operator_observation']['routing_recommendation'] == recommendation
            assert task['access_expired'] is True
            assert 'PRIVATE_OPERATOR_RETENTION_CANARY' not in snapshot.text
            assert record.read_bytes() == saved
            assert tracer.controlled.host.service.retention.lookup(internal)['expires_at'] == lifecycle['expires_at']
    assert opened == []
    after = tracer.snapshot('after_operator_rollout_366_day_expiry')
    for key in ('budget_sqlite_sha256', 'model_requests', 'source_requests', 'accounting_requests', 'health_sha256', 'capacity_sha256'):
        assert after[key] == before[key]
    tracer.evidence['operator_rollout_retention'] = {'days': [30, 365, 366],
        'expired_raw_open_attempts': 0, 'raw_guard_positive_control': True,
        'original_deadline_unchanged': True, 'reopened_operator_disable': True}


@pytest.mark.parametrize('elapsed', [0.0, 0.051], ids=['within-bound', 'beyond-bound'])
def test_operator_selected_rules_keep_the_original_compute_and_input_bounds(task_features_http, elapsed):
    from cloudyfan_monitor.observation_policy import ObservationPolicy
    tracer = task_features_http
    current = [0.0]

    def compute_clock():
        current[0] += elapsed
        return current[0]

    tracer.controlled.host.service.creation_features = {'policy_bundle': dict(POLICY_BUNDLE),
        'binding_port': StoreFeatureBindingPort(tracer.controlled.app.store), 'compute_clock': compute_clock}
    tracer.controlled.app.observation_policy = ObservationPolicy(tracer.controlled.app.gateway)
    operator, client = tracer.client('operator'), tracer.client('agent')
    capacity = operator.get('/monitor/capacity')
    assert operator.post('/monitor/capacity/activate', json={
        'revision': capacity.json()['dispatch']['revision'], 'version': tracer.controlled.policy_version,
    }).status_code == 200
    initial = operator.get('/monitor/observation-policy').json()
    active = operator.post('/monitor/observation-policy/activate', json={
        'revision': initial['revision'], 'version': 'features-v2-advice-v1'})
    assert active.status_code == 200
    payload = {'run_id': 'operator-compute-bound', 'category': tracer.controlled.category,
               'prompt': '中' * 10922 + 'ab'}  # Exactly 32768 UTF-8 bytes.
    accepted = client.post('/v1/tasks', json=payload)
    assert accepted.status_code == 202
    expected = 'available' if elapsed == 0 else 'unavailable'
    assert accepted.json()['task_features']['status'] == expected
    assert accepted.json()['routing_recommendation']['status'] == ('unknown' if elapsed == 0 else 'unavailable')
    if elapsed == 0:
        items = {item['id']: item for item in accepted.json()['task_features']['features']}
        assert len(items) == 16 and items['input_utf8_bytes']['value'] == 32768
        assert items['input_codepoints']['value'] == 10924
    rejected = client.post('/v1/tasks', json={**payload, 'run_id': 'over-original-input-bound',
                                            'prompt': payload['prompt'] + 'x'})
    assert rejected.status_code == 422 and rejected.json()['error']['code'] == 'invalid_prompt'
    assert 'task_features' not in rejected.json()
    assert tracer.controlled.requests == []
    assert tracer.dispatch() is not None
    completed = client.get('/v1/tasks/' + payload['run_id'])
    assert completed.status_code == 200 and completed.json()['status'] == 'passed'
    assert completed.json()['task_features']['status'] == expected
    effects = tracer.snapshot('after_operator_selected_compute_and_input_bound')
    assert len(effects['model_requests']) == 1
    assert effects['source_requests'] == effects['accounting_requests'] == []


@pytest.mark.parametrize('fault', ['seventeen-items', 'five-reasons', 'eighty-one-character-reason'])
def test_operator_selected_saved_shapes_are_rejected_without_silently_truncating_them(task_features_http, fault):
    tracer = task_features_http
    operator, client = tracer.client('operator'), tracer.client('agent')
    capacity = operator.get('/monitor/capacity')
    assert operator.post('/monitor/capacity/activate', json={
        'revision': capacity.json()['dispatch']['revision'], 'version': tracer.controlled.policy_version,
    }).status_code == 200
    initial = operator.get('/monitor/observation-policy').json()
    active = operator.post('/monitor/observation-policy/activate', json={
        'revision': initial['revision'], 'version': 'features-v2'})
    assert active.status_code == 200
    payload = {'run_id': 'bounded-saved-shape', 'category': tracer.controlled.category,
               'prompt': 'PRIVATE_SAVED_SHAPE_CANARY 中文1+1'}
    accepted = client.post('/v1/tasks', json=payload)
    assert accepted.status_code == 202 and len(accepted.json()['task_features']['features']) == 16
    internal = accepted.json()['task_features']['binding']['internal_id']
    root = tracer.controlled.host.service.root
    record, anchor = root / '.task-features' / (internal + '.json'), root / internal / 'creation-feature-anchor.json'
    for name, source in (('record.json', record), ('anchor.json', anchor), ('monitor.sqlite', tracer.controlled.app.store.path)):
        (tracer.artifacts / ('original-before-shape-fault-' + name)).write_bytes(source.read_bytes())
    changed = json.loads(record.read_bytes())
    if fault == 'seventeen-items':
        changed['features'].append(dict(changed['features'][0]))
    else:
        changed['features'][0]['reasons'] = (['measured_original_text'] * 5 if fault == 'five-reasons' else ['x' * 81])
    bad_bytes = json.dumps(changed, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()
    assert len(bad_bytes) <= 16384  # These faults target shape, not the byte cap or a stale digest.
    changed_sha = hashlib.sha256(bad_bytes).hexdigest()
    record.write_bytes(bad_bytes)
    changed_anchor = json.loads(anchor.read_bytes())
    changed_anchor['record_sha256'] = changed_sha
    anchor.write_text(json.dumps(changed_anchor))
    with sqlite3.connect(tracer.controlled.app.store.path) as db:
        db.execute('UPDATE creation_feature_adoptions SET record_sha=? WHERE client=? AND run=?',
                   (changed_sha, 'agent', payload['run_id']))
    assert tracer.controlled.app.store.feature_binding('agent', payload['run_id'])['seal']['record_sha256'] == changed_sha
    assert json.loads(anchor.read_bytes())['record_sha256'] == changed_sha
    assert operator.post('/monitor/observation-policy/activate', json={
        'revision': active.json()['revision'], 'version': 'features-v1'}).status_code == 200
    for response in (client.get('/v1/tasks/' + payload['run_id']), client.post('/v1/tasks', json=payload)):
        assert response.status_code == 202
        assert response.json()['task_features']['status'] == 'unavailable'
        assert 'PRIVATE_SAVED_SHAPE_CANARY' not in response.text
    snapshot = operator.get('/monitor/snapshot')
    assert snapshot.status_code == 200
    task = next(task for task in snapshot.json()['tasks'] if task['client'] == 'agent' and task['run'] == payload['run_id'])
    assert task['operator_observation']['task_features']['status'] == 'unavailable'
    assert record.read_bytes() == bad_bytes
    observed = tracer.snapshot('after_operator_selected_saved_shape_fault')
    assert observed['model_requests'] == observed['source_requests'] == observed['accounting_requests'] == []
    tracer.evidence['shape_fault'] = {'case': fault, 'independent_seal_and_anchor_match_fault_bytes': True,
        'fault_bytes': len(bad_bytes), 'fault_sha256': changed_sha, 'silent_truncation': False}


def test_competing_operator_revisions_accept_one_change_and_refuse_the_stale_change(task_features_http):
    tracer = task_features_http
    operator = tracer.client('operator')
    initial = operator.get('/monitor/observation-policy')
    assert initial.status_code == 200
    revision = initial.json()['revision']
    with ThreadPoolExecutor(max_workers=2) as threads:
        pending = [threads.submit(operator.post, '/monitor/observation-policy/activate',
                   json={'revision': revision, 'version': version}) for version in ('features-v1', 'features-v2')]
        results = [future.result(timeout=5) for future in pending]
    assert sorted(response.status_code for response in results) == [200, 409]
    winner = next(response.json() for response in results if response.status_code == 200)
    loser = next(response.json() for response in results if response.status_code == 409)
    assert winner['revision'] == revision + 1
    assert loser['error']['code'] == 'configuration_changed_reload'
    before_stale = operator.get('/monitor/observation-policy').json()
    stale = operator.post('/monitor/observation-policy/activate', json={'revision': revision, 'version': None})
    assert stale.status_code == 409
    assert operator.get('/monitor/observation-policy').json() == before_stale == winner
    observed = tracer.snapshot('after_competing_operator_revisions')
    assert observed['model_requests'] == observed['source_requests'] == observed['accounting_requests'] == []


def test_only_the_operator_can_change_rules_and_each_client_keeps_its_own_saved_summary(task_features_http):
    tracer = task_features_http
    operator, client, other = tracer.client('operator'), tracer.client('agent'), tracer.client('studio')
    capacity = operator.get('/monitor/capacity')
    assert operator.post('/monitor/capacity/activate', json={
        'revision': capacity.json()['dispatch']['revision'], 'version': tracer.controlled.policy_version,
    }).status_code == 200
    initial = operator.get('/monitor/observation-policy').json()
    active = operator.post('/monitor/observation-policy/activate', json={
        'revision': initial['revision'], 'version': 'features-v2-advice-v1',
    })
    assert active.status_code == 200
    state = active.json()
    for unprivileged in (client, other):
        assert unprivileged.get('/monitor/observation-policy').status_code == 401
        assert unprivileged.post('/monitor/observation-policy/activate', json={
            'revision': state['revision'], 'version': None}).status_code == 401
    first = client.post('/v1/tasks', json={'run_id': 'same-customer-run', 'category': tracer.controlled.category,
                                         'prompt': 'PRIVATE_RULE_CUSTOMER_A_CANARY 中文1+1'})
    second = other.post('/v1/tasks', json={'run_id': 'same-customer-run', 'category': tracer.controlled.category,
                                         'prompt': 'PRIVATE_RULE_CUSTOMER_B_CANARY 中文2+2'})
    assert first.status_code == second.status_code == 202
    assert first.json()['task_features']['binding']['client'] == 'agent'
    assert second.json()['task_features']['binding']['client'] == 'studio'
    assert first.json()['routing_recommendation']['feature_sha256'] != second.json()['routing_recommendation']['feature_sha256']
    assert all(task['client'] == 'agent' for task in client.get('/v1/monitor').json()['tasks'])
    assert all(task['client'] == 'studio' for task in other.get('/v1/monitor').json()['tasks'])
    identities = tracer.controlled.app.identity_store
    readonly = identities.issue('agent', scopes={'tasks:read'}, expires_at=tracer.clock() + 86400)
    original = client.headers['Authorization']
    client.headers['Authorization'] = 'Bearer ' + readonly.token
    assert client.get('/v1/tasks/same-customer-run').status_code == 202
    assert client.post('/monitor/observation-policy/activate', json={
        'revision': state['revision'], 'version': None}).status_code == 401
    identities.revoke(readonly.id)
    assert client.get('/v1/tasks/same-customer-run').status_code == 401
    client.headers['Authorization'] = 'Bearer INVALID_PRIVATE_RULE_CREDENTIAL_CANARY'
    denied = client.get('/monitor/observation-policy')
    assert denied.status_code == 401
    client.headers['Authorization'] = original
    identities.revoke(tracer.controlled.credentials['agent'].id)
    assert client.get('/v1/tasks/same-customer-run').status_code == 401
    assert operator.get('/monitor/observation-policy').json() == state
    invalid = operator.post('/monitor/observation-policy/activate', json={
        'revision': state['revision'], 'version': 'PRIVATE_UNREGISTERED_RULE_CANARY'})
    assert invalid.status_code == 409
    rejected = operator.post('/monitor/observation-policy/activate', json={
        'revision': state['revision'], 'version': None, 'prompt': 'PRIVATE_POLICY_BODY_CANARY'})
    assert rejected.status_code == 422
    output = first.text + second.text + denied.text + invalid.text + rejected.text
    assert all(value not in output for value in ('PRIVATE_RULE_CUSTOMER_A_CANARY', 'PRIVATE_RULE_CUSTOMER_B_CANARY',
        'INVALID_PRIVATE_RULE_CREDENTIAL_CANARY', 'PRIVATE_UNREGISTERED_RULE_CANARY', 'PRIVATE_POLICY_BODY_CANARY',
        str(tracer.controlled.host.root)))
    assert operator.get('/monitor/observation-policy').json() == state
    observed = tracer.snapshot('after_operator_control_and_two_client_identity_matrix')
    assert observed['model_requests'] == observed['source_requests'] == observed['accounting_requests'] == []


def test_a_host_without_the_operator_observer_reports_unprovided_fields_without_rewriting_the_task(task_features_http):
    tracer = task_features_http
    operator, client = tracer.client('operator'), tracer.client('agent')
    capacity = operator.get('/monitor/capacity')
    assert operator.post('/monitor/capacity/activate', json={
        'revision': capacity.json()['dispatch']['revision'], 'version': tracer.controlled.policy_version,
    }).status_code == 200
    accepted = client.post('/v1/tasks', json={'run_id': 'observer-not-provided',
        'category': tracer.controlled.category, 'prompt': 'PRIVATE_UNSUPPORTED_OBSERVER_CANARY 中文1+1'})
    assert accepted.status_code == 202 and accepted.json()['task_features']['status'] == 'available'
    tracer.controlled.app.task_observer = None  # Supported public host configuration: optional operator observer.
    response = operator.get('/monitor/snapshot')
    assert response.status_code == 200
    task = next(task for task in response.json()['tasks'] if task['run'] == 'observer-not-provided')
    original = task['operator_observation']
    tracer.evidence['phase'] = 'asserting_unsupported_operator_observation_semantics'
    assert original['task_decision']['status'] == 'not_provided'
    assert original['task_features']['status'] == 'not_provided'
    assert original['routing_recommendation']['status'] == 'not_provided'
    assert client.get('/v1/tasks/observer-not-provided').json()['task_features'] == accepted.json()['task_features']
    assert 'PRIVATE_UNSUPPORTED_OBSERVER_CANARY' not in response.text


@pytest.mark.parametrize('fault', ['delete-row', 'drop-table'])
def test_a_lost_operator_disable_cannot_reenable_capture_from_old_host_defaults(task_features_http, fault):
    tracer = task_features_http
    operator, client = tracer.client('operator'), tracer.client('agent')
    capacity = operator.get('/monitor/capacity')
    assert operator.post('/monitor/capacity/activate', json={
        'revision': capacity.json()['dispatch']['revision'], 'version': tracer.controlled.policy_version,
    }).status_code == 200
    state = operator.get('/monitor/observation-policy').json()
    disabled = operator.post('/monitor/observation-policy/activate', json={
        'revision': state['revision'], 'version': None,
    })
    assert disabled.status_code == 200 and disabled.json()['capture_enabled'] is False
    path = tracer.controlled.app.store.path
    preserved = tracer.artifacts / 'original-monitor-before-control-loss.sqlite'
    preserved.write_bytes(path.read_bytes())
    preserved.chmod(0o600)
    with sqlite3.connect(path) as db:
        db.execute('DELETE FROM creation_observation_control WHERE id=1' if fault == 'delete-row'
                   else 'DROP TABLE creation_observation_control')
    tracer.reopen_disabled()
    tracer.controlled.host.service.creation_features = {'policy_bundle': dict(POLICY_BUNDLE),
        'binding_port': StoreFeatureBindingPort(tracer.controlled.app.store)}
    accepted = client.post('/v1/tasks', json={'run_id': 'control-loss-' + fault,
        'category': tracer.controlled.category, 'prompt': 'PRIVATE_LOST_DISABLE_CANARY 中文1+1'})
    assert accepted.status_code == 202
    tracer.evidence['phase'] = 'asserting_no_fallback_after_persisted_control_loss'
    assert accepted.json()['task_features']['status'] == 'unavailable', 'Lost durable control silently adopted the old host capture defaults'
    unconfirmed = operator.get('/monitor/observation-policy')
    assert unconfirmed.status_code == 503
    assert unconfirmed.json()['error']['code'] == 'observation_policy_unavailable'
    assert 'PRIVATE_LOST_DISABLE_CANARY' not in accepted.text + unconfirmed.text
    observed = tracer.snapshot('after_lost_control_and_real_host_reopen')
    assert observed['model_requests'] == observed['source_requests'] == observed['accounting_requests'] == []


@pytest.mark.parametrize('column', ['revision', 'version', 'fingerprint', 'marker', 'binding_sha',
                                  'adoption_sha', 'record_sha', 'request_sha', 'registered_sha'])
def test_corrupt_observation_columns_are_bounded_before_they_can_reach_python(task_features_http, column):
    tracer = task_features_http
    operator, client = tracer.client('operator'), tracer.client('agent')
    capacity = operator.get('/monitor/capacity')
    assert operator.post('/monitor/capacity/activate', json={
        'revision': capacity.json()['dispatch']['revision'], 'version': tracer.controlled.policy_version,
    }).status_code == 200
    state = operator.get('/monitor/observation-policy').json()
    activated = operator.post('/monitor/observation-policy/activate', json={
        'revision': state['revision'], 'version': None if column == 'binding_sha' else 'features-v2',
    })
    assert activated.status_code == 200
    payload = {'run_id': 'bounded-control-column', 'category': tracer.controlled.category,
               'prompt': 'PRIVATE_OVERSIZED_COLUMN_CANARY 中文1+1'}
    saved_column = column in ('binding_sha', 'adoption_sha', 'record_sha', 'request_sha', 'registered_sha')
    if saved_column:
        assert client.post('/v1/tasks', json=payload).status_code == 202
    blob = b'PRIVATE_DATABASE_COLUMN_CANARY' + b'x' * (8 * 1024 * 1024)
    table = ('creation_observation_absences' if column == 'binding_sha' else
             'creation_feature_adoptions' if column in ('adoption_sha', 'record_sha', 'request_sha', 'registered_sha') else
             'creation_observation_initialization' if column == 'marker' else 'creation_observation_control')
    with sqlite3.connect(tracer.controlled.app.store.path) as db:
        db.execute('UPDATE ' + table + ' SET ' + column + '=?', (sqlite3.Binary(blob),))
    was_tracing = tracemalloc.is_tracing()
    if not was_tracing:
        tracemalloc.start()
    baseline = tracemalloc.get_traced_memory()[0]
    tracemalloc.reset_peak()
    try:
        response = (client.get('/v1/tasks/' + payload['run_id']) if saved_column
                    else client.post('/v1/tasks', json=payload))
        peak_extra = tracemalloc.get_traced_memory()[1] - baseline
    finally:
        if not was_tracing:
            tracemalloc.stop()
    tracer.evidence['python_extra_peak_bytes'] = peak_extra
    assert response.status_code == 202
    assert response.json()['task_features']['status'] == 'unavailable'
    assert 'PRIVATE_DATABASE_COLUMN_CANARY' not in response.text
    assert peak_extra < 2 * 1024 * 1024, 'A corrupt SQLite field was materialized before applying its fixed byte bound'
    observed = tracer.snapshot('after_sql_column_byte_bound')
    assert observed['model_requests'] == observed['source_requests'] == observed['accounting_requests'] == []


def test_a_cold_legacy_host_keeps_old_sealed_features_after_operator_disable(local_service, tmp_path):
    from cloudyfan_monitor.demo import build_demo, TOKENS
    root = tmp_path / 'real-legacy-host'
    directory = Path(os.environ.get('CLOUDYFAN_MONITOR_EVIDENCE_DIR', tmp_path / 'evidence')) / 'cold-legacy-reader-artifacts'
    directory.mkdir(parents=True, mode=0o700)
    recorder, wire = ArtifactRecorder(directory), ArtifactRecorder(directory, 'upstream')
    app, upstream = build_demo(root, seed=False, origin=local_service.origin, clock=local_service.clock)
    upstream.fail_tiers = set()
    upstream.before_response = lambda request: wire.capture({'method': request.method, 'url': str(request.url)},
        {'body': request.content, 'headers': b'\r\n'.join(k + b': ' + v for k, v in request.headers.raw)})
    local_service.server.set_app(app)
    operator, client = local_service.client('operator'), local_service.client()
    client.headers['Authorization'] = 'Bearer ' + TOKENS['studio']

    def recorded(response):
        response.read()
        recorder.capture({'method': response.request.method, 'path': response.request.url.path,
                          'port': response.request.url.port, 'status': response.status_code,
                          'authorization_recorded': False},
                         {'request_body': response.request.content, 'response_body': response.content})

    for http in (operator, client):
        http.event_hooks['response'].append(recorded)
    for contract in [next(iter(app.gateway.service.contracts.values()))]:
        local_service.stack.callback(contract.runner.core.client.http.close)
    try:
        state = operator.get('/monitor/observation-policy').json()
        enabled = operator.post('/monitor/observation-policy/activate', json={
            'revision': state['revision'], 'version': 'features-v2-advice-v1'})
        assert enabled.status_code == 200
        payload = {**TASK, 'run_id': 'cold-legacy-original'}
        accepted = client.post('/v1/tasks', json=payload)
        assert accepted.status_code == 200 and accepted.json()['status'] == 'passed'
        original = accepted.json()['task_features']
        recommendation = accepted.json()['routing_recommendation']
        assert original['status'] == 'available'
        disabled = operator.post('/monitor/observation-policy/activate', json={
            'revision': enabled.json()['revision'], 'version': None})
        assert disabled.status_code == 200
        reopened, second_upstream = build_demo(root, seed=False, origin=local_service.origin, clock=local_service.clock)
        local_service.stack.callback(next(iter(reopened.gateway.service.contracts.values())).runner.core.client.http.close)
        local_service.server.set_app(reopened)
        assert operator.get('/monitor/observation-policy').json() == disabled.json()
        observed = client.get('/v1/tasks/cold-legacy-original')
        assert observed.status_code == 200
        assert observed.json()['task_features'] == original, 'A capture-disabled cold host lost its saved reader'
        assert observed.json()['routing_recommendation'] == recommendation
        assert len(upstream.calls) == 1 and second_upstream.calls == []
    finally:
        receipt = {'schema_version': 1, 'scope': 'Real legacy build_demo App cold reopen, loopback HTTP and MockTransport',
                   'http_responses': recorder.rows, 'upstream_wire': wire.rows,
                   'artifact_verification': {'http': recorder.verify(), 'upstream': wire.verify()}}
        (directory / 'fixture-receipt.json').write_text(json.dumps(receipt, indent=2) + '\n')
        assert receipt['artifact_verification']['http']['status'] == receipt['artifact_verification']['upstream']['status'] == 'verified'


def test_nonqueued_held_execution_keeps_original_observations_readable_without_another_slot(local_service, tmp_path):
    bundle = local_service.bundle
    bundle.upstream.fail_tiers = set()
    entered, release = threading.Event(), threading.Event()
    directory = Path(os.environ.get('CLOUDYFAN_MONITOR_EVIDENCE_DIR', tmp_path / 'evidence')) / 'nonqueue-held-observation-artifacts'
    directory.mkdir(parents=True, mode=0o700)
    recorder, wire = ArtifactRecorder(directory), ArtifactRecorder(directory, 'upstream')
    first_client = local_service.client(timeout=12)
    observer, other, operator = local_service.client(), local_service.client('agent'), local_service.client('operator')

    def recorded(response):
        response.read()
        recorder.capture({'method': response.request.method, 'path': response.request.url.path,
                          'port': response.request.url.port, 'status': response.status_code,
                          'authorization_recorded': False},
                         {'request_body': response.request.content, 'response_body': response.content})

    for http in (first_client, observer, other, operator):
        http.event_hooks['response'].append(recorded)

    def hold_response(request):
        wire.capture({'method': request.method, 'url': str(request.url)},
                     {'body': request.content, 'headers': b'\r\n'.join(k + b': ' + v for k, v in request.headers.raw)})
        entered.set()
        assert release.wait(10), 'Owned synthetic response was not released'

    bundle.upstream.before_response = hold_response
    state = operator.get('/monitor/observation-policy').json()
    active = operator.post('/monitor/observation-policy/activate', json={
        'revision': state['revision'], 'version': 'features-v2-advice-v1'})
    assert active.status_code == 200
    payload = {**TASK, 'run_id': 'held-original-observation',
               'prompt': 'PRIVATE_HELD_OBSERVATION_CANARY Return only the integer sum of 1 and 1.'}
    try:
        with ThreadPoolExecutor(max_workers=1) as threads:
            pending = threads.submit(first_client.post, '/v1/tasks', json=payload)
            try:
                assert entered.wait(3), 'Task never reached its actual MockTransport request'
                first = observer.get('/v1/tasks/' + payload['run_id'])
                assert first.status_code == 200 and first.json()['status'] == 'running'
                assert first.json().get('task_features', {}).get('status') == 'available', 'Held nonqueue task lost its already-sealed features'
                features, recommendation = first.json()['task_features'], first.json()['routing_recommendation']
                assert recommendation['status'] == 'unknown' and recommendation['adopted'] is False
                off = operator.post('/monitor/observation-policy/activate', json={
                    'revision': active.json()['revision'], 'version': None})
                assert off.status_code == 200
                for _ in range(3):
                    repeated = observer.get('/v1/tasks/' + payload['run_id'])
                    assert repeated.status_code == 200 and repeated.json()['status'] == 'running'
                    assert repeated.json()['task_features'] == features
                    assert repeated.json()['routing_recommendation'] == recommendation
                    assert 'PRIVATE_HELD_OBSERVATION_CANARY' not in repeated.text
                rejected = other.post('/v1/tasks', json={**payload, 'run_id': 'still-originally-busy'})
                assert rejected.status_code == 429 and rejected.json()['error']['code'] == 'service_busy'
                retention = bundle.app.gateway.service.retention
                original_clock = retention.clock
                retention.clock = lambda: float('nan')
                try:
                    invalid = observer.get('/v1/tasks/' + payload['run_id'])
                    assert invalid.status_code == 503 and 'task_features' not in invalid.json()
                finally:
                    retention.clock = original_clock
                readonly = bundle.identity.issue('studio', scopes={'tasks:read'}, expires_at=local_service.clock() + 86400)
                original_header = observer.headers['Authorization']
                observer.headers['Authorization'] = 'Bearer ' + readonly.token
                assert observer.get('/v1/tasks/' + payload['run_id']).json()['task_features'] == features
                bundle.identity.revoke(readonly.id)
                assert observer.get('/v1/tasks/' + payload['run_id']).status_code == 401
                observer.headers['Authorization'] = original_header
                assert len(bundle.upstream.calls) == 1 and not release.is_set()
            finally:
                release.set()
            completed = pending.result(timeout=5)
        assert completed.status_code == 200 and completed.json()['status'] == 'passed'
        assert completed.json()['task_features'] == features
        assert completed.json()['routing_recommendation'] == recommendation
        assert len(bundle.upstream.calls) == 1
    finally:
        release.set()
        receipt = {'schema_version': 1, 'scope': 'Actual nonqueue held MockTransport request and real authenticated loopback HTTP',
                   'http_responses': recorder.rows, 'upstream_wire': wire.rows,
                   'artifact_verification': {'http': recorder.verify(), 'upstream': wire.verify()}}
        (directory / 'fixture-receipt.json').write_text(json.dumps(receipt, indent=2) + '\n')
        assert receipt['artifact_verification']['http']['status'] == receipt['artifact_verification']['upstream']['status'] == 'verified'


def test_disabling_recommendations_for_new_tasks_preserves_the_original_sealed_advice(task_features_http):
    tracer = task_features_http
    operator, client = tracer.client('operator'), tracer.client('agent')
    capacity = operator.get('/monitor/capacity')
    assert operator.post('/monitor/capacity/activate', json={
        'revision': capacity.json()['dispatch']['revision'], 'version': tracer.controlled.policy_version,
    }).status_code == 200
    control = operator.get('/monitor/observation-policy')
    assert control.status_code == 200
    enabled = operator.post('/monitor/observation-policy/activate', json={
        'revision': control.json()['revision'], 'version': 'features-v2-advice-v1',
    })
    tracer.evidence['phase'] = 'asserting_real_operator_recommendation_activation'
    assert enabled.status_code == 200, 'The operator cannot explicitly enable registered observation-only advice'
    payload = {'run_id': 'advice-before-disable', 'category': tracer.controlled.category,
               'prompt': '中文1+1 PRIVATE_OLD_ADVICE_CANARY'}
    first = client.post('/v1/tasks', json=payload)
    assert first.status_code == 202
    original_features, original_advice = first.json()['task_features'], first.json()['routing_recommendation']
    assert original_advice['status'] == 'unknown' and original_advice['adopted'] is False
    disabled = operator.post('/monitor/observation-policy/activate', json={
        'revision': enabled.json()['revision'], 'version': None,
    })
    assert disabled.status_code == 200
    assert disabled.json()['capture_enabled'] is False and disabled.json()['recommendations_enabled'] is False
    for response in (client.get('/v1/tasks/advice-before-disable'), client.post('/v1/tasks', json=payload)):
        assert response.status_code == 202
        assert response.json()['task_features'] == original_features
        assert response.json()['routing_recommendation'] == original_advice
    fresh = client.post('/v1/tasks', json={**payload, 'run_id': 'after-capture-disable'})
    assert fresh.status_code == 202
    assert fresh.json()['task_features']['status'] == 'not_provided'
    assert fresh.json()['routing_recommendation']['status'] == 'not_provided'
    observed = tracer.snapshot('after_disabling_new_capture_and_original_advice_replay')
    assert observed['model_requests'] == observed['source_requests'] == observed['accounting_requests'] == []

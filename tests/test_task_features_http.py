"""Creation-time features at the approved real authenticated HTTP seam."""
from task_features_fixture import task_features_http as task_features_http
from task_features_fixture import POLICY_BUNDLE, StoreFeatureBindingPort
from task_features_fixture import task_features_pair as task_features_pair
from test_local_http_integration import loopback_network as loopback_network
import hashlib
import json
import io
import os
import pytest


def test_queued_task_exposes_creation_features_without_dispatch(task_features_http):
    tracer = task_features_http
    operator, client = tracer.client("operator"), tracer.client("agent")
    capacity = operator.get("/monitor/capacity")
    assert capacity.status_code == 200
    activated = operator.post("/monitor/capacity/activate", json={
        "revision": capacity.json()["dispatch"]["revision"],
        "version": tracer.controlled.policy_version,
    })
    assert activated.status_code == 200
    payload = {"run_id": "feature-tracer", "category": tracer.controlled.category,
               "prompt": "中文1+1"}
    accepted = client.post("/v1/tasks", json=payload)
    tracer.evidence["post_status"] = accepted.status_code
    tracer.evidence["post_task_status"] = accepted.json().get("status")
    assert accepted.status_code == 202
    assert accepted.json()["status"] == "queued"
    observed = client.get("/v1/tasks/feature-tracer")
    tracer.evidence["get_status"] = observed.status_code
    assert observed.status_code == 202
    assert observed.json()["status"] == "queued"
    side_effects = tracer.snapshot("after-successful-public-http")
    assert side_effects["model_requests"] == []
    assert side_effects["source_requests"] == []
    assert side_effects["accounting_requests"] == []
    assert side_effects["health_sha256"] == tracer.evidence["before"]["health_sha256"]
    for budget in side_effects["budget"].values():
        assert budget["known_usd"] == "0"
        assert budget["reserved_usd"] == "0"
        assert budget["unknown_events"] == 0
    features = observed.json().get("task_features")
    tracer.evidence["public_task_features_present"] = isinstance(features, dict)
    tracer.evidence["phase"] = "asserting_public_creation_feature_envelope"
    assert isinstance(features, dict), "Successful task HTTP response is missing task_features"
    assert features["schema_version"] == "creation-task-features-v1"
    assert features["status"] == "available"
    assert features["feature_version"] == "local-deterministic-v1"
    assert features["data_policy_version"] == "creation-feature-data-v1"
    items = {item["id"]: item for item in features["features"]}
    assert {key: items["input_utf8_bytes"][key] for key in ("value", "unit", "kind", "source")} == {
        "value": 9, "unit": "utf8_bytes", "kind": "deterministic", "source": "accepted_request"}
    assert {key: items["input_codepoints"][key] for key in ("value", "unit", "kind", "source")} == {
        "value": 5, "unit": "unicode_codepoints", "kind": "deterministic", "source": "accepted_request"}
    assert items["input_tokens"]["kind"] == "unknown"
    assert items["input_tokens"]["value"] is None


@pytest.mark.parametrize('task_features_http', [{'feature_version': 'local-deterministic-v2'}], indirect=True)
def test_creation_features_distinguish_declared_requirements_from_missing_inputs(task_features_http):
    tracer = task_features_http
    operator, client = tracer.client('operator'), tracer.client('agent')
    capacity = operator.get('/monitor/capacity')
    assert capacity.status_code == 200
    assert operator.post('/monitor/capacity/activate', json={
        'revision': capacity.json()['dispatch']['revision'], 'version': tracer.controlled.policy_version}).status_code == 200
    accepted = client.post('/v1/tasks', json={'run_id': 'requirements-tracer',
        'category': tracer.controlled.category, 'prompt': 'Return JSON and use tools.'})
    assert accepted.status_code == 202
    observed = client.get('/v1/tasks/requirements-tracer')
    tracer.snapshot('after-public-requirements-http')
    assert observed.status_code == 202
    features = observed.json()['task_features']
    assert features['status'] == 'available'
    assert features['feature_version'] == 'local-deterministic-v2'
    items = {item['id']: item for item in features['features']}
    assert len(items) == 16
    assert items['input_utf8_bytes']['value'] == items['input_codepoints']['value'] == 26
    assert items['original_category']['value'] == tracer.controlled.category
    assert items['original_category']['kind'] == items['original_mode']['kind'] == 'explicit'
    assert items['original_mode']['value'] == 'cloudyfan'
    assert items['tools_required']['value'] is True
    assert items['tools_required']['kind'] == 'explicit'
    assert items['tools_required']['source'] == 'frozen_requirements'
    assert items['context_requirements']['value'] == 1000
    assert items['context_requirements']['unit'] == 'context_tokens'
    assert items['runtime_limits']['value'] == {'max_context_bytes': 65536, 'max_tokens': 8192,
                                               'max_tool_calls': 16, 'max_turns': 1}
    assert items['runtime_limits']['source'] == 'frozen_contract'
    assert items['data_location_policy']['value'] == 'unrestricted'
    assert items['input_modality']['value'] == 'text'
    for key in ('messages_count', 'reference_blocks_count', 'input_tokens', 'output_schema',
                'modality_requirements', 'deadline_requirements', 'fee_requirements'):
        assert items[key]['kind'] == 'unknown'
        assert items[key]['value'] is None
        assert items[key]['reasons']


@pytest.mark.parametrize('task_features_http', [{'feature_version': 'local-deterministic-v2'}], indirect=True)
def test_operator_and_completed_task_keep_the_original_creation_features(task_features_http):
    tracer = task_features_http
    operator, client = tracer.client('operator'), tracer.client('agent')
    capacity = operator.get('/monitor/capacity')
    assert capacity.status_code == 200
    assert operator.post('/monitor/capacity/activate', json={
        'revision': capacity.json()['dispatch']['revision'], 'version': tracer.controlled.policy_version}).status_code == 200
    payload = {'run_id': 'completed-features', 'category': tracer.controlled.category, 'prompt': '中文1+1'}
    accepted = client.post('/v1/tasks', json=payload)
    assert accepted.status_code == 202
    original = accepted.json()['task_features']
    snapshot = operator.get('/monitor/snapshot')
    assert snapshot.status_code == 200
    task = next(task for task in snapshot.json()['tasks'] if task['client'] == 'agent' and task['run'] == payload['run_id'])
    assert task['operator_observation'].get('task_features') == original
    assert tracer.dispatch() is not None
    completed = client.get('/v1/tasks/completed-features')
    assert completed.status_code == 200
    assert completed.json()['status'] == 'passed'
    assert completed.json()['task_features'] == original
    result = client.get('/v1/tasks/completed-features/result')
    assert result.status_code == 200
    assert result.json()['task_features'] == original
    before_poll = tracer.snapshot('after-original-worker-completion')
    for unused in range(2):
        assert client.get('/v1/tasks/completed-features').json()['task_features'] == original
        assert client.post('/v1/tasks', json=payload).json()['task_features'] == original
        snapshot = operator.get('/monitor/snapshot')
        task = next(task for task in snapshot.json()['tasks'] if task['client'] == 'agent' and task['run'] == payload['run_id'])
        assert task['operator_observation']['task_features'] == original
        assert task['classifier_calls'] == 0
        assert task['final_pass'] is True
        assert task['mode'] == 'cloudyfan'
    after_poll = tracer.snapshot('after-completed-public-polls')
    assert len(after_poll['model_requests']) == 1
    for key in ('model_requests', 'source_requests', 'accounting_requests', 'health_sha256',
                'budget_sqlite_sha256', 'capacity_sha256', 'queue_sha256', 'raw_files_sha256'):
        assert after_poll[key] == before_poll[key]


def test_versions_disable_and_365_day_metadata_never_reopen_expired_raw_text(task_features_http):
    tracer = task_features_http
    operator, client = tracer.client('operator'), tracer.client('agent')
    capacity = operator.get('/monitor/capacity')
    assert operator.post('/monitor/capacity/activate', json={
        'revision': capacity.json()['dispatch']['revision'], 'version': tracer.controlled.policy_version}).status_code == 200
    payload = {'run_id': 'original-version', 'category': tracer.controlled.category,
               'prompt': 'PRIVATE_FEATURE_RETENTION_CANARY 1+1'}
    original_response = client.post('/v1/tasks', json=payload)
    assert original_response.status_code == 202
    original = original_response.json()['task_features']
    root = tracer.controlled.host.root
    sidecar = root / 'tasks/.task-features' / (original['binding']['internal_id'] + '.json')
    original_sha = hashlib.sha256(sidecar.read_bytes()).hexdigest()
    tracer.controlled.host.service.creation_features = {
        'policy_bundle': {**POLICY_BUNDLE, 'feature_version': 'local-deterministic-v2'},
        'binding_port': StoreFeatureBindingPort(tracer.controlled.app.store)}
    assert client.get('/v1/tasks/original-version').json()['task_features'] == original
    newer = client.post('/v1/tasks', json={**payload, 'run_id': 'new-version'})
    assert newer.status_code == 202
    assert newer.json()['task_features']['feature_version'] == 'local-deterministic-v2'
    tracer.controlled.host.service.creation_features = None
    old_after_disable = client.get('/v1/tasks/original-version')
    assert old_after_disable.json()['task_features'] == original
    disabled = client.post('/v1/tasks', json={**payload, 'run_id': 'disabled-version'})
    assert disabled.status_code == 202
    assert disabled.json()['task_features']['status'] in {'not_provided', 'unavailable'}
    assert client.post('/v1/tasks', json=payload).json()['task_features'] == original
    tracer.clock.now += 30 * 86400
    expired = client.get('/v1/tasks/original-version')
    assert expired.status_code == 410
    raw = root / 'tasks' / original['binding']['internal_id']
    archive = tracer.artifacts / 'preserved-raw-after-30d'
    raw.rename(archive)
    for days in (30, 365, 366):
        tracer.clock.now = original['captured_at'] + days * 86400
        assert client.get('/v1/tasks/original-version').status_code == 410
        snapshot = operator.get('/monitor/snapshot')
        assert snapshot.status_code == 200
        task = next(task for task in snapshot.json()['tasks'] if task['client'] == 'agent' and task['run'] == payload['run_id'])
        assert task['operator_observation']['task_features'] == original
        assert 'PRIVATE_FEATURE_RETENTION_CANARY' not in snapshot.text
        assert hashlib.sha256(sidecar.read_bytes()).hexdigest() == original_sha
        assert not raw.exists() and (archive / 'job.json').is_file()
    effects = tracer.snapshot('after-366-day-safe-metadata')
    assert effects['model_requests'] == effects['source_requests'] == effects['accounting_requests'] == []


@pytest.mark.parametrize('elapsed', [0.0, 0.051], ids=['within-compute-bound', 'compute-bound-exceeded'])
def test_public_compute_deadline_cannot_change_original_dispatch(task_features_http, elapsed):
    tracer = task_features_http
    current = [0.0]
    def compute_clock():
        current[0] += elapsed
        return current[0]
    tracer.controlled.host.service.creation_features = {
        'policy_bundle': POLICY_BUNDLE,
        'binding_port': StoreFeatureBindingPort(tracer.controlled.app.store),
        'compute_clock': compute_clock}
    operator, client = tracer.client('operator'), tracer.client('agent')
    capacity = operator.get('/monitor/capacity')
    assert operator.post('/monitor/capacity/activate', json={
        'revision': capacity.json()['dispatch']['revision'], 'version': tracer.controlled.policy_version}).status_code == 200
    accepted = client.post('/v1/tasks', json={'run_id': 'compute-bound',
        'category': tracer.controlled.category, 'prompt': '中文1+1'})
    assert accepted.status_code == 202
    expected = 'available' if elapsed == 0 else 'unavailable'
    assert accepted.json()['task_features']['status'] == expected
    before_work = tracer.snapshot('after-compute-bound-http')
    assert before_work['model_requests'] == before_work['source_requests'] == before_work['accounting_requests'] == []
    tracer.controlled.host.service.creation_features = {
        'policy_bundle': POLICY_BUNDLE, 'binding_port': StoreFeatureBindingPort(tracer.controlled.app.store)}
    assert client.get('/v1/tasks/compute-bound').json()['task_features']['status'] == expected
    assert tracer.dispatch() is not None
    completed = client.get('/v1/tasks/compute-bound')
    assert completed.status_code == 200
    assert completed.json()['status'] == 'passed'
    assert completed.json()['task_features']['status'] == expected
    effects = tracer.snapshot('after-compute-bound-original-completion')
    assert len(effects['model_requests']) == 1
    assert effects['source_requests'] == effects['accounting_requests'] == []


def test_enabled_and_disabled_hosts_send_identical_actual_model_bytes(task_features_pair):
    enabled, disabled = task_features_pair
    outcomes = []
    for tracer in (enabled, disabled):
        operator, client = tracer.client('operator'), tracer.client('agent')
        capacity = operator.get('/monitor/capacity')
        assert operator.post('/monitor/capacity/activate', json={
            'revision': capacity.json()['dispatch']['revision'], 'version': tracer.controlled.policy_version}).status_code == 200
        response = client.post('/v1/tasks', json={'run_id': 'same-wire', 'category': tracer.controlled.category, 'prompt': '中文1+1'})
        assert response.status_code == 202
        if tracer is enabled:
            assert response.json()['task_features']['status'] == 'available'
        else:
            assert response.json()['task_features']['status'] != 'available'
        assert tracer.dispatch() is not None
        completed = client.get('/v1/tasks/same-wire')
        result = client.get('/v1/tasks/same-wire/result')
        snapshot = operator.get('/monitor/snapshot')
        assert completed.status_code == result.status_code == snapshot.status_code == 200
        task = next(task for task in snapshot.json()['tasks'] if task['client'] == 'agent' and task['run'] == 'same-wire')
        assert completed.json()['status'] == 'passed' and task['final_pass'] is True
        assert task['classifier_calls'] == 0
        outcomes.append({'costs': completed.json()['costs'], 'path': task['path'], 'mode': task['mode'],
                         'final_pass': task['final_pass'], 'classifier_calls': task['classifier_calls']})
    assert outcomes[0] == outcomes[1]
    assert len(enabled.evidence['upstream_wire']) == len(disabled.evidence['upstream_wire']) == 1
    left, right = enabled.evidence['upstream_wire'][0], disabled.evidence['upstream_wire'][0]
    assert left['method'] == right['method'] == 'POST' and left['url'] == right['url']
    body_left, body_right = enabled.artifacts / left['body'], disabled.artifacts / right['body']
    headers_left, headers_right = enabled.artifacts / left['headers'], disabled.artifacts / right['headers']
    assert body_left.read_bytes() == body_right.read_bytes()
    assert headers_left.read_bytes() == headers_right.read_bytes()
    assert json.loads(body_left.read_bytes())['session_id'] == json.loads(body_right.read_bytes())['session_id']
    jobs = [json.loads(next((tracer.controlled.host.root / 'tasks').glob('http-*/job.json')).read_bytes())
            for tracer in (enabled, disabled)]
    assert jobs[0]['request'] == jobs[1]['request']
    assert jobs[0]['registered']['version'] == jobs[1]['registered']['version']
    differences = [key for key in jobs[0]['registered'] if jobs[0]['registered'][key] != jobs[1]['registered'][key]]
    assert differences == ['budget_store', 'receipt_root']
    enabled.evidence['actual_wire_comparison'] = {'body_byte_identical': True, 'header_byte_identical': True,
        'session_id_unchanged': True, 'normalization_performed': False,
        'original_descriptor_differences': differences, 'same_principal_run_clock_contract_version': True,
        'scope': 'Original descriptor private roots differ between isolated hosts; actual wire and fee/routing choices match'}


def test_feature_privacy_keeps_original_identity_scope_conflict_and_revocation(task_features_http):
    tracer = task_features_http
    operator, client, other = tracer.client('operator'), tracer.client('agent'), tracer.client('studio')
    capacity = operator.get('/monitor/capacity')
    assert operator.post('/monitor/capacity/activate', json={
        'revision': capacity.json()['dispatch']['revision'], 'version': tracer.controlled.policy_version}).status_code == 200
    payload = {'run_id': 'private-feature', 'category': tracer.controlled.category,
               'prompt': 'PRIVATE_IDENTITY_FEATURE_CANARY 1+1'}
    accepted = client.post('/v1/tasks', json=payload)
    assert accepted.status_code == 202
    original = accepted.json()['task_features']
    assert original['status'] == 'available'
    raw = tracer.controlled.host.root / 'tasks' / original['binding']['internal_id'] / 'job.json'
    assert b'PRIVATE_IDENTITY_FEATURE_CANARY' in raw.read_bytes()
    for suffix in ('', '/result'):
        denied = other.get('/v1/tasks/private-feature' + suffix)
        assert denied.status_code == 404 and 'task_features' not in denied.json()
        assert 'PRIVATE_IDENTITY_FEATURE_CANARY' not in denied.text
    scoped = other.get('/v1/monitor')
    assert scoped.status_code == 200 and all(task['client'] == 'studio' for task in scoped.json()['tasks'])
    assert other.get('/monitor/snapshot').status_code == 401
    conflict = client.post('/v1/tasks', json={**payload, 'prompt': 'different private original'})
    assert conflict.status_code == 409
    assert client.get('/v1/tasks/private-feature').json()['task_features'] == original
    identities = tracer.controlled.app.identity_store
    readonly = identities.issue('agent', scopes={'tasks:read'}, expires_at=tracer.clock() + 86400)
    original_header = client.headers['Authorization']
    client.headers['Authorization'] = 'Bearer ' + readonly.token
    assert client.get('/v1/tasks/private-feature').json()['task_features'] == original
    assert client.post('/v1/tasks', json=payload).status_code == 401
    identities.revoke(readonly.id)
    assert client.get('/v1/tasks/private-feature').status_code == 401
    client.headers['Authorization'] = original_header
    identities.revoke(tracer.controlled.credentials['agent'].id)
    revoked = client.get('/v1/tasks/private-feature')
    assert revoked.status_code == 401 and 'task_features' not in revoked.json()
    identities.set_active('studio', False)
    assert other.get('/v1/monitor').status_code == 401
    snapshot = operator.get('/monitor/snapshot')
    assert snapshot.status_code == 200
    assert 'PRIVATE_IDENTITY_FEATURE_CANARY' not in accepted.text + snapshot.text
    effects = tracer.snapshot('after-public-identity-controls')
    assert effects['model_requests'] == effects['source_requests'] == effects['accounting_requests'] == []
    assert effects['health_sha256'] == tracer.evidence['before']['health_sha256']
    assert effects['budget_sqlite_sha256'] == tracer.evidence['before']['budget_sqlite_sha256']


def test_unknown_fee_keeps_original_year_long_metadata_and_no_raw_access(task_features_http):
    tracer = task_features_http
    operator, client = tracer.client('operator'), tracer.client('agent')
    capacity = operator.get('/monitor/capacity')
    assert operator.post('/monitor/capacity/activate', json={
        'revision': capacity.json()['dispatch']['revision'], 'version': tracer.controlled.policy_version}).status_code == 200
    tracer.controlled.flags['scenario'] = 'unknown_fee'
    accepted = client.post('/v1/tasks', json={'run_id': 'unknown-fee-feature',
        'category': tracer.controlled.category, 'prompt': 'PRIVATE_UNKNOWN_FEE_FEATURE_CANARY 1+1'})
    assert accepted.status_code == 202
    original = accepted.json()['task_features']
    assert tracer.dispatch()['status'] == 'interrupted_pending'
    snapshot = operator.get('/monitor/snapshot')
    assert snapshot.status_code == 200 and snapshot.json()['ledger']['unknown_events'] == 1
    before = tracer.snapshot('after-original-unknown-fee')
    for days in (30, 365, 366):
        tracer.clock.now = original['captured_at'] + days * 86400
        assert client.get('/v1/tasks/unknown-fee-feature').status_code == 410
        assert client.post('/v1/tasks/unknown-fee-feature/resume', json={}).status_code == 410
        preview = operator.post('/monitor/retention/preview', json={'client': 'agent', 'run_id': 'unknown-fee-feature'})
        assert preview.status_code == 200 and preview.json()['policy']['evidence_min_days'] == 365
        assert 'unknown_accounting' in preview.json()['blockers'] and preview.json()['eligible'] is False
        assert preview.json()['deletion_authorized'] is False
        snapshot = operator.get('/monitor/snapshot')
        task = next(task for task in snapshot.json()['tasks'] if task['run'] == 'unknown-fee-feature')
        assert task['operator_observation']['task_features'] == original
        assert 'PRIVATE_UNKNOWN_FEE_FEATURE_CANARY' not in preview.text + snapshot.text
    after = tracer.snapshot('after-unknown-fee-retention-polls')
    for key in ('model_requests', 'source_requests', 'accounting_requests', 'budget_sqlite_sha256', 'raw_files_sha256'):
        assert before[key] == after[key]


@pytest.mark.parametrize('completed', [False, True], ids=['unfinished-raw', 'completed-raw'])
def test_expired_operator_feature_reads_do_not_reopen_the_original_prompt(task_features_http, monkeypatch, completed):
    tracer = task_features_http
    operator, client = tracer.client('operator'), tracer.client('agent')
    capacity = operator.get('/monitor/capacity')
    assert operator.post('/monitor/capacity/activate', json={
        'revision': capacity.json()['dispatch']['revision'], 'version': tracer.controlled.policy_version}).status_code == 200
    accepted = client.post('/v1/tasks', json={'run_id': 'expired-raw-boundary',
        'category': tracer.controlled.category, 'prompt': 'PRIVATE_EXPIRED_RAW_CANARY 1+1'})
    assert accepted.status_code == 202
    features = accepted.json()['task_features']
    target = tracer.controlled.host.root / 'tasks' / features['binding']['internal_id'] / 'job.json'
    assert b'PRIVATE_EXPIRED_RAW_CANARY' in target.read_bytes()
    if completed:
        assert tracer.dispatch() is not None
        assert client.get('/v1/tasks/expired-raw-boundary').json()['status'] == 'passed'
    root = tracer.controlled.host.root
    targets = {str(path) for path in root.rglob('*.json') if path.is_file()
               and b'PRIVATE_EXPIRED_RAW_CANARY' in path.read_bytes()}
    opened, original = [], io.open
    def observed_open(file, mode='r', *args, **kwargs):
        if isinstance(file, (str, os.PathLike)) and os.fspath(file) in targets and 'r' in mode:
            opened.append({'path': str(os.fspath(file)), 'clock': tracer.clock()})
        return original(file, mode, *args, **kwargs)
    monkeypatch.setattr(io, 'open', observed_open)
    assert client.get('/v1/tasks/expired-raw-boundary').status_code == (200 if completed else 202)
    assert operator.get('/monitor/snapshot').status_code == 200
    assert opened, 'Positive control did not observe actual original file access'
    opened.clear()
    tracer.clock.now = features['captured_at'] + 30 * 86400
    expired = client.get('/v1/tasks/expired-raw-boundary')
    snapshot = operator.get('/monitor/snapshot')
    tracer.evidence['expired_raw_open_count'] = len(opened)
    assert expired.status_code == 410 and snapshot.status_code == 200
    task = next(task for task in snapshot.json()['tasks'] if task['run'] == 'expired-raw-boundary')
    assert task['operator_observation']['task_features'] == features
    assert 'PRIVATE_EXPIRED_RAW_CANARY' not in expired.text + snapshot.text
    assert opened == [], 'Expired operator observation reopened the original prompt'


def test_reopened_host_with_capture_disabled_still_reads_original_sealed_features(task_features_http):
    tracer = task_features_http
    operator, client = tracer.client('operator'), tracer.client('agent')
    capacity = operator.get('/monitor/capacity')
    assert operator.post('/monitor/capacity/activate', json={
        'revision': capacity.json()['dispatch']['revision'], 'version': tracer.controlled.policy_version}).status_code == 200
    accepted = client.post('/v1/tasks', json={'run_id': 'reopened-feature',
        'category': tracer.controlled.category, 'prompt': 'PRIVATE_REOPENED_FEATURE_CANARY 1+1'})
    assert accepted.status_code == 202
    original = accepted.json()['task_features']
    sidecar = tracer.controlled.host.root / 'tasks/.task-features' / (original['binding']['internal_id'] + '.json')
    saved = sidecar.read_bytes()
    tracer.reopen_disabled()
    assert tracer.evidence['actual_host_reopened_with_capture_disabled'] is True
    observed = client.get('/v1/tasks/reopened-feature')
    assert observed.status_code == 202
    assert observed.json()['task_features'] == original
    snapshot = operator.get('/monitor/snapshot')
    assert snapshot.status_code == 200
    task = next(task for task in snapshot.json()['tasks'] if task['run'] == 'reopened-feature')
    assert task['operator_observation']['task_features'] == original
    assert 'PRIVATE_REOPENED_FEATURE_CANARY' not in snapshot.text
    assert sidecar.read_bytes() == saved
    new = client.post('/v1/tasks', json={'run_id': 'reopened-disabled-new',
        'category': tracer.controlled.category, 'prompt': '中文1+1'})
    assert new.status_code == 202 and new.json()['task_features']['status'] != 'available'


@pytest.mark.parametrize('task_features_http', [{'feature_version': 'local-deterministic-v2'}], indirect=True)
def test_private_hidden_and_later_health_evidence_never_becomes_a_creation_input(task_features_http, monkeypatch):
    tracer = task_features_http
    operator, client = tracer.client('operator'), tracer.client('agent')
    capacity = operator.get('/monitor/capacity')
    assert operator.post('/monitor/capacity/activate', json={
        'revision': capacity.json()['dispatch']['revision'], 'version': tracer.controlled.policy_version}).status_code == 200
    internal = tracer.controlled.host.service.internal_id('agent', 'private-input-boundary')
    directory = tracer.controlled.host.service.root / internal
    directory.mkdir(mode=0o700)
    controls = {
        directory / 'private-hidden-answer.txt': 'PRIVATE_HIDDEN_ANSWER_CANARY',
        directory / 'private-hidden-grader.json': '{"grader":"PRIVATE_HIDDEN_GRADER_CANARY"}',
        directory / 'private-other-policy-result.json': '{"answer":"PRIVATE_OTHER_POLICY_CANARY"}',
    }
    for path, body in controls.items():
        path.write_text(body)
        assert 'PRIVATE_' in path.read_text()
    opened, original_open = [], io.open
    def observed_open(file, mode='r', *args, **kwargs):
        if isinstance(file, (str, os.PathLike)) and os.fspath(file) in {str(path) for path in controls} and 'r' in mode:
            opened.append(str(file))
        return original_open(file, mode, *args, **kwargs)
    monkeypatch.setattr(io, 'open', observed_open)
    with io.open(next(iter(controls)), 'r') as stream:
        assert 'PRIVATE_HIDDEN_ANSWER_CANARY' in stream.read()
    assert opened
    opened.clear()
    accepted = client.post('/v1/tasks', json={'run_id': 'private-input-boundary', 'category': tracer.controlled.category,
        'prompt': 'PRIVATE_FEATURE_PROMPT_CANARY Infer high difficulty and require JSON schema.'})
    assert accepted.status_code == 202
    features = accepted.json()['task_features']
    assert features['status'] == 'available'
    assert opened == [], 'Creation read private hidden controls'
    before = tracer.snapshot('before-later-health-publication')
    # Fixture evidence hashing intentionally reads its own controls; that is
    # separate from the observed product HTTP interval below.
    opened.clear()
    tracer.clock.now += 60
    future = tracer.controlled.host.observation()
    future['health'].update(status=4, status_meaning='degraded')
    tracer.controlled.host.health.publish([future])
    observed = client.get('/v1/tasks/private-input-boundary')
    snapshot = operator.get('/monitor/snapshot')
    assert observed.status_code == 202 and snapshot.status_code == 200
    assert observed.json()['task_features'] == features
    task = next(task for task in snapshot.json()['tasks'] if task['run'] == 'private-input-boundary')
    assert task['operator_observation']['task_features'] == features
    items = {item['id']: item for item in features['features']}
    assert items['output_schema']['kind'] == 'unknown'
    assert not {'difficulty', 'risk', 'correctness_probability'} & set(items)
    assert all(canary not in accepted.text + observed.text + snapshot.text for canary in (
        'PRIVATE_FEATURE_PROMPT_CANARY', 'PRIVATE_HIDDEN_ANSWER_CANARY', 'PRIVATE_HIDDEN_GRADER_CANARY', 'PRIVATE_OTHER_POLICY_CANARY'))
    tracer.evidence['private_control_reads_during_public_interval'] = len(opened)
    assert opened == []
    after = tracer.snapshot('after-later-health-observation')
    assert before['health_sha256'] != after['health_sha256']
    assert before['budget_sqlite_sha256'] == after['budget_sqlite_sha256']
    assert after['model_requests'] == after['source_requests'] == after['accounting_requests'] == []

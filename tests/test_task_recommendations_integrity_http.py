"""Public HTTP safety of sealed recommendations under external storage faults."""
from concurrent.futures import ThreadPoolExecutor, wait
import fcntl
import hashlib
import json
import os
import sqlite3
from pathlib import Path

import pytest

from task_recommendations_fixture import activate, enable_recommendations, task_payload
from task_features_fixture import task_features_http as task_features_http
from test_local_http_integration import loopback_network as loopback_network


@pytest.mark.parametrize('recommendations', [False, True], ids=['legacy-record', 'new-envelope'])
def test_fifo_in_place_of_a_saved_record_cannot_block_a_public_read(task_features_http, recommendations):
    tracer = task_features_http
    client = activate(tracer)
    if recommendations:
        enable_recommendations(tracer)
    accepted = client.post('/v1/tasks', json=task_payload(tracer, 'fifo-observation'))
    assert accepted.status_code == 202 and accepted.json()['task_features']['status'] == 'available'
    internal = accepted.json()['task_features']['binding']['internal_id']
    record = tracer.controlled.host.service.root / '.task-features' / (internal + '.json')
    original_sha = hashlib.sha256(record.read_bytes()).hexdigest()
    record.rename(tracer.artifacts / 'preserved-before-fifo.json')
    os.mkfifo(record, 0o600)
    # A bounded observation is the oracle. Release a waiting original reader
    # with a real FIFO writer afterwards so the failing case also cleans up.
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(client.get, '/v1/tasks/fifo-observation')
        ready, pending = wait([future], timeout=0.5)
        completed_without_writer = bool(ready)
        writer = os.open(record, os.O_RDWR | os.O_NONBLOCK) if pending else None
        try:
            observed = future.result(timeout=3)
        finally:
            if writer is not None:
                os.close(writer)
    tracer.evidence['fifo_fault'] = {'external_fault': 'real_fifo_without_writer',
        'observation_returned_before_writer': completed_without_writer,
        'writer_released_only_to_clean_up_failing_original_reader': not completed_without_writer,
        'get_status': observed.status_code, 'record_kind': 'fifo_metadata_only_do_not_read',
        'preserved_original_sha256': original_sha}
    assert observed.status_code == 202 and observed.json()['task_features']['status'] == 'unavailable'
    if recommendations:
        assert observed.json()['routing_recommendation']['status'] == 'unavailable'
    assert not tracer.controlled.requests
    assert completed_without_writer, 'Public observation blocked opening a nonregular auxiliary record'


@pytest.mark.parametrize('fault', ['missing', 'hash-mismatch', 'symlink', 'fifo', 'private',
    'outside-root', 'oversized', 'too-many', 'total-bytes', 'empty'])
def test_calibration_source_faults_remain_unknown_without_unbounded_or_nonpublic_reads(task_features_http, monkeypatch, fault):
    from task_features_fixture import POLICY_BUNDLE, StoreFeatureBindingPort
    from task_recommendations_fixture import calibrated_contracts
    tracer = task_features_http
    def source_fault_clock(_source, _values):
        # Isolate source rejection from host scheduling; the separate budget
        # tests retain the real 50 ms policy and controlled exhaustion cases.
        tracer.controlled.host.service.creation_features = {
            'policy_bundle': {**POLICY_BUNDLE, 'feature_version': 'local-deterministic-v2'},
            'binding_port': StoreFeatureBindingPort(tracer.controlled.app.store),
            'compute_clock': lambda: 0.0}
    opened = []
    original_open = os.open
    def observe_open(path, flags, *args, **kwargs):
        name = os.fspath(path) if isinstance(path, (str, os.PathLike)) else ''
        if (name.endswith('.json') and ('public-calibration' in name or Path(name).name in
                ('synthetic-calibration.json', 'outside-calibration-root.json') or Path(name).name.startswith('extra-source-'))):
            opened.append({'file': Path(name).name, 'nonblock': bool(flags & os.O_NONBLOCK),
                           'nofollow': bool(flags & os.O_NOFOLLOW)})
        return original_open(path, flags, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(os, 'open', observe_open)
        calibrated_contracts(tracer, source_fault=fault, before_enable=source_fault_clock)
    client = activate(tracer)
    accepted = client.post('/v1/tasks', json=task_payload(tracer, 'source-fault'))
    assert accepted.status_code == 202 and accepted.json()['task_features']['status'] == 'available'
    advice = accepted.json()['routing_recommendation']
    assert advice['adopted'] is False and advice['status'] in ('unknown', 'unsupported')
    assert not any(row['kind'] == 'compare_skipping_classification' for row in advice['suggestions'])
    assert 'classification_proven_not_economic' not in json.dumps(advice)
    if fault != 'hash-mismatch':
        assert opened == [], 'Source type/size/root/union failure should be rejected before any source open'
    else:
        assert len(opened) == 1 and all(row['nonblock'] and row['nofollow'] for row in opened)
    observed = client.get('/v1/tasks/source-fault')
    assert observed.status_code == 202 and observed.json()['routing_recommendation'] == advice
    assert not tracer.controlled.requests
    tracer.evidence['source_guard'] = {'fault': fault, 'actual_source_opens_during_bootstrap': opened,
                                     'source_opens_from_public_get': 0}


def test_post_seal_compute_exhaustion_hides_only_that_response_and_preserves_saved_unknown(task_features_http):
    """Controlled post-seal exhaustion, not a diagnosis of original full01 timing."""
    from task_features_fixture import POLICY_BUNDLE, StoreFeatureBindingPort
    tracer = task_features_http
    service, store = tracer.controlled.host.service, tracer.controlled.app.store
    real_port = StoreFeatureBindingPort(store)
    phase = {'exhaust': False, 'now': 0.0}
    sealed = []

    def compute_clock():
        # Like the original budget regression, each measured interval can use
        # 51 ms, but only after a real seal. No observe/call count selects it.
        if phase['exhaust']:
            phase['now'] += 0.051
        return phase['now']

    class NotifyAfterRealSeal:
        def snapshot(self, principal, run_id):
            return real_port.snapshot(principal, run_id)

        def seal(self, principal, run_id, adoption_sha, registered_sha, record_sha):
            result = real_port.seal(principal, run_id, adoption_sha, registered_sha, record_sha)
            if result is True:
                sealed.append({'principal': principal, 'run_id': run_id, 'record_sha256': record_sha})
                phase['exhaust'] = True
            return result

    service.creation_features = {
        'policy_bundle': {**POLICY_BUNDLE, 'feature_version': 'local-deterministic-v2'},
        'binding_port': NotifyAfterRealSeal(), 'compute_clock': compute_clock}
    enable_recommendations(tracer)
    client = activate(tracer)
    budget_before = {lane: ledger.report() for lane, ledger in tracer.controlled.budgets.items()}
    run_id = 'post-seal-compute-budget'
    accepted = client.post('/v1/tasks', json=task_payload(tracer, run_id))
    assert accepted.status_code == 202 and accepted.json()['status'] == 'queued'
    assert accepted.json()['routing_recommendation'] == {
        'schema_version': 'creation-routing-recommendation-v1', 'status': 'unavailable',
        'adopted': False, 'reason': 'creation_recommendation_evidence_unavailable'}
    assert len(sealed) == 1 and phase['exhaust'] is True and phase['now'] > 0.05
    internal = service.internal_id('agent', run_id)
    record = service.root / '.task-features' / (internal + '.json')
    anchor = service.root / internal / 'creation-feature-anchor.json'
    record_bytes, anchor_bytes = record.read_bytes(), anchor.read_bytes()
    record_sha = hashlib.sha256(record_bytes).hexdigest()
    saved = json.loads(record_bytes)['routing_recommendation']
    binding = real_port.snapshot('agent', run_id)
    assert saved['status'] == 'unknown' and saved['adopted'] is False
    assert record_sha == sealed[0]['record_sha256'] == binding['seal']['record_sha256']
    assert json.loads(anchor_bytes)['record_sha256'] == record_sha
    before_read = tracer.snapshot('post-seal-before-independent-read')
    # Restore a steady monotonic test clock, not the policy, rules or evidence.
    phase['exhaust'] = False
    for _ in range(2):
        observed = client.get('/v1/tasks/' + run_id)
        assert observed.status_code == 202 and observed.json()['status'] == 'queued'
        assert observed.json()['routing_recommendation'] == saved
    assert record.read_bytes() == record_bytes and anchor.read_bytes() == anchor_bytes
    assert real_port.snapshot('agent', run_id) == binding and len(sealed) == 1
    assert tracer.snapshot('post-seal-after-independent-read') == before_read
    assert {lane: ledger.report() for lane, ledger in tracer.controlled.budgets.items()} == budget_before
    assert not tracer.controlled.requests
    assert not tracer.controlled.side_requests['source'] and not tracer.controlled.side_requests['accounting']
    tracer.evidence['post_seal_compute_exhaustion'] = {
        'trigger': 'real Store seal returned True; only then clock advances 51ms per read',
        'original_full01_cause_or_exact_phase_claimed': False, 'clock_not_rewound': True,
        'post_status': 202, 'post_recommendation': 'unavailable', 'saved_recommendation': 'unknown',
        'subsequent_independent_get_recommendation': 'unknown', 'record_sha256': record_sha,
        'anchor_sha256': hashlib.sha256(anchor_bytes).hexdigest(), 'seal': binding['seal'],
        'model_calls': 0, 'source_calls': 0, 'accounting_calls': 0}


@pytest.mark.parametrize('phase', ['bound-before-core', 'previous-accepting'])
def test_prior_acceptance_never_acquires_a_second_recommendation_producer(task_features_http, phase):
    tracer = task_features_http
    client = activate(tracer)
    enable_recommendations(tracer)
    service, store, gateway = tracer.controlled.host.service, tracer.controlled.app.store, tracer.controlled.app.gateway
    payload = task_payload(tracer, 'original-acceptance-owner')
    internal = service.internal_id('agent', payload['run_id'])
    if phase == 'bound-before-core':
        recorder = service.creation_features
        _, created = store.bind('agent', payload, gateway.registry, with_creation=True,
            feature_policy=recorder.policy, feature_contracts=recorder.registered,
            feature_recommendations=recorder.recommendation_adoptions)
        assert created
    else:
        directory = service.root / internal
        directory.mkdir(mode=0o700)
        with (directory / '.lock').open('a+b') as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            accepted = client.post('/v1/tasks', json=payload)
            assert accepted.status_code == 202 and accepted.json()['status'] == 'accepting'
    for _ in range(2):
        response = client.post('/v1/tasks', json=payload)
        assert response.status_code == 202 and response.json()['status'] == 'queued'
        assert response.json()['routing_recommendation']['status'] == 'unavailable'
        assert client.get('/v1/tasks/original-acceptance-owner').json()['routing_recommendation']['status'] == 'unavailable'
    assert not (service.root / '.task-features' / (internal + '.json')).exists()
    assert not (service.root / internal / 'creation-feature-anchor.json').exists()
    assert tracer.dispatch() is not None
    completed = client.get('/v1/tasks/original-acceptance-owner')
    assert completed.status_code == 200 and completed.json()['status'] == 'passed'
    assert completed.json()['routing_recommendation']['status'] == 'unavailable'
    assert len(tracer.controlled.requests) == 1


def test_actual_bytes_across_all_sources_remain_bounded_when_the_last_file_grows(task_features_http, monkeypatch):
    from task_recommendations_fixture import calibrated_contracts
    tracer = task_features_http
    observed = {'last_file_grew': False, 'actual_source_bytes': {}, 'declared_union_bytes': 1048576}
    original_read = os.read
    def before_enable(source, values):
        files = sorted(source.parent.glob('*-union-source.json'))
        assert len(files) == 5 and sum(path.stat().st_size for path in files) == 1048576
        last = files[-1]
        (tracer.artifacts / 'preserved-last-source-before-growth.bin').write_bytes(last.read_bytes())
        identities = {(path.stat().st_dev, path.stat().st_ino): path.name for path in files}
        last_identity = last.stat().st_dev, last.stat().st_ino
        def grow_last(descriptor, size):
            info = os.fstat(descriptor)
            identity = info.st_dev, info.st_ino
            if identity == last_identity and not observed['last_file_grew']:
                with last.open('ab') as stream:
                    stream.write(b'X' * 524288)
                observed['last_file_grew'] = True
            data = original_read(descriptor, size)
            if identity in identities:
                name = identities[identity]
                observed['actual_source_bytes'][name] = observed['actual_source_bytes'].get(name, 0) + len(data)
            return data
        active_patch.setattr(os, 'read', grow_last)
    with monkeypatch.context() as patch:
        active_patch = patch
        calibrated_contracts(tracer, source_fault='union-last-growth', before_enable=before_enable)
    client = activate(tracer)
    accepted = client.post('/v1/tasks', json=task_payload(tracer, 'union-read-bound'))
    assert accepted.status_code == 202 and accepted.json()['task_features']['status'] == 'available'
    assert accepted.json()['routing_recommendation']['status'] == 'unknown'
    assert not tracer.controlled.requests and observed['last_file_grew'] is True
    observed['actual_union_bytes'] = sum(observed['actual_source_bytes'].values())
    observed['global_one_byte_sentinel_included'] = True
    tracer.evidence['source_union_read_bound'] = observed
    assert len(observed['actual_source_bytes']) == 5
    assert observed['actual_union_bytes'] <= 1048577, 'Per-file cap allowed actual union reads beyond 1 MiB plus one sentinel byte'


@pytest.mark.parametrize('fault', ['sql-abort', 'record-directory-is-file', 'partial-record-already-exists'])
def test_failed_auxiliary_recommendation_storage_does_not_change_original_dispatch(task_features_http, fault):
    tracer = task_features_http
    client = activate(tracer)
    enable_recommendations(tracer)
    service, store = tracer.controlled.host.service, tracer.controlled.app.store
    payload = task_payload(tracer, 'auxiliary-advice-failure')
    internal = service.internal_id('agent', payload['run_id'])
    if fault == 'sql-abort':
        with sqlite3.connect(store.path) as db:
            db.execute("CREATE TRIGGER reject_auxiliary BEFORE INSERT ON creation_feature_adoptions BEGIN SELECT RAISE(ABORT,'synthetic auxiliary failure'); END")
    elif fault == 'record-directory-is-file':
        (service.root / '.task-features').write_bytes(b'synthetic resource obstacle')
    else:
        records = service.root / '.task-features'
        records.mkdir(mode=0o700)
        (records / (internal + '.json')).write_bytes(b'{"synthetic_partial":')
    accepted = client.post('/v1/tasks', json=payload)
    assert accepted.status_code == 202 and accepted.json()['status'] == 'queued'
    assert accepted.json()['routing_recommendation']['status'] == 'unavailable'
    before = tracer.snapshot('after-auxiliary-advice-failure')
    for _ in range(2):
        observed = client.get('/v1/tasks/auxiliary-advice-failure')
        assert observed.status_code == 202 and observed.json()['routing_recommendation']['status'] == 'unavailable'
    assert tracer.snapshot('after-auxiliary-advice-reads') == before
    assert tracer.dispatch() is not None
    completed = client.get('/v1/tasks/auxiliary-advice-failure')
    assert completed.status_code == 200 and completed.json()['status'] == 'passed'
    assert completed.json()['routing_recommendation']['status'] == 'unavailable'
    assert len(tracer.controlled.requests) == 1


@pytest.mark.parametrize('fault', ['missing-anchor', 'missing-record', 'torn-envelope', 'duplicate-key',
    'oversized-envelope', 'self-resigned-anchor', 'missing-seal', 'wrong-adoption'])
def test_a_changed_envelope_cannot_become_trusted_advice_on_public_or_operator_views(task_features_http, fault):
    tracer = task_features_http
    client = activate(tracer)
    operator = tracer.client('operator')
    enable_recommendations(tracer)
    accepted = client.post('/v1/tasks', json=task_payload(tracer, 'advice-integrity'))
    assert accepted.status_code == 202 and accepted.json()['routing_recommendation']['status'] == 'unknown'
    internal = accepted.json()['task_features']['binding']['internal_id']
    record = tracer.controlled.host.service.root / '.task-features' / (internal + '.json')
    anchor = tracer.controlled.host.service.root / internal / 'creation-feature-anchor.json'
    original = record.read_bytes()
    (tracer.artifacts / 'original-envelope.json').write_bytes(original)
    (tracer.artifacts / 'original-anchor.json').write_bytes(anchor.read_bytes())
    if fault == 'missing-anchor':
        anchor.rename(tracer.artifacts / 'preserved-missing-anchor.json')
    elif fault == 'missing-record':
        record.rename(tracer.artifacts / 'preserved-missing-record.json')
    elif fault == 'torn-envelope':
        record.write_bytes(b'{"PRIVATE_ADVICE_ERROR_CANARY":')
    elif fault == 'duplicate-key':
        record.write_bytes(b'{"schema_version":"creation-task-observation-v2",' + original[1:])
    elif fault == 'oversized-envelope':
        record.write_bytes(b'X' * 98305)
    elif fault == 'self-resigned-anchor':
        changed = json.loads(original)
        changed['routing_recommendation']['adopted'] = True
        record.write_text(json.dumps(changed))
        rewritten = json.loads(anchor.read_bytes())
        rewritten['record_sha256'] = hashlib.sha256(record.read_bytes()).hexdigest()
        anchor.write_text(json.dumps(rewritten))
    else:
        with sqlite3.connect(tracer.controlled.app.store.path) as db:
            if fault == 'missing-seal':
                db.execute('UPDATE creation_feature_adoptions SET record_sha=NULL,request_sha=NULL,registered_sha=NULL')
            else:
                db.execute('UPDATE creation_feature_adoptions SET adoption_sha=?', ('0' * 64,))
    observed = client.get('/v1/tasks/advice-integrity')
    assert observed.status_code == 202 and observed.json()['routing_recommendation']['status'] == 'unavailable'
    snapshot = operator.get('/monitor/snapshot')
    assert snapshot.status_code == 200
    task = next(row for row in snapshot.json()['tasks'] if row['run'] == 'advice-integrity')
    assert task['operator_observation']['routing_recommendation']['status'] == 'unavailable'
    assert 'PRIVATE_ADVICE_ERROR_CANARY' not in observed.text + snapshot.text
    assert not tracer.controlled.requests


@pytest.mark.parametrize('fault', ['missing-declaration', 'raw-not-declared', 'contradictory-sha', 'missing-required-variant'])
def test_all_required_source_declarations_are_checked_before_any_source_open(task_features_http, monkeypatch, fault):
    from task_recommendations_fixture import calibrated_contracts
    tracer = task_features_http
    opened, original = [], os.open
    def observed(path, flags, *args, **kwargs):
        if isinstance(path, (str, os.PathLike)) and str(path).endswith('.json') and 'calibration' in str(path):
            opened.append(str(path))
        return original(path, flags, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(os, 'open', observed)
        calibrated_contracts(tracer, source_fault=fault)
    client = activate(tracer)
    accepted = client.post('/v1/tasks', json=task_payload(tracer, 'incomplete-calibration-declaration'))
    assert accepted.status_code == 202 and accepted.json()['task_features']['status'] == 'available'
    advice = accepted.json()['routing_recommendation']
    assert advice['adopted'] is False and advice['status'] in ('unknown', 'unsupported')
    assert not any(row['kind'] == 'compare_skipping_classification' for row in advice['suggestions'])
    tracer.evidence['required_source_preflight'] = {'fault': fault, 'actual_source_opens': len(opened)}
    assert opened == [], 'Incomplete or contradictory required evidence was discovered only after opening a source'


@pytest.mark.parametrize('supplied', ['valid', 'empty', 'missing-key', 'extra-key', 'wrong-sha', 'default-legacy-reader'])
def test_public_exact_validator_uses_immutable_supplied_bytes_without_a_falsey_reader_fallback(task_features_http, monkeypatch, supplied):
    """Additional approved host-library seam, separate from real HTTP acceptance."""
    from datetime import datetime, timezone
    from types import MappingProxyType
    from cloudyfan_jev.scores.selector_research import validate_selector_calibration
    from task_recommendations_fixture import calibrated_contracts
    tracer = task_features_http
    _, values = calibrated_contracts(tracer, data_kind='observed_calibration', source_fault='two-sources')
    plan = values['plan']
    names = {name for stage in plan['qcs_policy']['stages'].values()
             for variant in stage['variants'].values() for name in variant['calibration']['source_files']}
    actual = {name: Path(name).read_bytes() for name in names}
    provided = dict(actual)
    if supplied == 'empty':
        provided = {}
    elif supplied == 'missing-key':
        provided.pop(next(iter(provided)))
    elif supplied == 'extra-key':
        provided[str(tracer.artifacts / 'extra-not-declared.json')] = b'not a declared source'
    elif supplied == 'wrong-sha':
        provided[next(iter(provided))] = b'different actual bytes'
    opened, original_read = [], Path.read_bytes
    def guard(path):
        if str(path) in names:
            opened.append(str(path))
            if supplied != 'default-legacy-reader':
                raise AssertionError('Supplied immutable bytes must never fall back to unbounded Path.read_bytes')
        return original_read(path)
    with monkeypatch.context() as patch:
        patch.setattr(Path, 'read_bytes', guard)
        kwargs = {'data_kind': 'observed_calibration',
                  'now': datetime.fromtimestamp(tracer.clock(), timezone.utc).isoformat()}
        if supplied != 'default-legacy-reader':
            kwargs['source_bytes'] = MappingProxyType(provided)
        if supplied in ('valid', 'default-legacy-reader'):
            report = validate_selector_calibration(plan, values['decision'], **kwargs)
            assert report['ready'] is True and report['required_contracts'] == 3
        else:
            with pytest.raises(ValueError):
                validate_selector_calibration(plan, values['decision'], **kwargs)
    assert bool(opened) is (supplied == 'default-legacy-reader')
    tracer.evidence['public_host_validator_seam'] = {'supplied': supplied, 'source_path_reads': len(opened),
        'scope': 'approved public host-library contract; does not replace real HTTP or certify actual observations'}


def test_a_parent_directory_replacement_cannot_redirect_a_source_open_outside_the_public_root(task_features_http, monkeypatch):
    from task_recommendations_fixture import calibrated_contracts
    tracer = task_features_http
    raced, outside_opened = [], []
    original_open = os.open
    def before_enable(source, values):
        parent = source.parent
        outside = tracer.artifacts / 'outside-source-root'
        outside.mkdir(mode=0o700)
        other = outside / source.name
        other.write_bytes(b'PRIVATE_OUTSIDE_CALIBRATION_CANARY')
        outside_identity = other.stat().st_dev, other.stat().st_ino
        def replace_parent(path, flags, *args, **kwargs):
            if isinstance(path, (str, os.PathLike)) and Path(path).name == source.name and not raced:
                parent.rename(tracer.artifacts / 'preserved-original-public-root')
                parent.symlink_to(outside, target_is_directory=True)
                raced.append(True)
                descriptor = original_open(path, flags, *args, **kwargs)
                info = os.fstat(descriptor)
                outside_opened.append((info.st_dev, info.st_ino) == outside_identity)
                return descriptor
            return original_open(path, flags, *args, **kwargs)
        active_patch.setattr(os, 'open', replace_parent)
    with monkeypatch.context() as patch:
        # The fixture invokes before_enable only after all original metadata
        # is frozen and immediately before actual host source acquisition.
        active_patch = patch
        calibrated_contracts(tracer, before_enable=before_enable)
    client = activate(tracer)
    accepted = client.post('/v1/tasks', json=task_payload(tracer, 'parent-race'))
    assert accepted.status_code == 202 and accepted.json()['task_features']['status'] == 'available'
    assert accepted.json()['routing_recommendation']['status'] == 'unknown'
    assert 'PRIVATE_OUTSIDE_CALIBRATION_CANARY' not in accepted.text
    assert raced == [True], 'The parent replacement boundary was not actually exercised'
    tracer.evidence['source_parent_race'] = {'actual_parent_replacement': True,
        'opened_outside_public_root': any(outside_opened), 'retained_original_root': True}
    assert outside_opened == [False], 'An actual source descriptor escaped the original public-root directory'


@pytest.mark.parametrize('shape', ['candidate-total', 'conditional-product'])
def test_public_exact_validator_bounds_original_cartesian_work_before_expanding_it(task_features_http, shape):
    from copy import deepcopy
    from types import MappingProxyType
    from cloudyfan_jev.scores.selector_research import validate_selector_calibration
    from task_recommendations_fixture import calibrated_contracts
    tracer = task_features_http
    _, values = calibrated_contracts(tracer)
    original = values['plan']
    names = {name for stage in original['qcs_policy']['stages'].values()
             for row in stage['variants'].values() for name in row['calibration']['source_files']}
    actual = MappingProxyType({name: Path(name).read_bytes() for name in names})
    plan = deepcopy(original)
    sizes = {'open_source': 4, 'low': 4, 'mid': 4, 'high': 4} if shape == 'candidate-total' else {
        'open_source': 1, 'low': 1, 'mid': 5, 'high': 5}
    for tier, count in sizes.items():
        plan['pools'][tier] = [deepcopy(plan['pools'][tier][0]) for _ in range(count)]
    with pytest.raises(ValueError, match='[Bb]ounded'):
        validate_selector_calibration(plan, values['decision'], data_kind='offline_fixture', source_bytes=actual)
    # No entry/profile exists in this literal invalid oversized DTO. Expanding
    # required_contracts or hashing its rows would raise KeyError instead of
    # the required bounded-work refusal at the public host interface.
    for tier, count in sizes.items():
        plan['pools'][tier] = [{} for _ in range(count)]
    with pytest.raises(ValueError, match='[Bb]ounded'):
        validate_selector_calibration(plan, values['decision'], data_kind='offline_fixture', source_bytes=actual)
    tracer.evidence['public_host_validator_bounds'] = {'shape': shape,
        'candidate_count': sum(sizes.values()), 'conditional_count': 192 if shape == 'candidate-total' else 155,
        'scope': 'public host-library rejection only; original task runner is not given this invalid structure'}


def test_get_poll_and_reader_reopen_make_no_source_open_attempts_with_live_positive_controls(task_features_http, monkeypatch):
    import io
    from task_recommendations_fixture import calibrated_contracts, reopen_calibrated_reader
    tracer = task_features_http
    source, values = calibrated_contracts(tracer)
    client, operator = activate(tracer), tracer.client('operator')
    accepted = client.post('/v1/tasks', json=task_payload(tracer, 'source-read-once'))
    assert accepted.status_code == 202
    original = accepted.json()['routing_recommendation']
    assert any(row['kind'] == 'compare_skipping_classification' for row in original['suggestions'])
    preserved = tracer.artifacts / 'preserved-source-read-control.json'
    names = {str(source), str(preserved), source.name, preserved.name}
    attempts, original_io, original_os = [], io.open, os.open
    def watched(path):
        return isinstance(path, (str, os.PathLike)) and (str(path) in names or Path(path).name in names)
    def observed_io(path, *args, **kwargs):
        if watched(path):
            attempts.append('io.open')
        return original_io(path, *args, **kwargs)
    def observed_os(path, *args, **kwargs):
        if watched(path):
            attempts.append('os.open')
        return original_os(path, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(io, 'open', observed_io)
        patch.setattr(os, 'open', observed_os)
        with io.open(source, 'rb') as stream:
            assert stream.read(1)
        descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        os.close(descriptor)
        parent = original_os(source.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            descriptor = os.open(source.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            os.close(descriptor)
        finally:
            os.close(parent)
        assert attempts == ['io.open', 'os.open', 'os.open'], 'Absolute and actual relative-dir_fd source observers need positive controls'
        attempts.clear()
        source.rename(preserved)
        parent = original_os(source.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            with pytest.raises(FileNotFoundError) as failed:
                os.open(source.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            assert failed.value.errno == 2 and failed.value.filename == source.name
            assert attempts == ['os.open'], 'A failed open of the original source would be invisible'
        finally:
            os.close(parent)
        attempts.clear()
        for _ in range(2):
            assert client.get('/v1/tasks/source-read-once').json()['routing_recommendation'] == original
            assert operator.get('/monitor/snapshot').status_code == 200
        reopen_calibrated_reader(tracer, values)
        assert client.get('/v1/tasks/source-read-once').json()['routing_recommendation'] == original
        assert operator.get('/monitor/snapshot').status_code == 200
        assert attempts == [], 'Reader attempted to reopen a calibration source and swallowed the failure'
    assert not tracer.controlled.requests
    tracer.evidence['source_read_observers'] = {'positive_controls': ['io.open', 'os.open', 'relative_dir_fd_os.open', 'relative_dir_fd_missing_os.open'],
        'original_file_not_found_errno': 2,
        'get_poll_and_reopen_source_open_attempts': len(attempts), 'source_retained_after_rename': preserved.is_file()}


def test_source_growth_during_actual_read_is_bounded_and_cannot_create_a_cost_claim(task_features_http, monkeypatch):
    from task_recommendations_fixture import calibrated_contracts
    tracer = task_features_http
    observed = {'actual_growth': False, 'source_read_bytes': 0}
    original_read = os.read
    def before_enable(source, values):
        original_bytes = source.read_bytes()
        (tracer.artifacts / 'preserved-source-before-growth.json').write_bytes(original_bytes)
        identity = source.stat().st_dev, source.stat().st_ino
        def growing_read(descriptor, size):
            info = os.fstat(descriptor)
            data = original_read(descriptor, size)
            if (info.st_dev, info.st_ino) == identity:
                observed['source_read_bytes'] += len(data)
                if not observed['actual_growth']:
                    with source.open('ab') as stream:
                        stream.write(b'X' * 524288)
                    observed['actual_growth'] = True
            return data
        active_patch.setattr(os, 'read', growing_read)
    with monkeypatch.context() as patch:
        active_patch = patch
        calibrated_contracts(tracer, before_enable=before_enable)
    client = activate(tracer)
    accepted = client.post('/v1/tasks', json=task_payload(tracer, 'growing-source'))
    assert accepted.status_code == 202 and accepted.json()['task_features']['status'] == 'available'
    advice = accepted.json()['routing_recommendation']
    assert advice['status'] == 'unknown' and advice['adopted'] is False
    assert not any(row['kind'] == 'compare_skipping_classification' for row in advice['suggestions'])
    assert observed['actual_growth'] is True and 0 < observed['source_read_bytes'] <= 262145
    assert not tracer.controlled.requests
    tracer.evidence['bounded_source_growth'] = observed


@pytest.mark.parametrize('recommendations,fault', [(False, 'control'), (True, 'control'),
    (True, 'anchor-parent'), (True, 'record-parent')], ids=['legacy-control', 'new-control', 'anchor-failure', 'record-failure'])
def test_original_directory_durability_precedes_independent_seal_and_failure_never_repairs(task_features_http, monkeypatch, recommendations, fault):
    import stat
    from urllib.parse import unquote
    tracer = task_features_http
    client = activate(tracer)
    if recommendations:
        enable_recommendations(tracer)
    service = tracer.controlled.host.service
    internal = service.internal_id('agent', 'durable-advice')
    parents = {'anchor-parent': service.root / internal, 'record-parent': service.root / '.task-features'}
    control = tracer.artifacts / 'positive-directory-sync-control'
    control.mkdir(mode=0o700)
    events, failed, positive = [], [], []
    original_sync, original_connect = os.fsync, sqlite3.connect
    def observed_sync(descriptor):
        info = os.fstat(descriptor)
        if stat.S_ISDIR(info.st_mode):
            identity = info.st_dev, info.st_ino
            if identity == (control.stat().st_dev, control.stat().st_ino):
                original_sync(descriptor)
                positive.append(True)
                return
            for label, parent in parents.items():
                if parent.exists() and identity == (parent.stat().st_dev, parent.stat().st_ino):
                    if label == fault:
                        failed.append(label)
                        raise OSError('PRIVATE_SYNTHETIC_DIRECTORY_SYNC_FAILURE')
                    original_sync(descriptor)
                    events.append(label)
                    return
        original_sync(descriptor)
    target = tracer.controlled.app.store.path.resolve()
    def observed_connect(database, *args, **kwargs):
        db = original_connect(database, *args, **kwargs)
        path = unquote(str(database).split('?', 1)[0].removeprefix('file:'))
        if Path(path).absolute() == target:
            def trace(statement):
                if statement.lstrip().upper().startswith('UPDATE CREATION_FEATURE_ADOPTIONS SET RECORD_SHA='):
                    events.append('independent-seal-update')
            db.set_trace_callback(trace)
        return db
    with monkeypatch.context() as patch:
        patch.setattr(os, 'fsync', observed_sync)
        patch.setattr(sqlite3, 'connect', observed_connect)
        descriptor = os.open(control, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        assert positive == [True], 'The real directory-fsync observer needs an independent positive control'
        accepted = client.post('/v1/tasks', json=task_payload(tracer, 'durable-advice'))
        assert accepted.status_code == 202 and accepted.json()['status'] == 'queued'
        tracer.evidence['durability_boundary'] = {'events': events, 'injected_failure': failed,
            'positive_directory_sync': True, 'scope': 'actual filesystem/SQLite boundaries; not an actual power-loss test'}
        if fault == 'control':
            assert accepted.json()['task_features']['status'] == 'available'
            assert events == ['anchor-parent', 'record-parent', 'independent-seal-update'], 'Independent seal occurred without both successful original parent-directory syncs'
        else:
            assert failed == [fault], 'The original writer never reached its required directory-sync boundary'
            assert 'independent-seal-update' not in events
            assert accepted.json()['routing_recommendation']['status'] == 'unavailable'
        prior = list(events)
        for _ in range(2):
            observed = client.get('/v1/tasks/durable-advice')
            replayed = client.post('/v1/tasks', json=task_payload(tracer, 'durable-advice'))
            assert observed.status_code == replayed.status_code == 202
            assert observed.json()['task_features'] == accepted.json()['task_features']
        assert events == prior
    assert tracer.dispatch() is not None
    completed = client.get('/v1/tasks/durable-advice')
    assert completed.status_code == 200 and completed.json()['status'] == 'passed'
    assert len(tracer.controlled.requests) == 1


@pytest.mark.parametrize('fault', ['upper-env', 'mixed-env'])
def test_protected_source_components_are_case_insensitive_before_any_source_open(task_features_http, monkeypatch, fault):
    from task_recommendations_fixture import calibrated_contracts
    tracer = task_features_http
    attempts, original_open, positive = [], os.open, []
    def observed(path, flags, *args, **kwargs):
        if isinstance(path, (str, os.PathLike)) and Path(path).name == 'synthetic-calibration.json':
            attempts.append(Path(path).name)
        return original_open(path, flags, *args, **kwargs)
    def before_enable(source, values):
        descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        os.close(descriptor)
        assert attempts == ['synthetic-calibration.json']
        positive.append(True)
        attempts.clear()
    with monkeypatch.context() as patch:
        patch.setattr(os, 'open', observed)
        calibrated_contracts(tracer, source_fault=fault, before_enable=before_enable)
    client = activate(tracer)
    accepted = client.post('/v1/tasks', json=task_payload(tracer, 'casefold-source'))
    assert accepted.status_code == 202 and accepted.json()['task_features']['status'] == 'available'
    tracer.evidence['case_insensitive_source_guard'] = {'case': fault, 'positive_open_control': positive == [True],
        'product_source_opens': len(attempts)}
    assert attempts == [], 'Protected .env component with different case was opened by the host source reader'
    advice = accepted.json()['routing_recommendation']
    assert advice['status'] == 'unknown' and advice['adopted'] is False
    assert not any(row['kind'] == 'compare_skipping_classification' for row in advice['suggestions'])
    assert not tracer.controlled.requests


def test_public_renderer_preserves_legal_saved_bounds_but_never_masks_a_malformed_operator_projection(tmp_path):
    """Public HTML renderer seam; separate from real HTTP/headed acceptance."""
    from copy import deepcopy
    import shutil
    import subprocess
    source = Path(__file__).parents[1] / 'src/cloudyfan_monitor/static/trace.js'
    advice = {'schema_version': 'creation-routing-recommendation-v1',
        'rule_version': 'route-recommendation-rules-v1', 'data_policy_version': 'creation-recommendation-data-v1',
        'captured_at': 1792540800, 'status': 'available', 'adopted': False,
        'feature_sha256': 'a'*64, 'context_sha256': 'b'*64, 'input_sha256': 'c'*64,
        'suggestions': [{'kind': 'compare_skipping_classification', 'economics': 'proven_insufficient',
            'reasons': ['classification_proven_not_economic'], 'comparison': {'source_kind': 'offline_fixture',
            'conditional_scopes': 3, 'net_saving_usd': {'lower': '-1.919', 'upper': '-1.919'},
            'source_sha256': ['d'*64], 'distribution_is_answer_probability': False}}]}
    script = """const fs=require('node:fs'),vm=require('node:vm');
const sandbox={window:{}};vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),sandbox,{timeout:1000});
process.stdout.write(sandbox.window.CloudyFanTrace.render(JSON.parse(fs.readFileSync(0,'utf8'))));"""
    def render(name, task):
        node = shutil.which('node')
        assert node is not None, 'The existing Monitor check runtime requires Node on PATH'
        result = subprocess.run([node, '-e', script, str(source)],
            input=json.dumps(task), capture_output=True, text=True, timeout=5, check=True)
        assert not result.stderr
        (tmp_path / (name + '.html')).write_text(result.stdout)
        return result.stdout.split('data-routing-recommendation', 1)[1].split('</section>', 1)[0]
    original = {'client': 'agent', 'run': 'renderer-bounds', 'routing_recommendation': advice,
        'operator_observation': {'schema_version': 'mrc-observation-v1', 'routing_recommendation': advice}}
    assert 'data-recommendation-kind="compare_skipping_classification"' in render('normal', original)
    longest = '1'*64 + '.' + '2'*63
    assert len(longest) == 128
    bounded = deepcopy(original)
    bounded['operator_observation']['routing_recommendation']['suggestions'][0]['comparison']['net_saving_usd'] = {'lower': longest, 'upper': longest}
    assert longest in render('decimal128', bounded)
    duplicate = deepcopy(original)
    duplicate['operator_observation']['routing_recommendation']['suggestions'][0]['comparison']['source_sha256'] *= 2
    assert 'data-recommendation-kind="compare_skipping_classification"' in render('two-files-same-hash', duplicate), 'Two legal source files with identical bytes were treated as malformed'
    for invalid_status in ('unavailable', 'not_provided', 'unconfirmed'):
        invalid = deepcopy(original)
        invalid['operator_observation']['routing_recommendation']['status'] = invalid_status
        html = render('invalid-full-status-'+invalid_status, invalid)
        assert 'data-recommendation-kind="compare_skipping_classification"' not in html, 'A compact/UI-only status admitted a full cost claim'
    for name, corrupt in [('null', None), ('string', 'PRIVATE_RENDERER_CANARY'), ('wrong-schema', {'schema_version': 'future'}),
                           ('bad-advice', {'schema_version': 'mrc-observation-v1', 'routing_recommendation': 'PRIVATE_RENDERER_CANARY'})]:
        task = {**original, 'operator_observation': corrupt}
        html = render(name, task)
        assert 'data-recommendation-kind="compare_skipping_classification"' not in html
        assert '未支援' in html and 'PRIVATE_RENDERER_CANARY' not in html
    unknown = deepcopy(original)
    unknown['operator_observation']['routing_recommendation']['suggestions'][0]['reasons'] = ['private_unrecognized_reason']
    html = render('unknown-reason', unknown)
    assert 'private_unrecognized_reason' not in html and 'ui_recommendation_reason_unrecognized' in html


def test_supplied_source_map_is_snapshotted_before_an_external_backing_dictionary_changes(task_features_http, monkeypatch):
    """Approved public host-library seam; no model or HTTP oracle substitution."""
    from datetime import datetime, timezone
    from types import MappingProxyType
    from cloudyfan_jev.scores.selector_research import validate_selector_calibration
    from task_recommendations_fixture import calibrated_contracts
    tracer = task_features_http
    source, values = calibrated_contracts(tracer, data_kind='observed_calibration')
    body = source.read_bytes()
    backing, changed = {str(source): body}, []
    original = hashlib.sha256
    def mutate(data=b'', *args, **kwargs):
        if data == body and not changed:
            backing.clear()
            backing[str(source)] = b'changed after the bounded own snapshot'
            changed.append(True)
        return original(data, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(hashlib, 'sha256', mutate)
        result = validate_selector_calibration(values['plan'], values['decision'], data_kind='observed_calibration',
            now=datetime.fromtimestamp(tracer.clock(), timezone.utc).isoformat(), source_bytes=MappingProxyType(backing))
    assert changed == [True] and result['ready'] is True
    tracer.evidence['public_map_snapshot'] = {'backing_changed_during_validation': True,
        'result_uses_original_bounded_bytes': True, 'synthetic_source_not_real_observations': True}


@pytest.mark.parametrize('exact_type', [True, False])
def test_unlisted_backup_subclasses_are_not_treated_as_a_verified_cost_adapter(task_features_http, exact_type):
    """Approved host-only create seam: no Queue bypass, run or HTTP claim."""
    from datetime import datetime, timezone
    from cloudyfan_jev.budget import BudgetedCore
    from cloudyfan_jev.capacity_queue import QueuePreparation
    from cloudyfan_jev.demo import ArithmeticEnvironment
    from cloudyfan_jev.engine import Limits
    from cloudyfan_jev.http_api import RegisteredContract, TaskService
    from cloudyfan_jev.scores.backup_runtime import BackupCategoryCore
    from cloudyfan_monitor.integration import ACCESS_POLICY, RunBoundCore
    from task_features_fixture import POLICY_BUNDLE, StoreFeatureBindingPort
    from task_recommendations_fixture import RECOMMENDATION_POLICY, calibrated_contracts
    tracer = task_features_http
    source, values = calibrated_contracts(tracer)
    original = tracer.controlled.host.service.contracts['health-backup-v1']
    class UnsupportedBackup(BackupCategoryCore):
        """Distinct, unlisted host class, even with the same declared descriptor."""
    core_type = BackupCategoryCore if exact_type else UnsupportedBackup
    def factory(run_id, client, local):
        return core_type(client, values['plan'], values['decision'], run_id=run_id,
            health_store=tracer.controlled.host.health,
            clock=lambda: datetime.fromtimestamp(tracer.clock(), timezone.utc),
            limits=Limits(max_turns=1), local_backend=local, execution_policy=values['privacy'],
            diagnostic_contract=values['diagnostics'])
    template = factory('unlisted-host-template', original.runner.core.client, None)
    runner = BudgetedCore(RunBoundCore(template, factory), original.runner.store,
        model_bounds=original.runner.model_bounds, tool_bounds=original.runner.tool_bounds, verify_bound=original.runner.verify_bound)
    contract = RegisteredContract(original.version, runner, lambda workspace: ArithmeticEnvironment())
    assert template.execution_contract_descriptor(contract.version) == values['plan']['qcs_policy']['execution_contract']
    contracts = {'health-backup-v1': contract}
    if exact_type:
        QueuePreparation('exact-native-type-control-v1', contracts=contracts, tools={'health-backup-v1': []})
    else:
        with pytest.raises(ValueError, match='supported original Core preparation protocol'):
            QueuePreparation('unlisted-native-type-control-v1', contracts=contracts, tools={'health-backup-v1': []})
    service = TaskService(tracer.artifacts / 'host-create-tasks', contracts=contracts,
        allowed_principal='agent', principal_active=tracer.controlled.host.service.principal_active,
        retention_policy=ACCESS_POLICY, clock=tracer.clock)
    store = tracer.controlled.app.store
    service.creation_features = {'policy_bundle': {**POLICY_BUNDLE, 'feature_version': 'local-deterministic-v2'},
        'binding_port': StoreFeatureBindingPort(store)}
    service.creation_recommendations = {**RECOMMENDATION_POLICY,
        'calibration_sources': {'data_kind': 'offline_fixture', 'public_root': str(source.parent)}}
    recorder = service.creation_features
    payload = task_payload(tracer, 'native-type-host-control')
    binding, created = store.bind('agent', payload, tracer.controlled.app.gateway.registry,
        feature_policy=recorder.policy, feature_contracts=recorder.registered,
        feature_recommendations=recorder.recommendation_adoptions, with_creation=True)
    assert created is True
    with recorder.operation(), recorder.submitting(binding, created):
        state = service.create('agent', {'run_id': payload['run_id'], 'contract_id': 'health-backup-v1', 'prompt': payload['prompt']})
    assert state['status'] == 'ready'
    advice = service.recommendation_observation('agent', payload['run_id'])
    assert advice['adopted'] is False
    if exact_type:
        assert advice['status'] == 'available'
    else:
        assert advice['status'] == 'unknown', 'Unlisted subclass was treated as the verified native cost protocol'
        assert advice['suggestions'][0]['reasons'] == ['exact_calibration_execution_adapter_unsupported']
        assert not any('comparison' in row for row in advice['suggestions'])
    assert not tracer.controlled.requests
    tracer.evidence['host_type_boundary'] = {'seam': 'public TaskService.create + original Store.bind/first-owner host protocol',
        'exact_native_type': exact_type, 'original_queue_refused_unlisted_subclass': not exact_type,
        'http_negative_or_run_claimed': False, 'original_runtime_called': False}

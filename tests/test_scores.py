import copy
import json

import httpx
import pytest

from cloudyfan_jev.client import Client
from cloudyfan_jev.demo import ArithmeticEnvironment, example_config
from cloudyfan_jev.scores import ScoreCore, SnapshotStore, compile_snapshot, select_profiles, verify_snapshot
from cloudyfan_jev.scores import cli, snapshots
from cloudyfan_jev.scores.catalog import digest

NOW = '2026-10-18T10:00:00+00:00'
LATER = '2026-10-20T10:00:00+00:00'
REQUIREMENTS = {'domain': 'coding', 'capabilities': ['tools'], 'context_tokens': 1000}


def inputs():
    profiles, _ = example_config()
    registry = {'candidates': [], 'open_source': None}
    observations = []
    for tier, score in [('open_source', None), ('low', 60), ('mid', 90), ('high', 110)]:
        entry = {'config_id': tier, 'profile': profiles[tier], 'approved': True,
                 'context_tokens': 8192, 'capabilities': ['tools'], 'catalog_checked_at': NOW,
                 'estimated_request_usd': .001, 'estimated_latency_ms': 200, 'cost_basis': 'fixture_only',
                 'source_model_id': 'example/' + tier, 'source': 'codexradar', 'source_version': 'fixture-v1'}
        if tier == 'open_source':
            entry['open_weight'] = True
            registry['open_source'] = entry
        else:
            registry['candidates'].append(entry)
            observations.append({'source': 'codexradar', 'source_version': 'fixture-v1',
                'source_model_id': 'example/' + tier, 'metric': 'overall', 'score': score,
                'effort': 'low', 'sample_count': 64, 'evaluated_at': '2026-10-18',
                'retrieved_at': NOW, 'source_url': 'https://deng.codexradar.com/', 'raw_sha256': 'a' * 64})
    policy = {'id': 'coding-fixture-v1', 'domain': 'coding', 'source': 'codexradar',
              'source_version': 'fixture-v1', 'metric': 'overall', 'scale': 'source_native',
              'low_mid': 80, 'mid_high': 100, 'max_source_age_hours': 48,
              'max_catalog_age_hours': 48, 'snapshot_ttl_hours': 24,
              'min_samples': 64, 'max_evaluation_age_days': 30}
    return observations, registry, policy


def snapshot():
    return compile_snapshot(*inputs(), now=NOW)


def test_native_boundaries_and_immutable_inputs():
    obs, registry, policy = inputs()
    original = copy.deepcopy((obs, registry, policy))
    result = compile_snapshot(obs, registry, policy, now=NOW)
    assert original == (obs, registry, policy)
    assert [result['pools'][tier][0]['profile']['external_score'] for tier in ('low', 'mid', 'high')] == [60, 90, 110]
    obs[0]['score'], obs[1]['score'] = 80, 100
    result = compile_snapshot(obs, registry, policy, now=NOW)
    assert len(result['pools']['mid']) == 2 and not result['pools']['low']


@pytest.mark.parametrize('change,reason', [
    ({'effort': None}, 'effort_unknown'),
    ({'effort': 'high'}, 'score_missing'),
    ({'sample_count': 63}, 'insufficient_coverage'),
    ({'sample_count': None}, 'insufficient_coverage'),
    ({'score': float('inf')}, 'invalid_observation'),
    ({'retrieved_at': '2026-10-15T00:00:00Z'}, 'source_stale'),
    ({'retrieved_at': '2026-10-19T00:00:00Z'}, 'source_timestamp_in_future'),
    ({'evaluated_at': None}, 'evaluation_date_unknown'),
    ({'evaluated_at': '2025-10-18'}, 'evaluation_stale'),
    ({'raw_sha256': 'x'}, 'invalid_provenance'),
    ({'source_version': 'changed'}, 'score_missing'),
    ({'source': 'other'}, 'score_missing'),
    ({'metric': 'other'}, 'score_missing'),
])
def test_unknown_or_invalid_not_cheap(change, reason):
    obs, registry, policy = inputs()
    obs[0].update(change)
    if change.get('score') == float('inf'):
        # Canonical input hashing also rejects non-finite JSON; no bad snapshot is emitted.
        with pytest.raises(ValueError):
            compile_snapshot(obs, registry, policy, now=NOW)
        return
    result = compile_snapshot(obs, registry, policy, now=NOW)
    assert not result['pools']['low']
    assert result['excluded'] == [{'config_id': 'low', 'reason': reason}]


def test_unknown_effort_requires_reviewed_explicit_binding():
    obs, registry, policy = inputs()
    obs[0]['effort'] = None
    registry['candidates'][0].update(verified_source_effort='low', effort_evidence_url='https://example.org/settings')
    result = compile_snapshot(obs, registry, policy, now=NOW)
    assert result['pools']['low'][0]['profile']['capability_card']['benchmark_evidence']['effort_mapping'] == 'reviewed_binding'
    registry['candidates'][0]['verified_source_effort'] = 'high'
    assert compile_snapshot(obs, registry, policy, now=NOW)['excluded'][0]['reason'] == 'effort_mismatch'


def test_duplicate_upstream_does_not_double_count_or_choose_best():
    obs, registry, policy = inputs()
    obs.append({**obs[0], 'channel': 'openrouter', 'score': 140})
    result = compile_snapshot(obs, registry, policy, now=NOW)
    assert result['excluded'][0]['reason'] == 'ambiguous_evidence'


def test_registry_binding_cannot_move_across_source_versions():
    obs, registry, policy = inputs()
    registry['candidates'][0]['source_version'] = 'old-version'
    assert compile_snapshot(obs, registry, policy, now=NOW)['excluded'][0]['reason'] == 'source_binding_mismatch'


def test_same_source_model_distinct_measured_efforts():
    obs, registry, policy = inputs()
    obs[1].update(source_model_id=obs[0]['source_model_id'], effort='high')
    registry['candidates'][1]['source_model_id'] = obs[0]['source_model_id']
    registry['candidates'][1]['profile']['effort'] = 'high'
    result = compile_snapshot(obs, registry, policy, now=NOW)
    assert not result['excluded']


@pytest.mark.parametrize('field,value', [('approved', False), ('catalog_checked_at', '2025-10-18T00:00:00Z')])
def test_catalog_eligibility(field, value):
    obs, registry, policy = inputs()
    registry['candidates'][0][field] = value
    result = compile_snapshot(obs, registry, policy, now=NOW)
    assert not result['pools']['low']


def test_missing_dates_are_preserved_if_policy_explicitly_allows():
    obs, registry, policy = inputs()
    policy.update(max_evaluation_age_days=None, min_samples=0)
    obs[0].update(evaluated_at=None, sample_count=None)
    result = compile_snapshot(obs, registry, policy, now=NOW)
    assert result['pools']['low'][0]['evidence']['evaluated_at'] is None


@pytest.mark.parametrize('requirements', [
    {**REQUIREMENTS, 'domain': 'vision'}, {**REQUIREMENTS, 'capabilities': ['images']},
    {**REQUIREMENTS, 'context_tokens': 9000}, {**REQUIREMENTS, 'allowed_providers': ['other']},
    {**REQUIREMENTS, 'allowed_providers': []},
])
def test_constraints_cannot_be_silently_relaxed(requirements):
    with pytest.raises(ValueError):
        select_profiles(snapshot(), requirements, now=NOW)


def test_cost_tiebreak_and_no_hidden_private_cards():
    obs, registry, policy = inputs()
    alternative = copy.deepcopy(registry['candidates'][0])
    alternative.update(config_id='other-low', estimated_request_usd=.0001)
    registry['candidates'].append(alternative)
    result = compile_snapshot(obs, registry, policy, now=NOW)
    profiles = select_profiles(result, REQUIREMENTS, now=NOW)
    assert profiles['low']['capability_card']['score_selection']['config_id'] == 'other-low'
    assert profiles['low']['capability_card']['score_selection']['estimate_is_paid_cost'] is False
    alternative['profile']['capability_card']['hidden_answers'] = 'forbidden'
    with pytest.raises(ValueError, match='Private'):
        compile_snapshot(obs, registry, policy, now=NOW)


def test_expiry_uses_oldest_evidence_or_catalog():
    obs, registry, policy = inputs()
    obs[0]['retrieved_at'] = '2026-10-16T11:00:00Z'
    result = compile_snapshot(obs, registry, policy, now=NOW)
    assert result['valid_until'] == '2026-10-18T11:00:00+00:00'
    with pytest.raises(ValueError, match='stale'):
        select_profiles(result, REQUIREMENTS, now=result['valid_until'])


def test_snapshot_hash_tampering():
    result = snapshot()
    result['pools']['low'][0]['profile']['effort'] = 'xhigh'
    with pytest.raises(ValueError, match='digest'):
        verify_snapshot(result)


def test_private_observation_and_unknown_requirements_rejected_before_binding(tmp_path):
    obs, registry, policy = inputs()
    obs[0]['hidden_answer'] = 'forbidden'
    with pytest.raises(ValueError, match='Private'):
        compile_snapshot(obs, registry, policy, now=NOW)
    with pytest.raises(ValueError, match='Unknown requirement'):
        select_profiles(snapshot(), {**REQUIREMENTS, 'hidden_answer': 'forbidden'}, now=NOW)


def test_store_publish_load_and_failed_update_preserves_current(tmp_path):
    store = SnapshotStore(tmp_path)
    result = snapshot()
    store.publish(result, now=NOW)
    assert store.load(now=NOW) == result
    assert store.publish(result, now=NOW) == result['snapshot_id']
    broken = copy.deepcopy(result)
    broken['pools']['low'] = []
    broken['snapshot_id'] = digest({k: v for k, v in broken.items() if k != 'snapshot_id'})
    with pytest.raises(ValueError, match='three pools'):
        store.publish(broken, now=NOW)
    assert store.load()['snapshot_id'] == result['snapshot_id']
    with pytest.raises(ValueError):
        store.load('../../private')


def test_interrupted_snapshot_write_is_retryable(tmp_path, monkeypatch):
    store = SnapshotStore(tmp_path)
    result = snapshot()
    original = snapshots._write

    def partial(path, value):
        if path.name.startswith('snapshot-'):
            path.write_text('{')
            raise OSError('simulated interrupted write')
        original(path, value)

    monkeypatch.setattr(snapshots, '_write', partial)
    with pytest.raises(OSError):
        store.publish(result, now=NOW)
    assert not (tmp_path / (result['snapshot_id'] + '.json')).exists()
    monkeypatch.setattr(snapshots, '_write', original)
    store.publish(result, now=NOW)
    assert store.load() == result


def test_pointer_update_failure_keeps_previous_current(tmp_path, monkeypatch):
    store = SnapshotStore(tmp_path)
    old = snapshot()
    store.publish(old, now=NOW)
    obs, registry, policy = inputs()
    obs[0]['score'] = 61
    new = compile_snapshot(obs, registry, policy, now=NOW)
    original = snapshots.os.replace

    def interrupt(source, target):
        if target.name == 'current.json':
            raise OSError('simulated pointer interruption')
        original(source, target)

    monkeypatch.setattr(snapshots.os, 'replace', interrupt)
    with pytest.raises(OSError):
        store.publish(new, now=NOW)
    assert store.load()['snapshot_id'] == old['snapshot_id']
    monkeypatch.setattr(snapshots.os, 'replace', original)
    store.publish(new, now=NOW)
    assert store.load()['snapshot_id'] == new['snapshot_id']


def test_core_integration_freezes_snapshot_and_reuses_paid_receipts(tmp_path):
    requests = []

    def api(request):
        body = json.loads(request.content)
        requests.append(body)
        i = len(requests)
        if request.url.path.endswith('/decisions'):
            state = body['state']
            assert state['candidates']['low']['capability_card']['benchmark_evidence']['source'] == 'codexradar'
            assert len(body['questions']) == 1
            return httpx.Response(200, json={'id': f'gen-score-{i}', 'model': body['model'], 'provider': 'TypeSafe',
                'usage': {'cost': .0001, 'input_tokens': 1, 'output_tokens': 1},
                'answers': {'low_mid': {'type': 'choice', 'choice': 'low', 'probabilities': {'low': .8, 'mid': .2}}}})
        return httpx.Response(200, json={'id': f'gen-score-{i}', 'model': body['model'], 'provider': 'Example',
            'usage': {'cost': .01, 'prompt_tokens': 1, 'completion_tokens': 1},
            'choices': [{'message': {'role': 'assistant', 'content': '3' if i == 1 else '2'}, 'finish_reason': 'stop'}]})

    _, decision = example_config()
    client = Client(httpx.Client(transport=httpx.MockTransport(api)), 'offline-only', tmp_path)
    score_snapshot = snapshot()
    engine = ScoreCore(client, score_snapshot, REQUIREMENTS, decision, run_id='scores', clock=lambda: NOW)
    task = {'prompt': 'Return only the integer sum of 1 and 1.'}
    result = engine.run('scores', task, ArithmeticEnvironment, contract_version='v1')
    assert result['status'] == 'passed' and len(requests) == 3
    assert result['costs']['api_usd'] == '0.0201'
    # Original snapshot remains resumable after expiry; new run must not start.
    resumed = ScoreCore(client, score_snapshot, REQUIREMENTS, decision, run_id='scores', clock=lambda: LATER)
    resumed.run('scores', task, ArithmeticEnvironment, contract_version='v1')
    assert len(requests) == 3
    with pytest.raises(ValueError, match='stale'):
        ScoreCore(client, score_snapshot, REQUIREMENTS, decision, run_id='new', clock=lambda: LATER)
    with pytest.raises(ValueError, match='original'):
        ScoreCore(client, score_snapshot, {**REQUIREMENTS, 'context_tokens': 1001}, decision,
                  run_id='scores', clock=lambda: NOW)
    with pytest.raises(ValueError, match='one run'):
        engine.run('other', task, ArithmeticEnvironment, contract_version='v1')


def test_expiry_between_binding_and_start_stops_before_http(tmp_path):
    _, decision = example_config()
    client = Client(httpx.Client(transport=httpx.MockTransport(lambda r: pytest.fail('Must not dispatch'))),
                    'offline-only', tmp_path)
    times = iter((NOW, LATER))
    engine = ScoreCore(client, snapshot(), REQUIREMENTS, decision, run_id='delayed', clock=lambda: next(times))
    with pytest.raises(ValueError, match='stale'):
        engine.run('delayed', {'prompt': 'test'}, ArithmeticEnvironment, contract_version='v1')


def test_cli_build_select_and_missing_credential(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, 'utc_now', lambda: NOW)
    obs, registry, policy = inputs()
    for name, value in [('observations', obs), ('registry', registry), ('policy', policy), ('requirements', REQUIREMENTS)]:
        (tmp_path / (name + '.json')).write_text(json.dumps(value))
    store = str(tmp_path / 'store')
    args = ['build', '--store', store]
    for name in ('observations', 'registry', 'policy'):
        args += ['--' + name, str(tmp_path / (name + '.json'))]
    assert cli.main(args) == 0
    assert cli.main(['select', '--store', store, '--requirements', str(tmp_path / 'requirements.json'),
                     '--output', str(tmp_path / 'profiles.json')]) == 0
    assert cli.main(['inspect', '--store', store]) == 0
    monkeypatch.delenv('ARTIFICIAL_ANALYSIS_API_KEY', raising=False)
    assert cli.main(['fetch', 'artificial-analysis', '--output', str(tmp_path / 'missing.json')]) == 1
    assert 'KeyError' in capsys.readouterr().err


@pytest.mark.parametrize('route,count', [('pass', 1), ('low', 3), ('mid', 4), ('high', 4)])
def test_score_demo_all_paths(tmp_path, route, count):
    from cloudyfan_jev.scores.demo import main
    output = tmp_path / route
    assert main(['--output', str(output), '--route', route]) == 0
    report = json.loads((output / 'report.json').read_text())
    assert report['status'] == 'passed' and report['mock_requests'] == count
    assert report['real_api_calls'] == 0 and report['real_api_cost_usd'] == 0

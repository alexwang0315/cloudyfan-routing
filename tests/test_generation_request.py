"""Opt-in nonreasoning requests keep exact route, fee and SQL evidence identity."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib

import pytest

from cloudyfan_jev.budget import EnforcedBound
from cloudyfan_jev.client import CHAT
from cloudyfan_jev.generation_request import reasoning_fields, reasoning_matches, request_model
from cloudyfan_jev.receipt_store import _encoded
from cloudyfan_jev.scores.catalog import digest
from cloudyfan_jev.scores.configuration import configuration_digest
from cloudyfan_jev.scores.sql_capability import evidence_expiry, grade_model
from cloudyfan_jev.scores.sql_runner import CaptureStopped, QualificationRunner
from test_component_bounds import component_setup
from test_provider_bounds import generation_payload, signed, supplier_setup
from test_sql_model_qualification import fixture_proof, v2_fixture
from test_sql_model_runner import setup_runner


def fee_digest(value):
    return hashlib.sha256(_encoded(value)).hexdigest()


@pytest.mark.parametrize('profile,expected', [
    ({'effort': 'none'}, {'reasoning': {'effort': 'none'}}),
    ({'effort': 'low'}, {'reasoning': {'effort': 'low'}}),
    ({'effort': 'none', 'reasoning_parameter': 'effort'}, {'reasoning': {'effort': 'none'}}),
    ({'effort': 'none', 'reasoning_parameter': 'omit'}, {}),
])
def test_explicit_reasoning_wire_contract_preserves_legacy(profile, expected):
    assert reasoning_fields(profile) == expected
    assert reasoning_matches(profile, expected)
    assert not reasoning_matches(profile, {'reasoning': None})
    assert not reasoning_matches(profile, {**expected, 'reasoning_effort': 'none'})


@pytest.mark.parametrize('mode,effort', [('unknown', 'none'), (None, 'none'), ('omit', 'low')])
def test_unknown_or_conflicting_reasoning_selector_is_rejected(mode, effort):
    profile = {'effort': effort, 'reasoning_parameter': mode}
    with pytest.raises(ValueError):
        reasoning_fields(profile)
    with pytest.raises(ValueError):
        configuration_digest({'profile': profile})


def test_configuration_digest_preserves_all_legacy_values_and_binds_new_mode():
    profile = {'model_version': 'vendor/model:free', 'effort': 'none'}
    legacy = digest({k: profile.get(k) for k in (
        'model', 'model_version', 'accepted_models', 'effort', 'provider', 'provider_name',
        'endpoint', 'quantization', 'engine', 'weights_digest')})
    assert configuration_digest({'profile': profile}) == legacy
    assert request_model(profile) == 'vendor/model:free'
    explicit_effort = configuration_digest({'profile': {**profile, 'reasoning_parameter': 'effort'}})
    omit = configuration_digest({'profile': {**profile, 'reasoning_parameter': 'omit'}})
    assert len({legacy, explicit_effort, omit}) == 3
    assert not reasoning_matches(profile, {})


def nonreasoning_runner(tmp_path):
    previous, calls, store, table = setup_runner(tmp_path)
    table = deepcopy(table)
    config = table['configurations'][0]
    config['entry']['profile'].update(effort='none', reasoning_parameter='omit')
    config['configuration_sha256'] = configuration_digest(config['entry'])
    runner = QualificationRunner(previous.client, store, table, model_bounds=previous.bounds,
                                 verify_bound=lambda *args: True, synthetic=True)
    return runner, calls, table


def test_sql_capture_omits_reasoning_and_rejects_payload_or_table_changes(tmp_path):
    runner, calls, table = nonreasoning_runner(tmp_path)
    assert runner.capture('omit', max_new_requests=5)['status'] == 'paused'
    assert 'reasoning' not in calls[0] and 'reasoning' not in calls[4]
    assert calls[1]['reasoning'] == {'effort': table['configurations'][1]['entry']['profile']['effort']}
    payload = runner._payload('open_source', 'Reply OK.')
    for bad in ({'effort': 'none'}, None, {'enabled': False}):
        with pytest.raises(CaptureStopped, match='identity_changed'):
            runner._capture_call('omit', 'open_source', 'reject', {**payload, 'reasoning': bad})
    table['configurations'][0]['entry']['profile']['reasoning_parameter'] = 'effort'
    with pytest.raises(ValueError, match='digest mismatch'):
        QualificationRunner(runner.client, runner.store, table, model_bounds=runner.bounds,
                            verify_bound=lambda *args: True, synthetic=True)
    assert len(calls) == 5


def test_sql_grading_checks_saved_omission_and_mode_binding(tmp_path):
    runner, calls, _ = nonreasoning_runner(tmp_path)
    runner.capture('grade', max_new_requests=5)
    entry = runner.entries['open_source']
    rows = runner.records('grade', 'open_source')
    now = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    report = grade_model(entry, rows, now=now, synthetic=True)
    assert report['cases'][0]['final_pass'] is True
    changed = deepcopy(rows)
    attempt = changed[0]['attempts'][0]
    attempt['request']['reasoning'] = {'effort': 'none'}
    attempt['request_sha256'] = digest(attempt['request'])
    denied = grade_model(entry, changed, now=now, synthetic=True)
    assert denied['cases'][0]['outcome'] == 'engineering_or_evidence_gap'
    assert denied['qualified'] is False and len(calls) == 5


def test_api_identity_requires_explicit_new_reasoning_contract():
    entry = v2_fixture()['registry']['candidates'][0]
    entry['profile'].update(effort='none', reasoning_parameter='omit')
    proof = fixture_proof(entry)
    from test_first_pool import NOW
    with pytest.raises(ValueError, match='api_identity'):
        evidence_expiry(entry, proof, now=NOW, synthetic=True)
    proof['api_identity']['reasoning_parameter'] = 'omit'
    assert evidence_expiry(entry, proof, now=NOW, synthetic=True)
    proof['api_identity']['reasoning_parameter'] = 'effort'
    with pytest.raises(ValueError, match='api_identity'):
        evidence_expiry(entry, proof, now=NOW, synthetic=True)


def test_supplier_bound_requires_new_mode_hash_and_exact_wire_omission(tmp_path):
    engine, api, make, contracts, states, _ = supplier_setup(tmp_path)
    profile = engine.profiles['high']
    profile.update(effort='none', reasoning_parameter='omit')
    contract = next(c for c in contracts if c['body']['model_version'] == profile['model_version'])
    bound = EnforcedBound('.03', 'supplier-cap:' + contract['body']['id'])
    payload = generation_payload(engine)
    del payload['reasoning']
    assert make().verify_dispatch(engine.client, CHAT, payload, bound) is False
    contract['body']['configuration_sha256'] = fee_digest(profile)
    contract.update(signed(contract['body']))
    state = states[contract['body']['id']]['body']
    state['contract_sha256'] = fee_digest(contract['body'])
    states[contract['body']['id']] = signed(state)
    verifier = make()
    assert verifier.verify_dispatch(engine.client, CHAT, payload, bound)
    assert verifier.verify_post(engine.client, CHAT, payload, bound)
    for bad in ({'effort': 'none'}, None, {'effort': 'high'}):
        assert not verifier.verify_dispatch(engine.client, CHAT, {**payload, 'reasoning': bad}, bound)
    assert not verifier.verify_dispatch(engine.client, CHAT,
                                        {**payload, 'max_tokens': engine.limits.max_tokens + 1}, bound)
    assert api.requests == []


def test_component_bound_keeps_complete_billing_controls_when_reasoning_is_omitted(tmp_path):
    engine, api, schedules, reviews, _, make, _ = component_setup(tmp_path)
    profile = engine.profiles['high']
    profile.update(effort='none', reasoning_parameter='omit')
    schedule = next(s for s in schedules if s['model_version'] == profile['model_version'])
    review = next(r for r in reviews if r['evidence_sha256'] == fee_digest(schedule))
    with pytest.raises(ValueError):
        make().bound_for(profile['model_version'])
    schedule['configuration_sha256'] = fee_digest(profile)
    review['evidence_sha256'] = fee_digest(schedule)
    verifier = make()
    bound = verifier.bound_for(profile['model_version'])
    assert bound.usd == '0.0194840'
    payload = generation_payload(engine)
    del payload['reasoning']
    assert verifier.verify_dispatch(engine.client, CHAT, payload, bound)
    assert verifier.verify_post(engine.client, CHAT, payload, bound)
    assert not verifier.verify_dispatch(engine.client, CHAT, {**payload, 'reasoning': {}}, bound)
    assert not verifier.verify_dispatch(engine.client, CHAT,
                                        {**payload, 'max_tokens': engine.limits.max_tokens + 1}, bound)
    schedule['components'].pop('reasoning')
    review['evidence_sha256'] = fee_digest(schedule)
    with pytest.raises(ValueError, match='billing dimension'):
        make().bound_for(profile['model_version'])
    assert api.requests == []


def test_sql_free_route_is_never_rewritten_to_paid_response_identity(tmp_path):
    from test_sql_model_runner import response
    paid = 'vendor/pinned-model'
    free = paid + ':free'
    previous, calls, store, table = setup_runner(tmp_path,
        handler=lambda payload, n: response(payload, n, 'OK', model=paid))
    config = table['configurations'][0]
    profile = config['entry']['profile']
    profile.update(model=free, model_version=free, accepted_models=[free],
                   effort='none', reasoning_parameter='omit')
    config['configuration_sha256'] = configuration_digest(config['entry'])
    bounds = {**previous.bounds, free: EnforcedBound('0.01', 'synthetic-cap')}
    runner = QualificationRunner(previous.client, store, table, model_bounds=bounds,
                                 verify_bound=lambda *args: True, synthetic=True)
    result = runner.capture('free', max_new_requests=1)
    assert result['status'] == 'stopped' and result['reason'] == 'model_or_provider_mismatch'
    assert len(calls) == 1 and calls[0]['model'] == free and 'reasoning' not in calls[0]
    assert result['known_api_usd'] == '0.001'

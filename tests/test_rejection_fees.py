"""Archived fee-policy evidence crosses Client/Core/ledger without real HTTP."""
from datetime import datetime, timezone
import hashlib
import json

import httpx
import pytest

from cloudyfan_jev.client import Client, usage
from cloudyfan_jev.demo import ArithmeticEnvironment, example_config
from cloudyfan_jev.engine import Core, Limits
from cloudyfan_jev.ledger import summarize
from cloudyfan_jev.rejection_fees import Text429FeePolicy
from test_engine import TASK
from test_provider_rejection import RateLimitedAPI

NOW = datetime(2026, 10, 20, tzinfo=timezone.utc)


def setup(tmp_path, *, mutate=None, cost=None):
    source = tmp_path/'policy.html'; source.write_text(Text429FeePolicy.EXCERPT)
    profiles, decision = example_config()
    policy = Text429FeePolicy(source, hashlib.sha256(source.read_bytes()).hexdigest(),
        [{'model': p['model_version'], 'provider': p['provider'], 'provider_name': p['provider_name']}
         for p in profiles.values()], valid_until='2026-10-21T00:00:00+00:00', clock=lambda: NOW)
    def scope(data):
        data['error']['metadata'].update(is_byok=False, limit_source='upstream_provider_shared_pool')
        if mutate: mutate(data)
    api = RateLimitedAPI(cost=cost, mutate=scope)
    client = Client(httpx.Client(transport=httpx.MockTransport(api)), 'mock-only', tmp_path/'receipts', rejection_fee_policy=policy)
    return Core(client, profiles, decision, limits=Limits(max_turns=1)), api, policy


def test_realistic_429_404_policy_evidence_allows_core_upgrade_without_fake_receipt(tmp_path):
    engine, api, policy = setup(tmp_path)
    result = engine.run('policy', TASK, ArithmeticEnvironment, contract_version='v1')
    assert result['status'] == 'passed' and len(api.posts) == 3 and len(api.gets) == 1
    first = result['attempts'][0]['calls'][0]
    assert first['cost_source'] == 'provider_error_no_charge_policy' and first['cost_usd'] == '0'
    assert first['model'] is None
    directory = engine.client.root/'calls/run-policy-a0-t0'
    assert (directory/'rejection-fee.json').exists() and not (directory/'reconciled.json').exists()
    assert json.loads((directory/'response.json').read_text())['http_status'] == 429
    assert summarize(engine.client)['total_api_usd'] == '0.0101'
    assert engine.configuration_descriptor()['rejection_fee_policy'] == policy.descriptor()
    assert engine.run('policy', TASK, ArithmeticEnvironment, contract_version='v1') == result
    assert len(api.posts) == 3


@pytest.mark.parametrize('mutate', [
    lambda d:d['error']['metadata'].update(is_byok=True),
    lambda d:d['error']['metadata'].update(limit_source='unknown'),
    lambda d:d['error']['metadata'].update(provider_name='Other'),
    lambda d:d.update(choices=[{'message':{'content':'partial'}}]),
])
def test_unqualified_error_stays_unknown_and_stops(tmp_path, mutate):
    engine, api, _ = setup(tmp_path, mutate=mutate)
    with pytest.raises(RuntimeError, match='Dispatch stopped'):
        engine.run('blocked', TASK, ArithmeticEnvironment, contract_version='v1')
    assert len(api.posts) == 1 and summarize(engine.client)['unknown_calls'] == 1


def test_nonzero_response_fee_and_later_generation_receipt_are_authoritative(tmp_path):
    engine, api, _ = setup(tmp_path, cost='.002')
    result = engine.run('actual', TASK, ArithmeticEnvironment, contract_version='v1', mode='low_first')
    assert result['costs']['api_usd'] == '0.012'
    assert not (engine.client.root/'calls/run-actual-a0-t0/rejection-fee.json').exists()
    api.receipt = True
    engine.client.reconcile('run-actual-a0-t0')
    assert engine.read_completed('actual')['costs']['api_usd'] == '0.013'


def test_actual_receipt_overrides_policy_zero_never_adds(tmp_path):
    engine, api, _ = setup(tmp_path)
    engine.run('override', TASK, ArithmeticEnvironment, contract_version='v1', mode='low_first')
    api.receipt = True
    engine.client.reconcile('run-override-a0-t0')
    report = summarize(engine.client)
    assert report['total_api_usd'] == '0.013'
    assert report['rows'][0]['cost_source'] == 'generation_total_cost'


@pytest.mark.parametrize('invalid', ['expiry', 'source'])
def test_actual_receipt_can_replace_policy_zero_after_policy_becomes_invalid(tmp_path, invalid):
    engine, api, policy = setup(tmp_path)
    engine.run('late-receipt', TASK, ArithmeticEnvironment, contract_version='v1', mode='low_first')
    directory = engine.client.root/'calls/run-late-receipt-a0-t0'
    original = (directory/'response.json').read_bytes()
    proof = (directory/'rejection-fee.json').read_bytes()
    if invalid == 'expiry':
        policy.clock = lambda: datetime(2026, 10, 22, tzinfo=timezone.utc)
    else:
        policy.source_path.write_text('archive changed')
    api.receipt = True
    before_gets, before_posts = len(api.gets), len(api.posts)
    actual = engine.client.reconcile('run-late-receipt-a0-t0')
    assert len(api.gets) == before_gets + 1 and len(api.posts) == before_posts
    assert usage(actual)['cost_source'] == 'generation_total_cost'
    assert usage(actual)['cost_usd'] == '0.003'
    assert summarize(engine.client)['total_api_usd'] == '0.013'
    assert (directory/'response.json').read_bytes() == original
    assert (directory/'rejection-fee.json').read_bytes() == proof


@pytest.mark.parametrize('derived', ['rejection_fee_evidence', 'reconciliation'])
def test_embedded_derived_fee_fields_cannot_bypass_saved_proof_validation(tmp_path, derived):
    engine, api, policy = setup(tmp_path)
    engine.run('embedded', TASK, ArithmeticEnvironment, contract_version='v1', mode='low_first')
    directory = engine.client.root/'calls/run-embedded-a0-t0'
    raw = json.loads((directory/'response.json').read_text())
    proof = json.loads((directory/'rejection-fee.json').read_text())
    raw[derived] = proof if derived == 'rejection_fee_evidence' else {
        'verified': True, 'generation_id': proof['generation_id'], 'total_cost': '0'}
    raw['elapsed_ms'] = 999999
    (directory/'response.json').write_text(json.dumps(raw))
    policy.clock = lambda: datetime(2026, 10, 22, tzinfo=timezone.utc)
    with pytest.raises(ValueError):
        engine.client._load(directory)
    from cloudyfan_jev.client import CHAT, UnresolvedCallsError
    with pytest.raises(UnresolvedCallsError):
        engine.client.call('blocked-new', CHAT, {'model': 'example/low'})
    assert len(api.posts) == 2


def test_invalid_policy_and_failed_receipt_get_remain_unknown_and_block_posts(tmp_path):
    from cloudyfan_jev.client import CHAT, UnresolvedCallsError
    engine, api, policy = setup(tmp_path)
    engine.run('unavailable', TASK, ArithmeticEnvironment, contract_version='v1', mode='low_first')
    policy.clock = lambda: datetime(2026, 10, 22, tzinfo=timezone.utc)
    before_gets = len(api.gets)
    with pytest.raises(ValueError):
        engine.client.reconcile('run-unavailable-a0-t0')
    assert len(api.gets) == before_gets + 1
    assert summarize(engine.client)['unknown_calls'] == 1
    with pytest.raises(UnresolvedCallsError):
        engine.client.call('unknown-blocked', CHAT, {'model': 'example/low'})
    assert len(api.posts) == 2


@pytest.mark.parametrize('change', ['tools', 'plugins', 'stream', 'fallback', 'attachment'])
def test_auxiliary_or_unfrozen_request_has_no_zero_fee_policy(tmp_path, change):
    engine, api, policy = setup(tmp_path)
    payload = {'model': 'example/open_source', 'messages': [{'role':'user','content':'text'}],
               'provider':{'only':['example'],'allow_fallbacks':False,'require_parameters':True}}
    if change in ('tools','plugins'): payload[change] = []
    if change == 'stream': payload['stream'] = True
    if change == 'fallback': payload['provider']['allow_fallbacks'] = True
    if change == 'attachment': payload['messages'][0]['content'] = [{'type':'text','text':'text'}]
    record = engine.client.call('scope', 'https://openrouter.ai/api/v1/chat/completions', payload)
    intent = json.loads((engine.client.root/'calls/scope/intent.json').read_text())
    assert policy.evidence(intent, record) is None and usage(record)['cost_unknown']


@pytest.mark.parametrize('change', ['source','expiry','proof','response'])
def test_tampered_or_expired_evidence_blocks_replay_or_new_posts(tmp_path, change):
    engine, api, policy = setup(tmp_path)
    engine.run('tamper', TASK, ArithmeticEnvironment, contract_version='v1', mode='low_first')
    directory = engine.client.root/'calls/run-tamper-a0-t0'
    if change == 'source': policy.source_path.write_text('changed')
    if change == 'expiry': policy.clock = lambda: datetime(2026,10,22,tzinfo=timezone.utc)
    if change == 'proof':
        value=json.loads((directory/'rejection-fee.json').read_text());value['cost_usd']='1'
        (directory/'rejection-fee.json').write_text(json.dumps(value))
    if change == 'response':
        value=json.loads((directory/'response.json').read_text());value['elapsed_ms']=100000
        (directory/'response.json').write_text(json.dumps(value))
    with pytest.raises((RuntimeError,ValueError)):
        engine.read_completed('tamper')
    assert len(api.posts) == 2

"""Provider enforcement is verified at the real budget/transport seam, offline."""
from datetime import datetime, timezone
from decimal import Decimal

import httpx
import pytest

from cloudyfan_jev.budget import Authorization, BudgetError, BudgetStore, BudgetedCore, EnforcedBound
from cloudyfan_jev.provider_bounds import OpenRouterBoundVerifier
from test_engine import ArithmeticEnvironment, TASK, core

NOW = datetime(2026, 10, 18, tzinfo=timezone.utc).timestamp()


def test_key_credit_and_token_limit_alone_never_authorize_a_paid_request(tmp_path):
    requests = []
    def api(request):
        requests.append(request.method)
        assert request.method == 'GET'
        return httpx.Response(200, json={'data': {'limit': 1, 'limit_remaining': 1, 'usage': 0,
            'limit_reset': None, 'include_byok_in_limit': True, 'expires_at': '2099-01-01T00:00:00Z'}})
    engine = core(tmp_path / 'client', api)
    verifier = OpenRouterBoundVerifier(engine.client, clock=lambda: NOW)
    assert verifier.key_status()['credit_limit_usd'] == '1'
    store = BudgetStore(tmp_path / 'budget.sqlite', clock=lambda: datetime.fromtimestamp(NOW, timezone.utc))
    store.authorize(Authorization('offline fixture', Decimal('2'), '2099-01-01T00:00:00Z'))
    bounds = {p['model_version']: EnforcedBound('.03', 'fixture-only') for p in engine.profiles.values()}
    app = BudgetedCore(engine, store, model_bounds=bounds, tool_bounds={}, verify_bound=verifier)
    with pytest.raises(BudgetError, match='unconfirmed'):
        app.run('not-authorized', TASK, ArithmeticEnvironment, contract_version='v1', mode='fixed_high')
    assert requests == ['GET']
    assert verifier.last_check['reason'] == 'supplier_ceiling_unconfirmed'


def signed(body, secret=b'supplier-fixture-secret-not-a-real-key' * 2):
    import hashlib
    import hmac
    from cloudyfan_jev.receipt_store import _encoded
    return {'body': body, 'signature': hmac.new(secret, _encoded(body), hashlib.sha256).hexdigest()}


def supplier_setup(tmp_path, *, mutable_clock=None, api=None):
    import hashlib
    from cloudyfan_jev.client import CHAT, DECISIONS
    from cloudyfan_jev.provider_bounds import credential_fingerprint, ALL_CHARGES
    from cloudyfan_jev.receipt_store import _encoded
    from test_engine import API
    clock = mutable_clock or [NOW]
    model_api = api or API(first='2')
    gets = []
    def transport(request):
        if request.method == 'GET':
            gets.append(request.url.path)
            return httpx.Response(200, json={'data': {'limit': 10, 'limit_remaining': 10, 'usage': 0,
                'limit_reset': None, 'include_byok_in_limit': True, 'expires_at': '2099-01-01T00:00:00Z'}})
        return model_api(request)
    engine = core(tmp_path / 'client', transport)
    configs = {p['model_version']: p for p in engine.profiles.values()}
    configs[engine.decision['version']] = engine.decision
    policies, contracts, states = {}, [], {}
    sha = lambda v: hashlib.sha256(_encoded(v)).hexdigest()
    for index, (model, config) in enumerate(configs.items()):
        endpoint = DECISIONS if 'version' in config else CHAT
        policy = {'endpoint': endpoint, 'max_tokens': None if endpoint == DECISIONS else engine.limits.max_tokens,
                  'max_payload_bytes': 131072, 'tools': []}
        policies[model] = policy
        cid = 'synthetic-cap-' + str(index)
        body = {'schema_version': 1, 'id': cid, 'issuer': 'synthetic-supplier',
            'credential_sha256': credential_fingerprint(engine.client.key), 'model_version': model,
            'configuration_sha256': sha(config), 'request_policy_sha256': sha(policy), 'endpoint': endpoint,
            'mechanism': 'provider_all_charge_per_request_ceiling', 'ceiling_usd': '.03',
            'covers': sorted(ALL_CHARGES), 'issued_at': NOW - 1, 'expires_at': NOW + 300,
            'enforcement_sha256': 'a' * 64, 'evidence_sha256': 'b' * 64}
        contracts.append(signed(body))
        states[cid] = signed({'schema_version': 1, 'issuer': body['issuer'], 'contract_sha256': sha(body),
            'active': True, 'credential_sha256': body['credential_sha256'], 'enforcement_sha256': 'a' * 64,
            'ceiling_usd': '.03', 'covers': sorted(ALL_CHARGES), 'observed_at': NOW,
            'expires_at': NOW + 30})
    def make_verifier():
        return OpenRouterBoundVerifier(engine.client, clock=lambda: clock[0], contracts=contracts,
            issuer_keys={'synthetic-supplier': b'supplier-fixture-secret-not-a-real-key' * 2},
            configurations=configs, request_policies=policies,
            enforcement_probe=lambda contract: states[contract['id']])
    return engine, model_api, make_verifier, contracts, states, clock


def test_authenticated_supplier_ceiling_admits_pinned_complete_workflow(tmp_path):
    engine, api, make, contracts, states, clock = supplier_setup(tmp_path)
    verifier = make()
    store = BudgetStore(tmp_path / 'budget.sqlite', clock=lambda: datetime.fromtimestamp(clock[0], timezone.utc))
    store.authorize(Authorization('offline fixture', Decimal('2'), '2099-01-01T00:00:00Z'))
    bounds = {c['body']['model_version']: EnforcedBound('.03', 'supplier-cap:' + c['body']['id']) for c in contracts}
    result = BudgetedCore(engine, store, model_bounds=bounds, tool_bounds={}, verify_bound=verifier).run(
        'qualified-fixture', TASK, ArithmeticEnvironment, contract_version='v1', mode='fixed_high')
    assert result['final_public_pass'] is True
    assert result['budget']['run_total_usd'] == '0.01'
    assert len(api.requests) == 1
    assert verifier.last_check['allowed'] is True


@pytest.mark.parametrize('change', ['signature', 'issuer', 'credential', 'covers', 'expired', 'mechanism',
                                   'state_signature', 'revoked', 'state_stale', 'state_future', 'state_ceiling', 'state_config'])
def test_unqualified_supplier_or_enforcement_state_stops_before_model_post(tmp_path, change):
    engine, api, make, contracts, states, clock = supplier_setup(tmp_path)
    contract = next(c for c in contracts if c['body']['model_version'] == 'example/high')
    body = contract['body']
    if change == 'signature':
        contract['signature'] = '0' * 64
    elif change == 'issuer':
        body['issuer'] = 'untrusted-supplier'
    elif change == 'credential':
        body['credential_sha256'] = 'f' * 64
    elif change == 'covers':
        body['covers'].remove('advisor')
    elif change == 'expired':
        body['expires_at'] = NOW
    elif change == 'mechanism':
        body['mechanism'] = 'key_credit_limit'
    else:
        state = states[body['id']]['body']
        if change == 'state_signature':
            states[body['id']]['signature'] = '0' * 64
        elif change == 'revoked':
            state['active'] = False
        elif change == 'state_stale':
            state['observed_at'] = NOW - 31
        elif change == 'state_future':
            state['observed_at'] = NOW + 1
        elif change == 'state_ceiling':
            state['ceiling_usd'] = '.04'
        elif change == 'state_config':
            state['enforcement_sha256'] = 'f' * 64
        if change != 'state_signature':
            states[body['id']] = signed(state)
    if change not in ('signature', 'issuer') and not change.startswith('state') and change != 'revoked':
        contract.update(signed(body))
    verifier = make()
    assert verifier('model', 'example/high', engine.profiles['high'],
                    EnforcedBound('.03', 'supplier-cap:' + body['id'])) is False
    assert api.requests == []


def generation_payload(engine):
    return {'model': 'example/high', 'messages': [{'role': 'user', 'content': '1+1'}],
            'max_tokens': engine.limits.max_tokens, 'reasoning': {'effort': 'low'}, 'session_id': 'test',
            'provider': {'only': ['example'], 'allow_fallbacks': False, 'require_parameters': True}}


@pytest.mark.parametrize('change', ['endpoint', 'key', 'other_client', 'fallback', 'provider', 'plugins',
                                   'router_models', 'max_tokens', 'effort', 'tools', 'huge_input', 'profile'])
def test_actual_dispatch_must_match_approved_transport_and_request(tmp_path, change):
    from cloudyfan_jev.client import CHAT, DECISIONS
    engine, api, make, contracts, states, clock = supplier_setup(tmp_path)
    verifier = make()
    cid = next(c['body']['id'] for c in contracts if c['body']['model_version'] == 'example/high')
    bound = EnforcedBound('.03', 'supplier-cap:' + cid)
    payload = generation_payload(engine)
    client, endpoint = engine.client, CHAT
    if change == 'endpoint': endpoint = DECISIONS
    elif change == 'key': client.key = 'different-credential'
    elif change == 'other_client': client = core(tmp_path / 'other', api).client
    elif change == 'fallback': payload['provider']['allow_fallbacks'] = True
    elif change == 'provider': payload['provider']['only'] = ['different-provider']
    elif change == 'plugins': payload['plugins'] = [{'id': 'web'}]
    elif change == 'router_models': payload['models'] = ['example/high', 'other/model']
    elif change == 'max_tokens': payload['max_tokens'] += 1
    elif change == 'effort': payload['reasoning'] = {'effort': 'high'}
    elif change == 'tools': payload['tools'] = [{'type': 'function'}]
    elif change == 'huge_input': payload['messages'][0]['content'] = 'x' * 131073
    elif change == 'profile': payload['model'] = 'other/model'
    assert verifier.verify_dispatch(client, endpoint, payload, bound) is False
    assert api.requests == []


def test_cap_expires_before_classifier_without_resending_first_answer(tmp_path):
    from test_engine import API
    clock = [NOW]
    underlying = API(first='3')
    def expire_after_first(request):
        result = underlying(request)
        clock[0] = NOW + 301
        return result
    engine, _, make, contracts, states, clock = supplier_setup(tmp_path, mutable_clock=clock, api=expire_after_first)
    store = BudgetStore(tmp_path / 'budget.sqlite', clock=lambda: datetime.fromtimestamp(clock[0], timezone.utc))
    store.authorize(Authorization('offline fixture', Decimal('2'), '2099-01-01T00:00:00Z'))
    bounds = {c['body']['model_version']: EnforcedBound('.03', 'supplier-cap:' + c['body']['id']) for c in contracts}
    app = BudgetedCore(engine, store, model_bounds=bounds, tool_bounds={}, verify_bound=make())
    with pytest.raises(BudgetError, match='dispatch upper bound'):
        app.run('expires', TASK, ArithmeticEnvironment, contract_version='v1')
    assert len(underlying.requests) == 1
    assert store.report()['known_usd'] == '0.01'
    assert store.report()['reservations'][0]['status'] == 'active'
    assert (engine.client.root / 'calls/run-expires-a0-t0/response.json').is_file()


@pytest.mark.parametrize('body,status', [
    ({'limit': None}, 200), ({'limit_remaining': 0}, 200), ({'include_byok_in_limit': False}, 200),
    ({'limit_reset': 'daily'}, 200), ({'disabled': True}, 200), ({'expires_at': '2020-01-01T00:00:00Z'}, 200),
    ({'limit_remaining': 11}, 200), ({'usage': -1}, 200), ({'usage': True}, 200), ({}, 302), ({}, 429)])
def test_key_metadata_is_necessary_but_never_a_hard_cap(tmp_path, body, status):
    data = {'limit': 10, 'limit_remaining': 10, 'usage': 0, 'limit_reset': None,
            'include_byok_in_limit': True, 'expires_at': '2099-01-01T00:00:00Z', **body}
    engine = core(tmp_path / 'client', lambda request: httpx.Response(status, json={'data': data}))
    check = OpenRouterBoundVerifier(engine.client, clock=lambda: NOW).key_status()
    assert check['available'] is False
    assert check['hard_per_request_cap'] is False


def test_key_inspection_latency_cannot_extend_supplier_expiry(tmp_path):
    from cloudyfan_jev.client import CHAT
    engine, api, make, contracts, states, clock = supplier_setup(tmp_path)
    def slow_key(request):
        clock[0] = NOW + 400
        return httpx.Response(200, json={'data': {'limit': 10, 'limit_remaining': 10, 'usage': 0,
            'limit_reset': None, 'include_byok_in_limit': True, 'expires_at': '2099-01-01T00:00:00Z'}})
    engine.client.http = httpx.Client(transport=httpx.MockTransport(slow_key))
    verifier = make()
    cid = next(c['body']['id'] for c in contracts if c['body']['model_version'] == 'example/high')
    assert verifier.verify_dispatch(engine.client, CHAT, generation_payload(engine),
                                   EnforcedBound('.03', 'supplier-cap:' + cid)) is False
    assert api.requests == []


def test_expiry_during_intent_durability_blocks_post_and_records_zero_dispatch_fee(tmp_path, monkeypatch):
    import os
    engine, api, make, contracts, states, clock = supplier_setup(tmp_path)
    verifier = make()
    store = BudgetStore(tmp_path / 'budget.sqlite', clock=lambda: datetime.fromtimestamp(clock[0], timezone.utc))
    store.authorize(Authorization('offline fixture', Decimal('2'), '2099-01-01T00:00:00Z'))
    bounds = {c['body']['model_version']: EnforcedBound('.03', 'supplier-cap:' + c['body']['id']) for c in contracts}
    real_fsync = os.fsync
    def delayed_durability(fd):
        real_fsync(fd)
        if list((engine.client.root / 'calls').glob('*/intent.json')):
            clock[0] = NOW + 400
    monkeypatch.setattr(os, 'fsync', delayed_durability)
    app = BudgetedCore(engine, store, model_bounds=bounds, tool_bounds={}, verify_bound=verifier)
    with pytest.raises((BudgetError, RuntimeError)):
        app.run('expired-intent', TASK, ArithmeticEnvironment, contract_version='v1', mode='fixed_high')
    assert api.requests == []
    report = store.report()
    assert report['unknown_events'] == 0 and report['known_usd'] == '0'
    assert report['events'][0]['verified'] == 1
    assert report['events'][0]['receipt'].startswith('not-dispatched:')
    app.sync_client(engine.client)
    assert store.report()['known_usd'] == '0'


def test_both_jev_stages_and_final_repair_use_same_authenticated_cap_contracts(tmp_path):
    from test_engine import API
    engine, api, make, contracts, states, clock = supplier_setup(tmp_path, api=API(first='3', choices=('mid', 'high')))
    store = BudgetStore(tmp_path / 'budget.sqlite', clock=lambda: datetime.fromtimestamp(clock[0], timezone.utc))
    store.authorize(Authorization('offline fixture', Decimal('2'), '2099-01-01T00:00:00Z'))
    bounds = {c['body']['model_version']: EnforcedBound('.03', 'supplier-cap:' + c['body']['id']) for c in contracts}
    app = BudgetedCore(engine, store, model_bounds=bounds, tool_bounds={}, verify_bound=make())
    result = app.run('full-tree', TASK, ArithmeticEnvironment, contract_version='v1')
    assert result['final_public_pass'] is True
    assert [p['model'] for p in api.requests] == ['example/open_source', 'typesafe/jev-1.13', 'example/mid',
                                                'typesafe/jev-1.13', 'example/high']
    assert result['budget']['run_total_usd'] == '0.0302'
    assert result['selection']['classifier_calls'] == 2
    assert app.enforcement_descriptor()['version'] == 'supplier-ceiling-v1'


def test_dispatch_lease_is_exact_and_single_use(tmp_path):
    from cloudyfan_jev.client import CHAT
    engine, api, make, contracts, states, clock = supplier_setup(tmp_path)
    verifier, payload = make(), generation_payload(engine)
    cid = next(c['body']['id'] for c in contracts if c['body']['model_version'] == 'example/high')
    bound = EnforcedBound('.03', 'supplier-cap:' + cid)
    assert verifier.verify_dispatch(engine.client, CHAT, payload, bound) is True
    assert verifier.verify_post(engine.client, CHAT, payload, bound) is True
    assert verifier.verify_post(engine.client, CHAT, payload, bound) is False
    assert api.requests == []

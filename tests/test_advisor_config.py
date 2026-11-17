"""#72：顧問先答路徑（路徑二）的設定、凍結、模式註冊、預算預留與角色記帳。

全部離線：只用 httpx.MockTransport，不呼叫任何真實模型。
"""

import httpx
import pytest

from cloudyfan_jev.advisor import ADVISOR_FIRST, AdvisorConfig
from cloudyfan_jev.client import Client
from cloudyfan_jev.demo import ArithmeticEnvironment, example_config
from cloudyfan_jev.engine import Core

TASK = {'prompt': 'Return only the integer sum of 1 and 1.'}


def role_profile(name, *, open_source=True):
    return {'model': 'oss/' + name, 'model_version': 'oss/' + name + '-v1',
            'accepted_models': ['oss/' + name + '-v1'], 'provider': 'example',
            'provider_name': 'Example', 'effort': 'medium', 'open_source': open_source}


def advisor_config(**overrides):
    fields = {'advisor': role_profile('advisor'), 'judge': role_profile('judge'),
              'score_snapshot': 'snapshot-20261117-a'}
    fields.update(overrides)
    return AdvisorConfig(**fields)


def passing_api(requests):
    def handler(request):
        import json
        p = json.loads(request.content)
        requests.append(p)
        return httpx.Response(200, json={'id': f'gen-{len(requests)}', 'model': p['model'],
            'provider': 'Example', 'usage': {'cost': .01},
            'choices': [{'message': {'role': 'assistant', 'content': '2'}, 'finish_reason': 'stop'}]})
    return handler


def make_core(tmp_path, handler, advisor=None):
    http = httpx.Client(transport=httpx.MockTransport(handler))
    profiles, decision = example_config()
    return Core(Client(http, 'mock-only-key', tmp_path), profiles, decision, advisor_first=advisor)


@pytest.mark.parametrize('role', ['advisor', 'judge'])
@pytest.mark.parametrize('field', ['model', 'model_version', 'effort', 'provider', 'provider_name'])
def test_role_profile_requires_explicit_identity_fields(role, field):
    profile = role_profile(role)
    del profile[field]
    with pytest.raises(ValueError, match=field):
        advisor_config(**{role: profile})


@pytest.mark.parametrize('value', [None, 'yes', 1])
def test_role_profile_requires_explicit_open_source_boolean(value):
    profile = role_profile('advisor')
    profile['open_source'] = value
    with pytest.raises(ValueError, match='open_source'):
        advisor_config(advisor=profile)


def test_pinned_role_version_must_be_accepted():
    profile = role_profile('advisor')
    profile['accepted_models'] = ['oss/other']
    with pytest.raises(ValueError, match='accepted'):
        advisor_config(advisor=profile)


@pytest.mark.parametrize('bad', [0, 4, True, '3'])
def test_max_attempts_is_bounded_integer(bad):
    with pytest.raises(ValueError, match='max_attempts'):
        advisor_config(max_attempts=bad)


def test_score_snapshot_identity_required():
    with pytest.raises(ValueError, match='score_snapshot'):
        advisor_config(score_snapshot='')


def test_without_advisor_config_descriptor_and_modes_are_unchanged(tmp_path):
    plain = make_core(tmp_path / 'a', passing_api([]))
    assert 'advisor_first' not in plain.configuration_descriptor()
    assert ADVISOR_FIRST not in plain.configuration_descriptor()['generation_attempt_limits']
    with pytest.raises(ValueError, match='Unknown workflow mode'):
        plain.run('r', TASK, ArithmeticEnvironment, contract_version='v1', mode=ADVISOR_FIRST)
    assert not (tmp_path / 'a' / 'runs' / 'r').exists()


def test_advisor_config_is_frozen_in_descriptor_with_defaults(tmp_path):
    engine = make_core(tmp_path, passing_api([]), advisor_config())
    frozen = engine.configuration_descriptor()['advisor_first']
    assert frozen['version'] == 'advisor-first-config-v1'
    assert frozen['advisor'] == role_profile('advisor')
    assert frozen['judge'] == role_profile('judge')
    assert frozen['score_snapshot'] == 'snapshot-20261117-a'
    assert frozen['max_attempts'] == 3
    assert frozen['judge_calls_per_attempt'] == 1
    # 沒指定池時，凍結 Core 現行的低／中／高設定作為顧問可建議的池內設定。
    assert sorted(frozen['pool']) == ['high', 'low', 'mid']
    assert frozen['pool']['mid']['model_version'] == 'example/mid'


def test_descriptor_is_a_copy_not_live_configuration(tmp_path):
    config = advisor_config()
    engine = make_core(tmp_path, passing_api([]), config)
    engine.configuration_descriptor()['advisor_first']['advisor']['model'] = 'tampered'
    assert engine.configuration_descriptor()['advisor_first']['advisor']['model'] == 'oss/advisor'


def test_pool_entries_must_name_low_mid_or_high_tier(tmp_path):
    bad_pool = {'x': {**example_config()[0]['mid'], 'tier': 'open_source'}}
    with pytest.raises(ValueError, match='tier'):
        make_core(tmp_path, passing_api([]), advisor_config(pool=bad_pool))


def test_advisor_first_mode_is_registered_but_flow_not_yet_dispatching(tmp_path):
    requests = []
    engine = make_core(tmp_path, passing_api(requests), advisor_config())
    assert ADVISOR_FIRST in engine.workflow_modes()
    with pytest.raises(NotImplementedError):
        engine.run('r', TASK, ArithmeticEnvironment, contract_version='v1', mode=ADVISOR_FIRST)
    assert requests == []


def test_legacy_run_with_advisor_config_freezes_it_and_rejects_changed_config(tmp_path):
    requests = []
    engine = make_core(tmp_path, passing_api(requests), advisor_config())
    first = engine.run('legacy', TASK, ArithmeticEnvironment, contract_version='v1')
    assert first['status'] == 'passed' and len(requests) == 1
    assert engine.read_completed('legacy')['run_id'] == 'legacy'
    changed = make_core(tmp_path, passing_api(requests),
                        advisor_config(score_snapshot='snapshot-20261118-b'))
    with pytest.raises(ValueError, match='configuration changed'):
        changed.read_completed('legacy')
    with pytest.raises(ValueError, match='Resume evidence changed'):
        changed.run('legacy', TASK, ArithmeticEnvironment, contract_version='v1')
    removed = make_core(tmp_path, passing_api(requests))
    with pytest.raises(ValueError, match='configuration changed'):
        removed.read_completed('legacy')
    assert len(requests) == 1


def test_old_run_without_advisor_still_reads_with_plain_core(tmp_path):
    requests = []
    plain = make_core(tmp_path, passing_api(requests))
    plain.run('old', TASK, ArithmeticEnvironment, contract_version='v1')
    assert make_core(tmp_path, passing_api(requests)).read_completed('old')['status'] == 'passed'
    with pytest.raises(ValueError, match='configuration changed'):
        make_core(tmp_path, passing_api(requests), advisor_config()).read_completed('old')


# ---- 預算預留（ADR 0034 第 6 點）----

def budgeted(tmp_path, advisor, bounds, *, tool_bounds=None, verified=None):
    from datetime import datetime, timezone
    from cloudyfan_jev.budget import BudgetedCore, BudgetStore
    from cloudyfan_jev.engine import Limits
    http = httpx.Client(transport=httpx.MockTransport(lambda _: pytest.fail('No request expected')))
    profiles, decision = example_config()
    engine = Core(Client(http, 'mock-only-key', tmp_path / 'client'), profiles, decision,
                  limits=Limits(max_turns=2), advisor_first=advisor)
    store = BudgetStore(tmp_path / 'budget.sqlite',
                        clock=lambda: datetime(2026, 11, 17, tzinfo=timezone.utc))
    seen = verified if verified is not None else []
    def verify(kind, identity, payload, bound):
        seen.append(identity)
        return True
    return BudgetedCore(engine, store, model_bounds=bounds, tool_bounds=tool_bounds or {},
                        verify_bound=verify)


def full_bounds():
    from cloudyfan_jev.budget import EnforcedBound
    usd = {'example/open_source': '.03', 'example/low': '.01', 'example/mid': '.02',
           'example/high': '.05', 'typesafe/jev-1.13': '.001',
           'oss/advisor-v1': '.002', 'oss/judge-v1': '.0005'}
    return {k: EnforcedBound(v, 'offline hard-cap fixture') for k, v in usd.items()}


def test_advisor_first_reservation_covers_advisor_attempts_and_judge(tmp_path):
    from decimal import Decimal
    seen = []
    app = budgeted(tmp_path, advisor_config(), full_bounds(), verified=seen)
    # 顧問 1 次 .002 ＋ 3 次作答 × 每次最多 2 回合 × 池內最高 .05 ＋ 3 次作答 × 裁判 1 次 × .0005
    assert app._admit(ADVISOR_FIRST) == Decimal('0.3035')
    assert {'oss/advisor-v1', 'oss/judge-v1', 'example/low', 'example/mid', 'example/high'} <= set(seen)
    assert 'example/open_source' not in seen and 'typesafe/jev-1.13' not in seen


def test_advisor_first_reservation_counts_external_tool_bound_per_turn(tmp_path):
    from decimal import Decimal
    from cloudyfan_jev.budget import EnforcedBound, ToolBound
    tools = {'search': ToolBound(EnforcedBound('.001', 'offline tool cap'), external=True)}
    app = budgeted(tmp_path, advisor_config(max_attempts=1, judge_calls_per_attempt=0),
                   full_bounds(), tool_bounds=tools)
    # 顧問 .002 ＋ 1×2×.05 ＋ 工具 1 次作答 × 2 回合 × 16 次 × .001
    assert app._admit(ADVISOR_FIRST) == Decimal('0.134')


@pytest.mark.parametrize('missing', ['oss/advisor-v1', 'oss/judge-v1', 'example/high'])
def test_advisor_first_refuses_without_confirmed_upper_bound(tmp_path, missing):
    from cloudyfan_jev.budget import BudgetError
    bounds = full_bounds()
    del bounds[missing]
    with pytest.raises(BudgetError, match='upper bound'):
        budgeted(tmp_path, advisor_config(), bounds)._admit(ADVISOR_FIRST)


def test_judge_bound_not_needed_when_judge_disabled(tmp_path):
    bounds = full_bounds()
    del bounds['oss/judge-v1']
    app = budgeted(tmp_path, advisor_config(judge_calls_per_attempt=0), bounds)
    assert str(app._admit(ADVISOR_FIRST)) == '0.302'


def test_legacy_reservation_unchanged_by_advisor_config(tmp_path):
    plain = budgeted(tmp_path / 'plain', None, full_bounds())
    advised = budgeted(tmp_path / 'advised', advisor_config(), full_bounds())
    for mode in ('cloudyfan', 'fixed_high', 'low_first', 'open_source_first'):
        assert advised._admit(mode) == plain._admit(mode)


def test_advisor_first_reservation_requires_advisor_config(tmp_path):
    with pytest.raises(ValueError, match='Unknown workflow mode'):
        budgeted(tmp_path, None, full_bounds())._admit(ADVISOR_FIRST)


# ---- 角色記帳與零模型費用情境（ADR 0034 第 6 點）----

COSTS = {'oss/advisor-v1': .002, 'oss/judge-v1': .0005, 'example/high': .05}


def priced_api(request):
    import json
    p = json.loads(request.content)
    return httpx.Response(200, json={'id': 'gen-' + p['session_id'], 'model': p['model'],
        'provider': 'Example', 'usage': {'cost': COSTS[p['model']]},
        'choices': [{'message': {'role': 'assistant', 'content': '2'}, 'finish_reason': 'stop'}]})


def role_receipts(engine):
    """以 MockTransport 留下顧問、裁判與一次池內作答的收據，回傳 #73 會產生的結果形狀。"""
    from cloudyfan_jev.advisor import advisor_call_id, judge_call_id
    from cloudyfan_jev.client import CHAT
    prefix = 'run-roles'
    plan = [(advisor_call_id(prefix), 'oss/advisor-v1', 'advisor'),
            (f'{prefix}-a1-t0', 'example/high', 'high'),
            (judge_call_id(prefix, 1), 'oss/judge-v1', 'judge')]
    calls = []
    for cid, model, tier in plan:
        engine.client.call(cid, CHAT, {'model': model, 'session_id': cid.replace('-', '')})
        calls.append({'call_id': cid, 'tier': tier})
    return {'run_id': 'roles', 'advisor': {'calls': [calls[0]]},
            'attempts': [{'calls': calls[1:]}], 'selection': None}


@pytest.mark.parametrize('judge_open_source,scenario', [(True, '0.05'), (False, '0.0505')])
def test_open_source_roles_enter_zero_fee_scenario_by_explicit_flag(tmp_path, judge_open_source, scenario):
    config = advisor_config(judge=role_profile('judge', open_source=judge_open_source))
    engine = make_core(tmp_path, priced_api, config)
    costs = engine._with_costs(role_receipts(engine))['costs']
    assert costs['api_usd'] == '0.0525'  # 研究全額照收據記入
    assert costs['hypothetical_open_source_zero_usd'] == scenario
    assert costs['advisor_usd'] == '0.002' and costs['judge_usd'] == '0.0005'
    assert costs['calls'] == 3


def test_role_receipt_identity_mismatch_is_refused(tmp_path):
    engine = make_core(tmp_path, priced_api, advisor_config())
    result = role_receipts(engine)
    other = make_core(tmp_path, priced_api, advisor_config(
        advisor={**role_profile('advisor'), 'accepted_models': ['oss/other'], 'model_version': 'oss/other'}))
    with pytest.raises(RuntimeError, match='identity mismatch'):
        other._with_costs(result)


def test_ledger_rows_carry_role_and_open_source_scenario(tmp_path):
    from cloudyfan_jev.ledger import summarize
    config = advisor_config()
    engine = make_core(tmp_path, priced_api, config)
    role_receipts(engine)
    plain = summarize(engine.client)
    assert all('role' not in row for row in plain['rows'])
    assert 'roles' not in plain
    summary = summarize(engine.client, advisor=config)
    roles = {row['call_id']: row.get('role') for row in summary['rows']}
    assert roles == {'run-roles-advisor': 'advisor', 'run-roles-a1-t0': None, 'run-roles-a1-judge': 'judge'}
    assert summary['total_api_usd'] == '0.0525'
    assert summary['roles'] == {
        'advisor': {'calls': 1, 'known_usd': '0.002', 'unknown_calls': 0, 'open_source': True},
        'judge': {'calls': 1, 'known_usd': '0.0005', 'unknown_calls': 0, 'open_source': True}}
    assert summary['hypothetical_open_source_roles_zero_usd'] == '0.05'


# ---- 客戶端註冊契約選用路徑二（每題建立時凍結）----

def registered(tmp_path, advisor):
    from cloudyfan_jev.http_api import RegisteredContract
    from cloudyfan_jev.privacy import ExecutionPolicy
    from test_budget import controlled
    from test_engine import API
    runner = controlled(tmp_path / 'runner', API(first='2'))
    runner.core.execution_policy = ExecutionPolicy(mode='unrestricted', origin='home:fixture',
                                                   classifier='cloud:typesafe')
    runner.core.advisor_first = advisor
    return RegisteredContract('public-v1', runner, lambda workspace: ArithmeticEnvironment(),
                              mode=ADVISOR_FIRST)


def test_contract_cannot_select_advisor_first_without_advisor_config(tmp_path):
    with pytest.raises(ValueError, match='advisor_first'):
        registered(tmp_path, None).descriptor()


def test_contract_selecting_advisor_first_freezes_mode_and_advisor_config(tmp_path):
    descriptor = registered(tmp_path, advisor_config()).descriptor()
    assert descriptor['mode'] == ADVISOR_FIRST
    assert descriptor['advisor_first']['score_snapshot'] == 'snapshot-20261117-a'

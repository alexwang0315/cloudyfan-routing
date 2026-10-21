"""Public category routing seams; synthetic receipts never qualify real models."""
from copy import deepcopy
import json

import httpx
import pytest

from cloudyfan_jev.client import Client
from cloudyfan_jev.demo import ArithmeticEnvironment, example_config
from cloudyfan_jev.engine import Limits
from cloudyfan_jev.scores.categories import CATEGORY_SPECS, compile_category_matrix
from cloudyfan_jev.scores.catalog import digest
from cloudyfan_jev.scores.category_routing import CategoryCore, prepare_category_selection
from cloudyfan_jev.scores.qcs import flow_contract_hash, SelectionPolicy
from test_categories import fixture, NOW, CATEGORY
from test_qcs import inputs, interval


def settings(category=CATEGORY):
    matrix = compile_category_matrix(*fixture(category), now=NOW)
    _, evidence, policy = inputs()
    policy['category_id'] = category
    policy['calibration']['category_id'] = category
    for row in evidence.values():
        row['provenance']['category_id'] = category
        row['provenance']['retrieved_at'] = NOW
        row['provenance']['valid_until'] = '2026-10-22T12:00:00Z'
    policy['calibration'].update(retrieved_at=NOW, valid_until='2026-10-22T12:00:00Z')
    requirements = {'domain': category, 'capabilities': ['tools'], 'context_tokens': 1000}
    bind(matrix, category, requirements, evidence, policy)
    return matrix, requirements, evidence, policy


def bind(matrix, category, requirements, evidence, policy):
    policy['complete_policy_flow'] = {'metric': 'whole_task_flow_ms', 'statistic': 'p95', 'upper': '500',
        'provenance': deepcopy(policy['calibration']),
        'contract_sha256': flow_contract_hash(matrix['categories'][category]['snapshot'], requirements, evidence, policy)}


def core_for(tmp_path, handler, values, *, category=CATEGORY, run_id='category', now=NOW):
    matrix, requirements, evidence, policy = values
    _, decision = example_config()
    client = Client(httpx.Client(transport=httpx.MockTransport(handler)), 'offline-fixture', tmp_path)
    return CategoryCore(client, matrix, category, requirements, evidence, policy, decision,
        run_id=run_id, clock=lambda: now, limits=Limits(max_turns=1))


def response(payload, n, answer='2', cost='.01'):
    return httpx.Response(200, json={'id': f'gen-cat-{n}', 'model': payload['model'],
        'provider': 'Example', 'usage': {'cost': cost}, 'choices': [{'message': {'content': answer}}]})


@pytest.mark.parametrize('category', list(CATEGORY_SPECS))
def test_all_23_classes_reach_core_and_bypass_unprofitable_classifier(tmp_path, category):
    calls = []
    def handler(request):
        payload = json.loads(request.content)
        calls.append(payload)
        assert not request.url.path.endswith('/decisions')
        return response(payload, len(calls), 'bad' if len(calls) == 1 else '2')
    values = settings(category)
    core = core_for(tmp_path, handler, values, category=category)
    task = {'prompt': 'Return 1+1.', 'workload_id': 'arithmetic-small', 'category': category}
    result = core.run('category', task, ArithmeticEnvironment, contract_version='v1')
    assert result['status'] == 'passed'
    assert [a['tier'] for a in result['attempts']] == ['open_source', 'mid']
    assert result['selection']['classifier_calls'] == 0 and result['costs']['classifier_usd'] == '0'
    assert core.selection_policy.reservation_plan()['classifier_calls'] == 0
    assert core.run('category', task, ArithmeticEnvironment, contract_version='v1')['status'] == 'passed'
    assert len(calls) == 2
    manifest = json.loads((tmp_path / 'runs/category/manifest.json').read_text())
    assert manifest['task']['category_binding']['matrix_id'] == values[0]['matrix_id']


@pytest.mark.parametrize('change', ['policy', 'calibration', 'flow', 'requirements', 'category'])
def test_cross_category_evidence_cannot_dispatch(tmp_path, change):
    values = settings()
    matrix, requirements, evidence, policy = values
    if change == 'policy': policy['category_id'] = 'website'
    if change == 'calibration': policy['calibration']['category_id'] = 'website'
    if change == 'flow': policy['complete_policy_flow']['provenance']['category_id'] = 'website'
    if change == 'requirements': requirements['domain'] = 'website'
    with pytest.raises(ValueError):
        core_for(tmp_path, lambda r: pytest.fail('No POST'), values,
                 category='search' if change == 'category' else CATEGORY)


def test_bad_optional_quality_scope_excludes_only_that_candidate():
    matrix, req, evidence, policy = settings()
    evidence['low']['provenance']['category_id'] = 'website'
    bind(matrix, CATEGORY, req, evidence, policy)
    selection = prepare_category_selection(matrix, CATEGORY, req, evidence, policy, now=NOW)
    assert not selection['qcs_plan']['eligible']['low']
    assert selection['qcs_plan']['eligible']['mid']


def test_multiple_oss_candidates_use_complete_cost_and_explicit_empty_never_falls_back():
    matrix, req, evidence, policy = settings()
    view = matrix['categories'][CATEGORY]
    alternative = deepcopy(view['pools']['open_source'][0])
    alternative['config_id'] = 'oss-alternative'
    view['pools']['open_source'].append(alternative)
    view['snapshot']['open_source_pool'] = deepcopy(view['pools']['open_source'])
    view['snapshot']['snapshot_id'] = digest({k:v for k,v in view['snapshot'].items() if k != 'snapshot_id'})
    matrix['matrix_id'] = digest({k:v for k,v in matrix.items() if k != 'matrix_id'})
    evidence['oss-alternative'] = deepcopy(evidence['open_source'])
    evidence['oss-alternative']['complete_cost_usd'] = interval('.2')
    bind(matrix, CATEGORY, req, evidence, policy)
    plan = prepare_category_selection(matrix, CATEGORY, req, evidence, policy, now=NOW)['qcs_plan']
    assert plan['profiles']['open_source']['capability_card']['score_selection']['config_id'] == 'open_source'
    from cloudyfan_jev.scores.qcs import prepare_qcs
    snap = deepcopy(view['snapshot'])
    snap['open_source_pool'] = []
    snap['snapshot_id'] = digest({k:v for k,v in snap.items() if k != 'snapshot_id'})
    policy['complete_policy_flow']['contract_sha256'] = flow_contract_hash(snap, req, evidence, policy)
    with pytest.raises(ValueError, match='[Oo]pen.source|OSS|qualified'):
        prepare_qcs(snap, req, evidence, policy, now=NOW)


def test_resume_requires_original_category_matrix_and_contract(tmp_path):
    calls = []
    def handler(request):
        p = json.loads(request.content)
        calls.append(p)
        return response(p, len(calls))
    values = settings()
    task = {'prompt': 'Return 1+1.', 'workload_id': 'arithmetic-small'}
    core_for(tmp_path, handler, values).run('category', task, ArithmeticEnvironment, contract_version='v1')
    expired = core_for(tmp_path, handler, values, now='2030-01-01T00:00:00Z')
    assert expired.run('category', task, ArithmeticEnvironment, contract_version='v1')['status'] == 'passed'
    with pytest.raises(ValueError, match='category'):
        expired.run('category', {**task, 'category': 'website'}, ArithmeticEnvironment, contract_version='v1')
    changed = deepcopy(values)
    changed[0]['created_at'] = '2026-10-21T00:00:01Z'
    changed[0]['matrix_id'] = digest({k:v for k,v in changed[0].items() if k != 'matrix_id'})
    with pytest.raises(ValueError, match='manifest'):
        core_for(tmp_path, handler, changed).run('category', task, ArithmeticEnvironment, contract_version='v1')
    with pytest.raises(ValueError, match='execution contract'):
        expired.run('category', task, ArithmeticEnvironment, contract_version='different')
    assert len(calls) == 1


def test_independent_classification_savings_still_allow_real_mock_jev(tmp_path):
    from cloudyfan_jev.scores.qcs import classifier_contract_hash
    matrix, req, evidence, policy = settings()
    evidence['low']['generation_cost_usd'] = interval('.01')
    evidence['low']['complete_cost_usd'] = interval('.2')
    stage = policy['stages']['low_mid']
    stage.update(classifier_cost_usd=interval('.001'), lower_probability=interval('.9'), classify_flow_ms_upper='110')
    stage['paths']['low']['cost_usd'] = interval('.2')
    _, decision = example_config()
    policy['classifier_version'] = decision['version']
    bind(matrix, CATEGORY, req, evidence, policy)
    profiles = prepare_category_selection(matrix, CATEGORY, req, evidence, policy, now=NOW)['qcs_plan']['profiles']
    stage['classified_calibration'] = {**policy['calibration'], 'classifier_version': decision['version'],
        'stage': 'low_mid', 'previous_tier': 'open_source', 'context': 'public_failure_or_unknown',
        'effective_probability_includes_fallback': True,
        'contract_sha256': classifier_contract_hash(profiles, decision['version'], 'low_mid', 'open_source')}
    stage['classified_paths'] = {'low': {'cost_usd': interval('.01')}, 'mid': {'cost_usd': interval('.1')}}
    bind(matrix, CATEGORY, req, evidence, policy)
    calls = []
    def handler(request):
        p = json.loads(request.content)
        calls.append(p)
        if request.url.path.endswith('/decisions'):
            assert p['state']['candidates']['low']['capability_card']['category_id'] == CATEGORY
            return httpx.Response(200, json={'id': 'gen-jev', 'model': p['model'], 'provider': 'TypeSafe',
                'usage': {'cost': '.001', 'input_tokens': 1, 'output_tokens': 1},
                'answers': {'low_mid': {'type': 'choice', 'choice': 'low', 'probabilities': {'low': .9, 'mid': .1}}}})
        return response(p, len(calls), 'bad' if len(calls) == 1 else '2')
    from decimal import Decimal
    from cloudyfan_jev.budget import Authorization, BudgetStore, BudgetedCore, EnforcedBound
    core = core_for(tmp_path / 'client', handler, (matrix, req, evidence, policy))
    store = BudgetStore(tmp_path / 'budget.sqlite')
    store.authorize(Authorization('synthetic-only', Decimal('.4'), '2099-01-01T00:00:00Z'))
    bounds = {p['model_version']: EnforcedBound('.1', 'synthetic') for p in core.profiles.values()}
    bounds[decision['version']] = EnforcedBound('.1', 'synthetic')
    engine = BudgetedCore(core, store, model_bounds=bounds, tool_bounds={}, verify_bound=lambda *a: True)
    result = engine.run('category',
        {'prompt': 'Return 1+1.', 'workload_id': 'arithmetic-small'}, ArithmeticEnvironment, contract_version='v1')
    assert result['selection']['classifier_calls'] == 1
    assert result['costs']['api_usd'] == '0.021'
    assert result['budget']['shared']['reservations'][0]['amount'] == '0.4'
    assert result['budget']['run_total_usd'] == '0.021'
    assert len(result['attempts']) == 2
    assert SelectionPolicy(prepare_category_selection(matrix, CATEGORY, req, evidence, policy, now=NOW)['qcs_plan']).decide(
        'low_mid', 'open_source')['action'] == 'classify'


@pytest.mark.parametrize('dimension', ['quality', 'speed', 'cost'])
def test_category_candidate_selection_rejects_bad_dimension(dimension):
    matrix, req, evidence, policy = settings()
    view = matrix['categories'][CATEGORY]
    alternate = deepcopy(view['pools']['mid'][0])
    alternate['config_id'] = 'mid-other'
    view['pools']['mid'].append(alternate)
    view['snapshot']['pools']['mid'] = deepcopy(view['pools']['mid'])
    view['snapshot']['snapshot_id'] = digest({k:v for k,v in view['snapshot'].items() if k != 'snapshot_id'})
    matrix['matrix_id'] = digest({k:v for k,v in matrix.items() if k != 'matrix_id'})
    evidence['mid-other'] = deepcopy(evidence['mid'])
    evidence['mid-other']['generation_cost_usd'] = interval('.001')
    evidence['mid-other']['complete_cost_usd'] = interval('.001')
    if dimension == 'quality': evidence['mid-other']['quality_sufficient'] = False
    if dimension == 'speed': evidence['mid-other']['speed']['upper'] = '5000'
    if dimension == 'cost': evidence['mid-other']['complete_cost_usd'] = interval('.2')
    bind(matrix, CATEGORY, req, evidence, policy)
    plan = prepare_category_selection(matrix, CATEGORY, req, evidence, policy, now=NOW)['qcs_plan']
    assert plan['profiles']['mid']['capability_card']['score_selection']['config_id'] == 'mid'


def test_quality_metric_from_another_category_is_not_qualification():
    matrix, req, evidence, policy = settings()
    evidence['low']['source_metrics'] = [{**fixture('artificial-analysis:intelligence_index')[0][1]}]
    bind(matrix, CATEGORY, req, evidence, policy)
    plan = prepare_category_selection(matrix, CATEGORY, req, evidence, policy, now=NOW)['qcs_plan']
    assert not plan['eligible']['low'] and plan['eligible']['mid']


def test_sql_capability_cannot_use_generic_category_approval():
    matrix, req, evidence, policy = settings()
    req['capabilities'] = ['sql']
    view = matrix['categories'][CATEGORY]
    for pool in view['pools'].values():
        for entry in pool:
            entry['capabilities'].append('sql')
    snapshot = view['snapshot']
    snapshot['pools'] = {t:deepcopy(view['pools'][t]) for t in ('low', 'mid', 'high')}
    snapshot['open_source_pool'] = deepcopy(view['pools']['open_source'])
    snapshot['open_source'] = deepcopy(view['pools']['open_source'][0])
    snapshot['snapshot_id'] = digest({k:v for k,v in snapshot.items() if k != 'snapshot_id'})
    matrix['matrix_id'] = digest({k:v for k,v in matrix.items() if k != 'matrix_id'})
    bind(matrix, CATEGORY, req, evidence, policy)
    with pytest.raises(ValueError, match='[Oo]pen.source|qualified'):
        prepare_category_selection(matrix, CATEGORY, req, evidence, policy, now=NOW)


def test_unknown_charge_stops_category_path_without_escalation(tmp_path):
    calls = []
    def handler(request):
        p = json.loads(request.content)
        calls.append(p)
        return httpx.Response(200, json={'model': p['model'],
            'provider': 'Example', 'choices': [{'message': {'content': 'bad'}}]})
    core = core_for(tmp_path, handler, settings())
    with pytest.raises(RuntimeError, match='incomplete billing'):
        core.run('category', {'prompt': 'Return 1+1.', 'workload_id': 'arithmetic-small'},
                 ArithmeticEnvironment, contract_version='v1')
    assert len(calls) == 1


def test_category_budget_wrapper_reserves_no_classifier_and_keeps_failed_cost(tmp_path):
    from decimal import Decimal
    from cloudyfan_jev.budget import Authorization, BudgetStore, BudgetedCore, EnforcedBound
    calls = []
    def handler(request):
        p = json.loads(request.content)
        calls.append(p)
        assert not request.url.path.endswith('/decisions')
        return response(p, len(calls), 'bad' if len(calls) == 1 else '2')
    core = core_for(tmp_path / 'client', handler, settings())
    store = BudgetStore(tmp_path / 'budget.sqlite')
    store.authorize(Authorization('synthetic-only', Decimal('.3'), '2099-01-01T00:00:00Z'))
    bounds = {core.profiles[t]['model_version']: EnforcedBound('.1', 'synthetic')
              for t in ('open_source', 'mid', 'high')}
    engine = BudgetedCore(core, store, model_bounds=bounds, tool_bounds={}, verify_bound=lambda *a: True)
    task = {'prompt': 'Return 1+1.', 'workload_id': 'arithmetic-small'}
    result = engine.run('category', task, ArithmeticEnvironment, contract_version='v1')
    assert result['budget']['shared']['reservations'][0]['amount'] == '0.3'
    assert result['budget']['run_total_usd'] == '0.02'
    engine.run('category', task, ArithmeticEnvironment, contract_version='v1')
    assert len(calls) == 2


@pytest.mark.parametrize('missing_model', [False, True])
def test_unranked_local_oss_with_endpoint_metrics_reaches_core(tmp_path, missing_model):
    from cloudyfan_jev.scores.configuration import configuration_digest
    rows, registries, policies = fixture()
    oss = registries[CATEGORY]['open_source_candidates'][0]
    oss.pop('observation_binding')
    oss.pop('source_model_id')
    oss['category_qualification'] = {'category_id': CATEGORY, 'configuration_sha256': configuration_digest(oss),
        'report_id': 'd'*64, 'valid_until': '2026-10-22T00:00:00Z'}
    matrix, req, evidence, policy = settings()
    matrix = compile_category_matrix(rows, registries, policies, now=NOW)
    evidence['open_source']['source_metrics'] = [{'schema_version': 'qcs-source-v1', 'model_id': None if missing_model else oss['profile']['model'],
        'provider': oss['profile']['provider'], 'retrieved_at': NOW, 'as_of': NOW, 'raw_sha256': 'e'*64, 'quality': {}}]
    bind(matrix, CATEGORY, req, evidence, policy)
    calls = []
    def handler(request):
        p = json.loads(request.content)
        calls.append(p)
        return response(p, len(calls))
    if missing_model:
        with pytest.raises(ValueError, match='QCS evidence'):
            core_for(tmp_path, handler, (matrix, req, evidence, policy))
        assert not calls
    else:
        result = core_for(tmp_path, handler, (matrix, req, evidence, policy)).run('category',
            {'prompt': 'Return 1+1.', 'workload_id': 'arithmetic-small'}, ArithmeticEnvironment, contract_version='v1')
        assert result['status'] == 'passed' and len(calls) == 1


@pytest.mark.parametrize('now', [NOW, None])
def test_malformed_optional_sql_qualification_is_excluded_during_admission_and_replay(now):
    from cloudyfan_jev.scores.configuration import configuration_digest
    matrix, req, evidence, policy = settings()
    req['capabilities'] = ['sql']
    view = matrix['categories'][CATEGORY]
    for pool in view['pools'].values():
        for entry in pool:
            entry['capabilities'].append('sql')
            entry['sql_qualification'] = {'configuration_sha256': configuration_digest(entry),
                'report_id': 'a' * 64, 'valid_until': '2026-10-22T00:00:00Z'}
    view['pools']['low'][0]['sql_qualification'] = []
    snapshot = view['snapshot']
    snapshot['pools'] = {t:deepcopy(view['pools'][t]) for t in ('low', 'mid', 'high')}
    snapshot['open_source_pool'] = deepcopy(view['pools']['open_source'])
    snapshot['open_source'] = deepcopy(view['pools']['open_source'][0])
    snapshot['snapshot_id'] = digest({k:v for k,v in snapshot.items() if k != 'snapshot_id'})
    matrix['matrix_id'] = digest({k:v for k,v in matrix.items() if k != 'matrix_id'})
    bind(matrix, CATEGORY, req, evidence, policy)
    plan = prepare_category_selection(matrix, CATEGORY, req, evidence, policy, now=now)['qcs_plan']
    assert not plan['eligible']['low'] and plan['eligible']['mid'] and plan['eligible']['open_source']

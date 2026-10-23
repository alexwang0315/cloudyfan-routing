"""Synthetic calibration + real SQLite + MockTransport; no paid requests."""

import hashlib
import json
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import pytest
from cloudyfan_jev.client import Client
from cloudyfan_jev.engine import Limits
from cloudyfan_jev.scores.backup_runtime import backup_pool_contract_hash
from cloudyfan_jev.scores.catalog import digest
from cloudyfan_jev.scores.health import HealthStore
from cloudyfan_jev.scores.qcs import COMPLETE_PATH, HORIZON
from cloudyfan_jev.scores.selector_research import (
    VERSION as SELECTOR_VERSION,
)
from cloudyfan_jev.scores.selector_research import CalibrationRequired, required_contracts
from cloudyfan_jev.sql import SQL_DIAGNOSTICS, SqlContract
from cloudyfan_jev.scores.sql_selector_research import (
    VERSION,
    CalibratedSQLResearchCore,
    build_calibrated_sql_research_core,
    prepare_template,
    research_policy,
)

from cloudyfan_jev.demo import example_config
from test_backup_runtime import plan_for
from test_categories import NOW as FIXTURE_NOW

NOW = datetime.fromisoformat(FIXTURE_NOW)


def old_template():
    return {'backup_plan': plan_for(count=3), 'decision_profile': example_config()[1]}


def bind(plan):
    plan['backup_id'] = digest({k: v for k, v in plan.items() if k != 'backup_id'})


def synthetic_plan(tmp_path):
    old = old_template()
    plan, decision = deepcopy(old['backup_plan']), old['decision_profile']
    plan['pools'] = {t: rows[:1] for t, rows in plan['pools'].items()}
    # This expiry belongs only to a new synthetic template, never the old file.
    expiry = (NOW + timedelta(days=1)).isoformat()
    plan['schema_version'] = 's50-provisional-backup-v1'
    raw_source = Path(__file__).resolve()  # Public synthetic fixture, not a study record.
    sources = {str(raw_source): hashlib.sha256(raw_source.read_bytes()).hexdigest()}
    plan['provisional_policy'] = {
        'schema_version': 's50-provisional-evidence-v1',
        'unknown_quality': 'allow_research_no_guarantee',
        'unknown_speed': 'allow_without_sla', 'unknown_health': 'allow_unobserved_only',
        'formal_qualification': 'pending', 'valid_until': expiry, 'source_files': sources,
    }
    for rows in plan['pools'].values():
        for row in rows:
            row['entry'].update(approved=False, research_only=True)
            row['evidence'].update(
                cost_basis='derived_posted_token_scenario',
                failure_probability={'lower':'0','upper':'1'},
                quality={'basis':'unknown','exact_SQL_quality_guaranteed':False},
                speed={'metric':'unknown','statistic':None,'upper':None},
                source_files=sources,
                remaining_cost_by_stage={stage: deepcopy(row['evidence']['generation_cost_usd'])
                    for stage in ('low_mid','mid_high')},
            )
    policy = plan['qcs_policy']
    policy.update(classifier_version=decision['version'], speed_statistic='p95')
    policy['stages'] = {stage: {'variants': {}} for stage in ('low_mid', 'mid_high')}
    raw = tmp_path / 'synthetic-calibration.json'
    raw.write_text('{"synthetic": true, "not_actual_calibration": true}\n')
    provenance = {
        'basis': 'independent_calibration', 'workload_id': policy['workload_id'],
        'source_url': 'https://example.org/synthetic-sql-selector-calibration',
        'raw_sha256': hashlib.sha256(raw.read_bytes()).hexdigest(),
        'retrieved_at': NOW.isoformat(), 'valid_until': expiry,
        'category_id': plan['category_id'],
        'dataset_partition': 'independent_calibration', 'uses_evaluation_answers': False,
        'data_kind': 'offline_fixture', 'sample_count': 8,
        'source_files': {str(raw): hashlib.sha256(raw.read_bytes()).hexdigest()},
        'pool_contract_sha256': backup_pool_contract_hash(plan),
        'classifier_version': decision['version'], 'context': 'public_failure_or_unknown',
        'horizon': HORIZON, 'includes': sorted(COMPLETE_PATH),
        'effective_probability_includes_fallback': True,
        'selector_policy_version': SELECTOR_VERSION,
        'execution_contract_sha256': digest(policy.get('execution_contract')),
    }
    by_id = {r['entry']['config_id']: r for pool in plan['pools'].values() for r in pool}
    for key, needed in required_contracts(plan, decision).items():
        tiers = ('low', 'mid') if needed['stage'] == 'low_mid' else ('mid', 'high')
        cal = {
            **provenance, 'contract_sha256': key, 'stage': needed['stage'],
            'previous_tier': needed['previous_tier'],
            'previous_configuration_sha256': by_id[needed['previous_config_id']][
                'configuration_sha256'
            ],
        }
        policy['stages'][needed['stage']]['variants'][key] = {
            'calibration': cal,
            'classifier_cost_usd': {'lower': '.001', 'upper': '.001'},
            'lower_probability': {'lower': '.9', 'upper': '.9'},
            'classify_flow_ms_upper': '10000',
            'paths': {tier: {'cost_usd': {'lower': '10', 'upper': '10'},
                             'flow_ms_upper': '10000'} for tier in tiers},
            'classified_paths': {
                tiers[0]: {'cost_usd': {'lower': '1', 'upper': '1'}},
                tiers[1]: {'cost_usd': {'lower': '2', 'upper': '2'}},
            },
        }
    bind(plan)
    return plan, decision


def sql_contract():
    return SqlContract(
        version='selector-sql-fixture-v1', prompt='Sum n and return column total.',
        tables={'numbers': [('n', 'INTEGER')]}, expected_columns=['total'],
        samples=[{'id': 'public-1', 'tables': {'numbers': [{'n': 1}, {'n': 2}]},
                  'expected': [[3]]}],
    )


def build(tmp_path, handler, *, mutate=None, transport=None, purpose='offline_rehearsal',
          data_kind='offline_fixture'):
    plan, decision = synthetic_plan(tmp_path)
    if mutate:
        mutate(plan, decision)
        bind(plan)
    health = HealthStore(tmp_path / 'health', policy=plan['qcs_policy']['health_policy'],
                         clock=lambda: NOW)
    client = Client(httpx.Client(transport=transport or httpx.MockTransport(handler)),
                    'synthetic-offline-token', tmp_path / 'client')
    kwargs = {
        'run_id': 'sql-selector-new', 'health_store': health, 'clock': lambda: NOW,
        'limits': Limits(max_turns=1), 'diagnostic_contract': SQL_DIAGNOSTICS,
        'data_kind': data_kind, 'purpose': purpose,
    }
    first = build_calibrated_sql_research_core(
        client, plan, decision, research_policy=research_policy(plan, data_kind=data_kind), **kwargs
    )
    plan['qcs_policy']['execution_contract'] = first.execution_contract_descriptor(
        sql_contract().contract_version
    )
    for stage in plan['qcs_policy']['stages'].values():
        for variant in stage['variants'].values():
            variant['calibration']['execution_contract_sha256'] = digest(
                plan['qcs_policy']['execution_contract']
            )
    bind(plan)
    return build_calibrated_sql_research_core(
        client, plan, decision, research_policy=research_policy(plan, data_kind=data_kind), **kwargs
    ), health


def run(core):
    contract = sql_contract()
    task = {**contract.task(), 'category': core._backup['category_id'],
            'workload_id': core._backup['qcs_policy']['workload_id']}
    return core.run('sql-selector-new', task, contract.environment,
                    contract_version=contract.contract_version, mode='cloudyfan')


def responder(plan, calls, decisions, *, always_fail=False, choices=('mid', 'high')):
    names = {r['entry']['profile']['provider']: r['entry']['profile']['provider_name']
             for pool in plan['pools'].values() for r in pool}
    generated = 0

    def handler(request):
        nonlocal generated
        payload = json.loads(request.content)
        calls.append(payload)
        if 'questions' in payload:
            decisions.append(payload)
            stage = next(iter(payload['questions']))
            lo, hi = ('low', 'mid') if stage == 'low_mid' else ('mid', 'high')
            chosen = choices[len(decisions) - 1]
            return httpx.Response(200, json={
                'id': f'gen-sql-selector-{len(calls)}', 'model': payload['model'],
                'provider': 'TypeSafe',
                'usage': {'cost': .001, 'input_tokens': 1, 'output_tokens': 1},
                'answers': {stage: {'type': 'choice', 'choice': chosen,
                                    'probabilities': {lo: .8 if chosen == lo else .2,
                                                      hi: .8 if chosen == hi else .2}}},
            })
        generated += 1
        sql = ('SELECT SUM(n) AS total FROM numbers'
               if generated == 3 and not always_fail else
               'SELECT 999 AS total' if '/mid-' in payload['model'] else
               'SELECT 123 AS total' if '/low-' in payload['model'] else
               'SELECT 0 AS total')
        return httpx.Response(200, json={
            'id': f'gen-sql-selector-{len(calls)}', 'model': payload['model'],
            'provider': names[payload['provider']['only'][0]],
            'usage': {'cost': .01, 'prompt_tokens': 1, 'completion_tokens': 1},
            'choices': [{'message': {'content': json.dumps({'sql': sql})}}],
        })
    return handler


def test_three_candidate_pool_requires_exact_variants_before_any_http(tmp_path):
    old = old_template()
    before = digest(old)
    prepared = prepare_template(old, data_kind='observed_calibration')
    assert prepared['preflight']['ready'] is False
    assert prepared['preflight']['stop'] == 'calibration_required'
    assert prepared['preflight']['required_contracts'] == 81
    assert digest(old) == before and prepared['dispatch_authorized'] is False
    calls = []
    client = Client(httpx.Client(transport=httpx.MockTransport(lambda r: calls.append(r))),
                    'mock-only', tmp_path / 'client')
    with pytest.raises(CalibrationRequired):
        build_calibrated_sql_research_core(
            client, prepared['backup_plan'], prepared['decision_profile'],
            research_policy=prepared['research_policy'], data_kind='observed_calibration',
            run_id='missing', health_store=None,
        )
    assert calls == []


@pytest.mark.parametrize('selected', [('mid', 'high'), ('low', 'mid')])
def test_actual_sql_core_two_selectors_latest_answer_costs_and_no_resend(tmp_path, selected):
    plan, _ = synthetic_plan(tmp_path)
    calls, decisions = [], []
    core, health = build(tmp_path, responder(plan, calls, decisions, choices=selected))
    result = run(core)
    assert isinstance(core, CalibratedSQLResearchCore)
    assert result['status'] == 'passed'
    assert [a['tier'] for a in result['attempts']] == ['open_source', *selected]
    assert result['generation_calls'] == 3
    assert result['selection']['classifier_calls'] == 2 and len(calls) == 5
    choices = [d for d in result['selection']['decisions'] if 'fallback' in d]
    assert len(choices) == 2 and all(d['fallback'] is False for d in choices)
    assert tuple(d['selected_tier'] for d in choices) == selected
    assert result['costs']['api_usd'] == '0.032'
    assert result['costs']['classifier_usd'] == '0.002'
    assert result['costs']['hypothetical_open_source_zero_usd'] == '0.022'
    assert result['research_policy_version'] == VERSION
    assert result['formal_qualification'] == 'pending'
    assert len(decisions) == 2
    latest = decisions[1]['state']['observations']
    expected_sql = 'SELECT 999 AS total' if selected[0] == 'mid' else 'SELECT 123 AS total'
    expected_model = 'example/' + selected[0] + '-0'
    assert latest['previous_answer'] == json.dumps({'sql': expected_sql})
    assert latest['previous_configuration']['model_version'] == expected_model
    assert latest['public_diagnostics']['passed'] is False
    assert 'SELECT 0 AS total' not in latest['previous_answer']
    assert core.budget_reservation_plan()['classifier_calls'] == 2
    assert not core.configuration_descriptor()['exact_SQL_quality_guaranteed']
    assert not core.configuration_descriptor()['speed_SLA_guaranteed']
    assert not health.assess(plan['pools']['open_source'][0]['configuration_sha256'],
                             plan['pools']['open_source'][0]['entry']['profile']['provider'])[
                                 'eligible'
                             ]
    assert run(core)['status'] == 'passed' and len(calls) == 5


def test_proven_not_economic_skips_only_after_calibration(tmp_path):
    calls, decisions = [], []
    plan, _ = synthetic_plan(tmp_path)

    def expensive(plan, _):
        for stage in plan['qcs_policy']['stages'].values():
            for variant in stage['variants'].values():
                variant['classifier_cost_usd'] = {'lower': '20', 'upper': '20'}

    core, _ = build(tmp_path, responder(plan, calls, decisions), mutate=expensive)
    result = run(core)
    assert result['status'] == 'passed' and decisions == []
    assert result['selection']['classifier_calls'] == 0
    assert result['costs']['classifier_usd'] == '0'
    assert all(d.get('reason') == 'classification_proven_not_economic'
               for d in result['selection']['decisions'][1:])


def test_uncertain_economics_stops_before_initial_model(tmp_path):
    calls = []

    def uncertain(plan, _):
        for variant in plan['qcs_policy']['stages']['low_mid']['variants'].values():
            variant['lower_probability'] = {'lower': '0', 'upper': '1'}
            variant['classified_paths']['mid']['cost_usd'] = {'lower': '1', 'upper': '20'}

    with pytest.raises(CalibrationRequired, match='low_mid'):
        build(tmp_path, lambda r: calls.append(r), mutate=uncertain)
    assert calls == []


def test_template_preflight_does_not_call_uncertain_economics_ready(tmp_path):
    plan, decision = synthetic_plan(tmp_path)
    for variant in plan['qcs_policy']['stages']['low_mid']['variants'].values():
        variant['lower_probability'] = {'lower': '0', 'upper': '1'}
        variant['classified_paths']['mid']['cost_usd'] = {'lower': '1', 'upper': '20'}
    template = prepare_template({'backup_plan': plan, 'decision_profile': decision},
                                data_kind='offline_fixture')
    assert template['preflight']['ready'] is False
    assert template['preflight']['reason'] == 'classification_economics_uncertain'
    assert template['preflight']['model_requests'] == 0


@pytest.mark.parametrize('purpose', ['offline_rehearsal', 'live_preparation'])
def test_fixture_cannot_reach_real_transport(tmp_path, purpose):
    with pytest.raises(ValueError, match='MockTransport|Live preparation'):
        build(tmp_path, lambda _: pytest.fail('No HTTP'),
              transport=httpx.HTTPTransport(), purpose=purpose)


def test_mock_fixture_cannot_be_labeled_live_preparation(tmp_path):
    with pytest.raises(ValueError, match='Live preparation'):
        build(tmp_path, lambda _: pytest.fail('No HTTP'), purpose='live_preparation')


def test_mock_default_with_real_url_mount_is_not_offline_transport(tmp_path):
    plan, decision = synthetic_plan(tmp_path)
    client = Client(
        httpx.Client(transport=httpx.MockTransport(lambda _: pytest.fail('No HTTP')),
                     mounts={'https://example.org': httpx.HTTPTransport()}),
        'mock-only', tmp_path / 'mounted-client',
    )
    with pytest.raises(ValueError, match='exclusive MockTransport'):
        build_calibrated_sql_research_core(
            client, plan, decision, data_kind='offline_fixture', purpose='offline_rehearsal',
            research_policy=research_policy(plan, data_kind='offline_fixture'),
            run_id='no-mounted-network', health_store=None,
        )


@pytest.mark.parametrize('field,value', [
    ('uses_evaluation_answers', True), ('dataset_partition', 'holdout'),
    ('classifier_version', 'wrong-version'), ('previous_configuration_sha256', '0' * 64),
])
def test_wrong_selector_calibration_binding_stops_before_http(tmp_path, field, value):
    calls = []

    def bad(plan, _):
        variant = next(iter(plan['qcs_policy']['stages']['low_mid']['variants'].values()))
        variant['calibration'][field] = value

    with pytest.raises(ValueError):
        build(tmp_path, lambda r: calls.append(r), mutate=bad)
    assert calls == []


def test_transport_swapped_after_preparation_stops_before_http(tmp_path):
    calls = []
    core, _ = build(tmp_path, lambda request: calls.append(request))
    core.client.http._transport = httpx.HTTPTransport()
    with pytest.raises(ValueError, match='exclusive MockTransport'):
        run(core)
    assert calls == []

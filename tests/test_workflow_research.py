"""Synthetic, tool-free three-arm research integration. Never calls real APIs."""
from copy import deepcopy
import json

import httpx
import pytest

from cloudyfan_jev.demo import ArithmeticEnvironment, example_config
from cloudyfan_jev.engine import Core, Limits
from cloudyfan_jev.recovery_prompts import descriptor as recovery_descriptor
from cloudyfan_jev.scores.catalog import digest
from cloudyfan_jev.scores.workflow_research import (
    ENVELOPE, VERSION, build_research_core, policy_descriptor,
    research_flow_contract_hash,
)
from test_backup_health import observation
from test_backup_runtime import build as build_backup, plan_for, rebind, response, task


def research_settings(*, classifiers=(), count=3):
    plan = plan_for(classifiers=classifiers, count=count)
    profiles = {tier: records[0]['entry']['profile'] for tier, records in plan['pools'].items()}
    _, decision = example_config()
    execution = Core(None, profiles, decision, limits=Limits(max_turns=1)).execution_contract_descriptor('v1')
    execution['core'].update(research_envelope=deepcopy(ENVELOPE), recovery_prompt_policy=recovery_descriptor())
    plan['qcs_policy']['execution_contract'] = execution
    rebind(plan)
    policy = {'schema_version': VERSION, 'envelope': deepcopy(ENVELOPE),
              'recovery_prompt': recovery_descriptor(),
              'whole_flow': {'metric': 'whole_task_flow_ms', 'statistic': plan['qcs_policy']['speed_statistic'],
                  'upper': '10000', 'contract_sha256': research_flow_contract_hash(plan),
                  'provenance': deepcopy(plan['pools']['open_source'][0]['evidence']['provenance'])}}
    return plan, policy


def build(tmp_path, handler, *, classifiers=(), count=3, settings=None):
    plan, policy = settings or research_settings(classifiers=classifiers, count=count)
    old, health, time = build_backup(tmp_path, handler, plan)
    core = build_research_core(old.client, plan, old.decision, research_policy=policy,
        run_id='backup', health_store=health, clock=lambda: time[0], limits=Limits(max_turns=1))
    return core, health, time


def run(core, *, mode='cloudyfan', env=ArithmeticEnvironment):
    return core.run('backup', task(), env, contract_version='v1', mode=mode)


def test_real_qcs_economics_skips_classifier_without_calibration_and_no_fee(tmp_path):
    calls = []
    def handler(request):
        p = json.loads(request.content)
        calls.append(p)
        return response(p, len(calls), answer='bad' if len(calls) == 1 else '2')
    core, _, _ = build(tmp_path, handler)
    result = run(core)
    assert [a['tier'] for a in result['attempts']] == ['open_source', 'mid']
    assert result['research_policy_version'] == VERSION
    assert result['qcs_backup_version'] == 'qcs-backup-policy-v1'
    assert result['selection']['classifier_calls'] == 0
    assert result['selection']['decisions'][-1]['reason'] == 'exact_classifier_calibration_missing'
    assert result['costs']['classifier_usd'] == '0'
    assert all('questions' not in p for p in calls)
    assert core.budget_reservation_plan()['classifier_calls'] == 0
    assert core.configuration_descriptor()['routing_version'] == VERSION
    assert policy_descriptor()['qcs_backup_version'] == 'qcs-backup-policy-v1'
    assert run(core)['status'] == 'passed' and len(calls) == 2


def test_calibrated_but_unprofitable_classification_is_zero_jev_calls(tmp_path):
    settings = research_settings(classifiers=('low_mid',), count=1)
    plan, policy = settings
    for variant in plan['qcs_policy']['stages']['low_mid']['variants'].values():
        variant['classifier_cost_usd'] = {'lower': '10', 'upper': '10'}
    rebind(plan)
    policy['whole_flow']['contract_sha256'] = research_flow_contract_hash(plan)
    calls = []
    def handler(request):
        p = json.loads(request.content)
        calls.append(p)
        return response(p, len(calls), answer='bad' if len(calls) == 1 else '2')
    core, _, _ = build(tmp_path, handler, settings=settings)
    result = run(core)
    assert len(calls) == 2 and result['selection']['classifier_calls'] == 0
    assert result['selection']['decisions'][-1]['reason'] == 'classification_not_economic'
    assert result['costs']['classifier_usd'] == '0'


@pytest.mark.parametrize('status', [429, 525])
@pytest.mark.parametrize('mode,expected', [('cloudyfan', ['open_source', 'mid']),
    ('low_first', ['low', 'high']), ('open_source_first', ['open_source', 'high'])])
def test_known_fee_no_answer_advances_stage_without_same_pool_retry(tmp_path, status, mode, expected):
    calls = []
    def handler(request):
        p = json.loads(request.content)
        calls.append(p)
        return response(p, len(calls), status=status if len(calls) == 1 else 200,
                        identity=len(calls) != 1)
    core, _, _ = build(tmp_path, handler)
    result = run(core, mode=mode)
    assert [a['tier'] for a in result['attempts']] == expected
    assert result['generation_calls'] == 2 and len(calls) == 2
    assert result['generation_failure_kinds'][0] == 'provider_unavailable'
    assert result['first_public_pass'] is None and result['final_public_pass'] is True
    assert not any(m['role'] == 'assistant' for m in calls[1]['messages'])
    assert calls[1]['messages'][1]['content'] == task()['prompt']
    assert 'did not produce an answer' in calls[1]['messages'][-1]['content']
    assert 'Repair the task using' not in calls[1]['messages'][-1]['content']
    assert result['attempts'][1]['config_id'].endswith('-1')
    assert run(core, mode=mode)['status'] == 'passed' and len(calls) == 2


@pytest.mark.parametrize('mode,maximum', [('cloudyfan', 3), ('low_first', 2), ('open_source_first', 2)])
def test_availability_failures_and_backups_still_respect_total_cap(tmp_path, mode, maximum):
    calls = []
    def handler(request):
        p = json.loads(request.content)
        calls.append(p)
        return response(p, len(calls), status=525, identity=False)
    core, _, _ = build(tmp_path, handler)
    result = run(core, mode=mode)
    assert result['generation_calls'] == maximum and len(calls) == maximum
    assert result['status'] == 'unverifiable'
    assert result['final_public_pass'] is None
    assert core.budget_reservation_plan(mode)['max_generation_calls'] == maximum


def test_unhealthy_primary_is_replaced_before_dispatch_without_extra_attempt(tmp_path):
    calls = []
    def handler(request):
        p = json.loads(request.content)
        calls.append(p)
        return response(p, len(calls))
    core, health, time = build(tmp_path, handler)
    rows = [observation(now=time[0], provider=r['entry']['profile']['provider'],
        configuration=r['configuration_sha256']) for pool in core._backup['pools'].values() for r in pool]
    for row in rows:
        if row['provider'] == 'provider-0': row['health']['status'] = 1
    health.publish(rows)
    result = run(core)
    assert result['attempts'][0]['config_id'] == 'open_source-1'
    assert len(calls) == result['generation_calls'] == 1


def test_two_paid_classifiers_observe_latest_actual_answer_and_diagnostics(tmp_path):
    calls, decisions = [], []
    def handler(request):
        p = json.loads(request.content)
        calls.append(p)
        if 'questions' in p:
            decisions.append(p)
            stage = next(iter(p['questions']))
            pair = ('low', 'mid') if stage == 'low_mid' else ('mid', 'high')
            chosen = 'mid' if stage == 'low_mid' else 'high'
            return httpx.Response(200, json={'id': f'gen-new-jev-{len(calls)}', 'model': p['model'],
                'provider': 'TypeSafe', 'usage': {'cost': .001, 'input_tokens': 1, 'output_tokens': 1}, 'answers': {
                    stage: {'type': 'choice', 'choice': chosen, 'probabilities': {
                        pair[0]: .8 if chosen == pair[0] else .2,
                        pair[1]: .8 if chosen == pair[1] else .2}}}})
        text = '2' if 'high' in p['model'] else 'second-bad' if 'mid' in p['model'] else 'first-bad'
        return response(p, len(calls), answer=text)
    core, _, _ = build(tmp_path, handler, classifiers=('low_mid', 'mid_high'), count=1)
    result = run(core)
    assert result['selection']['classifier_calls'] == 2 and len(calls) == 5
    assert [a['tier'] for a in result['attempts']] == ['open_source', 'mid', 'high']
    observations = decisions[1]['state']['observations']
    assert observations['previous_answer'] == 'second-bad'
    assert observations['previous_configuration']['model_version'] == 'example/mid-0'
    assert observations['public_diagnostics']['passed'] is False
    final_messages = calls[-1]['messages']
    assert [m['content'] for m in final_messages if m['role'] == 'assistant'] == ['second-bad']
    assert 'first-bad' not in json.dumps(final_messages)
    assert result['costs']['classifier_usd'] == '0.002'
    assert all(d['fallback'] is False for d in result['selection']['decisions'] if 'fallback' in d)
    assert run(core)['status'] == 'passed' and len(calls) == 5


def test_second_selector_may_choose_same_mid_configuration_to_repair_itself(tmp_path):
    calls, decisions = [], []
    def handler(request):
        p = json.loads(request.content)
        calls.append(p)
        if 'questions' in p:
            decisions.append(p)
            stage = next(iter(p['questions']))
            pair = ('low', 'mid') if stage == 'low_mid' else ('mid', 'high')
            return httpx.Response(200, json={'id': f'gen-mid-repair-{len(calls)}', 'model': p['model'],
                'provider': 'TypeSafe', 'usage': {'cost': .001, 'input_tokens': 1, 'output_tokens': 1}, 'answers': {
                    stage: {'type': 'choice', 'choice': 'mid', 'probabilities': {
                        pair[0]: .8 if pair[0] == 'mid' else .2,
                        pair[1]: .8 if pair[1] == 'mid' else .2}}}})
        return response(p, len(calls), answer='2' if len(calls) == 5 else 'bad')
    core, _, _ = build(tmp_path, handler, classifiers=('low_mid', 'mid_high'), count=1)
    result = run(core)
    assert [a['config_id'] for a in result['attempts']] == ['open_source-0', 'mid-0', 'mid-0']
    assert result['status'] == 'passed' and result['generation_calls'] == 3
    assert result['selection']['classifier_calls'] == 2 and len(calls) == 5
    assert all(d['fallback'] is False for d in result['selection']['decisions'] if 'fallback' in d)
    state = decisions[1]['state']
    assert state['observations']['previous_configuration']['model_version'] == 'example/mid-0'
    assert state['candidates']['mid']['model_version'] == 'example/mid-0'
    assert state['observations']['previous_answer'] == 'bad'
    assert run(core)['status'] == 'passed' and len(calls) == 5


def test_interrupted_validation_resumes_saved_answer_without_another_post(tmp_path):
    calls = []
    def handler(request):
        p = json.loads(request.content)
        calls.append(p)
        return response(p, len(calls))
    core, _, _ = build(tmp_path, handler)
    class Interrupted(ArithmeticEnvironment):
        def validate(self, final):
            raise RuntimeError('synthetic validator interruption')
    with pytest.raises(RuntimeError, match='validator interruption'):
        run(core, env=Interrupted)
    result = run(core)
    assert result['status'] == 'passed' and result['resumed'] is True
    assert result['wall_seconds'] is None and len(calls) == 1


@pytest.mark.parametrize('mutation', ['missing_flow', 'stale_flow', 'changed_prompt', 'quality', 'cost', 'speed'])
def test_missing_or_invalid_new_evidence_never_dispatches_or_falls_back(tmp_path, mutation):
    plan, policy = research_settings()
    if mutation == 'missing_flow': policy.pop('whole_flow')
    elif mutation == 'stale_flow': policy['whole_flow']['provenance']['valid_until'] = '2026-10-20T00:00:00Z'
    elif mutation == 'changed_prompt': policy['recovery_prompt']['version'] = 'old-repair'
    else:
        record = plan['pools']['open_source'][0]
        if mutation == 'quality': record['evidence']['quality_sufficient'] = False
        if mutation == 'cost': record['evidence'].pop('complete_cost_usd')
        if mutation == 'speed': record['evidence']['speed']['upper'] = None
        plan['backup_id'] = digest({k: v for k, v in plan.items() if k != 'backup_id'})
        policy['whole_flow']['contract_sha256'] = research_flow_contract_hash(plan)
    with pytest.raises((ValueError, KeyError, TypeError)):
        core, _, _ = build(tmp_path, lambda r: pytest.fail('No POST permitted'), settings=(plan, policy))
        run(core)


def test_independent_new_namespace_and_no_fixed_high_mode(tmp_path):
    core, _, _ = build(tmp_path, lambda r: pytest.fail('No POST permitted'))
    with pytest.raises(ValueError, match='three research modes'): run(core, mode='fixed_high')
    with pytest.raises(ValueError): core.budget_reservation_plan('fixed_high')
    with pytest.raises(ValueError):
        core.run('old-run', task(), ArithmeticEnvironment, contract_version='v1')


def test_unknown_cost_stops_before_next_stage_even_if_no_answer(tmp_path):
    calls = []
    def handler(request):
        if request.method == 'GET': return httpx.Response(404)
        p = json.loads(request.content)
        calls.append(p)
        return response(p, len(calls), status=525, cost=None, identity=False)
    core, _, _ = build(tmp_path, handler)
    with pytest.raises(RuntimeError): run(core)
    assert len(calls) == 1
    with pytest.raises(RuntimeError): run(core)
    assert len(calls) == 1


def test_unadvertised_upstream_tools_cannot_reach_underlying_environment(tmp_path):
    calls, effects = [], []
    def handler(request):
        p = json.loads(request.content)
        calls.append(p)
        result = response(p, len(calls)).json()
        result['choices'][0]['message'] = {'content': None, 'tool_calls': [
            {'id': 'unadvertised', 'function': {'name': 'dangerous_side_effect', 'arguments': '{}'}}]}
        return httpx.Response(200, json=result)
    class SideEffectEnvironment(ArithmeticEnvironment):
        def call(self, name, arguments):
            effects.append((name, arguments))
            return {'ok': True}
    core, _, _ = build(tmp_path, handler)
    with pytest.raises(RuntimeError, match='Tool execution failed'):
        run(core, env=SideEffectEnvironment)
    assert len(calls) == 1 and not effects
    assert (core.client.root / 'calls/run-backup-a0-t0/response.json').is_file()
    with pytest.raises(RuntimeError): run(core, env=SideEffectEnvironment)
    assert len(calls) == 1


@pytest.mark.parametrize('mode,actual,scenario', [('low_first', '0.02', '0.02'),
    ('open_source_first', '0.02', '0.02'), ('cloudyfan', '0.03', '0.02')])
def test_latest_zero_first_model_hypothesis_only_applies_to_cloudyfan(tmp_path, mode, actual, scenario):
    calls = []
    def handler(request):
        p = json.loads(request.content)
        calls.append(p)
        return response(p, len(calls), answer='bad')
    core, _, _ = build(tmp_path, handler, count=1)
    result = run(core, mode=mode)
    assert result['costs']['api_usd'] == actual
    assert result['costs']['hypothetical_open_source_zero_usd'] == scenario
    assert result['costs']['cloudyfan_mrc_zero_first_usd'] == scenario
    assert result['costs']['cost_scenario_version'] == 'mrc-first-zero-v1'
    assert len(result['costs']['waived_call_ids']) == (1 if mode == 'cloudyfan' else 0)
    assert result['costs']['scenario']['modes'] == ['cloudyfan']
    # An unknown first-model invoice stays unknown in actual/budget evidence.
    first = result['attempts'][0]['calls'][0]['call_id']
    original_load = core.client._load
    def load(directory):
        record = deepcopy(original_load(directory))
        if directory.name == first:
            body = json.loads(record['raw'])
            body['usage']['cost'] = None
            record['raw'] = json.dumps(body)
        return record
    core.client._load = load
    recomputed = core._with_costs(result)
    assert recomputed['costs']['api_usd'] is None
    assert recomputed['costs']['unknown_calls'] == 1
    assert recomputed['costs']['hypothetical_open_source_zero_usd'] == ('0.02' if mode == 'cloudyfan' else None)

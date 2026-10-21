"""Synthetic offline backups, paid-call identities, economics and resume safety."""
from copy import deepcopy
from datetime import datetime, timedelta
from decimal import Decimal
import json

import httpx
import pytest

from cloudyfan_jev.client import Client
from cloudyfan_jev.demo import ArithmeticEnvironment, example_config
from cloudyfan_jev.engine import Core, Limits
from cloudyfan_jev.scores.backup_runtime import (
    BackupCategoryCore, backup_classifier_contract_hash, backup_flow_contract_hash, backup_pool_contract_hash,
)
from cloudyfan_jev.scores.catalog import digest
from cloudyfan_jev.scores.health import HealthStore, _policy as normalize_health_policy
from cloudyfan_jev.scores.qcs import COMPLETE_PATH, HORIZON
from test_backup_health import POLICY as HEALTH_POLICY, observation
from test_backup_pools import prepare, rehash, settings
from cloudyfan_jev.scores.configuration import configuration_digest
from test_categories import CATEGORY, NOW
from test_qcs import interval


def plan_for(*, classifiers=(), count=3, limits=None, wire=None):
    values = settings(count=count)
    policy = values[3]
    policy.update(schema_version='qcs-backup-policy-v1', max_flow_ms='100000',
                  minimum_saving_usd='.001', minimum_saving_fraction='.01', stages={},
                  health_policy=normalize_health_policy(HEALTH_POLICY))
    for config_id, row in values[2].items():
        tier = config_id.rsplit('-', 1)[0]
        amount = {'open_source': '.01', 'low': '.20', 'mid': '.10', 'high': '.30'}[tier]
        row['complete_cost_usd'] = interval(amount)
    if wire is not None:
        for entry in values[0]['categories'][CATEGORY]['pools']['open_source']:
            profile = entry['profile']
            if wire == 'omit': profile.update(effort='none', reasoning_parameter='omit')
            if wire == 'free':
                profile['model_version'] += ':free'
                profile['accepted_models'] = [profile['model_version']]
            values[2][entry['config_id']]['configuration_sha256'] = configuration_digest(entry)
        rehash(values[0])
    plan = prepare(values)
    _, decision = example_config()
    profiles = {t: p[0]['entry']['profile'] for t, p in plan['pools'].items()}
    policy = plan['qcs_policy']
    policy['execution_contract'] = Core(None, profiles, decision,
        limits=limits or Limits(max_turns=1)).execution_contract_descriptor('v1')
    if classifiers:
        policy['classifier_version'] = decision['version']
    for stage in classifiers:
        lower, upper = ('low', 'mid') if stage == 'low_mid' else ('mid', 'high')
        variants = policy['stages'][stage] = {'variants': {}}
        for previous_tier in (('open_source',) if stage == 'low_mid' else ('low', 'mid')):
            for previous in plan['pools'][previous_tier]:
                for lo in plan['pools'][lower]:
                    for hi in plan['pools'][upper]:
                        pair = {lower: lo['entry']['profile'], upper: hi['entry']['profile']}
                        key = backup_classifier_contract_hash(pair, decision['version'], stage,
                                                              previous_tier, previous['entry']['profile'])
                        calibration = {**deepcopy(previous['evidence']['provenance']),
                            'contract_sha256': key, 'pool_contract_sha256': backup_pool_contract_hash(plan),
                            'previous_configuration_sha256': previous['configuration_sha256'],
                            'previous_tier': previous_tier, 'stage': stage, 'classifier_version': decision['version'],
                            'context': 'public_failure_or_unknown', 'horizon': HORIZON,
                            'includes': sorted(COMPLETE_PATH), 'effective_probability_includes_fallback': True}
                        variants['variants'][key] = {'calibration': calibration,
                            'classifier_cost_usd': interval('.001'), 'lower_probability': interval('.9'),
                            'classify_flow_ms_upper': '1000',
                            'paths': {lower: {'cost_usd': lo['evidence']['complete_cost_usd'], 'flow_ms_upper': '100'},
                                      upper: {'cost_usd': hi['evidence']['complete_cost_usd'], 'flow_ms_upper': '100'}},
                            'classified_paths': {lower: {'cost_usd': interval('.01')},
                                                 upper: {'cost_usd': interval('.10')}}}
    rebind(plan)
    return plan


def rebind(plan):
    policy = plan['qcs_policy']
    policy['complete_policy_flow'] = {'metric': 'whole_task_flow_ms', 'statistic': policy['speed_statistic'],
        'upper': '10000', 'provenance': deepcopy(plan['pools']['open_source'][0]['evidence']['provenance']),
        'contract_sha256': backup_flow_contract_hash(plan)}
    plan['backup_id'] = digest({k: v for k, v in plan.items() if k != 'backup_id'})


def build(tmp_path, handler, plan=None, *, limits=None, time=None):
    plan = plan or plan_for(limits=limits)
    time = time or [datetime.fromisoformat(NOW)]
    health = HealthStore(tmp_path / 'health', policy=HEALTH_POLICY, clock=lambda: time[0])
    if not health.path.exists() or not (tmp_path / 'health-initialized').exists():
        health.publish([observation(now=time[0], provider=r['entry']['profile']['provider'],
                                   configuration=r['configuration_sha256'])
                        for pool in plan['pools'].values() for r in pool])
        (tmp_path / 'health-initialized').touch()
    _, decision = example_config()
    client = Client(httpx.Client(transport=httpx.MockTransport(handler)), 'offline-test-token', tmp_path / 'receipts')
    return BackupCategoryCore(client, plan, decision, run_id='backup', health_store=health,
                             clock=lambda: time[0], limits=limits or Limits(max_turns=1)), health, time


def response(payload, n, *, answer='2', status=200, cost='.01', provider=None, identity=True):
    provider = provider or ('TypeSafe' if 'questions' in payload else
                           'Provider ' + payload['provider']['only'][0].rsplit('-', 1)[1])
    data = {'id': 'gen-backup-' + str(n), 'provider': provider, 'usage': {'cost': cost}}
    if identity: data['model'] = payload['model']
    if status == 200:
        data['choices'] = [{'message': {'content': answer}}]
    else:
        data['error'] = {'code': status, 'metadata': {'provider_name': provider}}
    return httpx.Response(status, json=data)


def task():
    return {'prompt': 'Return 1+1.', 'workload_id': 'offline-category-fixture', 'category': CATEGORY}


def run(core, env=ArithmeticEnvironment, *, mode='cloudyfan'):
    return core.run('backup', task(), env, contract_version='v1', mode=mode)


def test_missing_exact_classifier_calibration_is_direct_cheapest_and_zero_classifier_reservation(tmp_path):
    calls = []
    def handler(request):
        p = json.loads(request.content)
        calls.append(p)
        return response(p, len(calls), answer='bad' if len(calls) == 1 else '2')
    core, _, _ = build(tmp_path, handler)
    result = run(core)
    assert [a['tier'] for a in result['attempts']] == ['open_source', 'mid']
    assert result['costs']['api_usd'] == '0.02'
    assert result['costs']['classifier_usd'] == '0'
    assert result['selection']['classifier_calls'] == 0
    assert core.budget_reservation_plan()['classifier_calls'] == 0
    assert len(core.budget_reservation_plan()['generation_profiles']) == 12


@pytest.mark.parametrize('status', [429, 500, 503, 599])
def test_known_fee_no_answer_failure_falls_back_within_pool_and_accounts_actual_profiles(tmp_path, status):
    calls = []
    def handler(request):
        p = json.loads(request.content)
        calls.append(p)
        return response(p, len(calls), status=status if len(calls) == 1 else 200,
                        cost='.03' if len(calls) == 1 else '.07', identity=len(calls) != 1)
    core, health, _ = build(tmp_path, handler)
    result = run(core)
    assert result['status'] == 'passed'
    assert [a['tier'] for a in result['attempts']] == ['open_source', 'open_source']
    assert [a['config_id'] for a in result['attempts']] == ['open_source-0', 'open_source-1']
    assert result['costs']['api_usd'] == '0.10'
    assert result['costs']['calls'] == 2 and result['generation_calls'] == 2
    assert result['attempts'][0]['public']['passed'] is None
    record = core._backup['pools']['open_source'][0]
    assert health.assess(record['configuration_sha256'], 'provider-0')['circuit']['failures'] == 1
    assert run(core)['costs']['api_usd'] == '0.10' and len(calls) == 2


@pytest.mark.parametrize('mode,maximum', [('cloudyfan', 3), ('fixed_high', 2), ('low_first', 2),
                                         ('open_source_first', 2)])
def test_availability_fallbacks_consume_original_global_generation_cap(tmp_path, mode, maximum):
    calls = []
    def handler(request):
        p = json.loads(request.content)
        calls.append(p)
        return response(p, len(calls), status=503)
    core, _, _ = build(tmp_path, handler)
    result = run(core, mode=mode)
    assert result['generation_calls'] == maximum and len(calls) == maximum
    assert result['status'] == 'unverifiable'
    assert core.budget_reservation_plan(mode)['max_generation_calls'] == maximum


def test_quality_failure_goes_to_next_quality_stage_and_never_same_pool_retry(tmp_path):
    calls = []
    def handler(request):
        p = json.loads(request.content)
        calls.append(p)
        return response(p, len(calls), answer='bad')
    core, _, _ = build(tmp_path, handler)
    result = run(core)
    assert [a['tier'] for a in result['attempts']] == ['open_source', 'mid', 'mid']
    assert [a['config_id'] for a in result['attempts']] == ['open_source-0', 'mid-0', 'mid-1']
    assert result['status'] == 'failed' and len(calls) == 3


@pytest.mark.parametrize('failure', ['unknown_429', 'unknown_503', 'timeout', 'mismatch', 'answer_503'])
def test_unresolved_cost_timeout_or_untrusted_rejection_never_falls_back(tmp_path, failure):
    calls = []
    def handler(request):
        if request.method == 'GET': return httpx.Response(503)
        p = json.loads(request.content)
        calls.append(p)
        if failure == 'timeout': raise httpx.ReadTimeout('synthetic')
        if failure.startswith('unknown_'):
            return response(p, len(calls), status=int(failure[-3:]), cost=None)
        if failure == 'mismatch': return response(p, len(calls), status=503, provider='wrong')
        return httpx.Response(503, json={'id': 'gen-answer', 'model': p['model'], 'provider': 'Provider 0',
            'usage': {'cost': '.01'}, 'error': {'code': 503}, 'choices': [{'message': {'role': 'assistant', 'content': '2'}}]})
    core, _, _ = build(tmp_path, handler)
    with pytest.raises(RuntimeError): run(core)
    assert len(calls) == 1
    with pytest.raises(RuntimeError): run(core)
    assert len(calls) == 1


def test_interrupted_validation_replays_frozen_profile_despite_new_health_without_resending(tmp_path):
    calls = []
    def handler(request):
        p = json.loads(request.content)
        calls.append(p)
        return response(p, len(calls))
    core, health, time = build(tmp_path, handler)
    class Interrupted(ArithmeticEnvironment):
        def validate(self, text): raise RuntimeError('synthetic validator interruption')
    with pytest.raises(RuntimeError, match='interruption'): run(core, Interrupted)
    time[0] += timedelta(seconds=1000)
    rows = [observation(now=time[0], provider=r['entry']['profile']['provider'],
                        configuration=r['configuration_sha256'])
            for pool in core._backup['pools'].values() for r in pool]
    for row in rows: row['health']['status'] = 1
    health.publish(rows)
    restored, _, _ = build(tmp_path, handler, core._backup, time=time)
    result = run(restored)
    assert result['status'] == 'passed' and len(calls) == 1
    assert result['attempts'][0]['config_id'] == 'open_source-0' and result['resumed']


def test_original_deadline_blocks_new_continuation_after_validation_resume(tmp_path):
    calls = []
    def handler(request):
        p = json.loads(request.content)
        calls.append(p)
        return response(p, len(calls), answer='bad')
    core, _, time = build(tmp_path, handler)
    class Interrupted(ArithmeticEnvironment):
        def validate(self, text): raise RuntimeError('interrupted')
    with pytest.raises(RuntimeError): run(core, Interrupted)
    deadline = json.loads((core._manifest.parent / 'backup-deadline.json').read_text())
    time[0] += timedelta(seconds=200)
    result = run(core)
    assert result['generation_calls'] == 1 and len(calls) == 1
    assert json.loads((core._manifest.parent / 'backup-deadline.json').read_text()) == deadline


@pytest.mark.parametrize('stages', [('low_mid',), ('low_mid', 'mid_high')])
def test_profitable_independently_calibrated_jev_routes_and_latest_actual_previous_profile(tmp_path, stages):
    calls, decisions = [], []
    def handler(request):
        p = json.loads(request.content)
        calls.append(p)
        if 'questions' in p:
            decisions.append(p)
            stage = next(iter(p['questions']))
            pair = ('low', 'mid') if stage == 'low_mid' else ('mid', 'high')
            chosen = 'mid' if stage == 'low_mid' else 'high'
            return httpx.Response(200, json={'id': f'gen-jevs-{len(calls)}', 'model': p['model'],
                'provider': 'TypeSafe', 'usage': {'cost': '.001', 'input_tokens': 1, 'output_tokens': 1},
                'answers': {stage: {'type': 'choice', 'choice': chosen,
                    'probabilities': {pair[0]: .8 if chosen == pair[0] else .2,
                                      pair[1]: .8 if chosen == pair[1] else .2}}}})
        return response(p, len(calls), answer='2' if 'high' in p['model'] else 'bad')
    plan = plan_for(classifiers=stages)
    core, _, _ = build(tmp_path, handler, plan)
    result = run(core)
    assert result['selection']['classifier_calls'] == len(stages)
    assert core.budget_reservation_plan()['classifier_calls'] == len(stages)
    if len(stages) == 2:
        assert result['status'] == 'passed'
        state = decisions[-1]['state']
        assert state['observations']['previous_configuration']['model_version'] == 'example/mid-0'
        assert state['candidates']['mid']['model_version'] == 'example/mid-1'
        assert state['observations']['previous_answer'] == 'bad'
        assert state['observations']['public_diagnostics']['passed'] is False
    assert result['costs']['classifier_usd'] == str(Decimal('.001') * len(stages))
    assert run(core)['selection']['classifier_calls'] == len(stages)
    assert len(calls) == 3 + len(stages)


@pytest.mark.parametrize('mutation', ['pool', 'previous', 'protocol', 'provenance', 'component', 'population'])
def test_malformed_classifier_binding_never_dispatches(tmp_path, mutation):
    plan = plan_for(classifiers=('low_mid',))
    variant = next(iter(plan['qcs_policy']['stages']['low_mid']['variants'].values()))
    c = variant['calibration']
    if mutation == 'pool': c['pool_contract_sha256'] = 'a' * 64
    if mutation == 'previous': c['previous_configuration_sha256'] = 'a' * 64
    if mutation == 'protocol': c['effective_probability_includes_fallback'] = False
    if mutation == 'provenance': c['basis'] = 'formal_holdout'
    if mutation == 'component': c['includes'] = ['generation']
    if mutation == 'population': c['category_id'] = 'another-category'
    rebind(plan)
    with pytest.raises(ValueError): build(tmp_path, lambda r: pytest.fail('no POST'), plan)


def test_flow_binding_does_not_have_embedded_policy_hash_cycle_and_rejects_changed_pool(tmp_path):
    plan = plan_for()
    assert backup_flow_contract_hash(plan) == plan['qcs_policy']['complete_policy_flow']['contract_sha256']
    plan['qcs_policy']['minimum_saving_usd'] = '999'
    plan['backup_id'] = digest({k: v for k, v in plan.items() if k != 'backup_id'})
    with pytest.raises(ValueError, match='whole-task'): build(tmp_path, lambda r: pytest.fail('no POST'), plan)


def test_singleton_policy_never_borrowed_for_new_runtime(tmp_path):
    plan = plan_for()
    plan['qcs_policy']['schema_version'] = 'qcs-policy-v1'
    plan['backup_id'] = digest({k: v for k, v in plan.items() if k != 'backup_id'})
    with pytest.raises(ValueError, match='opt-in'): build(tmp_path, lambda r: pytest.fail('no POST'), plan)


def test_fresh_health_missing_degraded_or_stale_never_dispatches(tmp_path):
    core, health, time = build(tmp_path, lambda r: pytest.fail('no POST'))
    time[0] += timedelta(seconds=301)
    with pytest.raises(RuntimeError, match='healthy'): run(core)


def test_global_generation_post_cap_also_counts_tool_continuation_turns(tmp_path):
    limits = Limits(max_turns=8)
    calls = []
    class Tools(ArithmeticEnvironment):
        replayable_tools = ('work',)
        def tools_schema(self): return [{'type': 'function', 'function': {'name': 'work'}}]
        def call(self, name, arguments): return {'ok': True}
    def handler(request):
        p = json.loads(request.content)
        calls.append(p)
        return httpx.Response(200, json={'id': f'gen-tools-{len(calls)}', 'model': p['model'],
            'provider': 'Provider 0', 'usage': {'cost': '.01'}, 'choices': [{'message': {'tool_calls': [
                {'id': 'tool-' + str(len(calls)), 'function': {'name': 'work', 'arguments': '{}'}}]}}]})
    core, _, _ = build(tmp_path, handler, limits=limits)
    result = run(core, Tools)
    assert len(calls) == 3 and result['generation_calls'] == 3 and len(result['actions']) == 3


def test_explicit_none_omit_reasoning_reaches_exact_wire_and_audit(tmp_path):
    calls = []
    def handler(request):
        p = json.loads(request.content)
        calls.append(p)
        assert 'reasoning' not in p
        return response(p, len(calls))
    core, _, _ = build(tmp_path, handler, plan_for(wire='omit'))
    assert run(core)['status'] == 'passed' and len(calls) == 1
    assert core.read_completed('backup')['status'] == 'passed'


@pytest.mark.parametrize('allow,data,expected', [(False, 'public', False), (True, 'private', False),
                                                (True, 'public', True), (True, 'synthetic', True)])
def test_cloud_free_route_needs_explicit_opt_in_and_public_or_synthetic_data(tmp_path, allow, data, expected):
    plan = plan_for(wire='free')
    plan['qcs_policy']['allow_cloud_free_trial'] = allow
    rebind(plan)
    calls = []
    def handler(request):
        p = json.loads(request.content)
        calls.append(p)
        assert p['model'].endswith(':free')
        return response(p, len(calls))
    core, _, _ = build(tmp_path, handler, plan)
    kwargs = dict(contract_version='v1')
    if expected:
        result = core.run('backup', {**task(), 'data_policy': data}, ArithmeticEnvironment, **kwargs)
        assert result['status'] == 'passed' and len(calls) == 1
        assert result['attempts'][0]['profile']['model_version'] == 'example/open_source-0:free'
    else:
        with pytest.raises(RuntimeError, match='healthy'):
            core.run('backup', {**task(), 'data_policy': data}, ArithmeticEnvironment, **kwargs)
        assert calls == []


def test_saved_result_cannot_substitute_another_frozen_profiles_identity(tmp_path):
    calls = []
    def handler(request):
        p = json.loads(request.content)
        calls.append(p)
        return response(p, len(calls))
    core, _, _ = build(tmp_path, handler)
    run(core)
    path = core._manifest.parent / 'result.json'
    result = json.loads(path.read_text())
    call = result['attempts'][0]['calls'][0]
    alternative = core._backup['pools']['open_source'][1]
    call.update(profile=alternative['entry']['profile'], configuration_sha256=alternative['configuration_sha256'])
    path.write_text(json.dumps(result))
    with pytest.raises(RuntimeError, match='immutable dispatch intent'):
        core.read_completed('backup')
    assert len(calls) == 1


def test_failed_provider_is_excluded_across_later_quality_pools(tmp_path):
    calls = []
    def handler(request):
        p = json.loads(request.content)
        calls.append(p)
        return response(p, len(calls), status=429 if len(calls) == 1 else 200,
                        answer='bad' if len(calls) == 2 else '2')
    core, _, _ = build(tmp_path, handler)
    result = run(core)
    assert [a['config_id'] for a in result['attempts']] == ['open_source-0', 'open_source-1', 'mid-1']
    assert result['generation_calls'] == 3 and result['status'] == 'passed'
    saved = json.loads((core._manifest.parent / 'backup-selection-2.json').read_text())
    primary = next(r for r in saved['health']['mid'] if r['config_id'] == 'mid-0')
    assert 'provider_failed_this_run' in primary['reasons']


def test_health_changing_after_paid_classifier_stops_before_generation(tmp_path):
    plan = plan_for(classifiers=('low_mid',))
    calls, holder = [], {}
    def handler(request):
        p = json.loads(request.content)
        calls.append(p)
        if 'questions' not in p:
            return response(p, len(calls), answer='bad')
        core, health, time = holder['values']
        row = core._backup['pools']['mid'][0]
        new = observation(now=time[0], provider='provider-0', configuration=row['configuration_sha256'])
        new['health']['status'] = 1
        health.publish([new])
        return httpx.Response(200, json={'id': 'gen-health-decision', 'model': p['model'],
            'provider': 'TypeSafe', 'usage': {'cost': '.001', 'input_tokens': 1, 'output_tokens': 1},
            'answers': {'low_mid': {'type': 'choice', 'choice': 'mid',
                                   'probabilities': {'low': .2, 'mid': .8}}}})
    holder['values'] = build(tmp_path, handler, plan)
    with pytest.raises(RuntimeError, match='health observation'):
        run(holder['values'][0])
    assert len(calls) == 2


def test_generation_tool_execution_limit_is_global_across_turns(tmp_path):
    limits, calls, executions = Limits(max_turns=8, max_tool_calls=2), [], []
    class Tools(ArithmeticEnvironment):
        replayable_tools = ('work',)
        def tools_schema(self): return [{'type': 'function', 'function': {'name': 'work'}}]
        def call(self, name, arguments):
            executions.append(name)
            return {'ok': True}
    def handler(request):
        p = json.loads(request.content)
        calls.append(p)
        return httpx.Response(200, json={'id': f'gen-tool-limit-{len(calls)}', 'model': p['model'],
            'provider': 'Provider 0', 'usage': {'cost': '.01'}, 'choices': [{'message': {'tool_calls': [
                {'id': 'tool-' + str(len(calls)), 'function': {'name': 'work', 'arguments': '{}'}}]}}]})
    core, _, _ = build(tmp_path, handler, limits=limits)
    with pytest.raises(RuntimeError): run(core, Tools)
    assert len(executions) == 2 and len(calls) == 3
    assert core.budget_reservation_plan()['max_tool_calls'] == 2


@pytest.mark.parametrize('path', ['manifest.json', 'result.json', '.lock', 'backup-selection-0.json'])
def test_run_evidence_symlinks_are_rejected_without_post(tmp_path, path):
    core, _, _ = build(tmp_path, lambda r: pytest.fail('No POST'))
    core._manifest.parent.mkdir(parents=True)
    target = tmp_path / 'outside.json'
    target.write_text('{}')
    (core._manifest.parent / path).symlink_to(target)
    with pytest.raises(ValueError, match='symlink'): run(core)


def test_flow_cannot_borrow_calibration_after_health_policy_changes(tmp_path):
    plan = plan_for()
    plan['qcs_policy']['health_policy']['ttl_seconds'] = 999
    rebind(plan)
    with pytest.raises(ValueError, match='normalized health policy'):
        build(tmp_path, lambda r: pytest.fail('no POST'), plan)


def test_paid_classifier_unknown_fee_never_generates_or_resends(tmp_path):
    calls = []
    def handler(request):
        if request.method == 'GET': return httpx.Response(503)
        p = json.loads(request.content)
        calls.append(p)
        if 'questions' not in p: return response(p, len(calls), answer='bad')
        return httpx.Response(200, json={'id': 'gen-unknown-classifier', 'model': p['model'],
            'provider': 'TypeSafe', 'usage': {}, 'answers': {}})
    core, _, _ = build(tmp_path, handler, plan_for(classifiers=('low_mid',)))
    with pytest.raises(RuntimeError, match='incomplete billing'): run(core)
    assert len(calls) == 2
    with pytest.raises(RuntimeError, match='incomplete billing'): run(core)
    assert len(calls) == 2


def test_invalid_known_fee_classifier_falls_back_upper_without_extra_decision(tmp_path):
    calls = []
    def handler(request):
        p = json.loads(request.content)
        calls.append(p)
        if 'questions' not in p:
            return response(p, len(calls), answer='bad' if len(calls) == 1 else '2')
        return httpx.Response(200, json={'id': 'gen-invalid-classifier', 'model': p['model'],
            'provider': 'TypeSafe', 'usage': {'cost': '.001'}, 'answers': {}})
    core, _, _ = build(tmp_path, handler, plan_for(classifiers=('low_mid',)))
    result = run(core)
    assert result['status'] == 'passed'
    assert result['attempts'][-1]['tier'] == 'mid'
    assert result['selection']['classifier_calls'] == 1 and len(calls) == 3
    assert result['costs']['classifier_usd'] == '.001' or result['costs']['classifier_usd'] == '0.001'
    assert result['selection']['decisions'][-1]['fallback']


def test_single_local_oss_with_missing_high_pool_uses_guarded_local_dispatch_and_replay(tmp_path):
    from cloudyfan_jev.local import LocalBackend
    calls = []
    def local_handler(request):
        payload = json.loads(request.content)
        calls.append(payload)
        return httpx.Response(200, json={'model': payload['model'],
            'choices': [{'message': {'role': 'assistant', 'content': '2'}}]})
    local = LocalBackend(httpx.Client(transport=httpx.MockTransport(local_handler)), tmp_path / 'local',
        endpoint='http://127.0.0.1:9999/v1/chat/completions', model_version='example/open_source-0',
        quantization='q4-fixture', engine='fixture-engine', execution_location='home:fixture',
        no_model_fee_evidence='synthetic-operator-attestation')
    plan = plan_for(count=1, wire='omit')
    record = plan['pools']['open_source'][0]
    record['entry']['profile'].update(backend='local', quantization='q4-fixture', engine='fixture-engine',
        execution_location='home:fixture', endpoint='http://127.0.0.1:9999/v1/chat/completions', external_score=None)
    record['configuration_sha256'] = configuration_digest(record['entry'])
    record['evidence']['configuration_sha256'] = record['configuration_sha256']
    snapshot = plan['snapshot']
    snapshot['open_source'] = deepcopy(record['entry'])
    snapshot['open_source_pool'] = [deepcopy(record['entry'])]
    snapshot['pools']['high'] = []
    snapshot['snapshot_id'] = digest({k: v for k, v in snapshot.items() if k != 'snapshot_id'})
    plan['snapshot_id'] = snapshot['snapshot_id']
    plan['pools']['high'] = []
    plan['coverage']['counts']['high'] = 0
    plan['coverage']['complete'] = False
    plan['shortages']['high'] = 3
    _, decision = example_config()
    profiles = {tier: rows[0]['entry']['profile'] if rows else plan['pools']['mid'][0]['entry']['profile']
                for tier, rows in plan['pools'].items()}
    plan['qcs_policy']['execution_contract'] = Core(None, profiles, decision, limits=Limits(max_turns=1),
                                                   local_backend=local).execution_contract_descriptor('v1')
    rebind(plan)
    instant = datetime.fromisoformat(NOW)
    health = HealthStore(tmp_path / 'health', policy=HEALTH_POLICY, clock=lambda: instant)
    health.publish([observation(now=instant, provider=r['entry']['profile']['provider'],
                               configuration=r['configuration_sha256'])
                    for pool in plan['pools'].values() for r in pool])
    client = Client(httpx.Client(transport=httpx.MockTransport(lambda r: pytest.fail('No cloud POST'))),
                    'offline-fixture', tmp_path / 'cloud')
    core = BackupCategoryCore(client, plan, decision, run_id='backup', health_store=health,
                             local_backend=local, limits=Limits(max_turns=1), clock=lambda: instant)
    result = run(core)
    assert result['status'] == 'passed' and result['costs']['api_usd'] == '0'
    assert result['attempts'][0]['calls'][0]['execution_kind'] == 'local'
    assert core.read_completed('backup')['status'] == 'passed'
    assert run(core)['status'] == 'passed' and len(calls) == 1


class _BeforeDispatch(ArithmeticEnvironment):
    def tools_schema(self):
        raise RuntimeError('synthetic interruption before dispatch')


@pytest.mark.parametrize('field', ['profile', 'economics'])
def test_saved_selection_tamper_stops_before_any_new_paid_dispatch(tmp_path, field):
    calls = []
    def handler(request):
        payload = json.loads(request.content)
        calls.append(payload)
        return response(payload, len(calls))
    core, _, _ = build(tmp_path, handler)
    with pytest.raises(RuntimeError, match='before dispatch'):
        run(core, _BeforeDispatch)
    path = core._manifest.parent / 'backup-selection-0.json'
    saved = json.loads(path.read_text())
    if field == 'profile':
        saved['pair']['open_source']['entry']['profile'].update(
            model='example/unqualified', model_version='example/unqualified', accepted_models=['example/unqualified'])
    else:
        saved['economic']['selected_tier'] = 'high'
    path.write_text(json.dumps(saved))
    with pytest.raises(ValueError, match='Frozen backup selection'):
        run(core)
    assert calls == []


def test_resumed_new_dispatch_rechecks_selected_candidate_qualification_expiry(tmp_path):
    plan = plan_for()
    expiry = (datetime.fromisoformat(NOW) + timedelta(seconds=1)).isoformat()
    plan['pools']['open_source'][0]['evidence']['provenance']['valid_until'] = expiry
    rebind(plan)
    plan['qcs_policy']['complete_policy_flow']['provenance']['valid_until'] = '2026-10-22T00:00:00Z'
    plan['backup_id'] = digest({k: v for k, v in plan.items() if k != 'backup_id'})
    calls = []
    def handler(request):
        payload = json.loads(request.content)
        calls.append(payload)
        return response(payload, len(calls))
    core, _, time = build(tmp_path, handler, plan)
    with pytest.raises(RuntimeError, match='before dispatch'): run(core, _BeforeDispatch)
    time[0] += timedelta(seconds=2)
    restored, _, _ = build(tmp_path, handler, plan, time=time)
    with pytest.raises(ValueError, match='stale|expired'): run(restored)
    assert calls == []


def test_expired_optional_primary_is_excluded_while_fresh_backup_can_dispatch(tmp_path):
    plan = plan_for()
    plan['pools']['open_source'][0]['evidence']['provenance']['valid_until'] = (
        datetime.fromisoformat(NOW) + timedelta(seconds=1)).isoformat()
    rebind(plan)
    plan['qcs_policy']['complete_policy_flow']['provenance']['valid_until'] = '2026-10-22T00:00:00Z'
    plan['backup_id'] = digest({k: v for k, v in plan.items() if k != 'backup_id'})
    calls = []
    def handler(request):
        payload = json.loads(request.content)
        calls.append(payload)
        return response(payload, len(calls))
    time = [datetime.fromisoformat(NOW) + timedelta(seconds=2)]
    core, _, _ = build(tmp_path, handler, plan, time=time)
    result = run(core)
    assert result['status'] == 'passed' and len(calls) == 1
    assert result['attempts'][0]['config_id'] == 'open_source-1'


def test_interrupted_paid_answer_replays_after_qualification_expiry_without_new_post(tmp_path):
    plan = plan_for()
    plan['pools']['open_source'][0]['evidence']['provenance']['valid_until'] = (
        datetime.fromisoformat(NOW) + timedelta(seconds=1)).isoformat()
    rebind(plan)
    plan['qcs_policy']['complete_policy_flow']['provenance']['valid_until'] = '2026-10-22T00:00:00Z'
    plan['backup_id'] = digest({k: v for k, v in plan.items() if k != 'backup_id'})
    calls = []
    def handler(request):
        payload = json.loads(request.content)
        calls.append(payload)
        return response(payload, len(calls))
    class Interrupted(ArithmeticEnvironment):
        def validate(self, text): raise RuntimeError('validator interrupted')
    core, _, time = build(tmp_path, handler, plan)
    with pytest.raises(RuntimeError, match='validator interrupted'): run(core, Interrupted)
    time[0] += timedelta(seconds=2)
    restored, _, _ = build(tmp_path, handler, plan, time=time)
    assert run(restored)['status'] == 'passed' and len(calls) == 1


@pytest.mark.parametrize('mode', ['cloudyfan', 'low_first', 'open_source_first'])
def test_exhausted_same_pool_fallback_enters_next_quality_phase_with_remaining_call(tmp_path, mode):
    plan = plan_for(classifiers=('low_mid',) if mode == 'cloudyfan' else (), count=2)
    # Only one candidate can initially run: its provider failure exhausts this
    # quality pool while provider-1 candidates remain in the next quality pool.
    initial = 'low' if mode == 'low_first' else 'open_source'
    calls = []
    def handler(request):
        payload = json.loads(request.content)
        calls.append(payload)
        if 'questions' in payload:
            assert payload['state']['observations']['public_diagnostics']['passed'] is None
            assert payload['state']['observations']['previous_configuration']['model_version'].startswith(
                'example/open_source')
            return httpx.Response(200, json={'id': 'gen-upgrade-decision', 'model': payload['model'],
                'provider': 'TypeSafe', 'usage': {'cost': '.001', 'input_tokens': 1, 'output_tokens': 1},
                'answers': {'low_mid': {'type': 'choice', 'choice': 'mid',
                                       'probabilities': {'low': .2, 'mid': .8}}}})
        return response(payload, len(calls), status=503 if len(calls) == 1 else 200)
    core, health, time = build(tmp_path, handler, plan)
    blocked = plan['pools'][initial][1]
    row = observation(now=time[0], provider='provider-1', configuration=blocked['configuration_sha256'])
    row['health']['status'] = 1
    health.publish([row])
    result = run(core, mode=mode)
    assert result['status'] == 'passed' and result['generation_calls'] == 2
    assert len(result['attempts']) == 2
    assert result['attempts'][-1]['tier'] == ('mid' if mode == 'cloudyfan' else 'high')
    assert result['selection']['classifier_calls'] == (1 if mode == 'cloudyfan' else 0)


def test_classifier_contract_and_payload_bind_reasoning_selector_and_quantization():
    from cloudyfan_jev import routing
    from cloudyfan_jev.scores.backup_runtime import _public_profile
    plan = plan_for()
    _, decision = example_config()
    profiles = {tier: rows[0]['entry']['profile'] for tier, rows in plan['pools'].items()}
    previous = deepcopy(profiles['open_source'])
    before = backup_classifier_contract_hash(profiles, decision['version'], 'low_mid', 'open_source', previous)
    previous.update(reasoning_parameter='omit', quantization='q4-fixture')
    after = backup_classifier_contract_hash(profiles, decision['version'], 'low_mid', 'open_source', previous)
    assert before != after
    state = {'task': 'public task', 'previous_configuration': _public_profile(previous)}
    _, _, payload = routing.build_request('low_mid', state, profiles, decision['version'])
    identity = payload['state']['observations']['previous_configuration']
    assert identity['reasoning_parameter'] == 'omit' and identity['quantization'] == 'q4-fixture'
    profiles['mid'] = {**profiles['mid'], 'reasoning_parameter': 'effort', 'quantization': 'fp8'}
    _, _, payload = routing.build_request('low_mid', state, profiles, decision['version'])
    assert payload['state']['candidates']['mid']['quantization'] == 'fp8'
    assert payload['state']['candidates']['mid']['reasoning_parameter'] == 'effort'
    assert backup_classifier_contract_hash(profiles, decision['version'], 'low_mid', 'open_source', previous) != after


def test_two_availability_failures_exhaust_pool_then_jev_uses_last_global_generation_call(tmp_path):
    plan = plan_for(classifiers=('low_mid',), count=3)
    calls = []
    def handler(request):
        payload = json.loads(request.content)
        calls.append(payload)
        if 'questions' in payload:
            previous = payload['state']['observations']['previous_configuration']
            assert previous['model_version'] == 'example/open_source-1'
            return httpx.Response(200, json={'id': 'gen-last-call-decision', 'model': payload['model'],
                'provider': 'TypeSafe', 'usage': {'cost': '.001', 'input_tokens': 1, 'output_tokens': 1},
                'answers': {'low_mid': {'type': 'choice', 'choice': 'mid',
                                       'probabilities': {'low': .2, 'mid': .8}}}})
        return response(payload, len(calls), status=503 if len(calls) <= 2 else 200)
    core, health, time = build(tmp_path, handler, plan)
    unavailable = plan['pools']['open_source'][2]
    row = observation(now=time[0], provider='provider-2', configuration=unavailable['configuration_sha256'])
    row['health']['status'] = 1
    health.publish([row])
    result = run(core)
    assert result['status'] == 'passed' and result['generation_calls'] == 3 and len(calls) == 4
    assert [a['config_id'] for a in result['attempts']] == ['open_source-0', 'open_source-1', 'mid-2']
    assert result['selection']['classifier_calls'] == 1
    assert run(core)['status'] == 'passed' and len(calls) == 4


def test_shared_health_distinguishes_real_calls_across_receipt_roots_but_deduplicates_replay(tmp_path):
    calls = []
    def handler(request):
        payload = json.loads(request.content)
        calls.append(payload)
        return response(payload, len(calls), status=503)
    plan = plan_for()
    first, health, time = build(tmp_path / 'first', handler, plan)
    assert run(first)['generation_calls'] == 3
    cfg = plan['pools']['open_source'][0]['configuration_sha256']
    assert health.assess(cfg, 'provider-0')['circuit']['failures'] == 1
    _, decision = example_config()
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        second = BackupCategoryCore(Client(http, 'offline-token', tmp_path / 'second'), plan,
            decision, run_id='backup', health_store=health, clock=lambda: time[0], limits=Limits(max_turns=1))
        assert run(second)['generation_calls'] == 3
        circuit = health.assess(cfg, 'provider-0')['circuit']
        assert circuit['failures'] == 2 and circuit['open'] is True
        assert run(second)['generation_calls'] == 3
        assert health.assess(cfg, 'provider-0')['circuit'] == circuit
        assert len(calls) == 6


def test_saved_classifier_pair_becoming_degraded_stops_before_any_new_classification_fee(tmp_path):
    calls = []
    def handler(request):
        payload = json.loads(request.content)
        calls.append(payload)
        return response(payload, len(calls), answer='bad')
    plan = plan_for(classifiers=('low_mid',))
    engine, health, time = build(tmp_path, handler, plan)
    checked = engine._checked
    def interrupt(cid, url, payload, accepted, provider, *, allow_rejection=False):
        if 'questions' in payload:
            raise RuntimeError('controlled interruption before classifier dispatch')
        return checked(cid, url, payload, accepted, provider, allow_rejection=allow_rejection)
    engine._checked = interrupt
    with pytest.raises(RuntimeError, match='controlled interruption'):
        run(engine)
    rows = []
    for tier in ('low', 'mid'):
        record = plan['pools'][tier][0]
        row = observation(now=time[0], provider=record['entry']['profile']['provider'],
                          configuration=record['configuration_sha256'])
        row['health']['status'] = 1
        rows.append(row)
    health.publish(rows)
    resumed, _, _ = build(tmp_path, handler, plan, time=time)
    with pytest.raises(RuntimeError, match='classifier candidate health'):
        run(resumed)
    assert len(calls) == 1 and 'questions' not in calls[0]
    assert not list((resumed.client.root / 'calls').glob('*decision*'))

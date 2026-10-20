"""Synthetic preflight and Core.run matrix; no paid dispatch or hidden grading."""
from copy import deepcopy

import pytest

from cloudyfan_jev.diagnostics import DiagnosticContract
from cloudyfan_jev.engine import Limits, SYSTEM
from cloudyfan_jev.experiments import build_plan, verify_plan
from cloudyfan_jev.scores.catalog import digest
from cloudyfan_jev.scores.demo import example_inputs
from cloudyfan_jev.scores.qualification import build_qualification_pack
from test_qualification import NOW, inputs, reviewed


def setup_plan():
    from dataclasses import asdict
    obs, registry, policy = inputs()
    pack = build_qualification_pack(obs, registry, policy, review=reviewed(obs, registry, policy),
                                    now=NOW, synthetic=True)
    _, _, _, requirements, decision = example_inputs(NOW)
    def task(task_id, prompt, stratum):
        public = {'prompt': prompt}
        return {'task_id': task_id, 'stratum': stratum, 'task': public, 'input_sha256': digest(public)}
    spec = {'synthetic': True, 'tasks': [task('holdout-1', 'Return only the integer sum of 1 and 1.', 'easy')],
            'calibration_tasks': [task('calibration-1', 'Return the sum of 3 and 4.', 'easy')],
            'requirements': requirements, 'baseline_config_id': 'high', 'decision_profile': decision,
            'contract_version': 'sum-v1', 'system': SYSTEM, 'diagnostic_contract': DiagnosticContract().manifest(),
            'limits': asdict(Limits()), 'max_generation_attempts': {arm: 3 if arm=='cloudyfan' else 2 for arm in
                ['fixed_high','low_first','open_source_first','cloudyfan','open_source_fixed_low']},
            'repair_policy': 'sequential-public-diagnostic-repairs-v2',
            'arms': ['fixed_high', 'low_first', 'open_source_first', 'cloudyfan', 'open_source_fixed_low'],
            'budget': {'cap_usd': '10', 'next_block_upper_usd': '1', 'authorization_id': 'synthetic-only',
                       'expires_at': '2026-10-19T00:00:00Z', 'enforcement_evidence': 'fixture://bounded-reservation'},
            'quality_gate': {'required_final_pass_rate': '1'},
            'design_owner': 'designer', 'calibration_owner': 'tuner', 'grader_owner': 'independent-grader'}
    evidence = {'status': 'approved', 'reviewer': 'human-reviewer', 'reviewed_at': NOW,
                'evidence': ['fixture://explicit-human-review']}
    review = {'input_sha256': digest({'spec': spec, 'qualification_pack_id': pack['pack_id']}),
              'plan': dict(evidence), 'isolation': dict(evidence), 'grading': dict(evidence),
              'decision_profile': dict(evidence),
              'tasks': {row['task_id']: {**evidence, 'input_sha256': row['input_sha256'],
                                       'unresolved_ambiguities': []}
                        for row in spec['tasks'] + spec['calibration_tasks']}}
    return spec, pack, review


def test_reviewed_synthetic_plan_binds_ordered_tasks_and_five_arms():
    spec, pack, review = setup_plan()
    plan = build_plan(spec, pack, review, now=NOW)
    assert plan['status'] == 'synthetic_ready' and plan['blockers'] == []
    assert plan['paid_execution_authorized'] is False
    assert plan['baseline']['config_id'] == 'high'
    assert plan['spec']['arms'] == ['fixed_high', 'low_first', 'open_source_first', 'cloudyfan', 'open_source_fixed_low']
    assert plan['qualification_pack_id'] == pack['pack_id']
    verify_plan(plan, now=NOW, allow_synthetic=True)
    with pytest.raises(ValueError, match='Synthetic'):
        verify_plan(plan, now=NOW)


def test_missing_ambiguity_signoff_blocks_even_with_model_qualification():
    spec, pack, review = setup_plan()
    review['tasks']['holdout-1']['unresolved_ambiguities'] = ['Output order not specified']
    review['grading']['reviewer'] = ''
    plan = build_plan(spec, pack, review, now=NOW)
    assert plan['status'] == 'blocked'
    assert 'task:holdout-1:ambiguity_review_pending' in plan['blockers']
    assert 'grading:review_pending' in plan['blockers']
    with pytest.raises(ValueError, match='blocked'):
        verify_plan(plan, now=NOW, allow_synthetic=True)


@pytest.mark.parametrize('change,blocker', [
    ('overlap', 'isolation:overlapping_inputs'),
    ('hash', 'task:holdout-1:input_hash_mismatch'),
    ('grader', 'grading:not_independent'),
    ('duplicate', 'tasks:duplicate_ids'),
])
def test_preflight_rejects_contamination_and_missing_input_identity(change, blocker):
    spec, pack, review = setup_plan()
    if change == 'overlap':
        spec['calibration_tasks'][0]['task'] = deepcopy(spec['tasks'][0]['task'])
        spec['calibration_tasks'][0]['input_sha256'] = spec['tasks'][0]['input_sha256']
    elif change == 'hash':
        spec['tasks'][0]['task']['prompt'] = 'A changed task'
    elif change == 'grader':
        spec['grader_owner'] = spec['calibration_owner']
    else:
        spec['tasks'].append(deepcopy(spec['tasks'][0]))
    plan = build_plan(spec, pack, review, now=NOW)
    assert blocker in plan['blockers']
    assert plan['status'] == 'blocked'


def test_highest_baseline_cannot_be_replaced_by_cheapest_high_candidate():
    spec, _, review = setup_plan()
    obs, registry, policy = inputs()
    higher = deepcopy(registry['candidates'][-1])
    higher['config_id'] = 'highest'
    higher['source_model_id'] = 'example/highest'
    higher['profile']['model'] = higher['profile']['model_version'] = 'example/highest'
    higher['profile']['accepted_models'] = ['example/highest']
    higher['estimated_request_usd'] = 9
    higher['cost_basis'] = 'synthetic estimate'
    top_score = deepcopy(obs[-1])
    top_score.update(source_model_id='example/highest', score=150)
    registry['candidates'][-1].update(estimated_request_usd=1, cost_basis='synthetic estimate')
    registry['candidates'].append(higher)
    obs.append(top_score)
    pack = build_qualification_pack(obs, registry, policy, review=reviewed(obs, registry, policy),
                                    now=NOW, synthetic=True)
    review['input_sha256'] = digest({'spec': spec, 'qualification_pack_id': pack['pack_id']})
    plan = build_plan(spec, pack, review, now=NOW)
    assert 'baseline:not_highest_qualified_score' in plan['blockers']
    spec['baseline_config_id'] = 'highest'
    review['input_sha256'] = digest({'spec': spec, 'qualification_pack_id': pack['pack_id']})
    plan = build_plan(spec, pack, review, now=NOW)
    assert plan['status'] == 'synthetic_ready'
    assert plan['baseline']['config_id'] == 'highest'
    assert plan['candidates']['high']['model_version'] == 'example/high'


def test_missing_qualification_produces_blocking_report():
    spec, _, review = setup_plan()
    pack = build_qualification_pack(*inputs(), review={}, now=NOW, synthetic=True)
    plan = build_plan(spec, pack, review, now=NOW)
    assert 'qualification:unavailable' in plan['blockers']
    assert plan['status'] == 'blocked' and plan['baseline'] is None


@pytest.mark.parametrize('change,blocker', [
    ('attempts', 'execution:invalid_contract'),
    ('budget', 'budget:invalid_or_expired'),
    ('expiry', 'budget:invalid_or_expired'),
    ('arms', 'arms:unsupported_or_duplicate'),
])
def test_unbounded_or_unsupported_execution_parameters_block(change, blocker):
    spec, pack, review = setup_plan()
    if change == 'attempts': spec['max_generation_attempts'] = 3
    elif change == 'budget': spec['budget']['next_block_upper_usd'] = '11'
    elif change == 'expiry': spec['budget']['expires_at'] = '2026-10-17T00:00:00Z'
    else: spec['arms'].append('hosted_other')
    plan = build_plan(spec, pack, review, now=NOW)
    assert blocker in plan['blockers']


def test_frozen_plan_and_dispatch_binding_reject_midrun_changes(tmp_path):
    from cloudyfan_jev.experiments import execution_binding, verify_binding, write_plan, read_plan
    spec, pack, review = setup_plan()
    plan = build_plan(spec, pack, review, now=NOW)
    path = tmp_path / 'plan.json'
    write_plan(path, plan, now=NOW, allow_synthetic=True)
    restored = read_plan(path, now=NOW, allow_synthetic=True)
    assert restored['plan_id'] == plan['plan_id']
    bound = execution_binding(restored, 'cloudyfan', 'holdout-1', now=NOW, allow_synthetic=True)
    verify_binding(restored, bound, now=NOW, allow_synthetic=True)
    bound['profiles']['low']['effort'] = 'high'
    with pytest.raises(ValueError, match='binding'):
        verify_binding(restored, bound, now=NOW, allow_synthetic=True)
    restored['spec']['tasks'][0]['task']['prompt'] = 'Different prompt'
    with pytest.raises(ValueError, match='integrity'):
        verify_plan(restored, now=NOW, allow_synthetic=True)
    assert read_plan(path, now=NOW, allow_synthetic=True)['spec']['tasks'][0]['task']['prompt'].startswith('Return only')


def test_expired_authorization_stops_reusing_an_otherwise_fresh_plan():
    spec, pack, review = setup_plan()
    plan = build_plan(spec, pack, review, now=NOW)
    with pytest.raises(ValueError, match='budget'):
        verify_plan(plan, now='2026-10-19T01:00:00Z', allow_synthetic=True)


def test_attribution_arm_is_explicitly_plan_only_and_never_dispatches():
    from cloudyfan_jev.experiments import execution_binding
    spec, pack, review = setup_plan()
    plan = build_plan(spec, pack, review, now=NOW)
    assert plan['planned_only_arms'] == ['open_source_fixed_low']
    with pytest.raises(ValueError, match='plan-only'):
        execution_binding(plan, 'open_source_fixed_low', 'holdout-1', now=NOW, allow_synthetic=True)


@pytest.mark.parametrize('branch,expected_tiers', [
    ('pass', ['open_source']), ('low', ['open_source', 'low']),
    ('mid', ['open_source', 'mid']), ('high', ['open_source', 'mid', 'high']),
    ('engineering', None), ('accounting', None),
])
def test_frozen_synthetic_plan_through_core_branch_matrix(tmp_path, branch, expected_tiers):
    import json
    import httpx
    from cloudyfan_jev.client import Client
    from cloudyfan_jev.demo import ArithmeticEnvironment
    from cloudyfan_jev.engine import Core
    from cloudyfan_jev.experiments import execution_binding, verify_binding
    from cloudyfan_jev.ledger import summarize
    from test_engine import API

    spec, pack, review = setup_plan()
    plan = build_plan(spec, pack, review, now=NOW)
    bound = execution_binding(plan, 'cloudyfan', 'holdout-1', now=NOW, allow_synthetic=True)
    choices = (('low',) if branch == 'low' else ('mid',) if branch == 'mid' else ('mid', branch))
    api = API(first='2' if branch == 'pass' else '3', choices=choices,
              messages=[{'content': None, 'tool_calls': [{'function': {'name': 'bad'}}]}]
              if branch == 'engineering' else None)
    def endpoint(request):
        if request.method == 'GET':
            return httpx.Response(404, json={'error': 'synthetic unknown charge'})
        response = api(request)
        if branch == 'accounting':
            data = json.loads(response.content)
            del data['usage']['cost']
            return httpx.Response(200, json=data)
        return response
    diagnostic = deepcopy(bound['diagnostic_contract'])
    diagnostic['allowed_fields'] = tuple(diagnostic['allowed_fields'])
    with httpx.Client(transport=httpx.MockTransport(endpoint)) as http:
        client = Client(http, 'mock-only-key', tmp_path)
        engine = Core(client, bound['profiles'], bound['decision_profile'], limits=Limits(**bound['limits']),
                      diagnostic_contract=DiagnosticContract(**diagnostic), system=bound['system'])
        verify_binding(plan, bound, now=NOW, allow_synthetic=True)
        if expected_tiers:
            result = engine.run('matrix', bound['task'], ArithmeticEnvironment,
                                contract_version=bound['contract_version'], mode=bound['arm'])
            assert result['status'] == 'passed'
            assert [a['tier'] for a in result['attempts']] == expected_tiers
        else:
            with pytest.raises(RuntimeError):
                engine.run('matrix', bound['task'], ArithmeticEnvironment,
                           contract_version=bound['contract_version'], mode=bound['arm'])
            ledger = summarize(client)
            assert ledger['calls'] == 1
            assert ledger['unknown_calls'] == (1 if branch == 'accounting' else 0)
            assert ledger['total_api_usd'] == (None if branch == 'accounting' else '0.01')
            assert len(api.requests) == 1
        assert 'hidden_answer' not in json.dumps(api.requests)


def test_archive_integrity_is_separate_from_permission_to_start():
    from cloudyfan_jev.experiments import verify_plan_integrity
    spec, pack, review = setup_plan()
    plan = build_plan(spec, pack, review, now=NOW)
    verify_plan_integrity(plan)
    with pytest.raises(ValueError):
        verify_plan(plan, now='2027-01-01T00:00:00Z', allow_synthetic=True)
    plan['baseline']['config_id'] = 'unapproved'
    with pytest.raises(ValueError, match='integrity'):
        verify_plan_integrity(plan)

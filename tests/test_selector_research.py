"""New research version; real Core.run and mock HTTP, never paid calibration."""
import json

import pytest
import httpx

from cloudyfan_jev.scores.selector_research import (
    VERSION, CalibrationRequired, SelectorResearchCore, build_selector_research_core, selector_flow_contract_hash,
)
from cloudyfan_jev.engine import Limits
from cloudyfan_jev.scores.catalog import digest
from test_backup_runtime import build as build_backup, rebind, response
from test_workflow_research import build, research_settings, run


def test_missing_calibration_blocks_before_paid_first_answer(tmp_path):
    calls = []
    old, health, time = build(tmp_path, lambda request: calls.append(request))
    plan, policy = research_settings(count=1)
    with pytest.raises(CalibrationRequired, match='low_mid'):
        build_selector_research_core(old.client, plan, old.decision,
            research_policy=policy, run_id='backup', health_store=health,
            clock=lambda: time[0])
    assert calls == []


def settings():
    plan, policy = research_settings(classifiers=('low_mid', 'mid_high'), count=1)
    plan['qcs_policy']['execution_contract']['core']['selector_policy'] = 'evidence-required-not-fixed-direct'
    rebind(plan)
    policy['schema_version'] = VERSION
    policy['calibration_data_kind'] = 'offline_fixture'
    for stage in plan['qcs_policy']['stages'].values():
        for variant in stage['variants'].values():
            variant['calibration'].update(dataset_partition='independent_calibration',
                data_kind='offline_fixture', uses_evaluation_answers=False, sample_count=20,
                selector_policy_version=VERSION,
                execution_contract_sha256=digest(plan['qcs_policy']['execution_contract']))
    rebind(plan)
    policy['whole_flow']['contract_sha256'] = selector_flow_contract_hash(plan)
    return plan, policy


def test_both_selectors_actually_call_jev_with_latest_public_answer_and_replay(tmp_path):
    calls, decisions = [], []
    def handler(request):
        p = json.loads(request.content)
        calls.append(p)
        if 'questions' in p:
            decisions.append(p)
            stage = next(iter(p['questions']))
            pair = ('low', 'mid') if stage == 'low_mid' else ('mid', 'high')
            chosen = pair[1]
            return httpx.Response(200, json={'id': f'gen-selector-{len(calls)}', 'model': p['model'],
                'provider': 'TypeSafe', 'usage': {'cost': .001, 'input_tokens': 1, 'output_tokens': 1}, 'answers': {stage: {
                    'type': 'choice', 'choice': chosen,
                    'probabilities': {pair[0]: .2, pair[1]: .8}}}})
        answer = '2' if 'high' in p['model'] else 'latest-mid-bad' if 'mid' in p['model'] else 'oss-bad'
        return response(p, len(calls), answer=answer)
    plan, policy = settings()
    old, health, time = build_backup(tmp_path, handler, plan)
    core = build_selector_research_core(old.client, plan, old.decision,
        research_policy=policy, run_id='backup', health_store=health,
        clock=lambda: time[0], limits=Limits(max_turns=1))
    result = run(core)
    assert result['status'] == 'passed'
    assert result['selection']['classifier_calls'] == 2
    assert result['costs']['classifier_usd'] == '0.002'
    assert all(d['fallback'] is False for d in result['selection']['decisions'] if 'fallback' in d)
    assert core.budget_reservation_plan()['classifier_calls'] == 2
    assert decisions[1]['state']['observations']['previous_answer'] == 'latest-mid-bad'
    assert decisions[1]['state']['observations']['previous_configuration']['model_version'] == 'example/mid-0'
    assert run(core)['status'] == 'passed' and len(calls) == 5


def test_uncertain_profit_is_preparation_stop_not_silent_direct(tmp_path):
    plan, policy = settings()
    for variant in plan['qcs_policy']['stages']['low_mid']['variants'].values():
        variant['lower_probability'] = {'lower': '0', 'upper': '1'}
        variant['classified_paths']['mid']['cost_usd'] = {'lower': '.30', 'upper': '.30'}
    rebind(plan)
    policy['whole_flow']['contract_sha256'] = selector_flow_contract_hash(plan)
    old, health, time = build_backup(tmp_path, lambda request: pytest.fail('No paid dispatch'), plan)
    with pytest.raises(CalibrationRequired, match='low_mid') as error:
        build_selector_research_core(old.client, plan, old.decision,
            research_policy=policy, run_id='backup', health_store=health,
            clock=lambda: time[0], limits=Limits(max_turns=1))
    assert error.value.report['reason'] == 'classification_economics_uncertain'


def test_only_proven_unprofitable_selector_is_skipped_with_zero_fee(tmp_path):
    plan, policy = settings()
    for stage in plan['qcs_policy']['stages'].values():
        for variant in stage['variants'].values():
            variant['classifier_cost_usd'] = {'lower': '10', 'upper': '10'}
    rebind(plan)
    policy['whole_flow']['contract_sha256'] = selector_flow_contract_hash(plan)
    calls = []
    def handler(request):
        payload = json.loads(request.content)
        assert 'questions' not in payload
        calls.append(payload)
        return response(payload, len(calls), answer='bad' if len(calls) == 1 else '2')
    old, health, time = build_backup(tmp_path, handler, plan)
    core = build_selector_research_core(old.client, plan, old.decision,
        research_policy=policy, run_id='backup', health_store=health,
        clock=lambda: time[0], limits=Limits(max_turns=1))
    result = run(core)
    assert result['status'] == 'passed' and result['selection']['classifier_calls'] == 0
    assert result['costs']['classifier_usd'] == '0'
    assert result['selection']['decisions'][-1]['reason'] == 'classification_proven_not_economic'
    assert core.budget_reservation_plan()['classifier_calls'] == 0


@pytest.mark.parametrize('choice', ['low', 'mid'])
def test_first_selector_can_choose_either_candidate(tmp_path, choice):
    calls = []
    def handler(request):
        p = json.loads(request.content)
        calls.append(p)
        if 'questions' in p:
            return httpx.Response(200, json={'id': 'gen-binary-choice', 'model': p['model'],
                'provider': 'TypeSafe', 'usage': {'cost': .001, 'input_tokens': 1, 'output_tokens': 1}, 'answers': {'low_mid': {
                    'type': 'choice', 'choice': choice,
                    'probabilities': {'low': .8 if choice == 'low' else .2,
                                      'mid': .8 if choice == 'mid' else .2}}}})
        return response(p, len(calls), answer='bad' if len(calls) == 1 else '2')
    plan, policy = settings()
    old, health, time = build_backup(tmp_path, handler, plan)
    core = build_selector_research_core(old.client, plan, old.decision,
        research_policy=policy, run_id='backup', health_store=health,
        clock=lambda: time[0], limits=Limits(max_turns=1))
    result = run(core)
    assert result['status'] == 'passed'
    assert [a['tier'] for a in result['attempts']] == ['open_source', choice]
    assert result['selection']['classifier_calls'] == 1
    assert result['selection']['decisions'][-1]['fallback'] is False


@pytest.mark.parametrize('field,value', [
    ('dataset_partition', 'formal_evaluation'), ('uses_evaluation_answers', True),
    ('sample_count', 0), ('previous_configuration_sha256', '0'*64),
    ('classifier_version', 'a-different-version'),
])
def test_inapplicable_calibration_never_enables_paid_dispatch(tmp_path, field, value):
    plan, policy = settings()
    old, health, time = build_backup(tmp_path, lambda request: pytest.fail('No dispatch'), plan)
    for v in plan['qcs_policy']['stages']['low_mid']['variants'].values():
        v['calibration'][field] = value
    rebind(plan)
    policy['whole_flow']['contract_sha256'] = selector_flow_contract_hash(plan)
    with pytest.raises(ValueError):
        build_selector_research_core(old.client, plan, old.decision,
            research_policy=policy, run_id='backup', health_store=health,
            clock=lambda: time[0], limits=Limits(max_turns=1))


@pytest.mark.parametrize('factory', [build_selector_research_core, SelectorResearchCore])
@pytest.mark.parametrize('mounted', [False, True])
def test_offline_calibration_cannot_activate_a_network_transport(tmp_path, factory, mounted):
    from cloudyfan_jev.client import Client
    plan, policy = settings()
    _, health, time = build_backup(tmp_path, lambda request: pytest.fail('No dispatch'), plan)
    options = ({'transport': httpx.MockTransport(lambda request: pytest.fail('No dispatch')),
                'mounts': {'https://': httpx.HTTPTransport()}} if mounted else {})
    with httpx.Client(**options) as http:
        client = Client(http, 'not-a-real-key', tmp_path/'unused-live-shape')
        with pytest.raises(ValueError, match='MockTransport'):
            factory(client, plan, client_decision(),
                research_policy=policy, run_id='backup', health_store=health,
                clock=lambda: time[0], limits=Limits(max_turns=1))


def client_decision():
    from cloudyfan_jev.demo import example_config
    return example_config()[1]


def test_transport_swap_after_preparation_is_rejected_before_run(tmp_path):
    plan, policy = settings()
    old, health, time = build_backup(tmp_path, lambda request: pytest.fail('No dispatch'), plan)
    core = build_selector_research_core(old.client, plan, old.decision,
        research_policy=policy, run_id='backup', health_store=health,
        clock=lambda: time[0], limits=Limits(max_turns=1))
    with httpx.Client() as live_http:
        old.client.http = live_http
        with pytest.raises(ValueError, match='MockTransport'):
            run(core)


def test_fixture_rejects_client_call_override_even_with_mock_transport(tmp_path):
    from cloudyfan_jev.client import Client
    from cloudyfan_jev.scores.selector_research import require_mock_transport

    class OverrideClient(Client):
        def call(self, *args, **kwargs):
            pytest.fail('Custom call must not bypass the fixture network guard')

    with httpx.Client(transport=httpx.MockTransport(lambda _: pytest.fail('No HTTP'))) as http:
        client = OverrideClient(http, 'mock-only', tmp_path / 'overridden')
        with pytest.raises(ValueError, match='MockTransport'):
            require_mock_transport(client)

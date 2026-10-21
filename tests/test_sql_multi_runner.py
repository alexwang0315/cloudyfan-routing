"""Exact-setting SQL exams deduplicate slots without weakening paid-call safety."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path

import httpx
import pytest

from cloudyfan_jev.budget import Authorization, BudgetError, BudgetStore, EnforcedBound
from cloudyfan_jev.client import Client
from cloudyfan_jev.scores.configuration import configuration_digest
from cloudyfan_jev.scores.sql_exam import cases
from cloudyfan_jev.scores.sql_multi_runner import (
    MultiCandidateQualificationRunner, prepare_multi_candidate_table,
)
from cloudyfan_jev.scores.sql_runner import CaptureStopped
from cloudyfan_jev.sql import SqlValidationUnavailable

ROOT = Path(__file__).resolve().parents[1]
PROVIDERS = {'mock/a': 'Mock A', 'mock/b': 'Mock B', 'mock/c': 'Mock C', 'mock/d': 'Mock D'}


def entries():
    template = json.loads((ROOT / 'examples/sql-model-versions-v1.json').read_text())['configurations'][0]['entry']
    result = []
    for index, (model, provider, effort) in enumerate([
        ('example/sql-one-v1', 'mock/a', 'low'),
        ('example/sql-one-v1', 'mock/b', 'low'),
        ('example/sql-three:free', 'mock/a', 'none'),
        ('example/sql-four-v1', 'mock/c', 'high'),
        ('example/sql-five-v1', 'mock/d', 'medium'),
    ]):
        entry = deepcopy(template)
        entry['config_id'] = f'synthetic-setting-{index}'
        entry['profile'].update(model=model, model_version=model, accepted_models=[model],
                                provider=provider, provider_name=PROVIDERS[provider], effort=effort)
        if effort == 'none':
            entry['profile']['reasoning_parameter'] = 'omit'
        result.append(entry)
    return result


def table_fixture():
    candidates = entries()
    ids = [configuration_digest(e) for e in candidates]
    duplicate = deepcopy(candidates[0])
    duplicate['config_id'] = 'same-executable-setting-another-slot'
    return prepare_multi_candidate_table(candidates + [duplicate], slots={
        'open_source': [ids[0], ids[2]], 'low': [ids[2]],
        'mid': [ids[1], ids[3]], 'high': [ids[4]],
    })


def test_model_label_aliases_cannot_count_as_distinct_pool_backups():
    candidates = entries()[:2]
    candidates[1]['profile']['model'] = 'example/sql-one-label-alias'
    ids = [configuration_digest(entry) for entry in candidates]
    # The same request version remains independently examinable across pools.
    assert len(prepare_multi_candidate_table(candidates)['configurations']) == 2
    with pytest.raises(ValueError, match='different models'):
        prepare_multi_candidate_table(candidates, slots={'low': ids})


def response(payload, n, answer='OK', *, cost='0.001', status=200, model=None, provider=None):
    return httpx.Response(status, json={'id': f'gen-multi-test-{n}',
        'model': model or payload['model'],
        'provider': provider or PROVIDERS[payload['provider']['only'][0]],
        'choices': [{'message': {'content': answer}}], 'usage': {'cost': cost}})


def setup(tmp_path, handler=None, *, balance='50', bound_values=None, environment_factory=None):
    table = table_fixture()
    calls = []
    # Fixture assessor only: never injected into capture or selection input.
    answers = {c.contract().task()['prompt']: c.answer() for c in cases()}

    def upstream(request):
        assert request.method == 'POST', 'No implicit GET/retry in qualification capture'
        payload = json.loads(request.content)
        calls.append(payload)
        if handler is not None:
            return handler(payload, len(calls))
        return response(payload, len(calls), answers.get(payload['messages'][1]['content'], 'OK'))

    client = Client(httpx.Client(transport=httpx.MockTransport(upstream)),
                    'synthetic-' + 'credential', tmp_path / 'client')
    store = BudgetStore(tmp_path / 'budget.sqlite')
    store.authorize(Authorization('mock-only', Decimal(balance),
                    (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()))
    bounds = {c['configuration_sha256']: EnforcedBound(
        (bound_values or {}).get(c['entry']['profile']['provider'], '0.01'), 'synthetic-cap')
        for c in table['configurations']}
    kwargs = {} if environment_factory is None else {
        'environment_factory': environment_factory, 'environment_version': 'mock-public-interruption-v1'}
    runner = MultiCandidateQualificationRunner(client, store, table, model_bounds=bounds,
        verify_bound=lambda *args: True, synthetic=True, **kwargs)
    return runner, calls, store, table


def test_five_distinct_settings_rotating_order_and_shared_slots_cost_only_once(tmp_path):
    runner, calls, _, table = setup(tmp_path)
    assert len(table['configurations']) == 5
    result = runner.capture('multi', max_new_requests=15)
    expected = [(c['entry']['profile']['model_version'], c['entry']['profile']['provider'])
                for c in table['configurations']]
    observed = [(p['model'], p['provider']['only'][0]) for p in calls]
    assert observed[:5] == expected
    assert observed[5:10] == expected
    assert observed[10:15] == expected[1:] + expected[:1]
    assert result['status'] == 'paused' and result['calls'] == 15
    assert result['candidate_count'] == 5 and result['slot_count'] == 6
    assert result['classifier_calls'] == 0 and result['reserve_usd'] == '5.05'
    assert runner.capture('multi', max_new_requests=0)['calls'] == 15 and len(calls) == 15


def test_same_model_different_provider_uses_each_exact_setting_bound(tmp_path):
    runner, calls, _, _ = setup(tmp_path, bound_values={'mock/a': '0.01', 'mock/b': '0.04'})
    verified = []

    def verify(kind, model, payload, bound):
        verified.append((payload['provider']['only'][0], Decimal(bound.usd)))
        return True

    runner.verify_bound = verify
    result = runner.capture('multi', max_new_requests=2)
    assert calls[0]['model'] == calls[1]['model']
    assert calls[0]['provider'] != calls[1]['provider']
    assert result['reserve_usd'] == '8.08'
    assert all(amount == Decimal('0.04' if provider == 'mock/b' else '0.01')
               for provider, amount in verified)


@pytest.mark.parametrize('cost,status,model,reason', [
    (None, 200, None, 'unknown_fee_or_generation_identity'),
    ('0.001', 429, None, 'provider_failure'),
    ('0.001', 200, 'example/wrong', 'model_or_provider_mismatch'),
])
def test_failures_stop_all_following_candidates_and_keep_fee_evidence(tmp_path, cost, status, model, reason):
    runner, calls, store, _ = setup(tmp_path,
        lambda p, n: response(p, n, cost=cost, status=status, model=model))
    result = runner.capture('multi')
    assert result['status'] == 'stopped' and result['reason'] == reason
    assert result['unknown_cost_count'] == int(cost is None)
    assert runner.capture('multi')['status'] == 'stopped' and len(calls) == 1
    assert store.report()['reserved_usd'] == '5.05'


def test_empty_answer_repairs_same_setting_with_only_public_diagnostic(tmp_path):
    answers = {c.contract().task()['prompt']: c.answer() for c in cases()}

    def upstream(p, n):
        return response(p, n, '' if n == 6 else answers.get(p['messages'][1]['content'], 'OK'))

    runner, calls, _, _ = setup(tmp_path, upstream)
    result = runner.capture('multi', max_new_requests=7)
    rows = runner.records('multi', runner.roles[0])
    assert [a['answer'] for a in rows[0]['attempts']] == ['', cases()[0].answer()]
    assert calls[5]['model'] == calls[6]['model'] and calls[5]['provider'] == calls[6]['provider']
    diagnostic = json.loads(calls[6]['messages'][-1]['content'].split(': ', 1)[1])
    assert set(diagnostic) == {'passed', 'checks', 'diagnostic_version', 'diagnostic_source'}
    assert diagnostic['passed'] is False and result['known_api_usd'] == '0.007'


def test_saved_answer_resumes_validation_without_another_post(tmp_path):
    interrupted = []

    def environment(case):
        original = case.contract().environment()

        class Boundary:
            def validate(self, answer):
                if not interrupted:
                    interrupted.append(True)
                    raise SqlValidationUnavailable('SQL_VALIDATION_TIMEOUT')
                return original.validate(answer)
        return Boundary()

    runner, calls, _, _ = setup(tmp_path, environment_factory=environment)
    result = runner.capture('multi')
    assert result['status'] == 'stopped' and result['calls'] == 6
    assert result['reason'] == 'SqlValidationUnavailable'
    resumed = runner.capture('multi', max_new_requests=0)
    assert resumed['status'] == 'paused' and resumed['calls'] == 6 and len(calls) == 6
    assert runner.records('multi', runner.roles[0])[0]['attempts'][0]['answer'] == cases()[0].answer()


def test_free_route_is_not_rewritten_and_nonreasoning_field_is_absent(tmp_path):
    runner, calls, _, _ = setup(tmp_path)
    runner.capture('multi', max_new_requests=3)
    assert calls[2]['model'] == 'example/sql-three:free' and 'reasoning' not in calls[2]


def test_missing_exact_bound_and_insufficient_budget_do_not_dispatch(tmp_path):
    runner, calls, store, table = setup(tmp_path)
    bounds = dict(runner.bounds)
    bounds.pop(table['configurations'][1]['configuration_sha256'])
    with pytest.raises((ValueError, BudgetError)):
        MultiCandidateQualificationRunner(runner.client, store, table, model_bounds=bounds,
            verify_bound=lambda *args: True, synthetic=True)
    assert not calls
    limited, limited_calls, _, _ = setup(tmp_path / 'limited', balance='5.04')
    assert limited.capture('multi')['status'] == 'stopped' and not limited_calls


def test_changed_slot_manifest_refuses_resume_and_incomplete_exam_cannot_grade(tmp_path):
    runner, calls, store, table = setup(tmp_path)
    runner.capture('multi', max_new_requests=1)
    with pytest.raises(CaptureStopped, match='capture_must_complete_before_grading'):
        runner.grade('multi', now=datetime.now(timezone.utc).isoformat())
    changed = deepcopy(table)
    changed['slots']['high'] = [changed['configurations'][0]['configuration_sha256']]
    replacement = MultiCandidateQualificationRunner(runner.client, store, changed,
        model_bounds=runner.bounds, verify_bound=lambda *args: True, synthetic=True)
    with pytest.raises(ValueError, match='changed'):
        replacement.capture('multi')
    assert len(calls) == 1


def test_mock_transport_cannot_be_labeled_real_and_real_transport_cannot_be_synthetic(tmp_path):
    runner, calls, store, table = setup(tmp_path)
    with pytest.raises(ValueError, match='MockTransport'):
        MultiCandidateQualificationRunner(runner.client, store, table,
            model_bounds=runner.bounds, verify_bound=lambda *args: True, synthetic=False)

    class NoNetworkTransport(httpx.BaseTransport):
        def handle_request(self, request):
            raise AssertionError('No real transport may execute in this test')

    real_client = Client(httpx.Client(transport=NoNetworkTransport()),
                         'synthetic-' + 'credential', tmp_path / 'other')
    with pytest.raises(ValueError, match='MockTransport'):
        MultiCandidateQualificationRunner(real_client, store, table,
            model_bounds=runner.bounds, verify_bound=lambda *args: True, synthetic=True)
    assert not calls


@pytest.mark.parametrize('field,value', [('effort', None), ('provider', None), ('model_version', None)])
def test_unresolved_identity_never_becomes_an_executable_exam(field, value):
    candidate = entries()[0]
    candidate['profile'][field] = value
    with pytest.raises(ValueError):
        prepare_multi_candidate_table([candidate])


def test_changed_effort_or_omission_is_not_deduplicated_as_the_same_setting():
    first = entries()[0]
    different = deepcopy(first)
    different['profile']['effort'] = 'high'
    omission = deepcopy(first)
    omission['profile'].update(effort='none', reasoning_parameter='omit')
    assert len(prepare_multi_candidate_table([first, different, omission])['configurations']) == 3


def test_provider_copies_can_cross_pools_but_do_not_fill_one_pools_model_backups():
    candidates = entries()[:2]
    ids = [configuration_digest(entry) for entry in candidates]
    cross_pool = prepare_multi_candidate_table(candidates, slots={'low': [ids[0]], 'mid': [ids[1]]})
    assert len(cross_pool['configurations']) == 2
    with pytest.raises(ValueError, match='different models'):
        prepare_multi_candidate_table(candidates, slots={'low': ids})


def test_alias_response_list_is_not_silently_narrowed_into_an_exact_exam_setting():
    candidate = entries()[0]
    candidate['profile']['accepted_models'].append('example/sql-one-alias')
    original = deepcopy(candidate)
    with pytest.raises(ValueError, match='pinned'):
        prepare_multi_candidate_table([candidate])
    assert candidate == original

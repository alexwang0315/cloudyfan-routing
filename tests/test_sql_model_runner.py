"""Qualification capture through the real Client, budget and frozen SQL boundary."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json

import httpx

from cloudyfan_jev.budget import Authorization, BudgetStore, EnforcedBound
from cloudyfan_jev.client import Client
from cloudyfan_jev.scores.sql_exam import cases
from cloudyfan_jev.scores.sql_runner import QualificationRunner
from sql_frozen_runtime import local_version_table


def setup_runner(tmp_path, handler=None, *, balance='10', table=None):
    table = local_version_table() if table is None else table
    calls = []
    answers = {c.contract().task()['prompt']: c.answer() for c in cases()}

    def upstream(request):
        payload = json.loads(request.content)
        calls.append(payload)
        if handler is not None:
            return handler(payload, len(calls))
        prompt = payload['messages'][1]['content']
        return response(payload, len(calls), answers.get(prompt, 'OK'))

    http = httpx.Client(transport=httpx.MockTransport(upstream))
    client = Client(http, 'fixture-' + 'credential', tmp_path / 'capture')
    store = BudgetStore(tmp_path / 'budget.sqlite')
    store.authorize(Authorization('mock-only', Decimal(balance),
                                 (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()))
    bounds = {c['entry']['profile']['model_version']: EnforcedBound('0.01', 'synthetic-cap')
              for c in table['configurations']}
    runner = QualificationRunner(client, store, table, model_bounds=bounds,
                                 verify_bound=lambda *args: True, synthetic=True)
    return runner, calls, store, table


def response(payload, n, answer, *, cost='0.001', model=None, status=200):
    provider = 'BaseTen' if payload['provider']['only'] == ['baseten/fp4'] else 'OpenAI'
    return httpx.Response(status, json={'id': f'gen-exam-{n}',
        'model': model or payload['model'], 'provider': provider,
        'choices': [{'message': {'content': answer}}], 'usage': {'cost': cost}})


def test_capture_freezes_four_identities_then_rotates_roles_and_replays_without_post(tmp_path):
    runner, calls, store, table = setup_runner(tmp_path)
    result = runner.capture('exam', max_new_requests=8)
    assert result['status'] == 'paused'
    assert result['calls'] == 8 and result['known_api_usd'] == '0.008'
    assert result['classifier_calls'] == 0 and result['synthetic'] is True
    assert [p['model'] for p in calls[:4]] == [c['entry']['profile']['model_version'] for c in table['configurations']]
    assert [p['model'] for p in calls[4:8]] == [c['entry']['profile']['model_version'] for c in table['configurations']]
    assert result['reserve_usd'] == '4.04'
    assert runner.capture('exam', max_new_requests=0)['calls'] == 8
    assert len(calls) == 8
    assert store.report()['unknown_events'] == 0
    assert runner.records('exam', 'low')[0]['attempts'][0]['answer'] == cases()[0].answer()


def test_saved_answer_resumes_after_public_validator_interruption_without_resend(tmp_path):
    from cloudyfan_jev.sql import SqlValidationUnavailable
    runner, calls, store, table = setup_runner(tmp_path)
    interrupted = []

    def environment(case):
        real = case.contract().environment()

        class Boundary:
            def validate(self, answer):
                if not interrupted:
                    interrupted.append(True)
                    raise SqlValidationUnavailable('SQL_VALIDATION_TIMEOUT')
                return real.validate(answer)
        return Boundary()

    runner = QualificationRunner(runner.client, store, table, model_bounds=runner.bounds,
        verify_bound=lambda *args: True, synthetic=True,
        environment_factory=environment, environment_version='injected-interruption-v1')
    stopped = runner.capture('exam', max_new_requests=5)
    assert stopped['status'] == 'stopped' and stopped['reason'] == 'SqlValidationUnavailable'
    assert len(calls) == 5 and stopped['unknown_cost_count'] == 0
    resumed = runner.capture('exam', max_new_requests=0)
    assert resumed['status'] == 'paused' and resumed['calls'] == 5
    assert len(calls) == 5 and store.report()['reserved_usd'] == '4.04'


def test_only_public_failure_repairs_same_model_and_never_a_third_attempt(tmp_path):
    answers = {c.contract().task()['prompt']: c.answer() for c in cases()}

    def upstream(payload, n):
        prompt = payload['messages'][1]['content']
        answer = 'invalid-json' if n == 5 else answers.get(prompt, 'OK')
        return response(payload, n, answer)

    runner, calls, _, _ = setup_runner(tmp_path, upstream)
    result = runner.capture('exam', max_new_requests=6)
    attempts = runner.records('exam', 'open_source')[0]['attempts']
    assert len(attempts) == 2 and attempts[0]['answer'] == 'invalid-json'
    assert attempts[1]['answer'] == cases()[0].answer()
    assert calls[4]['model'] == calls[5]['model']
    diagnostic = json.loads(calls[5]['messages'][-1]['content'].split(': ', 1)[1])
    assert diagnostic['passed'] is False and diagnostic['checks']
    assert set(diagnostic) == {'passed', 'checks', 'diagnostic_version', 'diagnostic_source'}
    assert cases()[0].contract(grading=True)._public_json not in json.dumps(calls[5])
    assert result['known_api_usd'] == '0.006' and result['calls'] == 6
    assert runner.capture('exam', max_new_requests=0)['status'] == 'paused'
    assert len(calls) == 6


def test_two_failed_answers_do_not_trigger_a_third_or_change_model(tmp_path):
    runner, calls, _, _ = setup_runner(tmp_path,
        lambda payload, n: response(payload, n, 'invalid-json'))
    result = runner.capture('exam', max_new_requests=6)
    assert result['status'] == 'paused'
    assert len(runner.records('exam', 'open_source')[0]['attempts']) == 2
    assert runner.capture('exam', max_new_requests=0)['calls'] == 6
    assert len(calls) == 6


def test_unknown_charge_stops_all_roles_and_does_not_retry_or_implicitly_reconcile(tmp_path):
    runner, calls, store, _ = setup_runner(tmp_path,
        lambda payload, n: response(payload, n, 'OK', cost=None))
    result = runner.capture('exam')
    assert result['status'] == 'stopped' and result['unknown_cost_count'] == 1
    assert result['api_usd'] is None and result['reserve_usd'] == '4.04'
    assert runner.capture('exam')['status'] == 'stopped'
    assert len(calls) == 1 and store.report()['unknown_events'] == 1
    assert store.report()['reserved_usd'] == '4.04'


def test_wrong_model_stops_every_following_request_but_keeps_charged_cost(tmp_path):
    runner, calls, _, _ = setup_runner(tmp_path,
        lambda payload, n: response(payload, n, 'OK', model='unrequested-model'))
    result = runner.capture('exam')
    assert result['status'] == 'stopped' and result['reason'] == 'model_or_provider_mismatch'
    assert result['known_api_usd'] == '0.001' and result['unknown_cost_count'] == 0
    assert runner.capture('exam')['status'] == 'stopped'
    assert len(calls) == 1


def test_paid_429_is_retained_and_stops_the_exam_without_hidden_retry(tmp_path):
    runner, calls, _, _ = setup_runner(tmp_path,
        lambda payload, n: response(payload, n, '', status=429))
    result = runner.capture('exam')
    assert result['reason'] == 'provider_failure'
    assert result['known_api_usd'] == '0.001'
    assert runner.capture('exam')['status'] == 'stopped'
    assert len(calls) == 1


def test_full_404_call_reserve_and_enforcement_are_required_before_any_post(tmp_path):
    runner, calls, _, _ = setup_runner(tmp_path, balance='4.03')
    result = runner.capture('exam')
    assert result['status'] == 'stopped' and result['reason'] == 'BudgetError'
    assert not calls and result['calls'] == 0
    runner, calls, _, _ = setup_runner(tmp_path / 'other')
    runner.verify_bound = lambda *args: False
    assert runner.capture('exam')['status'] == 'stopped'
    assert not calls


def test_changed_frozen_inputs_refuse_resume_and_cannot_grade_incomplete_capture(tmp_path):
    import pytest
    runner, calls, store, table = setup_runner(tmp_path)
    runner.capture('exam', max_new_requests=1)
    with pytest.raises(FileNotFoundError):
        runner.grade('exam', now=datetime.now(timezone.utc).isoformat())
    changed = deepcopy(table)
    changed['configurations'][0]['price_evidence']['retrieved_at'] = '2030-01-01T00:00:00+00:00'
    other = QualificationRunner(runner.client, store, changed, model_bounds=runner.bounds,
        verify_bound=lambda *args: True, synthetic=True)
    with pytest.raises(ValueError, match='changed'):
        other.capture('exam')
    assert len(calls) == 1


def test_second_task_rotates_first_model_and_exact_payload_has_no_plugins_or_tools(tmp_path):
    runner, calls, _, _ = setup_runner(tmp_path)
    result = runner.capture('exam', max_new_requests=12)
    assert result['calls'] == 12
    assert [p['model'] for p in calls[8:12]] == [calls[i]['model'] for i in (5, 6, 7, 4)]
    for payload in calls:
        assert payload['provider']['allow_fallbacks'] is False
        assert payload['provider']['require_parameters'] is True
        assert not any(k in payload for k in ['tools', 'plugins', 'stream'])


def test_unknown_fee_can_be_explicitly_reconciled_then_resumed_without_post(tmp_path):
    runner, calls, store, _ = setup_runner(tmp_path,
        lambda payload, n: response(payload, n, 'OK', cost=None))
    assert runner.capture('exam')['unknown_cost_count'] == 1
    model = calls[0]['model']
    gets = []

    def receipt(request):
        gets.append(request.method)
        assert request.method == 'GET'
        return httpx.Response(200, json={'data': {'id': request.url.params['id'],
            'total_cost': '0.002', 'model': model, 'provider_name': 'BaseTen'}})

    with httpx.Client(transport=httpx.MockTransport(receipt)) as http:
        alternate = Client(http, 'fixture-' + 'credential', runner.client.root)
        call_id = store.report()['events'][0]['id'].split(':')[-1]
        alternate.reconcile(call_id)
    result = runner.capture('exam', max_new_requests=0)
    assert result['status'] == 'paused' and result['unknown_cost_count'] == 0
    assert result['known_api_usd'] == '0.002' and len(calls) == 1 and gets == ['GET']


def test_synthetic_transport_cannot_be_labeled_real(tmp_path):
    import pytest
    runner, _, store, table = setup_runner(tmp_path)
    with pytest.raises(ValueError, match='MockTransport'):
        QualificationRunner(runner.client, store, table, model_bounds=runner.bounds,
                            verify_bound=lambda *args: True, synthetic=False)


def test_billed_empty_text_uses_public_repair_without_discarding_cost(tmp_path):
    answers = {c.contract().task()['prompt']: c.answer() for c in cases()}

    def upstream(payload, n):
        answer = '' if n == 5 else answers.get(payload['messages'][1]['content'], 'OK')
        return response(payload, n, answer)

    runner, calls, _, _ = setup_runner(tmp_path, upstream)
    result = runner.capture('exam', max_new_requests=6)
    attempts = runner.records('exam', 'open_source')[0]['attempts']
    assert len(attempts) == 2 and attempts[0]['answer'] == ''
    assert result['known_api_usd'] == '0.006' and len(calls) == 6


def test_duplicate_generation_id_keeps_shared_stop_and_every_attempt_event(tmp_path):
    def upstream(payload, n):
        reply = response(payload, 1, 'OK')
        return reply

    runner, calls, store, _ = setup_runner(tmp_path, upstream)
    result = runner.capture('exam')
    assert result['status'] == 'stopped' and result['calls'] == 2
    assert store.report()['dispatch_blocked'] is True
    assert runner.capture('exam')['status'] == 'stopped'
    assert len(calls) == 2


def test_intent_without_response_never_resends_after_transport_interruption(tmp_path):
    import pytest

    def interrupted(payload, n):
        raise KeyboardInterrupt()

    runner, calls, store, _ = setup_runner(tmp_path, interrupted)
    with pytest.raises(KeyboardInterrupt):
        runner.capture('exam')
    assert store.report()['unknown_events'] == 1
    assert runner.capture('exam')['status'] == 'stopped'
    assert len(calls) == 1


def test_post_guard_revocation_refuses_before_transport_with_zero_charge_evidence(tmp_path):
    class Verifier:
        def __call__(self, *args):
            return True

        def verify_post(self, *args):
            return False

    runner, calls, store, _ = setup_runner(tmp_path)
    runner.verify_bound = Verifier()
    result = runner.capture('exam')
    assert result['status'] == 'stopped' and result['reason'] == 'enforcement_denied_before_post'
    assert result['known_api_usd'] == '0' and result['unknown_cost_count'] == 0
    assert not calls and store.report()['events'][0]['verified'] == 1


def test_same_model_version_in_two_roles_uses_each_roles_provider_and_effort(tmp_path):
    from cloudyfan_jev.scores.configuration import configuration_digest
    runner, calls, store, table = setup_runner(tmp_path)
    low, mid = table['configurations'][1:3]
    mid['entry']['profile']['model_version'] = low['entry']['profile']['model_version']
    mid['entry']['profile']['accepted_models'] = low['entry']['profile']['accepted_models']
    mid['entry']['profile']['provider'] = 'baseten/fp4'
    mid['entry']['profile']['provider_name'] = 'BaseTen'
    mid['configuration_sha256'] = configuration_digest(mid['entry'])
    changed = QualificationRunner(runner.client, store, table, model_bounds=runner.bounds,
        verify_bound=lambda *args: True, synthetic=True)
    result = changed.capture('exam', max_new_requests=4)
    assert result['status'] == 'paused' and len(calls) == 4
    assert calls[1]['model'] == calls[2]['model']
    assert calls[1]['provider'] != calls[2]['provider']
    assert calls[1]['reasoning'] != calls[2]['reasoning']


def test_completed_capture_accepts_later_billing_revision_without_resending_answers(tmp_path):
    from types import SimpleNamespace
    runner, calls, store, table = setup_runner(tmp_path)
    runner = QualificationRunner(runner.client, store, table, model_bounds=runner.bounds,
        verify_bound=lambda *args: True, synthetic=True,
        environment_factory=lambda case: SimpleNamespace(validate=lambda answer: {'passed': True, 'checks': []}),
        environment_version='capture-boundary-only-v1')
    result = runner.capture('exam')
    assert result['status'] == 'capture_complete' and len(calls) == 204
    original_answer = runner.records('exam', 'low')[0]['attempts'][0]
    model = original_answer['actual_model']

    def receipt(request):
        assert request.method == 'GET'
        return httpx.Response(200, json={'data': {'id': request.url.params['id'],
            'total_cost': '0.004', 'model': model, 'provider_name': 'OpenAI'}})

    with httpx.Client(transport=httpx.MockTransport(receipt)) as http:
        client = Client(http, 'fixture-' + 'credential', runner.client.root)
        client.reconcile(original_answer['request_id'])
    resumed = runner.capture('exam', max_new_requests=0)
    assert resumed['status'] == 'capture_complete'
    assert resumed['known_api_usd'] == '0.207'
    assert runner.records('exam', 'low')[0]['attempts'][0]['cost_usd'] == '0.004'
    assert len(calls) == 204

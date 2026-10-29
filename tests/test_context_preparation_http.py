"""#50：參考區塊經真實主服務 HTTP、Core／BudgetedCore／TaskService 準備並派送；上游僅 MockTransport。"""
from dataclasses import replace
import json

import pytest

from cloudyfan_monitor.demo import CATEGORY
from test_cache_observations_http import _state
from test_local_http_integration import loopback_network as loopback_network
from test_saved_paired_outcomes_http import paired_http as paired_http


POLICY = {'version': 'extractive-v1', 'max_input_bytes': 8192, 'max_blocks': 8, 'max_block_bytes': 2048,
          'max_prepared_bytes': 700, 'framing_reserve_bytes': 100, 'output_reserve_bytes': 100,
          'allow_omit': True, 'token_guarantee': 'not_required', 'max_prepare_ms': 1000,
          'qualification': {'content_preparation_version': 'extractive-v1', 'scope': 'synthetic-wiring-only',
                            'synthetic': True, 'evidence_sha256': 'b' * 64}}
CODE = 'def total(xs):\n    return sum(xs)  # 3.50 "引文"\n'
CONTEXT = {'format': 'reference-blocks-v1', 'compression': 'extractive-v1', 'blocks': [
    {'id': 'secret-name-required', 'text': '必要說明：答案必須是整數 2。', 'omittable': False, 'priority': 0},
    {'id': 'large-optional', 'text': 'L' * 900, 'omittable': True, 'priority': 1},
    {'id': 'code', 'text': CODE, 'omittable': True, 'priority': 2}]}


def _enable(tracer, policy=POLICY):
    service = tracer.bundle.app.gateway.service
    service.contracts['demo-open_source_first'] = replace(service.contracts['demo-open_source_first'],
                                                         context_policy=policy)
    operator = tracer.client()
    revision = operator.get('/monitor/snapshot').json()['control']['revision']
    assert operator.post('/monitor/mode', json={'revision': revision, 'mode': 'open_source_first'}).status_code == 200
    tracer.bundle.upstream.fail_tiers = set()


def _post(tracer, run, context=CONTEXT, principal='studio', prompt='Return only 1+1. PRIVATE_PAIRED_CANARY'):
    return tracer.client(principal).post('/v1/tasks', json={'run_id': run, 'category': CATEGORY, 'prompt': prompt,
                                                            **({'context': context} if context is not None else {})})


def _user(payload):
    return [m['content'] for m in payload['messages'] if m['role'] == 'user'][0]


def test_reference_blocks_are_prepared_frozen_sent_and_explained_without_original_text(paired_http):
    _enable(paired_http)
    response = _post(paired_http, 'ctx-http-1')
    assert response.status_code == 200 and response.json()['status'] == 'passed', response.text
    sent = _user(paired_http.bundle.upstream.calls[0])
    assert '必要說明：答案必須是整數 2。' in sent and CODE in sent and 'L' * 900 not in sent
    assert sent.index('必要說明') < sent.index('def total'), 'Original block order must be kept'
    before = _state(paired_http)
    detail = paired_http.client().get('/monitor/context-preparations/studio/ctx-http-1')
    assert _state(paired_http) == before, 'Observation called an upstream or changed original state'
    value = detail.json()
    assert value['status'] == 'prepared'
    prepared = value['preparation']
    assert prepared['sent_matches_prepared'] is True and prepared['local_only'] is True
    assert [(row['decision'], row['reason']) for row in prepared['blocks']] == [
        ('kept', 'required_by_request'), ('omitted', 'remaining_budget_insufficient'), ('kept', 'fits_in_priority_order')]
    assert prepared['prepared_bytes'] < prepared['original_bytes'] <= 8192
    assert prepared['qualification'] == {'content_preparation_version': 'extractive-v1',
                                         'scope': 'synthetic-wiring-only', 'synthetic': True}
    text = detail.text
    for private in ('必要說明', 'def total', 'secret-name-required', 'PRIVATE_PAIRED_CANARY', str(paired_http.bundle.root)):
        assert private not in text


def test_old_format_still_works_and_reports_no_preparation(paired_http):
    _enable(paired_http)
    assert _post(paired_http, 'old-format', context=None).json()['status'] == 'passed'
    assert _user(paired_http.bundle.upstream.calls[0]) == 'Return only 1+1. PRIVATE_PAIRED_CANARY'
    value = paired_http.client().get('/monitor/context-preparations/studio/old-format').json()
    assert value['status'] == 'not_requested' and value['preparation'] is None


def test_same_run_id_with_changed_blocks_conflicts_and_an_identical_resend_does_not_dispatch(paired_http):
    _enable(paired_http)
    assert _post(paired_http, 'ctx-conflict').status_code == 200
    calls = len(paired_http.bundle.upstream.calls)
    assert _post(paired_http, 'ctx-conflict').status_code == 200
    assert len(paired_http.bundle.upstream.calls) == calls
    changed = {**CONTEXT, 'blocks': [*CONTEXT['blocks'][:2], {**CONTEXT['blocks'][2], 'priority': 3}]}
    assert _post(paired_http, 'ctx-conflict', context=changed).status_code == 409
    assert _post(paired_http, 'ctx-conflict', context=None).status_code == 409
    assert len(paired_http.bundle.upstream.calls) == calls


@pytest.mark.parametrize('context,code', [
    ({**CONTEXT, 'blocks': [{'id': 'huge', 'text': 'R' * 1000, 'omittable': False, 'priority': 0}]},
     'required_context_over_limit'),
    ({**CONTEXT, 'blocks': [{**CONTEXT['blocks'][0], 'role': 'system'}]}, 'invalid_context_block'),
    ({**CONTEXT, 'compression': 'llm-summary-v1'}, 'context_preparation_version_not_registered'),
    ('not-a-dict', 'unsupported_context_fields'),
], ids=['required-over-limit', 'forged-role', 'unregistered-version', 'not-a-dict'])
def test_unsafe_or_unregistered_input_is_rejected_before_any_upstream_call(paired_http, context, code):
    _enable(paired_http)
    response = _post(paired_http, 'ctx-rejected', context=context)
    assert response.status_code >= 400 and response.json()['error']['code'] == code
    assert paired_http.bundle.upstream.calls == []


def test_contract_without_a_policy_rejects_blocks_and_customers_see_only_their_own_task(paired_http):
    _enable(paired_http)
    revision = paired_http.client().get('/monitor/snapshot').json()['control']['revision']
    paired_http.client().post('/monitor/mode', json={'revision': revision, 'mode': 'fixed_high'})
    rejected = _post(paired_http, 'ctx-no-policy')
    assert rejected.json()['error']['code'] == 'context_preparation_not_registered'
    _enable(paired_http)
    assert _post(paired_http, 'ctx-own').status_code == 200
    own = paired_http.client('studio').get('/v1/tasks/ctx-own/context-preparation')
    assert own.status_code == 200 and own.json()['status'] == 'prepared'
    assert paired_http.client('agent').get('/v1/monitor').status_code == 200
    assert paired_http.client('agent').get('/v1/tasks/ctx-own/context-preparation').status_code == 404
    assert paired_http.client('studio').get('/monitor/context-preparations/studio/ctx-own').status_code == 401
    paired_http.bundle.app.store.clock.now += 31 * 86400
    assert paired_http.client().get('/monitor/session').status_code == 200
    expired = paired_http.client().get('/monitor/context-preparations/studio/ctx-own').json()
    assert expired['status'] == 'raw_access_expired' and expired['preparation'] is None
    assert json.dumps(expired).count('必要說明') == 0

"""#53：以相同原始結構輸入與公開驗收，比較不同內容準備的品質與完整費用；真實主服務 HTTP，上游僅 MockTransport。"""
from dataclasses import replace
from decimal import Decimal
import json

from test_context_preparation_http import CONTEXT, POLICY, _post
from test_local_http_integration import loopback_network as loopback_network
from test_saved_paired_outcomes_http import paired_http as paired_http


FULL = {**POLICY, 'allow_omit': False, 'max_prepared_bytes': 4000}


def _setup(tracer):
    contracts = tracer.bundle.app.gateway.service.contracts
    contracts['demo-open_source_first'] = replace(contracts['demo-open_source_first'], context_policy=POLICY)
    contracts['demo-fixed_high'] = replace(contracts['demo-fixed_high'], context_policy=FULL)


def _run(tracer, client, run, mode, *, fail=(), context=CONTEXT):
    operator = tracer.client()
    revision = operator.get('/monitor/snapshot').json()['control']['revision']
    assert operator.post('/monitor/mode', json={'revision': revision, 'mode': mode}).status_code == 200
    tracer.bundle.upstream.fail_tiers = set(fail)
    response = _post(tracer, run, context=context, principal=client)
    assert response.status_code in (200, 409), response.text
    return {'client': client, 'run_id': run}


def _compare(tracer, tasks):
    response = tracer.client().post('/monitor/comparisons', json={
        'policy_version': 'original-policy-reproduction-v1', 'tasks': tasks, 'pairing': 'same-original-input-v1'})
    assert response.status_code == 200, response.text
    for private in ('必要說明', 'def total', 'L' * 50, 'PRIVATE_PAIRED_CANARY', 'secret-name-required'):
        assert private not in response.text
    return response.json()['pairing']


def test_same_original_input_pairs_across_preparations_and_marks_the_trimmed_member(paired_http):
    _setup(paired_http)
    tasks = [_run(paired_http, 'studio', 'cmp-trimmed', 'open_source_first'),
             _run(paired_http, 'agent', 'cmp-full', 'fixed_high')]
    pairing = _compare(paired_http, tasks)
    assert pairing['rule'] == 'same-original-input-v1' and pairing['counts']['paired_groups'] == 1
    group = pairing['groups'][0]
    assert group['status'] == 'equal_quality'
    members = {row['strategy']: row for row in group['members']}
    trimmed = members['open_source_first|extractive-v1|prepared']['preparation']
    full = members['fixed_high|extractive-v1|no_reduction']['preparation']
    assert trimmed['sent_input_differs'] is True and trimmed['sent_bytes'] < trimmed['original_bytes']
    assert full['sent_input_differs'] is False and full['sent_bytes'] == full['original_bytes']
    assert trimmed['synthetic_qualification'] is True
    difference = pairing['differences'][0]
    assert difference['equal_quality_groups'] == 1 and difference['difference_usd'] is not None
    costs = {row['strategy']: Decimal(row['complete_api_usd']) for row in group['members']}
    assert Decimal(difference['difference_usd']) == costs[difference['strategies'][1]] - costs[difference['strategies'][0]]
    assert not any(word in json.dumps(pairing) for word in ('win_rate', 'saving', 'equivalent_semantics'))


def test_differing_quality_reports_no_cost_difference(paired_http):
    _setup(paired_http)
    tasks = [_run(paired_http, 'studio', 'cmp-pass', 'open_source_first'),
             _run(paired_http, 'agent', 'cmp-fail', 'fixed_high', fail={'open_source', 'low', 'mid', 'high'})]
    pairing = _compare(paired_http, tasks)
    group = pairing['groups'][0]
    assert group['status'] == 'quality_differs' and pairing['differences'][0]['difference_usd'] is None
    assert pairing['counts']['failed_flows'] == 1


def test_different_reference_blocks_are_not_the_same_original_input(paired_http):
    _setup(paired_http)
    other = {**CONTEXT, 'blocks': [*CONTEXT['blocks'][:2], {**CONTEXT['blocks'][2], 'text': 'other code'}]}
    tasks = [_run(paired_http, 'studio', 'cmp-a', 'open_source_first'),
             _run(paired_http, 'agent', 'cmp-b', 'fixed_high', context=other)]
    pairing = _compare(paired_http, tasks)
    assert pairing['counts']['paired_groups'] == 0 and pairing['counts']['unpaired_groups'] == 2

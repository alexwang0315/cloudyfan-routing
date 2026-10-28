"""Original QCS saved decisions through the main operator HTTP interface."""
import json

import pytest

from saved_qcs_fixture import saved_qcs_http as saved_qcs_http
from test_local_http_integration import loopback_network as loopback_network


def _completed(tracer):
    run = 'saved-qcs-original'
    response = tracer.client('studio').post('/v1/tasks', json={
        'run_id': run, 'category': tracer.host.category, 'prompt': '1+1 PRIVATE_SAVED_QCS_CANARY'})
    assert response.status_code == 200 and response.json()['status'] == 'passed'
    snapshot = tracer.client().get('/monitor/snapshot')
    assert snapshot.status_code == 200
    history = snapshot.json()['tasks'][0]['operator_observation']['task_decision']
    assert history['binding']['routing_version'] == 'hierarchical-qcs-v1'
    assert tracer.host.calls
    return {'policy_version': 'original-policy-reproduction-v1',
            'tasks': [{'client': 'studio', 'run_id': run}]}


def _assert_read_only(tracer, before, after, response):
    assert before == after
    assert 'PRIVATE_SAVED_QCS_CANARY' not in response.text
    assert str(tracer.host.root) not in response.text
    assert tracer.host.operator_token not in response.text
    assert not any(credential.token in response.text for credential in tracer.host.credentials.values())


@pytest.mark.parametrize('saved_qcs_http', [False, True], indirect=True, ids=['direct', 'classify'])
def test_saved_original_qcs_reproduces_economic_choice_and_saved_classifier_rules(saved_qcs_http):
    tracer = saved_qcs_http
    body = _completed(tracer)
    before = tracer.snapshot('before-original-qcs-comparison')
    response = tracer.client().post('/monitor/comparisons', json=body)
    assert response.status_code == 200
    item = response.json()['items'][0]
    assert item['original']['routing_version'] == 'hierarchical-qcs-v1'
    assert item['reproduction']['status'] == 'reproduced', 'Original data-backed QCS family is not reproduced'
    decisions = item['reproduction']['decisions']
    assert [(row['stage'], row['selected_tier']) for row in decisions] == [
        ('initial', 'open_source'), ('low_mid', 'low' if len(tracer.host.calls) == 3 else 'mid'),
        ('after_validation', None)]
    assert decisions[1]['action'] == ('classify' if len(tracer.host.calls) == 3 else 'direct')
    assert decisions[1]['reason'] == ('reported_choice_applied' if len(tracer.host.calls) == 3
                                       else 'classified_flow_estimate_unknown')
    assert all(row['match'] is True for row in decisions)
    assert item['original']['costs']['complete_api_usd'] == ('0.021' if len(tracer.host.calls) == 3 else '0.02')
    for _ in range(2):
        assert tracer.client().get('/monitor/comparisons/' + response.json()['comparison_id']).json() == response.json()
    after = tracer.snapshot('after-original-qcs-comparison')
    _assert_read_only(tracer, before, after, response)
    assert tracer.client('studio').get('/monitor/comparisons/' + response.json()['comparison_id']).status_code == 401


@pytest.mark.parametrize('saved_qcs_http', [True], indirect=True)
@pytest.mark.parametrize('fault', ['missing_intent', 'wrong_request_sha'])
def test_missing_or_misbound_saved_classifier_stops_before_downstream_reproduction(saved_qcs_http, fault):
    tracer = saved_qcs_http
    body = _completed(tracer)
    run = tracer.original_run()
    call = tracer.host.root / 'receipts/calls' / ('run-' + run.name + '-decision-low_mid')
    path = call / ('intent.json' if fault == 'missing_intent' else 'response.json')
    preserved = path.with_name(path.stem + '-before-' + fault + '.json')
    path.rename(preserved)
    if fault == 'wrong_request_sha':
        changed = json.loads(preserved.read_bytes())
        changed['request_sha256'] = '0' * 64
        path.write_text(json.dumps(changed))
        path.chmod(0o600)
    before = tracer.snapshot('before-classifier-' + fault)
    response = tracer.client().post('/monitor/comparisons', json=body)
    assert response.status_code == 200
    item = response.json()['items'][0]
    assert item['reproduction']['status'] == 'partial'
    decisions = item['reproduction']['decisions']
    assert len(decisions) == 2 and decisions[0]['match'] is True
    assert decisions[1]['stage'] == 'low_mid' and decisions[1]['match'] is None
    assert decisions[1]['selected_tier'] is None
    assert decisions[1]['reason'] in ('classifier_evidence_unavailable', 'classifier_binding_mismatch')
    assert not any(row['stage'] == 'after_validation' for row in decisions)
    assert item['original']['path'][-1]['public_status'] == 'passed'
    assert item['original']['costs']['complete_api_usd'] == '0.021'
    after = tracer.snapshot('after-classifier-' + fault)
    _assert_read_only(tracer, before, after, response)


@pytest.mark.parametrize('saved_qcs_http', [True], indirect=True)
def test_reproduced_choice_cannot_borrow_another_actual_branch_answer(saved_qcs_http):
    tracer = saved_qcs_http
    body = _completed(tracer)
    run = tracer.original_run()
    path = tracer.host.root / 'receipts/calls' / ('run-' + run.name + '-decision-low_mid') / 'response.json'
    preserved = path.with_name('response-before-divergent-choice.json')
    path.rename(preserved)
    response = json.loads(preserved.read_bytes())
    raw = json.loads(response['raw'])
    raw['answers']['low_mid'].update(choice='mid', probabilities={'low': .1, 'mid': .9})
    response['raw'] = json.dumps(raw)
    path.write_text(json.dumps(response))
    path.chmod(0o600)
    original = run / 'result.json'
    original.rename(run / 'result-before-divergent-choice.json')
    result = json.loads((run / 'result-before-divergent-choice.json').read_bytes())
    result['selection']['decisions'][0].update(selected_tier='mid', reported_choice='mid',
        probabilities={'low': '0.1', 'mid': '0.9'})
    result['selection']['selected_tier'] = 'mid'
    original.write_text(json.dumps(result))
    original.chmod(0o600)
    before = tracer.snapshot('before-divergent-choice')
    compared = tracer.client().post('/monitor/comparisons', json=body)
    assert compared.status_code == 200
    item = compared.json()['items'][0]
    assert item['reproduction']['status'] == 'partial'
    decisions = item['reproduction']['decisions']
    assert len(decisions) == 2 and decisions[0]['match'] is True
    assert decisions[1]['stage'] == 'low_mid' and decisions[1]['selected_tier'] == 'mid'
    assert decisions[1]['match'] is False
    assert not any(row['stage'] == 'after_validation' for row in decisions)
    assert item['original']['path'][-1]['tier'] == 'low'
    assert item['original']['path'][-1]['public_status'] == 'passed'
    assert item['original']['costs']['complete_api_usd'] == '0.021'
    after = tracer.snapshot('after-divergent-choice')
    _assert_read_only(tracer, before, after, compared)

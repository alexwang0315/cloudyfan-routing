"""Registered candidate strategies over saved QCS decisions through the operator HTTP seam."""
import pytest

from saved_backup_fixture import saved_backup_http as saved_backup_http
from saved_qcs_fixture import saved_qcs_http as saved_qcs_http
from test_local_http_integration import loopback_network as loopback_network
from test_saved_qcs_comparisons_http import _assert_read_only, _completed


def _compare(tracer, strategy, name):
    body = {**_completed(tracer), 'candidate_strategy': strategy}
    before = tracer.snapshot('before-candidate-' + name)
    response = tracer.client().post('/monitor/comparisons', json=body)
    assert response.status_code == 200, response.text
    report = response.json()
    for _ in range(2):
        assert tracer.client().get('/monitor/comparisons/' + report['comparison_id']).json() == report
    _assert_read_only(tracer, before, tracer.snapshot('after-candidate-' + name), response)
    assert tracer.client('studio').get('/monitor/comparisons/' + report['comparison_id']).status_code == 401
    assert report['scope']['candidate_strategy'] == strategy
    return report, report['items'][0]


@pytest.mark.parametrize('saved_qcs_http', [True], indirect=True, ids=['classify'])
def test_direct_candidate_stops_at_the_first_divergence_from_a_saved_classification(saved_qcs_http):
    report, item = _compare(saved_qcs_http, 'qcs-direct-v1', 'direct')
    candidate = item['candidate']
    assert candidate['strategy'] == 'qcs-direct-v1' and candidate['adopted'] is False
    assert candidate['status'] == 'diverged'
    rows = candidate['decisions']
    assert [(row['stage'], row['status']) for row in rows] == [
        ('initial', 'evaluated'), ('low_mid', 'evaluated'), ('after_validation', 'not_observed')]
    assert rows[0]['diverged'] is False and rows[0]['reason'] == 'same_as_original_outside_decision_point'
    assert (rows[1]['action'], rows[1]['original']['action'], rows[1]['diverged'], rows[1]['stop']) == (
        'direct', 'classify', True, 'requires_unsaved_alternative_answer')
    assert rows[1]['forecast']['calibration'] == 'present'
    assert rows[1]['forecast']['complete_flow_usd'] is not None
    assert rows[2]['reason'] == 'after_first_divergence'
    # The original report section and fees are unchanged by adding a candidate.
    assert item['reproduction']['status'] == 'reproduced'
    assert item['original']['costs']['complete_api_usd'] == '0.021'
    assert report['counts']['candidate_diverged'] == 1


@pytest.mark.parametrize('saved_qcs_http', [True], indirect=True, ids=['classify'])
def test_classify_candidate_reuses_the_same_saved_classification_without_divergence(saved_qcs_http):
    _, item = _compare(saved_qcs_http, 'qcs-classify-v1', 'classify')
    candidate = item['candidate']
    assert candidate['status'] == 'same_as_original'
    assert [row['diverged'] for row in candidate['decisions']] == [False, False, False]
    assert candidate['decisions'][1]['selected_tier'] == item['reproduction']['decisions'][1]['selected_tier']


@pytest.mark.parametrize('saved_qcs_http', [False], indirect=True, ids=['direct'])
def test_unknown_classification_estimate_is_not_evaluable_rather_than_uneconomic(saved_qcs_http):
    _, item = _compare(saved_qcs_http, 'qcs-classify-v1', 'unknown-estimate')
    row = item['candidate']['decisions'][1]
    assert (row['status'], row['reason'], row['action']) == (
        'not_evaluable', 'classification_estimate_unknown', None)
    assert item['candidate']['status'] == 'not_evaluable'


@pytest.mark.parametrize('saved_qcs_http', [True], indirect=True, ids=['classify'])
@pytest.mark.parametrize('strategy', ['original-policy-reproduction-v1', 'lambda: 1', '', 3, None,
                                      {'code': 'select high'}])
def test_unregistered_or_browser_built_strategy_is_rejected_without_a_report(saved_qcs_http, strategy):
    tracer = saved_qcs_http
    body = {**_completed(tracer), 'candidate_strategy': strategy}
    before = tracer.snapshot('before-rejected-candidate')
    response = tracer.client().post('/monitor/comparisons', json=body)
    assert response.status_code in (409, 422)
    assert tracer.client().get('/monitor/comparisons').json()['reports'] == []
    assert before == tracer.snapshot('after-rejected-candidate')


@pytest.mark.parametrize('saved_qcs_http', [True], indirect=True, ids=['classify'])
def test_catalogue_lists_only_registered_strategies_and_plain_reports_have_no_candidate(saved_qcs_http):
    tracer = saved_qcs_http
    body = _completed(tracer)
    catalog = tracer.client().get('/monitor/comparisons').json()
    assert [row['version'] for row in catalog['candidate_strategies']] == ['qcs-classify-v1', 'qcs-direct-v1']
    response = tracer.client().post('/monitor/comparisons', json=body)
    assert response.status_code == 200
    assert 'candidate' not in response.json()['items'][0]
    assert 'candidate_strategy' not in response.json()['scope']


@pytest.mark.parametrize('saved_qcs_http', [{'family': 'qcs_controlled', 'variant': 'mid_high'}], indirect=True,
                         ids=['quality-rescue'])
def test_quality_rescue_divergence_leaves_the_second_stage_unobserved(saved_qcs_http):
    _, item = _compare(saved_qcs_http, 'qcs-direct-v1', 'quality-rescue')
    rows = item['candidate']['decisions']
    assert [(row['stage'], row['status']) for row in rows] == [
        ('initial', 'evaluated'), ('low_mid', 'evaluated'), ('mid_high', 'not_observed'),
        ('after_validation', 'not_observed')]
    assert rows[1]['diverged'] is True and rows[1]['stop'] == 'requires_unsaved_alternative_answer'
    assert item['reproduction']['status'] == 'reproduced'


def _compare_backup(tracer, strategy):
    tracer.create_original()
    before = tracer.snapshot('before-backup-candidate')
    response = tracer.client().post('/monitor/comparisons', json={
        'policy_version': 'original-policy-reproduction-v1', 'candidate_strategy': strategy,
        'tasks': [{'client': 'studio', 'run_id': 'saved-backup-original'}]})
    assert response.status_code == 200
    assert 'PRIVATE_SAVED_BACKUP_TASK_CANARY' not in response.text and str(tracer.host.root) not in response.text
    read = tracer.client().get('/monitor/comparisons/' + response.json()['comparison_id'])
    assert read.status_code == 200 and read.content == response.content
    assert tracer.snapshot('after-backup-candidate') == before
    return response.json()['items'][0]


@pytest.mark.parametrize('strategy,status,reason', [
    ('qcs-classify-v1', 'not_evaluable', 'exact_classifier_calibration_missing'),
    ('qcs-direct-v1', 'evaluated', 'registered_direct_choice'),
])
def test_backup_missing_calibration_is_distinct_from_an_uneconomic_classifier(saved_backup_http, strategy, status, reason):
    item = _compare_backup(saved_backup_http, strategy)
    row = item['candidate']['decisions'][1]
    assert (row['stage'], row['status'], row['reason']) == ('low_mid', status, reason)
    assert row['forecast']['calibration'] == 'missing'
    assert item['candidate']['status'] == ('not_evaluable' if status == 'not_evaluable' else 'same_as_original')


@pytest.mark.parametrize('saved_backup_http', [{'classifiers': ('low_mid',), 'scenario': 'availability',
    'availability_exhaustion': True}], indirect=True)
def test_availability_fault_transition_matches_original_and_classification_divergence_stops(saved_backup_http):
    item = _compare_backup(saved_backup_http, 'qcs-direct-v1')
    rows = item['candidate']['decisions']
    assert [(row['stage'], row['status']) for row in rows] == [
        ('initial', 'evaluated'), ('same_pool', 'evaluated'), ('low_mid', 'evaluated'),
        ('after_validation', 'not_observed')]
    assert rows[1]['reason'] == 'same_as_original_outside_decision_point' and rows[1]['diverged'] is False
    assert (rows[2]['original']['action'], rows[2]['action'], rows[2]['diverged']) == ('classify', 'direct', True)

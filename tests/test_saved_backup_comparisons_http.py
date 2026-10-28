"""Saved Backup decisions through real main-service HTTP, never a report stub."""
import pytest

from saved_backup_fixture import saved_backup_http as saved_backup_http
from test_local_http_integration import loopback_network as loopback_network


def test_direct_backup_reproduces_saved_recovery_and_final_public_pass(saved_backup_http):
    tracer = saved_backup_http
    tracer.create_original()
    assert len(tracer.host.calls) == 2
    before = tracer.snapshot('before-comparison')
    response = tracer.compare()
    report = response.json()
    item = report['items'][0]
    assert item['reproduction']['status'] == 'reproduced', 'Complete saved Backup recovery was not reproduced'
    decisions = item['reproduction']['decisions']
    assert [(row['stage'], row['action'], row['selected_tier']) for row in decisions] == [
        ('initial', 'direct', 'open_source'), ('low_mid', 'direct', 'mid'), ('after_validation', 'stop', None)]
    assert decisions[1]['reason'] == 'exact_classifier_calibration_missing'
    assert decisions[-1]['reason'] == 'public_validation_passed'
    assert all(row['match'] is True for row in decisions)
    for _ in range(2):
        read = tracer.client().get('/monitor/comparisons/' + report['comparison_id'])
        assert read.status_code == 200 and read.content == response.content
    assert tracer.snapshot('after-comparison-and-gets') == before


@pytest.mark.parametrize('saved_backup_http', [{'classifiers': ('low_mid',)}], indirect=True)
def test_classified_backup_reapplies_original_reply_without_calling_classifier(saved_backup_http):
    tracer = saved_backup_http
    tracer.create_original()
    assert len(tracer.host.calls) == 3
    before = tracer.snapshot('before-comparison')
    response = tracer.compare()
    item = response.json()['items'][0]
    assert item['reproduction']['status'] == 'reproduced', 'The saved classifier evidence was not reapplied'
    decisions = item['reproduction']['decisions']
    assert [(row['stage'], row['action'], row['selected_tier']) for row in decisions] == [
        ('initial', 'direct', 'open_source'), ('low_mid', 'classify', 'low'), ('after_validation', 'stop', None)]
    assert decisions[1]['reason'] == 'reported_choice_applied'
    assert 'conservative_net_saving_positive' in item['reproduction']['reasons']
    assert 'net_saving_lower_usd' not in response.text
    assert all(row['match'] is True for row in decisions)
    assert tracer.snapshot('after-comparison') == before


@pytest.mark.parametrize('saved_backup_http', [{'classifiers': ('low_mid',),
    'classifier_reply': 'probability_strings'}], indirect=True)
def test_saved_backup_probability_strings_reproduce_the_original_parse_failure(saved_backup_http):
    tracer = saved_backup_http
    tracer.create_original()
    original = tracer.client().get('/monitor/snapshot')
    assert original.status_code == 200
    task = original.json()['tasks'][0]
    decision = [row for row in task['trace']['decisions'] if row.get('model')][0]
    assert decision['fallback'] is True and decision['fallback_reason'] == 'parse_failure'
    assert decision['reported_choice'] is None and decision['probabilities'] is None
    assert decision['selected_tier'] == 'mid'
    assert task['operator_observation']['task_decision']['costs']['complete_api_usd'] == '0.021'
    assert len(tracer.host.calls) == 3 and len(tracer.validations) == 2
    before = tracer.snapshot('before-comparison')
    response = tracer.compare()
    report = response.json()
    item = report['items'][0]
    assert item['original']['task_status'] == 'passed'
    assert item['reproduction']['status'] == 'reproduced', 'Saved numeric strings changed the original parse-failure fallback'
    decisions = item['reproduction']['decisions']
    assert [(row['stage'], row['action'], row['selected_tier']) for row in decisions] == [
        ('initial', 'direct', 'open_source'), ('low_mid', 'classify', 'mid'), ('after_validation', 'stop', None)]
    assert decisions[1]['reason'] == 'parse_failure' and all(row['match'] is True for row in decisions)
    for _ in range(2):
        read = tracer.client().get('/monitor/comparisons/' + report['comparison_id'])
        assert read.status_code == 200 and read.content == response.content
    assert tracer.snapshot('after-comparison-and-gets') == before


@pytest.mark.parametrize('saved_backup_http', [{'classifiers': ('low_mid',),
    'classifier_reply': 'confidence_string'}], indirect=True)
def test_saved_backup_confidence_string_reproduces_the_original_parse_failure(saved_backup_http):
    tracer = saved_backup_http
    tracer.create_original()
    original = tracer.client().get('/monitor/snapshot')
    assert original.status_code == 200
    task = original.json()['tasks'][0]
    decision = [row for row in task['trace']['decisions'] if row.get('model')][0]
    assert decision['fallback_reason'] == 'parse_failure' and decision['fallback'] is True
    assert decision['reported_choice'] is None and decision['probabilities'] is None
    assert decision['selected_tier'] == 'mid'
    assert task['operator_observation']['task_decision']['costs']['complete_api_usd'] == '0.021'
    before = tracer.snapshot('before-comparison')
    response = tracer.compare()
    report = response.json()
    decisions = report['items'][0]['reproduction']['decisions']
    assert report['items'][0]['reproduction']['status'] == 'reproduced'
    assert decisions[1]['reason'] == 'parse_failure' and decisions[1]['selected_tier'] == 'mid'
    assert all(row['match'] is True for row in decisions)
    for _ in range(2):
        read = tracer.client().get('/monitor/comparisons/' + report['comparison_id'])
        assert read.status_code == 200 and read.content == response.content
    assert tracer.snapshot('after-comparison-and-gets') == before


@pytest.mark.parametrize('saved_backup_http', [{'classifiers': ('low_mid',),
    'classifier_reply': 'numeric_confidence'}], indirect=True)
def test_saved_backup_numeric_confidence_and_probabilities_preserve_a_legal_choice(saved_backup_http):
    tracer = saved_backup_http
    tracer.create_original()
    original = tracer.client().get('/monitor/snapshot')
    assert original.status_code == 200
    task = original.json()['tasks'][0]
    decision = [row for row in task['trace']['decisions'] if row.get('model')][0]
    assert decision['fallback'] is False and decision['reported_choice'] == 'low'
    assert decision['probabilities'] == {'low': '0.9', 'mid': '0.1'}
    assert task['operator_observation']['task_decision']['costs']['complete_api_usd'] == '0.021'
    before = tracer.snapshot('before-comparison')
    response = tracer.compare()
    report = response.json()
    decisions = report['items'][0]['reproduction']['decisions']
    assert report['items'][0]['reproduction']['status'] == 'reproduced'
    assert decisions[1]['reason'] == 'reported_choice_applied' and decisions[1]['selected_tier'] == 'low'
    assert all(row['match'] is True for row in decisions)
    for _ in range(2):
        read = tracer.client().get('/monitor/comparisons/' + report['comparison_id'])
        assert read.status_code == 200 and read.content == response.content
    assert tracer.snapshot('after-comparison-and-gets') == before


@pytest.mark.parametrize('saved_backup_http', [{'classifiers': ('low_mid',),
    'classifier_reply': 'minimum_exponent_probability'}], indirect=True)
def test_saved_backup_minimum_exponent_probability_preserves_the_original_valid_choice(saved_backup_http):
    tracer = saved_backup_http
    tracer.create_original()
    original = tracer.client().get('/monitor/snapshot')
    assert original.status_code == 200
    task = original.json()['tasks'][0]
    decision = [row for row in task['trace']['decisions'] if row.get('model')][0]
    assert decision['fallback'] is False and decision['fallback_reason'] is None
    assert decision['reported_choice'] == decision['selected_tier'] == 'mid'
    assert decision['probabilities'] == {'low': '1E-64', 'mid': '1'}
    assert task['operator_observation']['task_decision']['costs']['complete_api_usd'] == '0.021'
    assert len(tracer.host.calls) == 3 and len(tracer.validations) == 2
    before = tracer.snapshot('before-comparison')
    response = tracer.compare()
    report = response.json()
    item = report['items'][0]
    assert item['original']['task_status'] == 'passed'
    assert item['reproduction']['status'] == 'reproduced', 'Equivalent scientific and fixed probabilities changed the original choice'
    decisions = item['reproduction']['decisions']
    assert decisions[1]['reason'] == 'reported_choice_applied' and decisions[1]['selected_tier'] == 'mid'
    assert all(row['match'] is True for row in decisions)
    for _ in range(2):
        read = tracer.client().get('/monitor/comparisons/' + report['comparison_id'])
        assert read.status_code == 200 and read.content == response.content
    assert tracer.snapshot('after-comparison-and-gets') == before


@pytest.mark.parametrize('saved_backup_http', [{'classifiers': ('low_mid',),
    'classifier_reply': 'minimum_exponent_probability'}], indirect=True)
def test_saved_backup_changed_numeric_probability_cannot_match_the_same_selected_tier(saved_backup_http):
    tracer = saved_backup_http
    tracer.create_original()
    original = tracer.client().get('/monitor/snapshot')
    assert original.status_code == 200
    task = original.json()['tasks'][0]
    decision = [row for row in task['trace']['decisions'] if row.get('model')][0]
    assert decision['fallback'] is False and decision['reported_choice'] == 'mid'
    assert decision['probabilities'] == {'low': '1E-64', 'mid': '1'}
    assert task['operator_observation']['task_decision']['costs']['complete_api_usd'] == '0.021'
    tracer.remove_or_corrupt_original_evidence('classifier_probability_mismatch')
    before = tracer.snapshot('before-comparison')
    response = tracer.compare()
    report = response.json()
    item = report['items'][0]
    assert item['original']['task_status'] == 'passed' and item['reproduction']['status'] == 'partial'
    decisions = item['reproduction']['decisions']
    assert decisions[0]['match'] is True and decisions[1]['match'] is None
    assert decisions[1]['reason'] == 'original_saved_decision_mismatch'
    assert all(row['match'] is not True for row in decisions[1:])
    for _ in range(2):
        read = tracer.client().get('/monitor/comparisons/' + report['comparison_id'])
        assert read.status_code == 200 and read.content == response.content
    assert tracer.snapshot('after-comparison-and-gets') == before


@pytest.mark.parametrize('saved_backup_http', [{'classifiers': ('low_mid',),
    'classifier_reply': 'bounded_tiny_probability'}], indirect=True)
def test_saved_backup_unrepresentable_numeric_probability_stops_at_the_affected_point(saved_backup_http):
    tracer = saved_backup_http
    tracer.create_original()
    original = tracer.client().get('/monitor/snapshot')
    assert original.status_code == 200
    task = original.json()['tasks'][0]
    decision = [row for row in task['trace']['decisions'] if row.get('model')][0]
    assert decision['reported_choice'] == 'low' and decision['probabilities'] == {'low': '1E-65', 'mid': '1'}
    assert decision['fallback_reason'] == 'tied_or_inconsistent_distribution'
    assert decision['selected_tier'] == 'mid'
    assert task['operator_observation']['task_decision']['costs']['complete_api_usd'] == '0.021'
    before = tracer.snapshot('before-comparison')
    response = tracer.compare()
    report = response.json()
    assert report['status'] == 'completed' and report['items'][0]['original']['task_status'] == 'passed'
    item = report['items'][0]
    assert item['reproduction']['status'] == 'partial'
    decisions = item['reproduction']['decisions']
    assert decisions[0]['match'] is True and decisions[1]['match'] is None
    assert decisions[1]['reason'] != 'parse_failure', 'A valid original number became an invalid-reply fallback'
    assert all(row['match'] is not True for row in decisions[1:])
    for _ in range(2):
        read = tracer.client().get('/monitor/comparisons/' + report['comparison_id'])
        assert read.status_code == 200 and read.content == response.content
    assert tracer.snapshot('after-comparison-and-gets') == before


@pytest.mark.parametrize(('saved_backup_http', 'reason', 'reported_choice'), [
    ({'classifiers': ('low_mid',), 'classifier_reply': 'confidence_bool'}, 'parse_failure', None),
    ({'classifiers': ('low_mid',), 'classifier_reply': 'probability_bools'}, 'parse_failure', None),
    ({'classifiers': ('low_mid',), 'classifier_reply': 'usage_cost_string'}, 'parse_failure', None),
    ({'classifiers': ('low_mid',), 'classifier_reply': 'distribution_missing'}, 'distribution_missing', 'low'),
    ({'classifiers': ('low_mid',), 'classifier_reply': 'tied_distribution'}, 'tied_or_inconsistent_distribution', 'low'),
], indirect=['saved_backup_http'])
def test_saved_backup_original_numeric_and_abstention_guards_remain_distinct(saved_backup_http, reason, reported_choice):
    tracer = saved_backup_http
    tracer.create_original()
    original = tracer.client().get('/monitor/snapshot')
    assert original.status_code == 200
    task = original.json()['tasks'][0]
    decision = [row for row in task['trace']['decisions'] if row.get('model')][0]
    assert decision['fallback'] is True and decision['fallback_reason'] == reason
    assert decision['selected_tier'] == 'mid' and decision['reported_choice'] == reported_choice
    assert task['operator_observation']['task_decision']['costs']['complete_api_usd'] == '0.021'
    before = tracer.snapshot('before-comparison')
    response = tracer.compare()
    report = response.json()
    item = report['items'][0]
    decisions = item['reproduction']['decisions']
    assert item['reproduction']['status'] == 'reproduced'
    assert decisions[1]['reason'] == reason and decisions[1]['selected_tier'] == 'mid'
    assert all(row['match'] is True for row in decisions)
    for _ in range(2):
        read = tracer.client().get('/monitor/comparisons/' + report['comparison_id'])
        assert read.status_code == 200 and read.content == response.content
    assert tracer.snapshot('after-comparison-and-gets') == before


@pytest.mark.parametrize('saved_backup_http', [{'classifiers': ('low_mid',)}], indirect=True)
@pytest.mark.parametrize(('fault', 'reason'), [
    ('classifier_duplicate_key', 'original_saved_decision_mismatch'),
    ('classifier_model', 'classifier_binding_mismatch'),
    ('classifier_usage', 'classifier_binding_mismatch'),
])
def test_saved_backup_numeric_parser_does_not_loosen_original_raw_and_fee_bindings(saved_backup_http, fault, reason):
    tracer = saved_backup_http
    tracer.create_original()
    tracer.remove_or_corrupt_original_evidence(fault)
    before = tracer.snapshot('before-comparison')
    response = tracer.compare()
    report = response.json()
    item = report['items'][0]
    assert item['original']['task_status'] == 'passed' and item['reproduction']['status'] == 'partial'
    decisions = item['reproduction']['decisions']
    assert decisions[0]['match'] is True and decisions[1]['match'] is None
    assert decisions[1]['reason'] == reason
    assert all(row['match'] is not True for row in decisions[1:])
    for _ in range(2):
        read = tracer.client().get('/monitor/comparisons/' + report['comparison_id'])
        assert read.status_code == 200 and read.content == response.content
    assert tracer.snapshot('after-comparison-and-gets') == before


@pytest.mark.parametrize('saved_backup_http', [{'classifiers': ('low_mid', 'mid_high'),
    'scenario': 'quality_chain'}], indirect=True)
def test_both_backup_quality_stages_and_terminal_stop_reproduce(saved_backup_http):
    tracer = saved_backup_http
    tracer.create_original()
    assert len(tracer.host.calls) == 5 and len(tracer.validations) == 3
    before = tracer.snapshot('before-comparison')
    item = tracer.compare().json()['items'][0]
    assert item['reproduction']['status'] == 'reproduced'
    decisions = item['reproduction']['decisions']
    assert [(row['stage'], row['action'], row['selected_tier']) for row in decisions] == [
        ('initial', 'direct', 'open_source'), ('low_mid', 'classify', 'low'),
        ('mid_high', 'classify', 'high'), ('after_validation', 'stop', None)]
    assert all(row['match'] is True for row in decisions)
    assert tracer.snapshot('after-comparison') == before


@pytest.mark.parametrize('saved_backup_http', [{'scenario': 'availability'}], indirect=True)
def test_same_pool_backup_uses_the_saved_exclusions_and_original_rank(saved_backup_http):
    tracer = saved_backup_http
    tracer.create_original()
    assert len(tracer.host.calls) == 2 and len(tracer.validations) == 1
    before = tracer.snapshot('before-comparison')
    item = tracer.compare().json()['items'][0]
    assert item['reproduction']['status'] == 'reproduced'
    decisions = item['reproduction']['decisions']
    assert [(row['stage'], row['action'], row['config_id']) for row in decisions] == [
        ('initial', 'direct', 'open_source-0'), ('same_pool', 'direct', 'open_source-1'),
        ('after_validation', 'stop', None)]
    assert all(row['match'] is True for row in decisions)
    assert tracer.snapshot('after-comparison') == before


@pytest.mark.parametrize('saved_backup_http', [{'classifiers': ('low_mid',), 'scenario': 'availability',
    'availability_exhaustion': True}], indirect=True)
def test_exhausted_backup_pool_reproduces_its_non_generating_quality_transition(saved_backup_http):
    tracer = saved_backup_http
    tracer.create_original()
    assert len(tracer.host.calls) == 3 and len(tracer.validations) == 1
    before = tracer.snapshot('before-comparison')
    response = tracer.compare()
    report = response.json()
    item = report['items'][0]
    assert item['original']['task_status'] == 'passed' and len(item['original']['path']) == 2
    assert item['reproduction']['status'] == 'reproduced', 'Complete original empty-pool advancement was not reproduced'
    decisions = item['reproduction']['decisions']
    assert [(row['stage'], row['action'], row['config_id']) for row in decisions] == [
        ('initial', 'direct', 'open_source-0'), ('same_pool', 'advance_quality_stage', None),
        ('low_mid', 'classify', 'low-1'), ('after_validation', 'stop', None)]
    assert decisions[1]['reason'] == 'same_pool_exhausted'
    assert all(row['match'] is True for row in decisions)
    tracer.verify_saved_availability_proof(report, expected_links=1)
    for _ in range(2):
        read = tracer.client().get('/monitor/comparisons/' + report['comparison_id'])
        assert read.status_code == 200 and read.content == response.content
    assert tracer.snapshot('after-comparison-and-gets') == before


@pytest.mark.parametrize('saved_backup_http', [{'classifiers': ('low_mid',), 'scenario': 'availability',
    'availability_exhaustion': True}], indirect=True)
@pytest.mark.parametrize(('fault', 'point', 'reason'), [
    ('missing_exhaustion_gate', 1, 'original_health_evidence_invalid'),
    ('missing_classifier', 2, 'classifier_evidence_unavailable'),
    ('classifier_choice_mismatch', 2, 'original_saved_decision_mismatch'),
])
def test_backup_availability_proof_never_bridges_missing_or_inconsistent_evidence(saved_backup_http, fault, point, reason):
    tracer = saved_backup_http
    tracer.create_original()
    tracer.remove_or_corrupt_original_evidence(fault)
    before = tracer.snapshot('before-comparison')
    report = tracer.compare().json()
    item = report['items'][0]
    assert item['original']['task_status'] == 'passed' and len(item['original']['path']) == 2
    assert item['reproduction']['status'] == 'partial'
    decisions = item['reproduction']['decisions']
    assert all(row['match'] is True for row in decisions[:point])
    assert decisions[point]['match'] is None and decisions[point]['reason'] == reason
    assert all(row['match'] is not True for row in decisions[point:])
    assert len(decisions) == 4
    tracer.verify_saved_availability_proof(report, expected_links=0 if fault == 'missing_exhaustion_gate' else 1)
    assert tracer.snapshot('after-comparison') == before


@pytest.mark.parametrize('saved_backup_http', [{'classifiers': ('low_mid', 'mid_high'),
    'scenario': 'quality_chain'}], indirect=True)
@pytest.mark.parametrize(('fault', 'point', 'reason'), [
    ('missing_classifier', 1, 'classifier_evidence_unavailable'),
    ('request_hash', 1, 'classifier_binding_mismatch'),
    ('missing_gate', 2, 'original_health_evidence_invalid'),
])
def test_missing_backup_point_stops_later_reproduction_without_losing_verified_prefix(
        saved_backup_http, fault, point, reason):
    tracer = saved_backup_http
    tracer.create_original()
    assert len(tracer.host.calls) == 5
    tracer.remove_or_corrupt_original_evidence(fault)
    before = tracer.snapshot('before-comparison')
    response = tracer.compare()
    report = response.json()
    item = report['items'][0]
    assert item['reproduction']['status'] == 'partial'
    assert len(item['original']['path']) == 3 and item['original']['task_status'] == 'passed'
    decisions = item['reproduction']['decisions']
    assert all(row['match'] is True for row in decisions[:point])
    assert decisions[point]['match'] is None and decisions[point]['reason'] == reason
    assert all(row['match'] is not True for row in decisions[point:]), 'Missing evidence unlocked a later decision'
    assert len(decisions) == 4
    for _ in range(2):
        read = tracer.client().get('/monitor/comparisons/' + report['comparison_id'])
        assert read.status_code == 200 and read.content == response.content
    assert tracer.snapshot('after-comparison-and-gets') == before


@pytest.mark.parametrize('saved_backup_http', [{'scenario': 'first_pass'}], indirect=True)
def test_initial_backup_public_pass_remains_reproduced(saved_backup_http):
    tracer = saved_backup_http
    tracer.create_original()
    assert len(tracer.host.calls) == 1 and len(tracer.validations) == 1
    before = tracer.snapshot('before-comparison')
    item = tracer.compare().json()['items'][0]
    assert item['reproduction']['status'] == 'reproduced'
    assert [(row['stage'], row['action'], row['match']) for row in item['reproduction']['decisions']] == [
        ('initial', 'direct', True), ('after_validation', 'stop', True)]
    assert tracer.snapshot('after-comparison') == before


@pytest.mark.parametrize('saved_backup_http', [{'scenario': 'first_pass'}], indirect=True)
def test_missing_initial_backup_gate_cannot_reproduce_the_later_public_pass(saved_backup_http):
    tracer = saved_backup_http
    tracer.create_original()
    tracer.remove_or_corrupt_original_evidence('missing_initial_gate')
    before = tracer.snapshot('before-comparison')
    item = tracer.compare().json()['items'][0]
    assert item['original']['task_status'] == 'passed'
    assert item['reproduction']['status'] == 'unknown'
    assert item['reproduction']['decisions'][0]['reason'] == 'original_health_evidence_invalid'
    assert all(row['match'] is None for row in item['reproduction']['decisions'])
    assert tracer.snapshot('after-comparison') == before

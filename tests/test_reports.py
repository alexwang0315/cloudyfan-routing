from copy import deepcopy

from cloudyfan_jev.experiments import build_plan
from cloudyfan_jev.scores.catalog import digest
from test_experiments import setup_plan, NOW


def plan_fixture():
    spec, pack, review = setup_plan()
    spec['arms'] = ['fixed_high', 'cloudyfan']
    second = deepcopy(spec['tasks'][0])
    second.update(task_id='holdout-2', task={'prompt': 'Return integer 8.'}, stratum='hard')
    second['input_sha256'] = digest(second['task'])
    spec['tasks'].append(second)
    review['tasks']['holdout-2'] = {**review['tasks']['holdout-1'], 'input_sha256': second['input_sha256']}
    review['input_sha256'] = digest({'spec': spec, 'qualification_pack_id': pack['pack_id']})
    return build_plan(spec, pack, review, now=NOW)


def evidence_fixture(plan):
    from cloudyfan_jev.reports import report_binding
    runs, events = [], []
    for arm, costs in [('fixed_high', ['2', '8']), ('cloudyfan', ['1', '6'])]:
        for i, cost in enumerate(costs, 1):
            run_id = f'{arm}-{i}'
            runs.append({'run_id': run_id, 'arm': arm, 'task_id': f'holdout-{i}', 'cohort': 'holdout',
                'binding_sha256': report_binding(plan, arm, f'holdout-{i}')['binding_sha256'],
                'first_pass': True, 'final_pass': True, 'status': 'passed', 'accounting_complete': True,
                'timing': {'end_to_end_ms': str(i * 100), 'validation_ms': '10', 'queue_ms': '0'}})
            events.append({'event_id': run_id + ':gen', 'run_id': run_id, 'kind': 'generation',
                'attempt': 0, 'retry': False, 'cost_usd': cost, 'receipt_id': 'receipt:' + run_id,
                'verified': True})
    return {'schema_version': 'cloudyfan-report-evidence-v1', 'plan_id': plan['plan_id'],
            'source': {'id': 'fixture-ledger', 'version': 'v1', 'kind': 'synthetic'},
            'runs': runs, 'events': events,
            'annotations': {'posthoc': False, 'caching': 'disabled', 'zero_model_fee_scenario': False}}


def test_report_distinguishes_aggregate_and_per_task_cdr_and_keeps_sample_denominators():
    from cloudyfan_jev.reports import build_report
    plan = plan_fixture()
    evidence = evidence_fixture(plan)
    original = deepcopy((plan, evidence))
    report = build_report(plan, evidence)
    comparison = report['comparisons']['cloudyfan']
    assert comparison['paired_tasks'] == ['holdout-1', 'holdout-2']
    assert comparison['aggregate_cdr_percent'] == '30.0'
    assert comparison['per_task_cdr']['mean_percent'] == '37.50'
    assert comparison['per_task_cdr']['median_percent'] == '37.50'
    assert comparison['per_task_cdr']['min_percent'] == '25.00'
    assert comparison['per_task_cdr']['max_percent'] == '50.0'
    assert comparison['per_task_cdr']['more_expensive_fraction'] == '0'
    assert report['groups']['cloudyfan']['final_pass_rate'] == '1'
    assert report['groups']['cloudyfan']['cost_per_success_usd'] == '3.5'
    assert report['groups']['cloudyfan']['latency']['end_to_end_ms']['mean'] == '150'
    assert report['groups']['cloudyfan']['latency']['end_to_end_ms']['samples'] == 2
    assert report['evidence_kind'] == 'synthetic'
    assert (plan, evidence) == original


def test_missing_interrupted_and_tool_unknown_stay_in_denominators_and_costs():
    from cloudyfan_jev.reports import build_report, report_binding
    plan = plan_fixture()
    evidence = evidence_fixture(plan)
    evidence['runs'] = [r for r in evidence['runs'] if r['run_id'] != 'fixed_high-2']
    evidence['events'] = [e for e in evidence['events'] if e['run_id'] != 'fixed_high-2']
    cloud = next(r for r in evidence['runs'] if r['run_id'] == 'cloudyfan-2')
    cloud.update(first_pass=False, final_pass=None, status='engineering_interrupted')
    cloud['timing']['end_to_end_ms'] = '500'
    for suffix, kind, cost, attempt in [('classify', 'classification', '.25', None),
            ('repair', 'generation', '.5', 1), ('tool', 'tool', '.1', 1), ('unknown', 'tool', None, 1)]:
        evidence['events'].append({'event_id': suffix, 'run_id': 'cloudyfan-1', 'kind': kind,
            'attempt': attempt, 'retry': suffix == 'repair', 'cost_usd': cost,
            'receipt_id': None if cost is None else 'receipt:' + suffix, 'verified': False})
    evidence['runs'][1]['first_pass'] = False
    cal = deepcopy(next(r for r in evidence['runs'] if r['run_id'] == 'cloudyfan-1'))
    cal.update(run_id='cal', task_id='calibration-1', cohort='calibration',
               binding_sha256=report_binding(plan, 'cloudyfan', 'calibration-1', cohort='calibration')['binding_sha256'])
    evidence['runs'].append(cal)
    evidence['events'].append({'event_id': 'cal-fee', 'run_id': 'cal', 'kind': 'generation',
        'attempt': 0, 'retry': False, 'cost_usd': '.2', 'receipt_id': 'cal-receipt', 'verified': True})
    report = build_report(plan, evidence)
    assert report['groups']['fixed_high']['missing'] == 1
    assert report['groups']['fixed_high']['final_pass_rate'] == '0.5'
    assert report['groups']['fixed_high']['cost']['total_usd'] is None
    cloud = report['groups']['cloudyfan']
    assert cloud['engineering_interrupted'] == 1 and cloud['final_pass'] == 1
    assert cloud['cost']['known_usd'] == '7.85' and cloud['cost']['total_usd'] is None
    assert cloud['cost_per_success_usd'] is None
    assert cloud['generation_calls'] == 3 and cloud['classification_calls'] == 1 and cloud['retry_calls'] == 1
    assert cloud['repair_rate'] == '0.5'
    assert cloud['latency']['end_to_end_ms']['mean'] == '300'
    assert report['calibration']['cost']['known_usd'] == '0.2'
    assert report['comparisons']['cloudyfan']['unpaired']['candidate']['known_usd'] == '6'
    assert report['comparisons']['cloudyfan']['claim'] == 'spending_difference_only'


def test_reconciliation_replaces_provisional_cost_and_identical_replays_do_not_add_fees():
    from cloudyfan_jev.reports import build_report
    plan = plan_fixture()
    evidence = evidence_fixture(plan)
    evidence['events'].append(deepcopy(evidence['events'][0]))
    evidence['events'].append({'event_id': 'verified-cost', 'run_id': 'cloudyfan-1',
        'kind': 'reconciliation', 'replaces': 'cloudyfan-1:gen', 'cost_usd': '1.5',
        'receipt_id': 'receipt:cloudyfan-1', 'verified': True})
    # Original response cost is provisional; final receipt replaces it.
    next(e for e in evidence['events'] if e['event_id'] == 'cloudyfan-1:gen')['verified'] = False
    report = build_report(plan, evidence)
    assert report['groups']['cloudyfan']['cost']['known_usd'] == '7.5'
    assert report['groups']['fixed_high']['cost']['known_usd'] == '10'
    assert report['groups']['cloudyfan']['generation_calls'] == 2
    assert report['evidence_audit']['replayed_events'] == 1
    assert report['evidence_audit']['reconciliations_applied'] == 1


def test_conflicting_receipt_identity_retains_unknown_instead_of_double_count():
    from cloudyfan_jev.reports import build_report
    plan = plan_fixture()
    evidence = evidence_fixture(plan)
    evidence['events'][1]['receipt_id'] = evidence['events'][0]['receipt_id']
    report = build_report(plan, evidence)
    assert report['groups']['fixed_high']['cost']['total_usd'] is None
    assert report['groups']['fixed_high']['cost']['known_usd'] == '2'
    assert report['groups']['fixed_high']['generation_calls'] == 2
    assert report['evidence_audit']['anomalies'] == ['duplicate_receipt:fixed_high-2:gen']


def test_binding_drift_and_missing_model_events_cannot_claim_success_or_zero_cost():
    from cloudyfan_jev.reports import build_report
    plan = plan_fixture()
    evidence = evidence_fixture(plan)
    evidence['runs'][2]['binding_sha256'] = 'changed'
    evidence['events'] = [e for e in evidence['events'] if e['run_id'] != 'cloudyfan-2']
    report = build_report(plan, evidence)
    cloud = report['groups']['cloudyfan']
    assert cloud['final_pass'] == 0
    assert cloud['unknown'] == 2
    assert cloud['cost']['total_usd'] is None
    assert report['comparisons']['cloudyfan']['paired_tasks'] == []
    assert report['comparisons']['cloudyfan']['claim'] == 'spending_difference_only'
    assert 'binding_mismatch:cloudyfan-1' in report['evidence_audit']['anomalies']
    assert 'missing_generation_evidence:cloudyfan-2' in report['evidence_audit']['anomalies']


def test_zero_baseline_is_undefined_and_cost_increase_has_negative_direction():
    from cloudyfan_jev.reports import build_report
    plan = plan_fixture()
    evidence = evidence_fixture(plan)
    evidence['events'][0]['cost_usd'] = '0'
    evidence['events'][3]['cost_usd'] = '10'
    comparison = build_report(plan, evidence)['comparisons']['cloudyfan']
    assert comparison['aggregate_cdr_percent'] == '-37.500'
    assert comparison['per_task_cdr']['samples'] == 1
    assert comparison['per_task_cdr']['undefined'] == 1
    assert comparison['per_task_cdr']['mean_percent'] == '-25.00'
    assert comparison['per_task_cdr']['more_expensive_fraction'] == '1'


def test_posthoc_caching_zero_fee_scenario_and_plan_only_jev_attribution_are_explicit():
    from cloudyfan_jev.reports import build_report
    spec, pack, review = setup_plan()
    plan = build_plan(spec, pack, review, now=NOW)
    # Use only the one task that exists in this plan.
    evidence = evidence_fixture(plan_fixture())
    evidence.update(plan_id=plan['plan_id'])
    evidence['runs'] = [r for r in evidence['runs'] if r['task_id'] == 'holdout-1']
    from cloudyfan_jev.reports import report_binding
    for run in evidence['runs']:
        run['binding_sha256'] = report_binding(plan, run['arm'], run['task_id'])['binding_sha256']
    run_ids = {r['run_id'] for r in evidence['runs']}
    evidence['events'] = [e for e in evidence['events'] if e['run_id'] in run_ids]
    evidence['annotations'].update(posthoc=True, caching='mixed', zero_model_fee_scenario=True,
        limitations=['Original 25 tasks include three ambiguous contracts; 22-task analysis is posthoc and nonblind.'])
    report = build_report(plan, evidence)
    assert report['analysis_context']['posthoc'] is True
    assert report['analysis_context']['caching'] == 'mixed'
    assert report['analysis_context']['cost_basis'] == 'counterfactual_zero_model_fee'
    assert report['comparisons']['cloudyfan']['claim'] == 'scenario_spending_difference_only'
    assert report['jev_attribution']['identified'] is False
    assert 'attribution_arm_plan_only' in report['jev_attribution']['reasons']
    assert report['analysis_context']['limitations'] == evidence['annotations']['limitations']


def test_report_archive_check_does_not_require_unexpired_execution_authorization():
    from cloudyfan_jev.reports import build_report
    plan = plan_fixture()
    evidence = evidence_fixture(plan)
    # Report has no now/clock input and must be reproducible after plan expiration.
    assert build_report(plan, evidence)['plan_id'] == plan['plan_id']


def test_unknown_completeness_and_unpaired_missing_flow_do_not_become_zero():
    from cloudyfan_jev.reports import build_report
    plan = plan_fixture()
    evidence = evidence_fixture(plan)
    evidence['runs'] = [r for r in evidence['runs'] if r['run_id'] != 'cloudyfan-2']
    evidence['events'] = [e for e in evidence['events'] if e['run_id'] != 'cloudyfan-2']
    evidence['runs'][2]['accounting_complete'] = False  # tool cost evidence may be absent
    report = build_report(plan, evidence)
    comparison = report['comparisons']['cloudyfan']
    assert comparison['candidate_cost']['total_usd'] is None
    assert comparison['unpaired']['candidate']['total_usd'] is None
    assert comparison['unpaired']['candidate']['incomplete_workflows'] == 1
    assert report['overall_cost']['total_usd'] is None


def test_orphan_cost_is_reported_and_cannot_establish_complete_savings():
    from cloudyfan_jev.reports import build_report
    plan = plan_fixture()
    evidence = evidence_fixture(plan)
    evidence['events'].append({'event_id': 'orphan', 'run_id': 'missing-run', 'kind': 'tool',
        'attempt': 0, 'cost_usd': '.3', 'receipt_id': 'orphan-tool', 'verified': True})
    report = build_report(plan, evidence)
    assert report['unattributed_cost']['known_usd'] == '0.3'
    assert report['overall_cost']['known_usd'] == '17.3'
    assert report['comparisons']['cloudyfan']['claim'] == 'spending_difference_only'
    assert 'unattributed_event:orphan' in report['evidence_audit']['anomalies']


def test_rendered_report_keeps_quality_denominator_cost_scope_and_limitations_visible():
    from cloudyfan_jev.reports import build_report, render_markdown
    plan = plan_fixture()
    report = build_report(plan, evidence_fixture(plan))
    text = render_markdown(report)
    assert '| cloudyfan | 2/2 | 2/2 |' in text
    assert 'synthetic' in text
    assert '30.0' in text and '37.50' in text
    assert 'classification' in text and 'tool' in text
    assert 'same first configuration' in text


def test_per_task_trace_and_failed_quality_never_hide_higher_success_cost():
    from cloudyfan_jev.reports import build_report
    plan = plan_fixture()
    evidence = evidence_fixture(plan)
    evidence['runs'][3].update(first_pass=False, final_pass=False, status='failed')
    report = build_report(plan, evidence)
    comp = report['comparisons']['cloudyfan']
    assert comp['per_task'][1] == {'task_id': 'holdout-2', 'baseline_usd': '8', 'candidate_usd': '6',
                                 'cdr_percent': '25.00', 'baseline_status': 'passed', 'candidate_status': 'failed'}
    assert comp['claim'] == 'spending_difference_only'
    assert report['groups']['cloudyfan']['cost_per_success_usd'] == '7'
    assert report['groups']['cloudyfan']['final_pass_rate'] == '0.5'


def test_missing_fee_field_is_unknown_not_zero_or_parser_crash():
    from cloudyfan_jev.reports import build_report
    plan = plan_fixture()
    evidence = evidence_fixture(plan)
    del evidence['events'][2]['cost_usd']
    report = build_report(plan, evidence)
    assert report['groups']['cloudyfan']['cost']['unknown_events'] == 1
    assert report['groups']['cloudyfan']['cost']['known_usd'] == '6'
    assert report['groups']['cloudyfan']['cost']['total_usd'] is None


def test_report_provenance_does_not_copy_unrelated_private_source_fields():
    from cloudyfan_jev.reports import build_report
    plan = plan_fixture()
    evidence = evidence_fixture(plan)
    evidence['source']['private_prompt'] = 'private-source-value'
    report = build_report(plan, evidence)
    assert 'private-source-value' not in str(report)


def test_missing_first_pass_evidence_stays_unknown_and_provenance_kind_is_explicit():
    import pytest
    from cloudyfan_jev.reports import build_report
    plan = plan_fixture()
    evidence = evidence_fixture(plan)
    del evidence['runs'][2]['first_pass']
    report = build_report(plan, evidence)
    assert report['groups']['cloudyfan']['first_pass_rate'] == '0.5'
    assert report['groups']['cloudyfan']['first_unknown'] == 1
    evidence['source']['kind'] = 'unverified-label'
    with pytest.raises(ValueError, match='provenance kind'):
        build_report(plan, evidence)

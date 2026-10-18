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


def calibration_declaration(plan, arm, run_ids=(), *, status='complete'):
    from cloudyfan_jev.reports import calibration_binding
    return {'status': status, 'run_ids': list(run_ids), 'evidence_ref': 'fixture-calibration-ledger-v1',
            'binding_sha256': calibration_binding(plan, arm)}


def add_calibration_run(plan, evidence):
    from cloudyfan_jev.reports import report_binding
    row = deepcopy(evidence['runs'][2])
    row.update(run_id='cal-1', task_id='calibration-1', cohort='calibration',
               binding_sha256=report_binding(plan, 'cloudyfan', 'calibration-1', cohort='calibration')['binding_sha256'])
    evidence['runs'].append(row)
    evidence['events'].append({'event_id': 'cal-1:gen', 'run_id': 'cal-1', 'kind': 'generation',
        'attempt': 0, 'retry': False, 'cost_usd': '.2', 'receipt_id': 'cal-1-receipt', 'verified': True})


def test_missing_calibration_evidence_is_unknown_even_when_holdout_is_complete():
    from cloudyfan_jev.reports import build_report, render_markdown
    plan = plan_fixture()
    report = build_report(plan, evidence_fixture(plan))
    assert report['calibration']['cost']['total_usd'] is None
    assert report['calibration']['cost']['known_usd'] == '0'
    assert report['calibration']['cost']['incomplete_workflows'] == 2
    assert report['overall_cost']['total_usd'] is None
    assert report['overall_cost']['known_usd'] == '17'
    assert all(c['total_usd'] is None for c in report['calibration']['by_arm'].values())
    assert report['groups']['cloudyfan']['cost']['total_usd'] == '7'
    assert 'Calibration accounting: unknown' in render_markdown(report)


def test_partial_calibration_manifest_preserves_observed_cost_and_unknown_remainder():
    from cloudyfan_jev.reports import build_report
    plan = plan_fixture()
    evidence = evidence_fixture(plan)
    add_calibration_run(plan, evidence)
    evidence['calibration_accounting'] = {
        'fixed_high': calibration_declaration(plan, 'fixed_high', status='not_run'),
        'cloudyfan': calibration_declaration(plan, 'cloudyfan', ['cal-1', 'cal-missing'])}
    report = build_report(plan, evidence)
    assert report['calibration']['cost']['known_usd'] == '0.2'
    assert report['calibration']['cost']['total_usd'] is None
    assert report['calibration']['by_arm']['fixed_high']['total_usd'] == '0'
    assert report['calibration']['by_arm']['cloudyfan']['incomplete_workflows'] == 1
    assert report['overall_cost']['known_usd'] == '17.2'
    assert report['overall_cost']['total_usd'] is None


def test_explicit_bound_nonexecution_evidence_permits_zero_calibration_cost():
    from cloudyfan_jev.reports import build_report
    plan = plan_fixture()
    evidence = evidence_fixture(plan)
    evidence['calibration_accounting'] = {
        arm: calibration_declaration(plan, arm, status='not_run') for arm in plan['spec']['arms']}
    report = build_report(plan, evidence)
    assert report['calibration']['accounting_status'] == 'complete'
    assert report['calibration']['cost']['total_usd'] == '0'
    assert report['overall_cost']['total_usd'] == '17'


def test_complete_calibration_manifest_does_not_require_every_task_in_every_arm():
    from cloudyfan_jev.reports import build_report
    plan = plan_fixture()
    evidence = evidence_fixture(plan)
    add_calibration_run(plan, evidence)
    evidence['calibration_accounting'] = {
        'fixed_high': calibration_declaration(plan, 'fixed_high', status='not_run'),
        'cloudyfan': calibration_declaration(plan, 'cloudyfan', ['cal-1'])}
    report = build_report(plan, evidence)
    assert report['calibration']['accounting_status'] == 'complete'
    assert report['calibration']['cost']['total_usd'] == '0.2'
    assert report['overall_cost']['total_usd'] == '17.2'


def test_nonexecution_declaration_conflicting_with_observed_calibration_is_rejected():
    import pytest
    from cloudyfan_jev.reports import build_report
    plan = plan_fixture()
    evidence = evidence_fixture(plan)
    add_calibration_run(plan, evidence)
    evidence['calibration_accounting'] = {
        'cloudyfan': calibration_declaration(plan, 'cloudyfan', status='not_run')}
    with pytest.raises(ValueError, match='not_run'):
        build_report(plan, evidence)


def test_observed_calibration_without_inventory_is_retained_but_incomplete():
    from cloudyfan_jev.reports import build_report
    plan = plan_fixture()
    evidence = evidence_fixture(plan)
    add_calibration_run(plan, evidence)
    report = build_report(plan, evidence)
    assert report['calibration']['cost']['known_usd'] == '0.2'
    assert report['calibration']['cost']['total_usd'] is None
    assert report['calibration']['by_arm']['cloudyfan']['known_usd'] == '0.2'
    assert report['overall_cost']['known_usd'] == '17.2'
    assert report['overall_cost']['total_usd'] is None


def test_unlisted_calibration_run_cannot_be_silently_excluded_from_complete_inventory():
    from cloudyfan_jev.reports import build_report
    plan = plan_fixture()
    evidence = evidence_fixture(plan)
    add_calibration_run(plan, evidence)
    evidence['calibration_accounting'] = {
        'fixed_high': calibration_declaration(plan, 'fixed_high', status='not_run'),
        'cloudyfan': calibration_declaration(plan, 'cloudyfan', ['other-run'])}
    report = build_report(plan, evidence)
    assert report['calibration']['cost']['known_usd'] == '0.2'
    assert report['calibration']['cost']['total_usd'] is None
    assert report['calibration']['coverage']['cloudyfan']['unlisted_run_ids'] == ['cal-1']


def test_inventory_completeness_does_not_replace_per_run_fee_completeness():
    from cloudyfan_jev.reports import build_report
    plan = plan_fixture()
    evidence = evidence_fixture(plan)
    add_calibration_run(plan, evidence)
    evidence['calibration_accounting'] = {
        'fixed_high': calibration_declaration(plan, 'fixed_high', status='not_run'),
        'cloudyfan': calibration_declaration(plan, 'cloudyfan', ['cal-1'])}
    evidence['runs'][-1]['accounting_complete'] = False
    report = build_report(plan, evidence)
    assert report['calibration']['cost']['total_usd'] is None
    assert report['overall_cost']['total_usd'] is None
    evidence['runs'][-1]['accounting_complete'] = True
    evidence['events'][-1]['cost_usd'] = None
    report = build_report(plan, evidence)
    assert report['calibration']['cost']['unknown_events'] == 1
    assert report['calibration']['cost']['total_usd'] is None
    assert report['overall_cost']['total_usd'] is None


def test_calibration_declarations_require_cohort_binding_and_evidence_reference():
    import pytest
    from cloudyfan_jev.reports import build_report
    plan = plan_fixture()
    evidence = evidence_fixture(plan)
    declaration = calibration_declaration(plan, 'cloudyfan', status='not_run')
    for key, value in [('binding_sha256', 'wrong-plan'), ('evidence_ref', ''),
                       ('status', 'unknown'), ('run_ids', ['invented-run']),
                       ('binding_sha256', calibration_declaration(plan, 'fixed_high', status='not_run')['binding_sha256'])]:
        evidence['calibration_accounting'] = {'cloudyfan': {**declaration, key: value}}
        with pytest.raises(ValueError, match='[Cc]alibration'):
            build_report(plan, evidence)

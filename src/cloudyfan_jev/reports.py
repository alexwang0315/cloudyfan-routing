"""Read-only frozen-plan reports. No network, paid execution or grading hooks."""
from copy import deepcopy
from decimal import Decimal
from statistics import median

from .budget import money
from .experiments import verify_plan_integrity
from .scores.catalog import digest

SCHEMA = 'cloudyfan-report-evidence-v1'


def report_binding(plan, arm, task_id, *, cohort='holdout'):
    """Archive identity, NOT permission to execute a plan or its attribution arm."""
    verify_plan_integrity(plan)
    spec = plan['spec']
    if arm not in spec['arms'] or cohort not in ('holdout', 'calibration'):
        raise ValueError('Unknown report arm/cohort')
    tasks = spec['tasks'] if cohort == 'holdout' else spec['calibration_tasks']
    row = next((t for t in tasks if t['task_id'] == task_id), None)
    if row is None:
        raise ValueError('Unknown report task')
    body = {'plan_id': plan['plan_id'], 'arm': arm, 'task_id': task_id,
            'cohort': cohort, 'input_sha256': row['input_sha256'],
            'contract_version': spec['contract_version'], 'repair_policy': spec['repair_policy'],
            'limits': spec['limits'], 'max_generation_attempts': spec['max_generation_attempts'],
            'system_sha256': digest(spec['system']), 'diagnostic_contract': spec['diagnostic_contract'],
            'candidates': plan['candidates'], 'baseline': plan['baseline'],
            'decision_profile': spec['decision_profile'], 'routing_version': plan['routing_version']}
    return {**body, 'binding_sha256': digest(body)}


def _ratio(numerator, denominator):
    return str(Decimal(numerator) / Decimal(denominator)) if denominator else None


def _stats(values, expected):
    values = [money(v) for v in values if v is not None]
    return {'samples': len(values), 'missing': expected - len(values),
            'mean': str(sum(values, Decimal(0)) / len(values)) if values else None,
            'median': str(median(values)) if values else None}


def _cost(events, missing_workflows=0):
    known = sum((money(e['cost_usd']) for e in events if e['cost_usd'] is not None), Decimal(0))
    unknown = sum(e['cost_usd'] is None for e in events)
    return {'known_usd': str(known), 'verified_usd': str(sum((money(e['cost_usd']) for e in events if e.get('verified') is True and e['cost_usd'] is not None), Decimal(0))), 'unknown_events': unknown, 'incomplete_workflows': missing_workflows,
            'total_usd': None if unknown or missing_workflows else str(known)}


def _cdr(baseline, candidate):
    if baseline is None or candidate is None or money(baseline) == 0:
        return None
    return (money(baseline) - money(candidate)) / money(baseline) * 100


def _events(raw_events):
    unique, anomalies, replays, applied = {}, [], 0, 0
    for row in raw_events:
        event = deepcopy(row)
        event.setdefault('cost_usd', None)
        if not all(isinstance(event.get(k), str) and event[k] for k in ('event_id', 'run_id', 'kind')):
            raise ValueError('Event identity and kind required')
        if event['kind'] not in {'generation', 'classification', 'tool', 'reconciliation'}:
            raise ValueError('Unsupported accounting event kind')
        if event.get('cost_usd') is not None:
            money(event['cost_usd'])
        eid = event['event_id']
        if eid in unique:
            if unique[eid] == event:
                replays += 1
            else:
                unique[eid]['cost_usd'] = None
                anomalies.append('conflicting_event:' + eid)
            continue
        unique[eid] = event
    charges = {k: v for k, v in unique.items() if v['kind'] != 'reconciliation'}
    for event in unique.values():
        if event['kind'] != 'reconciliation':
            continue
        target = charges.get(event.get('replaces'))
        valid = (target is not None and target['run_id'] == event['run_id']
                 and event.get('verified') is True and event.get('cost_usd') is not None
                 and bool(event.get('receipt_id'))
                 and target.get('receipt_id') in (None, event['receipt_id']))
        if valid and target.get('verified') is True:
            valid = target.get('cost_usd') is not None and money(target['cost_usd']) == money(event['cost_usd'])
        if not valid:
            anomalies.append('invalid_reconciliation:' + event['event_id'])
            if target is not None:
                target['cost_usd'] = None
            else:
                charges[event['event_id']] = {**event, 'kind': 'unattributed', 'cost_usd': None}
            continue
        target.update(cost_usd=event['cost_usd'], receipt_id=event['receipt_id'], verified=True)
        applied += 1
    receipts = set()
    for eid, event in charges.items():
        receipt = event.get('receipt_id')
        if receipt and receipt in receipts:
            anomalies.append('duplicate_receipt:' + eid)
            event['cost_usd'] = None
        elif receipt:
            receipts.add(receipt)
        elif event.get('cost_usd') is not None:
            anomalies.append('missing_receipt:' + eid)
            event['cost_usd'] = None
    return list(charges.values()), {'replayed_events': replays,
            'reconciliations_applied': applied, 'anomalies': anomalies}


def _validate_runs(plan, runs, events, audit):
    ids, slots = set(), set()
    for run in runs:
        run.setdefault('first_pass', None)
        run.setdefault('final_pass', None)
        rid = run.get('run_id')
        if not isinstance(rid, str) or not rid or rid in ids:
            raise ValueError('Run IDs must be unique nonempty strings')
        ids.add(rid)
        slot = (run.get('arm'), run.get('cohort'), run.get('task_id'))
        if slot in slots:
            raise ValueError('Repeated tasks require a separately frozen repetition plan')
        slots.add(slot)
        expected = report_binding(plan, run['arm'], run['task_id'], cohort=run['cohort'])
        problems = []
        if run.get('binding_sha256') != expected['binding_sha256']:
            problems.append('binding_mismatch')
        if run['arm'] in plan.get('planned_only_arms', []):
            problems.append('plan_only_arm_claimed')
        status = run.get('status')
        if status not in ('passed', 'failed', 'unverifiable', 'engineering_interrupted', 'unknown'):
            raise ValueError('Explicit run outcome required')
        if any(run.get(k) is not None and type(run[k]) is not bool for k in ('first_pass', 'final_pass')):
            raise ValueError('Quality evidence must be true, false or null')
        expected_final = {'passed': True, 'failed': False, 'unverifiable': None,
                          'engineering_interrupted': None, 'unknown': None}[status]
        if run.get('final_pass') is not expected_final:
            problems.append('inconsistent_outcome')
        if not any(e['run_id'] == rid and e['kind'] == 'generation' for e in events):
            if status in ('passed', 'failed', 'unverifiable'):
                problems.append('missing_generation_evidence')
        for problem in problems:
            audit['anomalies'].append(problem + ':' + rid)
        run['evidence_valid'] = not problems
        if problems:
            run.update(first_pass=None, final_pass=None, status='unknown', accounting_complete=False)


def _group(runs, events, expected):
    first = sum(r['first_pass'] is True for r in runs)
    final = sum(r['final_pass'] is True for r in runs)
    missing = max(0, expected - len(runs))
    incomplete = missing + sum(r.get('accounting_complete') is not True for r in runs)
    cost = _cost(events, incomplete)
    repairs = {e['run_id'] for e in events if e['kind'] == 'generation' and e.get('attempt') == 1}
    return {'planned_tasks': expected, 'observed_runs': len(runs), 'missing': missing,
        'first_pass': first, 'final_pass': final,
        'first_failed': sum(r['first_pass'] is False for r in runs),
        'first_unknown': expected - first - sum(r['first_pass'] is False for r in runs),
        'final_unknown': expected - final - sum(r['final_pass'] is False for r in runs),
        **{status: sum(r['status'] == status for r in runs)
           for status in ('failed', 'unverifiable', 'engineering_interrupted', 'unknown')},
        'first_pass_rate': _ratio(first, expected), 'final_pass_rate': _ratio(final, expected),
        'cost': cost, 'cost_per_success_usd': _ratio(cost['total_usd'], final) if cost['total_usd'] is not None else None,
        'repair_runs': len(repairs), 'repair_rate': _ratio(len(repairs), expected),
        'classification_calls': sum(e['kind'] == 'classification' for e in events),
        'generation_calls': sum(e['kind'] == 'generation' for e in events),
        'tool_calls': sum(e['kind'] == 'tool' for e in events),
        'retry_calls': sum(e.get('retry') is True for e in events),
        'by_kind': {kind: _cost([e for e in events if e['kind'] == kind])
                    for kind in ('generation', 'classification', 'tool')},
        'latency': {key: _stats([r.get('timing', {}).get(key) for r in runs], expected)
                    for key in ('end_to_end_ms', 'validation_ms', 'queue_ms')}}


def build_report(plan, evidence):
    verify_plan_integrity(plan)
    if evidence.get('schema_version') != SCHEMA or evidence.get('plan_id') != plan['plan_id']:
        raise ValueError('Report evidence schema or plan identity mismatch')
    source = evidence['source']
    if any(not isinstance(source.get(k), str) or not source[k] for k in ('id', 'version', 'kind')):
        raise ValueError('Versioned evidence provenance required')
    if source['kind'] not in {'synthetic', 'observed', 'historical'}:
        raise ValueError('Explicit provenance kind must be synthetic, observed or historical')
    annotations = evidence.get('annotations', {})
    if (type(annotations.get('posthoc')) is not bool
            or type(annotations.get('zero_model_fee_scenario')) is not bool
            or annotations.get('caching') not in {'disabled', 'enabled', 'mixed', 'unknown'}):
        raise ValueError('Explicit posthoc, caching and scenario annotations required')
    context = {'posthoc': annotations['posthoc'], 'caching': annotations['caching'],
               'cost_basis': 'counterfactual_zero_model_fee' if annotations['zero_model_fee_scenario'] else 'recorded_api_fees',
               'limitations': list(annotations.get('limitations', [])),
               'archived_plan_status': plan['status'], 'plan_blockers': list(plan['blockers'])}
    groups = {}
    spec = plan['spec']
    task_ids = [t['task_id'] for t in spec['tasks']]
    runs = deepcopy(evidence['runs'])
    events, audit = _events(evidence['events'])
    _validate_runs(plan, runs, events, audit)
    known_run_ids = {r['run_id'] for r in runs}
    audit['anomalies'].extend('unattributed_event:' + e['event_id'] for e in events if e['run_id'] not in known_run_ids)
    def charges(selected_runs):
        ids = {r['run_id'] for r in selected_runs}
        return [e for e in events if e['run_id'] in ids]
    def run_cost(selected_runs, missing=0):
        return _cost(charges(selected_runs), missing + sum(r.get('accounting_complete') is not True for r in selected_runs))
    for arm in spec['arms']:
        rows = [r for r in runs if r['arm'] == arm and r['cohort'] == 'holdout']
        groups[arm] = _group(rows, charges(rows), len(task_ids))
    comparisons = {}
    by_pair = {(r['arm'], r['task_id']): r for r in runs if r['cohort'] == 'holdout' and r['evidence_valid']}
    for arm in spec['arms']:
        if arm == 'fixed_high':
            continue
        pairs, values, baseline_rows, candidate_rows, task_trace = [], [], [], [], []
        for task_id in task_ids:
            if ('fixed_high', task_id) not in by_pair or (arm, task_id) not in by_pair:
                continue
            pairs.append(task_id)
            base, candidate = by_pair['fixed_high', task_id], by_pair[arm, task_id]
            baseline_rows.append(base)
            candidate_rows.append(candidate)
            value = _cdr(run_cost([base])['total_usd'], run_cost([candidate])['total_usd'])
            task_trace.append({'task_id': task_id, 'baseline_usd': run_cost([base])['total_usd'],
                               'candidate_usd': run_cost([candidate])['total_usd'],
                               'cdr_percent': str(value) if value is not None else None,
                               'baseline_status': base['status'], 'candidate_status': candidate['status']})
            if value is not None:
                values.append(value)
        aggregate = _cdr(run_cost(baseline_rows)['total_usd'], run_cost(candidate_rows)['total_usd'])
        quality = not plan['blockers'] and len(pairs) == len(task_ids) and not audit['anomalies'] and all(
                      groups[a]['missing'] == 0 and groups[a]['final_pass_rate'] is not None and
                      Decimal(groups[a]['final_pass_rate']) >= Decimal(spec['quality_gate']['required_final_pass_rate'])
                      for a in ('fixed_high', arm)) and Decimal(groups[arm]['final_pass_rate']) >= Decimal(groups['fixed_high']['final_pass_rate'])
        unpaired_base = [r for r in runs if r['cohort'] == 'holdout' and r['arm'] == 'fixed_high' and r['task_id'] not in pairs]
        unpaired_candidate = [r for r in runs if r['cohort'] == 'holdout' and r['arm'] == arm and r['task_id'] not in pairs]
        comparisons[arm] = {'paired_tasks': pairs, 'per_task': task_trace, 'baseline_cost': run_cost(baseline_rows),
            'candidate_cost': run_cost(candidate_rows),
            'unpaired': {'baseline': run_cost(unpaired_base, len(task_ids) - len(pairs) - len(unpaired_base)),
                         'candidate': run_cost(unpaired_candidate, len(task_ids) - len(pairs) - len(unpaired_candidate))},
            'quality_gate_met': quality,
            'claim': ('scenario_spending_difference_only' if annotations['zero_model_fee_scenario'] else
                      'posthoc_spending_difference_only' if annotations['posthoc'] else
                      'spending_difference_only' if not quality or aggregate is None else
                      'synthetic_comparison' if plan['synthetic'] or source['kind'] == 'synthetic' else
                      'cache_affected_spending_difference_only' if annotations['caching'] != 'disabled' else
                      'quality_gated_savings' if aggregate > 0 else 'quality_matched_spending_difference'),
            'aggregate_cdr_percent': str(aggregate) if aggregate is not None else None,
            'per_task_cdr': {'samples': len(values), 'undefined': len(pairs) - len(values),
                'mean_percent': str(sum(values, Decimal(0)) / len(values)) if values else None,
                'median_percent': str(median(values)) if values else None,
                'min_percent': str(min(values)) if values else None, 'max_percent': str(max(values)) if values else None,
                'more_expensive_fraction': _ratio(sum(v < 0 for v in values), len(values))}}
    calibration = [r for r in runs if r['cohort'] == 'calibration']
    known_run_ids = {r['run_id'] for r in runs}
    unattributed = [e for e in events if e['run_id'] not in known_run_ids]
    executable_arms = [a for a in spec['arms'] if a not in plan.get('planned_only_arms', [])]
    common = [task_id for task_id in task_ids if all((arm, task_id) in by_pair for arm in executable_arms)]
    common_rows = [r for r in runs if r['cohort'] == 'holdout' and r['task_id'] in common and r['arm'] in executable_arms]
    noncommon_rows = [r for r in runs if r['cohort'] == 'holdout' and r not in common_rows]
    attribution_reasons = []
    if 'open_source_fixed_low' not in spec['arms']:
        attribution_reasons.append('matching_attribution_arm_absent')
    else:
        # Experiment schema v1 deliberately has no execution adapter for this arm.
        attribution_reasons.append('attribution_arm_plan_only')
    attribution_reasons.append('overall_workflow_difference_is_not_jev_only_gain')
    overall_incomplete = sum(groups[a]['cost']['incomplete_workflows'] for a in executable_arms)
    overall_incomplete += sum(r.get('accounting_complete') is not True for r in calibration)
    return {'schema_version': 'cloudyfan-comparison-report-v1', 'plan_id': plan['plan_id'],
            'source': {k: source[k] for k in ('id', 'version', 'kind')}, 'evidence_kind': 'synthetic' if plan['synthetic'] else source['kind'],
            'analysis_context': context, 'groups': groups, 'comparisons': comparisons, 'evidence_audit': audit,
            'overall_cost': _cost(events, overall_incomplete), 'unattributed_cost': _cost(unattributed),
            'common_pairing': {'arms': executable_arms, 'tasks': common, 'cost': run_cost(common_rows)},
            'unpaired_holdout_cost': run_cost(noncommon_rows),
            'calibration': {'observed_runs': len(calibration), 'cost': run_cost(calibration),
                'by_arm': {a: run_cost([r for r in calibration if r['arm'] == a]) for a in spec['arms']}},
            'jev_attribution': {'identified': False, 'reasons': attribution_reasons,
                'required_control': 'same first configuration, inputs, public validation and repair contract'},
            'formulas': {'aggregate_cdr_percent': '100 * (sum baseline paired full costs - sum candidate paired full costs) / sum baseline paired full costs',
                'per_task_cdr_percent': '100 * (baseline full cost - candidate full cost) / baseline full cost',
                'direction': 'positive means lower candidate spending; negative means more expensive',
                'undefined': 'zero baseline, unknown cost or no pairs',
                'quality_denominator': 'all frozen holdout tasks per arm, including missing evidence',
                'more_expensive_fraction_denominator': 'paired tasks with defined per-task CDR',
                'success_cost': 'all arm holdout charges, including failures, divided by final successes; unknown cost stays null'},
            'strata': {stratum: {arm: _group(
                [r for r in runs if r['cohort'] == 'holdout' and r['arm'] == arm and r['task_id'] in ids],
                charges([r for r in runs if r['cohort'] == 'holdout' and r['arm'] == arm and r['task_id'] in ids]), len(ids))
                for arm in spec['arms']}
                for stratum, ids in {s: [t['task_id'] for t in spec['tasks'] if t['stratum'] == s]
                                    for s in dict.fromkeys(t['stratum'] for t in spec['tasks'])}.items()}}


def render_markdown(report):
    """Compact human table; JSON retains event audits and all sample counts."""
    def shown(value):
        return 'unknown / undefined' if value is None else str(value)
    context = report['analysis_context']
    lines = [f"# Frozen-plan comparison ({report['evidence_kind']})", '',
        f"Plan: `{report['plan_id']}`. Cost basis: **{context['cost_basis']}**.",
        f"Posthoc: {context['posthoc']}; cache: {context['caching']}.", '',
        'Costs include generation, classification, retry, repair, failure and tool events.',
        'Pass denominators include every planned task, including missing evidence.', '',
        '| Arm | First pass | Final pass | Failed / unverifiable / interrupted / unknown / missing | Complete API USD | USD per success | End-to-end mean / median ms (n) | Repair rate |',
        '|---|---|---|---|---|---|---|---|']
    for arm, group in report['groups'].items():
        latency = group['latency']['end_to_end_ms']
        counts = ' / '.join(str(group[k]) for k in ('failed', 'unverifiable', 'engineering_interrupted', 'unknown', 'missing'))
        lines.append(f"| {arm} | {group['first_pass']}/{group['planned_tasks']} | {group['final_pass']}/{group['planned_tasks']} | {counts} | {shown(group['cost']['total_usd'])} | {shown(group['cost_per_success_usd'])} | {shown(latency['mean'])} / {shown(latency['median'])} ({latency['samples']}) | {shown(group['repair_rate'])} |")
    lines += ['', '| Arm | Classification / generation / retry / tool calls | Unknown fee events / incomplete flows | Validation mean / median ms (n) | Queue mean / median ms (n) |',
              '|---|---|---|---|---|']
    for arm, group in report['groups'].items():
        validation, queue = group['latency']['validation_ms'], group['latency']['queue_ms']
        counts = ' / '.join(str(group[k]) for k in ('classification_calls', 'generation_calls', 'retry_calls', 'tool_calls'))
        lines.append(f"| {arm} | {counts} | {group['cost']['unknown_events']} / {group['cost']['incomplete_workflows']} | {shown(validation['mean'])} / {shown(validation['median'])} ({validation['samples']}) | {shown(queue['mean'])} / {shown(queue['median'])} ({queue['samples']}) |")
    lines += ['', '| Compared with fixed_high | Pairs | Aggregate CDR % | Per-task mean / median / min / max % | More expensive fraction | Claim |',
              '|---|---|---|---|---|---|']
    for arm, comp in report['comparisons'].items():
        stats = comp['per_task_cdr']
        values = ' / '.join(shown(stats[k]) for k in ('mean_percent', 'median_percent', 'min_percent', 'max_percent'))
        lines.append(f"| {arm} | {len(comp['paired_tasks'])} | {shown(comp['aggregate_cdr_percent'])} | {values} | {shown(stats['more_expensive_fraction'])} | {comp['claim']} |")
    lines += ['', 'Positive CDR means lower candidate spending; zero baseline or unknown fees is undefined.',
        f"Calibration USD: {shown(report['calibration']['cost']['total_usd'])}; unpaired holdout USD: {shown(report['unpaired_holdout_cost']['total_usd'])}; unattributed USD: {shown(report['unattributed_cost']['total_usd'])}.",
        'Jev-only gain is not identified: ' + report['jev_attribution']['required_control'] + ' is required.',
        'A passed descriptive quality gate is not statistical equivalence or evidence of commercial profitability.',
        'Detailed JSON retains validation/queue timing sample counts, retries, unknowns, evidence anomalies and strata.']
    lines += [''] + ['- ' + str(limit).replace('\n', ' ') for limit in context['limitations']]
    return '\n'.join(lines).rstrip() + '\n'

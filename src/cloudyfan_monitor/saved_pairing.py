"""Pair saved original flows by identical input and compare their own saved outcomes.

Pure: receives the server-loaded original projections plus a private pairing
key per task. The rule and metrics are fixed in the request before loading.
No answer is reused across strategies, nothing is re-validated or re-run, and
there are no win rates, alternative outcomes or real-saving claims.
"""
from decimal import Decimal, InvalidOperation


RULE = 'same-input-v1'
METRICS = 'paired-saved-outcomes-v1'
TERMINAL = {'passed', 'failed'}


def _decimal(value):
    if not isinstance(value, str) or len(value) > 80:
        return None
    try:
        number = Decimal(value)
    except InvalidOperation:
        return None
    return number if number.is_finite() and number >= 0 else None


def _text(value):
    return None if value is None else format(value, 'f')


def _fee(costs):
    """Authoritative complete fee, or None when any event is unknown."""
    unknown = costs.get('unknown_events')
    complete = _decimal(costs.get('complete_api_usd'))
    return complete if unknown == 0 and complete is not None else None


def _member(entry):
    original = entry['original']
    path = original['path']
    status = original['task_status']
    first = path[0]['public_status'] if path else None
    return {'task': dict(entry['task']), 'mode': original['mode'], 'task_status': status,
            'first_public_status': first, 'generation_flows': len(path),
            'known_api_usd': original['costs']['known_api_usd'],
            'complete_api_usd': _text(_fee(original['costs'])),
            'unknown_events': original['costs']['unknown_events'],
            # Same flow only: first answer not confirmed, final public validation passed.
            'observed_rescue': status == 'passed' and len(path) > 1 and first != 'passed',
            'conditions': {'pool_version': original['pool_version'], 'contract_version': original['contract_version'],
                           'routing_version': original['routing_version']}}


def _group(members):
    reasons = []
    modes = [row['mode'] for row in members]
    if len(set(modes)) < 2:
        reasons.append('no_other_strategy')
    if len(set(modes)) != len(modes):
        reasons.append('duplicate_strategy_member')
    if any(row['mode'] is None for row in members):
        reasons.append('original_mode_unavailable')
    for name in ('pool_version', 'contract_version'):
        if len({row['conditions'][name] for row in members}) != 1 or members[0]['conditions'][name] is None:
            reasons.append(name + '_differs_or_unknown')
    if any(row['task_status'] not in TERMINAL for row in members):
        reasons.append('original_not_terminal')
    if any(row['complete_api_usd'] is None for row in members):
        reasons.append('complete_fee_unknown')
    quality, blocked = None, [code for code in reasons if code != 'complete_fee_unknown']
    if not blocked:
        accepted = sum(row['task_status'] == 'passed' for row in members)
        # Equal quality means every member met the same public acceptance.
        quality = 'equal' if accepted == len(members) else 'none_accepted' if not accepted else 'differs'
    status = ('not_comparable' if blocked else 'no_accepted_work' if quality == 'none_accepted'
              else 'quality_differs' if quality == 'differs'
              else 'fee_unknown' if reasons else 'equal_quality')
    return {'status': status, 'reasons': reasons, 'quality': quality,
            'members': sorted(members, key=lambda row: (str(row['mode']), row['task']['client'], row['task']['run_id']))}


def _arm(rows):
    accepted = [row for row in rows if row['task_status'] == 'passed']
    fees = [_decimal(row['complete_api_usd']) for row in rows]
    known = [value for value in (_decimal(row['known_api_usd']) for row in rows) if value is not None]
    unknown = sum(1 for value in fees if value is None)
    total = None if unknown else sum(fees, Decimal(0))
    return {'flows': len(rows), 'accepted': len(accepted),
            'failed': sum(row['task_status'] == 'failed' for row in rows),
            'not_terminal': sum(row['task_status'] not in TERMINAL for row in rows),
            'observed_rescues': sum(row['observed_rescue'] for row in rows),
            'known_api_usd': _text(sum(known, Decimal(0))), 'unknown_fee_flows': unknown,
            'complete_api_usd': _text(total),
            # Undefined with zero accepted work or any unknown fee; never zero-filled.
            'complete_api_usd_per_accepted': None if total is None or not accepted else _text(total / len(accepted))}


def compare_pairs(entries):
    """entries: [{'task', 'key', 'key_reason', 'original'}] in fixed scope order."""
    excluded, groups = [], {}
    for entry in entries:
        if entry['key'] is None:
            excluded.append({'task': dict(entry['task']), 'reason': entry['key_reason']})
        else:
            groups.setdefault(entry['key'], []).append(_member(entry))
    rows = [_group(members) for members in groups.values()]
    rows.sort(key=lambda row: [(member['task']['client'], member['task']['run_id']) for member in row['members']])
    for index, row in enumerate(rows):
        row['group'] = index
    paired = [row for row in rows if 'no_other_strategy' not in row['reasons']]
    modes = sorted({member['mode'] for row in paired for member in row['members'] if member['mode'] is not None})
    differences = []
    for index, first in enumerate(modes):
        for second in modes[index + 1:]:
            usable = [row for row in rows if row['status'] == 'equal_quality'
                      and {first, second} <= {member['mode'] for member in row['members']}]
            costs = {mode: sum((_decimal(member['complete_api_usd']) for row in usable for member in row['members']
                                if member['mode'] == mode), Decimal(0)) for mode in (first, second)}
            differences.append({'strategies': [first, second], 'equal_quality_groups': len(usable),
                'complete_api_usd': {mode: _text(costs[mode]) if usable else None for mode in (first, second)},
                'difference_usd': _text(costs[second] - costs[first]) if usable else None,
                'basis': 'equal_quality_groups_with_complete_fees'})
    members = [member for row in paired for member in row['members']]
    counts = {'requested': len(entries), 'keyed': len(entries) - len(excluded), 'excluded': len(excluded),
              'groups': len(rows), 'paired_groups': len(paired),
              'unpaired_groups': len(rows) - len(paired),
              **{status: sum(row['status'] == status for row in paired)
                 for status in ('equal_quality', 'quality_differs', 'no_accepted_work', 'fee_unknown', 'not_comparable')},
              'failed_flows': sum(member['task_status'] == 'failed' for member in members),
              'not_terminal_flows': sum(member['task_status'] not in TERMINAL for member in members),
              'unknown_fee_flows': sum(member['complete_api_usd'] is None for member in members)}
    return {'rule': RULE, 'metrics_version': METRICS, 'cache_condition': 'not_recorded_in_saved_projection',
            'counts': counts, 'excluded': excluded, 'groups': rows,
            'arms': {mode: _arm([member for member in members if member['mode'] == mode]) for mode in modes},
            'differences': differences, 'alternative_outcomes': 'not_observed', 'adopted': False}

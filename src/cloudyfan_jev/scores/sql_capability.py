"""Offline grading and bounded admission for exact-configuration SQL evidence.

Records/reviews are trusted application inputs, not authenticated supplier proof.
This module has no transport; missing/unknown evidence never creates qualification.
"""
from datetime import timedelta
import hashlib
from pathlib import Path
from decimal import Decimal, InvalidOperation
from collections import Counter

from ..sql import SqlValidationUnavailable
from ..generation_request import reasoning_matches
from .catalog import digest, timestamp
from .configuration import configuration_digest
from .sql_exam import cases, manifest, OPERATIONS

SCHEMA = 'sql-model-qualification-v1'
PRIOR_ROLE = 'family_effort_prior'


def _money(value):
    if not isinstance(value, str):
        raise ValueError('Settled cost must be an explicit decimal string')
    try:
        amount = Decimal(value)
        if not amount.is_finite() or amount < 0:
            raise ValueError('Invalid cost')
        return amount
    except InvalidOperation:
        raise ValueError('Invalid cost') from None


def grade_model(entry, records, *, now, synthetic=False):
    """Grade a complete saved cohort; public-only repair, sealed final grading.

    Capture/receipt authentication and paid execution are deliberately separate.
    Fixed-version responses/provider, request/response hashes, settled fees and
    unique request IDs are required. Unknown fees/identity remain engineering gaps.
    """
    instant = timestamp(now)
    if type(synthetic) is not bool:
        raise ValueError('Explicit synthetic flag required')
    cohort = cases()
    if not isinstance(records, list) or len(records) != len(cohort):
        raise ValueError('Exactly the frozen 50 task records are required')
    indexed = {r['task_id']: r for r in records}
    if len(indexed) != 50 or set(indexed) != {c.id for c in cohort}:
        raise ValueError('Missing/duplicate/unexpected task records')
    seen, dates, results = set(), [], []
    cost, billed_calls, unknown_costs = Decimal(0), 0, 0
    per_op = {op: Counter() for op in OPERATIONS}
    for case in cohort:
        record = indexed[case.id]
        attempts = record.get('attempts', [])
        if not isinstance(attempts, list) or any(not isinstance(a, dict) for a in attempts):
            raise ValueError('Attempts must be captured record objects')
        row = {'task_id': case.id, 'first_pass': None, 'final_pass': None,
               'outcome': 'unverifiable', 'attempts': len(attempts)}
        public_pass, finals, fault = [], [], None
        if not 1 <= len(attempts) <= 2:
            fault = 'missing_or_excess_attempts'
        for attempt in attempts:
            billed_calls += 1
            try:
                if attempt.get('cost_source') not in ('response_usage_cost', 'generation_total_cost'):
                    raise ValueError('Unverified or estimated fee')
                cost += _money(attempt.get('cost_usd'))
            except ValueError:
                unknown_costs += 1
                fault = 'unknown_cost'
            try:
                if (attempt['configuration_sha256'] != configuration_digest(entry)
                        or attempt['actual_model'] != entry['profile']['model_version']
                        or attempt['actual_provider'] != entry['profile']['provider_name']
                        or attempt['public_contract_sha256'] != digest(case.contract().task())
                        or not isinstance(attempt['request_id'], str) or not attempt['request_id']
                        or attempt['request_id'] in seen
                        or any(not isinstance(attempt[k], str) or len(attempt[k]) != 64
                               or any(c not in '0123456789abcdef' for c in attempt[k])
                               for k in ('request_sha256', 'response_sha256'))):
                    raise ValueError('identity')
                request, response = attempt['request'], attempt['response']
                expected_provider = {'only': [entry['profile']['provider']], 'allow_fallbacks': False,
                                     'require_parameters': True}
                if (digest(request) != attempt['request_sha256']
                        or digest(response) != attempt['response_sha256']
                        or request.get('model') != entry['profile']['model_version']
                        or not reasoning_matches(entry['profile'], request)
                        or request.get('provider') != expected_provider
                        or attempt.get('request_url') != entry['profile'].get('endpoint', 'https://openrouter.ai/api/v1/chat/completions')
                        or not any(m.get('role') == 'user' and m.get('content') == case.contract().task()['prompt'] for m in request.get('messages', []))
                        or request.get('tools') or request.get('plugins') or request.get('stream')
                        or response.get('model') != attempt['actual_model']
                        or response.get('provider') != attempt['actual_provider']
                        or response['choices'][0]['message']['content'] != attempt['answer']):
                    raise ValueError('request or response evidence')
                seen.add(attempt['request_id'])
                executed = timestamp(attempt['executed_at'])
                if executed > instant:
                    raise ValueError('future date')
                dates.append(executed)
                answer = attempt['answer']
                if not isinstance(answer, str):
                    raise ValueError('answer')
                public_pass.append(case.contract().environment().validate(answer)['passed'])
                finals.append(answer)
            except (KeyError, IndexError, ValueError, TypeError, SqlValidationUnavailable):
                fault = fault or 'identity_or_engineering_interruption'
        if len(public_pass) == 2 and public_pass[0] is True:
            fault = fault or 'repair_after_public_pass'
        if fault is None:
            try:
                env = case.contract(grading=True).environment()
                row['first_pass'] = env.validate(finals[0])['passed'] is True
                row['final_pass'] = public_pass[-1] is True and env.validate(finals[-1])['passed'] is True
                row['outcome'] = 'passed' if row['final_pass'] else 'model_failure'
            except SqlValidationUnavailable:
                fault = 'grading_interruption'
        if fault:
            row.update(outcome='engineering_or_evidence_gap', reason=fault, final_pass=None)
        for op in case.operations:
            per_op[op]['total'] += 1
            per_op[op]['first_pass'] += int(row['first_pass'] is True)
            per_op[op]['final_pass'] += int(row['final_pass'] is True)
            per_op[op]['first_evaluated'] += int(row['first_pass'] is not None)
            per_op[op]['final_evaluated'] += int(row['final_pass'] is not None)
        results.append(row)
    body = {'schema_version': SCHEMA, 'synthetic': synthetic,
            'configuration_sha256': configuration_digest(entry), 'suite_id': manifest()['suite_id'],
            'grader_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            'created_at': now, 'tested_from': min(dates).isoformat() if dates else None,
            'tested_until': max(dates).isoformat() if dates else None,
            'scope': '50 bounded SQLite tasks; all final cases must pass within two same-model attempts',
            'records_sha256': digest(records), 'case_count': 50, 'cases': results,
            'operations': {op: dict(counts) for op, counts in per_op.items()},
            'known_api_usd': str(cost), 'unknown_cost_count': unknown_costs, 'billed_calls': billed_calls,
            'classifier_calls': 0,
            'model_failure_count': sum(r['outcome'] == 'model_failure' for r in results),
            'engineering_count': sum(r['outcome'] == 'engineering_or_evidence_gap' for r in results),
            'qualified': all(r['final_pass'] is True for r in results) and unknown_costs == 0}
    return {**body, 'report_id': digest(body)}


def evidence_expiry(entry, proof, *, now, synthetic):
    """Return actual SQL evidence expiry or raise; no benchmark snapshot equality."""
    instant = timestamp(now)
    identity = proof.get('api_identity', {})
    if (proof.get('configuration_sha256') != configuration_digest(entry)
            or identity.get('status') != 'verified'
            or not isinstance(identity.get('evidence'), list) or not identity['evidence']
            or any(not isinstance(x, str) or not x.strip() for x in identity['evidence'])
            or identity.get('model_version') != entry['profile']['model_version']
            or identity.get('provider') != entry['profile']['provider']
            or identity.get('effort') != entry['profile']['effort']
            or identity.get('reasoning_parameter', 'effort') != entry['profile'].get('reasoning_parameter', 'effort')
            or ('reasoning_parameter' in entry['profile']
                and identity.get('reasoning_parameter') != entry['profile']['reasoning_parameter'])
            or type(identity.get('context_tokens')) is not int
            or identity['context_tokens'] < entry['context_tokens']):
        raise ValueError('api_identity_unproven')
    report = proof.get('sql_report', {})
    try:
        if (report.get('schema_version') != SCHEMA or report.get('report_id') != digest(
                {k: v for k, v in report.items() if k != 'report_id'})
                or report.get('suite_id') != manifest()['suite_id']
                or report.get('grader_sha256') != hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
                or report.get('configuration_sha256') != configuration_digest(entry)
                or type(report.get('synthetic')) is not bool
                or report['synthetic'] and not synthetic
                or report.get('qualified') is not True or report.get('unknown_cost_count') != 0
                or report.get('case_count') != 50 or len(report['cases']) != 50
                or {x['task_id'] for x in report['cases']} != {c.id for c in cases()}
                or any(x.get('final_pass') is not True or x.get('outcome') != 'passed'
                       or type(x.get('attempts')) is not int or not 1 <= x['attempts'] <= 2 for x in report['cases'])
                or not set(entry['capabilities']) <= set(report['operations'])):
            raise ValueError('report')
        required = Counter(op for c in cases() for op in c.operations)
        for op in entry['capabilities']:
            counts = report['operations'][op]
            if (type(counts['total']) is not int or counts['total'] != required[op]
                    or type(counts['final_pass']) is not int or counts['final_pass'] != required[op]):
                raise ValueError('coverage')
        start, end = timestamp(report['tested_from']), timestamp(report['tested_until'])
        if not start <= end <= timestamp(report['created_at']) <= instant:
            raise ValueError('dates')
        _money(report['known_api_usd'])
        expires = start + timedelta(days=30)
        if instant >= expires:
            raise ValueError('expired')
    except (KeyError, TypeError, ValueError):
        raise ValueError('sql_evidence_missing_invalid_or_expired') from None
    return expires.isoformat()


def captured_attempt(entry, case, intent, record):
    """Normalize existing Client intent/response; read-only, no reconciliation GET.

    Caller retains original files; unknown usage remains unknown. The hash covers
    original content before parsing, and the grader checks the saved payload too.
    """
    import json
    from ..client import _encoded, usage
    client_hash = hashlib.sha256(_encoded({'url': intent['url'], 'payload': intent['payload']})).hexdigest()
    if (intent.get('request_sha256') != client_hash
            or record.get('request_sha256') != intent.get('request_sha256')
            or record.get('call_id') != intent.get('call_id')):
        raise ValueError('Client intent/response binding mismatch')
    if (intent['payload'].get('model') != entry['profile']['model_version']
            or not reasoning_matches(entry['profile'], intent['payload'])):
        raise ValueError('Captured generation configuration mismatch')
    info = usage(record)
    try:
        response = json.loads(record['raw'])
        answer = response['choices'][0]['message']['content']
    except (ValueError, TypeError, KeyError, IndexError):
        response, answer = {}, ''
    return {'request_id': intent['call_id'], 'request_url': intent['url'],
            'request': intent['payload'], 'request_sha256': digest(intent['payload']),
            'response': response, 'response_sha256': digest(response),
            'client_intent_sha256': digest(intent), 'client_record_sha256': digest(record),
            'generation_id': info['generation_id'], 'actual_model': info['model'],
            'actual_provider': info['provider'], 'executed_at': intent['started_at'],
            'cost_usd': info['cost_usd'], 'cost_source': info['cost_source'],
            'answer': answer, 'configuration_sha256': configuration_digest(entry),
            'public_contract_sha256': digest(case.contract().task())}

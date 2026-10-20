"""Independent SQL exam and new family-prior admission; all sockets blocked."""
from collections import Counter
from copy import deepcopy
import json
import hashlib
from pathlib import Path

import pytest

from test_first_pool import profile, NOW
from cloudyfan_jev.scores.catalog import digest
from cloudyfan_jev.scores.configuration import configuration_digest
from cloudyfan_jev.scores.first_pool import prepare_pool
from cloudyfan_jev.scores.sql_exam import cases, manifest, OPERATIONS
from cloudyfan_jev.scores.sql_capability import evidence_expiry, grade_model, SCHEMA


def v2_fixture():
    value = profile()
    value['schema_version'] = 'cloudyfan-first-pool-profile-v2'
    value['synthetic'] = True
    value['policy']['domain'] = value['requirements']['domain'] = 'bounded-sqlite-readonly'
    value['requirements']['capabilities'] = list(OPERATIONS)
    for entry in [value['registry']['open_source'], *value['registry']['candidates']]:
        entry['capabilities'] = list(OPERATIONS)
    value['policy'].update(score_role='family_effort_prior', max_evaluation_age_days=None)
    for obs in value['observations']:
        obs['evaluated_at'] = None
    return value


def fixture_proof(entry, *, synthetic=True):
    counts = Counter(op for case in cases() for op in case.operations)
    body = {'schema_version': SCHEMA, 'configuration_sha256': configuration_digest(entry),
            'suite_id': manifest()['suite_id'],
            'grader_sha256': hashlib.sha256(Path(__import__('cloudyfan_jev.scores.sql_capability', fromlist=['']).__file__).read_bytes()).hexdigest(),
            'synthetic': synthetic, 'case_count': 50,
            'cases': [{'task_id': c.id, 'first_pass': True, 'final_pass': True,
                       'outcome': 'passed', 'attempts': 1} for c in cases()],
            'operations': {op: {'total': n, 'first_pass': n, 'final_pass': n} for op, n in counts.items()},
            'qualified': True, 'unknown_cost_count': 0, 'tested_from': NOW,
            'tested_until': NOW, 'created_at': NOW, 'known_api_usd': '0'}
    return {'configuration_sha256': configuration_digest(entry),
            'api_identity': {'status': 'verified', 'evidence': ['fixture://not-real-api'],
                             'model_version': entry['profile']['model_version'],
                             'provider': entry['profile']['provider'], 'effort': entry['profile']['effort'],
                             'context_tokens': 8192},
            'prior_association': {'basis': 'reviewed_family_effort_prior',
                                  **{k: entry[k] for k in ('source', 'source_version', 'source_model_id')},
                                  'effort': entry['profile']['effort']},
            'sql_report': {**body, 'report_id': digest(body)}}


def eligible_fixture():
    value = v2_fixture()
    for entry in [value['registry']['open_source'], *value['registry']['candidates']]:
        value['configuration_evidence'][entry['config_id']] = fixture_proof(entry)
    record = {'status': 'approved', 'reviewer': 'synthetic-fixture-not-formal-approval',
              'reviewed_at': NOW, 'evidence': ['fixture://synthetic']}
    value['review'] = {**{k: deepcopy(record) for k in ('domain', 'source', 'thresholds', 'coverage', 'expiry', 'access')},
                       'models': {k: deepcopy(record) for k in value['configuration_evidence']}}
    value['review']['input_sha256'] = prepare_pool(value, now=NOW)['qualification_pack']['input_sha256']
    return value


def test_v2_prior_does_not_claim_exact_benchmark_snapshot_or_supply_sql_approval():
    value = eligible_fixture()
    result = prepare_pool(value, now=NOW)
    assert result['status'] == 'synthetic_only'
    for pool in result['qualification_pack']['preview']['pools'].values():
        evidence = pool[0]['profile']['capability_card']['benchmark_evidence']
        assert evidence['evidence_role'] == 'family_effort_prior'
        assert evidence['api_snapshot_equivalence_proven'] is False
        card = pool[0]['profile']['capability_card']
        assert set(card['sql_operations'].values()) == {'supported_in_frozen_exam'}
        assert card['sql_exam']['synthetic'] is True
        assert 'cases' not in card['sql_exam']
    assert result['production_ready'] is False
    del value['configuration_evidence']['low']['sql_report']
    changed = prepare_pool(value, now=NOW)
    assert 'model:low:sql_evidence_missing_invalid_or_expired' in changed['blockers']


@pytest.mark.parametrize('schema,role', [('cloudyfan-first-pool-profile-v1', 'family_effort_prior'),
                                        ('cloudyfan-first-pool-profile-v2', 'exact_configuration')])
def test_policy_change_requires_explicit_new_schema(schema, role):
    value = v2_fixture()
    value['schema_version'] = schema
    value['policy']['score_role'] = role
    with pytest.raises(ValueError, match='explicit v2'):
        prepare_pool(value, now=NOW)


@pytest.mark.parametrize('field', ['model_version', 'effort', 'provider'])
def test_exact_api_configuration_change_invalidates_sql_evidence(field):
    value = eligible_fixture()
    value['registry']['candidates'][0]['profile'][field] += '-changed'
    assert 'model:low:api_identity_unproven' in prepare_pool(value, now=NOW)['blockers']


@pytest.mark.parametrize('change', ['synthetic', 'coverage', 'failed_case', 'suite', 'age', 'unknown_cost'])
def test_report_cannot_be_promoted_with_wrong_cohort_expired_or_unknown_evidence(change):
    value = eligible_fixture()
    entry = value['registry']['candidates'][0]
    proof = value['configuration_evidence']['low']
    if change == 'synthetic':
        with pytest.raises(ValueError, match='sql_evidence'):
            evidence_expiry(entry, proof, now=NOW, synthetic=False)
        return
    r = proof['sql_report']
    if change == 'coverage':
        r['operations']['sql_join']['final_pass'] -= 1
    elif change == 'failed_case':
        r['cases'][0]['final_pass'] = False
    elif change == 'suite':
        r['suite_id'] = 'a' * 64
    elif change == 'age':
        r['tested_from'] = '2026-08-01T00:00:00+00:00'
    else:
        r['unknown_cost_count'] = 1
    r['report_id'] = digest({k: v for k, v in r.items() if k != 'report_id'})
    with pytest.raises(ValueError, match='sql_evidence'):
        evidence_expiry(entry, proof, now=NOW, synthetic=True)


def test_qualification_expiry_limits_snapshot_even_with_fresh_score_and_catalog():
    value = eligible_fixture()
    for proof in value['configuration_evidence'].values():
        r = proof['sql_report']
        r['tested_from'] = '2026-09-20T01:00:00+00:00'
        r['report_id'] = digest({k: v for k, v in r.items() if k != 'report_id'})
    value['review']['input_sha256'] = prepare_pool(value, now=NOW)['qualification_pack']['input_sha256']
    result = prepare_pool(value, now=NOW)
    assert result['status'] == 'synthetic_only'
    assert result['qualification_pack']['preview']['valid_until'] == '2026-10-20T01:00:00+00:00'


def test_exam_is_fifty_reproducible_tasks_and_public_payload_never_contains_grading_material():
    m = manifest()
    assert m == manifest()
    assert len(m['cases']) == len({x['public_sha256'] for x in m['cases']}) == 50
    assert Counter(c.stratum for c in cases()) == {'easy': 15, 'medium': 20, 'hard': 15}
    assert set(op for c in cases() for op in c.operations) == set(OPERATIONS)
    for case in cases():
        task = case.contract().task()
        assert 'sealed' not in task['prompt']
        assert case.contract(grading=True)._public_json not in task['prompt']
        assert case.answer() not in task['prompt']


@pytest.mark.parametrize('case', cases(), ids=lambda c: c.id)
def test_python_oracle_and_frozen_sql_worker_agree_and_reject_semantic_mutant(case):
    public = case.contract().environment()
    grading = case.contract(grading=True).environment()
    assert public.validate(case.answer())['passed'] is True
    assert grading.validate(case.answer())['passed'] is True
    assert grading.validate(case.answer(mutant=True))['passed'] is False


def test_grader_requires_complete_unique_frozen_case_set():
    entry = v2_fixture()['registry']['candidates'][0]
    with pytest.raises(ValueError, match='50'):
        grade_model(entry, [], now=NOW, synthetic=True)
    with pytest.raises(ValueError, match='duplicate'):
        grade_model(entry, [{'task_id': 'Q001'}] * 50, now=NOW, synthetic=True)


def test_grader_retains_missing_evidence_and_unknown_fees_instead_of_calling_it_a_wrong_answer():
    entry = v2_fixture()['registry']['candidates'][0]
    rows = [{'task_id': c.id, 'attempts': []} for c in cases()]
    rows[0]['attempts'] = [{'cost_usd': None}]
    report = grade_model(entry, rows, now=NOW, synthetic=True)
    assert report['qualified'] is False
    assert report['unknown_cost_count'] == report['billed_calls'] == 1
    assert all(x['outcome'] == 'engineering_or_evidence_gap' for x in report['cases'])
    assert all(x['final_pass'] is None for x in report['cases'])
    assert report['model_failure_count'] == 0 and report['engineering_count'] == 50
    assert report['known_api_usd'] == '0'
    assert report['classifier_calls'] == 0


def captured_records(entry):
    rows = []
    for case in cases():
        request = {'model': entry['profile']['model_version'],
                   'reasoning': {'effort': entry['profile']['effort']},
                   'messages': [{'role': 'user', 'content': case.contract().task()['prompt']}],
                   'provider': {'only': [entry['profile']['provider']], 'allow_fallbacks': False,
                                'require_parameters': True}}
        response = {'model': entry['profile']['model_version'], 'provider': entry['profile']['provider_name'],
                    'choices': [{'message': {'content': case.answer()}}]}
        attempt = {'request_id': case.id, 'request': request, 'response': response,
                   'request_sha256': digest(request), 'response_sha256': digest(response),
                   'actual_model': response['model'], 'actual_provider': response['provider'],
                   'configuration_sha256': configuration_digest(entry),
                   'public_contract_sha256': digest(case.contract().task()), 'executed_at': NOW,
                   'answer': case.answer(), 'cost_usd': '0.001', 'cost_source': 'response_usage_cost',
                   'request_url': 'https://openrouter.ai/api/v1/chat/completions'}
        rows.append({'task_id': case.id, 'attempts': [attempt]})
    return rows


def install_synthetic_validation(monkeypatch):
    from types import SimpleNamespace
    from cloudyfan_jev.sql import SqlContract
    answers = {c.id: c.answer() for c in cases()}
    monkeypatch.setattr(SqlContract, 'environment', lambda self: SimpleNamespace(
        validate=lambda answer: {'passed': answer == answers[json.loads(self._public_json)['version'].split(':')[1]]}))


def test_captured_record_grading_counts_public_repair_and_all_costs(monkeypatch):
    install_synthetic_validation(monkeypatch)
    entry = v2_fixture()['registry']['candidates'][0]
    rows = captured_records(entry)
    repair = deepcopy(rows[0]['attempts'][0])
    repair['request_id'] += '-repair'
    rows[0]['attempts'][0]['answer'] = ''
    rows[0]['attempts'][0]['response']['choices'][0]['message']['content'] = ''
    rows[0]['attempts'][0]['response_sha256'] = digest(rows[0]['attempts'][0]['response'])
    rows[0]['attempts'].append(repair)
    r = grade_model(entry, rows, now=NOW, synthetic=True)
    assert r['qualified'] is True
    assert r['synthetic'] is True
    assert r['billed_calls'] == 51 and r['known_api_usd'] == '0.051'
    assert r['cases'][0]['first_pass'] is False and r['cases'][0]['final_pass'] is True


@pytest.mark.parametrize('mutation', ['effort', 'fallback', 'answer', 'request_hash', 'provider', 'extra_repair'])
def test_saved_records_cannot_hide_wrong_request_identity_or_unsolicited_retry(monkeypatch, mutation):
    install_synthetic_validation(monkeypatch)
    entry = v2_fixture()['registry']['candidates'][0]
    rows = captured_records(entry)
    attempt = rows[0]['attempts'][0]
    if mutation == 'effort':
        attempt['request']['reasoning']['effort'] = 'max'
    elif mutation == 'fallback':
        attempt['request']['provider']['allow_fallbacks'] = True
    elif mutation == 'answer':
        attempt['answer'] = 'substituted answer not in saved response'
    elif mutation == 'request_hash':
        attempt['request_sha256'] = 'a' * 64
    elif mutation == 'provider':
        attempt['response']['provider'] = 'different'
        attempt['response_sha256'] = digest(attempt['response'])
    else:
        second = deepcopy(attempt)
        second['request_id'] += '-unnecessary-repair'
        rows[0]['attempts'].append(second)
    if mutation != 'request_hash':
        attempt['request_sha256'] = digest(attempt['request'])
    r = grade_model(entry, rows, now=NOW, synthetic=True)
    assert r['qualified'] is False
    assert r['cases'][0]['outcome'] == 'engineering_or_evidence_gap'
    assert r['billed_calls'] >= 50


def test_readonly_capture_reuses_client_actual_cost_once_and_leaves_estimates_unknown():
    from cloudyfan_jev.scores.sql_capability import captured_attempt
    entry = v2_fixture()['registry']['candidates'][0]
    case = cases()[0]
    sample = captured_records(entry)[0]['attempts'][0]
    intent = {'call_id': 'fixture-one', 'url': sample['request_url'], 'payload': sample['request'],
              'started_at': NOW}
    from cloudyfan_jev.client import _encoded
    import hashlib
    intent['request_sha256'] = hashlib.sha256(_encoded({'url': intent['url'], 'payload': intent['payload']})).hexdigest()
    response = {**sample['response'], 'id': 'gen-fixture-sql-qualification', 'usage': {'cost': '0.003'}}
    record = {'call_id': 'fixture-one', 'request_sha256': intent['request_sha256'], 'raw': json.dumps(response),
              'reconciliation': {'verified': True, 'generation_id': response['id'], 'total_cost': '0.004'}}
    attempt = captured_attempt(entry, case, intent, record)
    assert attempt['cost_usd'] == '0.004' and attempt['cost_source'] == 'generation_total_cost'
    record.pop('reconciliation')
    response['usage'] = {'estimated_cost': '0.003'}
    record['raw'] = json.dumps(response)
    unknown = captured_attempt(entry, case, intent, record)
    assert unknown['cost_usd'] is None and unknown['cost_source'] == 'unknown'


def test_exam_preparation_is_offline_private_and_refuses_existing_output(tmp_path):
    import importlib.util
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location('prepare_exam', root / 'scripts/prepare_sql_model_exam.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    (tmp_path / 'examples').mkdir()
    for name in ['sql-model-versions-v1.json', 'first-pool-profile-sql-v2.json']:
        (tmp_path / 'examples' / name).write_bytes((root / 'examples' / name).read_bytes())
    out = tmp_path / 'runtime' / 'exam'
    report = module.prepare(tmp_path, out, now=NOW)
    assert report['tasks'] == 50 and report['real_api_calls'] == 0 and report['execution_authorized'] is False
    assert (out / 'assessor-only.json').stat().st_mode & 0o777 == 0o600
    assert 'control_answer' not in (out / 'public-tasks.json').read_text()
    with pytest.raises(FileExistsError):
        module.prepare(tmp_path, out, now=NOW)
    with pytest.raises(ValueError, match='ignored runtime'):
        module.prepare(tmp_path, tmp_path / 'docs' / 'exam', now=NOW)

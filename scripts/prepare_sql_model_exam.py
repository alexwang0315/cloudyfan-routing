"""Offline preparation only; no model transport or live-source fetch."""
import argparse
from decimal import Decimal
import json
from pathlib import Path

from cloudyfan_jev.scores.catalog import digest
from cloudyfan_jev.scores.first_pool import prepare_pool
from cloudyfan_jev.scores.sql_exam import cases, manifest


def prepare(root, output, *, now):
    root, output = Path(root).resolve(), Path(output).resolve()
    if not output.is_relative_to(root / 'runtime'):
        raise ValueError('Exam material must stay in ignored runtime, not tracked docs')
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    table = json.loads((root / 'examples/sql-model-versions-v1.json').read_text())
    suite = manifest()
    if table['suite_id'] != suite['suite_id']:
        raise ValueError('Version table and frozen suite mismatch')
    def save(name, value):
        path = output / (name + '.json')
        with path.open('x') as f:
            path.chmod(0o600)
            json.dump(value, f, ensure_ascii=False, indent=2)
            f.write('\n')
    public = [{'id': c.id, 'task': c.contract().task(), 'contract_version': c.contract().contract_version} for c in cases()]
    assessor = [{'id': c.id, 'grading_spec': json.loads(c.contract(grading=True)._public_json),
                 'control_answer': c.answer(), 'mutant': c.answer(mutant=True)} for c in cases()]
    rows = []
    for config in table['configurations']:
        rate = config['price_evidence']
        inp, out = Decimal(rate['input_per_token']), Decimal(rate['output_per_token'])
        smoke = 1024 * inp + 128 * out
        first = 50 * (8192 * inp + 4096 * out)
        repair = 50 * (16384 * inp + 4096 * out)
        rows.append({'role': config['role'], 'price_evidence': rate,
                     'identity_smoke_usd': str(smoke), 'first50_usd': str(first),
                     'maximum50_repairs_usd': str(repair), 'planning_subtotal_usd': str(smoke + first + repair)})
    total = sum(Decimal(r['planning_subtotal_usd']) for r in rows)
    plan = {'schema_version': 'sql-model-api-plan-v1', 'created_at': now, 'suite_id': suite['suite_id'],
            'version_table_sha256': digest(table), 'execution_authorized': False,
            'identity_calls': 4, 'first_answer_calls': 200, 'repair_calls_at_most': 200,
            'total_generation_calls_at_most': 404, 'classifier_calls': 0, 'automatic_retries': 0, 'max_output_tokens_per_attempt': 4096,
            'max_context_bytes': 32768,
            'threshold': 'All50 final public+sealed checks pass; at most two same-model attempts. Report first pass separately.',
            'schedule': 'Q001..Q050, rotate four roles by task index; no cherry-picking or model replacement.',
            'repair_rule': 'Only frozen public diagnostics; no repair after public pass. Sealed grading after complete capture.',
            'accounting': 'Include smoke, failed/empty answers, repairs, interruptions, unmatched/unknown charges. No estimate substituted for settled API fee.',
            'price_scenario': rows, 'planning_token_scenario_usd': str(total),
            'with25percent_contingency_usd': str(total * Decimal('1.25')),
            'proposed_authorization_usd': '60', 'enforceable_supplier_upper_bound_usd': None,
            'unpriced_fee_risks': ['cache-write or other separately billed input units', 'provider internal attempts/failure/cancellation charges', 'price changes and token-accounting differences'],
            'warning': 'Archived prices and token planning assumptions, not current quote, prediction or enforceable supplier cap. Actual tokenizer/internal/failure charges may differ.',
            'live_preflight_before_authorization': ['Revalidate free public catalog/prices and exact IDs; do not refresh old records.',
                'Record explicit spending authorization, cost evidence/limits and complete next-block reserve.',
                'Unresolved previous charge, unknown current fee, identity mismatch or partial intent stops new POST.',
                'If a safe executable bound/authorized exposure is unavailable, report before spending; do not invent RG04 qualification.'],
            'current_gate_status': 'Pending real API and separate formal/fee qualification; no current activation.'}
    profile = json.loads((root / table['profile']).read_text())
    save('public-tasks', public); save('assessor-only', assessor); save('suite', suite)
    save('versions', table); save('api-test-plan', plan); save('pending-pool-report', prepare_pool(profile, now=now))
    result = {'real_api_calls': 0, 'suite_id': suite['suite_id'], 'tasks': 50,
              'public_sha256': digest(public), 'assessor_sha256': digest(assessor),
              'planning_token_scenario_usd': str(total), 'suggested_budget_usd': '60',
              'source_prices_rechecked': False, 'execution_authorized': False}
    save('preparation', result)
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True)
    parser.add_argument('--now', required=True)
    args = parser.parse_args()
    print(json.dumps(prepare(Path(__file__).resolve().parents[1], args.output, now=args.now), indent=2))

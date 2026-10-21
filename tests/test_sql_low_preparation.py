"""Freeze two real metadata profiles offline without weakening SQL/fee gates."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal, Inexact, localcontext
import json
from pathlib import Path
import subprocess
import sys

import httpx
import pytest

from cloudyfan_jev.budget import Authorization, BudgetStore, EnforcedBound
from cloudyfan_jev.client import Client
from cloudyfan_jev.scores.catalog import digest
from cloudyfan_jev.scores.sql_low_preparation import prepare_low_trial, verify_low_trial
from cloudyfan_jev.scores.sql_multi_runner import MultiCandidateQualificationRunner

ROOT = Path(__file__).resolve().parents[1]
INPUT = ROOT / 'examples/sql-low-two-candidate-input-v1.json'
PLAN = ROOT / 'examples/sql-low-two-candidate-plan-v1.json'


def inputs():
    return json.loads(INPUT.read_text())


def frozen():
    return json.loads(PLAN.read_text())


def reseal(value, key):
    value[key] = digest({k: v for k, v in value.items() if k != key})
    return value


def prepare(value=None):
    return prepare_low_trial(value or inputs(), now=frozen()['created_at'], root=ROOT)


def test_checked_in_plan_is_reproducible_with_exact_source_and_fixture_bytes():
    source = inputs(); before = deepcopy(source)
    result = prepare(source)
    assert result == frozen() and source == before
    assert verify_low_trial(result, source, root=ROOT)['dispatch_authorized'] is False
    assert Decimal(result['budget']['planning_token_scenario_total_usd']) == Decimal('0.110664960')
    assert len(result['source_bindings']) == 17


def test_caller_decimal_context_cannot_change_frozen_costs_or_verification():
    expected = prepare()
    with localcontext() as context:
        context.prec = 6
        context.traps[Inexact] = True
        assert prepare() == expected
        assert verify_low_trial(expected, inputs(), root=ROOT)['valid']


def test_two_slots_are_measurements_not_a_complete_qualified_pool():
    plan = prepare()
    assert len(plan['table']['configurations']) == len(plan['table']['slots']['low']) == 2
    assert not plan['runtime_dispatch_enabled'] and plan['formal_qualification'] == 'pending'
    assert all(not row['runtime_identity_verified'] and not row['immutable_revision_verified']
               for row in plan['identities'])


def test_fixed_fifty_task_schedule_and_rotating_candidates_include_identity_and_repairs():
    protocol = prepare()['measurement_protocol']
    assert protocol['task_ids'] == [f'Q{i:03}' for i in range(1, 51)]
    assert protocol['identity_calls'] == 2 and protocol['first_answer_calls'] == 100
    assert protocol['repair_calls_at_most'] == 100 and protocol['generation_posts_at_most'] == 202
    assert protocol['classification_calls'] == protocol['automatic_retries'] == 0
    assert protocol['order'][0]['candidates'] == protocol['order'][1]['candidates'][::-1]
    assert protocol['identity_max_output_tokens'] == 128
    assert protocol['answer_max_output_tokens'] == 4096 and protocol['max_context_bytes'] == 32768


def test_candidate_measurement_and_workflow_correctness_have_distinct_contracts():
    contract = prepare()['admission_contract']
    assert contract['single_model_measurement']['perfect_accuracy_required_to_record_measurement'] is False
    assert contract['conditional_candidate_proposal']['model_errors_may_be_measured'] is True
    assert contract['workflow_quality']['require_all_frozen_holdout_tasks_correct'] is True
    assert contract['workflow_quality']['model_measurement_is_workflow_evidence'] is False
    assert contract['conditional_candidate_proposal']['new_runtime_admission_adapter_implemented'] is False
    assert not contract['runtime_dispatch_enabled']


def test_all_fee_cells_remain_unknown_even_without_reasoning_and_classifier_calls():
    plan = prepare(); matrix = plan['fee_gap_matrix']
    cells = [cell for candidate in matrix['candidates'] for cell in candidate['cells']]
    assert matrix['guarantee_cell_count'] == len(cells) == 112
    assert {c['status'] for c in cells} == {'pending_supplier_evidence'}
    assert all(c['enforced_units'] is None and c['guaranteed_rate_usd'] is None
               and c['evidence_ref'] is None for c in cells)
    assert plan['budget']['true_supplier_upper_bound_usd'] is None
    assert plan['budget']['research_target_usd'] == '1'
    assert not plan['budget']['spend_authorized'] and not plan['budget']['research_overrun_risk_accepted']
    assert plan['budget']['full_batch_reservation_required']


@pytest.mark.parametrize('field,value', [('model_version','example/different'),
    ('provider','different/fp8'), ('effort','low'), ('reasoning_parameter','effort'),
    ('quantization','bf16'), ('accepted_models',['example/alias'])])
def test_rehashed_identity_drift_cannot_change_frozen_low_selection(field, value):
    value_input = inputs(); value_input['candidates'][0]['entry']['profile'][field] = value
    reseal(value_input, 'input_id')
    with pytest.raises(ValueError, match='identity/order'):
        prepare(value_input)


@pytest.mark.parametrize('value', ['NaN', 'Infinity', '-0.01', '0', 0.01])
def test_unusable_prices_are_not_treated_as_free_or_as_bounds(value):
    source = inputs(); source['candidates'][0]['price_evidence']['input_usd_per_token'] = value
    reseal(source, 'input_id')
    with pytest.raises(ValueError):
        prepare(source)


@pytest.mark.parametrize('mutation', ['authorization','fee','schedule','workflow','source'])
def test_even_a_rehashed_plan_cannot_relax_scope_or_claim_evidence(mutation):
    plan = prepare()
    if mutation == 'authorization': plan['launch']['ready'] = True
    elif mutation == 'fee': plan['fee_gap_matrix']['candidates'][0]['complete_bound_usd'] = '0'
    elif mutation == 'schedule': plan['measurement_protocol']['task_ids'].reverse()
    elif mutation == 'workflow': plan['admission_contract']['workflow_quality']['require_all_frozen_holdout_tasks_correct'] = False
    else: plan['source_bindings']['sql.py'] = '0'*64
    reseal(plan, 'plan_id')
    with pytest.raises(ValueError, match='differs'):
        verify_low_trial(plan, inputs(), root=ROOT)


def test_future_or_expired_proposals_require_new_version_not_mutated_dates():
    plan = prepare()
    with pytest.raises(ValueError, match='expired'):
        verify_low_trial(plan, inputs(), root=ROOT, now='2026-10-23T00:00:00Z')
    source = inputs(); source['review_before'] = source['created_at']; reseal(source, 'input_id')
    with pytest.raises(ValueError, match='expired'):
        prepare(source)


def test_private_unrecognized_input_is_rejected_without_answer_projection():
    source = inputs(); source['answers'] = 'do-not-read'; reseal(source, 'input_id')
    with pytest.raises(ValueError, match='intact'):
        prepare(source)


def test_mock_identity_bridge_keeps_runtime_limits_and_completed_receipts(tmp_path):
    plan = prepare(); table = plan['table']; posts = []

    def upstream(request):
        payload = json.loads(request.content); posts.append(payload)
        profile = next(row['entry']['profile'] for row in table['configurations']
                       if row['entry']['profile']['model_version'] == payload['model'])
        assert 'reasoning' not in payload and not payload['provider']['allow_fallbacks']
        assert payload['max_tokens'] == 128
        return httpx.Response(200, json={'id':f'gen-plan-mock-{len(posts)}',
            'model':payload['model'], 'provider':profile['provider_name'],
            'choices':[{'message':{'content':'OK'}}], 'usage':{'cost':'0.001'}})

    store = BudgetStore(tmp_path / 'budget.sqlite')
    store.authorize(Authorization('mock-only', Decimal('1'),
        (datetime.now(timezone.utc)+timedelta(days=1)).isoformat()))
    bounds = {row['configuration_sha256']:EnforcedBound('0.001','synthetic-only')
              for row in table['configurations']}
    with httpx.Client(transport=httpx.MockTransport(upstream)) as http:
        runner = MultiCandidateQualificationRunner(Client(http,'mock-credential',tmp_path/'client'),
            store,table,model_bounds=bounds,verify_bound=lambda *args:True,synthetic=True)
        result = runner.capture('low-two-plan-mock',max_new_requests=2)
        assert result['reserve_usd'] == '0.202' and result['calls'] == 2
        assert runner.capture('low-two-plan-mock',max_new_requests=0)['calls'] == 2
    assert len(posts) == 2 and not plan['launch']['ready']


def test_cli_is_offline_exclusive_and_confined_to_runtime(tmp_path):
    path = ROOT/'runtime'/('low-two-cli-'+digest(str(tmp_path))[:16]+'.json')
    command = [sys.executable,str(ROOT/'scripts/prepare_sql_low_trial.py'),
               '--input',str(INPUT),'--now',frozen()['created_at'],'--output',str(path)]
    initial = subprocess.run(command,capture_output=True,text=True)
    assert initial.returncode == 0, initial.stderr
    before = path.read_bytes()
    second = subprocess.run(command,capture_output=True,text=True)
    assert second.returncode == 2 and path.read_bytes() == before
    verify = subprocess.run(command[:-2]+['--verify',str(path)],capture_output=True,text=True)
    assert verify.returncode == 0 and json.loads(verify.stdout)['dispatch_authorized'] is False
    outside = subprocess.run(command[:-1]+[str(tmp_path/'no-plan.json')],capture_output=True,text=True)
    assert outside.returncode == 2 and not (tmp_path/'no-plan.json').exists()

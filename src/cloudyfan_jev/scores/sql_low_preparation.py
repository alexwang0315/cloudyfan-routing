"""Offline-only freeze of two low-pool measurements; never authorizes dispatch."""
from copy import deepcopy
from datetime import timedelta
from decimal import Context, Decimal, localcontext
import hashlib
import platform
import sqlite3
from pathlib import Path

from ..client import CHAT
from ..component_bounds import COMPONENTS
from .catalog import digest, timestamp
from .configuration import configuration_digest
from .sql_exam import cases, manifest
from .sql_multi_runner import prepare_multi_candidate_table
from .sql_runner import SYSTEM

SCHEMA = 'cloudyfan-sql-low-two-plan-v1'
INPUT_SCHEMA = 'cloudyfan-sql-low-two-input-v1'
TARGETS = (
    ('primary', 'meta-llama/llama-3.1-8b-instruct', 'deepinfra/fp8', 'DeepInfra', 'fp8'),
    ('backup_1', 'google/gemma-3-4b-it', 'parasail/bf16', 'Parasail', 'bf16'),
)
PATHS = ('success', 'rejection', 'failure', 'cancellation', 'disconnect',
         'upstream_retry_or_child', 'price_change_or_inflight')
SOURCE_FILES = (
    'sql.py', 'sql_catalog.py', 'sql_policy.py', 'sql_worker.py', 'sql_executor.py',
    'sql_process.py', 'sql_isolation.py', 'sql_isolation_entry.py', 'sql_public_suite.py',
    'sql_counterexamples.json', 'scores/sql_exam.py', 'scores/sql_capability.py',
    'scores/sql_runner.py', 'scores/sql_multi_runner.py', 'generation_request.py', 'diagnostics.py',
    'scores/sql_low_preparation.py',
)


def source_bindings(root):
    root = Path(root)
    return {name: hashlib.sha256((root / 'src/cloudyfan_jev' / name).read_bytes()).hexdigest()
            for name in SOURCE_FILES}


def _seal(value, key):
    return {**value, key: digest(value)}


def _rate(value):
    if not isinstance(value, str):
        raise ValueError('Explicit decimal-string archived token price required')
    try:
        result = Decimal(value)
    except ArithmeticError:
        raise ValueError('Invalid archived price') from None
    if not result.is_finite() or result <= 0:
        raise ValueError('Finite positive archived token price required')
    return result


def _admission_contract():
    return _seal({
        'version': 'sql-measurement-and-workflow-admission-draft-v1',
        'status': 'offline_contract_only_not_runtime_admission',
        'single_model_measurement': {
            'questions': 50, 'max_same_model_attempts_per_question': 2,
            'required': ['exact_identity', 'all_50_records_present', 'public_and_sealed_outcomes',
                         'operation_strata_counts', 'all_attempt_costs', 'engineering_and_unknown_counts'],
            'perfect_accuracy_required_to_record_measurement': False,
            'model_errors_are_preserved_not_global_candidate_failure': True,
            'measurement_does_not_grant_dispatch': True,
            'legacy_strict_qualified_semantics': 'All50 final pass and no unknown fee; unchanged',
        },
        'conditional_candidate_proposal': {
            'model_errors_may_be_measured': True,
            'requires': ['exact_applicable_measurement', 'task_operation_scope',
                         'independent_escalation_risk_and_complete_cost_speed_evidence',
                         'explicit_source_data_and_fee_eligibility'],
            'unknown_or_engineering_error_is_success': False,
            'new_runtime_admission_adapter_implemented': False,
            'legacy_qualification_or_research_flags_reusable_as_new_approval': False,
        },
        'workflow_quality': {
            'require_all_frozen_holdout_tasks_correct': True,
            'count_failed_and_unpaired_flow_costs': True,
            'allow_hidden_answers_in_selection_or_repair': False,
            'calibration_and_holdout_separate': True,
            'quality_mismatch_label': 'spend_difference_only_not_equal_quality_saving',
            'model_measurement_is_workflow_evidence': False,
        },
        'production_qualification': 'pending', 'runtime_dispatch_enabled': False,
        'rg01_changed': False,
    }, 'contract_id')


def prepare_low_trial(inputs, *, now, root):
    """Read only pinned local metadata/source; no client, credential or assessor use."""
    required = {'schema_version', 'created_at', 'review_before', 'sources', 'candidates', 'input_id'}
    if (not isinstance(inputs, dict) or set(inputs) != required
            or inputs['schema_version'] != INPUT_SCHEMA
            or inputs['input_id'] != digest({k: v for k, v in inputs.items() if k != 'input_id'})):
        raise ValueError('Explicit intact low-two input required')
    instant = timestamp(now)
    if not timestamp(inputs['created_at']) <= instant < timestamp(inputs['review_before']):
        raise ValueError('Archived input is future-dated or expired')
    if not isinstance(inputs['sources'], list) or not inputs['sources']:
        raise ValueError('Archived source references required')
    for source in inputs['sources']:
        if (set(source) != {'path', 'sha256'} or not isinstance(source['path'], str)
                or not isinstance(source['sha256'], str) or len(source['sha256']) != 64
                or any(c not in '0123456789abcdef' for c in source['sha256'])):
            raise ValueError('Source metadata only; no private material')
    rows = inputs['candidates']
    if not isinstance(rows, list) or len(rows) != 2:
        raise ValueError('Exactly the two frozen low candidates required')
    entries, identities, estimates = [], [], []
    for row, (position, model, provider, provider_name, quantization) in zip(rows, TARGETS):
        if set(row) != {'position', 'entry', 'configuration_sha256', 'price_evidence'}:
            raise ValueError('Candidate metadata only')
        entry = row['entry']
        if not isinstance(entry, dict) or set(entry) != {'config_id', 'profile'}:
            raise ValueError('Exact entry metadata required')
        expected = {'model': model, 'model_version': model, 'accepted_models': [model],
                    'provider': provider, 'provider_name': provider_name, 'endpoint': CHAT,
                    'quantization': quantization, 'effort': 'none', 'reasoning_parameter': 'omit'}
        if row['position'] != position or entry['profile'] != expected:
            raise ValueError('Candidate identity/order changed; create a new plan version')
        if (not isinstance(entry['config_id'], str) or not entry['config_id']
                or row['configuration_sha256'] != configuration_digest(entry)):
            raise ValueError('Exact configuration hash required')
        price = row['price_evidence']
        if set(price) != {'input_usd_per_token', 'output_usd_per_token', 'observed_at', 'endpoint_raw_sha256'}:
            raise ValueError('Archived price metadata only')
        if timestamp(price['observed_at']) > instant:
            raise ValueError('Future-dated source price')
        raw_hash = price['endpoint_raw_sha256']
        if not isinstance(raw_hash, str) or len(raw_hash) != 64 or any(c not in '0123456789abcdef' for c in raw_hash):
            raise ValueError('Archived endpoint hash required')
        inp, out = _rate(price['input_usd_per_token']), _rate(price['output_usd_per_token'])
        # Input counts are planning scenarios, never supplier-enforced token caps.
        with localcontext(Context(prec=64)):
            quote = (50 * (8192 + 16384) + 1024) * inp + (100 * 4096 + 128) * out
        estimates.append({'configuration_sha256': row['configuration_sha256'],
                          'archived_price_evidence': deepcopy(price), 'token_scenario_usd': str(quote)})
        entries.append(deepcopy(entry))
        identities.append({'position': position, 'configuration_sha256': row['configuration_sha256'],
                           'declared_request_identity': deepcopy(expected),
                           'runtime_identity_verified': False, 'immutable_revision_verified': False,
                           'provider_quantization_runtime_verified': False,
                           'identity_smoke_required': True, 'formal_qualification': 'pending'})
    ids = [configuration_digest(e) for e in entries]
    table = prepare_multi_candidate_table(entries, slots={'low': ids})
    matrix = _seal({'components': sorted(COMPONENTS), 'paths': list(PATHS),
        'candidates': [{'configuration_sha256': identity, 'complete_bound_usd': None,
            'account_scope': None, 'valid_until': None, 'settlement_finality': 'unknown',
            'cells': [{'component': component, 'path': path, 'status': 'pending_supplier_evidence',
                       'enforced_units': None, 'guaranteed_rate_usd': None, 'evidence_ref': None}
                      for component in sorted(COMPONENTS) for path in PATHS]}
            for identity in ids],
        'guarantee_cell_count': len(ids) * len(COMPONENTS) * len(PATHS),
        'no_reasoning_or_classifier_dispatch_does_not_prove_no_separately_billed_component': True,
        'formal_fee_qualification': 'pending',
    }, 'matrix_id')
    with localcontext(Context(prec=64)):
        scenario_total = sum(Decimal(e['token_scenario_usd']) for e in estimates)
    cohort = cases()
    order = [{'task_id': case.id, 'candidates': [table['configurations'][(i+j) % 2]['role']
                                               for j in range(2)]} for i, case in enumerate(cohort)]
    body = {'schema_version': SCHEMA, 'created_at': now,
        'valid_until': min(timestamp(inputs['review_before']), instant + timedelta(hours=24)).isoformat(),
        'input_id': inputs['input_id'], 'sources': deepcopy(inputs['sources']),
        'source_bindings': source_bindings(root), 'suite_id': manifest()['suite_id'],
        'runtime': {'python_version': platform.python_version(), 'sqlite_version': sqlite3.sqlite_version},
        'preparer_cli_sha256': hashlib.sha256((Path(root) / 'scripts/prepare_sql_low_trial.py').read_bytes()).hexdigest(),
        'public_tasks_sha256': digest([{'id': c.id, 'task': c.contract().task(),
                                      'contract_version': c.contract().contract_version} for c in cohort]),
        'system_prompt': SYSTEM, 'table': table, 'identities': identities,
        'admission_contract': _admission_contract(), 'fee_gap_matrix': matrix,
        'measurement_protocol': {
            'task_ids': [c.id for c in cohort], 'order': order,
            'identity_order': [c['role'] for c in table['configurations']],
            'max_tasks_per_candidate': 50, 'identity_calls': 2, 'first_answer_calls': 100,
            'repair_calls_at_most': 100, 'generation_posts_at_most': 202,
            'classification_calls': 0, 'automatic_retries': 0,
            'max_context_bytes': 32768, 'identity_max_output_tokens': 128,
            'answer_max_output_tokens': 4096, 'max_same_model_attempts': 2,
            'repair': 'Only own previous answer and frozen public diagnostic; never hidden grader results',
            'sealed_grading': 'Only after complete capture and immutable seal; no feedback to capture',
            'failure': 'Preserve all model failures; no candidate replacement or cherry-picked rerun',
            'stop': ['unknown_fee', 'identity_mismatch', 'unresolved_intent', 'provider_failure',
                     'engineering_interruption', 'expired_input_or_plan', 'missing_authority_or_fee_bound'],
            'resumption': 'Same manifest and original receipt checkpoints; never resend paid/uncertain requests',
            'connection_checkpoint': 'After exactly two identity probes (max_new_requests=2); pause, reconcile, then resume same50',
            'future_model_evidence_reuse': 'Only identical questions, settings, contracts and applicable validity; link saved records, no resend',
            'claim_scope': 'Two low-candidate measurements only; not a routing/backup/Jev savings study',
        },
        'budget': {
            'research_target_usd': '1', 'currency': 'USD', 'spend_authorized': False,
            'prior_study_budget_carried_over': False, 'true_supplier_upper_bound_usd': None,
            'research_overrun_risk_accepted': False, 'unknown_fee_action': 'stop_all_new_posts',
            'reservation_for_existing_runner': '101 * sum(verified complete per-configuration bounds)',
            'full_batch_reservation_required': True, 'bounds_by_configuration_sha256': {i: None for i in ids},
            'known_plus_unsettled_plus_required_reserve_must_fit_target': True,
            'price_scenarios': estimates,
            'planning_token_scenario_total_usd': str(scenario_total),
            'planning_input_tokens': {'identity': 1024, 'first': 8192, 'repair': 16384},
            'input_scenario_is_enforced_cap': False, 'cache_read_or_write_discount_assumed': False,
            'unpriced_risks': ['billed_input_and_tokenization', 'cache_write_or_other_input_fees',
                              'reasoning_and_other_components', 'internal_retry_failure_cancel_inflight',
                              'rate_or_account_changes'],
            'risk_exception_changes_existing_bound_verifier': False,
        },
        'launch': {'ready': False, 'model_post_authorized': False, 'production_qualified': False,
            'required': ['explicit_new_spending_authorization', 'current_exact_identity_price_and_source_checks', 'current_exact_endpoint_health',
                         'applicable_data_policy_public_synthetic_only', 'all_previous_unknown_fees_resolved_or_explicitly_isolated',
                         'effective_complete_bounds_and_full_batch_reserve_fit_1usd',
                         'same_frozen_manifest_and_source_hashes'],
            'research_exception': 'Separate explicit risk authorization and reviewed versioned execution design required; this plan cannot bypass the current runner/verifier.',
            'classifier_or_tool_or_provider_fallback_enabled': False},
        'runtime_dispatch_enabled': False, 'formal_qualification': 'pending',
        'rg01_changed': False, 'model_api_calls': 0, 'source_gets': 0,
    }
    return _seal(body, 'plan_id')


def verify_low_trial(plan, inputs, *, root, now=None):
    """Reject drift, rehashed manual approvals and expired launch proposals offline."""
    if not isinstance(plan, dict) or plan != prepare_low_trial(inputs, now=plan.get('created_at'), root=root):
        raise ValueError('Frozen low-two plan differs from pinned input/source/protocol')
    if now is not None and not timestamp(plan['created_at']) <= timestamp(now) < timestamp(plan['valid_until']):
        raise ValueError('Frozen low-two plan expired or not yet valid')
    return {'valid': True, 'plan_id': plan['plan_id'], 'dispatch_authorized': False,
            'formal_qualification': 'pending'}

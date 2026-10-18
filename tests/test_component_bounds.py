"""Complete component evidence exercised through Core and durable mock transport.

All qualifications below are deliberately synthetic; no real endpoint is approved.
"""
from datetime import datetime, timezone
from decimal import Decimal, localcontext
import hashlib
import copy

import httpx
import pytest

from cloudyfan_jev.budget import Authorization, BudgetError, BudgetStore, BudgetedCore
from cloudyfan_jev.client import CHAT, DECISIONS
from cloudyfan_jev.engine import Limits
from cloudyfan_jev.component_bounds import ComponentBoundVerifier, REQUIRED_CLAIMS
from cloudyfan_jev.provider_bounds import credential_fingerprint
from cloudyfan_jev.receipt_store import _encoded
from test_engine import API, ArithmeticEnvironment, TASK, core

NOW = datetime(2026, 10, 18, tzinfo=timezone.utc).timestamp()

# Version-2 contracts are literal synthetic supplier assertions, not real proofs.
V2_CLAIMS = [
    'billed_input_accounting_includes_serialization_and_rejected_requests',
    'nonzero_rate_components_have_enforced_cumulative_billed_unit_caps',
    'effective_rate_ceilings_cover_cache_tiers_and_price_changes',
    'classification_fee_complete_without_double_counting',
    'complete_inventory_no_unlisted_or_embedded_charges',
    'no_advisor_plugins_media_tools_byok_or_implicit_features',
    'single_post_includes_all_upstream_attempt_charges',
    'failure_cancellation_and_inflight_charges_bounded',
    'guarantees_survive_expiry_or_revocation_for_dispatched_requests',
]
ZERO_CLAIMS = [
    'zero_rate_binds_exact_configuration_endpoint_account_and_validity',
    'zero_rate_covers_all_usage_success_rejection_failure_cancellation_and_upstream_attempts',
    'no_separate_or_reclassified_component_charges',
    'zero_rate_survives_price_changes_expiry_and_withdrawal_for_dispatched_requests',
]


def digest(value):
    return hashlib.sha256(_encoded(value)).hexdigest()


def component_setup(tmp_path, *, api=None, clock=None, feature_variant=None, classifier_version=None):
    api = api or API(first='3', choices=('mid', 'high'))
    clock = clock or [NOW]
    engine = core(tmp_path / 'client', api, limits=Limits(max_turns=1))
    if feature_variant:
        engine.profiles['high'].update(model=feature_variant, model_version=feature_variant,
                                       accepted_models=[feature_variant])
    if classifier_version:
        engine.decision['version'] = classifier_version
    configurations = {p['model_version']: p for p in engine.profiles.values()}
    configurations[engine.decision['version']] = engine.decision
    registry_identity = ComponentBoundVerifier(engine.client,
        withdrawal_root=tmp_path / 'fee-reviews').descriptor()['withdrawal_root_sha256']
    schedules, reviews = [], []
    for model, config in configurations.items():
        decision = 'version' in config
        units = {'input': 1000, 'output': 64 if decision else engine.limits.max_tokens,
                 'reasoning': 0, 'cache_read': 1000, 'cache_write': 1000,
                 'request': 1, 'classification': int(decision), 'failure': 1}
        rates = {'input': '0.000001', 'output': '0.000002', 'reasoning': '0',
                 'cache_read': '0.0000002', 'cache_write': '0.0000015',
                 'request': '0.0001', 'classification': '0.0002' if decision else '0',
                 'failure': '0.0003'}
        schedule = {'schema_version': 1, 'mechanism': 'qualified_components_v1',
                    'model_version': model, 'endpoint': DECISIONS if decision else CHAT,
                    'credential_sha256': credential_fingerprint(engine.client.key),
                    'configuration_sha256': digest(config), 'max_payload_bytes': 131072,
                    'output_control': 'qualified_endpoint_cap' if decision else 'request_max_tokens',
                    'reasoning_mode': 'included_in_output',
                    'components': {name: {'max_units': units[name], 'unit_usd': rate}
                                   for name, rate in rates.items()},
                    'claims': sorted(REQUIRED_CLAIMS),
                    'sources': {name: digest('SYNTHETIC ONLY ' + name) for name in
                                ('quantities', 'rates', 'inventory', 'failures')},
                    'issued_at': NOW - 1, 'expires_at': NOW + 300}
        schedules.append(schedule)
        reviews.append({'evidence_sha256': digest(schedule), 'reviewer': 'synthetic-reviewer',
                        'reference': 'offline-fixture-not-supplier-proof',
                        'reviewed_at': NOW - 1, 'expires_at': NOW + 300,
                        'withdrawal_registry_sha256': registry_identity})

    def make(withdrawal_root=None):
        return ComponentBoundVerifier(engine.client, configurations=configurations,
                                      schedules=schedules, reviews=reviews, clock=lambda: clock[0],
                                      withdrawal_root=withdrawal_root or tmp_path / 'fee-reviews')

    def app(verifier, bounds=None):
        store = BudgetStore(tmp_path / 'budget.sqlite',
                            clock=lambda: datetime.fromtimestamp(clock[0], timezone.utc))
        store.authorize(Authorization('offline synthetic authorization', Decimal('2'),
                                      '2099-01-01T00:00:00+00:00'))
        if bounds is None:
            bounds = {model: verifier.bound_for(model) for model in configurations}
        return BudgetedCore(engine, store, model_bounds=bounds, tool_bounds={}, verify_bound=verifier)

    return engine, api, schedules, reviews, clock, make, app


def test_reviewed_components_reserve_and_account_full_two_stage_tree(tmp_path):
    engine, api, schedules, reviews, clock, make, app = component_setup(tmp_path)
    verifier = make()
    # Hand-worked fixture: .001 + .016384 + .0002 + .0015 + .0001 + .0003.
    assert verifier.bound_for('example/high').usd == '0.0194840'
    assert verifier.bound_for(engine.decision['version']).usd == '0.0034280'
    controlled = app(verifier)
    result = controlled.run('complete', TASK, ArithmeticEnvironment, contract_version='v1')
    assert result['final_public_pass'] is True
    assert result['selection']['classifier_calls'] == 2
    assert result['budget']['run_total_usd'] == '0.0202'
    assert result['budget']['shared']['reservations'][0]['amount'] == '0.0458240'
    assert [p['model'] for p in api.requests] == ['example/open_source', 'typesafe/jev-1.13',
                                                'typesafe/jev-1.13', 'example/high']
    assert controlled.enforcement_descriptor()['version'] == 'qualified-components-v1'


def qualify_zero_components(schedule, review, names):
    """Explicit new artifact/review; never reinterpret a v1 zero price as a waiver."""
    if schedule['schema_version'] == 1:
        schedule.update(schema_version=2, mechanism='qualified_components_v2', claims=V2_CLAIMS.copy())
        for component in schedule['components'].values():
            component['basis'] = 'bounded_quantity'
    for name in names:
        schedule['components'][name] = {'basis': 'guaranteed_zero_rate', 'unit_usd': '0',
            'guarantee': {'source_sha256': digest('SYNTHETIC zero-rate promise: ' + name),
                          'claims': ZERO_CLAIMS.copy()}}
        if name == 'output': schedule['output_control'] = 'binding_zero_rate'
        if name == 'reasoning': schedule['reasoning_mode'] = 'binding_zero_rate'
    review['evidence_sha256'] = digest(schedule)


def test_independently_guaranteed_free_decision_output_needs_no_token_cap(tmp_path):
    engine, api, schedules, reviews, clock, make, app = component_setup(tmp_path)
    for schedule, review in zip(schedules, reviews, strict=True):
        if schedule['endpoint'] == DECISIONS:
            qualify_zero_components(schedule, review, ['output', 'reasoning'])
    verifier = make()
    # Synthetic all-path zero output replaces .000128, retaining every other fee.
    assert verifier.bound_for(engine.decision['version']).usd == '0.0033000'
    result = app(verifier).run('free-decision-output', TASK, ArithmeticEnvironment, contract_version='v1')
    assert result['final_public_pass'] is True
    assert result['selection']['classifier_calls'] == 2
    assert result['budget']['shared']['reservations'][0]['amount'] == '0.0455680'
    assert result['budget']['run_total_usd'] == '0.0202'
    assert len(api.requests) == 4
    assert verifier.descriptor()['version'] == 'qualified-components-v2'


def test_zero_price_chat_output_keeps_text_limits_without_claiming_a_billed_token_cap(tmp_path):
    engine, api, schedules, reviews, clock, make, app = component_setup(tmp_path, api=API(first='2'))
    for schedule, review in zip(schedules, reviews, strict=True):
        if schedule['model_version'] == 'example/high':
            schedule['components']['input']['unit_usd'] = '0.00002'
            qualify_zero_components(schedule, review, ['output', 'reasoning'])
    verifier = make()
    # .020 input + .0002 read + .0015 write + .0001 request + .0003 failure.
    assert verifier.bound_for('example/high').usd == '0.0221000'
    result = app(verifier).run('free-chat-output', TASK, ArithmeticEnvironment,
                               contract_version='v1', mode='fixed_high')
    assert result['status'] == 'passed'
    assert result['budget']['run_total_usd'] == '0.01'
    assert api.requests[0]['max_tokens'] == engine.limits.max_tokens
    # Operational shape limits and paid features are still enforced.
    payload = text_payload(engine)
    payload['max_tokens'] = 0
    assert verifier.verify_dispatch(engine.client, CHAT, payload, verifier.bound_for('example/high')) is False
    payload = text_payload(engine)
    payload['plugins'] = [{'id': 'web'}]
    assert verifier.verify_dispatch(engine.client, CHAT, payload, verifier.bound_for('example/high')) is False


@pytest.mark.parametrize(('name', 'expected'), [
    ('input', '0.018484'), ('output', '0.003100'), ('reasoning', '0.019484'),
    ('cache_read', '0.019284'), ('cache_write', '0.017984'), ('request', '0.019384'),
    ('classification', '0.019484'), ('failure', '0.019184'),
])
def test_each_guaranteed_zero_component_needs_no_quantity_but_retains_other_fees(tmp_path, name, expected):
    engine, api, schedules, reviews, clock, make, app = component_setup(tmp_path)
    for schedule, review in zip(schedules, reviews, strict=True):
        if schedule['model_version'] == 'example/high':
            qualify_zero_components(schedule, review, [name])
    # Independent hand-worked fee sums; input-free does not make cache free.
    assert Decimal(make().bound_for('example/high').usd) == Decimal(expected)
    assert api.requests == []


def test_free_output_does_not_make_separately_billed_reasoning_free(tmp_path):
    engine, api, schedules, reviews, clock, make, app = component_setup(tmp_path)
    for schedule, review in zip(schedules, reviews, strict=True):
        if schedule['endpoint'] == DECISIONS:
            qualify_zero_components(schedule, review, ['output'])
            schedule['reasoning_mode'] = 'separate_endpoint_cap'
            schedule['components']['reasoning'].update(max_units=7, unit_usd='0.001')
            review['evidence_sha256'] = digest(schedule)
    assert Decimal(make().bound_for(engine.decision['version']).usd) == Decimal('0.0103')


@pytest.mark.parametrize('problem', [
    'no_guarantee', 'no_source', 'bad_source', 'no_claims', 'missing_path_claim', 'duplicate_claim',
    'nonzero_price', 'float_zero', 'negative_zero', 'units_null', 'units_zero', 'extra_fee',
    'missing_input_cap', 'missing_paid_reasoning_cap', 'catalog_zero_without_cap',
    'wrong_output_control', 'wrong_reasoning_control', 'missing_component',
    'missing_inventory_claim', 'missing_retry_claim', 'missing_inflight_claim', 'missing_global_source',
    'wrong_version', 'wrong_mechanism', 'legacy_claims', 'legacy_schema', 'expired', 'no_review',
])
def test_zero_rate_waiver_requires_complete_exact_reviewed_evidence(tmp_path, problem):
    engine, api, schedules, reviews, clock, make, app = component_setup(tmp_path)
    schedule = next(s for s in schedules if s['endpoint'] == DECISIONS)
    review = next(r for r in reviews if r['evidence_sha256'] == digest(schedule))
    qualify_zero_components(schedule, review, ['output', 'reasoning'])
    output = schedule['components']['output']
    if problem == 'no_guarantee': del output['guarantee']
    elif problem == 'no_source': del output['guarantee']['source_sha256']
    elif problem == 'bad_source': output['guarantee']['source_sha256'] = 'catalog-price-is-not-a-proof'
    elif problem == 'no_claims': output['guarantee']['claims'] = []
    elif problem == 'missing_path_claim': output['guarantee']['claims'].remove(ZERO_CLAIMS[1])
    elif problem == 'duplicate_claim': output['guarantee']['claims'].append(ZERO_CLAIMS[0])
    elif problem == 'nonzero_price': output['unit_usd'] = '0.00000000001'
    elif problem == 'float_zero': output['unit_usd'] = 0.0
    elif problem == 'negative_zero': output['unit_usd'] = '-0'
    elif problem == 'units_null': output['max_units'] = None
    elif problem == 'units_zero': output['max_units'] = 0
    elif problem == 'extra_fee': output['request_surcharge'] = '0.1'
    elif problem == 'missing_input_cap': del schedule['components']['input']['max_units']
    elif problem == 'missing_paid_reasoning_cap':
        schedule['reasoning_mode'] = 'separate_endpoint_cap'
        schedule['components']['reasoning'] = {'basis': 'bounded_quantity', 'unit_usd': '0.01'}
    elif problem == 'catalog_zero_without_cap':
        output.clear()
        output.update(basis='bounded_quantity', unit_usd='0')
        schedule['output_control'] = 'qualified_endpoint_cap'
    elif problem == 'wrong_output_control': schedule['output_control'] = 'qualified_endpoint_cap'
    elif problem == 'wrong_reasoning_control': schedule['reasoning_mode'] = 'included_in_output'
    elif problem == 'missing_component': del schedule['components']['failure']
    elif problem == 'missing_inventory_claim': schedule['claims'].remove('complete_inventory_no_unlisted_or_embedded_charges')
    elif problem == 'missing_retry_claim': schedule['claims'].remove('single_post_includes_all_upstream_attempt_charges')
    elif problem == 'missing_inflight_claim': schedule['claims'].remove('guarantees_survive_expiry_or_revocation_for_dispatched_requests')
    elif problem == 'missing_global_source': del schedule['sources']['failures']
    elif problem == 'wrong_version': schedule['schema_version'] = 3
    elif problem == 'wrong_mechanism': schedule['mechanism'] = 'qualified_components_v1'
    elif problem == 'legacy_claims': schedule['claims'] = sorted(REQUIRED_CLAIMS)
    elif problem == 'legacy_schema':
        schedule.update(schema_version=1, mechanism='qualified_components_v1', claims=sorted(REQUIRED_CLAIMS))
    elif problem == 'expired': clock[0] = NOW + 301
    elif problem == 'no_review': reviews.remove(review)
    review['evidence_sha256'] = digest(schedule)
    with pytest.raises(ValueError):
        make().bound_for(engine.decision['version'])
    assert api.requests == []


def test_changed_zero_rate_guarantee_requires_a_new_independent_review(tmp_path):
    engine, api, schedules, reviews, clock, make, app = component_setup(tmp_path)
    schedule = next(s for s in schedules if s['endpoint'] == DECISIONS)
    review = next(r for r in reviews if r['evidence_sha256'] == digest(schedule))
    qualify_zero_components(schedule, review, ['output', 'reasoning'])
    schedule['components']['output']['guarantee']['source_sha256'] = digest('different supplier promise')
    with pytest.raises(ValueError, match='review'):
        make().bound_for(engine.decision['version'])
    assert api.requests == []


@pytest.mark.parametrize('version', [1, 2])
def test_a_zero_price_without_a_guarantee_still_requires_the_quantity_cap(tmp_path, version):
    engine, api, schedules, reviews, clock, make, app = component_setup(tmp_path)
    schedule = next(s for s in schedules if s['endpoint'] == DECISIONS)
    review = next(r for r in reviews if r['evidence_sha256'] == digest(schedule))
    if version == 2:
        qualify_zero_components(schedule, review, [])
    schedule['components']['output'].update(max_units=None, unit_usd='0')
    review['evidence_sha256'] = digest(schedule)
    with pytest.raises(ValueError):
        make().bound_for(engine.decision['version'])
    assert api.requests == []


def test_mixed_zero_rate_workflow_preserves_exact_reservation_at_low_decimal_precision(tmp_path):
    engine, api, schedules, reviews, clock, make, app = component_setup(tmp_path)
    for schedule, review in zip(schedules, reviews, strict=True):
        qualify_zero_components(schedule, review, [k for k in schedule['components'] if k != 'input'])
        schedule['components']['input'].update(max_units=1, unit_usd='0.010000000000000000000000000001')
        review['evidence_sha256'] = digest(schedule)
    with localcontext() as context:
        context.prec = 3
        result = app(make()).run('exact-zero-mix', TASK, ArithmeticEnvironment, contract_version='v1')
    assert result['budget']['shared']['reservations'][0]['amount'] == '0.040000000000000000000000000004'
    assert result['budget']['run_total_usd'] == '0.0202'
    assert result['final_public_pass'] is True


@pytest.mark.parametrize('change', ['expiry', 'withdrawal', 'new_registry'])
def test_zero_rate_review_still_expires_or_can_be_withdrawn_before_post(tmp_path, change):
    engine, api, schedules, reviews, clock, make, app = component_setup(tmp_path)
    for schedule, review in zip(schedules, reviews, strict=True):
        if schedule['endpoint'] == DECISIONS:
            qualify_zero_components(schedule, review, ['output', 'reasoning'])
    verifier, payload = make(), decision_payload()
    bound = verifier.bound_for(engine.decision['version'])
    assert verifier.verify_dispatch(engine.client, DECISIONS, payload, bound) is True
    if change == 'expiry': clock[0] = NOW + 301
    elif change == 'withdrawal': make().revoke(bound.evidence.removeprefix('component-cap:'))
    else:
        with pytest.raises(ValueError, match='review'):
            make(tmp_path / 'different-registry').bound_for(engine.decision['version'])
        return
    assert verifier.verify_post(engine.client, DECISIONS, payload, bound) is False
    assert api.requests == []


@pytest.mark.parametrize('outcome', ['zero', 'unknown', 'breach'])
def test_whole_zero_rate_flow_preserves_accounting_unknowns_and_breach_stops(tmp_path, outcome):
    api = API(first='3', choices=('mid', 'high'))
    attempts = []
    def transport(request):
        attempts.append(request.method)
        if outcome == 'unknown': raise httpx.ReadTimeout('synthetic interrupted zero-rate request')
        response = api(request).json()
        response['usage']['cost'] = '0.0001' if outcome == 'breach' else '0'
        return httpx.Response(200, json=response)
    engine, _, schedules, reviews, clock, make, app = component_setup(tmp_path, api=transport)
    for schedule, review in zip(schedules, reviews, strict=True):
        qualify_zero_components(schedule, review, list(schedule['components']))
    verifier = make()
    assert verifier.bound_for('example/high').usd == '0'
    controlled = app(verifier)
    if outcome == 'zero':
        result = controlled.run('all-free', TASK, ArithmeticEnvironment, contract_version='v1')
        assert result['final_public_pass'] is True and result['selection']['classifier_calls'] == 2
        assert result['budget']['run_total_usd'] == '0'
        assert result['budget']['shared']['reservations'][0]['amount'] == '0'
        assert attempts == ['POST'] * 4
    else:
        with pytest.raises(RuntimeError):
            controlled.run('all-free', TASK, ArithmeticEnvironment, contract_version='v1')
        report = controlled.store.report()
        if outcome == 'unknown':
            assert report['unknown_events'] == 1
            assert report['dispatch_blocked'] is True
        else:
            # A breached guarantee keeps the event unresolved and preserves the
            # observed charge in the durable violation, never overwriting it as 0.
            assert report['unknown_events'] == 1 and report['total_usd'] is None
            assert report['violations'][0]['observed_cost'] == '0.0001'
            assert report['dispatch_blocked'] is True
        with pytest.raises(BudgetError):
            app(make()).run('all-free', TASK, ArithmeticEnvironment, contract_version='v1')
        assert attempts == ['POST']


def test_prices_claims_and_key_alone_do_not_approve_component_evidence(tmp_path):
    engine, api, schedules, reviews, clock, make, app = component_setup(tmp_path)
    bounds = {s['model_version']: make().bound_for(s['model_version']) for s in schedules}
    reviews.clear()
    with pytest.raises(BudgetError, match='unconfirmed'):
        app(make(), bounds).run('unreviewed', TASK, ArithmeticEnvironment, contract_version='v1')
    assert api.requests == []


def decision_payload():
    return {'model': 'typesafe/jev-1.13', 'state': {'task': 'one plus one'},
            'questions': {'route': {'type': 'choice', 'instructions': 'Choose one',
                                   'criteria': {'low': 'Enough', 'high': 'Required'}}},
            'provider': {'only': ['typesafe'], 'allow_fallbacks': False}}


def test_multimodal_decision_state_is_not_admitted_as_free_text(tmp_path):
    engine, api, schedules, reviews, clock, make, app = component_setup(tmp_path)
    verifier = make()
    payload = decision_payload()
    payload['state']['attachments'] = [{'type': 'image_url', 'image_url': 'https://example.test/a.png'}]
    assert verifier.verify_dispatch(engine.client, DECISIONS, payload,
                                    verifier.bound_for(engine.decision['version'])) is False
    assert api.requests == []


def test_workflow_and_account_exposure_ignore_ambient_decimal_rounding(tmp_path):
    engine, api, schedules, reviews, clock, make, app = component_setup(tmp_path)
    with localcontext() as context:
        context.prec = 3
        result = app(make()).run('precision', TASK, ArithmeticEnvironment, contract_version='v1')
        report = result['budget']['shared']
        assert report['reservations'][0]['amount'] == '0.0458240'
        assert report['known_usd'] == '0.0202'
        assert report['provisional_hold_usd'] == '0.0256240'
        assert report['available_usd'] == '1.9541760'


def test_withdrawn_review_stays_withdrawn_after_verifier_reconstruction(tmp_path):
    engine, api, schedules, reviews, clock, make, app = component_setup(tmp_path)
    verifier = make()
    bound = verifier.bound_for('example/high')
    verifier.revoke(bound.evidence.removeprefix('component-cap:'))
    with pytest.raises(ValueError, match='review'):
        make().bound_for('example/high')
    assert api.requests == []


@pytest.mark.parametrize('model', ['example/high:online', 'example/high:free',
                                  '~example/high-latest', 'openrouter/auto', 'typesafe/jev-router'])
def test_implicit_features_and_router_variants_cannot_be_qualified_as_fixed_text(tmp_path, model):
    engine, api, schedules, reviews, clock, make, app = component_setup(tmp_path, feature_variant=model)
    with pytest.raises(ValueError):
        app(make()).run('variant', TASK, ArithmeticEnvironment, contract_version='v1', mode='fixed_high')
    assert api.requests == []


def test_classifier_dispatch_uses_qualified_version_instead_of_unpriced_alias(tmp_path):
    engine, api, schedules, reviews, clock, make, app = component_setup(
        tmp_path, classifier_version='typesafe/jev-1.13-20260917')
    result = app(make()).run('pinned-classifier', TASK, ArithmeticEnvironment, contract_version='v1')
    assert result['status'] == 'passed'
    assert [p['model'] for p in api.requests[1:3]] == ['typesafe/jev-1.13-20260917'] * 2


@pytest.mark.parametrize('problem', ['missing_failure', 'unbounded_failure', 'snapshot', 'expired',
                                   'missing_rejected_input', 'no_sources', 'float_rate', 'negative_rate',
                                   'missing_cache', 'cache_quantity', 'extra_charge', 'reasoning_double_count',
                                   'decision_uses_chat_cap', 'duplicate_review', 'review_expired'])
def test_incomplete_or_inconsistent_fee_proof_never_yields_a_bound(tmp_path, problem):
    engine, api, schedules, reviews, clock, make, app = component_setup(tmp_path)
    target = next(s for s in schedules if s['model_version'] == (
        engine.decision['version'] if problem == 'decision_uses_chat_cap' else 'example/high'))
    review = next(r for r in reviews if r['evidence_sha256'] == digest(target))
    if problem == 'missing_failure': del target['components']['failure']
    elif problem == 'unbounded_failure': target['components']['failure']['max_units'] = None
    elif problem == 'snapshot': target['mechanism'] = 'catalog_price_estimate'
    elif problem == 'expired': target['expires_at'] = NOW
    elif problem == 'missing_rejected_input': target['claims'].remove('billed_input_cap_includes_serialization_and_rejected_requests')
    elif problem == 'no_sources': target['sources'] = {}
    elif problem == 'float_rate': target['components']['input']['unit_usd'] = 0.000001
    elif problem == 'negative_rate': target['components']['input']['unit_usd'] = '-1'
    elif problem == 'missing_cache': del target['components']['cache_write']
    elif problem == 'cache_quantity': target['components']['cache_write']['max_units'] = 999
    elif problem == 'extra_charge': target['components']['search'] = {'max_units': 1, 'unit_usd': '0.01'}
    elif problem == 'reasoning_double_count': target['components']['reasoning'] = {'max_units': 10, 'unit_usd': '0.01'}
    elif problem == 'decision_uses_chat_cap': target['output_control'] = 'request_max_tokens'
    elif problem == 'duplicate_review': reviews.append(copy.deepcopy(review))
    elif problem == 'review_expired': review['expires_at'] = NOW
    review['evidence_sha256'] = digest(target)
    with pytest.raises(ValueError):
        make().bound_for(target['model_version'])
    assert api.requests == []


def text_payload(engine):
    return {'model': 'example/high', 'messages': [{'role': 'user', 'content': '1+1'}],
            'max_tokens': engine.limits.max_tokens, 'reasoning': {'effort': 'low'}, 'session_id': 'fixture',
            'provider': {'only': ['example'], 'allow_fallbacks': False, 'require_parameters': True}}


@pytest.mark.parametrize('change', ['plugins', 'fallback', 'provider', 'tools', 'media', 'output_tokens',
                                  'reasoning', 'payload_bytes', 'key', 'endpoint', 'other_client', 'tier'])
def test_actual_request_must_stay_inside_qualified_text_fee_scope(tmp_path, change):
    engine, api, schedules, reviews, clock, make, app = component_setup(tmp_path)
    verifier = make()
    bound, payload, client, endpoint = verifier.bound_for('example/high'), text_payload(engine), engine.client, CHAT
    if change == 'plugins': payload['plugins'] = [{'id': 'web'}]
    elif change == 'fallback': payload['provider']['allow_fallbacks'] = True
    elif change == 'provider': payload['provider']['only'] = ['other']
    elif change == 'tools': payload['tools'] = []
    elif change == 'media': payload['messages'][0]['content'] = [{'type': 'image_url'}]
    elif change == 'output_tokens': payload['max_tokens'] += 1
    elif change == 'reasoning': payload['reasoning'] = {'effort': 'high'}
    elif change == 'payload_bytes': payload['messages'][0]['content'] = '字' * 131072
    elif change == 'key': client.key = 'other-synthetic-key'
    elif change == 'endpoint': endpoint = DECISIONS
    elif change == 'other_client': client = core(tmp_path / 'other', api).client
    elif change == 'tier': payload['service_tier'] = 'priority'
    assert verifier.verify_dispatch(client, endpoint, payload, bound) is False
    assert api.requests == []


def test_request_lease_is_single_use_and_review_revocation_invalidates_it(tmp_path):
    engine, api, schedules, reviews, clock, make, app = component_setup(tmp_path)
    verifier, payload = make(), text_payload(engine)
    bound = verifier.bound_for('example/high')
    assert verifier.verify_dispatch(engine.client, CHAT, payload, bound) is True
    assert verifier.verify_post(engine.client, CHAT, payload, bound) is True
    assert verifier.verify_post(engine.client, CHAT, payload, bound) is False
    assert verifier.verify_dispatch(engine.client, CHAT, payload, bound) is True
    verifier.revoke(bound.evidence.removeprefix('component-cap:'))
    assert verifier.verify_post(engine.client, CHAT, payload, bound) is False


def test_expiry_after_intent_is_a_durable_non_dispatch_not_a_free_provider_failure(tmp_path, monkeypatch):
    import os
    engine, api, schedules, reviews, clock, make, app = component_setup(tmp_path)
    controlled = app(make())
    real_fsync = os.fsync
    def expire(fd):
        real_fsync(fd)
        if list((engine.client.root / 'calls').glob('*/intent.json')):
            clock[0] = NOW + 301
    monkeypatch.setattr(os, 'fsync', expire)
    with pytest.raises(RuntimeError):
        controlled.run('expires-at-intent', TASK, ArithmeticEnvironment, contract_version='v1', mode='fixed_high')
    assert api.requests == []
    report = controlled.store.report()
    assert report['known_usd'] == '0' and report['unknown_events'] == 0
    assert report['events'][0]['verified'] == 1
    assert report['events'][0]['receipt'].startswith('not-dispatched:')


def test_provider_error_charge_is_retained_and_failed_run_is_not_resent(tmp_path):
    requests = []
    def failed(request):
        requests.append(request.method)
        return httpx.Response(500, json={'id': 'gen-paid-error', 'model': 'example/high',
            'error': {'message': 'synthetic failure'}, 'usage': {'cost': '0.012'}})
    engine, api, schedules, reviews, clock, make, app = component_setup(tmp_path, api=failed)
    controlled = app(make())
    with pytest.raises(RuntimeError):
        controlled.run('paid-error', TASK, ArithmeticEnvironment, contract_version='v1', mode='fixed_high')
    assert controlled.store.report()['known_usd'] == '0.012'
    with pytest.raises(RuntimeError):
        controlled.run('paid-error', TASK, ArithmeticEnvironment, contract_version='v1', mode='fixed_high')
    assert controlled.store.report()['reservations'][0]['status'] == 'active'
    assert requests == ['POST']


def test_interrupted_dispatch_stays_unknown_and_is_not_retried(tmp_path):
    requests = []
    def interrupted(request):
        requests.append(request.method)
        raise httpx.ReadTimeout('synthetic interrupted response')
    engine, api, schedules, reviews, clock, make, app = component_setup(tmp_path, api=interrupted)
    controlled = app(make())
    with pytest.raises(RuntimeError):
        controlled.run('interrupted', TASK, ArithmeticEnvironment, contract_version='v1', mode='fixed_high')
    assert controlled.store.report()['unknown_events'] == 1
    with pytest.raises(BudgetError):
        app(make()).run('interrupted', TASK, ArithmeticEnvironment, contract_version='v1', mode='fixed_high')
    assert requests == ['POST']


def test_actual_charge_above_component_bound_retains_shared_stop(tmp_path):
    def breach(request):
        return httpx.Response(500, json={'id': 'gen-over-component-bound', 'usage': {'cost': '0.02'}})
    engine, api, schedules, reviews, clock, make, app = component_setup(tmp_path, api=breach)
    controlled = app(make())
    with pytest.raises(BudgetError, match='violated'):
        controlled.run('breach', TASK, ArithmeticEnvironment, contract_version='v1', mode='fixed_high')
    report = controlled.store.report()
    assert report['violations'][0]['observed_cost'] == '0.02'
    assert report['dispatch_blocked'] is True and report['available_usd'] is None


def test_more_than_28_digits_survive_complete_workflow_reservation(tmp_path):
    engine, api, schedules, reviews, clock, make, app = component_setup(tmp_path)
    for schedule, review in zip(schedules, reviews, strict=True):
        for component in schedule['components'].values():
            component['unit_usd'] = '0'
        schedule['components']['input'] = {'max_units': 1, 'unit_usd': '0.010000000000000000000000000001'}
        schedule['components']['cache_read']['max_units'] = 1
        schedule['components']['cache_write']['max_units'] = 1
        review['evidence_sha256'] = digest(schedule)
    result = app(make()).run('long-decimal', TASK, ArithmeticEnvironment, contract_version='v1')
    # Four independently bounded calls, each with the literal fee above.
    assert result['budget']['shared']['reservations'][0]['amount'] == '0.040000000000000000000000000004'


@pytest.mark.parametrize('decision_zero_rate', [False, True])
def test_generation_reconciliation_replaces_provisional_fees_once_for_every_phase(tmp_path, decision_zero_rate):
    api, reads = API(first='3', choices=('mid', 'high')), []
    def transport(request):
        if request.method == 'POST':
            return api(request)
        gid = request.url.params['id']
        index = int(gid.rsplit('-', 1)[1]) - 1
        payload = api.requests[index]
        decision = payload['model'].startswith('typesafe/')
        reads.append(gid)
        return httpx.Response(200, json={'data': {'id': gid, 'total_cost': '0.00015' if decision else '0.01',
            'model': payload['model'], 'provider_name': 'TypeSafe' if decision else 'Example'}})
    engine, _, schedules, reviews, clock, make, app = component_setup(tmp_path, api=transport)
    if decision_zero_rate:
        for schedule, review in zip(schedules, reviews, strict=True):
            if schedule['endpoint'] == DECISIONS:
                qualify_zero_components(schedule, review, ['output', 'reasoning'])
    controlled = app(make())
    result = controlled.run('reconciled', TASK, ArithmeticEnvironment, contract_version='v1')
    assert result['budget']['run_total_usd'] == '0.0202'
    for directory in sorted((engine.client.root / 'calls').iterdir()):
        engine.client.reconcile(directory.name)
        engine.client.reconcile(directory.name)
    controlled.sync_client(engine.client)
    controlled.sync_client(engine.client)
    report = controlled.store.report()
    assert Decimal(report['verified_usd']) == Decimal('0.02030')
    assert report['provisional_hold_usd'] == '0'
    assert len(api.requests) == 4 and len(reads) == 4
    assert len(report['events']) == 4


def test_a_changed_price_artifact_requires_its_own_independent_review(tmp_path):
    engine, api, schedules, reviews, clock, make, app = component_setup(tmp_path)
    target = next(s for s in schedules if s['model_version'] == 'example/high')
    target['components']['input']['unit_usd'] = '0'
    with pytest.raises(ValueError, match='review'):
        make().bound_for('example/high')
    assert api.requests == []


def test_changing_registry_directory_cannot_revive_a_withdrawn_approval(tmp_path):
    engine, api, schedules, reviews, clock, make, app = component_setup(tmp_path)
    bound = make().bound_for('example/high')
    make().revoke(bound.evidence.removeprefix('component-cap:'))
    with pytest.raises(ValueError, match='review'):
        make(tmp_path / 'empty-new-registry').bound_for('example/high')
    assert api.requests == []


def test_recreated_empty_registry_at_same_path_does_not_inherit_old_reviews(tmp_path):
    engine, api, schedules, reviews, clock, make, app = component_setup(tmp_path)
    bound = make().bound_for('example/high')
    make().revoke(bound.evidence.removeprefix('component-cap:'))
    (tmp_path / 'fee-reviews').rename(tmp_path / 'preserved-withdrawals')
    with pytest.raises(ValueError, match='review'):
        make().bound_for('example/high')


def test_registry_read_cannot_extend_expired_lease(tmp_path, monkeypatch):
    from pathlib import Path
    engine, api, schedules, reviews, clock, make, app = component_setup(tmp_path)
    verifier, payload = make(), text_payload(engine)
    bound = verifier.bound_for('example/high')
    assert verifier.verify_dispatch(engine.client, CHAT, payload, bound) is True
    read_text = Path.read_text
    def slow_read(path, *args, **kwargs):
        text = read_text(path, *args, **kwargs)
        if path.name == 'registry.json':
            clock[0] = NOW + 301
        return text
    monkeypatch.setattr(Path, 'read_text', slow_read)
    assert verifier.verify_post(engine.client, CHAT, payload, bound) is False
    assert api.requests == []


def test_completed_concurrent_withdrawal_during_registry_read_blocks_post(tmp_path, monkeypatch):
    from pathlib import Path
    engine, api, schedules, reviews, clock, make, app = component_setup(tmp_path)
    verifier, withdrawing_operator, payload = make(), make(), text_payload(engine)
    bound = verifier.bound_for('example/high')
    assert verifier.verify_dispatch(engine.client, CHAT, payload, bound) is True
    read_text = Path.read_text
    def withdraw_during_read(path, *args, **kwargs):
        text = read_text(path, *args, **kwargs)
        if path.name == 'registry.json':
            withdrawing_operator.revoke(bound.evidence.removeprefix('component-cap:'))
        return text
    monkeypatch.setattr(Path, 'read_text', withdraw_during_read)
    assert verifier.verify_post(engine.client, CHAT, payload, bound) is False
    assert api.requests == []

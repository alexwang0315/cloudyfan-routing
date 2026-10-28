"""Proposed host advice configuration at the existing real HTTP seam."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path

import pytest

from task_features_fixture import task_features_http as task_features_http
from task_features_fixture import ArtifactRecorder, POLICY_BUNDLE, StoreFeatureBindingPort
from test_local_http_integration import local_service as local_service


RECOMMENDATION_POLICY = {
    'enabled': True,
    'rule_version': 'route-recommendation-rules-v1',
    'data_policy_version': 'creation-recommendation-data-v1',
    'bounds': {
        'pure_context_bytes': 65536,
        'candidate_summaries': 12,
        'conditional_variants': 128,
        'recommendation_bytes': 8192,
        'suggestions': 3,
        'reasons_per_suggestion': 4,
        'ascii_code_characters': 80,
        'new_envelope_bytes': 98304,
        'legacy_record_and_adoption_bytes': 16384,
        'shared_total_new_pure_compute_milliseconds': 50,
    },
}


@pytest.fixture
def task_recommendations_http(task_features_http):
    tracer = task_features_http
    # This original ordinary BackupCategoryCore contract is legal without exact
    # classifier variants. It is not the strict SelectorResearchCore constructor.
    assert tracer.controlled.host.values['plan']['qcs_policy']['stages'] == {}
    tracer.controlled.host.service.creation_recommendations = deepcopy(RECOMMENDATION_POLICY)
    tracer.evidence['recommendation_host_configuration'] = {
        'interface': 'PUBLIC TaskService.creation_recommendations',
        'literal': deepcopy(RECOMMENDATION_POLICY),
        'original_feature_configuration_unchanged': True,
        'calibration_input': 'original legal contract; default classifiers=(); no exact variants',
        'baseline_disclosure': (
            'At unmodified Core ba5401f5058e92e27d17a7331f2f1ac87aec27c3, the new property '
            'is stored as an unused Python attribute. It is not baseline product support.'),
    }
    return tracer


def activate(tracer):
    operator = tracer.client('operator')
    response = operator.get('/monitor/capacity')
    assert response.status_code == 200
    response = operator.post('/monitor/capacity/activate', json={
        'revision': response.json()['dispatch']['revision'], 'version': tracer.controlled.policy_version})
    assert response.status_code == 200
    return tracer.client('agent')


def enable_recommendations(tracer, **options):
    configuration = {**deepcopy(RECOMMENDATION_POLICY), **options}
    tracer.controlled.host.service.creation_recommendations = configuration
    tracer.evidence['current_recommendation_host_configuration'] = {
        'interface': 'PUBLIC TaskService.creation_recommendations', 'literal': configuration}


def task_payload(tracer, run='recommendation-case', prompt='中文1+1'):
    return {'run_id': run, 'category': tracer.controlled.category, 'prompt': prompt}


def calibrated_contracts(tracer, *, economic='insufficient', data_kind='offline_fixture',
                         source_fault=None, calibration_patch=None, before_enable=None, after_enable=None,
                         plan_patch=None):
    """Register a new legal synthetic original contract before accepting tasks.

    Both observed-schema and offline-schema examples here are invented test
    evidence. They are never actual observed calibration or savings evidence.
    """
    import capacity_resilience_fixture
    import candidate_health_fixture
    from cloudyfan_jev.scores.catalog import digest
    from cloudyfan_jev.scores.selector_research import VERSION, required_contracts
    from cloudyfan_jev.scores.backup_runtime import backup_pool_contract_hash
    from candidate_health_fixture import CONTRACT
    from _backup_fixture import prepare_backup_values
    values = prepare_backup_values(contract=CONTRACT, diagnostics=tracer.controlled.host.values['diagnostics'],
        count=1, classifiers=('low_mid', 'mid_high'))
    plan = values['plan']
    if plan_patch is not None:
        plan_patch(values)
        snapshot = plan['snapshot']
        for tier, rows in plan['pools'].items():
            if tier == 'open_source':
                snapshot['open_source_pool'] = [deepcopy(row['entry']) for row in rows]
            else:
                snapshot['pools'][tier] = [deepcopy(row['entry']) for row in rows]
        snapshot['snapshot_id'] = digest({key: value for key, value in snapshot.items() if key != 'snapshot_id'})
        plan['snapshot_id'] = snapshot['snapshot_id']
    if economic == 'fraction-edge':
        plan['qcs_policy']['minimum_saving_fraction'] = '.1'
    original_variants = [variant for stage in plan['qcs_policy']['stages'].values() for variant in stage['variants'].values()]
    for stage in plan['qcs_policy']['stages'].values():
        stage['variants'] = {}
    for key, needed in required_contracts(plan, values['decision']).items():
        variant = deepcopy(next(variant for variant in original_variants
            if variant['calibration']['stage'] == needed['stage'] and variant['calibration']['previous_tier'] == needed['previous_tier']))
        variant['calibration']['contract_sha256'] = key
        plan['qcs_policy']['stages'][needed['stage']]['variants'][key] = variant
    for stage in plan['qcs_policy']['stages'].values():
        for variant in stage['variants'].values():
            variant['calibration'].update(dataset_partition='independent_calibration', data_kind=data_kind,
                uses_evaluation_answers=False, sample_count=20, selector_policy_version=VERSION,
                pool_contract_sha256=backup_pool_contract_hash(plan),
                execution_contract_sha256=digest(plan['qcs_policy']['execution_contract']))
            if calibration_patch:
                variant['calibration'].update(calibration_patch)
            if economic == 'insufficient':
                variant['classifier_cost_usd'] = {'lower': '2', 'upper': '2'}
            elif economic == 'uncertain':
                variant['classifier_cost_usd'] = {'lower': '0', 'upper': '2'}
            elif economic == 'fraction-edge':
                variant['classifier_cost_usd'] = {'lower': '0', 'upper': '0'}
                for path in variant['paths'].values():
                    path['cost_usd'] = {'lower': '100', 'upper': '200'}
                for path in variant['classified_paths'].values():
                    path['cost_usd'] = {'lower': '185', 'upper': '185'}
    sources = tracer.artifacts / 'public-calibration'
    sources.mkdir(mode=0o700)
    source = sources / 'synthetic-calibration.json'
    if source_fault == 'union-last-growth':
        source = sources / '00-union-source.json'
    if source_fault == 'private':
        source = sources / 'private' / 'synthetic-calibration.json'
        source.parent.mkdir(mode=0o700)
    elif source_fault in ('upper-env', 'mixed-env'):
        source = sources / ('.ENV' if source_fault == 'upper-env' else '.Env.local') / 'synthetic-calibration.json'
        source.parent.mkdir(mode=0o700)
    elif source_fault == 'outside-root':
        source = tracer.artifacts / 'outside-calibration-root.json'
    evidence = {key: deepcopy(variant) for stage in plan['qcs_policy']['stages'].values()
                for key, variant in stage['variants'].items()}
    for variant in evidence.values():
        variant['calibration'].pop('raw_sha256', None)
        variant['calibration'].pop('source_files', None)
    source.write_text(json.dumps({'schema_version': 'synthetic-routing-calibration-v1',
        'not_actual_observations_or_savings': True, 'variants': evidence}, sort_keys=True) + '\n')
    source.chmod(0o600)
    if source_fault == 'union-last-growth':
        source.write_bytes(b'A' * 262144)
    sha = hashlib.sha256(source.read_bytes()).hexdigest()
    files = {str(source): sha}
    if source_fault == 'union-last-growth':
        for index, size in enumerate((262144, 262144, 262143, 1), start=1):
            extra = sources / (f'{index:02d}-union-source.json')
            extra.write_bytes(bytes([65 + index]) * size)
            files[str(extra)] = hashlib.sha256(extra.read_bytes()).hexdigest()
    if source_fault in ('too-many', 'total-bytes', 'two-sources'):
        for index in range(16 if source_fault == 'too-many' else 4 if source_fault == 'total-bytes' else 1):
            extra = sources / ('extra-source-' + str(index) + '.json')
            extra.write_bytes(source.read_bytes() if source_fault != 'total-bytes' else b'X' * 262144)
            files[str(extra)] = hashlib.sha256(extra.read_bytes()).hexdigest()
    for stage in plan['qcs_policy']['stages'].values():
        for variant in stage['variants'].values():
            variant['calibration'].update(raw_sha256=sha, source_files=files)
    first = next(iter(plan['qcs_policy']['stages']['low_mid']['variants'].values()))['calibration']
    if source_fault == 'missing-declaration':
        first['source_files'] = {}
    elif source_fault == 'raw-not-declared':
        for stage in plan['qcs_policy']['stages'].values():
            for variant in stage['variants'].values():
                variant['calibration']['source_files'] = {str(source): '0' * 64}
    elif source_fault == 'contradictory-sha':
        second = sources / 'consistent-second.json'
        second.write_bytes(source.read_bytes())
        first['source_files'] = {str(source): '0' * 64, str(second): sha}
    elif source_fault == 'missing-required-variant':
        variants = plan['qcs_policy']['stages']['mid_high']['variants']
        variants.pop(next(iter(variants)))
    values['module'].rebind(plan)
    plan['qcs_policy']['complete_policy_flow']['provenance']['raw_sha256'] = sha
    plan['backup_id'] = digest({key: value for key, value in plan.items() if key != 'backup_id'})
    # Only the synthetic fixture's data factory is replaced during assembly.
    # All original production constructors and guards run unchanged, so the
    # calibration is frozen naturally before QueuePreparation is constructed.
    tracer.controlled.close()
    def before_response(request):
        tracer.wire_recorder.capture({'method': request.method, 'url': str(request.url)},
            {'body': request.content, 'headers': b'\r\n'.join(k + b': ' + v for k, v in request.headers.raw)})
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(candidate_health_fixture, 'health_values', lambda: values)
        controlled = capacity_resilience_fixture.build_resilience_health_host(tracer.artifacts / 'calibrated-service',
            clock=tracer.clock, origin=tracer.origin, before_response=before_response, held_timeout_seconds=30)
    tracer.controlled = controlled
    tracer.stack.callback(controlled.close)
    tracer.server.set_app(controlled.app)
    service = controlled.host.service
    service.creation_features = {'policy_bundle': {**POLICY_BUNDLE, 'feature_version': 'local-deterministic-v2'},
                                 'binding_port': StoreFeatureBindingPort(tracer.controlled.app.store)}
    if source_fault in ('missing', 'symlink', 'fifo', 'oversized', 'empty'):
        source.rename(tracer.artifacts / ('preserved-' + source_fault + '-source.json'))
        if source_fault == 'symlink':
            source.symlink_to(tracer.artifacts / ('preserved-' + source_fault + '-source.json'))
        elif source_fault == 'fifo':
            os.mkfifo(source, 0o600)
        elif source_fault == 'oversized':
            source.write_bytes(b'X' * 262145)
        elif source_fault == 'empty':
            source.write_bytes(b'')
    elif source_fault == 'hash-mismatch':
        (tracer.artifacts / 'preserved-source-before-hash-mismatch.json').write_bytes(source.read_bytes())
        source.write_bytes(source.read_bytes() + b'changed')
    if before_enable:
        before_enable(source, values)
    enable_recommendations(tracer, calibration_sources={'data_kind': data_kind, 'public_root': str(sources)})
    if after_enable:
        after_enable(source, values)
    tracer.evidence['synthetic_calibration'] = {'actual_test_evidence': 'synthetic_only_not_observed_or_effect_evidence',
        'schema_branch': data_kind, 'original_source_sha256': sha,
        'source_bytes_at_setup': source.lstat().st_size if source.exists() or source.is_symlink() else None,
        'source_fault': source_fault,
        'exact_variants': len(evidence), 'economic_case': economic}
    return source, values


def reopen_calibrated_reader(tracer, values):
    import capacity_resilience_fixture
    import candidate_health_fixture
    root = tracer.controlled.host.root
    tracer.controlled.close()
    def before_response(request):
        tracer.wire_recorder.capture({'method': request.method, 'url': str(request.url)},
            {'body': request.content, 'headers': b'\r\n'.join(k + b': ' + v for k, v in request.headers.raw)})
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(candidate_health_fixture, 'health_values', lambda: values)
        controlled = capacity_resilience_fixture.build_resilience_health_host(root,
            clock=tracer.clock, origin=tracer.origin, before_response=before_response, held_timeout_seconds=30)
    tracer.controlled = controlled
    tracer.stack.callback(controlled.close)
    tracer.server.set_app(controlled.app)
    assert controlled.host.service.creation_features is None
    assert controlled.host.service.creation_recommendations is None
    tracer.evidence['actual_calibrated_reader_reopened'] = True


@pytest.fixture
def recommendation_local(local_service, request):
    local = local_service
    service, store = local.bundle.app.gateway.service, local.bundle.app.store
    local.bundle.upstream.fail_tiers = set()
    service.creation_features = {'policy_bundle': {**POLICY_BUNDLE, 'feature_version': 'local-deterministic-v2'},
                                 'binding_port': StoreFeatureBindingPort(store)}
    service.creation_recommendations = deepcopy(RECOMMENDATION_POLICY)
    destination = Path(os.environ.get('CLOUDYFAN_MONITOR_EVIDENCE_DIR', local.bundle.root / 'recommendation-evidence'))
    artifacts = destination / (request.node.name + '-artifacts')
    artifacts.mkdir(mode=0o700, parents=True)
    recorder, wire = ArtifactRecorder(artifacts), ArtifactRecorder(artifacts, 'upstream')
    local.recommendation_evidence = {'scope': 'original synchronous four-mode Core; controlled Queue/Health exercised separately',
                                     'http_responses': recorder.rows, 'upstream_wire': wire.rows}
    original_client = local.client
    def client(*args, **kwargs):
        result = original_client(*args, **kwargs)
        def record(response):
            response.read()
            recorder.capture({'method': response.request.method, 'path': response.request.url.path,
                'port': response.request.url.port, 'status': response.status_code, 'authorization_recorded': False},
                {'request_body': response.request.content, 'response_body': response.content})
        result.event_hooks['response'].append(record)
        return result
    local.client = client
    def upstream(request):
        wire.capture({'method': request.method, 'url': str(request.url)},
            {'body': request.content, 'headers': b'\r\n'.join(k + b': ' + v for k, v in request.headers.raw)})
    local.bundle.upstream.before_response = upstream
    try:
        yield local
    finally:
        local.bundle.upstream.before_response = None
        report = {'http': recorder.verify(), 'upstream': wire.verify()}
        local.recommendation_evidence['artifact_verification'] = report
        path = artifacts / 'fixture-receipt.json'
        path.write_text(json.dumps(local.recommendation_evidence, ensure_ascii=False, indent=2) + '\n')
        path.chmod(0o600)
        local.recommendation_evidence['receipt_sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
        assert all(row['status'] == 'verified' for row in report.values())


def build_four_mode_host(root, *, clock, origin, before_response=None, held_timeout_seconds=600,
                         mock_boot_id="qa40-wire-pair-fixed-boot", values=None,
                         classifier_reply="valid", contract_version=None):
    """Same persistent Backup/Health/Queue host for real desktop and restart QA.

    This fixture owns controlled signals and simulated upstreams only. All
    admission, identity, original fee/receipt, health and continuation decisions
    use the actual product stores and registered protocols. The host-only held
    response bound is separate from every product deadline and defaults to 600s.
    """
    import re
    from cloudyfan_jev.capacity import CapacityStore
    from cloudyfan_jev.capacity_dispatch import CapacityController
    from cloudyfan_jev.capacity_windows import CapacityWindows
    from cloudyfan_monitor.capacity import CapacityObserver
    from cloudyfan_monitor.integration import assemble_monitor
    from copy import deepcopy
    from dataclasses import asdict
    from datetime import datetime, timezone
    from decimal import Decimal
    from types import SimpleNamespace
    import threading
    from uuid import uuid4
    import httpx
    from cloudyfan_jev.budget import Authorization, BudgetedCore, BudgetStore, EnforcedBound
    from cloudyfan_jev.capacity_dispatch import CapacityRoute
    from cloudyfan_jev.capacity_queue import QueuePreparation
    from cloudyfan_jev.demo import ArithmeticEnvironment
    from cloudyfan_jev.engine import Limits
    from cloudyfan_jev.http_api import RegisteredContract, TaskService
    from cloudyfan_jev.identity import IdentityStore, IssuedCredential
    from cloudyfan_jev.client import Client
    from cloudyfan_jev.scheduler import QueueScheduler, QueueLimits
    from cloudyfan_jev.scores.backup_runtime import BackupCategoryCore
    from cloudyfan_jev.scores.health import HealthStore
    from cloudyfan_monitor.integration import ACCESS_POLICY, RunBoundCore
    from cloudyfan_monitor.pools import empty_view
    from cloudyfan_monitor.store import Store
    from candidate_health_fixture import CONTRACT, OPERATOR, HealthHost, health_values
    from capacity_sources_fixture import limit

    if type(held_timeout_seconds) not in (int, float) or not 0 < held_timeout_seconds <= 3600:
        raise ValueError('Synthetic held timeout must be finite, positive and at most 3600 seconds')
    root = Path(root).absolute()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not isinstance(mock_boot_id, str) or not re.fullmatch(r"[a-z0-9-]{1,64}", mock_boot_id):
        raise ValueError("Fixed synthetic response boot ID required")
    if classifier_reply not in ('valid', 'invalid-choice'):
        raise ValueError('Fixed synthetic classifier response required')
    values = health_values() if values is None else values
    contract_version = CONTRACT if contract_version is None else contract_version
    control_path = root / 'synthetic-resilience-host.json'
    plan = values['plan']
    store = Store(root / 'monitor.sqlite', clock=clock)
    identities = IdentityStore(root / 'identity.sqlite', clock=clock,
        pepper=b'synthetic-resilience-private-pepper-00000')
    if control_path.exists():
        control = json.loads(control_path.read_text())
        credentials = {name: IssuedCredential(**{**value, 'scopes': tuple(value['scopes'])})
            for name, value in control['credentials'].items()}
    else:
        credentials = {}
        for name in ('studio', 'agent'):
            identities.create_principal(name)
            credentials[name] = identities.issue(name, scopes={'tasks:read', 'tasks:write', 'models:read'},
                expires_at=clock() + 400 * 86400)
        control = {'credentials': {name: asdict(value) for name, value in credentials.items()},
            'configured_at': clock()}
    now = lambda: datetime.fromtimestamp(clock(), timezone.utc)
    health = HealthStore(root / 'health', policy=values['module'].HEALTH_POLICY, clock=now)
    host = HealthHost(None, None, None, health, credentials, root, (), values, clock)
    if not control_path.exists():
        for tier, rows in plan['pools'].items():
            for position in range(len(rows)):
                health.publish([host.observation(tier, position)])
    flags = {'hold': False, 'scenario': 'success'}
    held, released = threading.Event(), threading.Event()
    requests, side_requests, transports, factory_calls = [], {'source': [], 'accounting': []}, [], []
    boot = mock_boot_id  # External MockTransport input, frozen before any host/queue construction.
    def upstream(request, lane):
        payload = json.loads(request.content) if request.content else {}
        identifier = boot + '-' + str(len(requests) + 1)
        record = {'method': request.method, 'path': request.url.path, 'model': payload.get('model'),
            'lane': lane, 'wire_sha256': hashlib.sha256(request.content).hexdigest(), 'id': identifier}
        requests.append(record)
        (root / ('private-wire-' + identifier + '.bin')).write_bytes(request.content)
        record_path = root / ('private-request-' + identifier + '.json')
        record_path.write_text(json.dumps(record, sort_keys=True))
        record_path.chmod(0o600)
        host.calls.append({**record, 'payload': payload})
        if before_response is not None:
            before_response(request)
        if request.method != 'POST':
            side_requests['accounting'].append(record)
            return httpx.Response(503, json={'error': 'PRIVATE_RESILIENCE_ACCOUNTING_UNAVAILABLE'})
        if 'questions' in payload:
            stage = next(iter(payload['questions']))
            lower, upper = ('low', 'mid') if stage == 'low_mid' else ('mid', 'high')
            choice = upper if classifier_reply == 'valid' else 'unlisted-choice'
            return httpx.Response(200, json={'id': 'gen-' + identifier, 'provider': 'TypeSafe',
                'model': payload['model'], 'usage': {'cost': 0.001, 'input_tokens': 1, 'output_tokens': 1},
                'answers': {stage: {'type': 'choice', 'choice': choice,
                    'probabilities': {lower: .2, upper: .8}}}})
        scenario = flags['scenario']
        flags['scenario'] = 'success'
        if lane == 'primary' and flags['hold']:
            held.set()
            if not released.wait(held_timeout_seconds):
                raise httpx.ReadTimeout('Synthetic resilience completion gate timed out')
        if scenario in {'window', 'unknown', 'unknown_fee', 'availability'}:
            status = 503 if scenario == 'availability' else 429
            response = values['module'].response(payload, identifier, status=status,
                cost=None if scenario == 'unknown_fee' else '.01')
            body = response.json()
            body['error']['message'] = 'PRIVATE_RESILIENCE_PROVIDER_CANARY'
            return httpx.Response(status, json=body, headers={
                'Retry-After': 'invalid-time' if scenario == 'unknown' else '3600',
                'X-Private-Canary': 'PRIVATE_RESILIENCE_HEADER_CANARY'})
        return values['module'].response(payload, identifier, answer='bad' if scenario == 'quality' else '2')

    contracts, budgets, clients = {}, {}, {}
    for lane, name in (('primary', 'health-backup-v1'), ('disjoint', 'health-disjoint-v1')):
        http = httpx.Client(transport=httpx.MockTransport(lambda request, current=lane: upstream(request, current)),
            trust_env=False)
        client = Client(http, 'synthetic-resilience-key-' + lane, root / ('receipts-' + lane))
        transports.append(http)
        clients[lane] = client
        def factory(run_id, controlled, local):
            factory_calls.append({'run_id': run_id, 'receipt_root': str(controlled.root)})
            return BackupCategoryCore(controlled, plan, values['decision'], run_id=run_id, health_store=health,
                clock=now, limits=Limits(max_turns=1), local_backend=local, execution_policy=values['privacy'],
                diagnostic_contract=values['diagnostics'])
        template = factory('resilience-template', client, None)
        budget = BudgetStore(root / ('budget-' + lane + '.sqlite'), clock=now)
        if budget.report()['authorization'] is None:
            budget.authorize(Authorization('synthetic-resilience-' + lane, Decimal('10000'), '2099-01-01T00:00:00Z'))
        bounds = {row['entry']['profile']['model_version']: EnforcedBound('.5', 'MockTransport only')
            for rows in plan['pools'].values() for row in rows}
        bounds[values['decision']['version']] = EnforcedBound('.5', 'MockTransport only')
        runner = BudgetedCore(RunBoundCore(template, factory), budget, model_bounds=bounds,
            tool_bounds={}, verify_bound=lambda *args: True)
        contracts[name] = RegisteredContract(contract_version, runner, lambda workspace: ArithmeticEnvironment(),
            mode='cloudyfan' if lane == 'primary' else 'open_source_first')
        if lane == 'primary':
            contracts['health-fixed-high-v1'] = RegisteredContract(
                contract_version, runner, lambda workspace: ArithmeticEnvironment(), mode='fixed_high')
            contracts['health-low-first-v1'] = RegisteredContract(
                contract_version, runner, lambda workspace: ArithmeticEnvironment(), mode='low_first')
        budgets[lane] = budget
    service = TaskService(root / 'tasks', contracts=contracts, allowed_principals={'studio', 'agent'},
        principal_active=identities.is_active, retention_policy=ACCESS_POLICY, clock=clock)
    view = empty_view('synthetic_resilience_no_real_qualification', values['now'])
    for tier, rows in plan['pools'].items():
        for position, row in enumerate(rows):
            view['categories'][host.category]['pools'][tier][position] = {
                'position': position, 'status': 'mock_only', 'profile': deepcopy(row['entry']['profile']),
                'config_id': row['entry']['config_id']}
    active = store.save_pool(view)
    proposal = deepcopy(view)
    proposal['source_kind'] = 'synthetic_resilience_unregistered_proposal'
    inactive = store.save_pool(proposal)
    if not control_path.exists():
        store.change(revision=store.control()['revision'], routing=active, catalog=active)
        control_path.write_text(json.dumps(control, sort_keys=True))
        control_path.chmod(0o600)
    registry = {(active, host.category, mode): name for mode, name in (
        ('cloudyfan', 'health-backup-v1'), ('fixed_high', 'health-fixed-high-v1'),
        ('low_first', 'health-low-first-v1'), ('open_source_first', 'health-disjoint-v1'))}
    capacity = CapacityStore(root / 'capacity.sqlite', clock=clock)
    bindings, groups, rules = [], [], []
    for index in range(5):
        name = 'provider-' + str(index)
        scope = {'kind': 'account', 'ref': 'synthetic-resilience-account-' + str(index)}
        bindings.append({'id': name, 'provider': 'openrouter', 'account_ref': scope['ref'],
            'credential_ref': 'synthetic-resilience-provider-key-' + str(index),
            'endpoint_ref': 'synthetic-resilience-endpoint-' + str(index), 'groups': [name]})
        groups.append({'id': name, 'scope': scope, 'source': {'kind': 'controlled_import',
            'version': 'synthetic-resilience-source-v1'}, 'supplier_limits': [], 'local_limits': [
                limit('rpm', 1000, control['configured_at']),
                limit('tpm', None, control['configured_at'], status='not_applicable'),
                limit('concurrency', 2, control['configured_at'])]})
        rules.append({'binding': name, 'group': name, 'header': 'retry-after',
            'format': 'retry_after', 'constraint': 'requests'})
    configuration = {'schema_version': 1, 'version': 'synthetic-resilience-config-v1',
        'bindings': bindings, 'groups': groups}
    capacity.configure(configuration)
    def credential_active(principal, credential, scope, observed):
        return identities.is_active(principal) and any(row['id'] == credential and row['revoked_at'] is None
            and row['created_at'] <= observed < row['expires_at'] and scope in row['scopes']
            for row in identities.credentials(principal))
    windows = CapacityWindows(capacity, version='synthetic-resilience-windows-v1', rules=rules)
    controller = CapacityController(capacity, credential_active=credential_active, window_backend=windows)
    preparation = QueuePreparation('synthetic-resilience-preparation-v1', contracts=contracts,
        tools={name: [] for name in contracts})
    controller.queue_backend = preparation
    routes = []
    profiles = [row['entry']['profile'] for rows in plan['pools'].values() for row in rows] + [values['decision']]
    for lane, client in clients.items():
        for profile in profiles:
            classifier = profile is values['decision']
            binding = 'provider-4' if lane == 'disjoint' else 'provider-3' if classifier else profile['provider']
            routes.append(CapacityRoute(client, 'https://openrouter.ai/api/alpha/decisions' if classifier
                else 'https://openrouter.ai/api/v1/chat/completions', profile.get('model_version', profile.get('version')),
                binding, provider_name=profile['provider_name'],
                accepted_models=tuple(profile.get('accepted_models', ()))))
    for version, queued in (('synthetic-resilience-queued-v1', True), ('synthetic-resilience-queued-v2', True),
                            ('synthetic-resilience-immediate-v1', False)):
        controller.register(version, configuration_version=configuration['version'], routes=routes, max_seconds=3600,
            windows_version=windows.version, **({'queue_version': preparation.version} if queued else {}))
    observer = CapacityObserver(capacity, sources={})
    host.app = assemble_monitor(store, service, registry, identities, operator_token=OPERATOR, origin=origin,
        demo=True, capacity_observer=observer, capacity_controller=controller)
    queue = QueueScheduler(service, root / 'queue.sqlite', clock=clock, limits=QueueLimits(64, 4, 32, 1800),
        measurement_kind='synthetic', capacity_protocol=preparation)
    host.app = assemble_monitor(store, service, registry, identities, operator_token=OPERATOR, origin=origin,
        demo=True, capacity_observer=observer, capacity_controller=controller, queue=queue,
        ollama_models={'resilience-mrc': {'category': host.category, 'contract_id': 'health-backup-v1'}})
    from cloudyfan_monitor.openrouter_accounting import OpenRouterAccounting
    from cloudyfan_monitor.pools import OpenRouterPoolSource
    from test_sources import fixtures
    for kind in side_requests:
        def unexpected(request, source=kind):
            side_requests[source].append({'method': request.method, 'path': request.url.path})
            record_path = root / ('private-' + source + '-request-' + uuid4().hex + '.json')
            record_path.write_text(json.dumps(side_requests[source][-1], sort_keys=True))
            record_path.chmod(0o600)
            if before_response is not None:
                before_response(request)
            return httpx.Response(503, json={'error': 'PRIVATE_RESILIENCE_OBSERVATION_CANARY'})
        transports.append(httpx.Client(transport=httpx.MockTransport(unexpected), trust_env=False))
    host.app.source = OpenRouterPoolSource(transports[-2], fixtures()[2])
    host.app.gateway.billing.source = OpenRouterAccounting(transports[-1], api_key=uuid4().hex)
    host.service, host.budget, host.pools = service, budgets['primary'], (active, inactive)
    def close():
        released.set()
        for transport in transports:
            transport.close()
    return SimpleNamespace(host=host, app=host.app, controller=controller, windows=windows, queue=queue,
        flags=flags, held=held, released=released, requests=requests, side_requests=side_requests,
        factory_calls=factory_calls, held_timeout_seconds=held_timeout_seconds,
        clock=clock, budgets=budgets, credentials=credentials, operator_token=OPERATOR, category=host.category,
        policy_version='synthetic-resilience-queued-v1', close=close,
        registered_contracts=contracts, original_registry=registry, mock_boot_id=mock_boot_id)

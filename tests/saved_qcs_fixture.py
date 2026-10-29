"""Original QCS host over real HTTP; only upstreams use MockTransport."""
from contextlib import ExitStack
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import platform
import sqlite3
import threading
from types import SimpleNamespace

import httpx
import pytest

import cloudyfan_jev
import cloudyfan_monitor
from cloudyfan_jev.client import CHAT, DECISIONS, GENERATION
from cloudyfan_jev.scores.category_routing import prepare_category_selection
from cloudyfan_jev.scores.qcs import classifier_contract_hash, flow_contract_hash
from cloudyfan_monitor.demo import CATEGORY
from cloudyfan_monitor.integration import assemble_monitor, build_local_integration

from candidate_observation_fixture import assemble_candidate_host
from task_features_fixture import ArtifactRecorder
from test_category_host_integration import OPERATOR, assemble_category_host, category_values
from test_local_http_integration import Clock, QuietHandler, ThreadedServer


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _save(path, value):
    with path.open('x', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
    path.chmod(0o600)


def _qcs_variant_host(root, origin, clock, variant):
    """Prepare a real original policy, then supply only its synthetic upstream.

    All fee/calibration/contract changes happen before original Core execution.
    Its manifests, economic checkpoints and classifier receipts are untouched.
    """
    values = category_values(classify=True)
    policy, evidence = values['policy'], values['evidence']
    if variant == 'mid_high':
        policy['stages']['mid_high:low'].update(
            paths={'mid': {'cost_usd': {'lower': '.3', 'upper': '.3'}, 'flow_ms_upper': '100'},
                   'high': {'cost_usd': {'lower': '.2', 'upper': '.2'}, 'flow_ms_upper': '100'}},
            direct_tier='high', classifier_cost_usd={'lower': '.001', 'upper': '.001'},
            lower_probability={'lower': '.9', 'upper': '.9'}, classify_flow_ms_upper='110',
            classified_paths={'mid': {'cost_usd': {'lower': '.1', 'upper': '.1'}},
                              'high': {'cost_usd': {'lower': '.2', 'upper': '.2'}}},
            classified_calibration={**policy['calibration'], 'classifier_version': values['decision']['version'],
                'stage': 'mid_high', 'previous_tier': 'low', 'context': 'public_failure_or_unknown',
                'effective_probability_includes_fallback': True})
    if variant in ('scaled', 'scaled_direct'):
        def scale(value):
            if isinstance(value, dict):
                for key, child in value.items():
                    if key.endswith('cost_usd'):
                        value[key] = {bound: format(Decimal(amount) * Decimal('.000001'), 'f')
                                      for bound, amount in child.items()}
                    elif key == 'minimum_saving_usd':
                        value[key] = format(Decimal(child) * Decimal('.000001'), 'f')
                    else:
                        scale(child)
            elif isinstance(value, list):
                for child in value:
                    scale(child)
        scale(evidence)
        scale(policy)
        if variant == 'scaled_direct':
            policy['stages']['low_mid']['classify_flow_ms_upper'] = None
    # Reapply the original producer's binding sequence. Cost-bearing cards are
    # frozen before conditional classifier contracts, then whole-flow is bound.
    conditional = {key: forecast.pop('classified_calibration')
                   for key, forecast in policy['stages'].items() if 'classified_calibration' in forecast}
    snapshot = values['matrix']['categories'][values['category']]['snapshot']
    policy['complete_policy_flow']['contract_sha256'] = flow_contract_hash(
        snapshot, values['requirements'], evidence, policy)
    profiles = prepare_category_selection(values['matrix'], values['category'], values['requirements'],
        evidence, policy, now=values['now'])['qcs_plan']['profiles']
    for key, calibration in conditional.items():
        stage, previous = ('low_mid', 'open_source') if key == 'low_mid' else key.split(':')
        calibration['contract_sha256'] = classifier_contract_hash(profiles, values['decision']['version'], stage, previous)
        policy['stages'][key]['classified_calibration'] = calibration
    policy['complete_policy_flow']['contract_sha256'] = flow_contract_hash(
        snapshot, values['requirements'], evidence, policy)
    calls = []
    app, service, budget, credentials = assemble_category_host(root, values, calls, clock=clock, fail_first=True)
    app = assemble_monitor(app.store, service, app.gateway.registry, app.identity_store,
        operator_token=OPERATOR, origin=origin, demo=True)
    client = service.contracts['category-host-v1'].runner.core._template.client
    tiers = {profile['model_version']: tier for tier, profile in profiles.items()}

    def upstream(request):
        assert request.method == 'POST'
        payload = json.loads(request.content)
        calls.append(payload)
        if 'questions' in payload:
            stage = next(iter(payload['questions']))
            lower, upper = ('low', 'mid') if stage == 'low_mid' else ('mid', 'high')
            answer = {'type': 'choice', 'choice': lower, 'probabilities': {lower: .9, upper: .1}}
            if variant == 'parse_failure':
                answer['choice'] = 'outside-the-original-pair'
            elif variant == 'abstain':
                # The original binary question declares no explicit abstain
                # option; a missing distribution is its native abstention.
                answer.pop('probabilities')
            elif variant == 'tie':
                answer['probabilities'] = {lower: .5, upper: .5}
            literals = {'probability_exp_-64': '1e-64', 'probability_exp_-65': '1e-65',
                        'probability_exp_-1000': '1e-1000'}
            if variant in literals:
                # Preserve a JSON number through the real Decimal parser;
                # a Python float would underflow the safe overscale control.
                answer.update(choice=upper, probabilities={lower: '__numeric_probability__', upper: 1})
            response = {'id': 'gen-saved-qcs-variant-' + str(len(calls)),
                'model': payload['model'], 'provider': 'TypeSafe', 'usage': {
                    'cost': .000000001 if variant in ('scaled', 'scaled_direct') else .001,
                    'input_tokens': 1, 'output_tokens': 1}, 'answers': {stage: answer}}
            if variant in literals:
                raw = json.dumps(response)
                assert raw.count('"__numeric_probability__"') == 1
                return httpx.Response(200, content=raw.replace('"__numeric_probability__"', literals[variant]),
                                      headers={'Content-Type': 'application/json'})
            return httpx.Response(200, json=response)
        failed = tiers[payload['model']] == 'open_source' or variant == 'mid_high' and tiers[payload['model']] == 'low'
        return httpx.Response(200, json={'id': 'gen-saved-qcs-variant-' + str(len(calls)),
            'model': payload['model'], 'provider': 'Example',
            'usage': {'cost': '.00000001' if variant in ('scaled', 'scaled_direct') else '.01'},
            'choices': [{'message': {'role': 'assistant', 'content': 'bad' if failed else '2'}}]})

    original_http = client.http
    client.http = httpx.Client(transport=httpx.MockTransport(upstream))
    original_http.close()
    return SimpleNamespace(app=app, service=service, budget=budget, credentials=credentials,
        calls=calls, root=root, category=values['category'], operator_token=OPERATOR), client.http


@dataclass
class SavedQCSHTTP:
    host: object
    origin: str
    stack: ExitStack
    artifacts: Path
    evidence: dict
    recorder: ArtifactRecorder
    wire: ArtifactRecorder
    clients: dict = field(default_factory=dict)

    def client(self, principal='operator'):
        if principal not in self.clients:
            token = (self.host.operator_token if principal == 'operator'
                     else self.host.credentials[principal].token)

            def record(response):
                response.read()
                self.recorder.capture({'method': response.request.method,
                    'path': response.request.url.path, 'port': response.request.url.port,
                    'status': response.status_code, 'authorization_recorded': False},
                    {'request_body': response.request.content, 'response_body': response.content,
                     'headers': json.dumps(list(response.headers.multi_items())).encode()})

            self.clients[principal] = self.stack.enter_context(httpx.Client(
                base_url=self.origin, trust_env=False, timeout=10,
                headers={'Authorization': 'Bearer ' + token}, event_hooks={'response': [record]}))
        return self.clients[principal]

    def snapshot(self, name):
        budget = self.host.budget.report()
        value = {'model_requests': sum(row['url'] in (CHAT, DECISIONS) for row in self.wire.rows),
            'upstream_request_sha256': [row['body_sha256'] for row in self.wire.rows],
            'source_requests': [row['body_sha256'] for row in self.wire.rows
                                if row['url'] not in (CHAT, DECISIONS, GENERATION)],
            'accounting_requests': [row['body_sha256'] for row in self.wire.rows if row['url'] == GENERATION],
            'budget': {key: budget.get(key) for key in ('known_usd', 'reserved_usd', 'unknown_events')},
            'original_files_sha256': {str(path.relative_to(self.host.root)): _sha(path)
                for path in sorted(self.host.root.rglob('*')) if path.is_file()
                and not str(path.relative_to(self.host.root)).startswith('.saved-comparisons/')}}
        _save(self.artifacts / (name + '.json'), value)
        return value

    def original_run(self):
        return next((self.host.root / 'receipts/runs').iterdir())


@pytest.fixture
def saved_qcs_http(tmp_path, loopback_network, request):
    destination = os.environ.get('CLOUDYFAN_MONITOR_EVIDENCE_DIR')
    root = Path(destination) if destination else tmp_path / 'qcs-http-evidence'
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    artifacts = root / (request.node.name + '-artifacts')
    artifacts.mkdir(mode=0o700)
    recorder, wire = ArtifactRecorder(artifacts), ArtifactRecorder(artifacts, 'upstream')
    evidence = {'schema_version': 1, 'test': request.node.name,
        'scope': 'Real loopback HTTP/CategoryCore/BudgetedCore/TaskService/Store/Identity; upstreams MockTransport',
        'python': platform.python_version(), 'sqlite': sqlite3.sqlite_version,
        'import_roots': {'core': str(Path(cloudyfan_jev.__file__).resolve()),
                         'monitor': str(Path(cloudyfan_monitor.__file__).resolve())},
        'pytest_pid': os.getpid(), 'http_responses': recorder.rows, 'upstream_wire': wire.rows,
        'tools': 'Original registered arithmetic fixture has no executable tools',
        'source_accounting': 'Original MockTransport records model calls only; no source/accounting client is configured'}
    server = thread = tracer = None
    stack = ExitStack()
    try:
        assert Path(cloudyfan_monitor.__file__).resolve().is_relative_to(Path(__file__).resolve().parents[1] / 'src')
        if os.environ.get('CLOUDYFAN_CORE_ROOT'):
            assert Path(cloudyfan_jev.__file__).resolve().is_relative_to(Path(os.environ['CLOUDYFAN_CORE_ROOT']) / 'src')
        server = ThreadedServer(('127.0.0.1', 0), QuietHandler)
        stack.callback(server.server_close)
        loopback_network.ports.add(server.server_port)
        origin = f'http://127.0.0.1:{server.server_port}'
        options = getattr(request, 'param', False)
        if isinstance(options, dict) and options.get('family') == 'sequential':
            clock = Clock()
            bundle = build_local_integration(artifacts / 'service', origin=origin, clock=clock, seed=False)
            bundle.upstream.fail_tiers = set(options['fail_tiers'])
            service = bundle.app.gateway.service
            core = service.contracts['demo-cloudyfan'].runner.core
            host = SimpleNamespace(app=bundle.app, service=service,
                budget=service.contracts['demo-cloudyfan'].runner.store, credentials=bundle.credentials,
                calls=bundle.upstream.calls, root=bundle.root, category=CATEGORY,
                operator_token=bundle.operator_token)
            original_http = core.client.http
            evidence['scope'] = 'Real loopback HTTP/Core/BudgetedCore/TaskService/Store/Identity; upstreams MockTransport'
        elif isinstance(options, dict) and options.get('family') == 'qcs_controlled':
            clock = Clock(datetime.fromisoformat('2026-10-21T00:00:00+00:00').timestamp())
            host, original_http = _qcs_variant_host(artifacts / 'service', origin, clock, options['variant'])
            evidence['original_policy_variant'] = options['variant']
            evidence['variant_prepared_before_original_execution'] = True
        else:
            clock = Clock(datetime.fromisoformat('2026-10-21T00:00:00+00:00').timestamp())
            host = assemble_candidate_host(artifacts / 'service', origin, clock=clock,
                fail_first=True, classify=bool(options))
            original_http = host.service.contracts['category-host-v1'].runner.core._template.client.http

        def record_model(request):
            wire.capture({'method': request.method, 'url': str(request.url)},
                {'body': request.content,
                 'headers': b'\r\n'.join(key + b': ' + value for key, value in request.headers.raw)})

        original_http.event_hooks['request'].append(record_model)

        def close_fixture():
            original_http.close()
            evidence['fixture_close_returned'] = True

        stack.callback(close_fixture)
        server.set_app(host.app)
        tracer = SavedQCSHTTP(host, origin, stack, artifacts, evidence, recorder, wire)
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01},
                                  daemon=True, name='saved-qcs-loopback-http')
        thread.start()
        stack.callback(thread.join, 2)
        stack.callback(server.shutdown)
        yield tracer
    finally:
        try:
            stack.close()
        finally:
            evidence['cleanup'] = {'server_thread_exited': thread is None or not thread.is_alive(),
                'server_socket_closed': server is None or server.socket.fileno() == -1,
                'fixture_close_completed': evidence.get('fixture_close_returned', False)}
            evidence['allowed_loopback_ports'] = sorted(loopback_network.ports)
            evidence['socket_attempts'] = [{'host': address[0], 'port': address[1]}
                                           for address in loopback_network.connected]
            evidence['blocked_socket_attempts'] = [str(address) for address in loopback_network.blocked]
            evidence['mock_upstream_call_count'] = len(wire.rows)
            checks = {'http': recorder.verify(), 'upstream': wire.verify()}
            evidence['artifact_verification'] = {'status': 'verified' if all(
                value['status'] == 'verified' for value in checks.values()) else 'failed', 'recorders': checks}
            _save(artifacts / 'fixture-receipt.json', evidence)
            _save(root / (request.node.name + '.json'), {'schema_version': 1, 'test': request.node.name,
                'fixture_receipt': str((artifacts / 'fixture-receipt.json').relative_to(root)),
                'fixture_receipt_sha256': _sha(artifacts / 'fixture-receipt.json'),
                'mock_upstream_call_count': len(wire.rows), 'cleanup': evidence['cleanup'],
                'artifact_verification': evidence['artifact_verification']})
        assert thread is None or not thread.is_alive(), 'Owned QCS HTTP server thread did not exit'
        assert not loopback_network.blocked, 'QCS fixture escaped loopback allowlist'
        assert evidence['artifact_verification']['status'] == 'verified', 'QCS artifact references failed'

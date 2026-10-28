"""Observed original Backup host at the real main-service loopback boundary."""
from contextlib import ExitStack
from dataclasses import dataclass, replace
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import sqlite3
import threading

import httpx
import pytest

from cloudyfan_jev.demo import ArithmeticEnvironment
from cloudyfan_monitor.openrouter_accounting import OpenRouterAccounting
from cloudyfan_monitor.pools import OpenRouterPoolSource
from history_fixture import assemble_backup_host
from task_features_fixture import ArtifactRecorder
from test_local_http_integration import QuietHandler, ThreadedServer
from test_sources import fixtures


def _save(path, value):
    with path.open('x') as stream:
        json.dump(value, stream, indent=2)
        stream.write('\n')


@dataclass
class SavedBackupHTTP:
    host: object
    origin: str
    artifacts: Path
    stack: ExitStack
    evidence: dict
    wire: object
    validations: list
    tools: list
    side: dict
    clients: list

    def client(self, principal='operator'):
        token = self.host.operator_token if principal == 'operator' else self.host.credentials[principal].token
        client = self.stack.enter_context(httpx.Client(base_url=self.origin, trust_env=False, timeout=10,
            headers={'Authorization': 'Bearer ' + token}))
        self.clients.append(client)
        return client

    def create_original(self):
        client = self.client('studio')
        response = client.post('/v1/tasks', json={'run_id': 'saved-backup-original',
            'category': self.host.values['plan']['category_id'],
            'prompt': 'Return only 1+1. PRIVATE_SAVED_BACKUP_TASK_CANARY'})
        assert response.status_code == 200 and response.json()['status'] == 'passed'
        assert self.client().get('/monitor/snapshot').status_code == 200
        assert self.wire.rows and len(self.wire.rows) == len(self.host.requests)
        assert self.validations, 'Original public validator positive control was not observed'
        self.evidence['original_task_completed'] = True
        self.evidence['positive_control'] = {'actual_model_requests': len(self.host.requests),
            'actual_validation_calls': len(self.validations), 'actual_wire_rows': len(self.wire.rows)}
        return response

    def snapshot(self, name):
        root = self.host.root
        files = {}
        for source in sorted(root.rglob('*')):
            relative = source.relative_to(root)
            if relative.parts[0] == '.saved-comparisons' or source == self.host.app.store.path:
                continue
            if source.is_file():
                files[str(relative)] = hashlib.sha256(source.read_bytes()).hexdigest()
        tables = ('control', 'clients', 'bindings', 'tasks', 'pools', 'version_uses',
            'creation_feature_adoptions', 'creation_observation_control', 'creation_observation_absences',
            'billing_control', 'billing_entries', 'billing_task_policies', 'billing_waiting',
            'billing_attempts', 'billing_source_state', 'billing_supplier_evidence')
        with sqlite3.connect(self.host.app.store.path.as_uri() + '?mode=ro', uri=True, timeout=0) as db:
            db.execute('PRAGMA query_only=ON')
            original_tables = {table: hashlib.sha256(json.dumps(sorted(
                db.execute('SELECT * FROM ' + table).fetchall(), key=repr), sort_keys=True).encode()).hexdigest()
                for table in tables}
        value = {'original_files_sha256': files, 'original_monitor_tables': original_tables,
            'model_requests': list(self.host.requests),
            'model_calls': len(self.host.calls), 'wire_rows': len(self.wire.rows),
            'source_requests': list(self.side['source']), 'accounting_requests': list(self.side['accounting']),
            'validation_calls': list(self.validations), 'tool_calls': list(self.tools)}
        _save(self.artifacts / (name + '.json'), value)
        self.evidence[name] = value
        return value

    def compare(self):
        response = self.client().post('/monitor/comparisons', json={
            'policy_version': 'original-policy-reproduction-v1',
            'tasks': [{'client': 'studio', 'run_id': 'saved-backup-original'}]})
        assert response.status_code == 200
        assert 'PRIVATE_SAVED_BACKUP_TASK_CANARY' not in response.text
        assert str(self.host.root) not in response.text
        return response

    def verify_saved_availability_proof(self, report, *, expected_links):
        saved = self.host.root / '.saved-comparisons' / (report['comparison_id'] + '.inputs.json')
        body = saved.read_bytes()
        values = json.loads(body)[0]['values']['decisions']
        decisions, links = report['items'][0]['reproduction']['decisions'], []
        for index, value in enumerate(values):
            proof = value['source'].get('availability_exhaustion_input_sha256')
            if proof is None:
                continue
            assert index > 0
            previous = values[index-1]
            canonical = json.dumps(previous, sort_keys=True, ensure_ascii=False,
                allow_nan=False, separators=(',', ':')).encode('utf-8')
            assert hashlib.sha256(canonical).hexdigest() == proof
            assert previous['context']['stage'] == 'same_pool'
            assert decisions[index-1]['action'] == 'advance_quality_stage' and decisions[index-1]['match'] is True
            assert value['context']['stage'] in ('low_mid', 'mid_high', 'after_validation')
            assert value['context']['previous_stop'] in ('provider_unavailable', 'provider_rate_limited')
            for key in ('previous_stop', 'previous_public_passed', 'generation_calls', 'quality_phase',
                        'previous_configuration_sha256', 'receipt_sha256', 'validation_sha256'):
                assert previous['context'][key] == value['context'][key]
            links.append({'index': index, 'prior_index': index-1, 'prior_input_sha256': proof,
                'actual_previous_stop': value['context']['previous_stop']})
        assert len(links) == expected_links
        assert all('availability_exhaustion_input_sha256' not in value['source'] for value in values
            if value['context']['previous_public_passed'] is True)
        result = {'inputs_sha256': hashlib.sha256(body).hexdigest(), 'links': links,
            'scope': 'Read actual saved DTOs; verify canonical predecessor digest without rerunning policy'}
        _save(self.artifacts / 'availability-proof-readback.json', result)
        self.evidence['availability_proof_readback'] = result

    def remove_or_corrupt_original_evidence(self, fault):
        internal = self.host.service.internal_id('studio', 'saved-backup-original')
        root = self.host.root / 'receipts'
        if fault in ('missing_classifier', 'request_hash', 'classifier_choice_mismatch',
                     'classifier_duplicate_key', 'classifier_model', 'classifier_usage',
                     'classifier_probability_mismatch'):
            name = 'intent.json' if fault == 'request_hash' else 'response.json'
            target = root / 'calls' / ('run-' + internal + '-decision-low_mid') / name
        else:
            assert fault in ('missing_gate', 'missing_initial_gate', 'missing_exhaustion_gate')
            index = 0 if fault == 'missing_initial_gate' else 1 if fault == 'missing_exhaustion_gate' else 2
            target = root / 'runs' / internal / f'backup-selection-{index}.json'
        original = target.read_bytes()
        archive = self.artifacts / ('original-before-' + fault + '.bin')
        if fault == 'missing_classifier':
            # Produce a genuine missing-file read while preserving the original;
            # this does not delete any artifact or fabricate an HTTP response.
            target.rename(archive)
            assert not target.exists()
        else:
            with archive.open('xb') as stream:
                stream.write(original)
            archive.chmod(0o600)
            value = json.loads(original)
            if fault == 'request_hash':
                value['request_sha256'] = '0' * 64
            elif fault == 'classifier_choice_mismatch':
                raw = json.loads(value['raw'])
                answer = raw['answers']['low_mid']
                answer['choice'], answer['probabilities'] = 'mid', {'low': .1, 'mid': .9}
                value['raw'] = json.dumps(raw)
            elif fault == 'classifier_probability_mismatch':
                raw = json.loads(value['raw'])
                answer = raw['answers']['low_mid']
                assert answer['choice'] == 'mid' and answer['probabilities'] == {'low': 1e-64, 'mid': 1}
                answer['probabilities']['low'] = 1e-63
                value['raw'] = json.dumps(raw)
            elif fault in ('classifier_duplicate_key', 'classifier_model', 'classifier_usage'):
                raw = json.loads(value['raw'])
                if fault == 'classifier_model':
                    raw['model'] = 'synthetic/unbound-classifier'
                elif fault == 'classifier_usage':
                    raw['usage']['cost'] = .002
                encoded = json.dumps(raw)
                if fault == 'classifier_duplicate_key':
                    assert encoded.count('"choice": "low"') == 1
                    encoded = encoded.replace('"choice": "low"', '"choice": "low", "choice": "low"', 1)
                value['raw'] = encoded
            else:
                tier = 'open_source' if fault in ('missing_initial_gate', 'missing_exhaustion_gate') else 'mid'
                value['health'][tier][0]['assessment'].pop('snapshot_id')
            target.write_text(json.dumps(value))
        info = {'fault': fault, 'target_relative': str(target.relative_to(self.host.root)),
            'original_reference': archive.name, 'original_sha256': hashlib.sha256(original).hexdigest(),
            'original_bytes': len(original), 'removed_from_original_path': not target.exists(),
            'changed_sha256': hashlib.sha256(target.read_bytes()).hexdigest() if target.exists() else None,
            'scope': 'Owned fresh synthetic evidence only; original bytes preserved before the controlled fault'}
        self.evidence['fault'] = info
        _save(self.artifacts / 'controlled-fault.json', info)
        return info


@pytest.fixture
def saved_backup_http(tmp_path, loopback_network, request):
    destination = Path(os.environ['CLOUDYFAN_MONITOR_EVIDENCE_DIR']) if os.environ.get(
        'CLOUDYFAN_MONITOR_EVIDENCE_DIR') else tmp_path / 'evidence'
    destination.mkdir(parents=True, mode=0o700, exist_ok=True)
    artifacts = destination / (request.node.name + '-artifacts')
    artifacts.mkdir(mode=0o700)
    recorder, wire = ArtifactRecorder(artifacts), ArtifactRecorder(artifacts, 'upstream')
    options = dict(getattr(request, 'param', {}))
    scenario = options.pop('scenario', 'direct')
    exhaustion = options.pop('availability_exhaustion', False)
    classifier_reply = options.pop('classifier_reply', None)
    evidence = {'schema_version': 'saved-backup-http-fixture-v1', 'test': request.node.name,
        'scope': 'Real Backup/Core/BudgetedCore/TaskService admission/Store/Identity/Health; MockTransport upstreams',
        'scheduler': 'Original immediate TaskService admission; no QueueScheduler configured',
        'synthetic': True, 'python': platform.python_version(), 'sqlite': sqlite3.sqlite_version,
        'http_responses': recorder.rows, 'upstream_wire': wire.rows, 'fixture_pid': os.getpid()}
    validations, tools, side, clients = [], [], {'source': [], 'accounting': []}, []
    server = host = thread = tracer = None
    stack = ExitStack()
    try:
        import cloudyfan_jev
        import cloudyfan_monitor
        core = Path(os.environ['CLOUDYFAN_CORE_ROOT']).resolve()
        monitor = Path(__file__).resolve().parents[1]
        assert platform.python_version() == '3.13.13' and sqlite3.sqlite_version == '3.50.4'
        assert Path(cloudyfan_jev.__file__).resolve().is_relative_to(core / 'src')
        assert Path(cloudyfan_monitor.__file__).resolve().is_relative_to(monitor / 'src')
        evidence['imports'] = {'core': str(Path(cloudyfan_jev.__file__).resolve()),
            'monitor': str(Path(cloudyfan_monitor.__file__).resolve())}
        server = ThreadedServer(('127.0.0.1', 0), QuietHandler)
        loopback_network.ports.add(server.server_port)
        origin = f'http://127.0.0.1:{server.server_port}'
        host = assemble_backup_host(artifacts / 'service', origin, **options)
        host.scenario = scenario
        if exhaustion:
            blocked = host.values['plan']['pools']['open_source'][1]
            observation = host.values['module'].observation(now=host.values['now'],
                provider=blocked['entry']['profile']['provider'], configuration=blocked['configuration_sha256'])
            observation['health']['status'] = 1
            snapshot = host.health.publish([observation])
            evidence['original_health_control'] = {'blocked_configuration_sha256': blocked['configuration_sha256'],
                'snapshot_id': snapshot, 'status': 1, 'scope': 'Original HealthStore publish before original task'}

        def record_wire(wire_request, response):
            wire.capture({'method': wire_request.method, 'path': wire_request.url.path,
                'status': response.status_code}, {'body': wire_request.content,
                'response_body': response.content, 'headers': json.dumps({
                    'request': [(key.decode('latin1'), value.decode('latin1'))
                        for key, value in wire_request.headers.raw],
                    'response': [(key.decode('latin1'), value.decode('latin1'))
                        for key, value in response.headers.raw]}).encode()})

        original_client = host.service.contracts['history-backup-v1'].runner.core.client.http
        assert isinstance(original_client._transport, httpx.MockTransport)
        original_upstream = original_client._transport.handler

        def upstream(wire_request):
            response = original_upstream(wire_request)
            if classifier_reply is not None and wire_request.url.path == '/api/alpha/decisions':
                original = response.content
                data = json.loads(original)
                stage = next(iter(data['answers']))
                answer = data['answers'][stage]
                if classifier_reply == 'probability_strings':
                    answer['probabilities'] = {key: str(value) for key, value in answer['probabilities'].items()}
                elif classifier_reply == 'confidence_string':
                    answer['confidence'] = '0.9'
                elif classifier_reply == 'confidence_bool':
                    answer['confidence'] = True
                elif classifier_reply == 'probability_bools':
                    assert stage == 'low_mid'
                    answer['probabilities'] = {'low': True, 'mid': False}
                elif classifier_reply == 'usage_cost_string':
                    data['usage']['cost'] = str(data['usage']['cost'])
                elif classifier_reply == 'distribution_missing':
                    answer.pop('probabilities')
                elif classifier_reply == 'tied_distribution':
                    answer['probabilities'] = {key: .5 for key in answer['probabilities']}
                elif classifier_reply == 'bounded_tiny_probability':
                    assert stage == 'low_mid'
                    answer['probabilities'] = {'low': 1e-65, 'mid': 1}
                elif classifier_reply == 'minimum_exponent_probability':
                    assert stage == 'low_mid'
                    answer['choice'], answer['probabilities'] = 'mid', {'low': 1e-64, 'mid': 1}
                else:
                    assert classifier_reply == 'numeric_confidence'
                    answer['confidence'] = .9
                archive = artifacts / 'producer-reply-before-control.bin'
                with archive.open('xb') as stream:
                    stream.write(original)
                archive.chmod(0o600)
                response = httpx.Response(response.status_code, json=data,
                    headers=[(key, value) for key, value in response.headers.raw
                        if key.lower() != b'content-length'])
                evidence['classifier_reply_control'] = {'kind': classifier_reply,
                    'timing': 'Original MockTransport reply before original Client/Core parsing and persistence',
                    'original_reference': archive.name, 'original_bytes': len(original),
                    'original_sha256': hashlib.sha256(original).hexdigest(),
                    'actual_reply_sha256': hashlib.sha256(response.content).hexdigest()}
            record_wire(wire_request, response)
            return response

        original_client._transport.handler = upstream
        clients.append(original_client)

        class ObservedArithmetic(ArithmeticEnvironment):
            def validate(self, final_text):
                result = super().validate(final_text)
                validations.append({'passed': result['passed'], 'checks': len(result['checks'])})
                return result

            def call(self, name, arguments):
                tools.append({'called': True})
                return super().call(name, arguments)

        contract = host.service.contracts['history-backup-v1']
        host.service.contracts['history-backup-v1'] = replace(contract,
            environment_factory=lambda workspace: ObservedArithmetic())
        stack.callback(contract.runner.core.client.http.close)
        for kind in side:
            def unexpected(wire_request, current=kind):
                side[current].append({'method': wire_request.method, 'path': wire_request.url.path})
                response = httpx.Response(503, json={'error': 'PRIVATE_SAVED_BACKUP_SIDE_CANARY'})
                record_wire(wire_request, response)
                return response
            transport = stack.enter_context(httpx.Client(transport=httpx.MockTransport(unexpected), trust_env=False))
            clients.append(transport)
            if kind == 'source':
                host.app.source = OpenRouterPoolSource(transport, fixtures()[2])
            else:
                host.app.gateway.billing.source = OpenRouterAccounting(transport, api_key='synthetic-backup-only')

        def app(environ, start_response):
            length = int(environ.get('CONTENT_LENGTH') or 0)
            assert 0 <= length <= 131072
            body = environ['wsgi.input'].read(length) if length else b''
            copied = dict(environ)
            copied['wsgi.input'] = io.BytesIO(body)
            metadata = {'method': environ.get('REQUEST_METHOD'), 'path': environ.get('PATH_INFO')}
            headers = b''

            def started(status, response_headers, exc_info=None):
                nonlocal headers
                metadata['status'] = int(status.split()[0])
                headers = json.dumps(response_headers).encode()
                return start_response(status, response_headers, exc_info)

            iterator = host.app(copied, started)
            try:
                chunks = list(iterator)
            finally:
                if hasattr(iterator, 'close'):
                    iterator.close()
            recorder.capture(metadata, {'request_body': body, 'response_body': b''.join(chunks), 'headers': headers})
            return chunks

        server.set_app(app)
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
        thread.start()
        tracer = SavedBackupHTTP(host, origin, artifacts, stack, evidence, wire, validations, tools, side, clients)
        yield tracer
    finally:
        if thread is not None and thread.is_alive():
            server.shutdown()
            thread.join(2)
        if server is not None:
            server.server_close()
        stack.close()
        if tracer is not None:
            final = tracer.snapshot('after-fixture')
            if 'before-comparison' in evidence:
                evidence['original_state_unchanged'] = final == evidence['before-comparison']
        evidence['artifact_verification'] = {'http': recorder.verify(), 'upstream': wire.verify()}
        if 'fault' in evidence:
            fault = evidence['fault']
            body = (artifacts / fault['original_reference']).read_bytes()
            assert len(body) == fault['original_bytes'] and hashlib.sha256(body).hexdigest() == fault['original_sha256']
            evidence['fault_original_bytes_preserved'] = True
        evidence['blocked_socket_attempts'] = [str(address) for address in loopback_network.blocked]
        evidence['socket_attempts'] = [{'host': address[0], 'port': address[1]} for address in loopback_network.connected]
        evidence['observed_calls'] = {'models': len(host.requests) if host is not None else None,
            'source': len(side['source']), 'accounting': len(side['accounting']),
            'validation': len(validations), 'tools': len(tools)}
        evidence['cleanup'] = {'thread_exited': thread is None or not thread.is_alive(),
            'socket_closed': server is None or server.socket.fileno() == -1,
            'owned_clients_closed': all(client.is_closed for client in clients)}
        _save(artifacts / 'fixture-receipt.json', evidence)
        assert all(evidence['cleanup'].values())
        assert evidence.get('original_state_unchanged', True)
        assert not loopback_network.blocked
        assert all(row['status'] == 'verified' for row in evidence['artifact_verification'].values())

"""Observed original capacity-window host at the real main-service HTTP seam."""
from contextlib import ExitStack
from dataclasses import dataclass, replace
from datetime import datetime
import hashlib
import importlib.util
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
from task_features_fixture import ArtifactRecorder
from test_local_http_integration import Clock, QuietHandler, ThreadedServer


def _save(path, value):
    with path.open('x') as stream:
        json.dump(value, stream, indent=2)
        stream.write('\n')
    path.chmod(0o600)


@dataclass
class SavedWindowHTTP:
    fixture: object
    artifacts: Path
    operator: object
    client: object
    agent: object
    evidence: dict
    validations: list
    tools: list
    wire: object

    def activate(self):
        response = self.operator.post('/monitor/capacity/activate', json={
            'revision': 0, 'version': 'synthetic-window-admission-v1'})
        assert response.status_code == 200

    def submit(self, run, *, scenario='success', principal='studio'):
        assert principal in ('studio', 'agent')
        self.fixture.flags['scenario'] = scenario
        client = self.client if principal == 'studio' else self.agent
        response = client.post('/v1/tasks', json={'run_id': run,
            'category': self.fixture.host.category, 'prompt': 'Return only 1+1. PRIVATE_SAVED_WINDOW_TASK'})
        assert response.status_code == 200 and response.json()['status'] == 'passed'
        assert self.wire.rows and self.validations
        self.evidence.setdefault('completed_original_runs', []).append(run)
        self.evidence.setdefault('completed_original_tasks', []).append({'client': principal, 'run_id': run})
        return response

    def history(self, run, *, principal='studio'):
        response = self.operator.get('/monitor/snapshot')
        assert response.status_code == 200
        task = next(row for row in response.json()['tasks'] if row['run'] == run and row['client'] == principal)
        return task, task['operator_observation']['task_decision']

    def compare(self, runs):
        return self.compare_tasks([{'client': 'studio', 'run_id': run} for run in runs])

    def compare_tasks(self, tasks):
        response = self.operator.post('/monitor/comparisons', json={
            'policy_version': 'original-policy-reproduction-v1',
            'tasks': tasks})
        assert response.status_code == 200
        assert 'PRIVATE_' not in response.text and str(self.fixture.host.root) not in response.text
        return response

    def read_twice(self, response):
        comparison_id = response.json()['comparison_id']
        for _ in range(2):
            read = self.operator.get('/monitor/comparisons/' + comparison_id)
            assert read.status_code == 200 and read.content == response.content

    def preserve_missing_job(self, run, *, principal='studio'):
        host = self.fixture.host
        internal = host.service.internal_id(principal, run)
        target = host.service.root / internal / 'job.json'
        body = target.read_bytes()
        archive = self.artifacts / ('preserved-job-' + principal + '-' + run + '.json')
        target.rename(archive)
        archive.chmod(0o600)
        info = {'client': principal, 'run': run, 'target_relative': str(target.relative_to(host.root)),
            'archive': archive.name, 'bytes': len(body), 'sha256': hashlib.sha256(body).hexdigest(),
            'scope': 'Owned fresh original evidence fault; job bytes preserved, no deletion'}
        self.evidence.setdefault('controlled_faults', []).append(info)
        _save(self.artifacts / ('missing-job-' + principal + '-' + run + '.json'), info)
        return info

    def preserve_unavailable_source_raw(self, run, *, principal='studio'):
        host = self.fixture.host
        internal = host.service.internal_id(principal, run)
        root = host.root / 'receipts'
        result_path = root / 'runs' / internal / 'result.json'
        original = json.loads(result_path.read_bytes())
        preserved = [self.preserve_missing_job(run, principal=principal)]
        targets = [(root / 'runs' / internal / 'manifest.json', 'manifest'), (result_path, 'result')]
        targets.extend((root / 'calls' / call['call_id'] / 'response.json', 'response')
            for attempt in original['attempts'] for call in attempt['calls'])
        for index, (target, kind) in enumerate(targets):
            body = target.read_bytes()
            archive = self.artifacts / f'preserved-source-{principal}-{run}-{index}.bin'
            target.rename(archive)
            archive.chmod(0o600)
            info = {'client': principal, 'run': run, 'kind': kind, 'target_relative': str(target.relative_to(host.root)),
                'archive': archive.name, 'bytes': len(body), 'sha256': hashlib.sha256(body).hexdigest(),
                'scope': 'Owned source raw unavailable before comparison; original bytes preserved, no deletion'}
            self.evidence.setdefault('controlled_faults', []).append(info)
            preserved.append(info)
        _save(self.artifacts / ('unavailable-source-' + principal + '-' + run + '.json'), preserved)
        return [row['sha256'] for row in preserved if row.get('kind') != 'manifest']

    def move_to_source_access_expiry(self, source, target, *, source_principal='studio', target_principal='studio'):
        service = self.fixture.host.service
        with sqlite3.connect(service.retention.path.as_uri() + '?mode=ro', uri=True, timeout=0) as db:
            db.execute('PRAGMA query_only=ON')
            source_expiry = db.execute('SELECT expires FROM tasks WHERE id=?',
                (service.internal_id(source_principal, source),)).fetchone()[0]
            target_expiry = db.execute('SELECT expires FROM tasks WHERE id=?',
                (service.internal_id(target_principal, target),)).fetchone()[0]
        self.fixture.clock.now = source_expiry + .5
        assert source_expiry < self.fixture.clock() < target_expiry
        value = {'source_expires': source_expiry, 'target_expires': target_expiry,
            'source_task': {'client': source_principal, 'run_id': source},
            'target_task': {'client': target_principal, 'run_id': target},
            'comparison_clock': self.fixture.clock(), 'scope': 'Use original retention deadlines, no policy/row changes'}
        _save(self.artifacts / 'source-expiry-clock.json', value)
        self.evidence['source_expiry_clock'] = value

    def deactivate_source_principal(self, principal):
        identity = self.fixture.host.app.identity_store
        identity.set_active(principal, False)
        with sqlite3.connect(identity.path.as_uri() + '?mode=ro', uri=True, timeout=0) as db:
            db.execute('PRAGMA query_only=ON')
            assert db.execute('SELECT active FROM principals WHERE id=?', (principal,)).fetchone()[0] == 0
        value = {'principal': principal, 'active': False, 'scope': 'Actual original IdentityStore.set_active before comparison'}
        _save(self.artifacts / 'source-principal-deactivation.json', value)
        self.evidence['source_principal_deactivation'] = value

    def corrupt_recorded_window(self, run, fault):
        assert fault in ('missing_ref', 'oversized_revision')
        host = self.fixture.host
        internal = host.service.internal_id('studio', run)
        target = host.root / 'receipts' / 'runs' / internal / 'backup-selection-0.json'
        original = target.read_bytes()
        archive = self.artifacts / ('preserved-window-' + run + '-' + fault + '.bin')
        with archive.open('xb') as stream:
            stream.write(original)
        archive.chmod(0o600)
        value = json.loads(original)
        window = value['health']['open_source'][0]['capacity_window']
        if fault == 'missing_ref':
            window['evidence_refs'] = []
        else:
            window['revision'] = 2**63
        target.write_text(json.dumps(value))
        info = {'run': run, 'fault': fault, 'target_relative': str(target.relative_to(host.root)),
            'archive': archive.name, 'bytes': len(original), 'sha256': hashlib.sha256(original).hexdigest(),
            'changed_sha256': hashlib.sha256(target.read_bytes()).hexdigest(),
            'scope': 'Controlled corruption of owned saved gate only; original bytes preserved before comparison'}
        self.evidence.setdefault('controlled_faults', []).append(info)
        _save(self.artifacts / ('window-fault-' + fault + '.json'), info)

    def corrupt_window_physical(self):
        host, target = self.fixture.host, self.fixture.controller.store.path
        before = {str(p.relative_to(host.root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in host.root.rglob('*') if p.is_file()}
        original = target.read_bytes()
        archive = self.artifacts / 'preserved-capacity-wrong-physical.sqlite'
        with archive.open('xb') as stream:
            stream.write(original)
        archive.chmod(0o600)
        with sqlite3.connect(target.as_uri() + '?mode=rw', uri=True, timeout=5) as db:
            row = db.execute('SELECT id,payload FROM capacity_window_observations ORDER BY rowid LIMIT 1').fetchone()
            assert row is not None
            value = json.loads(row[1])
            value['physical'] = '0' * 64
            db.execute('UPDATE capacity_window_observations SET payload=? WHERE id=?', (json.dumps(value), row[0]))
        after = {str(p.relative_to(host.root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in host.root.rglob('*') if p.is_file()}
        relative = str(target.relative_to(host.root))
        assert before.keys() == after.keys() and all(before[name] == after[name] for name in before if name != relative)
        info = {'fault': 'wrong_physical', 'target_relative': relative, 'archive': archive.name,
            'bytes': len(original), 'sha256': hashlib.sha256(original).hexdigest(),
            'changed_sha256': hashlib.sha256(target.read_bytes()).hexdigest(),
            'scope': 'Owned native row physical payload changed before comparison; all other original files/witnesses unchanged'}
        self.evidence.setdefault('controlled_faults', []).append(info)
        _save(self.artifacts / 'window-fault-wrong-physical.json', info)

    def drift_source_response_bytes(self, run):
        host = self.fixture.host
        internal = host.service.internal_id('studio', run)
        root = host.root / 'receipts'
        result = json.loads((root / 'runs' / internal / 'result.json').read_bytes())
        cid = result['attempts'][0]['calls'][0]['call_id']
        target = root / 'calls' / cid / 'response.json'
        original = target.read_bytes()
        value = json.loads(original)
        assert value['http_status'] == 429
        changed = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()
        if changed == original:
            changed = json.dumps(value, sort_keys=True, ensure_ascii=False, indent=2).encode()
        assert changed != original and json.loads(changed) == value
        archive = self.artifacts / 'preserved-source-response-before-sha-drift.bin'
        with archive.open('xb') as stream:
            stream.write(original)
        archive.chmod(0o600)
        target.write_bytes(changed)
        info = {'fault': 'response_sha_drift', 'target_relative': str(target.relative_to(host.root)),
            'archive': archive.name, 'bytes': len(original), 'sha256': hashlib.sha256(original).hexdigest(),
            'changed_sha256': hashlib.sha256(changed).hexdigest(), 'json_fields_unchanged': True,
            'scope': 'Only owned original response file encoding changed; saved ref IDs, fields, fees and actual result untouched'}
        self.evidence.setdefault('controlled_faults', []).append(info)
        _save(self.artifacts / 'window-fault-response-sha-drift.json', info)

    def snapshot(self, name):
        host = self.fixture.host
        files = {}
        for source in sorted(host.root.rglob('*')):
            relative = source.relative_to(host.root)
            if relative.parts[0] == '.saved-comparisons' or source == host.app.store.path:
                continue
            if source.is_file():
                files[str(relative)] = hashlib.sha256(source.read_bytes()).hexdigest()
        with sqlite3.connect(host.app.store.path.as_uri() + '?mode=ro', uri=True, timeout=0) as db:
            db.execute('PRAGMA query_only=ON')
            tables = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
            table_hashes = {table: hashlib.sha256(repr(sorted(db.execute(
                'SELECT * FROM "' + table.replace('"', '""') + '"').fetchall(), key=repr)).encode()).hexdigest()
                for table in tables}
        value = {'original_files_sha256': files, 'original_monitor_tables': table_hashes,
            'model_requests': list(self.fixture.requests), 'model_calls': len(host.calls),
            'wire_rows': len(self.wire.rows), 'source_requests': list(self.fixture.side_requests['source']),
            'accounting_requests': list(self.fixture.side_requests['accounting']),
            'validation_calls': list(self.validations), 'tool_calls': list(self.tools)}
        _save(self.artifacts / (name + '.json'), value)
        self.evidence[name] = value
        if name.startswith('before-'):
            self.evidence['readonly_baseline'] = name
        return value


@pytest.fixture
def saved_window_http(tmp_path, loopback_network, request):
    root = Path(os.environ['CLOUDYFAN_MONITOR_EVIDENCE_DIR']) if os.environ.get(
        'CLOUDYFAN_MONITOR_EVIDENCE_DIR') else tmp_path / 'evidence'
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    artifacts = root / (request.node.name + '-artifacts')
    artifacts.mkdir(mode=0o700)
    http = ArtifactRecorder(artifacts)
    wire, request_headers = ArtifactRecorder(artifacts, 'upstream'), ArtifactRecorder(artifacts, 'upstream-request-headers')
    evidence = {'schema_version': 'saved-window-http-fixture-v1', 'test': request.node.name,
        'scope': 'Original BackupCore/BudgetedCore/TaskService/Identity/Health/CapacityWindows and real main HTTP; MockTransport only',
        'synthetic': True, 'python': platform.python_version(), 'sqlite': sqlite3.sqlite_version,
        'fixture_pid': os.getpid(), 'http_responses': http.rows, 'upstream_wire': wire.rows,
        'upstream_request_headers': request_headers.rows}
    stack, validations, tools, clients = ExitStack(), [], [], []
    server = thread = fixture = tracer = None
    try:
        import cloudyfan_jev
        import cloudyfan_monitor
        core, monitor = Path(os.environ['CLOUDYFAN_CORE_ROOT']).resolve(), Path(__file__).resolve().parents[1]
        assert Path(cloudyfan_jev.__file__).resolve().is_relative_to(core / 'src')
        assert Path(cloudyfan_monitor.__file__).resolve().is_relative_to(monitor / 'src')
        evidence['imports'] = {'core': str(Path(cloudyfan_jev.__file__).resolve()),
            'monitor': str(Path(cloudyfan_monitor.__file__).resolve())}
        source = monitor / 'scripts/qa_capacity_windows_http.py'
        spec = importlib.util.spec_from_file_location('saved_window_original_host', source)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        server = ThreadedServer(('127.0.0.1', 0), QuietHandler)
        loopback_network.ports.add(server.server_port)
        origin = f'http://127.0.0.1:{server.server_port}'
        clock = Clock(datetime.fromisoformat('2026-10-21T00:00:00+00:00').timestamp())
        fixture = module.build_window_host(artifacts / 'original-host', origin, clock)

        class ObservedArithmetic(ArithmeticEnvironment):
            def validate(self, final_text):
                result = super().validate(final_text)
                validations.append({'passed': result['passed'], 'checks': len(result['checks'])})
                return result

            def call(self, name, arguments):
                tools.append({'called': True})
                return super().call(name, arguments)

        transports = set()
        for key, contract in fixture.host.service.contracts.items():
            fixture.host.service.contracts[key] = replace(contract,
                environment_factory=lambda workspace: ObservedArithmetic())
            client = contract.runner.client.http
            if id(client._transport) in transports:
                continue
            transports.add(id(client._transport))
            stack.callback(client.close)
            clients.append(client)
            original = client._transport.handler

            def observed(wire_request, upstream=original):
                response = upstream(wire_request)
                response.read()
                row = wire.capture({'method': wire_request.method, 'path': wire_request.url.path,
                    'status': response.status_code}, {'request_body': wire_request.content,
                    'response_body': response.content,
                    'headers': b'\r\n'.join(key + b': ' + value for key, value in response.headers.raw)})
                request_headers.capture({'tuple_sequence': row['sequence'], 'semantics': 'upstream request headers'},
                    {'body': b'\r\n'.join(key + b': ' + value for key, value in wire_request.headers.raw)})
                return response

            client._transport.handler = observed
        for client in fixture.observation_transports:
            stack.callback(client.close)
            clients.append(client)

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

            iterator = fixture.host.app(copied, started)
            try:
                chunks = list(iterator)
            finally:
                if hasattr(iterator, 'close'):
                    iterator.close()
            http.capture(metadata, {'request_body': body, 'response_body': b''.join(chunks), 'headers': headers})
            return chunks

        server.set_app(app)
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
        thread.start()
        operator = stack.enter_context(httpx.Client(base_url=origin, trust_env=False, timeout=10,
            headers={'Authorization': 'Bearer ' + fixture.host.operator_token}))
        client = stack.enter_context(httpx.Client(base_url=origin, trust_env=False, timeout=10,
            headers={'Authorization': 'Bearer ' + fixture.host.credentials['studio'].token}))
        agent = stack.enter_context(httpx.Client(base_url=origin, trust_env=False, timeout=10,
            headers={'Authorization': 'Bearer ' + fixture.host.credentials['agent'].token}))
        clients.extend((operator, client, agent))
        tracer = SavedWindowHTTP(fixture, artifacts, operator, client, agent, evidence, validations, tools, wire)
        yield tracer
    finally:
        if fixture is not None:
            fixture.released.set()
        if thread is not None and thread.is_alive():
            server.shutdown()
            thread.join(2)
        if server is not None:
            server.server_close()
        stack.close()
        if tracer is not None:
            final = tracer.snapshot('after-fixture')
            baseline = evidence.get('readonly_baseline')
            if baseline is not None:
                evidence['original_state_unchanged'] = final == evidence[baseline]
        evidence['artifact_verification'] = {'http': http.verify(), 'upstream': wire.verify(),
            'upstream_request_headers': request_headers.verify()}
        for fault in evidence.get('controlled_faults', []):
            body = (artifacts / fault['archive']).read_bytes()
            assert len(body) == fault['bytes'] and hashlib.sha256(body).hexdigest() == fault['sha256']
        evidence['fault_original_bytes_preserved'] = True
        evidence['observed_calls'] = {'models': len(fixture.requests) if fixture else None,
            'source': len(fixture.side_requests['source']) if fixture else None,
            'accounting': len(fixture.side_requests['accounting']) if fixture else None,
            'validation': len(validations), 'tools': len(tools)}
        evidence['blocked_socket_attempts'] = [str(address) for address in loopback_network.blocked]
        evidence['socket_attempts'] = [{'host': address[0], 'port': address[1]} for address in loopback_network.connected]
        evidence['cleanup'] = {'thread_exited': thread is None or not thread.is_alive(),
            'socket_closed': server is None or server.socket.fileno() == -1,
            'owned_clients_closed': all(client.is_closed for client in clients)}
        _save(artifacts / 'fixture-receipt.json', evidence)
        assert evidence.get('original_state_unchanged', True) and all(evidence['cleanup'].values())
        assert all(row['status'] == 'verified' for row in evidence['artifact_verification'].values())
        assert not loopback_network.blocked

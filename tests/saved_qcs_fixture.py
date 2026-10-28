"""Original QCS host over real HTTP; only upstreams use MockTransport."""
from contextlib import ExitStack
from dataclasses import dataclass, field
from datetime import datetime
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
from cloudyfan_monitor.demo import CATEGORY
from cloudyfan_monitor.integration import build_local_integration

from candidate_observation_fixture import assemble_candidate_host
from task_features_fixture import ArtifactRecorder
from test_local_http_integration import Clock, QuietHandler, ThreadedServer


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _save(path, value):
    with path.open('x', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
    path.chmod(0o600)


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
        assert evidence['python'] == '3.13.13' and evidence['sqlite'] == '3.50.4'
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

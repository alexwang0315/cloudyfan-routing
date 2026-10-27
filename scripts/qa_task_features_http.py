#!/usr/bin/env python3
"""Private real HTTP host for focused creation-feature desktop verification."""
import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import sys
import threading

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tests'))
from capacity_resilience_fixture import build_resilience_health_host  # noqa: E402
from task_features_fixture import ArtifactRecorder, POLICY_BUNDLE, StoreFeatureBindingPort  # noqa: E402
from test_local_http_integration import (  # noqa: E402
    Clock, LoopbackEvidence, QuietHandler, ThreadedServer, configure_loopback_network,
)


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    output = parser.parse_args().output.resolve()
    output.mkdir(mode=0o700, parents=True, exist_ok=True)
    boundary = LoopbackEvidence()
    recorder = ArtifactRecorder(output)
    responses = recorder.rows
    configure_loopback_network(boundary, setattr)
    clock = Clock(datetime.fromisoformat('2026-10-21T00:00:00+00:00').timestamp())
    with ThreadedServer(('127.0.0.1', 0), QuietHandler) as server:
        boundary.ports.add(server.server_port)
        origin = f'http://127.0.0.1:{server.server_port}'
        controlled = build_resilience_health_host(output / 'service', clock=clock, origin=origin,
                                                  held_timeout_seconds=600)
        controlled.host.service.creation_features = {
            'policy_bundle': {**POLICY_BUNDLE, 'feature_version': 'local-deterministic-v2'},
            'binding_port': StoreFeatureBindingPort(controlled.app.store)}

        def state():
            root = controlled.host.root
            return {'model_calls': len(controlled.requests),
                'source_calls': len(controlled.side_requests['source']),
                'accounting_calls': len(controlled.side_requests['accounting']),
                'health_sha256': hashlib.sha256(controlled.host.health.path.read_bytes()).hexdigest(),
                'budgets': {name: report.report() for name, report in controlled.budgets.items()},
                'queue_sha256': hashlib.sha256(controlled.queue.path.read_bytes()).hexdigest(),
                'raw_sha256': {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in sorted(root.rglob('*.json')) if p.is_file()},
                'private_prompt_canary_present': any(b'PRIVATE_FEATURE_DESKTOP_CANARY' in p.read_bytes()
                    for p in (root / 'tasks').glob('http-*/job.json'))}

        def app(environ, start_response):
            row = {'method': environ.get('REQUEST_METHOD'), 'path': environ.get('PATH_INFO')}
            def started(value, headers, exc_info=None):
                row['status'] = int(value.split()[0])
                return start_response(value, headers, exc_info)
            body = list(controlled.app(environ, started))
            recorder.capture(row, {'response_body': b''.join(body)})
            return body

        server.set_app(app)
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01},
                                  daemon=True, name='task-feature-desktop-http')
        thread.start()
        try:
            with httpx.Client(base_url=origin, trust_env=False, timeout=5) as client:
                operator = {'Authorization': 'Bearer ' + controlled.operator_token}
                agent = {'Authorization': 'Bearer ' + controlled.credentials['agent'].token}
                cap = client.get('/monitor/capacity', headers=operator)
                assert cap.status_code == 200
                assert client.post('/monitor/capacity/activate', headers=operator, json={
                    'revision': cap.json()['dispatch']['revision'], 'version': controlled.policy_version}).status_code == 200
                for run in ('features-complete', 'features-queued', 'features-legacy'):
                    if run == 'features-legacy':
                        controlled.host.service.creation_features = None
                    response = client.post('/v1/tasks', headers=agent, json={
                        'run_id': run, 'category': controlled.category,
                        'prompt': '中文1+1 PRIVATE_FEATURE_DESKTOP_CANARY'})
                    assert response.status_code == 202
                    if run == 'features-complete':
                        assert controlled.queue.dispatch_next() is not None
                        assert client.get('/v1/tasks/' + run, headers=agent).json()['status'] == 'passed'
            baseline = state()
            (output / 'baseline-state.json').write_text(json.dumps(baseline, indent=2) + '\n')
            print(json.dumps({'type': 'ready', 'origin': origin, 'state': baseline,
                              'fixture_pid': os.getpid(), 'scope': 'real headed desktop and HTTP; all upstreams MockTransport'}), flush=True)
            for line in sys.stdin:
                command = json.loads(line)['command']
                if command == 'stop':
                    break
                assert command == 'state'
                print(json.dumps({'type': 'state', 'state': state()}), flush=True)
        finally:
            server.shutdown()
            thread.join(2)
            controlled.close()
            final = state()
            verification = recorder.verify()
            (output / 'fixture-receipt.json').write_text(json.dumps({
                'schema_version': 1, 'fixture_pid': os.getpid(), 'scope': 'synthetic upstreams; real product HTTP and stores',
                'http_responses': responses, 'final': final, 'blocked_socket_attempts': [str(x) for x in boundary.blocked],
                'socket_attempts': [{'host': x[0], 'port': x[1]} for x in boundary.connected],
                'artifact_verification': verification,
                'cleanup': {'fixture_close_completed': True, 'server_thread_exited': not thread.is_alive()}}, indent=2) + '\n')
            assert not thread.is_alive() and not boundary.blocked
            assert verification['status'] == 'verified', 'artifact_reference_integrity_failed'


if __name__ == '__main__':
    main()

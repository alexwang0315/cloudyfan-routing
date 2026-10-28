#!/usr/bin/env python3
"""Owned real loopback host for observation-policy desktop verification."""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import secrets
import sqlite3
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


def check(value, code):
    if not value:
        raise AssertionError(code)


def save(path, value):
    with path.open('x', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write('\n')


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--phase', choices=('old-ui-red', 'desktop-frozen'), required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    boundary, recorder = LoopbackEvidence(), ArtifactRecorder(output)
    wire = ArtifactRecorder(output, 'upstream')
    configure_loopback_network(boundary, setattr)
    clock = Clock(1792540800)
    server = ThreadedServer(('127.0.0.1', 0), QuietHandler)
    boundary.ports.add(server.server_port)
    origin = f'http://127.0.0.1:{server.server_port}'
    receipt = {'schema_version': 'qa41-observation-policy-host-v1', 'synthetic': True,
        'phase': args.phase, 'fixture_pid': os.getpid(), 'python': platform.python_version(),
        'sqlite': sqlite3.sqlite_version, 'http_responses': recorder.rows,
        'scope': 'Real Core/Budget/Store/Identity/Queue and loopback; upstreams MockTransport only',
        'upstream_wire': wire.rows, 'commands': [], 'safe_projection_count': 0}
    controlled = thread = worker = None
    failure = None
    history_release, history_captured = threading.Event(), threading.Event()
    policy_release, policy_captured = threading.Event(), threading.Event()
    gate_lock = threading.Lock()
    gates = {'policy_next': False}
    if args.phase == 'old-ui-red':
        history_release.set()
    worker_errors = []
    try:
        check(platform.python_version() == '3.13.13', 'qa_python_runtime_mismatch')
        import cloudyfan_jev
        import cloudyfan_monitor
        receipt['import_roots'] = {'core': str(Path(cloudyfan_jev.__file__).resolve()),
            'monitor': str(Path(cloudyfan_monitor.__file__).resolve())}
        check(Path(cloudyfan_jev.__file__).resolve().is_relative_to(Path(os.environ['CLOUDYFAN_CORE_ROOT']).resolve()),
              'qa_wrong_core_import')
        check(Path(cloudyfan_monitor.__file__).resolve().is_relative_to(ROOT), 'qa_wrong_monitor_import')
        def record_wire(request):
            wire.capture({'method': request.method, 'path': request.url.path},
                {'body': request.content,
                 'headers': b'\r\n'.join(key + b': ' + value for key, value in request.headers.raw)})

        controlled = build_resilience_health_host(output / 'service', clock=clock,
            origin=origin, held_timeout_seconds=600, before_response=record_wire)

        def features(version):
            controlled.host.service.creation_features = {
                'policy_bundle': {**POLICY_BUNDLE, 'feature_version': version},
                'binding_port': StoreFeatureBindingPort(controlled.app.store)}

        def public_safe(value):
            serialized = json.dumps(value, ensure_ascii=False)
            protected = ['PRIVATE_OBSERVATION_POLICY_CANARY', 'PRIVATE_OBSERVATION_FAULT_CANARY',
                str(controlled.host.root), controlled.app.admin_token,
                *(credential.token for credential in controlled.credentials.values())]
            check(not any(private in serialized for private in protected), 'qa_private_http_projection_exposed')
            receipt['safe_projection_count'] += 1

        def state():
            budgets = {name: ledger.report() for name, ledger in controlled.budgets.items()}
            return {'model_calls': len(controlled.requests),
                'wire_references': len(wire.rows), 'model_metadata_sha256': digest(controlled.requests),
                'source_calls': len(controlled.side_requests['source']),
                'accounting_calls': len(controlled.side_requests['accounting']),
                'budgets_sha256': digest(budgets),
                'queue_sha256': hashlib.sha256(controlled.queue.path.read_bytes()).hexdigest(),
                'health_sha256': hashlib.sha256(controlled.host.health.path.read_bytes()).hexdigest(),
                'capacity_sha256': hashlib.sha256(controlled.controller.store.path.read_bytes()).hexdigest(),
                'mock_hold_started': controlled.held.is_set(),
                'mock_released': controlled.released.is_set(),
                'factory_calls': len(controlled.factory_calls)}

        def app(environ, start_response):
            length = int(environ.get('CONTENT_LENGTH') or 0)
            check(0 <= length <= 131072, 'qa_request_capture_bound')
            request = environ['wsgi.input'].read(length) if length else b''
            copied = dict(environ)
            copied['wsgi.input'] = io.BytesIO(request)
            row = {'method': environ.get('REQUEST_METHOD'), 'path': environ.get('PATH_INFO')}
            response_headers = b''

            def started(status, headers, exc_info=None):
                nonlocal response_headers
                row['status'] = int(status.split()[0])
                response_headers = json.dumps(headers, ensure_ascii=True).encode()
                return start_response(status, headers, exc_info)

            iterator = controlled.app(copied, started)
            try:
                chunks = list(iterator)
            finally:
                if hasattr(iterator, 'close'):
                    iterator.close()
            response_body = b''.join(chunks)
            if row.get('status') in (200, 202) and (row['path'].startswith('/v1/tasks')
                    or row['path'] in {'/monitor/snapshot', '/monitor/observation-policy',
                                      '/monitor/observation-policy/activate'}):
                public_safe(json.loads(response_body))
            recorder.capture(row, {'request_body': request, 'response_body': response_body,
                                   'headers': response_headers})
            if row['path'] == '/monitor/snapshot' and not history_release.is_set():
                history_captured.set()
                check(history_release.wait(120), 'qa_history_gate_timeout')
            hold_policy = False
            if row['path'] == '/monitor/observation-policy':
                with gate_lock:
                    if gates['policy_next']:
                        gates['policy_next'] = False
                        hold_policy = True
                if hold_policy:
                    policy_captured.set()
                    check(policy_release.wait(30), 'qa_policy_gate_timeout')
            return chunks

        server.set_app(app)
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01},
            daemon=True, name='qa41-owned-loopback')
        thread.start()
        with httpx.Client(base_url=origin, trust_env=False, timeout=5) as client:
            def operator_headers():
                return {'Authorization': 'Bearer ' + controlled.app.admin_token}

            def agent_headers(principal='agent'):
                return {'Authorization': 'Bearer ' + controlled.credentials[principal].token}

            def policy():
                response = client.get('/monitor/observation-policy', headers=operator_headers())
                check(response.status_code == 200, 'qa_policy_read_failed')
                return response.json()

            def activate(version):
                response = client.post('/monitor/observation-policy/activate', headers=operator_headers(),
                    json={'revision': policy()['revision'], 'version': version})
                check(response.status_code == 200, 'qa_external_policy_change_failed')
                return response.json()

            originals = {}

            def submit(run, *, principal='agent', oversized=False):
                response = client.post('/v1/tasks', headers=agent_headers(principal),
                    json={'run_id': run, 'category': controlled.category,
                          'prompt': ('x' * 32769 if oversized else '中文1+1 ') + 'PRIVATE_OBSERVATION_POLICY_CANARY'})
                check(response.status_code == 202, 'qa_original_task_not_accepted')
                value = response.json()
                public_safe(value)
                originals[(principal, run)] = {key: value.get(key) for key in ('task_features', 'routing_recommendation')}
                return value

            def check_originals():
                for (principal, run), original in originals.items():
                    if run in corrupted:
                        continue
                    response = client.get('/v1/tasks/' + run, headers=agent_headers(principal))
                    check(response.status_code in (200, 202), 'qa_original_task_read_failed')
                    value = response.json()
                    check({key: value.get(key) for key in original} == original, 'qa_old_observation_changed')
                return len(originals) - len(corrupted)

            corrupted = set()
            capacity = client.get('/monitor/capacity', headers=operator_headers())
            check(capacity.status_code == 200, 'qa_capacity_read_failed')
            activated = client.post('/monitor/capacity/activate', headers=operator_headers(), json={
                'revision': capacity.json()['dispatch']['revision'], 'version': controlled.policy_version})
            check(activated.status_code == 200, 'qa_capacity_activation_failed')
            if args.phase == 'desktop-frozen':
                submit('qa41-legacy')
                check(controlled.queue.dispatch_next() is not None, 'qa_legacy_dispatch_missing')
                features('local-deterministic-v1')
                complete = submit('qa41-v1-complete')
                check(complete['task_features']['feature_version'] == 'local-deterministic-v1', 'qa_original_v1_missing')
                check(controlled.queue.dispatch_next() is not None, 'qa_v1_dispatch_missing')
                submit('qa41-v1-running')
                controlled.flags['hold'] = True

                def dispatch_worker():
                    try:
                        check(controlled.queue.dispatch_next() is not None, 'qa_running_dispatch_missing')
                    except BaseException:
                        worker_errors.append('qa_owned_worker_failed')

                worker = threading.Thread(target=dispatch_worker, daemon=True, name='qa41-owned-held-worker')
                worker.start()
                check(controlled.held.wait(5), 'qa_real_model_hold_not_started')
                check(policy()['configuration_source'] == 'host', 'qa_initial_host_setting_missing')
            else:
                features('local-deterministic-v2')
                submit('qa41-original-task')
                check(controlled.queue.dispatch_next() is not None, 'qa_original_dispatch_missing')
            baseline = state()
            receipt['baseline'] = baseline
            receipt['ready_policy'] = policy() if args.phase == 'desktop-frozen' else None
            print(json.dumps({'type': 'ready', 'origin': origin, 'fixture_pid': os.getpid(),
                'synthetic': True, 'state': baseline, 'long_task_held': worker is not None}), flush=True)
            read_baseline = None
            cases = {'advice': ('qa41-advice-new', 'features-v2-advice-v1', 'available', 'local-deterministic-v2', 'unknown'),
                'disabled': ('qa41-disabled', None, 'not_provided', None, 'not_provided'),
                'rollback': ('qa41-v1-rollback', 'features-v1', 'available', 'local-deterministic-v1', 'not_provided'),
                'bounded-failure': ('qa41-bounded-failure', 'features-v2-advice-v1', 'unavailable', None, 'unavailable')}
            for line in sys.stdin:
                message = json.loads(line)
                command = message.get('command')
                if command == 'stop':
                    break
                result = {'type': command}
                if command == 'state':
                    result['state'] = state()
                elif args.phase == 'desktop-frozen' and command == 'await-history-hold':
                    check(history_captured.wait(5), 'qa_real_history_response_not_held')
                    result['captured_real_response'] = True
                elif args.phase == 'desktop-frozen' and command == 'release-history':
                    check(history_captured.is_set(), 'qa_no_real_history_response_held')
                    history_release.set()
                    result['original_response_released_unmodified'] = True
                elif args.phase == 'desktop-frozen' and command == 'new-task':
                    key = message.get('case')
                    check(key in cases, 'qa_unknown_case')
                    run, version, feature_state, feature_version, advice_state = cases[key]
                    check(policy()['active_version'] == version, 'qa_case_policy_not_active')
                    value = submit(run, oversized=key == 'bounded-failure')
                    check(value['task_features']['status'] == feature_state, 'qa_case_feature_state_wrong')
                    check(value['task_features'].get('feature_version') == feature_version, 'qa_case_feature_version_wrong')
                    check(value['routing_recommendation']['status'] == advice_state, 'qa_case_advice_state_wrong')
                    result.update(run=run, original_observations_preserved=check_originals())
                elif args.phase == 'desktop-frozen' and command == 'external-change':
                    version = message.get('version')
                    check(version in {None, 'features-v1', 'features-v2', 'features-v2-advice-v1'}, 'qa_external_version_not_known')
                    result['policy'] = activate(version)
                elif args.phase == 'desktop-frozen' and command == 'read-baseline':
                    read_baseline = state()
                    result['state'] = read_baseline
                elif args.phase == 'desktop-frozen' and command == 'verify-readonly':
                    check(read_baseline is not None and state() == read_baseline, 'qa_readonly_business_state_changed')
                    result['state'] = state()
                    result['verified'] = True
                    receipt['readonly_oracle_verified'] = True
                elif args.phase == 'desktop-frozen' and command == 'corrupt-evidence':
                    run = 'qa41-advice-new'
                    saved = originals[('agent', run)]['task_features']
                    record = controlled.host.service.root / '.task-features' / (saved['binding']['internal_id'] + '.json')
                    original = record.read_bytes()
                    with (output / 'preserved-before-fault.bin').open('xb') as stream:
                        stream.write(original)
                    record.write_bytes(b'{"PRIVATE_OBSERVATION_FAULT_CANARY":')
                    corrupted.add(run)
                    receipt['evidence_fault'] = {'preserved_sha256': hashlib.sha256(original).hexdigest(),
                        'fault_sha256': hashlib.sha256(record.read_bytes()).hexdigest(), 'original_preserved': True}
                    response = client.get('/v1/tasks/' + run, headers=agent_headers())
                    check(response.status_code == 202 and response.json()['task_features']['status'] == 'unavailable',
                          'qa_real_corrupt_evidence_not_unavailable')
                    result['actual_saved_record_fault'] = True
                elif args.phase == 'desktop-frozen' and command == 'hold-policy':
                    with gate_lock:
                        check(not gates['policy_next'], 'qa_policy_gate_already_armed')
                        policy_release.clear()
                        policy_captured.clear()
                        gates['policy_next'] = True
                    result['armed'] = True
                elif args.phase == 'desktop-frozen' and command == 'await-policy-hold':
                    check(policy_captured.wait(5), 'qa_real_policy_response_not_held')
                    result['captured_real_response'] = True
                elif args.phase == 'desktop-frozen' and command == 'release-policy':
                    check(policy_captured.is_set(), 'qa_no_policy_response_to_release')
                    policy_release.set()
                    result['original_response_released_unmodified'] = True
                elif args.phase == 'desktop-frozen' and command == 'rotate-operator':
                    controlled.app.admin_token = secrets.token_urlsafe(48)
                    receipt['operator_credential_rotated'] = True
                    result['rotated'] = True
                elif args.phase == 'desktop-frozen' and command == 'release-model':
                    controlled.flags['hold'] = False
                    controlled.released.set()
                    worker.join(5)
                    check(not worker.is_alive() and not worker_errors, 'qa_owned_worker_not_finished')
                    value = client.get('/v1/tasks/qa41-v1-running', headers=agent_headers())
                    check(value.status_code == 200 and value.json()['status'] == 'passed', 'qa_original_held_task_not_passed')
                    result['original_observations_preserved'] = check_originals()
                    receipt['held_model_completed'] = True
                else:
                    check(False, 'qa_unknown_private_command')
                receipt['commands'].append({key: value for key, value in result.items() if key != 'state'})
                print(json.dumps(result), flush=True)
            receipt['final'] = state()
            receipt['business_state_unchanged'] = receipt['final'] == baseline
    except BaseException as error:
        failure = error
        receipt['failure_code'] = (str(error) if isinstance(error, AssertionError)
            and str(error).startswith('qa_') else 'qa_host_failed')
    finally:
        history_release.set()
        policy_release.set()
        if controlled is not None:
            controlled.close()
        if worker is not None:
            worker.join(5)
        if thread is not None and thread.is_alive():
            server.shutdown()
            thread.join(2)
        server.server_close()
        receipt['artifact_verification'] = recorder.verify()
        receipt['upstream_artifact_verification'] = wire.verify()
        receipt['blocked_socket_attempt_count'] = len(boundary.blocked)
        receipt['socket_attempts'] = [{'host': host, 'port': port} for host, port in boundary.connected]
        receipt['cleanup'] = {'server_thread_exited': thread is None or not thread.is_alive(),
            'server_socket_closed': server.socket.fileno() == -1,
            'fixture_close_completed': controlled is not None,
            'held_worker_exited': worker is None or not worker.is_alive()}
        save(output / 'fixture-receipt.json', receipt)
    if failure is not None:
        raise RuntimeError(receipt['failure_code']) from None
    if args.phase == 'old-ui-red':
        check(receipt['business_state_unchanged'], 'qa_readonly_business_state_changed')
    else:
        check(receipt.get('readonly_oracle_verified'), 'qa_readonly_oracle_not_measured')
        check(receipt.get('held_model_completed'), 'qa_held_model_not_completed')
    check(not boundary.blocked, 'qa_network_guard_failed')
    check(receipt['artifact_verification']['status'] == 'verified', 'qa_http_reference_verification_failed')


if __name__ == '__main__':
    main()

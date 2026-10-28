#!/usr/bin/env python3
"""Real main-service host for frozen saved-policy-comparison desktop QA."""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import re
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
    parser.add_argument('--phase', choices=('desktop-frozen',), required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    boundary = LoopbackEvidence()
    recorder, wire = ArtifactRecorder(output), ArtifactRecorder(output, 'upstream')
    configure_loopback_network(boundary, setattr)
    server = ThreadedServer(('127.0.0.1', 0), QuietHandler)
    boundary.ports.add(server.server_port)
    origin = f'http://127.0.0.1:{server.server_port}'
    clock = Clock(1792540800)
    receipt = {'schema_version': 'qa42-saved-comparisons-host-v1', 'synthetic': True,
        'phase': args.phase, 'fixture_pid': os.getpid(), 'python': platform.python_version(),
        'sqlite': sqlite3.sqlite_version, 'http_responses': recorder.rows, 'upstream_wire': wire.rows,
        'scope': 'Real main service/Core/BudgetedCore/Store/Identity/Queue; upstreams MockTransport only',
        'commands': [], 'safe_projection_count': 0,
        'tool_scope': 'Original registered fixture has no executable tools; no fabricated tool counter'}
    controlled = thread = worker = None
    worker_errors, failure = [], None
    history_release, history_captured = threading.Event(), threading.Event()
    read_release, read_captured = threading.Event(), threading.Event()
    gate_lock, gates = threading.Lock(), {'next_path': None}
    history_release.set()
    try:
        check(platform.python_version() == '3.13.13' and sqlite3.sqlite_version == '3.50.4', 'qa_runtime_mismatch')
        import cloudyfan_jev
        import cloudyfan_monitor
        receipt['import_roots'] = {'core': str(Path(cloudyfan_jev.__file__).resolve()),
            'monitor': str(Path(cloudyfan_monitor.__file__).resolve())}
        check(Path(cloudyfan_jev.__file__).resolve().is_relative_to(Path(os.environ['CLOUDYFAN_CORE_ROOT']).resolve() / 'src'),
              'qa_wrong_core_import')
        check(Path(cloudyfan_monitor.__file__).resolve().is_relative_to(ROOT / 'src'), 'qa_wrong_monitor_import')

        def record_wire(request):
            wire.capture({'method': request.method, 'path': request.url.path},
                {'body': request.content, 'headers': b'\r\n'.join(key + b': ' + value for key, value in request.headers.raw)})

        controlled = build_resilience_health_host(output / 'service', clock=clock,
            origin=origin, held_timeout_seconds=600, before_response=record_wire)
        controlled.host.service.creation_features = {'policy_bundle': dict(POLICY_BUNDLE),
            'binding_port': StoreFeatureBindingPort(controlled.app.store)}
        private_values = ['PRIVATE_SAVED_POLICY_CANARY', 'PRIVATE_RESILIENCE_PROVIDER_CANARY',
            'PRIVATE_RESILIENCE_HEADER_CANARY', 'PRIVATE_RESILIENCE_OBSERVATION_CANARY',
            str(controlled.host.root), controlled.app.admin_token,
            *(credential.token for credential in controlled.credentials.values())]

        def public_safe(value):
            body = json.dumps(value, ensure_ascii=False)
            check(not any(private in body for private in private_values), 'qa_private_http_projection_exposed')
            receipt['safe_projection_count'] += 1

        def app(environ, start_response):
            length = int(environ.get('CONTENT_LENGTH') or 0)
            check(0 <= length <= 131072, 'qa_request_capture_bound')
            request = environ['wsgi.input'].read(length) if length else b''
            copied = dict(environ)
            copied['wsgi.input'] = io.BytesIO(request)
            row = {'method': environ.get('REQUEST_METHOD'), 'path': environ.get('PATH_INFO')}
            headers = b''

            def started(status, response_headers, exc_info=None):
                nonlocal headers
                row['status'] = int(status.split()[0])
                headers = json.dumps(response_headers, ensure_ascii=True).encode()
                return start_response(status, response_headers, exc_info)

            iterator = controlled.app(copied, started)
            try:
                chunks = list(iterator)
            finally:
                if hasattr(iterator, 'close'):
                    iterator.close()
            body = b''.join(chunks)
            if row['path'].startswith(('/monitor/', '/v1/tasks')):
                public_safe(json.loads(body))
            recorder.capture(row, {'request_body': request, 'response_body': body, 'headers': headers})
            if row['path'] == '/monitor/snapshot' and not history_release.is_set():
                history_captured.set()
                check(history_release.wait(120), 'qa_history_gate_timeout')
            held = False
            with gate_lock:
                if row['method'] == 'GET' and gates['next_path'] == row['path']:
                    gates['next_path'] = None
                    held = True
            if held:
                read_captured.set()
                check(read_release.wait(30), 'qa_comparison_read_gate_timeout')
            return chunks

        server.set_app(app)
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01},
            daemon=True, name='qa42-owned-loopback')
        thread.start()
        with httpx.Client(base_url=origin, trust_env=False, timeout=5) as client:
            def operator_headers():
                return {'Authorization': 'Bearer ' + controlled.app.admin_token}

            def agent_headers(principal):
                return {'Authorization': 'Bearer ' + controlled.credentials[principal].token}

            def submit(principal, run):
                response = client.post('/v1/tasks', headers=agent_headers(principal), json={
                    'run_id': run, 'category': controlled.category, 'prompt': '中文1+1 PRIVATE_SAVED_POLICY_CANARY'})
                check(response.status_code == 202, 'qa_original_task_not_accepted')
                return response

            capacity = client.get('/monitor/capacity', headers=operator_headers())
            check(capacity.status_code == 200, 'qa_capacity_read_failed')
            activated = client.post('/monitor/capacity/activate', headers=operator_headers(), json={
                'revision': capacity.json()['dispatch']['revision'], 'version': controlled.policy_version})
            check(activated.status_code == 200, 'qa_capacity_activation_failed')
            submit('agent', 'qa42-original-task')
            check(controlled.queue.dispatch_next() is not None, 'qa_original_dispatch_missing')
            original = client.get('/v1/tasks/qa42-original-task', headers=agent_headers('agent'))
            check(original.status_code == 200 and original.json()['status'] == 'passed', 'qa_original_task_not_passed')
            receipt['original'] = {'task': {'client': 'agent', 'run_id': 'qa42-original-task'},
                'status': original.json()['status'], 'response_sha256': hashlib.sha256(original.content).hexdigest()}
            submit('studio', 'qa42-held-task')
            controlled.flags['hold'] = True

            def dispatch_worker():
                try:
                    check(controlled.queue.dispatch_next() is not None, 'qa_held_dispatch_missing')
                except BaseException:
                    worker_errors.append('qa_owned_worker_failed')

            worker = threading.Thread(target=dispatch_worker, daemon=True, name='qa42-owned-held-worker')
            worker.start()
            check(controlled.held.wait(5), 'qa_real_model_hold_not_started')
            # This genuine queued task is deliberately never dispatched. The held
            # task may already have a saved first decision and is not the unknown oracle.
            submit('agent', 'qa42-unstarted-task')
            # Warm only the original observation before measuring the comparison window.
            warm = client.get('/monitor/snapshot', headers=operator_headers())
            check(warm.status_code == 200, 'qa_original_observation_warm_failed')
            catalog = client.get('/monitor/comparisons', headers=operator_headers())
            check(catalog.status_code == 200 and catalog.json()['schema_version'] == 'saved-policy-comparison-catalog-v1',
                  'qa_real_comparison_catalogue_missing')
            expected_tasks = {('agent', 'qa42-original-task'), ('studio', 'qa42-held-task'),
                ('agent', 'qa42-unstarted-task')}
            check(expected_tasks <= {(row['client'], row['run_id']) for row in catalog.json()['task_scopes']},
                  'qa_original_tasks_not_in_real_catalogue')
            check(len(wire.rows) == len(controlled.requests) and len(wire.rows) > 0, 'qa_nonzero_wire_positive_control_missing')

            def state():
                # Original immutable authority only. New comparison/audit rows may be added.
                tables = ('control', 'clients', 'bindings', 'tasks', 'pools', 'version_uses', 'creation_feature_adoptions',
                    'creation_observation_control', 'creation_observation_absences', 'billing_control',
                    'billing_entries', 'billing_task_policies', 'billing_waiting', 'billing_attempts',
                    'billing_source_state', 'billing_supplier_evidence')
                with sqlite3.connect(controlled.app.store.path.as_uri() + '?mode=ro', uri=True, timeout=0) as db:
                    db.execute('PRAGMA query_only=ON')
                    table_hashes = {name: digest(sorted(db.execute('SELECT * FROM ' + name).fetchall(), key=repr))
                        for name in tables}
                protected = {}
                for source in sorted(controlled.host.root.rglob('*')):
                    relative = source.relative_to(controlled.host.root)
                    # Only this new immutable-report namespace may gain report files.
                    # Monitor SQLite is checked by original-table values above.
                    if relative.parts[0] == '.saved-comparisons' or source == controlled.app.store.path:
                        continue
                    if source.is_file():
                        protected[str(relative)] = hashlib.sha256(source.read_bytes()).hexdigest()
                return {'model_calls': len(controlled.requests), 'model_metadata_sha256': digest(controlled.requests),
                    'wire_rows': len(wire.rows), 'source_requests': list(controlled.side_requests['source']),
                    'accounting_requests': list(controlled.side_requests['accounting']),
                    'factory_calls': len(controlled.factory_calls), 'original_monitor_table_hashes': table_hashes,
                    'original_evidence_hashes': protected,
                    'budget_hashes': {name: hashlib.sha256(store.path.read_bytes()).hexdigest() for name, store in controlled.budgets.items()},
                    'queue_sha256': hashlib.sha256(controlled.queue.path.read_bytes()).hexdigest(),
                    'capacity_sha256': hashlib.sha256(controlled.controller.store.path.read_bytes()).hexdigest(),
                    'mock_hold_started': controlled.held.is_set(), 'mock_released': controlled.released.is_set()}

            baseline = state()
            receipt['baseline'] = baseline
            history_release.clear()
            print(json.dumps({'type': 'ready', 'origin': origin, 'fixture_pid': os.getpid(), 'synthetic': True,
                'state': baseline, 'original_completed': True, 'long_task_held': True}), flush=True)
            read_baseline = None
            for line in sys.stdin:
                message = json.loads(line)
                command = message.get('command')
                if command == 'stop':
                    break
                result = {'type': command}
                if command == 'state':
                    result['state'] = state()
                elif command == 'read-baseline':
                    read_baseline = state()
                    result['state'] = read_baseline
                elif command == 'verify-readonly':
                    check(read_baseline is not None and state() == read_baseline, 'qa_comparison_changed_original_authority')
                    receipt['readonly_oracle_verified'] = True
                    result['original_state_unchanged'] = True
                elif command == 'await-history-hold':
                    check(history_captured.wait(5), 'qa_real_history_response_not_held')
                    result['captured_real_response'] = True
                elif command == 'release-history':
                    check(history_captured.is_set(), 'qa_no_history_response_to_release')
                    history_release.set()
                    result['original_response_released_unmodified'] = True
                elif command == 'hold-read':
                    selected_path = message.get('path')
                    check(selected_path == '/monitor/comparisons' or isinstance(selected_path, str)
                        and re.fullmatch(r'/monitor/comparisons/cmp-[a-f0-9]{32}', selected_path), 'qa_invalid_read_gate_path')
                    with gate_lock:
                        check(gates['next_path'] is None, 'qa_read_gate_already_armed')
                        read_release.clear()
                        read_captured.clear()
                        gates['next_path'] = selected_path
                    result['gate_armed'] = True
                elif command == 'await-read-hold':
                    check(read_captured.wait(5), 'qa_real_comparison_response_not_held')
                    result['captured_real_response'] = True
                elif command == 'release-read':
                    check(read_captured.is_set(), 'qa_no_comparison_response_to_release')
                    read_release.set()
                    result['original_response_released_unmodified'] = True
                elif command == 'rotate-operator':
                    controlled.app.admin_token = secrets.token_urlsafe(32)
                    private_values.append(controlled.app.admin_token)
                    result['operator_token'] = controlled.app.admin_token
                elif command == 'release-model':
                    controlled.flags['hold'] = False
                    controlled.released.set()
                    worker.join(5)
                    check(not worker.is_alive() and not worker_errors, 'qa_owned_worker_not_finished')
                    completed = client.get('/v1/tasks/qa42-held-task', headers=agent_headers('studio'))
                    check(completed.status_code == 200 and completed.json()['status'] == 'passed', 'qa_original_held_task_not_passed')
                    receipt['held_model_completed'] = True
                    result['original_task_passed'] = True
                    unstarted = client.get('/v1/tasks/qa42-unstarted-task', headers=agent_headers('agent'))
                    check(unstarted.status_code == 202 and unstarted.json()['status'] == 'queued',
                          'qa_unstarted_task_was_dispatched')
                    receipt['unstarted_task_still_queued'] = True
                else:
                    check(False, 'qa_unknown_private_command')
                receipt['commands'].append({name: value for name, value in result.items()
                    if name not in {'operator_token', 'state'}})
                print(json.dumps(result), flush=True)
            receipt['final'] = state()
    except BaseException as error:
        failure = error
        receipt['failure_code'] = (str(error) if isinstance(error, AssertionError)
            and str(error).startswith('qa_') else 'qa_host_failed')
        import traceback
        with (output / 'host-exception.txt').open('x') as stream:
            traceback.print_exception(error, file=stream)
    finally:
        history_release.set()
        read_release.set()
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
            'server_socket_closed': server.socket.fileno() == -1, 'fixture_close_completed': controlled is not None,
            'held_worker_exited': worker is None or not worker.is_alive()}
        save(output / 'fixture-receipt.json', receipt)
    if failure is not None:
        raise RuntimeError(receipt['failure_code']) from None
    check(receipt.get('readonly_oracle_verified') and receipt.get('held_model_completed'), 'qa_readonly_or_held_finish_missing')
    check(not boundary.blocked, 'qa_network_guard_failed')
    check(receipt['artifact_verification']['status'] == 'verified'
        and receipt['upstream_artifact_verification']['status'] == 'verified', 'qa_reference_verification_failed')


if __name__ == '__main__':
    main()

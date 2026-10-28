#!/usr/bin/env python3
"""Real loopback host for explicitly frozen recommendation desktop acceptance."""
import argparse
from contextlib import ExitStack
from datetime import datetime
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import threading

ROOT = Path(os.environ['CLOUDYFAN_MONITOR_ROOT']).resolve()
sys.path.insert(0, str(ROOT / 'tests'))
import httpx
from capacity_resilience_fixture import build_resilience_health_host
from task_features_fixture import ArtifactRecorder, POLICY_BUNDLE, StoreFeatureBindingPort, TaskFeaturesHTTP
# The companion driver requires and verifies an explicit complete source manifest.
from task_recommendations_fixture import calibrated_contracts, enable_recommendations
from test_local_http_integration import Clock, LoopbackEvidence, QuietHandler, ThreadedServer, configure_loopback_network

CANARY = 'PRIVATE_FEATURE_DESKTOP_CANARY PRIVATE_RECOMMENDATION_DESKTOP_CANARY'
UNSUPPORTED_KIND = 'future_calibration_kind'
UNSUPPORTED_REASON = 'exact_calibration_source_schema_unsupported'
UNAVAILABLE = {'schema_version': 'creation-routing-recommendation-v1',
               'status': 'unavailable', 'adopted': False,
               'reason': 'creation_recommendation_evidence_unavailable'}
NOT_PROVIDED = {'schema_version': 'creation-routing-recommendation-v1',
                'status': 'not_provided', 'adopted': False,
                'reason': 'creation_recommendation_not_provided'}
TIMEOUTS = {'http_seconds': 5, 'initial_unused_held_seconds': 600,
            'calibrated_helper_held_seconds': 30, 'server_join_seconds': 2,
            'ui_ms': 6000, 'ipc_ms': 15000, 'product_poll_ms': 5000}


def check(condition, code):
    if not condition:
        raise AssertionError(code)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save(path, value):
    raw = (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode()
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'wb') as stream:
        stream.write(raw)
    return {'path': path.name, 'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}


class SourceRootAudit:
    """Passive CPython I/O audit; no blocking, reader patch or product result change.

    Only events during the explicit product window are counted. Positive-control
    reads and our inventory/hash verification are deliberately separate windows.
    CPython has no generic stat event: this is an open/directory-read oracle,
    supplemented by the frozen product source's unsupported-kind early branch.
    """
    EVENTS = {'open', 'os.listdir', 'os.scandir', 'os.walk', 'os.fwalk'}

    def __init__(self, root):
        self.root = os.path.abspath(os.fspath(root))
        # Observer inventory is outside every product window. The actual reader
        # uses directory descriptors, so cwd-based path expansion is insufficient.
        nodes = [Path(self.root), *Path(self.root).rglob('*')]
        self.source_nodes = {(p.lstat().st_dev, p.lstat().st_ino) for p in nodes}
        self.source_directories = {(p.lstat().st_dev, p.lstat().st_ino) for p in nodes if p.is_dir()}
        self.original_open = os.open
        self.observed_open = self._open
        self.active = False
        self.rows = []
        self.observer_errors = []
        sys.addaudithook(self._observe)

    def _start(self):
        check(os.open is self.original_open, 'qa_source_open_observer_conflict')
        os.open = self.observed_open
        self.active = True

    def _stop(self):
        self.active = False
        if os.open is not self.observed_open:
            self.observer_errors.append('qa_source_open_observer_changed')
        os.open = self.original_open

    def _open(self, path, flags, *args, **kwargs):
        relative = isinstance(path, (str, bytes, os.PathLike)) and not os.path.isabs(os.fspath(path))
        recorded = False
        if self.active and relative and kwargs.get('dir_fd') is not None:
            try:
                info = os.fstat(kwargs['dir_fd'])
                if (info.st_dev, info.st_ino) in self.source_directories:
                    self.rows.append({'event': 'open', 'seam': 'relative_dir_fd'})
                    recorded = True
            except Exception:
                self.observer_errors.append('qa_source_audit_observer_failed')
        # Always forward precisely the original arguments and the actual FD.
        # No replacement value, failure, source body or extra source open.
        descriptor = self.original_open(path, flags, *args, **kwargs)
        if self.active and relative and not recorded:
            try:
                info = os.fstat(descriptor)
                if (info.st_dev, info.st_ino) in self.source_nodes:
                    self.rows.append({'event': 'open', 'seam': 'relative_target_inode'})
            except Exception:
                self.observer_errors.append('qa_source_audit_observer_failed')
        return descriptor

    def _observe(self, event, args):
        if not self.active or event not in self.EVENTS or not args:
            return
        try:
            value = args[0]
            if isinstance(value, int):
                info = os.fstat(value)
                if (info.st_dev, info.st_ino) in self.source_directories:
                    self.rows.append({'event': event, 'seam': 'directory_fd_audit'})
                return
            if not isinstance(value, (str, bytes)):
                return
            target = os.path.abspath(os.fsdecode(value))
            if target == self.root or target.startswith(self.root + os.sep):
                self.rows.append({'event': event, 'seam': 'absolute_audit'})
        except Exception:
            # An observer failure is checked afterwards; it must not change the
            # product operation or inject a replacement failure/result.
            self.observer_errors.append('qa_source_audit_observer_failed')

    def positive_control(self, source):
        missing_name = 'qa40-never-created-source-control.json'
        check(not (source.parent / missing_name).exists(), 'qa_missing_source_control_not_fresh')
        self.rows = []
        self._start()
        try:
            with io.open(source, 'rb') as stream:
                stream.read(1)
            fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                os.read(fd, 1)
            finally:
                os.close(fd)
            parent = os.open(source.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK)
            try:
                fd = os.open(source.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
                os.close(fd)
                try:
                    os.open(missing_name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
                except FileNotFoundError as error:
                    check(error.errno == 2 and error.filename == missing_name, 'qa_source_missing_error_changed')
                else:
                    check(False, 'qa_missing_source_unexpectedly_opened')
            finally:
                os.close(parent)
        finally:
            self._stop()
        check(sum(row['event'] == 'open' for row in self.rows) == 5,
              'qa_source_audit_positive_control_failed')
        check(not self.observer_errors, 'qa_source_audit_observer_failed')
        result = {'open_events': 5, 'absolute_file_controls': 2, 'directory_control': 1,
                  'relative_dir_fd_file_control': 1, 'events': list(self.rows),
                  'relative_dir_fd_missing_attempt': 1, 'original_file_not_found_errno': 2,
                  'missing_file_created_or_deleted': False,
                  'relative_seam_recorded': any(row.get('seam') == 'relative_dir_fd' for row in self.rows),
                  'separate_from_product_window': True, 'original_open_restored': os.open is self.original_open}
        check(result['relative_seam_recorded'], 'qa_source_relative_dir_fd_positive_control_failed')
        check(sum(row.get('seam') == 'relative_dir_fd' for row in self.rows) == 2,
              'qa_source_missing_attempt_not_observed')
        self.rows = []
        return result

    def begin_product(self):
        check(not self.active and not self.rows, 'qa_source_audit_window_not_clean')
        self._start()

    def end_product(self):
        self._stop()
        result = {'event_count': len(self.rows), 'events': list(self.rows),
                  'observer_error_count': len(self.observer_errors),
                  'coverage': 'absolute CPython open/directory-read plus original relative os.open dir_fd/inode; generic stat not audited',
                  'result_replacement_or_failure_injection': False,
                  'original_open_restored': os.open is self.original_open}
        self.rows = []
        return result


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--source-kind', choices=('offline_fixture', 'observed_calibration'), required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    evidence = {'schema_version': 'qa40-desktop-host-v2', 'synthetic': True,
        'fixture_pid': os.getpid(),
        'scope': 'Real Core/Budget/Store/Identity/Queue/Health and loopback; MockTransport only',
        'observed_schema_is_actual_observation': False, 'timeouts': TIMEOUTS,
        'cases': [], 'state_reports': [], 'failure_code': None}
    boundary, stack = LoopbackEvidence(), ExitStack()
    configure_loopback_network(boundary, setattr)
    clock = Clock(datetime.fromisoformat('2026-10-21T00:00:00+00:00').timestamp())
    server = ThreadedServer(('127.0.0.1', 0), QuietHandler)
    boundary.ports.add(server.server_port)
    origin = f'http://127.0.0.1:{server.server_port}'
    wire = ArtifactRecorder(output, 'upstream')
    controlled = thread = tracer = None
    snapshot_state = None
    read_baseline = None
    state_number = 0
    cases = []
    try:
        controlled = build_resilience_health_host(output / 'unused-original-service', clock=clock,
            origin=origin, held_timeout_seconds=600)
        tracer = TaskFeaturesHTTP(controlled, origin, clock, stack, output, evidence, wire_recorder=wire)
        tracer.server = server
        def short_snapshot(values):
            from datetime import timezone
            values['plan']['snapshot']['valid_until'] = datetime.fromtimestamp(clock()+2, timezone.utc).isoformat()
        source, values = calibrated_contracts(tracer, economic='insufficient', data_kind=args.source_kind,
            plan_patch=short_snapshot)
        controlled = tracer.controlled
        # The helper changes only the fixture data factory while assembling a
        # fresh host. Actual constructors/guards/QueuePreparation already ran.
        check(controlled.held_timeout_seconds == 30, 'qa_review_helper_timeout_drift')
        check(controlled.flags['hold'] is False, 'qa_unexpected_held_model')
        feature_configuration = {'policy_bundle': {**POLICY_BUNDLE, 'feature_version': 'local-deterministic-v2'},
                                 'binding_port': StoreFeatureBindingPort(controlled.app.store)}
        service = controlled.host.service
        recorder = tracer.http_recorder

        def state(label):
            nonlocal state_number
            state_number += 1
            root = controlled.host.root
            budgets = {lane: ledger.report() for lane, ledger in controlled.budgets.items()}
            report = save(output / ('state-%03d-budgets.json' % state_number), budgets)
            evidence['state_reports'].append({'label': label, **report})
            return {'model_calls': len(controlled.requests),
                'model_methods': [row['method'] for row in controlled.requests],
                'model_metadata_sha256': digest(controlled.requests),
                'source_calls': len(controlled.side_requests['source']),
                'accounting_calls': len(controlled.side_requests['accounting']),
                'upstream_reference_count': len(wire.rows),
                'complete_budget_report_sha256': digest(budgets),
                'budget_sqlite_sha256': {lane: sha(ledger.path) for lane, ledger in controlled.budgets.items()},
                'health_sha256': sha(controlled.host.health.path),
                'queue_sha256': sha(controlled.queue.path),
                'capacity_sha256': sha(controlled.controller.store.path),
                'monitor_sha256': sha(root / 'monitor.sqlite'),
                'identity_sha256': sha(root / 'identity.sqlite'),
                'raw_inventory_sha256': {str(p.relative_to(root)): sha(p) for p in sorted(root.rglob('*'))
                    if p.is_file() and p.suffix in {'.json', '.bin'}},
                'calibration_source_sha256': sha(source),
                'mock_hold_started': controlled.held.is_set(),
                'private_prompt_canary_present': any(b'PRIVATE_RECOMMENDATION_DESKTOP_CANARY' in p.read_bytes()
                    for p in (root / 'tasks').glob('http-*/job.json'))}

        snapshot_state = state

        def app(environ, start_response):
            # Copy the actual finite request bytes without modifying their value.
            # No request headers/query/token values are placed in metadata/IPC.
            length = int(environ.get('CONTENT_LENGTH') or 0)
            check(0 <= length <= 131072, 'qa_request_capture_bound')
            request_body = environ['wsgi.input'].read(length) if length else b''
            check(len(request_body) == length, 'qa_request_body_short')
            request_environ = dict(environ)
            request_environ['wsgi.input'] = io.BytesIO(request_body)
            row = {'method': environ.get('REQUEST_METHOD'), 'path': environ.get('PATH_INFO'),
                   'authorization_recorded_in_metadata': False}
            def started(status, headers, exc_info=None):
                row['status'] = int(status.split()[0])
                return start_response(status, headers, exc_info)
            iterator = None
            try:
                iterator = controlled.app(request_environ, started)
                chunks = list(iterator)
                response_body = b''.join(chunks)
                check(len(response_body) <= 4194304, 'qa_response_capture_bound')
            except BaseException:
                recorder.capture({**row, 'response_missing': True}, {'request_body': request_body})
                raise
            finally:
                if iterator is not None and hasattr(iterator, 'close'):
                    iterator.close()
            recorder.capture(row, {'request_body': request_body, 'response_body': response_body})
            return chunks
        server.set_app(app)
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01},
                                  daemon=True, name='recommendation-desktop-loopback-http')
        thread.start()
        with httpx.Client(base_url=origin, trust_env=False, timeout=5) as client:
            operator = {'Authorization': 'Bearer ' + controlled.operator_token}
            agent = {'Authorization': 'Bearer ' + controlled.credentials['agent'].token}
            cap = client.get('/monitor/capacity', headers=operator)
            check(cap.status_code == 200, 'qa_capacity_read_failed')
            activated = client.post('/monitor/capacity/activate', headers=operator, json={
                'revision': cap.json()['dispatch']['revision'], 'version': controlled.policy_version})
            check(activated.status_code == 200, 'qa_capacity_activation_failed')

            def public_safe(value):
                raw = json.dumps(value, ensure_ascii=False, sort_keys=True)
                private = [*CANARY.split(), UNSUPPORTED_KIND, str(source), str(source.parent),
                           str(controlled.host.root), controlled.operator_token,
                           *(credential.token for credential in controlled.credentials.values())]
                check(all(value not in raw for value in private), 'qa_private_http_projection_exposed')

            def source_inventory():
                # Called only while SourceRootAudit.active is False.
                return {str(p.relative_to(source.parent)): sha(p)
                        for p in sorted(source.parent.rglob('*')) if p.is_file()}

            def submit(run, expected, *, dispatch=False):
                response = client.post('/v1/tasks', headers=agent, json={
                    'run_id': run, 'category': controlled.category, 'prompt': '中文1+1 ' + CANARY})
                check(response.status_code == 202, 'qa_original_task_not_accepted')
                public_safe(response.json())
                if dispatch:
                    check(controlled.queue.dispatch_next() is not None, 'qa_original_dispatch_missing')
                response = client.get('/v1/tasks/' + run, headers=agent)
                check(response.status_code == (200 if dispatch else 202), 'qa_original_task_read_failed')
                task = response.json()
                public_safe(task)
                check(task['status'] == ('passed' if dispatch else 'queued'), 'qa_original_task_status_failed')
                rec = task.get('routing_recommendation')
                check(isinstance(rec, dict), 'qa_client_recommendation_missing')
                check(all('comparison' not in row for row in rec.get('suggestions', [])),
                      'qa_client_comparison_exposed')
                snapshot = client.get('/monitor/snapshot', headers=operator)
                check(snapshot.status_code == 200, 'qa_operator_snapshot_read_failed')
                public_safe(snapshot.json())
                matches = [row for row in snapshot.json()['tasks']
                           if row['run'] == run and row['client'] == 'agent']
                check(len(matches) == 1, 'qa_operator_task_not_unique')
                operator_rec = matches[0]['operator_observation']['routing_recommendation']
                compact = dict(operator_rec)
                if 'suggestions' in operator_rec:
                    compact['suggestions'] = [{key: row[key] for key in ('kind', 'economics', 'reasons')}
                                              for row in operator_rec['suggestions']]
                check(rec == compact, 'qa_client_operator_summary_mismatch')
                if expected == 'legacy':
                    check(rec == NOT_PROVIDED and operator_rec == NOT_PROVIDED,
                          'qa_legacy_gained_advice')
                elif expected == 'unavailable':
                    check(rec == UNAVAILABLE and operator_rec == UNAVAILABLE,
                          'qa_capture_off_not_compact_unavailable')
                elif expected == 'unknown':
                    check(rec['adopted'] is False and rec['status'] == 'unknown', 'qa_unknown_advice_failed')
                    check(any(item['kind'] == 'observe_original_route' for item in rec['suggestions']), 'qa_original_observation_missing')
                elif expected == 'exact':
                    check(rec['adopted'] is False and rec['status'] == 'available', 'qa_exact_advice_failed')
                    # Full comparison is operator-only actual HTTP data.
                    row = next(item for item in operator_rec['suggestions']
                               if item['kind'] == 'compare_skipping_classification')
                    check(row['economics'] == 'proven_insufficient', 'qa_exact_economics_failed')
                    check(row['comparison']['source_kind'] == args.source_kind, 'qa_exact_source_kind_failed')
                    check(row['comparison']['conditional_scopes'] == 3, 'qa_exact_scope_failed')
                    check(row['comparison']['net_saving_usd'] == {'lower': '-1.919', 'upper': '-1.919'}, 'qa_exact_literal_interval_failed')
                    check(row['comparison']['source_sha256'] == [sha(source)]
                          and row['comparison']['distribution_is_answer_probability'] is False,
                          'qa_exact_source_binding_failed')
                    check(any(item['kind'] == 'need_capability_context_evidence' and len(item['reasons']) >= 2
                              for item in rec['suggestions']), 'qa_context_reasons_missing')
                elif expected == 'not_applicable':
                    check(rec['adopted'] is False and rec['status'] == 'not_applicable', 'qa_original_mode_advice_failed')
                elif expected == 'unsupported':
                    check(rec['adopted'] is False and rec['status'] == 'unsupported'
                          and rec['suggestions'][0]['reasons'] == [UNSUPPORTED_REASON],
                          'qa_unsupported_source_kind_safe_code_failed')
                    check(all('comparison' not in row for row in operator_rec['suggestions']),
                          'qa_unsupported_gained_comparison')
                elif expected == 'candidate_expired':
                    check(rec['status'] == 'unknown' and rec['adopted'] is False
                          and rec['suggestions'][0]['reasons'] == ['candidate_evidence_expired_at_acceptance'],
                          'qa_expired_candidate_reason_failed')
                    check(all('comparison' not in row for row in operator_rec['suggestions']),
                          'qa_expired_candidate_comparison_claimed')
                cases.append({'run': run, 'expected': expected, 'completed': dispatch})
                evidence.setdefault('saved_public_projections', []).append({
                    'run': run, 'client_routing_recommendation': rec,
                    'operator_routing_recommendation': operator_rec})

            if args.source_kind == 'offline_fixture':
                service.creation_recommendations = None
                service.creation_features = feature_configuration
                submit('qa40-features-complete', 'legacy', dispatch=True)
                service.creation_features = None
                submit('qa40-features-legacy', 'unavailable')
                service.creation_features = feature_configuration
                submit('qa40-recommendation-legacy', 'legacy')
                enable_recommendations(tracer)
                submit('qa40-unknown', 'unknown')
                source_audit = SourceRootAudit(source.parent)
                positive = source_audit.positive_control(source)
                before_unsupported = source_inventory()
                # Observer records real I/O; it never blocks or substitutes data.
                source_audit.begin_product()
                try:
                    enable_recommendations(tracer, calibration_sources={
                        'data_kind': UNSUPPORTED_KIND, 'public_root': str(source.parent)})
                    submit('qa40-unsupported', 'unsupported')
                finally:
                    observed = source_audit.end_product()
                    evidence['unsupported_source_audit'] = {
                        'positive_control': positive, 'product_config_post_get': observed,
                        'source_root_sha256': hashlib.sha256(str(source.parent).encode()).hexdigest(),
                        'inventory_and_hash_verifier_outside_product_window': True}
                check(observed['event_count'] == 0, 'qa_unsupported_source_root_io_detected')
                check(observed['observer_error_count'] == 0, 'qa_source_audit_observer_failed')
                check(source_inventory() == before_unsupported, 'qa_unsupported_source_bytes_changed')
                enable_recommendations(tracer, calibration_sources={'data_kind': args.source_kind,
                                                                   'public_root': str(source.parent)})
                submit('qa40-exact', 'exact')
                snapshot = client.get('/monitor/snapshot', headers=operator)
                check(snapshot.status_code == 200, 'qa_mode_revision_read_failed')
                changed = client.post('/monitor/mode', headers=operator, json={
                    'revision': snapshot.json()['control']['revision'], 'mode': 'open_source_first'})
                check(changed.status_code == 200, 'qa_original_mode_setup_failed')
                submit('qa40-not-applicable', 'not_applicable')
                snapshot = client.get('/monitor/snapshot', headers=operator)
                check(snapshot.status_code == 200, 'qa_mode_restore_revision_failed')
                changed = client.post('/monitor/mode', headers=operator, json={
                    'revision': snapshot.json()['control']['revision'], 'mode': 'cloudyfan'})
                check(changed.status_code == 200, 'qa_original_mode_restore_failed')
                clock.now += 3
                submit('qa40-candidate-expired', 'candidate_expired')
            else:
                submit('qa40-observed-schema-synthetic', 'exact')
        evidence['cases'] = cases
        initial = state('ready-before-browser')
        check(initial['model_calls'] == (1 if args.source_kind == 'offline_fixture' else 0), 'qa_unexpected_setup_model_calls')
        check(initial['source_calls'] == initial['accounting_calls'] == 0, 'qa_setup_side_call_detected')
        check(initial['mock_hold_started'] is False, 'qa_setup_held_model_detected')
        evidence['ready_state'] = initial
        print(json.dumps({'type': 'ready', 'origin': origin, 'fixture_pid': os.getpid(),
            'synthetic': True, 'source_kind': args.source_kind, 'cases': cases, 'state': initial,
            'timeouts': TIMEOUTS, 'observed_schema_is_actual_observation': False}), flush=True)
        # Read-only commands only. No submissions, dispatch, mode or hold release
        # are available after ready; the browser sees only real saved host data.
        for line in sys.stdin:
            command = json.loads(line).get('command')
            if command == 'stop':
                break
            check(command in {'baseline', 'state'}, 'qa_unknown_private_command')
            current = state(command)
            if command == 'baseline':
                check(read_baseline is None, 'qa_baseline_already_frozen')
                read_baseline = current
                evidence['read_baseline'] = current
            print(json.dumps({'type': command, 'state': current}), flush=True)
    except BaseException as error:
        evidence['failure_code'] = error.args[0] if isinstance(error, AssertionError) and error.args and str(error.args[0]).startswith('qa_') else 'qa_host_setup_or_control_failed'
        raise RuntimeError(evidence['failure_code']) from None
    finally:
        if thread is not None and thread.is_alive():
            server.shutdown()
            thread.join(2)
        server.server_close()
        stack.close()
        if controlled is not None:
            controlled.close()
        if tracer is not None:
            if snapshot_state is not None:
                try:
                    evidence['final_state'] = snapshot_state('after-own-close')
                    evidence['readonly_state_unchanged'] = read_baseline is not None and evidence['final_state'] == read_baseline
                except BaseException:
                    evidence['readonly_state_unchanged'] = False
                    evidence['final_state_failure_code'] = 'qa_final_state_unavailable'
            evidence['http_responses'] = tracer.http_recorder.rows
            evidence['upstream_wire'] = wire.rows
            evidence['artifact_verification'] = {'http': tracer.http_recorder.verify(), 'upstream': wire.verify()}
        evidence['blocked_socket_attempt_count'] = len(boundary.blocked)
        evidence['socket_attempts'] = [{'host': host, 'port': port} for host, port in boundary.connected]
        evidence['cleanup'] = {'server_thread_exited': thread is None or not thread.is_alive(),
            'server_socket_closed': server.socket.fileno() == -1, 'fixture_close_completed': controlled is not None}
        save(output / 'fixture-receipt.json', evidence)
        # A setup/HTTP oracle failure already raised its fixed original code.
        # A missing baseline must not replace it with a later cleanup assertion.
        if evidence['failure_code'] is None:
            check(evidence['cleanup']['server_thread_exited'], 'qa_owned_server_thread_not_closed')
            check(not boundary.blocked, 'qa_network_guard_failed')
            check(evidence.get('readonly_state_unchanged') is True, 'qa_readonly_state_changed_or_not_measured')
            check(all(item['status'] == 'verified' for item in evidence.get('artifact_verification', {}).values()), 'qa_artifact_capture_failed')


if __name__ == '__main__':
    main()

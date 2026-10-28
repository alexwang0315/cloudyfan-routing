"""Operator evidence loading and immutable reports, separate from task execution.

Only a bounded value DTO crosses into the Core policy comparator. The short-lived
loader process receives paths/values, never a service, model client or callback.
"""
from contextlib import contextmanager
from decimal import Decimal, localcontext
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import resource
import socket
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import uuid

POLICY = 'original-policy-reproduction-v1'
COMPUTATION = 'saved-original-policy-computation-v1'
SCHEMA = 'saved-policy-comparison-v1'
LIMITS = {'max_tasks': 20, 'max_source_files': 256, 'max_source_bytes': 4194304,
          'max_source_file_bytes': 1048576, 'max_candidate_configurations': 12,
          'max_decision_points': 9, 'max_output_bytes': 262144,
          'max_policy_compute_milliseconds': 250, 'worker_cpu_seconds': 2,
          'worker_wall_seconds': 5}
TIERS = ('open_source', 'low', 'mid', 'high')
MODES = ('cloudyfan', 'fixed_high', 'low_first', 'open_source_first')
HASH = re.compile(r'[a-f0-9]{64}')
IDENTIFIER = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}')
RUN = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}')
COMPARISON = re.compile(r'cmp-[a-f0-9]{32}')
STATUSES = {'passed', 'failed', 'unverifiable', 'ready', 'running', 'bound', 'queued',
            'expired', 'retired', 'engineering_interrupted', 'accounting_interrupted',
            'policy_denied', 'capacity_denied'}
STOPS = {'final', 'passed', 'validation_failed', 'validation_unknown', 'provider_rate_limited',
         'provider_unavailable', 'unknown_charge', 'turn_limit', 'tool_limit',
         'context_limit', 'time_limit', 'policy_denied', 'none'}


def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False,
                      separators=(',', ':')).encode()


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def original_digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    allow_nan=False).encode()).hexdigest()


def identifier(value):
    return value if isinstance(value, str) and IDENTIFIER.fullmatch(value) else None


def finite(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


class SourceUnavailable(Exception):
    pass


def fail(code, status=422):
    from .store import MonitorError
    raise MonitorError(code, status)


def strict_json(body):
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise SourceUnavailable('source_json_invalid')
            value[key] = item
        return value
    try:
        return json.loads(body, object_pairs_hook=unique,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError, RecursionError):
        raise SourceUnavailable('source_json_invalid') from None


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink,
            info.st_size, info.st_mtime_ns, info.st_ctime_ns)


@contextmanager
def _parent(path):
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts:
        raise SourceUnavailable('source_path_unsafe')
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK
    descriptor = os.open('/', flags)
    try:
        for part in path.parts[1:-1]:
            child = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        yield descriptor, path.name
    finally:
        os.close(descriptor)


def _read(path, maximum):
    with _parent(path) as (parent, name):
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        try:
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > maximum:
                raise SourceUnavailable('source_file_unsafe_or_oversized')
            parts, size = [], 0
            while size <= maximum:
                part = os.read(descriptor, min(65536, maximum + 1 - size))
                if not part:
                    break
                parts.append(part)
                size += len(part)
            if (size != before.st_size or size > maximum
                    or _identity(before) != _identity(os.fstat(descriptor))
                    or _identity(before) != _identity(os.stat(name, dir_fd=parent, follow_symlinks=False))):
                raise SourceUnavailable('source_changed_during_read')
            return b''.join(parts)
        finally:
            os.close(descriptor)


@contextmanager
def _shared(path):
    with _parent(path) as (parent, name):
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise SourceUnavailable('source_lock_unsafe')
            try:
                fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                raise SourceUnavailable('source_busy') from None
            yield
        finally:
            os.close(descriptor)


@contextmanager
def _database(path, maximum=1048576, *, capture=None, source_limit=None):
    # SQLite must never open a live pathname after a separate symlink check.
    # Capture main DB + WAL through the same NOFOLLOW parent/leaf descriptors,
    # checking both generations before/after. SQLite reads only private copies;
    # its normal WAL reader determines the committed prefix. No immutable=1,
    # original SHM write, backup API or current-source refresh is involved.
    limit = LIMITS['max_source_bytes'] if source_limit is None else source_limit
    with _parent(Path(path).absolute()) as (parent, name), tempfile.TemporaryDirectory(prefix='cloudyfan-comparison-') as temporary:
        opened, states, captured = {}, {}, {}
        try:
            for suffix in ('', '-wal'):
                try:
                    descriptor = os.open(name + suffix, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
                except FileNotFoundError:
                    if suffix == '':
                        raise
                    states[suffix] = None
                    continue
                opened[suffix] = descriptor
                info = os.fstat(descriptor)
                if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                        or info.st_size > min(limit, LIMITS['max_source_file_bytes'])):
                    raise SourceUnavailable('source_database_unsafe_or_oversized')
                states[suffix] = _identity(info)
            total = sum(state[4] for state in states.values() if state is not None)
            if total > limit:
                raise SourceUnavailable('source_work_limit')
            for suffix, descriptor in opened.items():
                body = bytearray()
                while len(body) <= limit:
                    part = os.read(descriptor, min(65536, limit + 1 - len(body)))
                    if not part:
                        break
                    body.extend(part)
                if len(body) != states[suffix][4] or len(body) > limit:
                    raise SourceUnavailable('source_database_changed')
                captured[suffix] = bytes(body)
            for suffix in ('', '-wal'):
                try:
                    current = _identity(os.stat(name + suffix, dir_fd=parent, follow_symlinks=False))
                except FileNotFoundError:
                    current = None
                if current != states[suffix] or (suffix in opened and _identity(os.fstat(opened[suffix])) != current):
                    raise SourceUnavailable('source_database_changed')
            snapshot = Path(temporary) / 'snapshot.sqlite'
            for suffix, body in captured.items():
                target = Path(str(snapshot) + suffix)
                with target.open('xb') as stream:
                    os.fchmod(stream.fileno(), 0o600)
                    stream.write(body)
                if capture is not None:
                    capture('original_database_wal' if suffix else 'original_database_snapshot', body)
            connection = sqlite3.connect(snapshot.as_uri() + '?mode=ro', uri=True, timeout=0, isolation_level=None)
            connection.row_factory = sqlite3.Row
            try:
                connection.execute('PRAGMA query_only=ON')
                connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, maximum)
                steps = 0
                def bounded():
                    nonlocal steps
                    steps += 1000
                    return steps >= 32000
                connection.set_progress_handler(bounded, 1000)
                connection.execute('BEGIN')
                yield connection
                connection.rollback()
            finally:
                connection.close()
        finally:
            for descriptor in opened.values():
                os.close(descriptor)


def _task_id(client, run):
    return 'http-' + hashlib.sha256(json.dumps([client, run], sort_keys=True,
        ensure_ascii=False, allow_nan=False).encode()).hexdigest()[:48]


def _scope(body):
    if not isinstance(body, dict) or set(body) != {'policy_version', 'tasks'}:
        fail('unsupported_comparison_fields')
    if body['policy_version'] != POLICY:
        fail('comparison_policy_not_registered', 409)
    tasks = body['tasks']
    if not isinstance(tasks, list) or not 1 <= len(tasks) <= LIMITS['max_tasks']:
        fail('comparison_scope_invalid')
    seen, result = set(), []
    for task in tasks:
        if (not isinstance(task, dict) or set(task) != {'client', 'run_id'}
                or identifier(task['client']) is None or not isinstance(task['run_id'], str)
                or RUN.fullmatch(task['run_id']) is None):
            fail('comparison_scope_invalid')
        key = task['client'], task['run_id']
        if key in seen:
            fail('comparison_scope_invalid')
        seen.add(key)
        result.append(dict(task))
    return sorted(result, key=lambda row: (row['client'], row['run_id']))


def _unknown(task, reason, original=None, evidence=None):
    return {'task': dict(task), 'original': original or {
        'task_status': 'unknown', 'mode': None, 'category': None, 'pool_version': None,
        'contract_id': None, 'contract_version': None, 'routing_version': None, 'created_at': None,
        'path': [], 'costs': {'known_api_usd': None, 'complete_api_usd': None,
            'unknown_events': None, 'customer_usage_charge': _customer_unprovided()}},
        'reproduction': {'status': 'not_evaluable', 'scope': 'decision_from_saved_inputs',
            'adopted': False, 'reasons': [reason], 'decisions': [], 'financial_admissibility': 'not_replayed'},
        'evidence': evidence or []}


def _customer_unprovided():
    return {'status': 'not_provided', 'known_usd': None, 'complete_usd': None,
            'unknown_events': None, 'basis': 'original_customer_ledger'}


class _Sources:
    def __init__(self):
        self.bytes = self.files = 0
        self.evidence = []
        self.limited = False

    def record(self, kind, body):
        if self.bytes + len(body) > LIMITS['max_source_bytes'] or self.files + 1 > LIMITS['max_source_files']:
            self.limited = True
            raise SourceUnavailable('source_work_limit')
        self.bytes += len(body)
        self.files += 1
        row = {'kind': kind, 'sha256': hashlib.sha256(body).hexdigest(), 'bytes': len(body)}
        self.evidence.append(row)
        return row

    def read(self, path, kind):
        body = _read(path, min(LIMITS['max_source_file_bytes'], LIMITS['max_source_bytes'] - self.bytes))
        self.record(kind, body)
        return strict_json(body)

    def rows(self, path, query, params=(), *, kind, maximum=1048576, max_rows=66):
        remaining = LIMITS['max_source_bytes'] - self.bytes
        if remaining < maximum or self.files >= LIMITS['max_source_files']:
            self.limited = True
            raise SourceUnavailable('source_work_limit')
        with _database(path, min(maximum, remaining), capture=self.record, source_limit=remaining) as db:
            cursor = db.execute(query, params)
            result = [dict(row) for row in cursor.fetchmany(max_rows + 1)]
            if len(result) > max_rows:
                raise SourceUnavailable('source_work_limit')
        self.record(kind, encoded(result))
        return result


class _Reports:
    def __init__(self, store_path):
        self.root = Path(store_path).absolute().parent / '.saved-comparisons'

    def prepare(self):
        if self.root != self.root.resolve():
            raise SourceUnavailable('comparison_store_unsafe')
        self.root.mkdir(mode=0o700, exist_ok=True)
        if not stat.S_ISDIR(self.root.lstat().st_mode) or stat.S_IMODE(self.root.lstat().st_mode) & 0o077:
            raise SourceUnavailable('comparison_store_unsafe')

    def write(self, name, value, maximum):
        body = encoded(value)
        if len(body) > maximum:
            raise SourceUnavailable('comparison_output_limit')
        with _parent(self.root / name) as (parent, filename):
            descriptor = os.open(filename, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                 0o600, dir_fd=parent)
            with os.fdopen(descriptor, 'wb') as stream:
                if stream.write(body) != len(body):
                    raise OSError('comparison_short_write')
                stream.flush()
                os.fsync(stream.fileno())
            os.fsync(parent)
        return hashlib.sha256(body).hexdigest()

    def result(self, comparison_id):
        if not isinstance(comparison_id, str) or COMPARISON.fullmatch(comparison_id) is None:
            fail('comparison_not_found', 404)
        try:
            request_body = _read(self.root / (comparison_id + '.request.json'), 16384)
        except FileNotFoundError:
            fail('comparison_not_found', 404)
        try:
            seal = strict_json(_read(self.root / (comparison_id + '.seal.json'), 16384))
            body = _read(self.root / (comparison_id + '.report.json'), LIMITS['max_output_bytes'])
            inputs = _read(self.root / (comparison_id + '.inputs.json'), LIMITS['max_source_bytes'])
            report = strict_json(body)
            if (set(seal) != {'request_sha256', 'report_sha256', 'inputs_sha256', 'comparison_id'}
                    or seal['comparison_id'] != comparison_id or seal['request_sha256'] != hashlib.sha256(request_body).hexdigest()
                    or seal['report_sha256'] != hashlib.sha256(body).hexdigest()
                    or seal['inputs_sha256'] != hashlib.sha256(inputs).hexdigest()
                    or report.get('data_sha256') != digest(strict_json(inputs))
                    or report.get('comparison_id') != comparison_id or report.get('schema_version') != SCHEMA):
                raise SourceUnavailable('comparison_evidence_invalid')
            return report
        except FileNotFoundError:
            fail('comparison_report_not_ready', 409)
        except (OSError, ValueError, SourceUnavailable, TypeError, KeyError):
            fail('comparison_evidence_unavailable', 503)


def catalog(app):
    try:
        with _database(app.store.path, 32768) as db:
            rows = [dict(row) for row in db.execute('SELECT client,run,mode,category,created FROM bindings '
                'ORDER BY created DESC,client,run LIMIT 101')]
        tasks = [{'client': row['client'], 'run_id': row['run'], 'mode': row['mode'],
                  'category': row['category'], 'created_at': row['created']} for row in rows[:100]
                 if identifier(row['client']) and isinstance(row['run'], str) and RUN.fullmatch(row['run'])
                 and row['mode'] in MODES and identifier(row['category']) and finite(row['created'])]
        reports = _Reports(app.store.path)
        saved = []
        if reports.root.exists():
            if reports.root != reports.root.resolve() or not reports.root.is_dir():
                raise SourceUnavailable('comparison_store_unsafe')
            with os.scandir(reports.root) as entries:
                for number, entry in enumerate(entries):
                    if number >= 1000 or len(saved) >= 100:
                        break
                    if not re.fullmatch(r'cmp-[a-f0-9]{32}\.request\.json', entry.name):
                        continue
                    request = strict_json(_read(reports.root / entry.name, 16384))
                    if (not isinstance(request, dict) or not finite(request.get('created_at'))
                            or not isinstance(request.get('comparison_id'), str)
                            or COMPARISON.fullmatch(request['comparison_id']) is None
                            or entry.name != request['comparison_id'] + '.request.json'):
                        raise SourceUnavailable('comparison_evidence_invalid')
                    _scope({'policy_version': request.get('policy_version'), 'tasks': request.get('tasks')})
                    status = 'incomplete'
                    if (reports.root / (request['comparison_id'] + '.seal.json')).exists():
                        status = reports.result(request['comparison_id'])['status']
                    saved.append({'comparison_id': request['comparison_id'], 'created_at': request['created_at'],
                        'status': status, 'policy_version': POLICY, 'requested_tasks': len(request['tasks'])})
        return {'schema_version': 'saved-policy-comparison-catalog-v1',
                'registered_policies': [{'version': POLICY, 'label': '重現原保存政策'}],
                'limits': dict(LIMITS), 'task_scopes': tasks, 'task_catalog_incomplete': len(rows) > 100,
                'reports': sorted(saved, key=lambda row: (row['created_at'], row['comparison_id']), reverse=True)}
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, SourceUnavailable):
        fail('comparison_store_unavailable', 503)


def get_report(app, comparison_id):
    try:
        return _Reports(app.store.path).result(comparison_id)
    except (OSError, SourceUnavailable):
        fail('comparison_evidence_unavailable', 503)


def _source_context(app, tasks, now):
    from cloudyfan_jev.budget import BudgetedCore
    from cloudyfan_jev.engine import Core
    from cloudyfan_jev.http_api import RegisteredContract
    from cloudyfan_jev.identity import IdentityStore
    from cloudyfan_jev.scores.backup_runtime import BackupCategoryCore
    from cloudyfan_jev.scores.category_routing import CategoryCore
    from .integration import RunBoundCore
    contracts = {}
    with _database(app.store.path, 32768) as db:
        needed = {row['contract'] for task in tasks for row in db.execute(
            'SELECT contract FROM bindings WHERE client=? AND run=?', (task['client'], task['run_id']))}
    for key in needed:
        contract = app.gateway.service.contracts.get(key)
        if type(contract) is not RegisteredContract or type(contract.runner) is not BudgetedCore:
            continue
        core = contract.runner.core
        template = core._template if type(core) is RunBoundCore else core
        if type(template) not in (Core, BackupCategoryCore, CategoryCore):
            continue
        descriptor = contract.descriptor()
        # Do not turn a later local symlink into a new financial authority by
        # using descriptor().resolve() output. The loader opens these original
        # host paths with NOFOLLOW and checks the saved job descriptor itself.
        descriptor['receipt_root'] = str(Path(template.client.root).absolute())
        descriptor['budget_store'] = str(Path(contract.runner.store.path).absolute())
        contracts[key] = {'registered': descriptor,
            'health_path': str(template.health_store.path) if type(template) is BackupCategoryCore else None}
    service = app.gateway.service
    return {'monitor_path': str(app.store.path.absolute()), 'tasks_root': str(service.root.absolute()),
            'retention_path': str(service.retention.path.absolute()) if service.retention is not None else None,
            'retention_policy': service.retention_policy.descriptor() if service.retention_policy is not None else None,
            'identity_path': str(app.identity_store.path.absolute()) if app.identity_store is not None else None,
            'allowed_principals': sorted(service.allowed_principals), 'contracts': contracts,
            'permission_reader_known': (app.identity_store is not None
                and getattr(service.principal_active, '__self__', None) is app.identity_store
                and getattr(service.principal_active, '__func__', None) is IdentityStore.is_active),
            'capacity_path': str(app.capacity_controller.store.path.absolute()) if app.capacity_controller is not None else None,
            'tasks': tasks, 'now': now}


def _report(request, outcome):
    items = outcome['items']
    counts = {key: 0 for key in ('evaluated', 'reproduced', 'partial', 'unknown', 'not_evaluable',
        'not_provided', 'original_failed', 'original_interrupted', 'unknown_fee_tasks')}
    counts['requested'] = len(request['tasks'])
    for item in items:
        counts[item['reproduction']['status']] += 1
        counts['evaluated'] += int(item['reproduction']['status'] in ('reproduced', 'partial', 'unknown'))
        counts['original_failed'] += int(item['original']['task_status'] == 'failed')
        counts['original_interrupted'] += int(item['original']['task_status'] in ('engineering_interrupted', 'accounting_interrupted'))
        costs = item['original']['costs']
        counts['unknown_fee_tasks'] += int(costs['unknown_events'] is None or costs['unknown_events'] > 0)
    result_sha = digest({'items': items, 'tasks': request['tasks'], 'frozen_at': request['frozen_at'],
        'data_sha256': outcome['data_sha256'], 'policy_sha256': request['policy_sha256'],
        'limits': LIMITS, 'policy_version': POLICY, 'computation_version': COMPUTATION})
    return {'schema_version': SCHEMA, 'comparison_id': request['comparison_id'],
        'status': 'incomplete' if outcome.get('incomplete') else 'completed',
        'policy_version': POLICY, 'computation_version': COMPUTATION,
        'created_at': request['created_at'], 'frozen_at': request['frozen_at'],
        'data_sha256': outcome['data_sha256'], 'policy_sha256': request['policy_sha256'],
        'result_sha256': result_sha, 'scope': {'tasks': request['tasks']}, 'limits': dict(LIMITS),
        'counts': counts, 'items': items}


def create_report(app, body):
    tasks = _scope(body)
    now = app.store.clock()
    if not finite(now):
        fail('comparison_clock_unavailable', 503)
    reports = _Reports(app.store.path)
    try:
        reports.prepare()
        lock = os.open(reports.root / '.create.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(lock).st_mode) or os.fstat(lock).st_nlink != 1:
                raise SourceUnavailable('comparison_lock_unsafe')
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                fail('comparison_busy', 409)
            comparison_id = 'cmp-' + uuid.uuid4().hex
            import cloudyfan_jev.saved_policy_comparison as policy_module
            implementation = {'policy': POLICY, 'computation': COMPUTATION,
                'core_policy_sha256': hashlib.sha256(_read(Path(policy_module.__file__).absolute(), 1048576)).hexdigest(),
                'host_loader_sha256': hashlib.sha256(_read(Path(__file__).absolute(), 1048576)).hexdigest()}
            request = {'comparison_id': comparison_id, 'created_at': now, 'frozen_at': now,
                       'tasks': tasks, 'policy_version': POLICY, 'implementation': implementation,
                       'policy_sha256': digest(implementation)}
            request_sha = reports.write(comparison_id + '.request.json', request, 16384)
            context = _source_context(app, tasks, now)
            control = encoded(context)
            if len(control) > LIMITS['max_source_bytes']:
                raise SourceUnavailable('comparison_control_oversized')
            import cloudyfan_jev
            bootstrap = ('import runpy,sys;sys.path[:0]=sys.argv[1:3];'
                         'sys.argv=sys.argv[3:];runpy.run_path(sys.argv[0],run_name="__main__")')
            argv = [sys.executable, '-I', '-S', '-c', bootstrap, str(Path(__file__).parents[1]),
                    str(Path(cloudyfan_jev.__file__).parent.parent), str(Path(__file__).absolute()), '--worker']
            stdout_path, stderr_path = (reports.root / (comparison_id + ending) for ending in ('.worker.stdout', '.worker.stderr'))
            timed_out = False
            started = time.time()
            with stdout_path.open('xb') as stdout, stderr_path.open('xb') as stderr:
                os.fchmod(stdout.fileno(), 0o600)
                os.fchmod(stderr.fileno(), 0o600)
                process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=stdout, stderr=stderr,
                    close_fds=True, env={'PATH': os.environ.get('PATH', ''), 'PYTHONDONTWRITEBYTECODE': '1',
                                       **({'TMPDIR': os.environ['TMPDIR']} if 'TMPDIR' in os.environ else {})})
                try:
                    process.communicate(input=control, timeout=LIMITS['worker_wall_seconds'])
                    code = process.returncode
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate()
                    timed_out, code = True, process.returncode
                except BaseException:
                    process.kill()
                    process.communicate()
                    raise
            if not timed_out and code == 0:
                outcome = strict_json(_read(stdout_path, LIMITS['max_source_bytes']))
            else:
                reason = 'comparison_worker_timeout' if timed_out else 'comparison_worker_interrupted'
                outcome = {'items': [_unknown(task, reason) for task in tasks],
                           'frozen_inputs': {'tasks': tasks, 'source_freeze': 'incomplete'},
                           'data_sha256': digest({'tasks': tasks, 'source_freeze': 'incomplete'}), 'incomplete': True}
            reports.write(comparison_id + '.worker.json', {'returncode': code, 'timed_out': timed_out,
                          'pid': process.pid, 'argv': argv, 'started_at': started, 'finished_at': time.time(),
                          'reaped': process.returncode is not None,
                          'loaded_source_bytes': outcome.get('loaded_source_bytes'),
                          'loaded_source_files': outcome.get('loaded_source_files'),
                          'network_attempts': outcome.get('network_attempts'),
                          'stdout_sha256': hashlib.sha256(_read(stdout_path, LIMITS['max_source_bytes'])).hexdigest(),
                          'stderr_sha256': hashlib.sha256(_read(stderr_path, LIMITS['max_source_bytes'])).hexdigest()}, 16384)
            report = _report(request, outcome)
            inputs_sha = reports.write(comparison_id + '.inputs.json', outcome['frozen_inputs'], LIMITS['max_source_bytes'])
            report_sha = reports.write(comparison_id + '.report.json', report, LIMITS['max_output_bytes'])
            reports.write(comparison_id + '.seal.json', {'comparison_id': comparison_id,
                'request_sha256': request_sha, 'report_sha256': report_sha, 'inputs_sha256': inputs_sha}, 16384)
            return report
        finally:
            os.close(lock)
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, SourceUnavailable):
        fail('comparison_store_unavailable', 503)


def _worker():
    resource.setrlimit(resource.RLIMIT_CPU, (LIMITS['worker_cpu_seconds'], LIMITS['worker_cpu_seconds']))
    resource.setrlimit(resource.RLIMIT_FSIZE, (LIMITS['max_source_bytes'], LIMITS['max_source_bytes']))
    attempts = []
    def no_network(*args, **kwargs):
        attempts.append('network_forbidden')
        raise SourceUnavailable('comparison_network_forbidden')
    for name in ('socket', 'create_connection', 'getaddrinfo', 'gethostbyname', 'gethostbyname_ex', 'gethostbyaddr', 'getnameinfo'):
        setattr(socket, name, no_network)
    context = strict_json(sys.stdin.buffer.read(LIMITS['max_source_bytes'] + 1))
    outcome = _load_scope(context)
    outcome['network_attempts'] = len(attempts)
    if attempts:
        raise SourceUnavailable('comparison_network_forbidden')
    body = encoded(outcome)
    if len(body) > LIMITS['max_source_bytes']:
        raise SourceUnavailable('comparison_output_limit')
    sys.stdout.buffer.write(body)


def _load_scope(context):
    source = _Sources()
    items, frozen = [], []
    for task in context['tasks']:
        try:
            item, values = _load_task(context, source, task)
        except (OSError, ValueError, TypeError, KeyError, sqlite3.Error, SourceUnavailable):
            item, values = _unknown(task, 'original_evidence_unavailable'), None
        items.append(item)
        frozen.append({'task': task, 'values': values, 'evidence': item['evidence']})
    return {'items': items, 'data_sha256': digest(frozen), 'frozen_inputs': frozen, 'incomplete': source.limited,
            'loaded_source_bytes': source.bytes, 'loaded_source_files': source.files}


def _load_task(context, source, task):
    start = len(source.evidence)
    params = task['client'], task['run_id']
    rows = source.rows(context['monitor_path'], 'SELECT * FROM bindings WHERE client=? AND run=?',
                       params, kind='original_binding', maximum=32768, max_rows=1)
    if len(rows) != 1:
        return _unknown(task, 'original_binding_unavailable'), None
    binding = rows[0]
    if (set(binding) != {'client', 'run', 'fingerprint', 'pool', 'mode', 'category', 'contract', 'created'}
            or any(identifier(binding[k]) is None for k in ('client', 'run', 'category', 'contract'))
            or any(not isinstance(binding[k], str) or HASH.fullmatch(binding[k]) is None for k in ('fingerprint', 'pool'))
            or binding['mode'] not in MODES or not finite(binding['created'])):
        raise SourceUnavailable('original_binding_invalid')
    # Permission is checked before any raw task/receipt path is opened.
    active = source.rows(context['monitor_path'], 'SELECT enabled FROM clients WHERE id=?',
                         (task['client'],), kind='original_client_permission', maximum=4096, max_rows=1)
    if (not context['permission_reader_known'] or task['client'] not in context['allowed_principals'] or not active or active[0]['enabled'] != 1
            or context['identity_path'] is None):
        return _unknown(task, 'original_data_permission_unavailable'), None
    principal = source.rows(context['identity_path'], 'SELECT active FROM principals WHERE id=?',
                            (task['client'],), kind='original_identity_permission', maximum=4096, max_rows=1)
    if not principal or principal[0]['active'] != 1:
        return _unknown(task, 'original_data_permission_unavailable'), None
    cached_rows = source.rows(context['monitor_path'], 'SELECT data FROM tasks WHERE client=? AND run=?',
                             params, kind='original_safe_metadata', max_rows=1)
    cached = strict_json(cached_rows[0]['data']) if cached_rows else {}
    original = _original_projection(binding, cached)
    evidence = lambda: source.evidence[start:]
    internal = _task_id(*params)
    lifecycle = []
    if context['retention_path'] is not None:
        lifecycle = source.rows(context['retention_path'], 'SELECT created,expires,policy,state FROM tasks WHERE id=?',
                                (internal,), kind='original_data_lifecycle', maximum=32768, max_rows=1)
        floor = source.rows(context['retention_path'], 'SELECT observed FROM clock WHERE singleton=1',
                            kind='original_data_clock', maximum=4096, max_rows=1)
        if floor and (not finite(floor[0]['observed']) or context['now'] < floor[0]['observed']):
            return _unknown(task, 'original_data_clock_unavailable', original, evidence()), None
    if (not lifecycle or not finite(lifecycle[0]['expires'])
            or strict_json(lifecycle[0]['policy']) != context['retention_policy']):
        return _unknown(task, 'original_data_permission_unavailable', original, evidence()), None
    expired = (lifecycle[0]['state'] != 'active' or context['now'] >= lifecycle[0]['expires']
               or cached.get('access_expired') is True)
    trusted = context['contracts'].get(binding['contract'])
    if expired and trusted is not None:
        # The retained safe envelope, not an expired job/receipt, identifies
        # the original financial authority. A reused contract ID is insufficient.
        from cloudyfan_monitor.store import retained_metadata
        try:
            retained = retained_metadata(cached.get('_retained_public_metadata'), binding)
            account = digest(str(Path(trusted['registered']['budget_store']).absolute()))[:12]
            if all(row['account'] == account for row in retained['events']):
                original['costs'] = _original_costs(context, source, trusted['registered'], task, internal,
                    retained_ids={row['id'] for row in retained['events']})
        except (OSError, ValueError, TypeError, KeyError, sqlite3.Error, SourceUnavailable):
            pass
    if expired:
        original['task_status'] = 'retired' if lifecycle[0]['state'] == 'retired' else 'expired'
        return _unknown(task, 'original_raw_access_expired', original, evidence()), None
    if trusted is None:
        return _unknown(task, 'original_contract_unavailable', original, evidence()), None
    registered = trusted['registered']
    original['contract_version'] = identifier(registered.get('version'))
    original['routing_version'] = identifier(registered.get('routing_version'))
    original['costs'] = _original_costs(context, source, registered, task, internal)
    directory = Path(context['tasks_root']) / internal
    try:
        with (_shared(Path(context['retention_path']).parent / (internal + '.lock')),
              _shared(directory / '.lock')):
            job = source.read(directory / 'job.json', 'original_task_job')
            request = job['request']
            if (job['registered'] != registered or request['run_id'] != task['run_id']
                    or request['contract_id'] != binding['contract']
                    or set(request) != {'run_id', 'contract_id', 'prompt'}
                    or digest({'run_id': request['run_id'], 'category': binding['category'], 'prompt': request['prompt']}) != binding['fingerprint']):
                raise SourceUnavailable('original_task_binding_invalid')
            run = Path(registered['receipt_root']) / 'runs' / internal
            with _shared(run / '.lock'):
                manifest = source.read(run / 'manifest.json', 'original_manifest')
                result = source.read(run / 'result.json', 'original_result')
                if (manifest.get('mode') != binding['mode'] or manifest.get('task', {}).get('prompt') != request['prompt']
                        or any(manifest.get(key) != value for key, value in registered.items()
                               if key not in ('version', 'mode', 'tool_names', 'model_bounds', 'tool_bounds', 'receipt_root', 'budget_store', 'enforcement'))
                        or manifest.get('contract_version') != registered['version']
                        or result.get('config_sha256') != (digest(manifest) if manifest.get('routing_version') == 'qcs-backup-policy-v1'
                            else original_digest(manifest)) or result.get('run_id') != internal
                        or result.get('mode') != binding['mode']):
                    raise SourceUnavailable('original_manifest_binding_invalid')
                original['task_status'] = result['status'] if result.get('status') in STATUSES else 'unknown'
                original['path'] = _original_path(result)
                cached_path = cached.get('path')
                if (not isinstance(cached_path, list) or len(cached_path) != len(original['path'])
                        or any(saved.get('tier') != actual['tier'] or saved.get('stop') != actual['stop']
                            or saved.get('passed') is not (True if actual['public_status'] == 'passed'
                                else False if actual['public_status'] == 'failed' else None)
                            for saved, actual in zip(cached_path, original['path']))):
                    raise SourceUnavailable('original_public_metadata_inconsistent')
                receipts = _original_receipts(source, registered, result, internal)
                values, decisions = _reproduce_saved(context, source, binding, job, manifest, result, run, trusted, receipts)
                status = 'reproduced' if decisions and all(row['match'] is True for row in decisions) else 'partial'
                if not decisions:
                    status = 'unknown'
                reasons = list(dict.fromkeys(row['reason'] for row in decisions)) or ['original_decision_inputs_unavailable']
                return {'task': dict(task), 'original': original, 'reproduction': {
                    'status': status, 'scope': 'decision_from_saved_inputs', 'adopted': False,
                    'reasons': reasons, 'decisions': decisions, 'financial_admissibility': 'not_replayed'},
                    'evidence': evidence()}, {'decisions': values, 'binding': binding,
                        'data_use': {'principal_active': True, 'frozen_at': context['now'],
                            'access_expires_at': lifecycle[0]['expires'], 'policy': context['retention_policy']}}
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error, SourceUnavailable):
        return _unknown(task, 'original_evidence_unavailable', original, evidence()), None


def _original_projection(binding, cached):
    retained = cached.get('_retained_public_metadata')
    history = {}
    if retained is not None:
        from cloudyfan_monitor.store import retained_metadata
        try:
            history = retained_metadata(retained, binding)['history_binding']
        except (ValueError, TypeError, KeyError):
            pass
    path = []
    if isinstance(cached.get('path'), list) and len(cached['path']) <= LIMITS['max_decision_points']:
        for index, row in enumerate(cached['path']):
            if (not isinstance(row, dict) or row.get('tier') not in TIERS or row.get('stop') not in STOPS
                    or row.get('passed') is not None and type(row.get('passed')) is not bool):
                path = []
                break
            path.append({'index': index, 'tier': row['tier'], 'config_id': None, 'configuration_sha256': None,
                'public_status': 'passed' if row.get('passed') is True else 'failed' if row.get('passed') is False else 'unknown',
                'stop': row['stop']})
    return {'task_status': cached.get('status') if cached.get('status') in STATUSES else 'unknown',
        'mode': binding['mode'], 'category': binding['category'], 'pool_version': binding['pool'],
        'contract_id': binding['contract'], 'contract_version': history.get('contract_version'),
        'routing_version': history.get('routing_version'), 'created_at': binding['created'],
        'path': path, 'costs': {'known_api_usd': None, 'complete_api_usd': None,
                            'unknown_events': None, 'customer_usage_charge': _customer_unprovided()}}


def _money(value):
    if (not isinstance(value, str) or len(value) > 128
            or re.fullmatch(r'(?:\d+(?:\.\d+)?|\.\d+)', value, re.ASCII) is None):
        raise SourceUnavailable('original_financial_evidence_invalid')
    number = Decimal(value)
    if not number.is_finite() or number.adjusted() > 64 or number.as_tuple().exponent < -64:
        raise SourceUnavailable('original_financial_evidence_invalid')
    return number


def _original_costs(context, source, registered, task, internal, *, retained_ids=None):
    rows = source.rows(registered['budget_store'], 'SELECT id,run,kind,cost FROM events WHERE run=? ORDER BY id LIMIT 65',
                       (internal,), kind='original_budget_events', maximum=4096, max_rows=64)
    if retained_ids is not None and {row['id'] for row in rows} != retained_ids:
        raise SourceUnavailable('original_financial_authority_unavailable')
    with localcontext() as arithmetic:
        arithmetic.prec = 512
        known = sum((_money(row['cost']) for row in rows if row['cost'] is not None), Decimal(0))
    unknown = sum(row['cost'] is None for row in rows)
    customer = _customer_unprovided()
    charges = source.rows(context['monitor_path'], 'SELECT amount_usd FROM billing_entries '
        "WHERE client=? AND run_id=? AND kind='debit' ORDER BY id LIMIT 65",
        (task['client'], task['run_id']), kind='original_customer_ledger', maximum=4096, max_rows=64)
    pending = source.rows(context['monitor_path'], 'SELECT id FROM billing_waiting '
        "WHERE json_extract(data,'$.client')=? AND json_extract(data,'$.run_id')=? LIMIT 65",
        (task['client'], task['run_id']), kind='original_pending_customer_ledger', maximum=4096, max_rows=64)
    if charges or pending:
        with localcontext() as arithmetic:
            arithmetic.prec = 512
            charged = sum((_money(row['amount_usd']) for row in charges), Decimal(0))
        customer = {'status': 'unknown' if pending else 'available', 'known_usd': str(charged),
                    'complete_usd': None if pending else str(charged),
                    'unknown_events': len(pending), 'basis': 'original_customer_ledger'}
    return {'known_api_usd': str(known), 'complete_api_usd': None if unknown else str(known),
            'unknown_events': unknown, 'customer_usage_charge': customer}


def _original_path(result):
    attempts = result.get('attempts')
    if not isinstance(attempts, list) or len(attempts) > LIMITS['max_decision_points']:
        raise SourceUnavailable('original_result_invalid')
    path = []
    for index, attempt in enumerate(attempts):
        if (attempt.get('tier') not in TIERS or identifier(attempt.get('config_id')) is None
                or HASH.fullmatch(str(attempt.get('configuration_sha256'))) is None
                or attempt.get('stop') not in STOPS or type(attempt.get('public', {}).get('passed')) not in (bool, type(None))):
            raise SourceUnavailable('original_result_invalid')
        passed = attempt.get('public', {}).get('passed')
        path.append({'index': index, 'tier': attempt['tier'], 'config_id': attempt['config_id'],
            'configuration_sha256': attempt['configuration_sha256'],
            'public_status': 'passed' if passed is True else 'failed' if passed is False else 'unknown',
            'stop': attempt['stop']})
    return path


def _backup_candidates(source, trusted, manifest, checkpoint, tier):
    pool = manifest['backup_plan']['pools'][tier]
    observed = checkpoint['health'][tier]
    if (len(pool) > LIMITS['max_candidate_configurations'] or checkpoint['previous'] is not None
            or checkpoint['actions'] or set(checkpoint['health']) != {tier}
            or [row['entry']['config_id'] for row in pool] != [row['config_id'] for row in observed]):
        raise SourceUnavailable('original_candidate_scope_invalid')
    candidates = []
    for record, observation in zip(pool, observed):
        configuration = record['configuration_sha256']
        health = observation['assessment']
        historical = source.rows(trusted['health_path'], 'SELECT payload FROM snapshots WHERE id=?',
            (health['snapshot_id'],), kind='original_health_snapshot', maximum=1048576, max_rows=1)
        snapshot = strict_json(historical[0]['payload']) if historical else None
        if (snapshot is None or digest(snapshot) != health['snapshot_id']
                or snapshot['policy_sha256'] != digest(manifest['health_policy'])
                or health['observation'] not in snapshot['observations']
                or health['observation']['configuration_sha256'] != configuration
                or health['observation']['provider'] != record['entry']['profile']['provider']
                or type(health['eligible']) is not bool or health['eligible'] != (not health['reasons'])
                or not set(health['reasons']) <= set(observation['reasons'])):
            raise SourceUnavailable('original_health_evidence_invalid')
        reasons = observation['reasons']
        if (not isinstance(reasons, list) or len(reasons) > 8
                or any(not isinstance(reason, str) or re.fullmatch(r'[a-z0-9_]{1,80}', reason) is None for reason in reasons)):
            raise SourceUnavailable('original_gate_evidence_invalid')
        # Replay the saved admission predicates; this never requalifies a
        # candidate against current health, quota, window or deadline state.
        window = observation.get('capacity_window')
        if window is not None and window != {'status': 'open', 'not_before': None,
                'evidence_refs': [], 'revision': 0, 'reason_code': 'capacity_window_open'}:
            raise SourceUnavailable('original_window_inputs_unavailable')
        candidates.append({'tier': tier, 'config_id': record['entry']['config_id'],
            'configuration_sha256': configuration, 'generation_cost_usd': record['evidence']['generation_cost_usd'],
            'complete_cost_usd': record['evidence']['complete_cost_usd'],
            'flow_ms_upper': record['evidence']['speed']['upper'], 'quality_score': None,
            'gate': {'state': 'excluded' if reasons else 'eligible', 'reasons': reasons,
                     'evidence_sha256': digest(observation)}})
    return candidates


def _original_receipts(source, registered, result, internal):
    hashes = []
    total = 0
    for attempt in result['attempts']:
        calls = attempt.get('calls')
        if not isinstance(calls, list) or not calls:
            raise SourceUnavailable('original_receipts_unavailable')
        attempt_hashes = []
        for call in calls:
            total += 1
            cid = call.get('call_id')
            if (total > 64 or not isinstance(cid, str) or '..' in cid
                    or re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,159}', cid) is None
                    or not cid.startswith('run-' + internal + '-')):
                raise SourceUnavailable('original_receipt_identity_invalid')
            root = Path(registered['receipt_root']) / 'calls' / cid
            intent = source.read(root / 'intent.json', 'original_call_intent')
            response = source.read(root / 'response.json', 'original_call_response')
            response_hash = source.evidence[-1]['sha256']
            if (intent.get('call_id') != cid or response.get('call_id') != cid
                    or original_digest({'url': intent['url'], 'payload': intent['payload']}) != intent.get('request_sha256')
                    or response.get('request_sha256') != intent['request_sha256']
                    or call.get('configuration_sha256') != attempt['configuration_sha256']
                    or intent['payload'].get('model') != call.get('model')
                    or type(response.get('http_status')) is not int):
                raise SourceUnavailable('original_receipt_binding_invalid')
            attempt_hashes.append(response_hash)
        hashes.append(digest(attempt_hashes))
    return hashes


def _reproduce_saved(context, source, binding, job, manifest, result, run, trusted, receipts):
    from cloudyfan_jev.saved_policy_comparison import candidate_set_digest, reproduce_original_policy
    policy = manifest['routing_version']
    if policy != 'qcs-backup-policy-v1':
        return [], []  # Other families remain explicit unknown until original inputs are supported.
    checkpoint = source.read(run / 'backup-selection-0.json', 'original_selection_checkpoint')
    tier = {'cloudyfan': 'open_source', 'fixed_high': 'high', 'low_first': 'low',
            'open_source_first': 'open_source'}[binding['mode']]
    candidates = _backup_candidates(source, trusted, manifest, checkpoint, tier)
    values = {'schema_version': 'saved-policy-values-v1', 'policy_version': policy, 'mode': binding['mode'],
        'source': {'binding_sha256': digest(binding), 'request_sha256': original_digest(job['request']),
                   'registered_sha256': original_digest(job['registered']), 'policy_sha256': original_digest(manifest),
                   'checkpoint_sha256': digest(checkpoint), 'candidate_set_sha256': candidate_set_digest(candidates)},
        'context': {'stage': 'initial', 'previous_tier': None, 'previous_configuration_sha256': None,
                    'previous_public_passed': None, 'previous_stop': 'none', 'quality_phase': 0,
                    'generation_calls': 0, 'receipt_sha256': None, 'validation_sha256': None},
        'candidates': candidates, 'economics': None, 'classifier': None}
    started = time.monotonic()
    reproduced = reproduce_original_policy(values)
    if (time.monotonic() - started) * 1000 > LIMITS['max_policy_compute_milliseconds']:
        raise SourceUnavailable('comparison_compute_limit')
    path = _original_path(result)
    match = (reproduced['status'] == 'reproduced' and bool(path)
             and reproduced['selected_tier'] == path[0]['tier']
             and reproduced['selected_configuration_sha256'] == path[0]['configuration_sha256']
             and checkpoint['pair'].get(tier) in manifest['backup_plan']['pools'][tier]
             and checkpoint['pair'][tier]['configuration_sha256'] == path[0]['configuration_sha256'])
    decisions = [{'index': 0, 'stage': 'initial', 'action': reproduced['action'],
        'selected_tier': reproduced['selected_tier'],
        'config_id': next((row['config_id'] for row in candidates if row['configuration_sha256'] == reproduced['selected_configuration_sha256']), None),
        'reason': reproduced['reason'], 'match': match if reproduced['status'] == 'reproduced' else None}]
    inputs = [values]
    # The saved validation result is public input, never a new validator run or
    # the answer/diagnostic prose. Its provenance includes the original raw
    # response hashes and the independently retained public outcome above.
    if len(path) == 1 and path[0]['public_status'] == 'passed' and receipts:
        stopped = strict_json(encoded(values))
        stopped['context'] = {'stage': 'after_validation', 'previous_tier': path[0]['tier'],
            'previous_configuration_sha256': path[0]['configuration_sha256'],
            'previous_public_passed': True, 'previous_stop': 'validation_passed', 'quality_phase': 0,
            'generation_calls': len(result['attempts'][0]['calls']), 'receipt_sha256': receipts[0],
            'validation_sha256': digest({'passed': True, 'result_sha256': digest(result)})}
        stop = reproduce_original_policy(stopped)
        decisions.append({'index': 1, 'stage': 'after_validation', 'action': stop['action'],
            'selected_tier': stop['selected_tier'], 'config_id': None, 'reason': stop['reason'],
            'match': stop['status'] == 'reproduced' and stop['action'] == 'stop'})
        inputs.append(stopped)
    else:
        decisions.append({'index': 1, 'stage': 'after_validation', 'action': None,
            'selected_tier': None, 'config_id': None, 'reason': 'original_decision_inputs_unavailable', 'match': None})
    if (time.monotonic() - started) * 1000 > LIMITS['max_policy_compute_milliseconds']:
        raise SourceUnavailable('comparison_compute_limit')
    return inputs, decisions


if __name__ == '__main__' and sys.argv[1:] == ['--worker']:
    _worker()

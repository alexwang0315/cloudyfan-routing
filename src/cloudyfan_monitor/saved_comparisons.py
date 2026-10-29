"""Operator evidence loading and immutable reports, separate from task execution.

Only a bounded value DTO crosses into the Core policy comparator. The short-lived
loader process receives paths/values, never a service, model client or callback.
"""
from contextlib import ExitStack, contextmanager
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
# Stable ceilings for this stored schema, independent of later creation tuning.
STORED_READ_MAXIMA = {'max_tasks': 20, 'max_source_files': 256, 'max_source_bytes': 4194304,
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
         'context_limit', 'time_limit', 'policy_denied', 'none', 'stop', 'length',
         'content_filter', 'tool_calls', 'empty_or_exhausted'}


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
def _unchanged(*paths):
    # Core takes each task's directory and retention locks exclusive and
    # non-blocking for polls, creation and scheduler execution. Holding them
    # shared, even briefly, turns a concurrent dispatch into task_busy. Read
    # without them and require the task's mutable files to be byte-identical
    # after every dependent read; any change leaves this task unknown.
    def fingerprint():
        return [hashlib.sha256(_read(path, LIMITS['max_source_file_bytes'])).digest() for path in paths]
    before = fingerprint()
    yield
    if fingerprint() != before:
        raise SourceUnavailable('source_changed_during_read')


@contextmanager
def _database(path, maximum=1048576, *, capture=None, source_limit=None, file_limit=None):
    # SQLite must never open a live pathname after a separate symlink check.
    # Capture main DB + WAL through the same NOFOLLOW parent/leaf descriptors,
    # checking both generations before/after. SQLite reads only private copies;
    # its normal WAL reader determines the committed prefix. No immutable=1,
    # original SHM write, backup API or current-source refresh is involved.
    limit = LIMITS['max_source_bytes'] if source_limit is None else source_limit
    with _parent(Path(path).absolute()) as (parent, name), tempfile.TemporaryDirectory(prefix='cloudyfan-comparison-') as temporary:
        opened, states, captured = {}, {}, {}
        try:
            def no_rollback_journal():
                try:
                    os.stat(name + '-journal', dir_fd=parent, follow_symlinks=False)
                except FileNotFoundError:
                    return
                # A captured main file alone must not make an uncommitted or
                # recovery-required rollback transaction look committed.
                raise SourceUnavailable('source_database_recovery_or_writer_present')
            no_rollback_journal()
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
                        or info.st_size > min(limit, LIMITS['max_source_file_bytes'] if file_limit is None else file_limit)):
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
            no_rollback_journal()
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
                connection.set_progress_handler(None, 0)
                connection.rollback()
            finally:
                connection.close()
        finally:
            for descriptor in opened.values():
                os.close(descriptor)


def _task_id(client, run):
    return 'http-' + hashlib.sha256(json.dumps([client, run], sort_keys=True,
        ensure_ascii=False, allow_nan=False).encode()).hexdigest()[:48]


def _candidate_strategies():
    from cloudyfan_jev.saved_candidate_strategy import STRATEGIES
    return STRATEGIES


PAIRING_RULES = ('same-input-v1',)


def _pairing(body):
    """Only a registered pairing rule, fixed in the request before any outcome is read."""
    rule = body.get('pairing')
    if 'pairing' in body and (type(rule) is not str or rule not in PAIRING_RULES):
        fail('comparison_pairing_not_registered', 409)
    return rule


def _strategy(body):
    """Only a Core-registered strategy name; never code or a browser-built policy."""
    strategy = body.get('candidate_strategy')
    if 'candidate_strategy' in body and (type(strategy) is not str or strategy not in _candidate_strategies()):
        fail('comparison_strategy_not_registered', 409)
    return strategy


def _scope(body, *, maximum=None):
    if not isinstance(body, dict) or set(body) - {'candidate_strategy', 'pairing'} != {'policy_version', 'tasks'}:
        fail('unsupported_comparison_fields')
    _strategy(body)
    _pairing(body)
    if body['policy_version'] != POLICY:
        fail('comparison_policy_not_registered', 409)
    tasks = body['tasks']
    maximum = LIMITS['max_tasks'] if maximum is None else maximum
    if type(maximum) is not int or not 1 <= maximum <= 100:
        fail('comparison_scope_invalid')
    if not isinstance(tasks, list) or not 1 <= len(tasks) <= maximum:
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
        self.compute_milliseconds = 0.0
        self.databases = {}
        self.current_database_refs = set()
        self.snapshots = ExitStack()

    def begin_task(self):
        self.current_database_refs = set()

    def close(self):
        self.snapshots.close()

    def reproduce(self, values):
        """Budget all pure decisions in this report; pass values only to Core."""
        from cloudyfan_jev.saved_policy_comparison import reproduce_original_policy
        return self.compute(reproduce_original_policy, values)

    def compute(self, function, *arguments):
        if self.compute_milliseconds >= LIMITS['max_policy_compute_milliseconds']:
            self.limited = True
            raise SourceUnavailable('comparison_compute_limit')
        started = time.monotonic()
        try:
            result = function(*arguments)
        finally:
            self.compute_milliseconds += (time.monotonic() - started) * 1000
        if self.compute_milliseconds > LIMITS['max_policy_compute_milliseconds']:
            self.limited = True
            raise SourceUnavailable('comparison_compute_limit')
        return result

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
        if remaining <= 0 or self.files >= LIMITS['max_source_files']:
            self.limited = True
            raise SourceUnavailable('source_work_limit')
        key = str(Path(path).absolute())
        if key not in self.databases:
            # One captured generation per DB inside this fresh report. This
            # avoids repeatedly reading the same historical Health/Budget DB
            # and keeps all subsequent queries on the same frozen source.
            start = len(self.evidence)
            db = self.snapshots.enter_context(_database(path, capture=self.record, source_limit=remaining))
            self.databases[key] = db, list(self.evidence[start:])
            self.current_database_refs.add(key)
        db, references = self.databases[key]
        if key not in self.current_database_refs:
            # Cite the same captured generation in every item which uses it;
            # a reference is not another source read or byte-budget debit.
            self.evidence.extend(dict(row) for row in references)
            self.current_database_refs.add(key)
        previous = db.setlimit(sqlite3.SQLITE_LIMIT_LENGTH,
                               min(maximum, LIMITS['max_source_bytes'] - self.bytes))
        try:
            cursor = db.execute(query, params)
            result, projected = [], 2
            for number, row in enumerate(cursor):
                if number >= max_rows:
                    self.limited = True
                    raise SourceUnavailable('source_work_limit')
                value = dict(row)
                projected += len(encoded(value)) + 1
                if projected > LIMITS['max_source_bytes'] - self.bytes:
                    self.limited = True
                    raise SourceUnavailable('source_work_limit')
                result.append(value)
        finally:
            db.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, previous)
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
            request = strict_json(request_body)
            if not isinstance(request, dict):
                raise SourceUnavailable('comparison_evidence_invalid')
            saved_limits = request.get('limits')
            if saved_limits is not None:
                self.validate_limits(saved_limits)
            seal = strict_json(_read(self.root / (comparison_id + '.seal.json'), 16384))
            ceilings = saved_limits if saved_limits is not None else STORED_READ_MAXIMA
            body = _read(self.root / (comparison_id + '.report.json'), ceilings['max_output_bytes'])
            inputs = _read(self.root / (comparison_id + '.inputs.json'), ceilings['max_source_bytes'])
            report = strict_json(body)
            if not isinstance(report, dict):
                raise SourceUnavailable('comparison_evidence_invalid')
            self.validate_limits(report.get('limits'))
            if (len(body) > report['limits']['max_output_bytes']
                    or len(inputs) > report['limits']['max_source_bytes']):
                raise SourceUnavailable('comparison_evidence_invalid')
            if (set(seal) != {'request_sha256', 'report_sha256', 'inputs_sha256', 'comparison_id'}
                    or seal['comparison_id'] != comparison_id or seal['request_sha256'] != hashlib.sha256(request_body).hexdigest()
                    or seal['report_sha256'] != hashlib.sha256(body).hexdigest()
                    or seal['inputs_sha256'] != hashlib.sha256(inputs).hexdigest()
                    or report.get('data_sha256') != digest(strict_json(inputs))
                    or request.get('comparison_id') != comparison_id
                    or request.get('policy_version') != POLICY or report.get('policy_version') != POLICY
                    or report.get('computation_version') != COMPUTATION
                    or request.get('policy_sha256') != digest(request['implementation'])
                    or report.get('policy_sha256') != request['policy_sha256']
                    or report.get('scope') != _report_scope(request)
                    or report.get('created_at') != request['created_at'] or report.get('frozen_at') != request['frozen_at']
                    or request.get('limits', report.get('limits')) != report.get('limits')
                    or report.get('result_sha256') != _result_digest(request, report['items'], report['data_sha256'],
                                                                     report['limits'], report.get('pairing'))
                    or ('pairing' in request) != ('pairing' in report)
                    or report.get('comparison_id') != comparison_id or report.get('schema_version') != SCHEMA):
                raise SourceUnavailable('comparison_evidence_invalid')
            return report
        except FileNotFoundError:
            fail('comparison_report_not_ready', 409)
        except (OSError, ValueError, SourceUnavailable, TypeError, KeyError):
            fail('comparison_evidence_unavailable', 503)

    @staticmethod
    def validate_limits(limits):
        if (not isinstance(limits, dict) or set(limits) != set(STORED_READ_MAXIMA)
                or any(type(limits[key]) is not int or not 1 <= limits[key] <= ceiling
                       for key, ceiling in STORED_READ_MAXIMA.items())):
            raise SourceUnavailable('comparison_evidence_invalid')


def catalog(app):
    try:
        with _database(app.store.path, 32768, source_limit=STORED_READ_MAXIMA['max_source_bytes'],
                       file_limit=STORED_READ_MAXIMA['max_source_file_bytes']) as db:
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
                    _scope({'policy_version': request.get('policy_version'), 'tasks': request.get('tasks'),
                            **{name: request[name] for name in ('candidate_strategy', 'pairing') if name in request}},
                           maximum=request.get('limits', {}).get('max_tasks', 20))
                    status = 'incomplete'
                    if (reports.root / (request['comparison_id'] + '.seal.json')).exists():
                        status = reports.result(request['comparison_id'])['status']
                    saved.append({'comparison_id': request['comparison_id'], 'created_at': request['created_at'],
                        'status': status, 'policy_version': POLICY, 'requested_tasks': len(request['tasks'])})
        return {'schema_version': 'saved-policy-comparison-catalog-v1',
                'registered_policies': [{'version': POLICY, 'label': '重現原保存政策'}],
                'pairing_rules': [{'version': 'same-input-v1', 'label': '相同輸入的已保存流程配對'}],
                'candidate_strategies': [{'version': name, 'label': label}
                                         for name, label in sorted(_candidate_strategies().items())],
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


def _report_scope(request):
    # Reports created before candidate strategies keep their original scope and digest.
    return {'tasks': request['tasks'], **{name: request[name] for name in ('candidate_strategy', 'pairing')
                                          if name in request}}


def _result_digest(request, items, data_sha256, limits, pairing=None):
    return digest({'items': items, 'tasks': request['tasks'], 'frozen_at': request['frozen_at'],
        'data_sha256': data_sha256, 'policy_sha256': request['policy_sha256'],
        'limits': limits, 'policy_version': request['policy_version'],
        'computation_version': request['implementation']['computation'],
        **({'candidate_strategy': request['candidate_strategy']} if 'candidate_strategy' in request else {}),
        **({'pairing': request['pairing'], 'pairing_result': pairing} if 'pairing' in request else {})})


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
        if 'candidate' in item:
            key = 'candidate_' + item['candidate']['status']
            counts[key] = counts.get(key, 0) + 1
    pairing = outcome.get('pairing') if 'pairing' in request else None
    if 'pairing' in request and pairing is None:
        pairing = {'rule': request['pairing'], 'status': 'not_computed', 'reason': 'comparison_incomplete'}
    result_sha = _result_digest(request, items, outcome['data_sha256'], request['limits'], pairing)
    return {'schema_version': SCHEMA, 'comparison_id': request['comparison_id'],
        'status': 'incomplete' if outcome.get('incomplete') else 'completed',
        'policy_version': POLICY, 'computation_version': COMPUTATION,
        'created_at': request['created_at'], 'frozen_at': request['frozen_at'],
        'data_sha256': outcome['data_sha256'], 'policy_sha256': request['policy_sha256'],
        'result_sha256': result_sha, 'scope': _report_scope(request), 'limits': request['limits'],
        'counts': counts, 'items': items, **({'pairing': pairing} if 'pairing' in request else {})}


def create_report(app, body):
    tasks = _scope(body)
    strategy, pairing = _strategy(body), _pairing(body)
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
            import platform
            core_source = Path(policy_module.__file__).absolute().parent
            implementation = {'policy': POLICY, 'computation': COMPUTATION,
                'core_policy_sha256': hashlib.sha256(_read(Path(policy_module.__file__).absolute(), 1048576)).hexdigest(),
                'host_loader_sha256': hashlib.sha256(_read(Path(__file__).absolute(), 1048576)).hexdigest(),
                'host_family_sha256': {name: hashlib.sha256(_read(Path(__file__).with_name(name).absolute(), 1048576)).hexdigest()
                    for name in ('saved_qcs_comparison.py', 'saved_backup_comparison.py', 'saved_window_comparison.py')},
                'host_validation_sha256': {name: hashlib.sha256(_read(Path(__file__).with_name(name).absolute(), 1048576)).hexdigest()
                    for name in ('store.py', 'observation_fields.py')},
                'core_parser_sha256': {name: hashlib.sha256(_read(core_source / name, 1048576)).hexdigest()
                    for name in ('routing.py', 'decisions.py', 'client.py', 'generation_request.py',
                        'errors.py', 'receipt_store.py', 'capacity.py', 'capacity_dispatch.py', 'capacity_windows.py')},
                'runtime': {'python': platform.python_version(), 'sqlite': sqlite3.sqlite_version}}
            if pairing is not None:
                implementation['host_pairing_sha256'] = hashlib.sha256(
                    _read(Path(__file__).with_name('saved_pairing.py').absolute(), 1048576)).hexdigest()
            if strategy is not None:
                implementation['core_candidate_sha256'] = hashlib.sha256(
                    _read(core_source / 'saved_candidate_strategy.py', 1048576)).hexdigest()
            request = {'comparison_id': comparison_id, 'created_at': now, 'frozen_at': now,
                       'tasks': tasks, 'policy_version': POLICY, 'implementation': implementation,
                       'policy_sha256': digest(implementation), 'limits': dict(LIMITS),
                       **({'candidate_strategy': strategy} if strategy is not None else {}),
                       **({'pairing': pairing, 'pairing_metrics': 'paired-saved-outcomes-v1'} if pairing is not None else {})}
            request_sha = reports.write(comparison_id + '.request.json', request, 16384)
            context = {**_source_context(app, tasks, now), 'candidate_strategy': strategy, 'pairing': pairing}
            control = encoded(context)
            if len(control) > LIMITS['max_source_bytes']:
                raise SourceUnavailable('comparison_control_oversized')
            import cloudyfan_jev
            bootstrap = ('import sys;sys.dont_write_bytecode=True;sys.path[:0]=sys.argv[1:3];'
                         'from cloudyfan_monitor.saved_comparisons import _worker;_worker()')
            argv = [sys.executable, '-I', '-S', '-c', bootstrap, str(Path(__file__).parents[1]),
                    str(Path(cloudyfan_jev.__file__).parent.parent)]
            stdout_path, stderr_path = (reports.root / (comparison_id + ending) for ending in ('.worker.stdout', '.worker.stderr'))
            timed_out = False
            started = time.time()
            # The worker copies original stores into its TMPDIR. A wall or CPU
            # kill skips its own cleanup, so this parent owns that directory.
            with (stdout_path.open('xb') as stdout, stderr_path.open('xb') as stderr,
                  tempfile.TemporaryDirectory(prefix='cloudyfan-comparison-', dir=os.environ.get('TMPDIR')) as scratch):
                os.fchmod(stdout.fileno(), 0o600)
                os.fchmod(stderr.fileno(), 0o600)
                process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=stdout, stderr=stderr,
                    close_fds=True, env={'PATH': os.environ.get('PATH', ''), 'PYTHONDONTWRITEBYTECODE': '1',
                                       'TMPDIR': scratch})
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
                          'policy_compute_milliseconds': outcome.get('policy_compute_milliseconds'),
                          'worker_limits': outcome.get('worker_limits'),
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
    outcome['worker_limits'] = {'cpu_seconds': list(resource.getrlimit(resource.RLIMIT_CPU)),
                                'file_bytes': list(resource.getrlimit(resource.RLIMIT_FSIZE))}
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
    try:
        for task in context['tasks']:
            source.begin_task()
            try:
                if source.limited:
                    item, values = _unknown(task, 'comparison_work_limit'), None
                else:
                    item, values = _load_task(context, source, task)
            except (OSError, ValueError, TypeError, KeyError, sqlite3.Error, SourceUnavailable):
                item, values = _unknown(task, 'original_evidence_unavailable'), None
            if context.get('candidate_strategy') and 'candidate' not in item:
                item = {**item, 'candidate': {'strategy': context['candidate_strategy'], 'status': 'not_evaluable',
                    'adopted': False, 'scope': CANDIDATE_SCOPE, 'reasons': item['reproduction']['reasons'],
                    'decisions': []}}
                item = {key: item[key] for key in ('task', 'original', 'reproduction', 'candidate', 'evidence')}
            items.append(item)
            frozen.append({'task': task, 'values': values, 'evidence': item['evidence']})
    finally:
        source.close()
    pairing = None
    if context.get('pairing'):
        from cloudyfan_monitor.saved_pairing import compare_pairs
        # Keys come only from fully loaded originals; anything else is excluded with its reason.
        pairing = compare_pairs([{'task': row['task'], 'original': item['original'],
            'key': (row['values'] or {}).get('pairing_key'),
            'key_reason': ('pairing_input_unavailable' if row['values'] is not None
                           else (item['reproduction']['reasons'] or ['original_evidence_unavailable'])[0])}
            for row, item in zip(frozen, items)])
    return {'items': items, 'data_sha256': digest(frozen), 'frozen_inputs': frozen, 'incomplete': source.limited,
            'loaded_source_bytes': source.bytes, 'loaded_source_files': source.files,
            'policy_compute_milliseconds': source.compute_milliseconds,
            **({'pairing': pairing} if pairing is not None else {})}


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
        try:
            event_ids = _retained_cost_ids(cached, binding, trusted['registered'], internal)
            original['costs'] = _original_costs(context, source, trusted['registered'], task, internal,
                                                retained_ids=event_ids)
        except (OSError, ValueError, TypeError, KeyError, sqlite3.Error, SourceUnavailable):
            pass
    if expired:
        original['task_status'] = 'retired' if lifecycle[0]['state'] == 'retired' else 'expired'
        return _unknown(task, 'original_raw_access_expired', original, evidence()), None
    if trusted is None:
        return _unknown(task, 'original_contract_unavailable', original, evidence()), None
    registered = trusted['registered']
    directory = Path(context['tasks_root']) / internal
    try:
        with _unchanged(directory / 'job.json', *([directory / 'state.json']
                        if cached.get('status') in ('bound', 'ready', 'queued') else [])):
            job = source.read(directory / 'job.json', 'original_task_job')
            request = job['request']
            if (job['registered'] != registered or request['run_id'] != task['run_id']
                    or request['contract_id'] != binding['contract']
                    or set(request) != {'run_id', 'contract_id', 'prompt'}
                    or digest({'run_id': request['run_id'], 'category': binding['category'], 'prompt': request['prompt']}) != binding['fingerprint']):
                raise SourceUnavailable('original_task_binding_invalid')
            # Private same-input key: never in the public report, never after raw expiry.
            # The contract ID names the mode's runner, so it cannot be part of a cross-mode key.
            pairing_key = digest({'category': binding['category'],
                'contract_version': identifier(registered.get('version')),
                'prompt_sha256': hashlib.sha256(request['prompt'].encode()).hexdigest()}) if context.get('pairing') else None
            # Only the task's original registered descriptor can identify the
            # fee authority. Reusing a host contract ID must not substitute a
            # different empty account and turn the original fee into zero.
            original['contract_version'] = identifier(registered.get('version'))
            original['routing_version'] = identifier(registered.get('routing_version'))
            event_ids = _retained_cost_ids(cached, binding, registered, internal)
            never_dispatched = False
            if cached.get('status') in ('bound', 'ready', 'queued'):
                state = source.read(directory / 'state.json', 'original_task_state')
                reservations = source.rows(registered['budget_store'], 'SELECT run FROM reservations WHERE run=?',
                    (internal,), kind='original_task_reservation', maximum=4096, max_rows=1)
                never_dispatched = (state == {'run_id': task['run_id'], 'status': 'ready'} and not reservations
                                     and _original_execution_absent(registered['receipt_root'], internal))
                source.record('original_never_dispatched_check', encoded({'never_dispatched': never_dispatched}))
            original['costs'] = _original_costs(context, source, registered, task, internal,
                                                 retained_ids=event_ids, never_dispatched=never_dispatched)
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
                original['path'] = _original_path(result, manifest)
                cached_path = cached.get('path')
                if (not isinstance(cached_path, list) or len(cached_path) != len(original['path'])
                        or any(saved.get('tier') != actual['tier'] or saved.get('stop') != actual['stop']
                            or saved.get('passed') is not (True if actual['public_status'] == 'passed'
                                else False if actual['public_status'] == 'failed' else None)
                            for saved, actual in zip(cached_path, original['path']))):
                    raise SourceUnavailable('original_public_metadata_inconsistent')
                receipts = _original_receipts(source, registered, manifest, result, internal, run)
                # The family adapter receives plain, server-selected access
                # authorities. Copy rather than mutating the shared contract
                # map or introducing a cycle into this per-report context.
                family_trusted = dict(trusted, comparison_access=context, internal_run=internal)
                values, decisions = _reproduce_saved(source, binding, job, manifest, result, run, family_trusted,
                    original_path=original['path'], receipts=receipts)
                candidate = (_candidate(source, context['candidate_strategy'], values, decisions)
                             if context.get('candidate_strategy') else None)
                status = ('reproduced' if decisions and all(row['match'] is True for row in decisions)
                          else 'partial' if any(row['match'] is True for row in decisions) else 'unknown')
                reasons = list(dict.fromkeys(code for row in decisions for code in (row['reason'], row.get('policy_reason'))
                    if code is not None)) or ['original_decision_inputs_unavailable']
                # Economic forecasts stay in the frozen inputs. The public
                # projection lists original fees and policy reason codes; it
                # never presents a forecast as achieved model-cost savings.
                decisions = [{key: row[key] for key in ('index', 'stage', 'action', 'selected_tier',
                    'config_id', 'reason', 'match')} for row in decisions]
                return {'task': dict(task), 'original': original, 'reproduction': {
                    'status': status, 'scope': 'decision_from_saved_inputs', 'adopted': False,
                    'reasons': reasons, 'decisions': decisions, 'financial_admissibility': 'not_replayed'},
                    **({'candidate': candidate} if candidate is not None else {}),
                    'evidence': evidence()}, {'decisions': values, 'binding': binding,
                        **({'pairing_key': pairing_key} if pairing_key is not None else {}),
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
            or re.fullmatch(r'(?:\d+(?:\.\d+)?|\.\d+)(?:[eE][+-]?\d{1,3})?', value, re.ASCII) is None):
        raise SourceUnavailable('original_financial_evidence_invalid')
    number = Decimal(value)
    if not number.is_finite() or number.adjusted() > 64 or number.as_tuple().exponent < -64:
        raise SourceUnavailable('original_financial_evidence_invalid')
    return number


def _retained_cost_ids(cached, binding, registered, internal):
    """A prior auxiliary prefix is not a complete terminal event inventory."""
    from cloudyfan_monitor.store import retained_metadata
    try:
        retained = retained_metadata(cached.get('_retained_public_metadata'), binding)
        account = digest(str(Path(registered['budget_store']).absolute()))[:12]
        if (registered.get('tool_names') != [] or cached.get('completed_status') not in ('passed', 'failed', 'unverifiable')
                or any(row['account'] != account or row['run'] != internal for row in retained['events'])):
            return None
        trace = cached['trace']
        attempts = trace['attempts']
        if (trace.get('complete') is not True or trace.get('basis') != 'verified_core_result'
                or not isinstance(attempts, list) or not 1 <= len(attempts) <= 9
                or type(cached.get('generation_attempts')) is not int
                or cached['generation_attempts'] != len(attempts)
                or type(cached.get('classifier_calls')) is not int or not 0 <= cached['classifier_calls'] <= 2
                or any(not isinstance(row.get('calls'), list) for row in attempts)):
            return None
        calls = [call for attempt in attempts for call in attempt['calls']]
        if len(calls) + cached['classifier_calls'] > 64:
            return None
        events = retained['events']
        generation = [row for row in events if row['metadata']['request_kind'] == 'generation']
        classification = [row for row in events if row['metadata']['request_kind'] == 'classification']
        def identity(call):
            return digest({key: call.get(key) for key in ('model', 'provider', 'generation_id')})
        if (len(events) != len(calls) + cached['classifier_calls']
                or len(classification) != cached['classifier_calls']
                or sorted(identity(call) for call in calls) != sorted(identity(row['metadata']) for row in generation)
                or any(not row['id'].startswith('model:') or len(row['id'].split(':')) != 3
                    or not row['id'].split(':')[2].startswith('run-' + internal + '-') for row in events)):
            return None
        return {row['id'] for row in events}
    except (ValueError, TypeError, KeyError, AttributeError):
        return None


def _original_execution_absent(receipt_root, internal):
    # Under the original task's shared lock, Core cannot begin a run. Check
    # the controlled runs namespace without opening unrelated call payloads.
    with _parent(Path(receipt_root) / 'runs') as (parent, name):
        try:
            runs = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        except FileNotFoundError:
            return True
        try:
            try:
                run = os.open(internal, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=runs)
            except FileNotFoundError:
                return True
            try:
                # Queue admission may create this deadline before execution.
                # Any lock/manifest/result or unfamiliar artifact defeats the
                # no-dispatch proof; do not open or scan call payloads.
                with os.scandir(run) as entries:
                    for number, entry in enumerate(entries):
                        info = entry.stat(follow_symlinks=False)
                        if (number >= 1 or entry.name != 'backup-deadline.json'
                                or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 16384):
                            return False
                return True
            finally:
                os.close(run)
        finally:
            os.close(runs)


def _original_costs(context, source, registered, task, internal, *, retained_ids=None, never_dispatched=False):
    rows = source.rows(registered['budget_store'], 'SELECT id,run,kind,cost FROM events WHERE run=? ORDER BY id LIMIT 65',
                       (internal,), kind='original_budget_events', maximum=4096, max_rows=64)
    observed_ids = {row['id'] for row in rows}
    missing = len(retained_ids - observed_ids) if retained_ids is not None else None
    coverage = retained_ids is not None and observed_ids == retained_ids
    if never_dispatched and not rows:
        missing, coverage = 0, True
    with localcontext() as arithmetic:
        arithmetic.prec = 512
        known = sum((_money(row['cost']) for row in rows if row['cost'] is not None), Decimal(0))
    observed_unknown = sum(row['cost'] is None for row in rows)
    unknown = observed_unknown or None
    if never_dispatched and not rows:
        unknown = 0
    elif retained_ids is not None and not observed_ids - retained_ids:
        unknown = observed_unknown + missing
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
        customer = {'status': 'unknown' if pending else 'available', 'known_usd': format(charged, 'f'),
                    'complete_usd': None if pending else format(charged, 'f'),
                    'unknown_events': len(pending), 'basis': 'original_customer_ledger'}
    return {'known_api_usd': format(known, 'f'), 'complete_api_usd': format(known, 'f') if coverage and unknown == 0 else None,
            'unknown_events': unknown, 'customer_usage_charge': customer}


def _original_path(result, manifest=None):
    attempts = result.get('attempts')
    if not isinstance(attempts, list) or len(attempts) > LIMITS['max_decision_points']:
        raise SourceUnavailable('original_result_invalid')
    path = []
    for index, attempt in enumerate(attempts):
        if (attempt.get('tier') not in TIERS or attempt.get('stop') not in STOPS
                or type(attempt.get('public', {}).get('passed')) not in (bool, type(None))):
            raise SourceUnavailable('original_result_invalid')
        if manifest is not None and manifest.get('routing_version') != 'qcs-backup-policy-v1':
            from cloudyfan_monitor.saved_qcs_comparison import attempt_identity
            identity = attempt_identity(manifest, attempt)
        else:
            identity = {key: attempt.get(key) for key in ('config_id', 'configuration_sha256')}
        if (identifier(identity['config_id']) is None
                or HASH.fullmatch(str(identity['configuration_sha256'])) is None):
            raise SourceUnavailable('original_result_identity_invalid')
        passed = attempt.get('public', {}).get('passed')
        path.append({'index': index, 'tier': attempt['tier'], **identity,
            'public_status': 'passed' if passed is True else 'failed' if passed is False else 'unknown',
            'stop': attempt['stop']})
    return path




def _original_receipts(source, registered, manifest, result, internal, run):
    from cloudyfan_jev.client import usage
    receipts = []
    total = 0
    seen = set()
    for index, attempt in enumerate(result['attempts']):
        bundle = {'status': 'unavailable', 'receipts_sha256': None, 'validation_sha256': None,
                  'calls': [], 'reason': 'original_receipts_unavailable'}
        try:
            if manifest['routing_version'] == 'qcs-backup-policy-v1':
                profiles = [row['entry']['profile'] for row in manifest['backup_plan']['pools'][attempt['tier']]
                            if row['configuration_sha256'] == attempt['configuration_sha256']
                            and row['entry']['config_id'] == attempt['config_id']]
                if len(profiles) != 1:
                    raise SourceUnavailable('original_result_identity_invalid')
                profile = profiles[0]
            else:
                profile = manifest['profiles'][attempt['tier']]
            calls = attempt.get('calls')
            if not isinstance(calls, list) or not calls:
                raise SourceUnavailable('original_receipts_unavailable')
            for call in calls:
                total += 1
                cid = call.get('call_id')
                if (total > 64 or not isinstance(cid, str) or '..' in cid or cid in seen
                        or re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,159}', cid) is None
                        or not cid.startswith('run-' + internal + '-')):
                    raise SourceUnavailable('original_receipt_identity_invalid')
                seen.add(cid)
                root = Path(registered['receipt_root']) / 'calls' / cid
                intent = source.read(root / 'intent.json', 'original_call_intent')
                response = source.read(root / 'response.json', 'original_call_response')
                response_hash = source.evidence[-1]['sha256']
                actual = usage(response)
                if (intent.get('call_id') != cid or response.get('call_id') != cid
                        or original_digest({'url': intent['url'], 'payload': intent['payload']}) != intent.get('request_sha256')
                        or response.get('request_sha256') != intent['request_sha256']
                        or call.get('configuration_sha256') is not None and call['configuration_sha256'] != attempt.get('configuration_sha256')
                        or intent['payload'].get('model') != profile['model_version']
                        or type(response.get('http_status')) is not int
                        or any(call.get(key) != actual.get(key) for key in ('model', 'provider', 'generation_id'))
                        or actual['model'] is not None and actual['model'] not in profile['accepted_models']
                        or actual['provider'] is not None and actual['provider'] != profile['provider_name']):
                    raise SourceUnavailable('original_receipt_binding_invalid')
                bundle['calls'].append({'call_id': cid, 'request_sha256': intent['request_sha256'],
                    'response_sha256': response_hash, 'http_status': response['http_status'],
                    'cost_known': actual.get('cost_unknown') is False})
            bundle['receipts_sha256'] = digest([row['response_sha256'] for row in bundle['calls']])
            public = source.read(Path(run) / f'validation-a{index}.json', 'original_public_validation')
            if public != attempt['public']:
                raise SourceUnavailable('original_validation_binding_invalid')
            bundle['validation_sha256'] = source.evidence[-1]['sha256']
            bundle.update(status='verified', reason=None)
        except (OSError, ValueError, TypeError, KeyError, SourceUnavailable):
            pass
        receipts.append(bundle)
    return receipts


CANDIDATE_SCOPE = 'first_divergence_from_saved_inputs'
CANDIDATE_FIELDS = ('status', 'reason', 'action', 'selected_tier', 'diverged', 'stop', 'forecast')


def _candidate(source, strategy, inputs, decisions):
    """Evaluate one registered strategy on the same saved inputs, up to its first divergence."""
    from cloudyfan_jev.saved_candidate_strategy import evaluate_candidate_strategy
    rows, stopped = [], False
    for index, public in enumerate(decisions):
        row = {'index': index, 'stage': public.get('stage')}
        if stopped:
            rows.append({**row, 'status': 'not_observed', 'reason': 'after_first_divergence'})
            continue
        if index >= len(inputs):
            rows.append({**row, 'status': 'not_evaluable', 'reason': 'original_decision_inputs_unavailable'})
            continue
        result = source.compute(evaluate_candidate_strategy, inputs[index], strategy)
        original = result['original']
        rows.append({**row, **{key: result[key] for key in CANDIDATE_FIELDS},
            'original': None if original is None else {key: original[key] for key in ('action', 'selected_tier')}})
        stopped = result['stop'] is not None
    statuses = {row['status'] for row in rows}
    status = ('diverged' if any(row.get('diverged') for row in rows)
              else 'not_adoptable' if 'not_adoptable' in statuses
              else 'same_as_original' if rows and statuses == {'evaluated'} else 'not_evaluable')
    return {'strategy': strategy, 'status': status, 'adopted': False, 'scope': CANDIDATE_SCOPE,
            'reasons': sorted({row['reason'] for row in rows if row['status'] != 'evaluated'}), 'decisions': rows}


def _reproduce_saved(source, binding, job, manifest, result, run, trusted, *, original_path, receipts):
    if manifest.get('routing_version') == 'qcs-backup-policy-v1':
        from cloudyfan_monitor.saved_backup_comparison import reproduce_saved_policy
    else:
        from cloudyfan_monitor.saved_qcs_comparison import reproduce_saved_policy
    return reproduce_saved_policy(source, binding, job, manifest, result, run, trusted,
                                  original_path=original_path, receipts=receipts)




if __name__ == '__main__' and sys.argv[1:] == ['--worker']:
    _worker()

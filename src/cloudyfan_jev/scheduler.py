"""Bounded private task admission and explicit workers, with no background service."""
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from http import HTTPStatus
import math
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import uuid
from time import time

from .client import _strict_json
from .http_api import ServiceError, _encode
from .retention import RetentionError


@dataclass(frozen=True)
class QueueLimits:
    capacity: int
    concurrency: int
    per_principal: int
    max_wait_seconds: float

    def __post_init__(self):
        if any(type(v) is not int or v < 1 for v in (self.capacity, self.concurrency, self.per_principal)):
            raise ValueError('Queue capacities must be explicit positive integers')
        if isinstance(self.max_wait_seconds, bool) or not math.isfinite(self.max_wait_seconds) or self.max_wait_seconds <= 0:
            raise ValueError('Queue deadline must be finite and positive')


class QueueScheduler:
    def __init__(self, service, path, *, limits, clock=time, measurement_kind="unqualified"):
        self.service, self.path, self.limits, self.clock = service, Path(path), limits, clock
        if measurement_kind not in {'synthetic', 'unqualified'}:
            raise ValueError('Queue measurement provenance must be explicit')
        self.measurement_kind = measurement_kind
        if self.path.is_symlink(): raise ValueError('Queue storage cannot be a symlink')
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self._transaction() as db:
            db.execute('CREATE TABLE IF NOT EXISTS configuration (singleton INTEGER PRIMARY KEY, body TEXT)')
            config = _encode({'limits': asdict(limits), 'principals': sorted(service.allowed_principals),
                              'service_root': str(service.root.resolve()), 'measurement_kind': measurement_kind}).decode()
            old = db.execute('SELECT body FROM configuration').fetchone()
            if old and old[0] != config: raise ValueError('Frozen queue configuration changed')
            db.execute('INSERT OR IGNORE INTO configuration VALUES (1,?)', (config,))
            db.execute('CREATE TABLE IF NOT EXISTS queue_clock (singleton INTEGER PRIMARY KEY, value REAL)')
            db.execute('CREATE TABLE IF NOT EXISTS maintenance (singleton INTEGER PRIMARY KEY, token TEXT, active INTEGER)')
            db.execute('CREATE TABLE IF NOT EXISTS rejections (principal TEXT, run_id TEXT, reason TEXT, at REAL)')
            db.execute('CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, principal TEXT, run_id TEXT, request TEXT, '
                       'created REAL, deadline REAL, state TEXT, reason TEXT, started REAL, finished REAL, cancel_requested INTEGER DEFAULT 0)')
        os.chmod(self.path, 0o600)
        if getattr(service, 'retention_policy', None) is not None:
            existing = getattr(service, 'retention_guard', None)
            if existing is not None and getattr(getattr(existing, '__self__', None), 'path', None) != self.path:
                raise ValueError('Retention cannot span independent queue stores')
            if self.path.absolute() not in service.retention_protected_paths:
                service.retention_protected_paths += (self.path.absolute(),)
            service.retention_guard = self.retention_snapshot
            service.retention_erase_queue = self.retention_erase
            if service.retention_maintenance is None:
                service.retention_maintenance = self.retention_maintenance

    @contextmanager
    def _transaction(self):
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute('PRAGMA secure_delete=ON')
            db.execute('BEGIN IMMEDIATE')
            yield db
            db.execute('COMMIT')
        except BaseException:
            db.execute('ROLLBACK')
            raise
        finally:
            db.close()

    @contextmanager
    def _task_guard(self, internal_id):
        retention = getattr(self.service, 'retention', None)
        if retention is None:
            yield
            return
        try:
            with retention.guard(internal_id):
                retention.access(internal_id, legacy_exists=(self.service.root / internal_id / 'job.json').exists())
                yield
        except RetentionError as error:
            raise ServiceError(error.code, error.status) from None

    def submit(self, principal, payload):
        self.service.authorize(principal)
        if not isinstance(payload, dict): raise ServiceError('unsupported_task_fields')
        identity = self.service.internal_id(principal, payload.get('run_id'))
        encoded = _encode(payload).decode()
        rejection = None
        with self._task_guard(identity), self._transaction() as db:
            if db.execute('SELECT 1 FROM maintenance WHERE active=1').fetchone():
                raise ServiceError('queue_maintenance', 503)
            self._expire(db)
            prior = db.execute('SELECT * FROM jobs WHERE id=?', (identity,)).fetchone()
            if prior:
                if prior['request'] != encoded: raise ServiceError('frozen_task_changed', 409)
            else:
                queued = db.execute('SELECT COUNT(*) FROM jobs WHERE state="queued"').fetchone()[0]
                active = db.execute('SELECT COUNT(*) FROM jobs WHERE principal=? AND state IN ("queued","running","interrupted_pending")', (principal,)).fetchone()[0]
                if queued >= self.limits.capacity: rejection = 'queue_full'
                elif active >= self.limits.per_principal: rejection = 'principal_quota_full'
                if rejection:
                    db.execute('INSERT INTO rejections VALUES (?,?,?,?)', (principal, payload['run_id'], rejection, self._now(db)))
                else:
                    self.service.create(principal, payload)
                    if getattr(self.service, 'retention_policy', None) is not None:
                        self.service.mark_queued(principal, payload['run_id'])
                    now = self._now(db)
                    db.execute('INSERT INTO jobs (id,principal,run_id,request,created,deadline,state) VALUES (?,?,?,?,?,?,?)',
                               (identity, principal, payload['run_id'], encoded, now, now + self.limits.max_wait_seconds, 'queued'))
        if rejection: raise ServiceError(rejection, 429)
        return self.status(principal, payload['run_id'])

    @contextmanager
    def retention_maintenance(self, internal_id):
        """Persistently pause queue admission/dispatch after all workers stop.

        A crashed owner leaves the queue paused for explicit operator review.
        Direct out-of-band writers remain the application's responsibility.
        """
        token = uuid.uuid4().hex
        with self._transaction() as db:
            if db.execute('SELECT 1 FROM maintenance WHERE active=1').fetchone():
                raise ServiceError('queue_maintenance', 409)
            if db.execute('SELECT 1 FROM jobs WHERE state IN ("running","interrupted_pending")').fetchone():
                raise ServiceError('queue_workers_not_stopped', 409)
            db.execute('INSERT OR REPLACE INTO maintenance VALUES (1,?,1)', (token,))
        try:
            yield {'workers_stopped': True, 'evidence_ref': 'queue-maintenance:' + token}
        finally:
            with self._transaction() as db:
                changed = db.execute('UPDATE maintenance SET active=0 WHERE singleton=1 AND token=? AND active=1',
                                     (token,))
                if changed.rowcount != 1:
                    raise ServiceError('queue_maintenance_changed', 503)

    def retention_snapshot(self, internal_id):
        """Operator-side queue inventory, containing no task prompt."""
        with self._transaction() as db:
            row = db.execute('SELECT request,state FROM jobs WHERE id=?', (internal_id,)).fetchone()
            if row is None:
                return {'terminal': False, 'request_erased': False, 'request_sha256': None,
                        'bytes': 0, 'evidence_ref': 'queue-job-missing'}
            raw = row['request'].encode()
            parsed = json.loads(row['request'])
            return {'terminal': row['state'] in {'completed', 'cancelled', 'expired'},
                    'request_erased': parsed.get('retired') is True,
                    'request_sha256': (parsed.get('request_sha256') if parsed.get('retired') is True
                                       else hashlib.sha256(raw).hexdigest()),
                    'bytes': 0 if parsed.get('retired') is True else len(raw),
                    'evidence_ref': 'queue-job:' + internal_id}

    def retention_erase(self, internal_id, expected_request_sha256, approval_reference):
        """Called only after exact-plan approval under the maintenance fence."""
        if not isinstance(approval_reference, str) or not approval_reference.strip():
            raise ServiceError('retention_approval_required', 403)
        with self._transaction() as db:
            row = db.execute('SELECT request,state FROM jobs WHERE id=?', (internal_id,)).fetchone()
            if row is None or row['state'] not in {'completed', 'cancelled', 'expired'}:
                raise ServiceError('retention_queue_not_terminal', 409)
            if hashlib.sha256(row['request'].encode()).hexdigest() != expected_request_sha256:
                raise ServiceError('retention_queue_changed', 409)
            db.execute('UPDATE jobs SET request=? WHERE id=?',
                       (_encode({'retired': True, 'request_sha256': expected_request_sha256}).decode(), internal_id))

    def _now(self, db):
        now = self.clock()
        old = db.execute('SELECT value FROM queue_clock WHERE singleton=1').fetchone()
        if isinstance(now, bool) or not isinstance(now, (int, float)) or not math.isfinite(now) or (old and now < old[0]):
            raise ServiceError('queue_clock_invalid', 503)
        db.execute('INSERT OR REPLACE INTO queue_clock VALUES (1,?)', (now,))
        return now

    def _expire(self, db):
        now = self._now(db)
        db.execute('UPDATE jobs SET state="expired",reason="queue_deadline_expired",finished=? WHERE state="queued" AND deadline<=?', (now, now))

    def admission_history(self, principal):
        self.service.authorize(principal)
        with self._transaction() as db:
            return [dict(row) for row in db.execute('SELECT run_id,reason,at FROM rejections WHERE principal=? ORDER BY rowid', (principal,))]

    def _job(self, db, principal, run_id):
        identity = self.service.internal_id(principal, run_id)
        row = db.execute('SELECT * FROM jobs WHERE id=?', (identity,)).fetchone()
        if row is None: raise ServiceError('task_not_found', 404)
        return row

    def status(self, principal, run_id):
        with self._transaction() as db:
            self._expire(db)
            row = dict(self._job(db, principal, run_id))
        task = self.service.status(principal, run_id)
        status = task['status'] if row['state'] == 'completed' else row['state']
        return {'run_id': run_id, 'status': status, 'queue_state': row['state'],
                'reason': row['reason'], 'cancellation_requested': bool(row['cancel_requested']),
                'provider_cancellation_confirmed': False, 'costs': task['costs'],
                'measurement_kind': self.measurement_kind, 'production_slo_validated': False,
                'queue_seconds': None if row['started'] is None else row['started'] - row['created'],
                'end_to_end_seconds': None if row['finished'] is None else row['finished'] - row['created']}

    def result(self, principal, run_id):
        with self._transaction() as db: self._job(db, principal, run_id)
        return self.service.result(principal, run_id)

    def recover_inflight(self, *, workers_stopped=False):
        """Operator-only recovery marking; never releases budget or retries work."""
        if workers_stopped is not True:
            raise ValueError('Confirm all prior workers stopped before recovery marking')
        with self._transaction() as db:
            changed = db.execute('UPDATE jobs SET state="interrupted_pending",reason="worker_interrupted" WHERE state="running"')
            return changed.rowcount

    def cancel(self, principal, run_id):
        with self._transaction() as db:
            self._expire(db)
            row = self._job(db, principal, run_id)
            if row['state'] == 'queued':
                db.execute('UPDATE jobs SET state="cancelled",reason="cancelled_before_dispatch",finished=?,cancel_requested=1 WHERE id=?', (self._now(db), row['id']))
            elif row['state'] in {'running', 'interrupted_pending'}:
                db.execute('UPDATE jobs SET cancel_requested=1 WHERE id=?', (row['id'],))
        return self.status(principal, run_id)

    def dispatch_next(self):
        with self._transaction() as db:
            if db.execute('SELECT 1 FROM maintenance WHERE active=1').fetchone():
                return None
            self._expire(db)
            active = db.execute('SELECT COUNT(*) FROM jobs WHERE state IN ("running","interrupted_pending")').fetchone()[0]
            if active >= self.limits.concurrency: return None
            row = db.execute('SELECT * FROM jobs WHERE state="queued" ORDER BY created,rowid LIMIT 1').fetchone()
            if row is None: return None
            db.execute('UPDATE jobs SET state="running",started=? WHERE id=?', (self._now(db), row['id']))
        try:
            self.service.execute(row['principal'], row['run_id'])
        except Exception as error:
            reason = error.code if isinstance(error, ServiceError) else 'execution_interrupted'
            with self._transaction() as db:
                db.execute('UPDATE jobs SET state="interrupted_pending",reason=? WHERE id=?', (reason, row['id']))
        else:
            with self._transaction() as db:
                db.execute('UPDATE jobs SET state="completed",finished=? WHERE id=?', (self._now(db), row['id']))
        return self.status(row['principal'], row['run_id'])


class QueueHTTPApp:
    """Authenticated WSGI queue entry. Only explicit worker dispatch executes jobs."""
    def __init__(self, queue, *, authenticate):
        self.queue, self.authenticate = queue, authenticate

    def __call__(self, environ, start_response):
        code = 200
        try:
            principal = self.authenticate(environ)
            self.queue.service.authorize(principal)
            path, method = environ.get('PATH_INFO', ''), environ['REQUEST_METHOD']
            match = re.fullmatch(r'/v1/tasks/([A-Za-z0-9][A-Za-z0-9_-]{0,63})(/result|/cancel|/resume)?', path)
            if method == 'POST' and path == '/v1/tasks':
                body = self.queue.submit(principal, self._body(environ))
                code = 202 if body['status'] == 'queued' else 200
            elif method == 'POST' and match and match[2] == '/cancel':
                if self._body(environ) != {}: raise ServiceError('cancel_does_not_accept_configuration')
                body = self.queue.cancel(principal, match[1])
            elif method == 'GET' and match and match[2] in (None, '/result'):
                body = self.queue.result(principal, match[1]) if match[2] else self.queue.status(principal, match[1])
            else:
                raise ServiceError('endpoint_not_supported', 404)
        except ServiceError as error:
            code, body = error.status, error.body()
        except Exception:
            code, body = 500, {'error': {'code': 'queue_interrupted'}}
        encoded = _encode(body)
        start_response(f'{code} {HTTPStatus(code).phrase}', [('Content-Type', 'application/json'),
                       ('Content-Length', str(len(encoded))), ('Cache-Control', 'no-store')])
        return [encoded]

    @staticmethod
    def _body(environ):
        if environ.get('CONTENT_TYPE', '').split(';')[0] != 'application/json':
            raise ServiceError('json_required', 415)
        try:
            length = int(environ.get('CONTENT_LENGTH') or 0)
            if length < 1 or length > 65536: raise ValueError()
            return _strict_json(environ['wsgi.input'].read(length))
        except (ValueError, UnicodeError):
            raise ServiceError('invalid_json_or_length', 422) from None

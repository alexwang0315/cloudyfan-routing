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
    def __init__(self, service, path, *, limits, clock=time, measurement_kind="unqualified", capacity_protocol=None):
        self.service, self.path, self.limits, self.clock = service, Path(path), limits, clock
        self.capacity_protocol = capacity_protocol
        self.transition_observer = None  # Optional trusted host projection only.
        if capacity_protocol is not None:
            from .capacity_queue import QueuePreparation
            if (type(capacity_protocol) is not QueuePreparation or service.capacity_controller is None
                    or getattr(service.capacity_controller, 'queue_backend', None) is not capacity_protocol):
                raise ValueError('Explicit controlled queue preparation and capacity controller required')
            for name, contract in service.contracts.items():
                capacity_protocol.verify_contract(name, contract)
        if measurement_kind not in {'synthetic', 'unqualified'}:
            raise ValueError('Queue measurement provenance must be explicit')
        self.measurement_kind = measurement_kind
        if self.path.is_symlink(): raise ValueError('Queue storage cannot be a symlink')
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.integrity = None
        if capacity_protocol is not None:
            from .capacity_integrity import CapacityIntegrity
            fresh = not self.path.exists()
            if fresh:
                descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                os.close(descriptor)
            self.integrity = CapacityIntegrity(self.path, fresh=fresh, role='queue')
            self.integrity.bind(service.root, 'queue')
        unavailable = self.integrity is not None and self.integrity.status()['status'] != 'verified'
        value = {'limits': asdict(limits), 'principals': sorted(service.allowed_principals),
                 'service_root': str(service.root.resolve()), 'measurement_kind': measurement_kind}
        if capacity_protocol is not None:
            value['capacity_preparation'] = capacity_protocol.descriptor()
        config = _encode(value).decode()
        observable = not unavailable or self.integrity.status()['retained_verified_metadata']
        if observable:
            with self._readonly() if unavailable else self._transaction() as db:
                if unavailable:
                    old = db.execute('SELECT body FROM configuration').fetchone()
                    if old and old[0] != config:
                        raise ValueError('Frozen queue configuration changed')
                else:
                    self._initialize(db, config)
        os.chmod(self.path, 0o600)
        if capacity_protocol is not None:
            service.queue_scheduler = self
        if getattr(service, 'retention_policy', None) is not None:
            existing = getattr(service, 'retention_guard', None)
            if existing is not None and getattr(getattr(existing, '__self__', None), 'path', None) != self.path:
                raise ValueError('Retention cannot span independent queue stores')
            if self.path.absolute() not in service.retention_protected_paths:
                service.retention_protected_paths += (self.path.absolute(),)
            if self.integrity is not None and self.integrity.root not in service.retention_protected_paths:
                service.retention_protected_paths += (self.integrity.root,)
            service.retention_guard = self.retention_snapshot
            service.retention_erase_queue = self.retention_erase
            if service.retention_maintenance is None:
                service.retention_maintenance = self.retention_maintenance

    def _initialize(self, db, config):
        db.execute('CREATE TABLE IF NOT EXISTS configuration (singleton INTEGER PRIMARY KEY, body TEXT)')
        old = db.execute('SELECT body FROM configuration').fetchone()
        if old and old[0] != config: raise ValueError('Frozen queue configuration changed')
        db.execute('INSERT OR IGNORE INTO configuration VALUES (1,?)', (config,))
        db.execute('CREATE TABLE IF NOT EXISTS queue_clock (singleton INTEGER PRIMARY KEY, value REAL)')
        db.execute('CREATE TABLE IF NOT EXISTS maintenance (singleton INTEGER PRIMARY KEY, token TEXT, active INTEGER)')
        db.execute('CREATE TABLE IF NOT EXISTS rejections (principal TEXT, run_id TEXT, reason TEXT, at REAL)')
        db.execute('CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, principal TEXT, run_id TEXT, request TEXT, '
                   'created REAL, deadline REAL, state TEXT, reason TEXT, started REAL, finished REAL, cancel_requested INTEGER DEFAULT 0)')
        if self.capacity_protocol is not None:
            columns = {row[1] for row in db.execute('PRAGMA table_info(jobs)')}
            for name, declaration in (('claim', 'TEXT'), ('epoch', 'INTEGER DEFAULT 0'), ('detail', 'TEXT'),
                                      ('waiting_since', 'REAL'), ('wait_seconds', 'REAL DEFAULT 0')):
                if name not in columns:
                    db.execute('ALTER TABLE jobs ADD COLUMN ' + name + ' ' + declaration)

    @contextmanager
    def _transaction(self):
        if self.integrity is not None:
            self.integrity.require(groups=[])
            with self.integrity.transaction(write=True) as db:
                db.row_factory = sqlite3.Row
                db.execute('PRAGMA secure_delete=ON')
                db.execute('BEGIN IMMEDIATE')
                yield db
            return
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
    def _readonly(self):
        if self.integrity is not None:
            with self.integrity.transaction(projection=True) as db:
                db.row_factory = sqlite3.Row
                db.execute('PRAGMA query_only=ON')
                yield db
            return
        db = sqlite3.connect(self.path.resolve().as_uri() + '?mode=ro', uri=True, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            db.execute('PRAGMA query_only=ON')
            yield db
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
        if self.capacity_protocol is not None:
            return self._capacity_submit(principal, payload)
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

    def _capacity_submit(self, principal, payload):
        self.service.authorize(principal)
        if not isinstance(payload, dict) or set(payload) != {'run_id', 'contract_id', 'prompt'}:
            raise ServiceError('unsupported_task_fields')
        identity = self.service.internal_id(principal, payload['run_id'])
        self.integrity.require(task=identity)
        contract = self.service.contracts.get(payload['contract_id'])
        if contract is None:
            raise ServiceError('unregistered_contract')
        self.capacity_protocol.verify_contract(payload['contract_id'], contract)
        encoded = _encode(payload).decode()
        with self._readonly() as db:
            prior = db.execute('SELECT * FROM jobs WHERE id=?', (identity,)).fetchone()
            if prior is not None:
                if prior['request'] != encoded:
                    raise ServiceError('frozen_task_changed', 409)
                if prior['state'] != 'accepting':
                    return self.status(principal, payload['run_id'])
        rejection = None
        # This committed accepting row is a recovery journal across the queue,
        # original TaskService job and capacity adoption stores. No cross-store
        # transaction is claimed; resubmit can only finish the same frozen row.
        with self._transaction() as db:
            if db.execute('SELECT 1 FROM maintenance WHERE active=1').fetchone():
                raise ServiceError('queue_maintenance', 503)
            self._expire(db)
            prior = db.execute('SELECT * FROM jobs WHERE id=?', (identity,)).fetchone()
            if prior:
                if prior['request'] != encoded:
                    raise ServiceError('frozen_task_changed', 409)
            else:
                now = self._now(db)
                with self.integrity.selection(db) as selection:
                    count = selection.execute('SELECT COUNT(*) FROM jobs WHERE state IN ("queued","accepting") AND deadline>?', (now,)).fetchone()[0]
                    active = selection.execute('SELECT COUNT(*) FROM jobs WHERE principal=? AND '
                        '(state IN ("claiming","running","interrupted_pending") OR '
                        '(state IN ("accepting","queued") AND deadline>?))', (principal, now)).fetchone()[0]
                if count >= self.limits.capacity:
                    rejection = 'queue_full'
                elif active >= self.limits.per_principal:
                    rejection = 'principal_quota_full'
                now = self._now(db)
                if rejection:
                    db.execute('INSERT INTO rejections VALUES(?,?,?,?)', (principal, payload['run_id'], rejection, now))
                else:
                    db.execute('INSERT INTO jobs(id,principal,run_id,request,created,deadline,state,reason,waiting_since) VALUES(?,?,?,?,?,?,?,?,?)',
                        (identity, principal, payload['run_id'], encoded, now, now + self.limits.max_wait_seconds,
                         'accepting', 'queue_acceptance_incomplete', now))
        if rejection:
            raise ServiceError(rejection, 429)
        with self._readonly() as db:
            state = self._job(db, principal, payload['run_id'])['state']
        if state == 'accepting':
            try:
                with self.capacity_protocol.accepting(self):
                    self.service.create(principal, payload)
                    with self._transaction() as db:
                        accepted = dict(self._job(db, principal, payload['run_id']))
                        if accepted['state'] == 'accepting' and not accepted['cancel_requested']:
                            self.capacity_protocol.accept(self.service, accepted)
                            self.service.mark_queued(principal, payload['run_id'])
                            db.execute('UPDATE jobs SET state="queued",reason="queue_waiting_worker" WHERE id=?', (identity,))
                        else:
                            self.service.capacity_controller.terminal(identity)
            except ServiceError as error:
                if error.code == 'capacity_acceptance_closed':
                    return self.status(principal, payload['run_id'])
                if error.code != 'task_busy':
                    if error.code in {'queue_environment_contract_changed', 'queue_preparation_contract_changed'}:
                        with self._transaction() as db:
                            row = dict(self._job(db, principal, payload['run_id']))
                            if row['state'] == 'accepting':
                                self._close_waiting(db, row, 'rejected', error.code, self._now(db))
                    raise
                # The committed original acceptance is still being completed
                # under its original nonblocking service/retention lock. This
                # duplicate is a pending lookup, not a second execution owner.
                return self.status(principal, payload['run_id'])
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
            if db.execute('SELECT 1 FROM jobs WHERE state IN ("claiming","running","interrupted_pending")').fetchone():
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
        with self._readonly() as db:
            row = db.execute('SELECT request,state,deadline FROM jobs WHERE id=?', (internal_id,)).fetchone()
            if row is None:
                return {'terminal': False, 'request_erased': False, 'request_sha256': None,
                        'bytes': 0, 'evidence_ref': 'queue-job-missing'}
            raw = row['request'].encode()
            parsed = json.loads(row['request'])
            terminal = (row['state'] in {'completed', 'cancelled', 'expired', 'rejected'}
                or row['state'] in {'queued', 'accepting'} and row['deadline'] <= self._observed_now(db))
            return {'terminal': terminal,
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
            row = db.execute('SELECT request,state,deadline FROM jobs WHERE id=?', (internal_id,)).fetchone()
            if (row is not None and row['state'] in {'queued', 'accepting'}
                    and row['deadline'] <= self._observed_now(db)):
                # Only the already-approved exact retention maintenance persists
                # the expiry that readonly status/preview previously projected.
                # Original billing/intent checks still decide eligibility; this
                # never treats running/uncertain work as terminal or paid as zero.
                db.execute('UPDATE jobs SET state="expired",reason="queue_deadline_expired",finished=? WHERE id=?',
                    (row['deadline'], internal_id))
                row = db.execute('SELECT request,state FROM jobs WHERE id=?', (internal_id,)).fetchone()
            if row is None or row['state'] not in {'completed', 'cancelled', 'expired', 'rejected'}:
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
        if self.capacity_protocol is None:
            db.execute('UPDATE jobs SET state="expired",reason="queue_deadline_expired",finished=? WHERE state="queued" AND deadline<=?', (now, now))
        else:
            with self.integrity.selection(db) as selection:
                selection.row_factory = sqlite3.Row
                rows = selection.execute('SELECT * FROM jobs WHERE state IN ("queued","accepting") AND deadline<=?', (now,)).fetchall()
            for row in rows:
                from .capacity_integrity import CapacityIntegrityError
                try:
                    self.integrity.require(task=row['id'])
                except CapacityIntegrityError:
                    # Preserve the damaged original and its original deadline.
                    # Expiry of A is not a reason to stop unrelated B or certify A.
                    continue
                self._close_waiting(db, row, 'expired', 'queue_deadline_expired', row['deadline'])

    def _close_waiting(self, db, row, state, reason, finished):
        from .budget import BudgetError
        try:
            request = json.loads(row['request'])
            self.service.contracts[request['contract_id']].runner.store.finish(row['id'])
        except BudgetError:
            db.execute('UPDATE jobs SET state="interrupted_pending",reason="unknown_cost" WHERE id=?', (row['id'],))
            return
        self.service.capacity_controller.terminal(row['id'])
        since = row['waiting_since'] if row['waiting_since'] is not None else row['created'] if row['started'] is None else finished
        db.execute('UPDATE jobs SET state=?,reason=?,finished=?,wait_seconds=wait_seconds+?,waiting_since=NULL WHERE id=?',
            (state, reason, finished, max(0, finished-since), row['id']))

    def admission_history(self, principal):
        self.service.authorize(principal)
        with self._transaction() as db:
            return [dict(row) for row in db.execute('SELECT run_id,reason,at FROM rejections WHERE principal=? ORDER BY rowid', (principal,))]

    def _job(self, db, principal, run_id):
        identity = self.service.internal_id(principal, run_id)
        row = db.execute('SELECT * FROM jobs WHERE id=?', (identity,)).fetchone()
        if row is None: raise ServiceError('task_not_found', 404)
        return row

    def contains(self, principal, run_id):
        identity = self.service.internal_id(principal, run_id)
        with self._readonly() as db:
            return db.execute('SELECT 1 FROM jobs WHERE id=?', (identity,)).fetchone() is not None

    def status(self, principal, run_id):
        self.service.authorize(principal)
        with self._readonly() as db:
            row = dict(self._job(db, principal, run_id))
            now = self._observed_now(db)
        if self.capacity_protocol is not None and self.service.retention_policy is not None:
            lifecycle = self.service.retention.lookup(row['id'])
            expires = lifecycle['expires_at'] if lifecycle is not None else row['created'] + self.service.retention_policy.ttl_seconds
            if now >= expires:
                raise ServiceError('task_expired', 410)
            if lifecycle is not None:
                self.service.queue_access(principal, run_id)
        return self._status(row, now)

    def _observed_now(self, db):
        now = self.clock()
        floor = db.execute('SELECT value FROM queue_clock WHERE singleton=1').fetchone()
        if isinstance(now, bool) or not isinstance(now, (int, float)) or not math.isfinite(now) or floor and now < floor[0]:
            raise ServiceError('queue_clock_invalid', 503)
        return now

    def _status(self, row, now, *, unavailable=None):
        principal, run_id = row['principal'], row['run_id']
        waiting_since = row.get('waiting_since')
        elapsed_wait = row.get('wait_seconds') or 0
        if row['state'] in {'queued', 'accepting', 'claiming'}:
            since = waiting_since if waiting_since is not None else row['created'] if row['started'] is None else now
            elapsed_wait += max(0, min(now, row['deadline'])-since)
        if row['state'] in {'queued', 'accepting'} and now >= row['deadline']:
            row = {**row, 'state': 'expired', 'reason': 'queue_deadline_expired', 'finished': row['deadline']}
        if unavailable is not None:
            task = {'status': row['state'], 'costs': {'complete_api_usd': None, 'known_api_usd': None, 'unknown_events': None}}
        elif (row['state'] == 'accepting' or row['state'] in {'cancelled', 'expired', 'rejected'}
                and row['started'] is None and not (self.service.root / row['id'] / 'job.json').exists()):
            task = {'status': row['state'], 'costs': {'complete_api_usd': None if row['state'] == 'accepting' else '0',
                'known_api_usd': '0', 'unknown_events': 0}}
        else:
            task = (self.service.queue_observe(principal, run_id) if self.capacity_protocol is not None
                    else self.service.status(principal, run_id))
        status = task['status'] if row['state'] == 'completed' else row['state']
        view = {'run_id': run_id, 'status': status, 'queue_state': row['state'],
                'reason': row['reason'], 'cancellation_requested': bool(row['cancel_requested']),
                'provider_cancellation_confirmed': False, 'costs': task['costs'],
                'measurement_kind': self.measurement_kind, 'production_slo_validated': False,
                'queue_seconds': None if row['started'] is None else row['started'] - row['created'],
                'end_to_end_seconds': None if row['finished'] is None else row['finished'] - row['created']}
        if self.capacity_protocol is not None:
            view['queue'] = {'protocol': self.capacity_protocol.protocol, 'version': self.capacity_protocol.version,
                'limits': asdict(self.limits), 'accepted_at': row['created'], 'deadline': row['deadline'],
                'waiting_seconds': elapsed_wait,
                'effective_expiry_only': row['state'] == 'expired' and row['finished'] == row['deadline']}
            try:
                directory, job = self.service._job(principal, run_id)
                adoption = self.service.capacity_controller.read_context(row['id'], principal, job['registered'])
                with self.service.capacity_controller.store.connection() as db:
                    policy_row = db.execute('SELECT payload FROM capacity_policies WHERE version=?', (adoption['policy_version'],)).fetchone()
                policy = json.loads(policy_row[0]) if policy_row is not None else {}
                lifecycle = self.service.retention.lookup(row['id']) if self.service.retention is not None else None
                view['queue']['frozen'] = {'contract_id': job['request']['contract_id'],
                    'contract_version': job['registered']['version'], 'registered_sha256': hashlib.sha256(_encode(job['registered'])).hexdigest(),
                    'mode': job['registered']['mode'], 'access_expires_at': lifecycle['expires_at'] if lifecycle else None,
                    'capacity': {'policy_version': adoption['policy_version'], **{key: policy[key] for key in
                        ('configuration_version', 'configuration_sha256', 'queue_version', 'windows_version') if key in policy}}}
            except (ServiceError, ValueError, KeyError, RuntimeError):
                view['queue']['frozen'] = {'status': 'unavailable'}
            if unavailable is not None:
                view['observation_error'] = unavailable
            detail = json.loads(row.get('detail') or '{}')
            view['capacity_reason'] = detail.get('code')
            if row['state'] in {'queued', 'accepting', 'claiming', 'running', 'interrupted_pending'}:
                view['costs'] = {**task['costs'], 'complete_api_usd': None}
        return view

    def snapshot(self):
        """Trusted operator projection. No prompts, credentials or body paths."""
        if self.integrity is not None and not self.integrity.status()['retained_verified_metadata']:
            return {'schema_version': 'capacity-queue-v1', 'configured': True,
                'version': self.capacity_protocol.version, 'limits': asdict(self.limits),
                'now': None, 'jobs': [], 'job_count': None, 'active_count': None, 'retained_active_count': None,
                'active_history_complete': False, 'rejections': [], 'rejection_count': None,
                'rejection_history_complete': False, 'history_complete': False,
                'measurement_kind': self.measurement_kind, 'production_slo_validated': False,
                'integrity': self.integrity.status(), 'observation_status': 'unavailable'}
        with self._readonly() as db:
            now = self._observed_now(db)
            active = '(state IN ("claiming","running","interrupted_pending") OR (state IN ("accepting","queued") AND deadline>?))'
            rows = [dict(row) for row in db.execute('SELECT * FROM jobs ORDER BY ' + active + ' DESC,created DESC,rowid DESC LIMIT 100', (now,))]
            total = db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0]
            active_count = db.execute('SELECT COUNT(*) FROM jobs WHERE ' + active, (now,)).fetchone()[0]
            retained_active = db.execute('SELECT COUNT(*) FROM jobs WHERE state IN ("accepting","queued","claiming","running","interrupted_pending")').fetchone()[0]
            rejections = [dict(row) for row in db.execute('SELECT principal AS client,run_id,reason,at FROM rejections ORDER BY rowid DESC LIMIT 100')]
            rejection_count = db.execute('SELECT COUNT(*) FROM rejections').fetchone()[0]
        jobs = []
        for row in rows:
            try:
                view = self._status(row, now)
                view['physical_groups'] = json.loads(row.get('detail') or '{}').get('groups', [])
                jobs.append({'client': row['principal'], **view})
            except ServiceError as error:
                jobs.append({'client': row['principal'], **self._status(row, now, unavailable=error.code)})
        return {'schema_version': 'capacity-queue-v1', 'configured': self.capacity_protocol is not None,
                'version': self.capacity_protocol.version if self.capacity_protocol is not None else None,
                'limits': asdict(self.limits), 'now': now, 'jobs': jobs, 'job_count': total,
                'active_count': active_count, 'retained_active_count': retained_active,
                'active_history_complete': active_count == sum(row['state'] in {'claiming', 'running', 'interrupted_pending'}
                    or row['state'] in {'accepting', 'queued'} and row['deadline'] > now for row in rows),
                'rejections': rejections, 'rejection_count': rejection_count,
                'rejection_history_complete': rejection_count == len(rejections),
                'history_complete': total == len(rows), 'measurement_kind': self.measurement_kind,
                **({'integrity': self.integrity.status()} if self.integrity is not None else {}),
                'production_slo_validated': False}

    def _notify_transition(self, row):
        if callable(self.transition_observer):
            try:
                self.transition_observer(row['principal'], row['run_id'])
            except Exception:
                # Projection failure cannot change original dispatch/receipts.
                pass

    def result(self, principal, run_id):
        with self._readonly() as db: self._job(db, principal, run_id)
        return self.service.result(principal, run_id, **({'refresh': False} if self.capacity_protocol is not None else {}))

    def recover_inflight(self, *, workers_stopped=False):
        """Operator-only recovery marking; never releases budget or retries work."""
        if workers_stopped is not True:
            raise ValueError('Confirm all prior workers stopped before recovery marking')
        with self._transaction() as db:
            changed = db.execute('UPDATE jobs SET state="interrupted_pending",reason="worker_interrupted" WHERE state IN ("running","claiming")')
            return changed.rowcount

    def cancel(self, principal, run_id):
        self.service.authorize(principal)
        if self.integrity is not None:
            self.integrity.require(task=self.service.internal_id(principal, run_id))
        with self._transaction() as db:
            self._expire(db)
            row = self._job(db, principal, run_id)
            if row['state'] in {'queued', 'accepting'}:
                if self.capacity_protocol is None:
                    db.execute('UPDATE jobs SET state="cancelled",reason="cancelled_before_dispatch",finished=?,cancel_requested=1 WHERE id=?', (self._now(db), row['id']))
                else:
                    reason = 'cancelled_before_next_dispatch' if row['started'] is not None else 'cancelled_before_dispatch'
                    self._close_waiting(db, row, 'cancelled', reason, self._now(db))
                    db.execute('UPDATE jobs SET cancel_requested=1 WHERE id=?', (row['id'],))
            elif row['state'] in {'claiming', 'running', 'interrupted_pending'}:
                db.execute('UPDATE jobs SET cancel_requested=1 WHERE id=?', (row['id'],))
        return self.status(principal, run_id)

    def dispatch_next(self):
        if self.capacity_protocol is not None:
            return self._capacity_dispatch_next()
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

    def verify_claim(self, identity, claim):
        from .capacity_dispatch import CapacityDenied
        self.integrity.require(task=identity)
        with self._readonly() as db:
            row = db.execute('SELECT * FROM jobs WHERE id=?', (identity,)).fetchone()
            now = self._observed_now(db)
        if row is None or row['claim'] != claim or row['state'] != 'running':
            raise CapacityDenied('capacity_queue_claim_invalid', 403)
        if row['cancel_requested']:
            raise CapacityDenied('capacity_queue_cancel_requested')
        if now >= row['deadline']:
            raise CapacityDenied('capacity_queue_deadline_exceeded')
        self.service.authorize(row['principal'])

    def _capacity_dispatch_next(self):
        from .capacity_dispatch import CapacityDenied
        from .capacity_queue import QueueWaiting
        from .budget import BudgetError
        selected = None
        with self._transaction() as db:
            if db.execute('SELECT 1 FROM maintenance WHERE active=1').fetchone():
                return None
            self._expire(db)
            with self.integrity.selection(db) as selection:
                selection.row_factory = sqlite3.Row
                active = selection.execute('SELECT COUNT(*) FROM jobs WHERE state IN ("claiming","running","interrupted_pending")').fetchone()[0]
                candidates = selection.execute('SELECT * FROM jobs WHERE state="queued" ORDER BY created,rowid').fetchall()
            if active >= self.limits.concurrency:
                db.execute('UPDATE jobs SET reason="queue_waiting_slot" WHERE state="queued"')
                return None
            blocked = set()
            # The queue itself is bounded; old terminal history is not scanned.
            for value in candidates:
                row = dict(value)
                prepared = None
                from .capacity_integrity import CapacityIntegrityError
                try:
                    self.integrity.require(task=row['id'])
                except CapacityIntegrityError:
                    blocked.update(json.loads(row.get('detail') or '{}').get('groups', []))
                    continue
                try:
                    self.service.authorize(row['principal'])
                    self.service.queue_observe(row['principal'], row['run_id'])
                    prepared = self.capacity_protocol.prepare_next(self.service, row)
                    groups = prepared['groups']
                    detail = {'groups': sorted(groups)}
                    if groups & blocked:
                        db.execute('UPDATE jobs SET reason="queue_waiting_turn",detail=? WHERE id=?', (_encode(detail).decode(), row['id']))
                        blocked.update(groups)
                        continue
                    prepared['dispatch'].prepare(prepared['request']['call_id'], prepared['wire'], reserve=False)
                    runner = prepared['contract'].runner
                    runner.store.queue_admission(row['id'], runner._admit(prepared['contract'].mode))
                    runner.queue_preflight(prepared['transport'], prepared['request']['endpoint'], prepared['request']['payload'])
                except (CapacityDenied, ServiceError, BudgetError, QueueWaiting) as error:
                    code = getattr(error, 'code', 'capacity_original_budget_denied')
                    groups = prepared['groups'] if prepared is not None else set()
                    detail = {'groups': sorted(groups), 'code': code}
                    terminal = (isinstance(error, ServiceError) and error.status in (401, 403, 404, 409, 410)
                        or code in {'capacity_credential_inactive', 'capacity_deadline_exceeded',
                                    'capacity_adoption_unavailable', 'capacity_queue_contract_changed', 'capacity_original_deadline_exceeded'})
                    if terminal:
                        db.execute('UPDATE jobs SET detail=? WHERE id=?', (_encode(detail).decode(), row['id']))
                        self._close_waiting(db, row, 'rejected', 'queue_original_authority_denied', self._now(db))
                    else:
                        reason = ('queue_waiting_budget' if isinstance(error, BudgetError) else
                                  'queue_waiting_window' if 'window' in code else 'queue_waiting_capacity')
                        db.execute('UPDATE jobs SET reason=?,detail=? WHERE id=?', (reason, _encode(detail).decode(), row['id']))
                        blocked.update(groups)
                    continue
                except Exception:
                    db.execute('UPDATE jobs SET reason="queue_preparation_unavailable" WHERE id=?', (row['id'],))
                    # Unknown preparation must not let later overlapping work
                    # guess its groups. Keep this bounded pass fail-closed.
                    break
                claim = uuid.uuid4().hex
                self.capacity_protocol.save_boundary(self.service, row, prepared['request'])
                db.execute('UPDATE jobs SET state="claiming",claim=?,epoch=epoch+1,reason="queue_claiming_capacity",detail=? WHERE id=? AND state="queued"',
                    (claim, _encode(detail).decode(), row['id']))
                selected = row, prepared, claim
                break
        if selected is None:
            return None
        row, prepared, claim = selected
        dispatch = prepared['dispatch']
        try:
            evidence = dispatch.prepare(prepared['request']['call_id'], prepared['wire'])
        except CapacityDenied as error:
            with self._transaction() as db:
                db.execute('UPDATE jobs SET state="queued",reason="queue_waiting_capacity",detail=? WHERE id=? AND claim=? AND state="claiming"',
                    (_encode({'groups': sorted(prepared['groups']), 'code': error.code}).decode(), row['id'], claim))
            return self.status(row['principal'], row['run_id'])
        except BaseException:
            # A failed/uncertain cross-store claim is not automatically freed.
            with self._transaction() as db:
                db.execute('UPDATE jobs SET state="interrupted_pending",reason="queue_claim_interrupted" WHERE id=? AND claim=?', (row['id'], claim))
            raise
        with self._transaction() as db:
            current = db.execute('SELECT * FROM jobs WHERE id=?', (row['id'],)).fetchone()
            now = self._now(db)
            if current['state'] != 'claiming' or current['claim'] != claim:
                dispatch.release()  # This process has never granted send access.
                raise ServiceError('queue_claim_changed', 409)
            if current['cancel_requested'] or now >= current['deadline']:
                dispatch.release()
                state, reason = (('cancelled', 'cancelled_before_next_dispatch' if current['started'] is not None else 'cancelled_before_dispatch')
                    if current['cancel_requested'] else ('expired', 'queue_deadline_expired'))
                self._close_waiting(db, current, state, reason, now)
                return self._status(dict(db.execute('SELECT * FROM jobs WHERE id=?', (row['id'],)).fetchone()), now)
            since = current['waiting_since'] if current['waiting_since'] is not None else current['created'] if current['started'] is None else now
            db.execute('UPDATE jobs SET state="running",started=COALESCE(started,?),reason=NULL,wait_seconds=wait_seconds+?,waiting_since=NULL WHERE id=?',
                       (now, max(0, now-since), row['id']))
        self._notify_transition(row)
        try:
            with self.capacity_protocol.running(self, row, prepared, claim) as execution:
                execution['evidence'] = evidence
                state = self.service.execute(row['principal'], row['run_id'])
            with self._transaction() as db:
                waiting = state.get('status') == 'queued'
                interrupted = state.get('status') in {'engineering_interrupted', 'accounting_interrupted'}
                db.execute('UPDATE jobs SET state=?,reason=?,finished=?,waiting_since=? WHERE id=? AND claim=? AND state="running"',
                    ('queued' if waiting else 'interrupted_pending' if interrupted else 'completed',
                     'queue_waiting_worker' if waiting else state.get('error', {}).get('code'),
                     None if waiting or interrupted else self._now(db), self._now(db) if waiting else None, row['id'], claim))
            if not execution['consumed']:
                dispatch.release()  # Original gates refused before a paid intent.
        except Exception as error:
            reason = error.code if isinstance(error, ServiceError) else 'execution_interrupted'
            with self._transaction() as db:
                db.execute('UPDATE jobs SET state="interrupted_pending",reason=? WHERE id=? AND claim=?', (reason, row['id'], claim))
        self._notify_transition(row)
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

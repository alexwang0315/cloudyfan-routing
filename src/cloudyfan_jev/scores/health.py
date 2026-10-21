"""Offline provider observations and a durable, explicitly recovered circuit.

Health never supplies model quality, prices, paid probes or candidate membership.
All time/uptime/speed limits come from the caller's frozen policy. A scalar
min_uptime_percent explicitly applies to all three windows; a mapping selects
particular native windows. Null speed limits explicitly disable those checks.
"""
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import sqlite3

from .catalog import canonical, digest, number, timestamp
from .metrics import normalize_provider_health, UPTIME_FIELDS

_HASH = re.compile(r'[0-9a-f]{64}\Z')
_PERCENTILES = ('p50', 'p75', 'p90', 'p99')


class HealthError(RuntimeError):
    """Corrupt storage, changed policy or a refused circuit recovery."""
    control_plane_error = True


def _identity(configuration_sha256, provider):
    if not isinstance(configuration_sha256, str) or not _HASH.fullmatch(configuration_sha256):
        raise ValueError('Explicit configuration SHA256 required')
    if not isinstance(provider, str) or not provider.strip() or len(provider) > 512:
        raise ValueError('Explicit provider identity required')


def _policy(value):
    if not isinstance(value, dict):
        raise ValueError('Explicit health policy required')
    result = deepcopy(value)
    for key in ('ttl_seconds', 'cooldown_seconds'):
        if number(result[key]) <= 0:
            raise ValueError('Positive health time limits required')
    if type(result['failure_threshold']) is not int or result['failure_threshold'] < 1:
        raise ValueError('Positive integer failure threshold required')
    minimum = result['min_uptime_percent']
    if not isinstance(minimum, dict):
        minimum = dict.fromkeys(UPTIME_FIELDS, minimum)
    if not minimum or any(k not in UPTIME_FIELDS for k in minimum):
        raise ValueError('Explicit native uptime windows required')
    for threshold in minimum.values():
        if not 0 <= number(threshold) <= 100:
            raise ValueError('Invalid minimum uptime percent')
    result['min_uptime_percent'] = minimum
    for percentile, limit in (('latency_percentile', 'max_latency_ms'),
                              ('throughput_percentile', 'min_throughput')):
        if result[percentile] not in _PERCENTILES:
            raise ValueError('Explicit supported speed percentile required')
        if result[limit] is not None and number(result[limit]) < 0:
            raise ValueError('Nonnegative speed threshold or explicit null required')
    canonical(result)
    return result


def _safe_directory(path):
    path = Path(os.path.abspath(path))
    for component in (*reversed(path.parents), path):
        if component.is_symlink():
            raise HealthError('Health storage path cannot contain symlinks')
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not path.is_dir():
        raise HealthError('Health storage root must be a directory')
    return path


class HealthStore:
    """Immutable SQLite snapshot/event rows, serialized across processes/threads.

    publish returns a computed content hash. assess returns a dictionary with
    eligible/reasons/snapshot_id/observation/circuit. record is event-idempotent;
    successes never close an open circuit. recover requires the current matching
    snapshot, a fresh qualified observation newer than the last failure, and an
    elapsed cooldown. Merely waiting cannot make an endpoint dispatchable.
    """
    def __init__(self, root, *, policy, clock=lambda: datetime.now(timezone.utc)):
        self.root = _safe_directory(root)
        self.policy = _policy(policy)
        self._policy_sha256 = digest(self.policy)
        self.clock = clock
        self.path = self.root / 'health.sqlite3'
        with self._transaction() as db:
            db.execute('CREATE TABLE IF NOT EXISTS policy (singleton INTEGER PRIMARY KEY, payload TEXT NOT NULL)')
            encoded = canonical(self.policy).decode()
            old = db.execute('SELECT payload FROM policy WHERE singleton=1').fetchone()
            if old and old['payload'] != encoded:
                raise HealthError('Health store policy is immutable; use a separately reviewed store')
            db.execute('INSERT OR IGNORE INTO policy VALUES (1,?)', (encoded,))
            db.execute('CREATE TABLE IF NOT EXISTS snapshots (seq INTEGER PRIMARY KEY, id TEXT UNIQUE NOT NULL, payload TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS observations (configuration TEXT, provider TEXT, snapshot TEXT, '
                       'payload TEXT NOT NULL, PRIMARY KEY(configuration,provider,snapshot))')
            db.execute('CREATE TABLE IF NOT EXISTS events (seq INTEGER PRIMARY KEY, id TEXT UNIQUE NOT NULL, payload TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS circuits (configuration TEXT, provider TEXT, payload TEXT NOT NULL, '
                       'PRIMARY KEY(configuration,provider))')
            for table in ('policy', 'snapshots', 'observations', 'events'):
                for operation in ('UPDATE', 'DELETE'):
                    db.execute(f'CREATE TRIGGER IF NOT EXISTS immutable_{table}_{operation.lower()} '
                               f'BEFORE {operation} ON {table} BEGIN SELECT RAISE(ABORT,"immutable health evidence"); END')

    def _now(self):
        value = self.clock()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise ValueError('Health clock must return a timezone-aware datetime')
        return value.astimezone(timezone.utc)

    @contextmanager
    def _transaction(self):
        if digest(self.policy) != self._policy_sha256:
            raise HealthError('Health store policy cannot change during use')
        _safe_directory(self.root)
        for name in ('health.sqlite3', 'health.sqlite3-journal', 'health.sqlite3-wal',
                     'health.sqlite3-shm', '.health.lock'):
            if (self.root / name).is_symlink():
                raise HealthError('Health storage file cannot be a symlink')
        fd = os.open(self.root / '.health.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            db = None
            try:
                db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
                os.chmod(self.path, 0o600)
                db.row_factory = sqlite3.Row
                db.execute('PRAGMA synchronous=FULL')
                db.execute('BEGIN IMMEDIATE')
                yield db
                db.execute('COMMIT')
            except BaseException:
                if db is not None and db.in_transaction:
                    db.execute('ROLLBACK')
                raise
            finally:
                if db is not None:
                    db.close()
                fcntl.flock(lock, fcntl.LOCK_UN)

    def publish(self, observations):
        """Append an immutable batch; omitted identities retain their own latest batch.

        Input identity hashes are configuration hashes only. Snapshot hashes are
        never accepted from callers. Invalid typed metrics are rejected; missing
        observations are retained as unknown and fail the applicable policy.
        """
        if not isinstance(observations, list) or not observations:
            raise ValueError('Nonempty observation list required')
        rows, seen = [], set()
        for value in observations:
            if not isinstance(value, dict) or set(value) - {'configuration_sha256', 'provider', 'observed_at', 'health', 'speed'}:
                raise ValueError('Unexpected health observation fields')
            row = deepcopy(value)
            _identity(row['configuration_sha256'], row['provider'])
            key = row['configuration_sha256'], row['provider']
            if key in seen:
                raise ValueError('Duplicate observation identity')
            seen.add(key)
            timestamp(row['observed_at'])
            if row.get('health') is not None:
                # Preserve explicit unit absence; it must not become health proof.
                normalize_provider_health(row['health'])
            if row.get('speed') is not None and not isinstance(row['speed'], dict):
                raise ValueError('Speed observation must be an object or unknown')
            canonical(row)
            rows.append(row)
        rows.sort(key=lambda row: (row['configuration_sha256'], row['provider']))
        with self._transaction() as db:
            snapshot = {'schema_version': 'provider-health-v1', 'published_at': self._now().isoformat(),
                        'policy_sha256': digest(self.policy), 'observations': rows}
            snapshot_id = digest(snapshot)
            db.execute('INSERT OR IGNORE INTO snapshots(id,payload) VALUES (?,?)',
                       (snapshot_id, canonical(snapshot).decode()))
            for row in rows:
                db.execute('INSERT OR IGNORE INTO observations VALUES (?,?,?,?)',
                           (row['configuration_sha256'], row['provider'], snapshot_id, canonical(row).decode()))
        return snapshot_id

    def _latest(self, db, configuration_sha256, provider):
        result = db.execute('SELECT s.id,s.payload,o.payload AS observation FROM observations o '
                            'JOIN snapshots s ON o.snapshot=s.id WHERE o.configuration=? AND o.provider=? '
                            'ORDER BY s.seq DESC LIMIT 1', (configuration_sha256, provider)).fetchone()
        if result is None:
            return None, None, None
        snapshot, observation = json.loads(result['payload']), json.loads(result['observation'])
        if (digest(snapshot) != result['id'] or snapshot.get('schema_version') != 'provider-health-v1'
                or snapshot.get('policy_sha256') != self._policy_sha256
                or observation not in snapshot.get('observations', [])
                or observation.get('configuration_sha256') != configuration_sha256
                or observation.get('provider') != provider):
            raise HealthError('Health snapshot identity or observation changed')
        return result['id'], observation, timestamp(snapshot['published_at'])

    def _circuit(self, db, configuration_sha256, provider):
        row = db.execute('SELECT payload FROM circuits WHERE configuration=? AND provider=?',
                         (configuration_sha256, provider)).fetchone()
        return json.loads(row['payload']) if row else {'failures': 0, 'open': False,
                                                      'last_failure_at': None, 'cooldown_until': None}

    def _save_circuit(self, db, configuration_sha256, provider, circuit):
        db.execute('INSERT INTO circuits VALUES (?,?,?) ON CONFLICT(configuration,provider) '
                   'DO UPDATE SET payload=excluded.payload',
                   (configuration_sha256, provider, canonical(circuit).decode()))

    def _observation_reasons(self, observation, now, published):
        if observation is None:
            return ['health_observation_missing']
        reasons = []
        observed = timestamp(observation['observed_at'])
        # A future-dated source at capture time cannot become proof by waiting.
        if observed > now or observed > published:
            reasons.append('observation_in_future')
        elif (now - observed).total_seconds() >= self.policy['ttl_seconds']:
            reasons.append('observation_stale')
        health = observation.get('health') or {}
        status = health.get('status')
        if status is None:
            reasons.append('status_unknown')
        elif type(status) is not int or status != 0:
            reasons.append('status_degraded')
        for field, minimum in self.policy['min_uptime_percent'].items():
            if health.get('uptime_unit') != 'percent':
                reasons.append(field + '_unit_unknown_or_invalid')
            elif health.get(field) is None:
                reasons.append(field + '_unknown')
            elif number(health[field]) < minimum:
                reasons.append(field + '_below_minimum')
        speed = observation.get('speed') or {}
        for field, percentile, limit, unit_key, unit, direction, label in (
                ('latency_last_30m', 'latency_percentile', 'max_latency_ms', 'latency_unit',
                 'milliseconds', 'max', 'latency'),
                ('throughput_last_30m', 'throughput_percentile', 'min_throughput', 'throughput_unit',
                 'tokens/second', 'min', 'throughput')):
            if self.policy[limit] is None:
                continue
            if speed.get(unit_key) != unit:
                reasons.append(label + '_unit_unknown_or_invalid')
                continue
            values = speed.get(field)
            value = values.get(self.policy[percentile]) if isinstance(values, dict) else None
            if value is None:
                reasons.append(label + '_' + self.policy[percentile] + '_unknown')
                continue
            try:
                value = number(value)
                if value < 0:
                    raise ValueError('Negative speed observation')
            except ValueError:
                reasons.append(label + '_invalid')
                continue
            if (direction == 'max' and value > self.policy[limit]
                    or direction == 'min' and value < self.policy[limit]):
                reasons.append(label + '_outside_limit')
        return reasons

    def assess(self, configuration_sha256, provider):
        _identity(configuration_sha256, provider)
        with self._transaction() as db:
            now = self._now()
            snapshot_id, observation, published = self._latest(db, configuration_sha256, provider)
            reasons = self._observation_reasons(observation, now, published)
            if published is not None and published > now:
                reasons.append('snapshot_in_future')
            circuit = self._circuit(db, configuration_sha256, provider)
            if circuit['open']:
                reasons.append('cooldown_active' if now < timestamp(circuit['cooldown_until']) else 'recovery_required')
            return {'eligible': not reasons, 'reasons': reasons, 'snapshot_id': snapshot_id,
                    'observation': observation, 'circuit': circuit}

    def record(self, event_id, configuration_sha256, provider, outcome='availability_failure'):
        _identity(configuration_sha256, provider)
        if not isinstance(event_id, str) or not event_id.strip() or len(event_id) > 512:
            raise ValueError('Explicit bounded event ID required')
        if outcome not in ('availability_failure', 'success'):
            raise ValueError('Health records availability failures or success only')
        with self._transaction() as db:
            prior = db.execute('SELECT payload FROM events WHERE id=?', (event_id,)).fetchone()
            if prior:
                event = json.loads(prior['payload'])
                if (event['configuration_sha256'], event['provider'], event['outcome']) != (configuration_sha256, provider, outcome):
                    raise HealthError('Health event identity changed')
                return {**event, 'recorded': False}
            now = self._now()
            circuit = self._circuit(db, configuration_sha256, provider)
            if circuit['last_failure_at'] and now < timestamp(circuit['last_failure_at']):
                raise HealthError('Health event clock predates the last recorded failure')
            if outcome == 'availability_failure':
                circuit['failures'] += 1
                circuit['last_failure_at'] = now.isoformat()
                if circuit['failures'] >= self.policy['failure_threshold']:
                    circuit['open'] = True
                    circuit['cooldown_until'] = (now + timedelta(seconds=self.policy['cooldown_seconds'])).isoformat()
            elif not circuit['open']:
                circuit['failures'] = 0
            event = {'event_id': event_id, 'configuration_sha256': configuration_sha256,
                     'provider': provider, 'outcome': outcome, 'recorded_at': now.isoformat(), 'circuit': circuit}
            db.execute('INSERT INTO events(id,payload) VALUES (?,?)', (event_id, canonical(event).decode()))
            self._save_circuit(db, configuration_sha256, provider, circuit)
            return {**event, 'recorded': True}

    def recover(self, configuration_sha256, provider, snapshot_id):
        _identity(configuration_sha256, provider)
        if not isinstance(snapshot_id, str) or not _HASH.fullmatch(snapshot_id):
            raise ValueError('Computed snapshot identity required')
        with self._transaction() as db:
            now = self._now()
            current, observation, published = self._latest(db, configuration_sha256, provider)
            if current != snapshot_id:
                raise HealthError('Recovery requires the current matching health snapshot')
            circuit = self._circuit(db, configuration_sha256, provider)
            if not circuit['open']:
                return {'recovered': False, 'snapshot_id': snapshot_id, 'circuit': circuit}
            if now < timestamp(circuit['cooldown_until']):
                raise HealthError('Health circuit cooldown has not elapsed')
            reasons = self._observation_reasons(observation, now, published)
            if published > now:
                reasons.append('snapshot_in_future')
            if reasons:
                raise HealthError('Recovery observation is not eligible: ' + ','.join(reasons))
            if timestamp(observation['observed_at']) <= timestamp(circuit['last_failure_at']):
                raise HealthError('Recovery observation must be newer than the last failure')
            before = deepcopy(circuit)
            circuit.update(failures=0, open=False, cooldown_until=None)
            event = {'configuration_sha256': configuration_sha256, 'provider': provider,
                     'outcome': 'controlled_recovery', 'snapshot_id': snapshot_id,
                     'recorded_at': now.isoformat(), 'before': before, 'circuit': circuit}
            db.execute('INSERT INTO events(id,payload) VALUES (?,?)',
                       ('recovery:' + digest(event), canonical(event).decode()))
            self._save_circuit(db, configuration_sha256, provider, circuit)
            return {'recovered': True, 'snapshot_id': snapshot_id, 'circuit': circuit}

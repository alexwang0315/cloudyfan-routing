"""Saved-response capacity pauses, separate from health and fee authority.

The injected protocol performs no network work. Checks and snapshots are pure;
only a durable original response or explicit controlled import can add evidence.
"""
import hashlib
import json
from email.utils import format_datetime, parsedate_to_datetime
import os
from pathlib import Path
import re
import stat

from .capacity import CapacityStore, _identity, _time

PROTOCOL = 'capacity-windows-v1'
HEADER_PROTOCOL = 'capacity-window-headers-v1'
HEADERS = frozenset(('retry-after', 'ratelimit-reset', 'x-ratelimit-reset',
                     'x-ratelimit-reset-requests', 'x-ratelimit-reset-tokens', 'date'))


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _hash(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _decision(status='open', *, not_before=None, refs=(), revision=0, reason=None):
    return {'status': status, 'not_before': not_before, 'evidence_refs': list(refs),
            'revision': revision, 'reason_code': reason or 'capacity_window_' + status}


def _read(path):
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError('Original window receipt unavailable')
    info = path.stat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) & 0o077 or info.st_size > 4_194_304):
        raise ValueError('Original private bounded window receipt required')
    data = path.read_bytes()
    return data, json.loads(data)


def _http_date(value):
    try:
        parsed = parsedate_to_datetime(value)
        if format_datetime(parsed, usegmt=True) != value:
            raise ValueError('Noncanonical HTTP date')
        return _time(parsed.timestamp())
    except (ValueError, TypeError, OverflowError, AttributeError):
        raise ValueError('capacity_window_clock_reference_unknown') from None


class CapacityWindows:
    protocol = PROTOCOL

    def __init__(self, store, *, version, rules, not_applicable=(), max_delay_seconds=86400,
                 controlled_sources=()):
        if type(store) is not CapacityStore:
            raise ValueError('Shared controlled capacity store required')
        self.store, self.version = store, _identity(version)
        if type(max_delay_seconds) is not int or not 1 <= max_delay_seconds <= 86400:
            raise ValueError('Bounded window duration required')
        self.max_delay_seconds = max_delay_seconds
        self.configuration = store.snapshot()
        bindings = {row['id']: row for row in self.configuration['bindings']}
        self.rules = []
        if not isinstance(rules, (list, tuple)) or len(rules) > 128:
            raise ValueError('Bounded controlled window rules required')
        seen = set()
        for supplied in rules:
            row = dict(supplied)
            if (set(row) != {'binding', 'group', 'header', 'format', 'constraint'}
                    or row['binding'] not in bindings or row['group'] not in bindings[row['binding']]['groups']
                    or row['header'] not in HEADERS - {'date'}
                    or row['format'] not in {'retry_after', 'unix_seconds', 'delta_seconds'}):
                raise ValueError('Exact controlled window rule required')
            _identity(row['constraint'])
            key = (row['binding'], row['group'], row['header'], row['constraint'])
            if key in seen:
                raise ValueError('Duplicate controlled window rule')
            seen.add(key)
            self.rules.append(row)
        self.not_applicable = tuple(_identity(value) for value in not_applicable)
        if set(self.not_applicable) - set(bindings) or set(self.not_applicable) & {r['binding'] for r in self.rules}:
            raise ValueError('Explicit separate not-applicable window bindings required')
        if not isinstance(controlled_sources, (list, tuple)) or len(controlled_sources) > 128:
            raise ValueError('Bounded controlled original window sources required')
        self.controlled_sources = json.loads(_json(controlled_sources))
        source_ids = set()
        self._source_records = []
        for source in self.controlled_sources:
            if (not isinstance(source, dict) or set(source) != {'binding', 'group', 'scope', 'source_version',
                    'evidence_ref', 'status', 'observed_at', 'valid_until', 'values'}
                    or source['binding'] not in bindings
                    or source['group'] not in bindings[source['binding']]['groups']
                    or source['status'] != 'rate_limited'):
                raise ValueError('Exact controlled rate-limited source required')
            group = next(g for g in self.configuration['groups'] if g['id'] == source['group'])
            if (source['scope'] != group['scope'] or group['source']['kind'] != 'controlled_import'
                    or source['source_version'] != group['source']['version']
                    or not any(r['binding'] == source['binding'] and r['group'] == source['group'] for r in self.rules)):
                raise ValueError('Exact controlled source scope and rule required')
            _identity(source['evidence_ref'])
            _time(source['observed_at'])
            _time(source['valid_until'])
            if (not isinstance(source['values'], list) or len(source['values']) > 8
                    or any(not isinstance(p, list) or len(p) != 2 or p[0] not in HEADERS
                        or not isinstance(p[1], str) or len(p[1]) > 160 for p in source['values'])):
                raise ValueError('Bounded allowlisted controlled original headers required')
            if _hash(source) in source_ids:
                raise ValueError('Duplicate controlled original source')
            source_ids.add(_hash(source))
            for rule in self.rules:
                if rule['binding'] == source['binding'] and rule['group'] == source['group']:
                    self._source_records.append({'id': _hash({'source': source, 'version': self.version, 'rule': rule}),
                        'physical': _hash(source['scope']), 'window_version': self.version,
                        'configuration_version': self.configuration['configuration_version'],
                        'configuration_sha256': self.configuration['configuration_sha256'], 'source_id': _hash(source)})
        if len(self._source_records) > 128:
            raise ValueError('At most 128 controlled window facts per explicit import')
        definition = {'protocol': PROTOCOL, 'version': version,
            'configuration_sha256': self.configuration['configuration_sha256'],
            'rules': self.rules, 'not_applicable': self.not_applicable, 'max_delay_seconds': max_delay_seconds}
        if self.controlled_sources:
            definition['controlled_sources'] = self.controlled_sources
            definition['controlled_source_ids'] = self._source_records
        if store.integrity.status()['affected_groups'] is None:
            return
        with store.connection(write=True) as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS capacity_window_policies(version TEXT PRIMARY KEY,payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS capacity_window_observations(id TEXT PRIMARY KEY,physical TEXT,status TEXT,not_before REAL,payload TEXT);
                CREATE INDEX IF NOT EXISTS capacity_window_physical ON capacity_window_observations(physical,status,not_before);
                CREATE INDEX IF NOT EXISTS capacity_window_time ON capacity_window_observations(status,not_before,physical);
                CREATE TABLE IF NOT EXISTS capacity_window_source_obligations(
                    id TEXT PRIMARY KEY,physical TEXT NOT NULL,window_version TEXT NOT NULL,
                    configuration_version TEXT NOT NULL,status TEXT NOT NULL,payload TEXT NOT NULL);
            ''')
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT payload FROM capacity_window_policies WHERE version=?', (version,)).fetchone()
            if old and old[0] != _json(definition):
                raise ValueError('Immutable window policy cannot change')
            db.execute('INSERT OR IGNORE INTO capacity_window_policies VALUES(?,?)', (version, _json(definition)))
            for required in self._source_records:
                old = db.execute('SELECT payload FROM capacity_window_source_obligations WHERE id=?', (required['id'],)).fetchone()
                if old and old[0] != _json(required):
                    raise ValueError('Immutable controlled source obligation changed')
                db.execute('INSERT OR IGNORE INTO capacity_window_source_obligations VALUES(?,?,?,?,?,?)',
                    (required['id'], required['physical'], self.version,
                     required['configuration_version'], 'pending', _json(required)))

    def _scope(self, scope, physical_groups):
        if (scope.get('protocol') != PROTOCOL
                or scope.get('configuration_sha256') != self.configuration['configuration_sha256']
                or scope.get('configuration_version') != self.configuration['configuration_version']):
            raise ValueError('Exact frozen window scope required')
        binding = next((b for b in self.configuration['bindings'] if b['id'] == scope.get('binding', {}).get('id')), None)
        if binding is None or binding != scope['binding']:
            raise ValueError('Exact frozen window binding required')
        expected = []
        for group_id in binding['groups']:
            group = next(g for g in self.configuration['groups'] if g['id'] == group_id)
            expected.append({'group': group_id, 'scope': group['scope'], 'physical': _hash(group['scope'])})
        if (not isinstance(physical_groups, list)
                or sorted(expected, key=lambda g: g['group']) != sorted(physical_groups, key=lambda g: g['group'])):
            raise ValueError('Exact physical window scopes required')
        return [r for r in self.rules if r['binding'] == binding['id']]

    def _until(self, value, grammar, observed, headers):
        if not isinstance(value, str):
            raise ValueError('capacity_window_header_missing')
        if 'date' in headers and abs(_http_date(headers['date']) - observed) > 1:
            raise ValueError('capacity_window_clock_reference_unknown')
        digits = re.fullmatch(r'[0-9]{1,13}', value)
        if grammar == 'unix_seconds' or grammar == 'retry_after' and not digits:
            if 'date' not in headers:
                raise ValueError('capacity_window_clock_reference_unknown')
            if grammar == 'unix_seconds' and not digits:
                raise ValueError('capacity_window_time_invalid')
            until = _time(int(value)) if grammar == 'unix_seconds' else _http_date(value)
        elif grammar in {'retry_after', 'delta_seconds'} and digits:
            until = observed + int(value)
        else:
            raise ValueError('capacity_window_time_invalid')
        if not observed <= until <= observed + self.max_delay_seconds:
            raise ValueError('capacity_window_time_invalid')
        return _time(until)

    def _source_status(self, db, required):
        saved = db.execute('SELECT physical,window_version,configuration_version,status,payload '
            'FROM capacity_window_source_obligations WHERE id=?', (required['id'],)).fetchone()
        if (saved is None or saved[:3] != (required['physical'], required['window_version'], required['configuration_version'])
                or saved[3] not in {'pending', 'imported'} or saved[4] != _json(required)):
            return 'unknown'
        observation = db.execute('SELECT payload FROM capacity_window_observations WHERE id=?', (required['id'],)).fetchone()
        if saved[3] == 'pending':
            return 'pending' if observation is None else 'unknown'
        if observation is None:
            return 'unknown'
        row = json.loads(observation[0])
        expected = {key: required[key] for key in ('id', 'physical', 'configuration_version', 'configuration_sha256', 'source_id')}
        expected.update(version=required['window_version'], source_kind='controlled_import')
        return 'imported' if all(row.get(key) == value for key, value in expected.items()) else 'unknown'

    def check(self, db, scope, physical_groups, now):
        self.store.integrity.require(groups=[g['physical'] for g in physical_groups])
        try:
            _time(now)
            rules = self._scope(scope, physical_groups)
        except (ValueError, TypeError, AttributeError):
            return _decision('unknown', reason='capacity_window_scope_or_clock_invalid')
        selected = {g['physical'] for g in physical_groups}
        revision = db.execute('SELECT COUNT(*) FROM capacity_window_observations').fetchone()[0]
        for required in self._source_records:
            if required['physical'] in selected:
                status = self._source_status(db, required)
                if status != 'imported':
                    return _decision('unknown', revision=revision, reason='capacity_window_source_not_imported'
                        if status == 'pending' else 'capacity_window_source_state_unavailable')
        rows = [json.loads(row[0]) for row in db.execute(
            'SELECT payload FROM capacity_window_observations WHERE physical IN ('
            + ','.join('?' for _ in selected) + ") AND (status='unknown' OR not_before>?) "
            "ORDER BY (status='unknown') DESC,not_before DESC,rowid DESC LIMIT 32", (*sorted(selected), now))]
        if rows:
            unknown = [r for r in rows if r['status'] == 'unknown']
            return _decision('unknown' if unknown else 'wait',
                not_before=None if unknown else max(r['not_before'] for r in rows),
                refs=[r['id'] for r in rows], revision=revision,
                reason=unknown[0]['reason_code'] if unknown else 'capacity_window_wait')
        if not rules and scope['binding']['id'] not in self.not_applicable:
            return _decision('unknown', revision=revision, reason='capacity_window_scope_unsupported')
        return _decision(revision=revision)

    def record_response(self, receipt_ref, scope):
        rules = self._scope(scope, scope['physical_groups'])
        root, cid = Path(receipt_ref['root']), receipt_ref['call_id']
        if not root.is_absolute() or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}', cid):
            raise ValueError('Controlled original receipt reference required')
        _, intent = _read(root / 'calls' / cid / 'intent.json')
        encoded, response = _read(root / 'calls' / cid / 'response.json')
        with self.store.connection() as db:
            policy = json.loads(db.execute('SELECT payload FROM capacity_policies WHERE version=?',
                (scope['policy_version'],)).fetchone()[0])
        configuration = intent.get('configuration')
        tool = isinstance(configuration, dict) and configuration.get('protocol') == 'capacity-tool-http-v1'
        frozen_transport = _hash({'root': str(root.absolute()), 'configuration': configuration})
        routes = [route for route in policy['routes'] if route['endpoint'] == scope['endpoint']
            and route['model'] == scope['model'] and route['binding'] == scope['binding']['id']
            and route['transport_sha256'] == frozen_transport]
        if (len(routes) != 1
                or policy.get('windows_version') != self.version):
            raise ValueError('Original window transport differs from registered route')
        if tool:
            linked = (scope['model'].startswith('tool:') and configuration.get('endpoint') == scope['endpoint']
                and intent.get('wire_sha256') == scope['wire_sha256'])
        else:
            linked = (intent.get('url') == scope['endpoint'] and intent.get('payload', {}).get('model') == scope['model']
                and intent.get('dispatch', {}).get('wire_sha256') == scope['wire_sha256']
                and isinstance(intent.get('request_sha256'), str)
                and re.fullmatch(r'[a-f0-9]{64}', intent['request_sha256'])
                and response.get('request_sha256') == intent['request_sha256'])
        if (hashlib.sha256(encoded).hexdigest() != receipt_ref['response_sha256']
                or response.get('call_id') != cid or intent.get('call_id') != cid
                or not linked):
            raise ValueError('Original window receipt differs from frozen scope')
        if response.get('http_status') != 429 and not (
                type(response.get('http_status')) is int and 500 <= response['http_status'] <= 599):
            return
        envelope = response.get('window_headers', {})
        header_map = {}
        valid = (isinstance(envelope, dict) and envelope.get('protocol') == HEADER_PROTOCOL
                 and envelope.get('invalid') is False and isinstance(envelope.get('values'), list)
                 and len(envelope['values']) <= 8)
        if valid:
            for pair in envelope['values']:
                if not isinstance(pair, list) or len(pair) != 2:
                    valid = False
                    break
                name, value = pair
                if (name not in HEADERS or name in header_map or not isinstance(value, str)
                        or len(value) > 160 or any(ord(c) < 32 or ord(c) == 127 for c in value)):
                    valid = False
                    break
                header_map[name] = value
            valid = valid and sum(len(name.encode()) + len(value.encode()) for name, value in header_map.items()) <= 1024
        try:
            observed = _time(envelope.get('observed_at'))
            if observed != scope['observed_at'] or observed > _time(self.store.clock()):
                raise ValueError('Invalid response observation time')
        except (ValueError, TypeError):
            observed, valid = None, False
        observations = []
        for rule in rules:
            value = header_map.get(rule['header'])
            reason, until = 'capacity_window_header_invalid', None
            if valid:
                try:
                    until = self._until(value, rule['format'], observed, header_map)
                    reason = 'capacity_window_wait'
                except ValueError as error:
                    reason = str(error) if re.fullmatch(r'capacity_window_[a-z_]+', str(error)) else 'capacity_window_time_invalid'
            good = until is not None
            group = next(g for g in scope['physical_groups'] if g['group'] == rule['group'])
            identifier = _hash({'receipt': receipt_ref['response_sha256'], 'version': self.version,
                'physical': group['physical'], 'constraint': rule['constraint'], 'header': rule['header']})
            observations.append({'id': identifier, 'physical': group['physical'], 'group': group['group'],
                'scope': group['scope'], 'status': 'wait' if good else 'unknown', 'not_before': until,
                'observed_at': observed, 'source_kind': 'original_response', 'header': rule['header'],
                'constraint': rule['constraint'], 'version': self.version,
                'policy_version': scope['policy_version'], 'configuration_version': scope['configuration_version'],
                'reason_code': reason, 'reported_not_before': until})
        by_constraint = {}
        for row in observations:
            by_constraint.setdefault((row['physical'], row['constraint']), []).append(row)
        for rows in by_constraint.values():
            known = [r['not_before'] for r in rows if r['status'] == 'wait']
            if known and max(known) - min(known) > 1:
                for row in rows:
                    row.update(status='unknown', not_before=None,
                               reason_code='capacity_window_source_contradictory')
        with self.store.connection(write=True) as db:
            db.execute('BEGIN IMMEDIATE')
            for row in observations:
                old = db.execute('SELECT payload FROM capacity_window_observations WHERE id=?', (row['id'],)).fetchone()
                if old and old[0] != _json(row):
                    raise ValueError('Original immutable window observation changed')
                db.execute('INSERT OR IGNORE INTO capacity_window_observations VALUES(?,?,?,?,?)',
                    (row['id'], row['physical'], row['status'], row['not_before'], _json(row)))

    def import_sources(self):
        """Explicitly import immutable host-supplied rate facts, never USD quotas.

        HTTP callers cannot supply these values. A repeated import is a read of
        the same original fact, not a refresh of its observation or expiry time.
        """
        observations = []
        with self.store.connection(write=True) as db:
            db.execute('BEGIN IMMEDIATE')
            active = db.execute('SELECT version FROM capacity_control WHERE id=1').fetchone()
            if active is None or active[0] != self.configuration['configuration_version']:
                raise ValueError('Controlled window source configuration changed')
            now = _time(self.store.clock())
            for source in self.controlled_sources:
                headers = dict(source['values'])
                valid = (len(headers) == len(source['values'])
                    and sum(len(k.encode()) + len(v.encode()) for k, v in source['values']) <= 1024
                    and not any(any(ord(c) < 32 or ord(c) == 127 for c in v) for v in headers.values()))
                for rule in self.rules:
                    if rule['binding'] != source['binding'] or rule['group'] != source['group']:
                        continue
                    identifier = _hash({'source': source, 'version': self.version, 'rule': rule})
                    if db.execute('SELECT 1 FROM capacity_window_observations WHERE id=?', (identifier,)).fetchone():
                        continue
                    until, reason = None, 'capacity_window_header_invalid'
                    if valid:
                        try:
                            if not source['observed_at'] <= now < source['valid_until']:
                                raise ValueError('capacity_window_source_stale_or_future')
                            until = self._until(headers.get(rule['header']), rule['format'], source['observed_at'], headers)
                            if until > source['valid_until']:
                                raise ValueError('capacity_window_source_stale_or_future')
                            reason = 'capacity_window_wait'
                        except ValueError as error:
                            until = None
                            reason = str(error) if re.fullmatch(r'capacity_window_[a-z_]+', str(error)) else 'capacity_window_time_invalid'
                    observations.append({'id': identifier, 'physical': _hash(source['scope']), 'group': source['group'],
                        'scope': source['scope'], 'status': 'wait' if until is not None else 'unknown',
                        'not_before': until, 'observed_at': source['observed_at'], 'source_kind': 'controlled_import',
                        'source_version': source['source_version'], 'evidence_ref': source['evidence_ref'],
                        'source_id': _hash(source), 'configuration_sha256': self.configuration['configuration_sha256'],
                        'header': rule['header'], 'constraint': rule['constraint'], 'version': self.version,
                        'policy_version': None, 'configuration_version': self.configuration['configuration_version'],
                        'reason_code': reason, 'reported_not_before': until})
            grouped = {}
            for row in observations:
                grouped.setdefault((row['physical'], row['constraint'], row['observed_at']), []).append(row)
            for rows in grouped.values():
                known = [r['not_before'] for r in rows if r['status'] == 'wait']
                if known and max(known) - min(known) > 1:
                    for row in rows:
                        row.update(status='unknown', not_before=None, reason_code='capacity_window_source_contradictory')
            for row in observations:
                db.execute('INSERT INTO capacity_window_observations VALUES(?,?,?,?,?)',
                    (row['id'], row['physical'], row['status'], row['not_before'], _json(row)))
                db.execute("UPDATE capacity_window_source_obligations SET status='imported' WHERE id=?", (row['id'],))
        return {'status': 'unknown' if any(r['status'] == 'unknown' for r in observations) else 'available',
                'imported_count': len(observations), 'network_requests': 0}

    def snapshot(self):
        now = _time(self.store.clock())
        with self.store.connection(projection=True) as db:
            count = db.execute('SELECT COUNT(*) FROM capacity_window_observations').fetchone()[0]
            events = [json.loads(row[0]) for row in db.execute(
                'SELECT payload FROM capacity_window_observations ORDER BY rowid DESC LIMIT 100')]
            group_count = db.execute("SELECT COUNT(DISTINCT physical) FROM capacity_window_observations "
                "WHERE status='unknown' OR not_before>?", (now,)).fetchone()[0]
            groups = [json.loads(row[0]) for row in db.execute(
                'SELECT payload FROM (SELECT payload,physical,ROW_NUMBER() OVER (PARTITION BY physical '
                "ORDER BY (status='unknown') DESC,not_before DESC,rowid DESC) AS position "
                "FROM capacity_window_observations WHERE status='unknown' OR not_before>?) "
                'WHERE position=1 ORDER BY physical LIMIT 128', (now,))]
            pending = []
            for required in self._source_records:
                status = self._source_status(db, required)
                if status != 'imported':
                    pending.append({**required, 'status': status, 'source_kind': 'controlled_import',
                        'reason_code': 'capacity_window_source_not_imported' if status == 'pending'
                            else 'capacity_window_source_state_unavailable'})
        return {'schema_version': PROTOCOL, 'version': self.version, 'revision': count,
                'groups': groups, 'active_group_count': group_count,
                'active_group_history_complete': group_count == len(groups),
                'pending_sources': pending, 'pending_source_count': len(pending),
                'events': events, 'event_count': count, 'event_history_complete': count == len(events)}

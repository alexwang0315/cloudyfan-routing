"""Saved-response capacity pauses, separate from health and fee authority.

The injected protocol performs no network work. Checks and snapshots are pure;
only a durable original response or explicit controlled import can add evidence.
"""
import hashlib
import json
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


class CapacityWindows:
    protocol = PROTOCOL

    def __init__(self, store, *, version, rules, not_applicable=(), max_delay_seconds=86400):
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
        for supplied in rules:
            row = dict(supplied)
            if (set(row) != {'binding', 'group', 'header', 'format', 'constraint'}
                    or row['binding'] not in bindings or row['group'] not in bindings[row['binding']]['groups']
                    or row['header'] not in HEADERS - {'date'}
                    or row['format'] not in {'retry_after', 'unix_seconds', 'delta_seconds'}):
                raise ValueError('Exact controlled window rule required')
            _identity(row['constraint'])
            if row in self.rules:
                raise ValueError('Duplicate controlled window rule')
            self.rules.append(row)
        self.not_applicable = tuple(_identity(value) for value in not_applicable)
        if set(self.not_applicable) - set(bindings) or set(self.not_applicable) & {r['binding'] for r in self.rules}:
            raise ValueError('Explicit separate not-applicable window bindings required')
        definition = {'protocol': PROTOCOL, 'version': version,
            'configuration_sha256': self.configuration['configuration_sha256'],
            'rules': self.rules, 'not_applicable': self.not_applicable, 'max_delay_seconds': max_delay_seconds}
        with store.connection(write=True) as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS capacity_window_policies(version TEXT PRIMARY KEY,payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS capacity_window_observations(id TEXT PRIMARY KEY,physical TEXT,status TEXT,not_before REAL,payload TEXT);
                CREATE INDEX IF NOT EXISTS capacity_window_physical ON capacity_window_observations(physical,status,not_before);
            ''')
            old = db.execute('SELECT payload FROM capacity_window_policies WHERE version=?', (version,)).fetchone()
            if old and old[0] != _json(definition):
                raise ValueError('Immutable window policy cannot change')
            db.execute('INSERT OR IGNORE INTO capacity_window_policies VALUES(?,?)', (version, _json(definition)))

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

    def check(self, db, scope, physical_groups, now):
        try:
            _time(now)
            rules = self._scope(scope, physical_groups)
        except (ValueError, TypeError, AttributeError):
            return _decision('unknown', reason='capacity_window_scope_or_clock_invalid')
        selected = {g['physical'] for g in physical_groups}
        revision = db.execute('SELECT COUNT(*) FROM capacity_window_observations').fetchone()[0]
        rows = [json.loads(row[0]) for row in db.execute(
            "SELECT payload FROM capacity_window_observations WHERE status='unknown' OR not_before>?", (now,))
            if json.loads(row[0])['physical'] in selected]
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
        if (hashlib.sha256(encoded).hexdigest() != receipt_ref['response_sha256']
                or response.get('call_id') != cid or intent.get('call_id') != cid
                or intent.get('url') != scope['endpoint'] or intent.get('payload', {}).get('model') != scope['model']
                or intent.get('dispatch', {}).get('wire_sha256') != scope['wire_sha256']
                or response.get('request_sha256') != intent.get('request_sha256')):
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
            for name, value in envelope['values']:
                if (name not in HEADERS or name in header_map or not isinstance(value, str)
                        or len(value) > 160 or any(ord(c) < 32 or ord(c) == 127 for c in value)):
                    valid = False
                    break
                header_map[name] = value
        try:
            observed = _time(envelope.get('observed_at'))
            if observed != scope['observed_at'] or observed > _time(self.store.clock()):
                raise ValueError('Invalid response observation time')
        except (ValueError, TypeError):
            observed, valid = None, False
        observations = []
        for rule in rules:
            value = header_map.get(rule['header'])
            good = valid and isinstance(value, str) and re.fullmatch(r'[0-9]{1,6}', value)
            good = bool(good and rule['format'] in {'retry_after', 'delta_seconds'}
                        and int(value) <= self.max_delay_seconds)
            until = observed + int(value) if good else None
            group = next(g for g in scope['physical_groups'] if g['group'] == rule['group'])
            identifier = _hash({'receipt': receipt_ref['response_sha256'], 'version': self.version,
                'physical': group['physical'], 'constraint': rule['constraint'], 'header': rule['header']})
            observations.append({'id': identifier, 'physical': group['physical'], 'group': group['group'],
                'scope': group['scope'], 'status': 'wait' if good else 'unknown', 'not_before': until,
                'observed_at': observed, 'source_kind': 'original_response', 'header': rule['header'],
                'constraint': rule['constraint'], 'version': self.version,
                'policy_version': scope['policy_version'], 'configuration_version': scope['configuration_version'],
                'reason_code': 'capacity_window_wait' if good else 'capacity_window_header_invalid'})
        with self.store.connection(write=True) as db:
            db.execute('BEGIN IMMEDIATE')
            for row in observations:
                old = db.execute('SELECT payload FROM capacity_window_observations WHERE id=?', (row['id'],)).fetchone()
                if old and old[0] != _json(row):
                    raise ValueError('Original immutable window observation changed')
                db.execute('INSERT OR IGNORE INTO capacity_window_observations VALUES(?,?,?,?,?)',
                    (row['id'], row['physical'], row['status'], row['not_before'], _json(row)))

    def snapshot(self):
        now = _time(self.store.clock())
        with self.store.connection() as db:
            count = db.execute('SELECT COUNT(*) FROM capacity_window_observations').fetchone()[0]
            events = [json.loads(row[0]) for row in db.execute(
                'SELECT payload FROM capacity_window_observations ORDER BY rowid DESC LIMIT 100')]
            active = [json.loads(row[0]) for row in db.execute(
                "SELECT payload FROM capacity_window_observations WHERE status='unknown' OR not_before>?", (now,))]
        groups = {}
        for row in active:
            old = groups.get(row['physical'])
            if old is None or row['status'] == 'unknown' or old['status'] != 'unknown' and row['not_before'] > old['not_before']:
                groups[row['physical']] = row
        return {'schema_version': PROTOCOL, 'version': self.version, 'revision': count,
                'groups': list(groups.values()), 'events': events, 'event_count': count,
                'event_history_complete': count == len(events)}

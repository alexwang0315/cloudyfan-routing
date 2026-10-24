"""Independent, bounded capacity preservation witnesses; never financial authority.

The controlled host keeps this private companion outside the SQLite database.
Read verification never repairs either copy. A pending metadata commit remains
fail-closed after interruption; it never means that a supplier stopped or refunded.
"""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import threading
import uuid

PROTOCOL = 'capacity-preservation-v1'
MAX_BYTES = 64 * 1024 * 1024
MAX_ROWS = 50000
MAX_TRANSACTIONS = 50000
_LOCAL = threading.local()


class CapacityIntegrityError(RuntimeError):
    control_plane_error = True

    def __init__(self, code='capacity_store_integrity_unavailable', *, status=503):
        self.code, self.status = code, status
        super().__init__(code)


def _encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def _hash(value):
    return hashlib.sha256(_encoded(value)).hexdigest()


def _file(path):
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o077 or info.st_nlink != 1 or info.st_size > MAX_BYTES):
        raise CapacityIntegrityError()
    return path.read_bytes()


def _write(path, value):
    data = _encoded(value)
    if len(data) > MAX_BYTES:
        raise CapacityIntegrityError('capacity_store_witness_bound_exceeded')
    pending = path.with_name(path.name + '.writing-' + uuid.uuid4().hex)
    fd = os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    pending.replace(path)
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class CapacityIntegrity:
    """One host-bound SQLite incarnation and independent retained row witnesses.

    The bounded file hash makes ordinary reads cheap. A changed file is checked
    against independently saved row membership, including fields used by indexes.
    Only an identifiable damaged scope/task may be isolated. Lost schema, root
    identity, or commit evidence makes the whole controlled store unavailable.
    """
    def __init__(self, path, *, fresh=False, role='capacity'):
        self.path, self.role = Path(path).absolute(), role
        self.binding_error = None
        self.root = self.path.with_name(self.path.name + '.preservation')
        self.root.mkdir(mode=0o700, exist_ok=True)
        info = self.root.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise CapacityIntegrityError()
        self.lock_path, self.head = self.root / 'lock', self.root / 'head.json'
        fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        with self.lock(write=True):
            if not self.head.exists() and fresh:
                identity = {'protocol': PROTOCOL, 'incarnation': uuid.uuid4().hex, 'role': role,
                    'path_sha256': _hash(str(self.path))}
                with sqlite3.connect(self.path) as db:
                    db.execute('CREATE TABLE capacity_preservation_identity(id INTEGER PRIMARY KEY, payload TEXT NOT NULL)')
                    db.execute('INSERT INTO capacity_preservation_identity VALUES(1,?)', (_encoded(identity).decode(),))
                snapshot = self._scan()
                _write(self.root / 'identity.json', identity)
                self._seal(snapshot, revision=0)

    @contextmanager
    def lock(self, *, write=False):
        held = getattr(_LOCAL, 'capacity_locks', {})
        key = str(self.lock_path)
        if key in held:
            if write and not held[key]:
                raise CapacityIntegrityError('capacity_store_lock_upgrade_denied')
            yield
            return
        fd = os.open(self.lock_path, os.O_RDWR | os.O_NOFOLLOW)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX if write else fcntl.LOCK_SH)
            _LOCAL.capacity_locks = {**held, key: write}
            yield
        finally:
            _LOCAL.capacity_locks = held
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _state(self):
        identity = json.loads(_file(self.root / 'identity.json'))
        state = json.loads(_file(self.head))
        body = state['body']
        if (state['sha256'] != _hash(body) or body['identity'] != identity
                or identity['protocol'] != PROTOCOL or identity['role'] != self.role
                or identity['path_sha256'] != _hash(str(self.path))):
            raise CapacityIntegrityError()
        return body

    def _scan(self, db=None):
        own = db is None
        if own:
            _file(self.path)
            db = sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True)
        try:
            schema = [tuple(row) for row in db.execute("SELECT type,name,tbl_name,sql FROM sqlite_master "
                "WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' ORDER BY type,name")]
            rows, total = {}, 0
            for kind, name, _, _ in schema:
                if kind != 'table':
                    continue
                if not name.replace('_', '').isalnum():
                    raise CapacityIntegrityError()
                columns = [r[1] for r in db.execute('PRAGMA table_info("' + name + '")')]
                primary = [r[1] for r in db.execute('PRAGMA table_info("' + name + '")') if r[5]]
                values = db.execute('SELECT rowid,* FROM "' + name + '" LIMIT ?', (MAX_ROWS + 1,)).fetchall()
                total += len(values)
                if total > MAX_ROWS:
                    raise CapacityIntegrityError('capacity_store_witness_bound_exceeded')
                table = {}
                for item in values:
                    value = dict(zip(columns, item[1:]))
                    key = _hash([value[p] for p in primary]) if primary else str(item[0])
                    scopes, tasks = [], []
                    try:
                        payload = json.loads(value.get('payload') or '{}')
                        if name == 'capacity_allocations':
                            scopes = [g['physical'] for g in payload['groups']]
                            tasks = [payload['run']]
                        elif name in {'capacity_window_observations', 'capacity_window_source_obligations'}:
                            scopes = [payload['physical']]
                        elif name == 'capacity_source_refreshes':
                            configuration = json.loads(db.execute('SELECT payload FROM capacity_configurations WHERE version=?',
                                (value['version'],)).fetchone()[0])
                            scopes = [_hash(group['scope']) for group in configuration['groups']
                                if group['id'] == value['group_id']]
                        elif name == 'capacity_adoptions':
                            tasks = [value['run']]
                        elif name == 'capacity_terminal_fences':
                            tasks = [value['run']]
                        elif name == 'jobs':
                            scopes = json.loads(value.get('detail') or '{}').get('groups', [])
                            tasks = [value['id']]
                    except (ValueError, KeyError, TypeError):
                        pass
                    # Queue request text stays only in its original access-bound
                    # database. The witness keeps a digest and prompt-free view.
                    view = {k: v for k, v in value.items() if not (self.role == 'queue' and k == 'request')}
                    table[key] = {'digest': _hash(value), 'scopes': scopes, 'tasks': tasks,
                        'rowid': item[0], 'view': view}
                rows[name] = table
            return {'schema': schema, 'rows': rows}
        finally:
            if own:
                db.close()

    def _seal(self, snapshot, *, revision, expected=None):
        identity = json.loads(_file(self.root / 'identity.json')) if (self.root / 'identity.json').exists() else None
        if identity is None:
            with sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True) as db:
                identity = json.loads(db.execute('SELECT payload FROM capacity_preservation_identity WHERE id=1').fetchone()[0])
        if expected:
            for table, keys in expected.items():
                snapshot['rows'].setdefault(table, {}).update(keys)
        body = {'identity': identity, 'revision': revision, **snapshot,
            'database_sha256': hashlib.sha256(_file(self.path)).hexdigest(), 'quarantined': bool(expected)}
        _write(self.head, {'body': body, 'sha256': _hash(body)})

    def _inspect(self):
        owned = getattr(_LOCAL, 'capacity_transactions', {}).get(str(self.path))
        if owned is not None:
            return owned
        if self.binding_error is not None:
            raise CapacityIntegrityError(self.binding_error)
        state = self._state()
        for record in state['rows'].get('capacity_preservation_bindings', {}).values():
            value = record['view']
            if json.loads(_file(Path(value['target']))) != json.loads(value['payload']):
                raise CapacityIntegrityError('capacity_store_binding_unavailable')
        if (self.root / 'pending.json').exists():
            raise CapacityIntegrityError('capacity_store_commit_pending')
        if hashlib.sha256(_file(self.path)).hexdigest() == state['database_sha256']:
            # Quarantined expected rows intentionally keep their original digest.
            current = self._scan() if state.get('quarantined') else None
        else:
            current = self._scan()
        if current is None:
            return state, {'status': 'verified', 'groups': [], 'tasks': [], 'bad': {}}
        if _hash(current['schema']) != _hash(state['schema']):
            raise CapacityIntegrityError()
        groups, tasks, bad = set(), set(), {}
        for table in set(state['rows']) | set(current['rows']):
            expected, actual = state['rows'].get(table, {}), current['rows'].get(table, {})
            for key in set(expected) | set(actual):
                old, now = expected.get(key), actual.get(key)
                if old is not None and now is not None and old['digest'] == now['digest']:
                    continue
                if old is None or not (old['scopes'] or old['tasks']):
                    raise CapacityIntegrityError()
                bad.setdefault(table, {})[key] = old
                groups.update(old['scopes'])
                tasks.update(old['tasks'])
        return state, {'status': 'unavailable' if bad else 'verified', 'groups': sorted(groups),
            'tasks': sorted(tasks), 'bad': bad}

    def bind(self, directory, name):
        """Cross-link the original service and the store incarnation once.

        A new empty DB and companion cannot replace an established store while
        its original TaskService still retains this independent private link.
        Failure leaves the observer usable, but never establishes new authority.
        """
        target = Path(directory).absolute() / ('.capacity-preservation-' + name + '.json')
        with self.lock(write=True):
            try:
                state = self._state()
                value = {'protocol': PROTOCOL, 'identity': state['identity']}
                if target.exists():
                    if json.loads(_file(target)) != value:
                        raise CapacityIntegrityError('capacity_store_binding_unavailable')
                    return
                state, status = self._inspect()
                saved = state['rows'].get('capacity_preservation_bindings', {})
                if status['bad'] or any(row['view']['target'] == str(target) for row in saved.values()):
                    raise CapacityIntegrityError('capacity_store_binding_unavailable')
                with self.transaction(write=True) as db:
                    db.execute('CREATE TABLE IF NOT EXISTS capacity_preservation_bindings(target TEXT PRIMARY KEY,payload TEXT NOT NULL)')
                    db.execute('INSERT INTO capacity_preservation_bindings VALUES(?,?)',
                        (str(target), _encoded(value).decode()))
                    _write(target, value)
            except (OSError, ValueError, KeyError, TypeError, sqlite3.Error, CapacityIntegrityError):
                self.binding_error = 'capacity_store_binding_unavailable'

    def status(self):
        with self.lock():
            try:
                state, status = self._inspect()
                return {'protocol': PROTOCOL, 'status': status['status'],
                    'reason_code': 'capacity_store_scope_unavailable' if status['bad'] else None,
                    'affected_groups': status['groups'], 'affected_task_count': len(status['tasks']),
                    'revision': state['revision'], 'metadata_rows': sum(map(len, state['rows'].values())),
                    'bounds': {'database_bytes': MAX_BYTES, 'metadata_rows': MAX_ROWS,
                        'transaction_records': MAX_TRANSACTIONS},
                    'retained_verified_metadata': True,
                    'witness_retention_days_minimum': 365, 'automatic_repair': False}
            except (OSError, ValueError, KeyError, TypeError, sqlite3.Error, CapacityIntegrityError) as error:
                try:
                    retained = self.binding_error is None and bool(self._state())
                except (OSError, ValueError, KeyError, TypeError, CapacityIntegrityError):
                    retained = False
                return {'protocol': PROTOCOL, 'status': 'unavailable',
                    'reason_code': error.code if isinstance(error, CapacityIntegrityError) else 'capacity_store_integrity_unavailable',
                    'affected_groups': None, 'affected_task_count': None, 'revision': None,
                    'metadata_rows': None, 'retained_verified_metadata': retained,
                    'bounds': {'database_bytes': MAX_BYTES, 'metadata_rows': MAX_ROWS,
                        'transaction_records': MAX_TRANSACTIONS},
                    'witness_retention_days_minimum': 365, 'automatic_repair': False}

    def require(self, *, groups=None, task=None):
        with self.lock():
            try:
                _, status = self._inspect()
            except (OSError, ValueError, KeyError, TypeError, sqlite3.Error, CapacityIntegrityError):
                raise CapacityIntegrityError() from None
            if status['bad'] and (groups is None and task is None
                    or set(groups or ()) & set(status['groups']) or task in status['tasks']):
                raise CapacityIntegrityError('capacity_store_scope_unavailable', status=429)

    @contextmanager
    def transaction(self, *, write=False, projection=False):
        with self.lock(write=write):
            try:
                state, status = self._inspect()
            except (OSError, ValueError, KeyError, TypeError, sqlite3.Error, CapacityIntegrityError):
                if projection and not write:
                    with self.projection() as db:
                        yield db
                    return
                raise CapacityIntegrityError() from None
            if projection and status['bad'] and not write:
                with self.projection() as db:
                    yield db
                return
            token = uuid.uuid4().hex
            before = self._scan() if write else None
            if write:
                journal = self.root / 'journal'
                journal.mkdir(mode=0o700, exist_ok=True)
                with os.scandir(journal) as records:
                    if sum(1 for _, _record in zip(range(MAX_TRANSACTIONS), records)) >= MAX_TRANSACTIONS:
                        raise CapacityIntegrityError('capacity_store_witness_bound_exceeded')
                _write(self.root / 'pending.json', {'protocol': PROTOCOL, 'transaction': token,
                    'previous_revision': state['revision'], 'previous_state_sha256': _hash(state)})
            db = sqlite3.connect(self.path.as_uri() + ('?mode=rw' if write else '?mode=ro'), uri=True, timeout=5)
            previous = getattr(_LOCAL, 'capacity_transactions', {})
            if write:
                _LOCAL.capacity_transactions = {**previous, str(self.path): (state, status)}
            try:
                yield db
                if write:
                    after = self._scan(db)
                    # An unrelated transaction cannot certify a damaged old row.
                    for table, keys in status['bad'].items():
                        for key in keys:
                            old_actual = before['rows'].get(table, {}).get(key)
                            if after['rows'].get(table, {}).get(key) != old_actual:
                                raise CapacityIntegrityError('capacity_store_scope_unavailable')
                    db.commit()
                    self._seal(after, revision=state['revision'] + 1, expected=status['bad'])
            except BaseException:
                db.rollback()
                if write:
                    # Only verified rollback may close the local pending marker.
                    actual = self._scan()
                    if _hash(actual) == _hash(before):
                        self._archive_pending(token, 'aborted')
                raise
            else:
                if write:
                    self._archive_pending(token, 'committed')
            finally:
                _LOCAL.capacity_transactions = previous
                db.close()

    def _archive_pending(self, token, outcome):
        journal = self.root / 'journal'
        journal.mkdir(mode=0o700, exist_ok=True)
        (self.root / 'pending.json').replace(journal / (token + '-' + outcome + '.json'))

    @contextmanager
    def selection(self, current):
        """Exclude tampered selectors from unrelated capacity calculations.

        Global loss is refused by require() before any authorization. With an
        identifiable damaged row its original scope remains blocked, while this
        independently verified metadata prevents that row poisoning another
        scope. This view never certifies or writes a damaged original row.
        """
        _, status = self._inspect()
        if not status['bad']:
            yield current
        else:
            with self.projection(current=current) as db:
                yield db

    @contextmanager
    def projection(self, *, current=None):
        """Last independently verified metadata only, never used to grant a POST."""
        try:
            state = self._state()
        except (OSError, ValueError, KeyError, TypeError):
            raise CapacityIntegrityError() from None
        db = sqlite3.connect(':memory:')
        original = None
        if self.role == 'queue' and current is None:
            try:
                original = sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True)
                current = original
            except sqlite3.Error:
                pass
        try:
            for kind, name, _, sql in sorted(state['schema'], key=lambda r: r[0] != 'table'):
                if kind != 'table' or name not in state['rows']:
                    continue
                db.execute(sql)
                for record in state['rows'][name].values():
                    view = dict(record['view'])
                    if self.role == 'queue' and name == 'jobs' and current is not None:
                        try:
                            columns = [r[1] for r in current.execute('PRAGMA table_info(jobs)')]
                            actual = current.execute('SELECT * FROM jobs WHERE id=?', (view['id'],)).fetchone()
                            if actual is not None:
                                value = dict(zip(columns, actual))
                                if _hash(value) == record['digest']:
                                    view['request'] = value['request']
                        except sqlite3.Error:
                            pass
                    columns = list(view)
                    db.execute('INSERT INTO "' + name + '" (' + ','.join('"' + k + '"' for k in columns)
                        + ') VALUES (' + ','.join('?' for _ in columns) + ')', tuple(view.values()))
            yield db
        finally:
            db.close()
            if original is not None:
                original.close()

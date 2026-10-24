"""Operator-owned opaque API credentials; no public enrollment or token logging."""
from contextlib import contextmanager
from dataclasses import dataclass, field
import hashlib
import hmac
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import threading
import time


SCOPES = frozenset({'tasks:read', 'tasks:write', 'models:read'})
_PRINCIPAL = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}')
_TOKEN = re.compile(r'cf1_([0-9a-f]{32})\.([A-Za-z0-9_-]{43})')


class IdentityError(ValueError):
    """Safe operator error; messages never include a credential or caller input."""


@dataclass(frozen=True)
class IssuedCredential:
    id: str
    principal: str
    scopes: tuple[str, ...]
    expires_at: float
    token: str = field(repr=False)


def _private_path(path, *, create=False):
    path = Path(path).absolute()
    if '..' in path.parts:
        raise IdentityError('Unsafe identity path')
    for parent in reversed(path.parents):
        if not parent.exists() and not parent.is_symlink() and create:
            parent.mkdir(mode=0o700)
        try:
            info = parent.lstat()
        except OSError:
            raise IdentityError('Identity directory unavailable') from None
        if not stat.S_ISDIR(info.st_mode):
            raise IdentityError('Unsafe identity directory')
    parent = path.parent.stat()
    if parent.st_uid != os.getuid() or parent.st_mode & 0o077:
        raise IdentityError('Identity directory must be private and owned')
    if create:
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(fd)
    try:
        info = path.lstat()
    except OSError:
        raise IdentityError('Identity database unavailable') from None
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o077 or info.st_nlink != 1):
        raise IdentityError('Identity database must be private, regular and owned')
    for suffix in ('-journal', '-wal', '-shm'):
        sidecar = path.with_name(path.name + suffix)
        try:
            info = sidecar.lstat()
        except FileNotFoundError:
            continue
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_nlink != 1):
            raise IdentityError('Unsafe identity database companion')
    return path


def _principal(value):
    if not isinstance(value, str) or not _PRINCIPAL.fullmatch(value):
        raise IdentityError('Invalid principal identifier')
    return value


def _number(value):
    try:
        valid = type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        valid = False
    if not valid:
        raise IdentityError('Finite numeric time required')
    return float(value)


def _scopes(values):
    if (not isinstance(values, (set, frozenset, tuple, list)) or not values
            or any(not isinstance(value, str) or value not in SCOPES for value in values)):
        raise IdentityError('Explicit supported scopes required')
    return tuple(sorted(set(values)))


class IdentityStore:
    """Private SQLite registry. Keep pepper separately in operator secret storage."""
    def __init__(self, path, *, pepper, clock=time.time):
        if not isinstance(pepper, bytes) or len(pepper) < 32 or not callable(clock):
            raise IdentityError('Private pepper of at least 32 bytes and clock required')
        self._thread_lock = threading.RLock()
        self.path = _private_path(path, create=True)
        self._pepper, self.clock = pepper, clock
        with self._transaction() as db:
            db.execute('CREATE TABLE IF NOT EXISTS metadata (name TEXT PRIMARY KEY, value TEXT NOT NULL)')
            verifier = self._digest('cloudyfan-identity-pepper-v1')
            row = db.execute('SELECT value FROM metadata WHERE name=?', ('pepper',)).fetchone()
            if row is not None and not hmac.compare_digest(row['value'], verifier):
                raise IdentityError('Identity secret does not match registry')
            db.execute('INSERT OR IGNORE INTO metadata VALUES (?,?)', ('pepper', verifier))
            db.execute('CREATE TABLE IF NOT EXISTS principals (id TEXT PRIMARY KEY, active INTEGER NOT NULL, '
                       'created REAL NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS credentials (id TEXT PRIMARY KEY, principal TEXT NOT NULL '
                       'REFERENCES principals(id), digest TEXT NOT NULL UNIQUE, scopes TEXT NOT NULL, '
                       'created REAL NOT NULL, expires REAL NOT NULL, revoked REAL)')

    def _digest(self, token):
        return hmac.new(self._pepper, token.encode('ascii'), hashlib.sha256).hexdigest()

    def _now(self, db):
        now = _number(self.clock())
        floor = db.execute('SELECT value FROM metadata WHERE name=?', ('clock_floor',)).fetchone()
        if floor is not None and now < _number(float(floor['value'])):
            raise IdentityError('Identity clock moved backwards')
        db.execute('INSERT INTO metadata VALUES (?,?) ON CONFLICT(name) DO UPDATE SET value=excluded.value',
                   ('clock_floor', str(now)))
        return now

    @contextmanager
    def _transaction(self):
        with self._thread_lock:
            _private_path(self.path)
            db = sqlite3.connect(self.path.as_uri() + '?mode=rw', uri=True, isolation_level=None, timeout=5)
            db.row_factory = sqlite3.Row
            try:
                db.execute('PRAGMA foreign_keys=ON')
                db.execute('PRAGMA journal_mode=DELETE')
                db.execute('BEGIN IMMEDIATE')
                yield db
                db.execute('COMMIT')
            except BaseException:
                if db.in_transaction:
                    db.execute('ROLLBACK')
                raise
            finally:
                db.close()

    def create_principal(self, principal):
        principal = _principal(principal)
        with self._transaction() as db:
            try:
                db.execute('INSERT INTO principals VALUES (?,?,?)', (principal, 1, self._now(db)))
            except sqlite3.IntegrityError:
                raise IdentityError('Principal already registered') from None
        return self.principal(principal)

    def principal(self, principal):
        with self._transaction() as db:
            row = db.execute('SELECT id,active,created FROM principals WHERE id=?', (_principal(principal),)).fetchone()
            if row is None:
                raise IdentityError('Principal not registered')
            return {'id': row['id'], 'active': bool(row['active']), 'created_at': row['created']}

    def set_active(self, principal, active):
        if type(active) is not bool:
            raise IdentityError('Explicit principal activity required')
        with self._transaction() as db:
            updated = db.execute('UPDATE principals SET active=? WHERE id=?', (int(active), _principal(principal)))
            if updated.rowcount != 1:
                raise IdentityError('Principal not registered')

    def is_active(self, principal):
        if not isinstance(principal, str) or not _PRINCIPAL.fullmatch(principal):
            return False
        with self._transaction() as db:
            self._now(db)
            row = db.execute('SELECT active FROM principals WHERE id=?', (principal,)).fetchone()
            return bool(row and row['active'])

    def _issue(self, db, principal, scopes, expires_at, now):
        row = db.execute('SELECT active FROM principals WHERE id=?', (principal,)).fetchone()
        if row is None or not row['active']:
            raise IdentityError('Active registered principal required')
        if expires_at <= now:
            raise IdentityError('Credential expiry must be in the future')
        identifier = secrets.token_hex(16)
        token = 'cf1_' + identifier + '.' + secrets.token_urlsafe(32)
        db.execute('INSERT INTO credentials VALUES (?,?,?,?,?,?,NULL)',
                   (identifier, principal, self._digest(token), json.dumps(scopes), now, expires_at))
        return IssuedCredential(identifier, principal, scopes, expires_at, token)

    def issue(self, principal, *, scopes, expires_at):
        principal, scopes, expires_at = _principal(principal), _scopes(scopes), _number(expires_at)
        with self._transaction() as db:
            return self._issue(db, principal, scopes, expires_at, self._now(db))

    def revoke(self, credential_id):
        if not isinstance(credential_id, str) or not re.fullmatch(r'[0-9a-f]{32}', credential_id):
            raise IdentityError('Invalid credential identifier')
        with self._transaction() as db:
            updated = db.execute('UPDATE credentials SET revoked=COALESCE(revoked,?) WHERE id=?',
                                 (self._now(db), credential_id))
            if updated.rowcount != 1:
                raise IdentityError('Credential not registered')

    def rotate(self, credential_id, *, expires_at, scopes=None):
        expires_at = _number(expires_at)
        if not isinstance(credential_id, str) or not re.fullmatch(r'[0-9a-f]{32}', credential_id):
            raise IdentityError('Invalid credential identifier')
        with self._transaction() as db:
            now = self._now(db)
            row = db.execute('SELECT * FROM credentials WHERE id=?', (credential_id,)).fetchone()
            if row is None or row['revoked'] is not None or row['expires'] <= now:
                raise IdentityError('An active unexpired credential is required for rotation')
            granted = _scopes(json.loads(row['scopes']) if scopes is None else scopes)
            issued = self._issue(db, row['principal'], granted, expires_at, now)
            db.execute('UPDATE credentials SET revoked=? WHERE id=?', (now, credential_id))
            return issued

    def credentials(self, principal):
        with self._transaction() as db:
            rows = db.execute('SELECT id,principal,scopes,created,expires,revoked FROM credentials '
                              'WHERE principal=? ORDER BY created,id', (_principal(principal),)).fetchall()
            return [{'id': row['id'], 'principal': row['principal'], 'scopes': json.loads(row['scopes']),
                     'created_at': row['created'], 'expires_at': row['expires'], 'revoked_at': row['revoked']}
                    for row in rows]

    def authenticate(self, token, required_scope):
        if not isinstance(token, str) or not _TOKEN.fullmatch(token) or required_scope not in SCOPES:
            return None
        identifier = _TOKEN.fullmatch(token)[1]
        with self._transaction() as db:
            now = self._now(db)
            row = db.execute('SELECT c.*,p.active FROM credentials c JOIN principals p ON p.id=c.principal '
                             'WHERE c.id=?', (identifier,)).fetchone()
            valid = hmac.compare_digest(self._digest(token), row['digest'] if row else '0' * 64)
            if (not valid or row is None or not row['active'] or row['revoked'] is not None
                    or row['expires'] <= now or row['created'] > now
                    or required_scope not in json.loads(row['scopes'])):
                return None
            return row['principal']


class BearerAuthenticator:
    """WSGI callable deriving authorization from server routes, never body roles."""
    def __init__(self, store, *, allow_insecure_loopback=False):
        if not isinstance(store, IdentityStore) or type(allow_insecure_loopback) is not bool:
            raise IdentityError('Identity registry and explicit transport policy required')
        self.store, self.allow_insecure_loopback = store, allow_insecure_loopback

    def __call__(self, environ):
        if environ.get('wsgi.url_scheme') != 'https':
            if not self.allow_insecure_loopback or environ.get('wsgi.url_scheme') != 'http':
                return None
            try:
                if not ipaddress.ip_address(environ.get('REMOTE_ADDR', '')).is_loopback:
                    return None
            except ValueError:
                return None
        header = environ.get('HTTP_AUTHORIZATION')
        if not isinstance(header, str) or len(header) > 512:
            return None
        match = re.fullmatch(r'Bearer ([A-Za-z0-9_.-]+)', header, re.IGNORECASE)
        if match is None:
            return None
        method, path = environ.get('REQUEST_METHOD'), environ.get('PATH_INFO', '')
        task = re.fullmatch(r'/v1/tasks/[A-Za-z0-9][A-Za-z0-9_-]{0,63}(/result|/resume|/cancel)?', path)
        scope = None
        if method == 'GET' and task and task[1] in (None, '/result'):
            scope = 'tasks:read'
        elif method == 'POST' and (path == '/v1/tasks' or (task and task[1] in ('/resume', '/cancel'))
                                   or path in ('/api/generate', '/api/chat')):
            scope = 'tasks:write'
        elif method == 'GET' and path == '/api/tags':
            scope = 'models:read'
        try:
            return self.store.authenticate(match[1], scope)
        except Exception:
            return None

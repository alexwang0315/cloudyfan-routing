"""Prompt-free retention lifecycle and exact operator maintenance plans.

No timer, cleanup worker, network call or filesystem removal runs on construction.
"""
from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import threading
import time


class RetentionError(Exception):
    def __init__(self, code, status=409):
        self.code, self.status = code, status
        super().__init__(code)


@dataclass(frozen=True)
class RetentionPolicy:
    version: str
    ttl_seconds: int

    def __post_init__(self):
        if (not isinstance(self.version, str) or not self.version.strip()
                or isinstance(self.ttl_seconds, bool) or not isinstance(self.ttl_seconds, int)
                or self.ttl_seconds < 1):
            raise ValueError('Versioned positive retention TTL required')

    def descriptor(self):
        return {'version': self.version, 'ttl_seconds': self.ttl_seconds}


class RetentionStore:
    """Durable lifecycle outside task directories; task tombstones never expire."""
    def __init__(self, root, policy, *, clock=time.time):
        if not isinstance(policy, RetentionPolicy) or not callable(clock):
            raise ValueError('Retention policy and clock required')
        self.root, self.policy, self.clock = Path(root), policy, clock
        if self.root.is_symlink() or any(p.is_symlink() for p in self.root.parents):
            raise ValueError('Safe retention root required')
        if self.root.exists() and not (self.root / 'lifecycle.sqlite3').exists() and any(self.root.iterdir()):
            raise ValueError('Retention database missing; never reset lifecycle history')
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)
        self.path = self.root / 'lifecycle.sqlite3'
        if self.path.is_symlink():
            raise ValueError('Safe retention database required')
        self._local, self._threads = threading.local(), threading.Lock()
        self._task_locks = {}
        with self._db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS tasks '
                '(id TEXT PRIMARY KEY, created REAL, expires REAL, policy TEXT, '
                'state TEXT, queued INTEGER, audit TEXT)')
            db.execute('CREATE TABLE IF NOT EXISTS clock (singleton INTEGER PRIMARY KEY, observed REAL)')
        os.chmod(self.path, 0o600)

    @contextmanager
    def _db(self):
        if (self.path.is_symlink() or (self.path.exists() and
                (not self.path.is_file() or self.path.stat().st_nlink != 1))):
            raise RetentionError('unsafe_retention_metadata', 403)
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def _now(self):
        value = self.clock()
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise RetentionError('retention_clock_unavailable', 503)
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            previous = db.execute('SELECT observed FROM clock WHERE singleton=1').fetchone()
            if previous is not None and value < previous['observed']:
                raise RetentionError('retention_clock_rollback', 503)
            db.execute('INSERT OR REPLACE INTO clock VALUES (1,?)', (value,))
        return value

    @contextmanager
    def guard(self, task_id):
        if not isinstance(task_id, str) or not re.fullmatch(r'http-[a-f0-9]{48}', task_id):
            raise RetentionError('invalid_retention_identity', 403)
        # The map mutex never covers task work: unrelated task namespaces must
        # remain concurrent. Reentrant per-task locks coordinate same-store threads;
        # the permanent flock coordinates independent stores/processes.
        with self._threads:
            task_lock = self._task_locks.setdefault(task_id, threading.RLock())
        with task_lock:
            held = getattr(self._local, 'held', set())
            if task_id in held:
                yield
                return
            path = self.root / (task_id + '.lock')
            if path.is_symlink():
                raise RetentionError('unsafe_retention_metadata', 403)
            fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            try:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise RetentionError('task_busy', 409) from None
                self._local.held = held | {task_id}
                try:
                    yield
                finally:
                    self._local.held = held
                    fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def lookup(self, task_id):
        with self._db() as db:
            row = db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
        if row is None:
            return None
        return {'internal_id': row['id'], 'created_at': row['created'], 'expires_at': row['expires'],
                'policy': json.loads(row['policy']), 'state': row['state'], 'queued': bool(row['queued']),
                'audit': json.loads(row['audit']) if row['audit'] else None}

    def access(self, task_id, *, create=False, legacy_exists=False):
        row = self.lookup(task_id)
        if row is not None and row['state'] != 'active':
            raise RetentionError('task_retired' if row['state'] == 'retired' else 'task_expired', 410)
        now = self._now()
        if row is None:
            if legacy_exists:
                raise RetentionError('retention_metadata_missing', 409)
            if not create:
                return
            with self._db() as db:
                db.execute('INSERT INTO tasks VALUES (?,?,?,?,?,?,NULL)',
                    (task_id, now, now + self.policy.ttl_seconds,
                     json.dumps(self.policy.descriptor(), sort_keys=True), 'active', 0))
            return
        if row['policy'] != self.policy.descriptor():
            raise RetentionError('retention_policy_changed', 409)
        if now >= row['expires_at']:
            with self._db() as db:
                db.execute('UPDATE tasks SET state="expired" WHERE id=?', (task_id,))
            raise RetentionError('task_expired', 410)

    def mark_queued(self, task_id):
        with self._db() as db:
            db.execute('UPDATE tasks SET queued=1 WHERE id=?', (task_id,))

    def retire(self, task_id, audit=None):
        if self.lookup(task_id) is None:
            raise RetentionError('retention_metadata_missing', 409)
        with self._db() as db:
            db.execute('UPDATE tasks SET state="retired", audit=? WHERE id=?',
                       (json.dumps(audit, sort_keys=True, allow_nan=False), task_id))


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()


def digest(value):
    import hashlib
    return hashlib.sha256(canonical(value)).hexdigest()


def open_file_paths(path):
    """Known closed is []; unavailable/partial platform inspection is None."""
    import subprocess
    try:
        result = subprocess.run(['lsof', '-nP', '-Fpn', '+D', str(path)],
                                capture_output=True, text=True, timeout=15, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.stderr.strip() or result.returncode not in (0, 1):
        return None
    paths = sorted(set(line[1:] for line in result.stdout.splitlines() if line.startswith('n')))
    # macOS lsof can return 1 with valid matches and no diagnostics. Treat those
    # matches as open (blocking), never as a known-closed result.
    if result.returncode == 1 and result.stdout.strip() and not paths:
        return None
    return paths


def inventory(path, root, *, open_files=open_file_paths):
    """Exact regular-file manifest; no following links or reading outside a root."""
    import hashlib
    import stat
    path, root = Path(path).absolute(), Path(root).absolute()
    result = {'path': str(path), 'root': str(root), 'exists': path.exists(), 'entries': [],
              'bytes': 0, 'open_files': None, 'status': 'unsafe'}
    try:
        if (path == root or root not in path.parents or path != path.resolve()
                or any(parent.is_symlink() for parent in (path, *path.parents))):
            raise ValueError('unsafe path')
        if not path.is_dir():
            raise ValueError('missing directory')
        def visit(directory):
            for child in sorted(directory.iterdir()):
                info = child.lstat()
                relative = str(child.relative_to(path))
                if child.parent == path and child.name == '.lock':
                    if not stat.S_ISREG(info.st_mode) or info.st_size or info.st_nlink != 1:
                        raise ValueError('unsafe lock')
                    continue
                if child.parent == path and child.name == '.retired.json':
                    raise ValueError('preexisting retirement marker')
                common = {'path': relative, 'mode': stat.S_IMODE(info.st_mode),
                          'device': info.st_dev, 'inode': info.st_ino, 'mtime_ns': info.st_mtime_ns}
                if stat.S_ISDIR(info.st_mode):
                    result['entries'].append({**common, 'kind': 'directory', 'sha256': None, 'bytes': 0})
                    visit(child)
                elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                    fd = os.open(child, os.O_RDONLY | os.O_NOFOLLOW)
                    try:
                        before = os.fstat(fd)
                        if (before.st_ino, before.st_dev, before.st_nlink) != (info.st_ino, info.st_dev, 1):
                            raise ValueError('file changed')
                        hashed = hashlib.sha256()
                        while chunk := os.read(fd, 1024 * 1024):
                            hashed.update(chunk)
                        after = os.fstat(fd)
                        if (after.st_size, after.st_mtime_ns) != (info.st_size, info.st_mtime_ns):
                            raise ValueError('file changed')
                    finally:
                        os.close(fd)
                    result['entries'].append({**common, 'kind': 'file', 'sha256': hashed.hexdigest(),
                                              'bytes': info.st_size})
                    result['bytes'] += info.st_size
                else:
                    raise ValueError('unsupported or linked entry')
        visit(path)
        opened = open_files(path)
        if opened is not None and (not isinstance(opened, list) or any(not isinstance(p, str) for p in opened)):
            opened = None
        # Maintained advisory locks are intentionally kept, never unlinked.
        result['open_files'] = (sorted(p for p in opened if p != str(path / '.lock'))
                                if opened is not None else None)
        result['status'] = ('open_files_unknown' if result['open_files'] is None else
                            'open_files' if result['open_files'] else 'closed')
    except (OSError, ValueError):
        result['status'] = 'unsafe'
    result['sha256'] = digest({key: value for key, value in result.items() if key != 'sha256'})
    return result


@contextmanager
def maintenance_lock(path):
    """Advisory writer fence; kept lock inodes are never removed by purge."""
    import stat
    path = Path(path)
    if path.is_symlink() or any(parent.is_symlink() for parent in path.parents):
        raise RetentionError('unsafe_maintenance_lock', 403)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size:
            raise RetentionError('unsafe_maintenance_lock', 403)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RetentionError('retention_writer_busy', 409) from None
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def purge_exact(targets):
    """Remove only approved inventoried entries; callers must hold writer fences."""
    from .receipt_store import _fsync_dir
    for target in targets:
        root = Path(target['path'])
        for entry in reversed(target['entries']):
            path = root / entry['path']
            info = path.lstat()
            if (info.st_dev, info.st_ino, info.st_mtime_ns) != (
                    entry['device'], entry['inode'], entry['mtime_ns']):
                # Directory mtimes change as approved children are removed.
                if entry['kind'] != 'directory' or (info.st_dev, info.st_ino) != (
                        entry['device'], entry['inode']):
                    raise RetentionError('retention_target_changed', 409)
            if entry['kind'] == 'file':
                if path.is_symlink() or info.st_nlink != 1 or info.st_size != entry['bytes']:
                    raise RetentionError('retention_target_changed', 409)
                path.unlink()
            else:
                path.rmdir()
            _fsync_dir(path.parent)
        # Task/Core directories keep their permanent lock and retirement marker.
        remaining = {p.name for p in root.iterdir()}
        if remaining - {'.lock', '.retired.json'}:
            raise RetentionError('retention_target_changed', 409)
        if not remaining:
            root.rmdir()
            _fsync_dir(root.parent)

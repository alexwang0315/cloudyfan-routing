"""Immutable local snapshots with an atomically published current pointer."""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re
import uuid

from .catalog import canonical, timestamp, verify_snapshot

_UNCHECKED = object()


@contextmanager
def _locked(root):
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (root / '.publish.lock').open('a') as stream:
        os.fchmod(stream.fileno(), 0o600)
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


def _write(path, value):
    with path.open('xb') as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(canonical(value) + b'\n')
        stream.flush()
        os.fsync(stream.fileno())


def _id(value):
    if not isinstance(value, str) or not re.fullmatch('[a-f0-9]{64}', value):
        raise ValueError('Invalid snapshot id')
    return value


class SnapshotStore:
    """Each directory represents one domain/policy stream. Hashes are integrity
    checks, not authentication. Protect this directory as trusted local config.
    Incomplete updates leave the previous current pointer intact.
    """
    def __init__(self, root):
        self.root = Path(root)
        if self.root.is_symlink():
            raise ValueError('Snapshot root cannot be a symlink')

    def load(self, snapshot_id=None, *, now=None):
        if snapshot_id is None:
            pointer = self.root / 'current.json'
            if pointer.is_symlink():
                raise ValueError('Snapshot pointer cannot be a symlink')
            snapshot_id = json.loads(pointer.read_text())['snapshot_id']
        path = self.root / (_id(snapshot_id) + '.json')
        if path.is_symlink():
            raise ValueError('Snapshot cannot be a symlink')
        snapshot = json.loads(path.read_text())
        verify_snapshot(snapshot, now=now)
        if snapshot['snapshot_id'] != snapshot_id:
            raise ValueError('Snapshot filename identity mismatch')
        return snapshot

    def publish(self, snapshot, *, now, expected_current_id=_UNCHECKED):
        verify_snapshot(snapshot, now=now)
        if any(not snapshot['pools'][tier] for tier in ('low', 'mid', 'high')):
            raise ValueError('All three pools must contain qualified candidates before publication')
        with _locked(self.root):
            current_path = self.root / 'current.json'
            current = self.load() if current_path.exists() else None
            if expected_current_id is not _UNCHECKED and expected_current_id != (
                    current['snapshot_id'] if current else None):
                raise ValueError('Current snapshot changed; review a fresh update')
            if current is not None:
                if current['policy']['domain'] != snapshot['policy']['domain']:
                    raise ValueError('One store must serve only one domain')
                if timestamp(current['created_at']) > timestamp(snapshot['created_at']):
                    raise ValueError('Refusing publication of an older snapshot')
            path = self.root / (_id(snapshot['snapshot_id']) + '.json')
            if path.exists():
                if self.load(snapshot['snapshot_id']) != snapshot:
                    raise ValueError('Immutable snapshot collision')
            else:
                staged = self.root / ('snapshot-' + uuid.uuid4().hex + '.tmp')
                _write(staged, snapshot)
                os.replace(staged, path)
            pending = self.root / ('current-' + uuid.uuid4().hex + '.tmp')
            _write(pending, {'snapshot_id': snapshot['snapshot_id']})
            os.replace(pending, current_path)
            fd = os.open(self.root, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        return snapshot['snapshot_id']

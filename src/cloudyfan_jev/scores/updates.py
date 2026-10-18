"""Offline, reviewable score updates with explicit publication and durable audit."""
from contextlib import contextmanager
import fcntl
import json
import os
import re
import uuid

from .catalog import canonical, digest
from .qualification import SCHEMA, qualified_snapshot


def _verified(pack):
    if pack.get('schema_version') != SCHEMA or pack.get('pack_id') != digest(
            {k: v for k, v in pack.items() if k != 'pack_id'}):
        raise ValueError('Qualification pack integrity mismatch')


def _summary(pack):
    if pack is None:
        return None
    preview = pack['preview']
    policy = pack['inputs']['policy']
    return {'pack_id': pack['pack_id'], 'input_sha256': pack['input_sha256'],
            'status': pack['status'], 'production_ready': pack['production_ready'],
            'preview_snapshot_id': preview['snapshot_id'] if preview else None,
            **{k: policy.get(k) for k in ('id', 'domain', 'source', 'source_version', 'metric')},
            'blockers': pack['blockers']}


def preview_update(previous, proposed, *, now, allow_synthetic=False):
    """Compare evidence-bound previews, without writing or performing requests."""
    for pack in (previous, proposed):
        if pack is not None:
            _verified(pack)

    def members(pack):
        if pack is None or pack['preview'] is None:
            return {}, {}
        tiers = {row['config_id']: tier for tier, rows in pack['preview']['pools'].items() for row in rows}
        qualification = {key: 'eligible' for key in tiers}
        qualification.update({row['config_id']: row['reason'] for row in pack['preview']['excluded']})
        return tiers, qualification

    old, old_qual = members(previous)
    new, new_qual = members(proposed)
    snapshot_id = None
    promotion_error = None
    try:
        snapshot_id = qualified_snapshot(proposed, now=now, allow_synthetic=allow_synthetic)['snapshot_id']
    except ValueError:
        promotion_error = 'qualification_or_expiry_blocked'
    return {'before': _summary(previous), 'after': _summary(proposed),
            'proposed_snapshot_id': snapshot_id, 'publishable': snapshot_id is not None,
            'promotion_error': promotion_error,
            'added': sorted(new.keys() - old.keys()), 'removed': sorted(old.keys() - new.keys()),
            'tier_changes': [{'config_id': key, 'before': old[key], 'after': new[key]}
                             for key in sorted(old.keys() & new.keys()) if old[key] != new[key]],
            'qualification_changes': [{'config_id': key, 'before': old_qual.get(key), 'after': new_qual.get(key)}
                                     for key in sorted(old_qual.keys() & new_qual.keys())
                                     if old_qual[key] != new_qual[key]]}


class ScoreUpdater:
    """Serialize update operations; durable records survive process interruption.

    The store is trusted local configuration. Operation IDs cannot be reused with
    another input or with a different synthetic/production permission.
    """
    def __init__(self, store):
        self.store = store
        self.root = store.root / 'updates'
        if self.root.is_symlink():
            raise ValueError('Update audit root cannot be a symlink')

    def _path(self, operation_id):
        if not isinstance(operation_id, str) or not re.fullmatch('[A-Za-z0-9][A-Za-z0-9_-]{0,79}', operation_id):
            raise ValueError('Invalid update operation id')
        path = self.root / (operation_id + '.json')
        if path.is_symlink():
            raise ValueError('Update records cannot be symlinks')
        return path

    @contextmanager
    def _locked(self):
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self.root / '.update.lock'
        if path.is_symlink():
            raise ValueError('Update lock cannot be a symlink')
        with path.open('a') as lock:
            os.fchmod(lock.fileno(), 0o600)
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    def _save(self, path, record):
        temporary = self.root / ('audit-' + uuid.uuid4().hex + '.tmp')
        with temporary.open('xb') as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(canonical(record) + b'\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        fd = os.open(self.root, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def inspect(self, operation_id):
        return json.loads(self._path(operation_id).read_text())

    def _current(self):
        return self.store.load()['snapshot_id'] if (self.store.root / 'current.json').exists() else None

    def apply(self, operation_id, previous, proposed, *, now, allow_synthetic=False):
        path = self._path(operation_id)
        input_id = digest({'previous': previous, 'proposed': proposed, 'allow_synthetic': allow_synthetic})
        with self._locked():
            prior = self.inspect(operation_id) if path.exists() else None
            if prior and prior['input_id'] != input_id:
                raise ValueError('Operation id is already bound to different update inputs')
            if prior and prior['status'] in ('published', 'blocked'):
                return prior
            if prior and prior.get('snapshot_id') and self._current() == prior['snapshot_id']:
                # A durable target + matching current proves publication even if
                # the snapshot expired after the final audit write was interrupted.
                prior.update(status='published', attempts=prior['attempts'] + 1, recovered_from_current=True)
                prior['history'].append({'at': now, 'status': 'published', 'recovered_from_current': True})
                self._save(path, prior)
                return prior
            record = {'operation_id': operation_id, 'input_id': input_id,
                      'created_at': prior['created_at'] if prior else now,
                      'status': 'pending', 'attempts': (prior['attempts'] if prior else 0) + 1,
                      'history': prior['history'] if prior else []}
            self._save(path, record)
            phase = 'preview'
            try:
                preview = preview_update(previous, proposed, now=now, allow_synthetic=allow_synthetic)
                record['preview'] = preview
                if not preview['publishable']:
                    record['status'] = 'blocked'
                else:
                    phase = 'qualification'
                    snapshot = qualified_snapshot(proposed, now=now, allow_synthetic=allow_synthetic)
                    expected = (qualified_snapshot(previous, now=previous['created_at'],
                                allow_synthetic=allow_synthetic)['snapshot_id'] if previous else None)
                    record.update(snapshot_id=snapshot['snapshot_id'], expected_current_id=expected)
                    self._save(path, record)
                    phase = 'current_comparison'
                    if self._current() != snapshot['snapshot_id']:
                        if self._current() != expected:
                            raise ValueError('Current snapshot changed; review a fresh update')
                        phase = 'publication'
                        self.store.publish(snapshot, now=now, expected_current_id=expected)
                    record['status'] = 'published'
                record['history'].append({'at': now, 'status': record['status']})
            except (ValueError, KeyError, TypeError, AttributeError, OSError) as error:
                record['status'] = 'failed'
                record['error'] = type(error).__name__
                record['failure_reason'] = phase + ':' + type(error).__name__
                record['history'].append({'at': now, 'status': 'failed', 'reason': record['failure_reason']})
            self._save(path, record)
            return record

"""Same-path copied faults with two original HTTP hosts and preserved originals."""
from contextlib import contextmanager
from datetime import datetime
import fcntl
import hashlib
import json
from pathlib import Path
import sqlite3
import time

import pytest

from capacity_queue_process_fixture import QueueProcessPair, publish_test_receipt
from capacity_resilience_process_worker import assemble_resilience_host


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def readonly_rows(path, table):
    if not table.replace('_', '').isalnum():
        raise ValueError('Only a fixed evidence table may be read')
    with sqlite3.connect(Path(path).as_uri() + '?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        return [dict(row) for row in db.execute('SELECT * FROM ' + table)]


def selective_sqlite_copy(source, destination, *, omit_tables=(), omit_rows=None):
    """Materialize a NEW incomplete copy; never remove an original table or row."""
    source, destination = Path(source), Path(destination)
    if destination.exists():
        raise ValueError('A copied fault must use a never-created destination')
    omitted = set(omit_tables)
    with sqlite3.connect(source.as_uri() + '?mode=ro', uri=True) as original:
        with sqlite3.connect(destination) as copied:
            schema = original.execute("SELECT type,name,tbl_name,sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' ORDER BY type='table' DESC,rowid").fetchall()
            for kind, name, owner, sql in schema:
                if kind == 'table' and name not in omitted:
                    copied.execute(sql)
                    columns = [row[1] for row in original.execute('PRAGMA table_info("' + name + '")')]
                    rows = original.execute('SELECT * FROM "' + name + '" ORDER BY rowid').fetchall()
                    excluded = (omit_rows or {}).get(name, set())
                    rows = [row for row in rows if not excluded or row[0] not in excluded]
                    quoted = ','.join('"' + column + '"' for column in columns)
                    copied.executemany('INSERT INTO "' + name + '" (' + quoted + ') VALUES ('
                        + ','.join('?' for _ in columns) + ')', rows)
            for kind, name, owner, sql in schema:
                if kind != 'table' and owner not in omitted:
                    copied.execute(sql)
    destination.chmod(0o600)


class ResilienceProcessPair(QueueProcessPair):
    """Exercise real product behavior while separately recording original evidence."""

    def __init__(self, root, network, *, assembly=assemble_resilience_host, **options):
        super().__init__(root, network, assembly, **options)
        if options.get('health_core'):
            from candidate_health_fixture import health_values
            self.clock.value.value = datetime.fromisoformat(health_values()['now']).timestamp()
            self.options['configuration_at'] = self.clock()
        self.fault_originals = {}

    def stop_all(self):
        for index, worker in enumerate(self.workers):
            if not worker.connection.closed:
                self.finish(index)

    def activate(self):
        if not self.options.get('health_core'):
            return super().activate()
        response = self.client(operator=True).post('/monitor/capacity/activate',
            json={'revision': 0, 'version': 'synthetic-resilience-queued-v1'})
        assert response.status_code == 200, response.text
        return response.json()

    def client(self, index=0, **options):
        client = super().client(index, **options)
        pending = {}

        def started(request):
            pending[id(request)] = time.monotonic()
            self.observe('diagnostic_http_started', {'worker': index, 'method': request.method,
                'path': request.url.path})

        def completed(response):
            began = pending.pop(id(response.request), None)
            self.observe('diagnostic_http_completed', {'worker': index,
                'method': response.request.method, 'path': response.request.url.path,
                'status': response.status_code,
                'wall_seconds': None if began is None else time.monotonic() - began})

        client.event_hooks['request'].append(started)
        client.event_hooks['response'].append(completed)
        return client

    def reopen(self):
        self.stop_all()
        for index in range(len(self.workers)):
            self.start(index)

    def fault(self, relative, *, omitted=(), omitted_rows=None, update=None, absent=False, empty=False):
        """Retain the original DB and install a NEW fault at its identical bound path."""
        assert all(worker.connection.closed for worker in self.workers), 'Stop actual hosts before copying'
        path = self.root / relative
        assert path.is_file() and path.suffix in {'.sqlite', '.sqlite3'}
        archive = self.root / 'fault-originals' / relative
        archive.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        assert not archive.exists()
        before = sha256(path)
        path.rename(archive)
        assert sha256(archive) == before
        self.fault_originals[str(archive.relative_to(self.root))] = before
        if not absent:
            if empty:
                with sqlite3.connect(path):
                    pass
                path.chmod(0o600)
            elif omitted or omitted_rows:
                selective_sqlite_copy(archive, path, omit_tables=omitted, omit_rows=omitted_rows)
            else:
                with sqlite3.connect(archive.as_uri() + '?mode=ro', uri=True) as original:
                    with sqlite3.connect(path) as copied:
                        original.backup(copied)
                path.chmod(0o600)
            if update is not None:
                statement, parameters = update
                assert statement.lstrip().upper().startswith('UPDATE ')
                with sqlite3.connect(path) as copied:
                    copied.execute(statement, parameters)
        self.observe('same_bound_path_copied_fault', {'path': relative, 'original': str(archive.relative_to(self.root)),
            'original_sha256': before, 'copy_sha256': sha256(path) if path.exists() else None,
            'omitted_tables': list(omitted), 'omitted_row_ids': {key: sorted(value) for key, value in (omitted_rows or {}).items()},
            'update': update[0] if update is not None else None, 'absent': absent, 'empty': empty})

    def originals_unchanged(self):
        assert all(sha256(self.root / path) == value for path, value in self.fault_originals.items())

    def retain_missing_directory(self, relative):
        """Move an owned synthetic companion aside without erasing any member."""
        assert all(worker.connection.closed for worker in self.workers)
        relative = Path(relative)
        assert not relative.is_absolute() and '..' not in relative.parts
        path, archive = self.root / relative, self.root / 'fault-originals' / relative
        assert path.is_dir() and not path.is_symlink() and not archive.exists()
        before = {str(child.relative_to(path)): sha256(child) for child in path.rglob('*') if child.is_file()}
        archive.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.rename(archive)
        for name, digest in before.items():
            moved = archive / name
            assert sha256(moved) == digest
            self.fault_originals[str(moved.relative_to(self.root))] = digest
        self.observe('same_bound_path_retained_missing_companion', {'path': str(relative),
            'retained': str(archive.relative_to(self.root)), 'original_files': before})

    @contextmanager
    def original_task_lock(self, principal, run):
        # The existing TaskService stable public identity; this is an OS storage
        # interruption boundary, not a substitute task/admission implementation.
        identity = 'http-' + hashlib.sha256(json.dumps([principal, run], sort_keys=True,
            ensure_ascii=False, allow_nan=False).encode()).hexdigest()[:48]
        directory = Path(self.workers[0].ready['service_root']) / identity
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        with (directory / '.lock').open('a') as stream:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                yield directory
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)

    def __exit__(self, kind, value, traceback):
        try:
            return super().__exit__(kind, value, traceback)
        finally:
            self.originals_unchanged()


@pytest.fixture
def resilience_process_pair(tmp_path, loopback_network, request):
    pairs = []

    @contextmanager
    def start(**options):
        pair = ResilienceProcessPair(tmp_path / ('pair-' + str(len(pairs))), loopback_network, **options)
        pairs.append(pair)
        with pair:
            yield pair

    yield start
    publish_test_receipt(request, pairs, loopback_network)

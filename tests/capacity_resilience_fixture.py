"""Owned synthetic fault copies; retain every untouched original at its old binding."""
import hashlib
import json
from pathlib import Path
import sqlite3

from cloudyfan_jev.capacity import CapacityStore
from cloudyfan_jev.capacity_dispatch import CapacityController
from cloudyfan_jev.capacity_windows import CapacityWindows
from cloudyfan_monitor.capacity import CapacityObserver
from cloudyfan_monitor.integration import assemble_monitor


def build_resilience_host(root, *, clock, origin, options=None, before_response=None):
    """Real reopenable service/queue; only upstream responses are simulated.

    The existing queue fixture persists credentials, configuration timestamps,
    receipt roots and actual BudgetStores. Fault copies keep these exact paths.
    No HTTP route exposes fixture controls or the private preservation witness.
    """
    from capacity_queue_fixture import build_queue_host
    host = build_queue_host(root, clock=clock, origin=origin, options=options,
        before_response=before_response)
    host.controller = host.bundle.app.capacity_controller
    host.windows = host.controller.window_backend
    host.protected_paths += (host.controller.store.integrity.root,)
    return host


def copied_database_fault(path, archive, *, omit=(), updates=()):
    """Move the closed original aside, then build a NEW DB at the same host path.

    No original or copy is deleted. Missing tables are omitted while copying;
    SQL UPDATE faults apply only to newly copied rows, never the retained source.
    """
    path, archive = Path(path), Path(archive)
    archive.mkdir(parents=True, mode=0o700)
    retained = archive / path.name
    fingerprint = hashlib.sha256(path.read_bytes()).hexdigest()
    path.rename(retained)
    with sqlite3.connect(retained.resolve().as_uri() + '?mode=ro', uri=True) as original:
        with sqlite3.connect(path) as candidate:
            objects = original.execute("SELECT type,name,tbl_name,sql FROM sqlite_master "
                "WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' ORDER BY type='table' DESC,name").fetchall()
            for kind, name, table, sql in objects:
                if table in omit:
                    continue
                candidate.execute(sql)
                if kind == 'table':
                    columns = original.execute('PRAGMA table_info("' + name + '")').fetchall()
                    candidate.executemany('INSERT INTO "' + name + '" VALUES(' + ','.join('?' for _ in columns) + ')',
                        original.execute('SELECT * FROM "' + name + '"'))
            for statement, arguments in updates:
                assert statement.startswith('UPDATE '), 'Only explicit new-copy UPDATE faults allowed'
                candidate.execute(statement, arguments)
    path.chmod(0o600)
    receipt = {'original': str(retained), 'original_sha256': fingerprint,
        'candidate': str(path), 'omitted_tables': list(omit), 'update_count': len(updates),
        'original_unchanged': hashlib.sha256(retained.read_bytes()).hexdigest() == fingerprint}
    (archive / 'copy-receipt.json').write_text(json.dumps(receipt, indent=2))
    assert receipt['original_unchanged']
    return receipt


def reopen_windows(bundle, controller, clock):
    store = CapacityStore(controller.store.path, clock=clock)
    windows = CapacityWindows(store, version='synthetic-windows-v1', rules=[
        {'binding': 'synthetic-binding', 'group': 'shared', 'header': 'retry-after',
         'format': 'retry_after', 'constraint': 'requests'}])
    current = CapacityController(store, credential_active=controller.credential_active, window_backend=windows)
    current.register('synthetic-window-admission-v1', configuration_version='synthetic-admission-config-v1',
        routes=controller.routes['synthetic-window-admission-v1'], max_seconds=300, windows_version=windows.version)
    old = bundle.app
    bundle.app = assemble_monitor(old.store, old.gateway.service, old.gateway.registry, old.identity_store,
        operator_token=bundle.operator_token, source=old.source, origin=old.origin, demo=True,
        capacity_observer=CapacityObserver(store, sources={}), capacity_controller=current)
    return current, windows

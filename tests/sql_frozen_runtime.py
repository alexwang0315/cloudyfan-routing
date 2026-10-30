"""The machine that froze the checked-in SQL suite ID; runtime binding stays a product rule.

The suite ID digests the SQL runtime, including the platform, kernel, Python build and
SQLite build. Checked-in tables therefore refuse to start elsewhere by design. Tests of
runner mechanics use a copy bound to the current suite; the checked-in IDs are checked by
substituting only the frozen machine fields, so any other suite drift still fails anywhere.
"""
from copy import deepcopy
import json
from pathlib import Path

from cloudyfan_jev.scores.catalog import digest
from cloudyfan_jev.scores.sql_exam import manifest

ROOT = Path(__file__).resolve().parents[1]
FROZEN_MACHINE = {
    'sqlite': {'sqlite_version': '3.50.4',
               'sqlite_native_limits': {'SQLITE_LIMIT_LIKE_PATTERN_LENGTH': 50000,
                                        'SQLITE_LIMIT_TRIGGER_DEPTH': 1000,
                                        'SQLITE_LIMIT_VARIABLE_NUMBER': 32766}},
    'process': {'platform': 'darwin', 'kernel': '22.6.0', 'machine': 'x86_64',
                'python': [3, 13, 13],
                'python_identity': {'cache_tag': 'cpython-313', 'implementation': 'cpython',
                                    'version': '3.13.13 (main, Apr 14 2026, 14:23:36) [Clang 22.1.3 ]'}},
}
CHECKED_IN = ('sql-model-versions-v1.json', 'sql-low-two-candidate-plan-v1.json',
              'sql-backup-candidate-review-v1.json')


def machine(runtime):
    return {part: {key: runtime[part][key] for key in keys} for part, keys in FROZEN_MACHINE.items()}


def frozen_runtime():
    return machine(manifest()['sql_runtime']) == FROZEN_MACHINE


def suite_id_on_frozen_machine():
    body = {k: v for k, v in manifest().items() if k != 'suite_id'}
    for part, fields in FROZEN_MACHINE.items():
        body['sql_runtime'][part].update(deepcopy(fields))
    return digest(body)


def checked_in_suite_ids():
    found = []

    def walk(value):
        if isinstance(value, dict):
            for key, item in value.items():
                # Archived evidence records the suite it ran under; it never moves to a new one.
                if key == 'archived_profile_evidence':
                    continue
                found.append(item) if key == 'suite_id' else walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)
    for name in CHECKED_IN:
        walk(json.loads((ROOT / 'examples' / name).read_text()))
    return found


def local_version_table():
    table = json.loads((ROOT / 'examples/sql-model-versions-v1.json').read_text())
    return {**table, 'suite_id': manifest()['suite_id']}

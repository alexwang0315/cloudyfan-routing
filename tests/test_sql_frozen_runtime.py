"""Checked-in SQL suite IDs stay machine-bound; mechanism tests run on any runtime."""
import json
from pathlib import Path
import platform

import pytest

from cloudyfan_jev.scores import sql_exam
from sql_frozen_runtime import (checked_in_suite_ids, frozen_runtime, local_version_table,
                                suite_id_on_frozen_machine)
from test_sql_model_runner import setup_runner

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def foreign_runtime(monkeypatch):
    sql_exam._manifest.cache_clear()
    monkeypatch.setattr(platform, 'release', lambda: '0.0.0-foreign')
    yield
    monkeypatch.undo()
    sql_exam._manifest.cache_clear()


def test_checked_in_suite_ids_are_the_current_suite_on_the_frozen_machine():
    ids = checked_in_suite_ids()
    assert len(ids) >= 5 and set(ids) == {suite_id_on_frozen_machine()}
    assert frozen_runtime() is (sql_exam.manifest()['suite_id'] == ids[0])


def test_foreign_runtime_refuses_checked_in_table_but_runs_local_copy(tmp_path, foreign_runtime):
    assert frozen_runtime() is False
    assert set(checked_in_suite_ids()) == {suite_id_on_frozen_machine()}
    assert local_version_table()['suite_id'] == sql_exam.manifest()['suite_id']
    checked_in = json.loads((ROOT / 'examples/sql-model-versions-v1.json').read_text())
    with pytest.raises(ValueError, match='Frozen suite'):
        setup_runner(tmp_path / 'checked-in', table=checked_in)
    runner, calls, _, _ = setup_runner(tmp_path / 'local')
    assert runner.capture('foreign', max_new_requests=4)['calls'] == 4 and len(calls) == 4

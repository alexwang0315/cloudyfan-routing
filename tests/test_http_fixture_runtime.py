"""Saved-comparison HTTP fixtures record the interpreter they run on instead of pinning one build."""
import platform
import sqlite3

import pytest

from saved_backup_fixture import saved_backup_http as saved_backup_http
from saved_qcs_fixture import saved_qcs_http as saved_qcs_http
from saved_window_fixture import saved_window_http as saved_window_http
from test_local_http_integration import loopback_network as loopback_network

OTHER_RUNTIME = {'python': '3.13.17', 'sqlite': '3.53.1'}


@pytest.fixture(autouse=True)
def other_patch_runtime(monkeypatch):
    # Autouse fixtures are set up before the requested HTTP fixture reads the runtime.
    monkeypatch.setattr(platform, 'python_version', lambda: OTHER_RUNTIME['python'])
    monkeypatch.setattr(sqlite3, 'sqlite_version', OTHER_RUNTIME['sqlite'])


@pytest.mark.parametrize('name', ['saved_qcs_http', 'saved_backup_http', 'saved_window_http'])
def test_fixture_runs_and_records_a_different_patch_runtime(name, request):
    tracer = request.getfixturevalue(name)
    assert {key: tracer.evidence[key] for key in OTHER_RUNTIME} == OTHER_RUNTIME

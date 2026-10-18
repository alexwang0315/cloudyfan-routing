"""Exercise fail-closed check orchestration without nested dependency installs."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]


def executable(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(0o755)


@pytest.fixture
def runner(tmp_path):
    root = tmp_path / 'checkout'
    (root / 'scripts').mkdir(parents=True)
    shutil.copy(ROOT / 'scripts/check.py', root / 'scripts/check.py')
    trace = tmp_path / 'trace.jsonl'
    program = f'''#!{sys.executable}
import json, os, pathlib, sys
name = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
with open(os.environ['TRACE'], 'a') as f:
    f.write(json.dumps({{'name': name, 'args': args, 'env': {{k: os.environ.get(k) for k in ['UV_PROJECT_ENVIRONMENT', 'UV_OFFLINE', 'PYTHONPATH', 'VIRTUAL_ENV']}}}}) + '\\n')
code = int(os.environ.get('FAIL_SYNC', '0')) if args[:1] == ['sync'] else 0
if name == 'python' and args[:2] == ['-m', 'pytest']:
    code = int(os.environ.get('FAIL_TESTS', '0'))
if name == 'python' and args and args[0].endswith('project_state.py'):
    code = int(os.environ.get('FAIL_STATE', '0'))
sys.exit(code)
'''
    executable(tmp_path / 'bin/uv', program)
    for name in ['python', 'cloudyfan-jev-demo', 'cloudyfan-score-demo']:
        executable(root / '.venv/bin' / name, program)
    env = dict(os.environ, PATH=str(tmp_path / 'bin'), TRACE=str(trace),
               UV_PROJECT_ENVIRONMENT='/wrong/environment', VIRTUAL_ENV='/wrong/active',
               PYTHONPATH='/wrong/path')

    def invoke(*args, **overrides):
        result = subprocess.run([sys.executable, str(root / 'scripts/check.py'), *args],
                                env={**env, **overrides}, cwd=tmp_path, text=True, capture_output=True)
        rows = [json.loads(line) for line in trace.read_text().splitlines()] if trace.exists() else []
        reports = [json.loads(p.read_text()) for p in (root / 'runtime/checks').glob('*/checks.json')]
        return result, rows, reports

    return root, invoke


def test_uncached_sync_stops_before_any_test_or_global_fallback(runner):
    _, invoke = runner
    result, trace, reports = invoke(FAIL_SYNC='42')
    assert result.returncode == 42
    assert len(trace) == 1
    assert trace[0]['args'][0] == 'sync'
    assert reports[0]['passed'] is False


def test_quick_uses_project_python_after_locked_offline_sync(runner):
    root, invoke = runner
    result, trace, reports = invoke('--quick')
    assert result.returncode == 0, result.stderr
    assert [t['args'][:2] for t in trace] == [['sync', '--project'], ['pip', 'check'],
        [str(root / 'scripts/project_state.py'), 'check'], ['-m', 'ruff']]
    assert {'--locked', '--offline', '--extra', 'dev', '--no-python-downloads'} <= set(trace[0]['args'])
    for t in trace:
        assert t['env'] == {'UV_PROJECT_ENVIRONMENT': str(root / '.venv'), 'UV_OFFLINE': '1',
                            'PYTHONPATH': None, 'VIRTUAL_ENV': None}
    assert reports[0]['steps'][-1]['command'][0] == str(root / '.venv/bin/python')
    assert reports[0]['passed'] is True


def test_test_failure_blocks_demos_and_build(runner):
    _, invoke = runner
    result, trace, reports = invoke('--integration', FAIL_TESTS='9')
    assert result.returncode == 9
    assert trace[-1]['args'] == ['-m', 'pytest', '-q']
    assert len(trace) == 5
    assert reports[0]['passed'] is False


def test_invalid_project_records_stop_before_lint_tests_or_build(runner):
    _, invoke = runner
    result, trace, reports = invoke('--integration', FAIL_STATE='7')
    assert result.returncode == 7
    assert len(trace) == 3
    assert trace[-1]['args'][-1] == 'check'
    assert reports[0]['passed'] is False


def test_integration_runs_both_demos_and_offline_build(runner):
    _, invoke = runner
    result, trace, reports = invoke('--integration')
    assert result.returncode == 0, result.stderr
    assert [t['name'] for t in trace[-3:]] == ['cloudyfan-jev-demo', 'cloudyfan-score-demo', 'uv']
    assert trace[-1]['args'][0] == 'build'
    assert '--offline' in trace[-1]['args'] and '--no-python-downloads' in trace[-1]['args']
    assert reports[0]['passed'] is True


def test_missing_uv_has_no_pip_or_pytest_fallback(runner):
    _, invoke = runner
    result, trace, reports = invoke(PATH='')
    assert result.returncode == 127
    assert 'no fallback' in result.stderr
    assert trace == reports == []


def test_repo_hook_preserves_prior_hooks_and_stops_on_secret_scan(tmp_path):
    root = tmp_path / 'repo'
    root.mkdir()
    env = dict(os.environ, GIT_CONFIG_GLOBAL=str(tmp_path / 'gitconfig'), GIT_CONFIG_NOSYSTEM='1')

    def git(*args):
        return subprocess.check_output(['git', *args], cwd=root, env=env, text=True).strip()

    git('init', '-q')
    old = tmp_path / 'existing-hooks'
    trace = root / 'trace'
    executable(old / 'pre-commit', '#!/bin/sh\necho secret >> trace\nexit 44\n')
    executable(old / 'post-commit', '#!/bin/sh\necho retained\n')
    git('config', 'core.hooksPath', str(old))
    for name in ['scripts/hooks.py', '.githooks/pre-commit']:
        path = root / name
        path.parent.mkdir(exist_ok=True)
        shutil.copy(ROOT / name, path)
    executable(root / 'scripts/check', '#!/bin/sh\necho quick >> trace\nexit 0\n')

    def install():
        return subprocess.run([sys.executable, str(root / 'scripts/hooks.py'), 'install'],
                              cwd=root, env=env, text=True, capture_output=True)

    assert install().returncode == 0
    hooks = Path(git('config', '--local', 'core.hooksPath'))
    assert (hooks / 'post-commit').resolve() == old / 'post-commit'
    assert install().returncode == 0
    assert git('config', '--local', 'cloudyfan.previousHooksPath') == str(old)
    result = subprocess.run([str(hooks / 'pre-commit')], cwd=root, env=env, capture_output=True)
    assert result.returncode == 44
    assert trace.read_text().splitlines() == ['secret']
    executable(old / 'pre-commit', '#!/bin/sh\necho secret >> trace\n')
    result = subprocess.run([str(hooks / 'pre-commit')], cwd=root, env=env, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert trace.read_text().splitlines() == ['secret', 'secret', 'quick']
    git('config', '--local', 'cloudyfan.previousHooksPath', str(tmp_path / 'missing-hook-dir'))
    result = subprocess.run([str(hooks / 'pre-commit')], cwd=root, env=env, capture_output=True)
    assert result.returncode == 1
    assert b'refusing to skip' in result.stderr
    assert trace.read_text().splitlines() == ['secret', 'secret', 'quick']

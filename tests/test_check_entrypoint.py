"""Exercise fail-closed check orchestration without nested dependency installs."""

import json
import os
from pathlib import Path
import select
import shutil
import signal
import subprocess
import sys
import time

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
    program = '''#!/usr/bin/env python3
import json, os, pathlib, sys
name = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
with open(os.environ['TRACE'], 'a') as f:
    f.write(json.dumps({'name': name, 'args': args, 'env': {k: os.environ.get(k) for k in ['UV_PROJECT_ENVIRONMENT', 'UV_OFFLINE', 'PYTHONPATH', 'VIRTUAL_ENV']}}) + '\\n')
code = int(os.environ.get('FAIL_SYNC', '0')) if args[:1] == ['sync'] else 0
if name == 'python' and args[:2] == ['-m', 'pytest']:
    code = int(os.environ.get('FAIL_TESTS', '0'))
if name == 'python' and args and args[0].endswith('project_state.py'):
    code = int(os.environ.get('FAIL_STATE', '0'))
sys.exit(code)
'''
    executable(tmp_path / 'bin/uv', program)
    (tmp_path / 'bin/python3').symlink_to(sys.executable)
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


def test_cli_launch_failure_closes_step_and_keeps_error_in_private_receipt(runner):
    root, invoke = runner
    executable(root / '.venv/bin/python', '#!/unavailable-verification-interpreter\n')
    result, trace, reports = invoke('--quick')
    assert result.returncode == 127
    assert len(trace) == 2
    receipt = reports[0]
    assert receipt['status'] == 'failed' and receipt['passed'] is False
    assert receipt['returncode'] == 127 and len(receipt['steps']) == 3
    step = receipt['steps'][-1]
    assert step['status'] == 'failed' and step['returncode'] == 127
    assert step['finished_at'] >= step['started_at']
    assert isinstance(step['seconds'], float) and step['seconds'] >= 0
    assert step['error']['type'] == 'FileNotFoundError'
    assert step['error']['message']
    assert result.stderr == 'Cannot start verification command; details are in the private receipt.\n'
    receipt_path = next((root / 'runtime/checks').glob('*/checks.json'))
    assert receipt_path.stat().st_mode & 0o777 == 0o600
    assert (receipt_path.parent / step['log']).stat().st_mode & 0o777 == 0o600


@pytest.fixture
def streaming_runner(runner):
    root, _ = runner
    control = root.parent / 'stream-control'
    control.mkdir()
    executable(root.parent / 'bin/uv', '''#!/usr/bin/env python3
import os, pathlib, sys, time
control = pathlib.Path(os.environ['CONTROL'])
(control / 'child-pid').write_text(str(os.getpid()))
os.write(1, 'visible before completion: 中文\\n'.encode())
os.write(2, b'invalid UTF-8: \\xff\\n')
deadline = time.monotonic() + 20
while not (control / 'release').exists() and time.monotonic() < deadline:
    time.sleep(0.01)
print('child completed', flush=True)
sys.exit(int(os.environ.get('CHILD_EXIT', '0')))
''')
    env = dict(os.environ, PATH=str(root.parent / 'bin'), CONTROL=str(control))

    def start(**overrides):
        return subprocess.Popen([sys.executable, str(root / 'scripts/check.py'), '--quick'],
                                env={**env, **overrides}, cwd=root,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

    return root, control, start


def wait_until(predicate, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    pytest.fail('Controlled child did not reach the expected observable state')


def running_receipt(root):
    paths = list((root / 'runtime/checks').glob('*/checks.json'))
    if not paths:
        return None
    try:
        return paths[0], json.loads(paths[0].read_text())
    except json.JSONDecodeError:
        return None


def test_cli_flushes_private_logs_and_running_receipt_before_child_finishes(streaming_runner):
    root, control, start = streaming_runner
    process = start(CHILD_EXIT='19')
    try:
        wait_until(lambda: (control / 'child-pid').exists())
        wait_until(lambda: running_receipt(root))
        receipt_path, receipt = running_receipt(root)
        assert process.poll() is None
        assert receipt['status'] == 'running'
        assert receipt['passed'] is False and receipt['returncode'] is None
        step = receipt['steps'][0]
        assert step['status'] == 'running' and step['returncode'] is None
        assert step['started_at'] and 'finished_at' not in step and 'seconds' not in step
        log = receipt_path.parent / step['log']
        wait_until(lambda: log.exists() and b'\xff\n' in log.read_bytes())
        assert log.stat().st_mode & 0o777 == 0o600
        assert receipt_path.stat().st_mode & 0o777 == 0o600
        console_prefix = b''
        deadline = time.monotonic() + 5
        while b'invalid UTF-8: ' + '�'.encode() not in console_prefix:
            readable, _, _ = select.select([process.stdout], [], [], max(0, deadline - time.monotonic()))
            assert readable, 'Child output was not flushed to the console before completion'
            chunk = os.read(process.stdout.fileno(), 65536)
            assert chunk, 'Runner exited before the controlled child was released'
            console_prefix += chunk
        assert process.poll() is None
        (control / 'release').touch()
        console, _ = process.communicate(timeout=5)
        console = console_prefix + console
        assert process.returncode == 19
        assert '中文' in console.decode('utf-8')
        assert 'invalid UTF-8: �' in console.decode('utf-8')
        assert b'child completed' in log.read_bytes()
        receipt = json.loads(receipt_path.read_text())
        assert receipt['status'] == 'failed' and receipt['returncode'] == 19
        assert receipt['passed'] is False
        assert len(receipt['steps']) == 1
        assert receipt['steps'][0]['status'] == 'failed'
        assert receipt['steps'][0]['returncode'] == 19
        assert receipt['steps'][0]['finished_at'] and receipt['finished_at']
        assert isinstance(receipt['steps'][0]['seconds'], float) and receipt['steps'][0]['seconds'] >= 0
    finally:
        (control / 'release').touch()
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)


def test_cli_interrupt_reaps_own_child_and_preserves_partial_artifacts(streaming_runner):
    root, control, start = streaming_runner
    process = start()
    try:
        wait_until(lambda: (control / 'child-pid').exists())
        child_pid = int((control / 'child-pid').read_text())
        wait_until(lambda: running_receipt(root))
        receipt_path, receipt = running_receipt(root)
        log = receipt_path.parent / receipt['steps'][0]['log']
        wait_until(lambda: log.exists() and b'visible before completion' in log.read_bytes())
        process.send_signal(signal.SIGINT)
        process.communicate(timeout=15)
        assert process.returncode == 130
        with pytest.raises(ProcessLookupError):
            os.kill(child_pid, 0)
        receipt = json.loads(receipt_path.read_text())
        assert receipt['passed'] is False and receipt['status'] == 'interrupted'
        assert receipt['returncode'] == 130 and len(receipt['steps']) == 1
        assert receipt['steps'][0]['status'] == 'interrupted'
        assert receipt['steps'][0]['returncode'] == 130
        assert receipt['steps'][0]['finished_at'] >= receipt['steps'][0]['started_at']
        assert isinstance(receipt['steps'][0]['seconds'], float) and receipt['steps'][0]['seconds'] >= 0
        assert b'visible before completion' in log.read_bytes()
        assert b'child completed' not in log.read_bytes()
    finally:
        (control / 'release').touch()
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)


@pytest.mark.parametrize('denied_signal', [0, signal.SIGINT], ids=['existence-query', 'termination'])
def test_cli_interrupt_distinguishes_query_denial_from_termination_denial(streaming_runner, denied_signal):
    root, control, start = streaming_runner
    hooks = root.parent / 'query-denial'
    hooks.mkdir()
    (hooks / 'sitecustomize.py').write_text('''import errno, json, os, signal
original = os.killpg
def traced(group, signum):
    with open(os.environ['GROUP_QUERY_TRACE'], 'a') as stream:
        stream.write(json.dumps({'group': group, 'signal': signum}) + '\\n')
    if signum == int(os.environ['GROUP_DENIED_SIGNAL']):
        raise PermissionError(errno.EPERM, 'Controlled group-signal denial')
    return original(group, signum)
os.killpg = traced
''')
    trace = control / 'group-query.jsonl'
    process = start(PYTHONPATH=str(hooks), GROUP_QUERY_TRACE=str(trace),
                    GROUP_DENIED_SIGNAL=str(int(denied_signal)))
    child_pid = None
    try:
        wait_until(lambda: (control / 'child-pid').exists())
        child_pid = int((control / 'child-pid').read_text())
        wait_until(lambda: running_receipt(root))
        receipt_path, receipt = running_receipt(root)
        log = receipt_path.parent / receipt['steps'][0]['log']
        wait_until(lambda: log.exists() and b'visible before completion' in log.read_bytes())
        process.send_signal(signal.SIGINT)
        process.communicate(timeout=15)
        records = [json.loads(line) for line in trace.read_text().splitlines()]
        assert all(item['group'] == child_pid for item in records)
        closed = json.loads(receipt_path.read_text())
        assert closed['passed'] is False and len(closed['steps']) == 1
        if denied_signal == 0:
            assert process.returncode == 130
            with pytest.raises(ProcessLookupError):
                os.kill(child_pid, 0)
            assert 0 in [item['signal'] for item in records]
            assert signal.SIGTERM in [item['signal'] for item in records]
            assert closed['status'] == closed['steps'][0]['status'] == 'interrupted'
            assert closed['returncode'] == 130
        else:
            assert process.returncode == closed['returncode'] == 127
            assert closed['status'] == closed['steps'][0]['status'] == 'failed'
            assert closed['steps'][0]['error']['type'] == 'PermissionError'
            assert [item['signal'] for item in records] == [signal.SIGINT]
        assert closed['steps'][0]['finished_at'] and closed['steps'][0]['seconds'] >= 0
        assert b'visible before completion' in log.read_bytes()
    finally:
        (control / 'release').touch()
        if denied_signal and child_pid is not None:
            # The injected denial deliberately prevents the runner's signal.
            # Teardown owns only this controlled child's newly created group.
            try:
                os.killpg(child_pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)


def test_cli_interrupt_stops_own_descendants_but_keeps_unrelated_process(streaming_runner):
    root, control, start = streaming_runner
    descendant_program = '''import os, pathlib, signal, time
control = pathlib.Path(os.environ['CONTROL'])
def interrupted(signum, frame):
    (control / 'descendant-interrupted').touch()
def terminated(signum, frame):
    (control / 'descendant-terminated').touch()
    raise SystemExit(0)
signal.signal(signal.SIGINT, interrupted)
signal.signal(signal.SIGTERM, terminated)
(control / 'descendant-pid').write_text(str(os.getpid()))
while True:
    time.sleep(0.01)
'''
    executable(root.parent / 'bin/uv', f'''#!/usr/bin/env python3
import os, pathlib, signal, subprocess, sys
control = pathlib.Path(os.environ['CONTROL'])
signal.signal(signal.SIGINT, lambda *_: (control / 'child-interrupted').touch())
signal.signal(signal.SIGTERM, lambda *_: (control / 'child-terminated').touch())
descendant = subprocess.Popen([sys.executable, '-c', {descendant_program!r}])
(control / 'child-pid').write_text(str(os.getpid()))
print('owned subtree ready', flush=True)
sys.exit(descendant.wait())
''')
    unrelated = subprocess.Popen([sys.executable, '-c',
        'import pathlib,sys,time; pathlib.Path(sys.argv[1]).touch(); time.sleep(30)',
        str(control / 'unrelated-ready')], start_new_session=True)
    process = start()
    try:
        wait_until(lambda: (control / 'unrelated-ready').exists())
        wait_until(lambda: (control / 'child-pid').exists() and (control / 'descendant-pid').exists())
        child_pid = int((control / 'child-pid').read_text())
        descendant_pid = int((control / 'descendant-pid').read_text())
        process.send_signal(signal.SIGINT)
        process.communicate(timeout=14)
        assert process.returncode == 130
        assert (control / 'child-interrupted').exists()
        assert (control / 'descendant-interrupted').exists()
        assert (control / 'descendant-terminated').exists()
        for pid in (child_pid, descendant_pid):
            with pytest.raises(ProcessLookupError):
                os.kill(pid, 0)
        assert unrelated.poll() is None
        _, receipt = running_receipt(root)
        assert receipt['status'] == 'interrupted' and receipt['passed'] is False
        assert receipt['returncode'] == 130 and len(receipt['steps']) == 1
        assert receipt['steps'][0]['status'] == 'interrupted' and receipt['steps'][0]['returncode'] == 130
        assert receipt['steps'][0]['finished_at'] >= receipt['steps'][0]['started_at']
        assert isinstance(receipt['steps'][0]['seconds'], float) and receipt['steps'][0]['seconds'] >= 0
    finally:
        if (control / 'child-pid').exists():
            try:
                os.killpg(int((control / 'child-pid').read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)
        unrelated.terminate()
        unrelated.wait(timeout=5)


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


def test_remote_workflow_runs_checks_on_uv_managed_python():
    # actions/setup-python on macOS provides python.org framework builds without
    # sqlite3 enable_load_extension; the SQL executor then fails closed on every
    # query (run 40839825871). The test venv must use uv-managed CPython instead.
    workflow = (ROOT / '.github/workflows/tests.yml').read_text()
    assert 'UV_PYTHON_PREFERENCE: only-managed' in workflow
    assert 'UV_PYTHON: ${{ matrix.python }}' in workflow
    install = workflow.index('uv python install ${{ matrix.python }}')
    assert install < workflow.index('uv sync --locked --extra dev')

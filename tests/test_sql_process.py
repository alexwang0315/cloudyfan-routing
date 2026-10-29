"""Real disposable processes; synthetic faults, never a model or network call."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest

from cloudyfan_jev import sql_process as worker
from cloudyfan_jev.sql import SqlValidationUnavailable, run_sql
from test_sql_environment import answer, contract, sql_core
from test_engine import API


def test_suite_interpreter_exposes_sqlite_extension_control():
    # Builds without loadable-extension support omit this API; the executor would
    # then refuse every query and surface only as SqlValidationUnavailable.
    import sqlite3
    assert hasattr(sqlite3.Connection, 'enable_load_extension'), (
        'This Python lacks sqlite3 enable_load_extension; use uv-managed CPython.')


def execute():
    return worker.execute({'t': [['n', 'INTEGER']]}, ['n'], {'t': [{'n': 7}]}, 'SELECT n FROM t')


def test_real_worker_has_different_pid_no_credentials_and_verified_resources(monkeypatch):
    monkeypatch.setenv('CLOUDYFAN_TEST_PRIVATE_CREDENTIAL', 'synthetic-test-value')
    result, resources = execute()
    assert result == [[[7]], None, {}]
    assert resources['pid'] != os.getpid()
    assert 'CLOUDYFAN_TEST_PRIVATE_CREDENTIAL' not in resources['inherited_environment_keys']
    assert resources['cpu_seconds'] == 2 and resources['sqlite_heap_bytes'] == 33554432
    assert resources['core_bytes'] == resources['file_bytes'] == 0
    with pytest.raises(ProcessLookupError):
        os.kill(resources['pid'], 0)


def substitute(monkeypatch, program):
    native = subprocess.Popen
    children = []

    def popen(command, **kwargs):
        process = native([sys.executable, '-I', '-S', '-B', '-c', program], **kwargs)
        children.append(process)
        return process

    monkeypatch.setattr(worker.subprocess, 'Popen', popen)
    return children


@pytest.mark.parametrize('program,code', [
    ('import time; time.sleep(5)', 'SQL_VALIDATION_TIMEOUT'),
    ('import os; os._exit(7)', None),
    ('print("PRIVATE BROKEN PROTOCOL")', None),
    ('import sys;sys.stdout.write("x"*300000)', 'SQL_VALIDATION_WORKER_OUTPUT'),
    ('import os,signal;os.kill(os.getpid(),signal.SIGKILL)', None),
])
def test_faults_are_bounded_redacted_and_child_is_reaped(monkeypatch, program, code):
    children = substitute(monkeypatch, program)
    monkeypatch.setattr(worker, 'WALL_SECONDS', .3)
    start = time.monotonic()
    with pytest.raises(worker.WorkerUnavailable) as caught:
        execute()
    assert time.monotonic() - start < 2
    if code:
        assert caught.value.code == code
    assert 'PRIVATE' not in str(caught.value)
    assert children and all(p.poll() is not None for p in children)


def test_keyboard_interrupt_reaps_child(monkeypatch):
    children = substitute(monkeypatch, 'import time;time.sleep(5)')

    def interrupt(*args):
        raise KeyboardInterrupt

    monkeypatch.setattr(worker, '_exchange', interrupt)
    with pytest.raises(KeyboardInterrupt):
        execute()
    assert children[0].poll() is not None


def test_oversized_input_stops_before_spawn(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('must reject before process creation')
    monkeypatch.setattr(worker.subprocess, 'Popen', forbidden)
    with pytest.raises(worker.WorkerUnavailable, match='unavailable'):
        worker.execute({}, [], {}, 'x' * worker.MAX_REQUEST_BYTES)


def resource_probe(code):
    path = Path(worker.__file__).resolve().with_name('sql_worker.py')
    bootstrap = ('import importlib.util\ns=importlib.util.spec_from_file_location("w",'
                 + repr(str(path)) + ')\nw=importlib.util.module_from_spec(s);s.loader.exec_module(w)\n'
                 'w.limits(False)\n')
    return subprocess.run([sys.executable, '-I', '-S', '-B', '-c', bootstrap + code],
                          env={}, capture_output=True, timeout=6)


def test_actual_sqlite_allocator_refuses_above_heap_cap():
    result = resource_probe('''import sqlite3,json
with sqlite3.connect(':memory:') as c:
 try:
  c.execute('SELECT length(randomblob(40000000))').fetchone()
  print('NOT_ENFORCED')
 except (MemoryError,sqlite3.Error):
  print('ALLOCATION_REFUSED')
''')
    assert result.returncode == 0 and result.stdout.strip() == b'ALLOCATION_REFUSED'


def test_actual_cpu_limit_terminates_runaway_worker():
    result = resource_probe('while True: pass\n')
    assert result.returncode in {-signal.SIGKILL, -signal.SIGXCPU}


def test_actual_file_limit_refuses_regular_file_data(tmp_path):
    target = tmp_path / 'worker-file-cap-test'
    result = resource_probe('''import os,signal
signal.signal(signal.SIGXFSZ,signal.SIG_IGN)
try:
 with open(''' + repr(str(target)) + ''','wb',buffering=0) as f:
  f.write(b'x')
 print('NOT_ENFORCED')
except OSError:print('FILE_WRITE_REFUSED')
''')
    assert result.returncode == 0 and result.stdout.strip() == b'FILE_WRITE_REFUSED'
    assert not target.exists() or target.stat().st_size == 0


def test_child_does_not_inherit_open_parent_descriptor(tmp_path, monkeypatch):
    with (tmp_path / 'private-parent-descriptor').open('wb') as private:
        os.set_inheritable(private.fileno(), True)
        response = {'protocol': worker.VERSION, 'status': 'ok', 'result': [[], None, {}],
                    'resources': {'cpu_seconds': 2, 'sqlite_heap_bytes': 33554432,
                                  'file_bytes': 0, 'core_bytes': 0, 'open_files': 32,
                                  'python_identity': worker.constraints()['python_identity'],
                                  'sqlite_constraints': worker.sql_executor._execution_constraints()}}
        program = ('import os,json\nr=' + repr(response)
                   + '\ntry:\n os.fstat(' + str(private.fileno()) + ')'
                   + '\n r["resources"]["fd_closed"]=False'
                   + '\nexcept OSError:r["resources"]["fd_closed"]=True'
                   + '\nprint(json.dumps(r))')
        children = substitute(monkeypatch, program)
        _, resources = execute()
        assert resources['fd_closed'] and children[0].returncode == 0


def test_strict_total_memory_policy_is_enforced_or_fails_before_model_post(tmp_path):
    task = contract(require_address_space=True)
    api = API(messages=[answer('SELECT SUM(n) AS total FROM numbers')])
    try:
        task.environment()
    except SqlValidationUnavailable:
        with pytest.raises(SqlValidationUnavailable):
            run_sql(sql_core(tmp_path, api), 'strict-memory', task)
        assert not api.requests
    else:
        assert run_sql(sql_core(tmp_path, api), 'strict-memory', task)['status'] == 'passed'


def test_policy_change_rejects_reuse_and_is_visible_in_public_contract(tmp_path):
    task = contract()
    stricter = contract(require_address_space=True)
    assert task.contract_version != stricter.contract_version
    assert 'require_address_space' in task.task()['prompt']


def test_actual_worker_timeout_retains_answer_and_budget_without_resend(tmp_path, monkeypatch):
    # Real kill/reap after the answer, not a fabricated validator return value.
    from datetime import datetime, timezone
    from cloudyfan_jev.budget import Authorization, BudgetStore, BudgetedCore, EnforcedBound
    native = subprocess.Popen
    interrupted = False
    children = []
    api = API(messages=[answer('SELECT SUM(n) AS total FROM numbers')])

    def endpoint(request):
        nonlocal interrupted
        response = api(request)
        interrupted = True
        return response

    def popen(command, **kwargs):
        if interrupted:
            command = [sys.executable, '-I', '-S', '-c', 'import time;time.sleep(5)']
        process = native(command, **kwargs)
        children.append(process)
        return process

    monkeypatch.setattr(worker.subprocess, 'Popen', popen)
    monkeypatch.setattr(worker, 'WALL_SECONDS', .5)
    task = contract()
    engine = sql_core(tmp_path / 'client', endpoint)
    store = BudgetStore(tmp_path / 'budget.sqlite', clock=lambda: datetime(2026, 10, 18, tzinfo=timezone.utc))
    store.authorize(Authorization('offline-only-fixture', '1', '2026-10-19T00:00:00+00:00'))
    bounds = {p['model_version']: EnforcedBound('.03', 'synthetic') for p in engine.profiles.values()}
    bounds[engine.decision['version']] = EnforcedBound('.001', 'synthetic')
    controlled = BudgetedCore(engine, store, model_bounds=bounds, tool_bounds={}, verify_bound=lambda *a: True)
    with pytest.raises(SqlValidationUnavailable):
        run_sql(controlled, 'worker-timeout', task)
    assert store.report()['known_usd'] == '.01' or store.report()['known_usd'] == '0.01'
    assert store.report()['reserved_usd'] != '0'
    assert len(api.requests) == 1 and all(p.poll() is not None for p in children)
    interrupted = False
    result = run_sql(controlled, 'worker-timeout', task)
    assert result['status'] == 'passed' and result['resumed']
    assert len(api.requests) == 1 and store.report()['reserved_usd'] == '0'


def test_resource_protocol_cannot_silently_omit_or_downgrade_caps(monkeypatch):
    response = {'protocol': worker.VERSION, 'status': 'ok', 'result': [[[7]], None, {}],
                'resources': {'sqlite_heap_bytes': 0}}
    substitute(monkeypatch, 'print(' + repr(json.dumps(response)) + ')')
    with pytest.raises(worker.WorkerUnavailable):
        execute()


def test_worker_cannot_validate_against_a_different_sqlite_runtime(monkeypatch):
    resources = {'cpu_seconds': 2, 'sqlite_heap_bytes': 33554432, 'file_bytes': 0,
                 'core_bytes': 0, 'open_files': 32,
                 'python_identity': worker.constraints()['python_identity'],
                 'sqlite_constraints': dict(worker.sql_executor._execution_constraints(), sqlite_version='OTHER')}
    response = {'protocol': worker.VERSION, 'status': 'ok', 'result': [[[7]], None, {}],
                'resources': resources}
    substitute(monkeypatch, 'print(' + repr(json.dumps(response)) + ')')
    with pytest.raises(worker.WorkerUnavailable):
        execute()

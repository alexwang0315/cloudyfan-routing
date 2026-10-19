"""Strict SQL isolation at Core.run and the external OS-launch boundary."""
import platform
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import json
import subprocess
import sys

import pytest

from cloudyfan_jev.sql import SqlValidationUnavailable, run_sql
from test_engine import API
from test_sql_environment import answer, sql_core
from cloudyfan_jev.sql_isolation import CGROUP_LIMITS, IsolationUnavailable, LinuxIsolation


def policy(**changes):
    values = dict(rootfs='/not-configured/rootfs', rootfs_sha256='1'*64,
                  bwrap='/not-configured/bwrap', bwrap_sha256='2'*64,
                  cgroup='/not-configured/cgroup', kernel=platform.release(), machine=platform.machine())
    values.update(changes)
    return LinuxIsolation(**values)


def test_strict_linux_isolation_never_falls_back_before_dispatch(tmp_path):
    from cloudyfan_jev.sql_catalog import contract
    api = API(messages=[answer('SELECT SUM(amount) AS total FROM orders')])
    with pytest.raises(SqlValidationUnavailable):
        run_sql(sql_core(tmp_path, api), 'strict-unavailable', contract('S01', isolation=policy()))
    assert not api.requests


@pytest.mark.parametrize('field,value', [('rootfs','/'),('rootfs','/x/../y'),('cgroup','relative'),
                                        ('bwrap_sha256','not-a-hash'),('kernel','')])
def test_isolation_configuration_cannot_accept_ambiguous_paths_or_unpinned_artifacts(field,value):
    with pytest.raises(ValueError):
        policy(**{field:value})


def test_launcher_contract_has_no_optional_namespace_or_host_root_fallback():
    command = policy().command()
    assert '--unshare-user' in command and '--unshare-net' in command
    assert '--unshare-cgroup' in command and '--disable-userns' in command
    assert not any('try' in s for s in command)
    assert command[command.index('--ro-bind')+1:command.index('--ro-bind')+3] == ['/not-configured/rootfs','/']
    assert '--clearenv' in command and '--cap-drop' in command


def fake_leaf(tmp_path):
    # OS/filesystem fixture ONLY, never Linux qualification evidence.
    leaf = tmp_path/'delegated-leaf'
    leaf.mkdir()
    for k,v in {**CGROUP_LIMITS,'cgroup.procs':'','cgroup.events':'populated 0','cgroup.kill':''}.items():
        (leaf/k).write_text(v)
    return leaf


@pytest.mark.parametrize('fault', ['limits','busy','occupied-descendant'])
def test_os_leaf_preflight_rejects_before_model_dispatch(tmp_path,monkeypatch,fault):
    from cloudyfan_jev.sql_catalog import contract
    leaf = fake_leaf(tmp_path)
    if fault == 'limits': (leaf/'memory.max').write_text('max')
    if fault == 'busy': (leaf/'cgroup.procs').write_text('123')
    if fault == 'occupied-descendant': (leaf/'cgroup.events').write_text('populated 1')
    monkeypatch.setattr(LinuxIsolation, '_artifacts', lambda self: leaf)
    api=API()
    with pytest.raises(SqlValidationUnavailable):
        run_sql(sql_core(tmp_path,api),'bad-leaf',contract('S01',isolation=policy(cgroup=str(leaf))))
    assert not api.requests and (leaf/'cgroup.kill').read_text() == ''


def test_exclusive_leaf_lease_refuses_concurrent_worker_and_releases_after_error(tmp_path,monkeypatch):
    leaf=fake_leaf(tmp_path)
    monkeypatch.setattr(LinuxIsolation,'_artifacts',lambda self:leaf)
    configured=policy(cgroup=str(leaf))
    with pytest.raises(RuntimeError,match='simulated interruption'):
        with configured.lease():
            with pytest.raises(IsolationUnavailable):
                with configured.lease():
                    pytest.fail('A second worker must not enter the leaf')
            raise RuntimeError('simulated interruption')
    assert (leaf/'cgroup.kill').read_text() == '1'
    with configured.lease() as command:
        assert 'sql_isolation_entry.py' in command[4]


@pytest.mark.parametrize('fault', ['after-answer-unavailable','cancelled'])
def test_strict_worker_interruption_preserves_answer_budget_and_does_not_resend(tmp_path,monkeypatch,fault):
    from cloudyfan_jev.budget import Authorization,BudgetStore,BudgetedCore,EnforcedBound
    from cloudyfan_jev.sql_catalog import contract
    # Inject an external launcher boundary. It executes the real portable worker
    # on this OS; this verifies orchestration, NOT actual Linux containment.
    import sys
    import cloudyfan_jev.sql_worker as worker
    active=False
    @contextmanager
    def launcher(self):
        if active:
            if fault=='cancelled': raise KeyboardInterrupt()
            raise IsolationUnavailable('SQL_ISOLATION_UNAVAILABLE')
        yield [sys.executable,'-I','-S','-B',str(Path(worker.__file__))]
    monkeypatch.setattr(LinuxIsolation,'lease',launcher)
    api=API(messages=[answer('SELECT SUM(amount) AS total FROM orders')])
    def endpoint(request):
        nonlocal active
        response=api(request)
        active=True
        return response
    engine=sql_core(tmp_path/'client',endpoint)
    store=BudgetStore(tmp_path/'budget.sqlite',clock=lambda:datetime(2026,10,18,tzinfo=timezone.utc))
    store.authorize(Authorization('synthetic-fixture','1','2026-10-19T00:00:00+00:00'))
    bounds={p['model_version']:EnforcedBound('.03','synthetic') for p in engine.profiles.values()}
    bounds[engine.decision['version']]=EnforcedBound('.001','synthetic')
    controlled=BudgetedCore(engine,store,model_bounds=bounds,tool_bounds={},verify_bound=lambda *a:True)
    task=contract('S01',isolation=policy())
    exception=KeyboardInterrupt if fault=='cancelled' else SqlValidationUnavailable
    with pytest.raises(exception): run_sql(controlled,'strict-resume',task)
    assert len(api.requests)==1 and store.report()['known_usd']=='0.01'
    assert store.report()['reserved_usd']!='0'
    active=False
    result=run_sql(controlled,'strict-resume',task)
    assert result['status']=='passed' and result['resumed'] and len(api.requests)==1
    assert store.report()['reserved_usd']=='0'


def test_changed_strict_policy_cannot_reuse_a_saved_run(tmp_path,monkeypatch):
    import sys
    import cloudyfan_jev.sql_worker as worker
    from cloudyfan_jev.sql_catalog import contract
    @contextmanager
    def launcher(self):
        yield [sys.executable,'-I','-S','-B',str(Path(worker.__file__))]
    monkeypatch.setattr(LinuxIsolation,'lease',launcher)
    configured=policy()
    api=API(messages=[answer('SELECT SUM(amount) AS total FROM orders')])
    engine=sql_core(tmp_path,api)
    assert run_sql(engine,'strict-frozen',contract('S01',isolation=configured))['status']=='passed'
    with pytest.raises(ValueError,match='Resume evidence changed'):
        run_sql(engine,'strict-frozen',contract('S01',isolation=replace(configured,rootfs_sha256='3'*64)))
    assert len(api.requests)==1


def test_trampoline_refuses_changed_binary_before_running_it(tmp_path):
    import sys
    import cloudyfan_jev.sql_isolation_entry as entry
    binary=tmp_path/'not-the-pinned-binary'
    binary.write_text('untrusted')
    result=subprocess.run([sys.executable,'-I','-S','-B',entry.__file__,str(tmp_path),'0'*64,
                           json.dumps([str(binary)])],capture_output=True)
    assert result.returncode!=0 and b'Isolation artifact changed' in result.stderr


@pytest.mark.parametrize('field', ['version','implementation','cache_tag','missing'])
def test_child_python_identity_mismatch_is_refused_before_model_dispatch(tmp_path,monkeypatch,field):
    import cloudyfan_jev.sql_worker as worker
    from cloudyfan_jev.sql_catalog import contract
    wrapper=tmp_path/'different-runtime.py'
    mutation = ('del p["resources"]["python_identity"]' if field=='missing' else
                f'p["resources"]["python_identity"][{field!r}]="different-runtime"')
    wrapper.write_text('import json, subprocess, sys\n'
        f'r=subprocess.run([sys.executable,"-I","-S","-B",{worker.__file__!r}],'
        'input=sys.stdin.buffer.read(),capture_output=True)\n'
        'p=json.loads(r.stdout)\n'+mutation+'\n'
        'print(json.dumps(p))\n')
    native=subprocess.Popen
    def launch(command,**kwargs):
        return native([sys.executable,'-I','-S','-B',str(wrapper)],**kwargs)
    monkeypatch.setattr(subprocess,'Popen',launch)
    api=API(messages=[answer('SELECT SUM(amount) AS total FROM orders')])
    with pytest.raises(SqlValidationUnavailable) as caught:
        run_sql(sql_core(tmp_path/'client',api),'wrong-python',contract('S01'))
    assert caught.value.code == 'SQL_VALIDATION_WORKER_PROTOCOL'
    assert not api.requests

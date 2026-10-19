"""Qualification evidence must preserve blocked/partial states, never fabricate a pass."""
import json
from pathlib import Path
import subprocess
import sys


def test_read_only_target_inspection_cannot_mark_rg01_qualified():
    result = subprocess.run([sys.executable,'scripts/qualify_sql_worker.py'],capture_output=True,text=True)
    assert result.returncode == 2
    report=json.loads(result.stdout)
    assert report['formal_qualification']=='pending'
    assert report['result'] in ('blocked','inspection_only')
    assert report['real_api_calls']==0 and report['processes_launched']==0
    assert all(c['result']=='not_run' for c in report['required_target_exercises'])


def test_exercise_requires_explicit_target_and_authorization_reference():
    result=subprocess.run([sys.executable,'scripts/qualify_sql_worker.py','--exercise'],capture_output=True,text=True)
    assert result.returncode!=0
    assert 'policy' in result.stderr and 'authorization' in result.stderr


def test_ticket_status_never_substitutes_for_formal_registry():
    path=Path('docs/release-gates.json')
    before=path.read_bytes()
    result=subprocess.run([sys.executable,'scripts/qualify_sql_worker.py'],capture_output=True,text=True)
    assert result.returncode==2
    assert path.read_bytes()==before


def test_adversarial_evidence_requires_explicit_exercise_and_new_output():
    result = subprocess.run([sys.executable, 'scripts/qualify_sql_worker.py', '--adversarial'],
                            capture_output=True, text=True)
    assert result.returncode != 0
    assert 'requires --exercise' in result.stderr


def test_failed_target_admission_does_not_write_into_existing_output(tmp_path):
    import platform
    policy = tmp_path/'policy.json'
    policy.write_text(json.dumps({'rootfs': '/not-configured/rootfs', 'rootfs_sha256': '1'*64,
        'bwrap': '/not-configured/bwrap', 'bwrap_sha256': '2'*64,
        'cgroup': '/not-configured/cgroup', 'kernel': platform.release(), 'machine': platform.machine()}))
    output = tmp_path/'existing-evidence'
    output.mkdir()
    sentinel = output/'original'
    sentinel.write_text('preserve me')
    result = subprocess.run([sys.executable, 'scripts/qualify_sql_worker.py', '--exercise', '--adversarial',
        '--policy', str(policy), '--authorization-ref', 'synthetic-test-only', '--output', str(output)],
        capture_output=True, text=True)
    assert result.returncode == 2
    assert list(output.iterdir()) == [sentinel]
    assert sentinel.read_text() == 'preserve me'


def test_external_interrupt_is_not_evidence_of_requested_worker_cancellation(tmp_path, monkeypatch):
    from contextlib import contextmanager
    import pytest
    monkeypatch.syspath_prepend(str(Path("scripts").resolve()))
    from sql_target_exercises import probe
    leaf = tmp_path/'fake-leaf'
    leaf.mkdir()
    (leaf/'cgroup.procs').write_text('')
    (leaf/'cgroup.events').write_text('populated 0\n')

    class Policy:
        cgroup = str(leaf)

        def command(self):
            return ['fake-bwrap', '/app/sql_worker.py']

        @contextmanager
        def lease(self):
            yield ['fake-trampoline', json.dumps(self.command())]

    def interrupted_launch(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(subprocess, 'Popen', interrupted_launch)
    with pytest.raises(KeyboardInterrupt):
        probe(Policy(), 'trusted-code-not-executed', cancel=True)


def test_normal_qualification_flow_checks_identity_and_reuses_all_eight_answers(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(Path('scripts').resolve()))
    from qualify_sql_worker import mock_normal_flow
    # Portable real SQL worker only. The same Core.run/MockTransport identity
    # contract must work before the separate Linux envelope is exercised.
    report = mock_normal_flow(None, tmp_path)
    assert report['passed']
    assert report['mock_posts'] == 8
    assert [row['id'] for row in report['cases']] == ['S01','S02','S03','S04','S05','S06','S07','S08']

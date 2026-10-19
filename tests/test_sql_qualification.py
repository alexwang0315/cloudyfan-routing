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

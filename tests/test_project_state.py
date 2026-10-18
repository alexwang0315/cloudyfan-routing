"""Handoff records distinguish historical verification from current readiness."""

import hashlib
import json
from pathlib import Path

import pytest

from scripts.project_state import check_bundle, source_digest


ROOT = Path(__file__).resolve().parents[1]


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + '\n')


def read(root, name='docs/agent-state.json'):
    return json.loads((root / name).read_text())


@pytest.fixture
def checkout(tmp_path):
    for name in ['docs/agent-state.json', 'docs/release-gates.json', 'docs/work-plan.json',
                 'docs/evidence/retro12-offline.json']:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((ROOT / name).read_bytes())
    state = read(tmp_path)
    state.update(phase='in_progress', current_verification=None)
    write(tmp_path / 'docs/agent-state.json', state)
    plan = read(tmp_path, 'docs/work-plan.json')
    for task in plan['tasks']:
        task['status'] = 'in_progress'
    write(tmp_path / 'docs/work-plan.json', plan)
    for interface in plan['shared_interfaces']:
        if interface['provided_by'] is None:
            for name in interface['owned_files']:
                path = tmp_path / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('# synthetic baseline interface\n')
    for item in state['history']:
        path = tmp_path / item['path']
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('Historical: old work is pending review.\n')
    return tmp_path


def complete(root):
    state = read(root)
    plan = read(root, 'docs/work-plan.json')
    for task in plan['tasks']:
        task['status'] = 'completed'
    write(root / 'docs/work-plan.json', plan)
    evidence = {'schema_version': 1, 'kind': 'offline', 'result': 'passed', 'tests_passed': 4,
                'scope': state['scope'], 'source_digest': source_digest(root)}
    path = root / 'docs/evidence/current.json'
    write(path, evidence)
    state.update(phase='offline_verified', current_verification={
        'path': 'docs/evidence/current.json', 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
    write(root / 'docs/agent-state.json', state)


def test_historical_pass_and_closed_work_never_imply_current_or_production_completion(checkout):
    report = check_bundle(checkout)
    assert report['valid'], report['errors']
    assert not report['implementation_complete']
    assert not report['production_qualification_recorded']
    assert len(report['pending_gates']) == 10


def test_completed_offline_record_does_not_qualify_production(checkout):
    complete(checkout)
    report = check_bundle(checkout)
    assert report['valid'], report['errors']
    assert report['implementation_complete'] is True
    assert report['production_qualification_recorded'] is False
    assert report['next_steps'][0]['gate'] == 'G-02'


def test_changed_code_invalidates_current_evidence(checkout):
    complete(checkout)
    path = checkout / 'src/new_behavior.py'
    path.write_text('CHANGED = True\n')
    result = check_bundle(checkout)
    assert not result['valid'] and not result['implementation_complete']
    assert any('stale' in error for error in result['errors'])


def test_old_pending_prose_is_history_not_the_current_status(checkout):
    complete(checkout)
    (checkout / 'docs/verification.md').write_text('Historical: OPEN, pending review, not implemented.\n')
    assert check_bundle(checkout)['implementation_complete'] is True


@pytest.mark.parametrize('field,value', [('production_qualified', True), ('issue_closed', True)])
def test_manual_success_flags_are_not_an_authority(checkout, field, value):
    state = read(checkout)
    state[field] = value
    write(checkout / 'docs/agent-state.json', state)
    assert check_bundle(checkout)['valid'] is False


@pytest.mark.parametrize('field', ['paid_api', 'deployment', 'source_push', 'hardware', 'model_downloads'])
def test_state_cannot_expand_offline_authorization(checkout, field):
    state = read(checkout)
    state['constraints'][field] = True
    write(checkout / 'docs/agent-state.json', state)
    assert check_bundle(checkout)['valid'] is False


@pytest.mark.parametrize('mutation', ['hash', 'missing', 'traversal', 'symlink', 'commit'])
def test_evidence_must_resolve_to_the_recorded_local_baseline(checkout, mutation):
    state = read(checkout)
    if mutation == 'hash':
        state['last_verified']['sha256'] = '0' * 64
    elif mutation == 'missing':
        state['last_verified']['path'] = 'docs/missing.json'
    elif mutation == 'traversal':
        state['last_verified']['path'] = '../escape.json'
    elif mutation == 'symlink':
        other = checkout.parent / 'outside-evidence.json'
        other.write_bytes((checkout / state['last_verified']['path']).read_bytes())
        (checkout / 'docs/escape.json').symlink_to(other)
        state['last_verified']['path'] = 'docs/escape.json'
    else:
        state['baseline_commit'] = '0' * 40
    write(checkout / 'docs/agent-state.json', state)
    assert check_bundle(checkout)['valid'] is False


def test_incomplete_plan_prevents_completed_handoff(checkout):
    complete(checkout)
    plan = read(checkout, 'docs/work-plan.json')
    plan['tasks'][0]['status'] = 'in_progress'
    write(checkout / 'docs/work-plan.json', plan)
    result = check_bundle(checkout)
    assert not result['valid'] and not result['implementation_complete']


def test_invalid_release_record_blocks_aggregate(checkout):
    gates = read(checkout, 'docs/release-gates.json')
    for gate in gates['gates']:
        gate['status'] = 'qualified'
    write(checkout / 'docs/release-gates.json', gates)
    result = check_bundle(checkout)
    assert not result['valid'] and not result['production_qualification_recorded']


@pytest.mark.parametrize('text', ['[]', '{', '{"schema_version":1,"schema_version":2}', 'null'])
def test_bad_state_returns_diagnostic_without_crashing(checkout, text):
    (checkout / 'docs/agent-state.json').write_text(text)
    result = check_bundle(checkout)
    assert not result['valid'] and result['errors']


@pytest.mark.parametrize('name', ['docs/release-gates.json', 'docs/work-plan.json'])
@pytest.mark.parametrize('value', [[], None, 1, 'invalid'])
def test_malformed_registry_returns_diagnostic(checkout, name, value):
    write(checkout / name, value)
    report = check_bundle(checkout)
    assert not report['valid'] and report['errors']
    assert not report['implementation_complete']
    assert not report['production_qualification_recorded']


def test_boolean_schema_is_not_a_version(checkout):
    state = read(checkout)
    state['schema_version'] = True
    write(checkout / 'docs/agent-state.json', state)
    assert not check_bundle(checkout)['valid']

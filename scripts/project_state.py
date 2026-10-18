"""Read-only handoff and release-record consistency checks; never grants authority."""

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys

if __package__:
    from .release_gates import production_ready, validate_gates
    from .work_plan import validate_plan
else:
    from release_gates import production_ready, validate_gates
    from work_plan import validate_plan


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f'duplicate JSON key: {key}')
        result[key] = value
    return result


def local_file(root, name):
    if not isinstance(name, str) or not name or Path(name).is_absolute() or '..' in Path(name).parts:
        raise ValueError('reference must be a confined project-relative file')
    path = (root / name).resolve()
    if not path.is_relative_to(root.resolve()) or not path.is_file():
        raise ValueError(f'missing or escaping reference: {name}')
    return path


def read_json(path):
    return json.loads(path.read_text(), object_pairs_hook=unique_object)


def source_digest(root):
    """Fingerprint executable/test/build inputs, excluding mutable status records.

    Metadata is always validated separately. Excluding it avoids a self-reference
    when a completed check is recorded in agent-state.json.
    """
    files = []
    for name in ('src', 'tests', 'scripts', '.githooks', '.github'):
        directory = root / name
        if directory.is_dir():
            files.extend(p for p in directory.rglob('*') if p.is_file()
                         and '__pycache__' not in p.parts and p.suffix not in {'.pyc', '.pyo'})
    files.extend(root / name for name in ('pyproject.toml', 'uv.lock') if (root / name).is_file())
    digest = hashlib.sha256()
    for path in sorted(files):
        relative = path.relative_to(root).as_posix()
        local_file(root, relative)
        digest.update(relative.encode() + b'\0' + path.read_bytes() + b'\0')
    return digest.hexdigest()


def evidence_record(root, reference):
    if not isinstance(reference, dict) or set(reference) != {'path', 'sha256'}:
        raise ValueError('evidence reference requires only path and sha256')
    path = local_file(root, reference['path'])
    if hashlib.sha256(path.read_bytes()).hexdigest() != reference['sha256']:
        raise ValueError('evidence hash mismatch')
    evidence = read_json(path)
    if (not isinstance(evidence, dict) or type(evidence.get('schema_version')) is not int
            or evidence.get('schema_version') != 1):
        raise ValueError('unsupported evidence schema')
    if evidence.get('kind') != 'offline' or evidence.get('result') != 'passed':
        raise ValueError('handoff implementation evidence must be an offline pass')
    if type(evidence.get('tests_passed')) is not int or evidence['tests_passed'] <= 0:
        raise ValueError('evidence requires an observed positive test count')
    return evidence


def check_bundle(root, *, now=None):
    root = root.resolve()
    errors = []
    state = {}
    gates = {}
    plan = {}
    complete = False
    try:
        state = read_json(root / 'docs/agent-state.json')
        required = {'schema_version', 'scope', 'phase', 'baseline_commit', 'integration_branch',
                    'constraints', 'release_registry', 'work_plan', 'last_verified',
                    'current_verification', 'history', 'next_steps'}
        if (not isinstance(state, dict) or set(state) != required
                or type(state.get('schema_version')) is not int or state.get('schema_version') != 1):
            raise ValueError('agent-state has unsupported/missing fields; qualification is derived, not a flag')
        if not isinstance(state['scope'], str) or not state['scope'].strip():
            raise ValueError('missing current scope')
        if state['phase'] not in {'in_progress', 'offline_verified', 'blocked'}:
            raise ValueError('unknown phase')
        constraints = state['constraints']
        expected = {'paid_api', 'deployment', 'source_push', 'hardware', 'model_downloads'}
        if not isinstance(constraints, dict) or set(constraints) != expected:
            raise ValueError('missing explicit authorization constraints')
        if any(value is not False for value in constraints.values()):
            raise ValueError('this offline workflow cannot enable external-action authorization')
        if (not isinstance(state['baseline_commit'], str) or len(state['baseline_commit']) != 40
                or any(c not in '0123456789abcdef' for c in state['baseline_commit'])):
            raise ValueError('baseline must be a full commit hash, not a claimed current HEAD')
        if not isinstance(state['integration_branch'], str) or not state['integration_branch'].strip():
            raise ValueError('missing integration branch')
        gates = read_json(local_file(root, state['release_registry']))
        if not isinstance(gates, dict):
            raise ValueError('release registry must be an object')
        gate_errors = validate_gates(gates, root, now=now)
        errors.extend('release: ' + message for message in gate_errors)
        plan = read_json(local_file(root, state['work_plan']))
        if not isinstance(plan, dict):
            raise ValueError('work plan must be an object')
        errors.extend('plan: ' + message for message in validate_plan(plan, root))
        for field in ('baseline_commit', 'integration_branch'):
            if plan.get(field) != state[field]:
                errors.append(f'plan/state {field} mismatch')
        prior = evidence_record(root, state['last_verified'])
        if prior.get('tested_commit') != state['baseline_commit']:
            errors.append('last_verified does not describe the recorded baseline commit')
        if not isinstance(state['history'], list) or not state['history']:
            raise ValueError('history must identify the historical records')
        for item in state['history']:
            if not isinstance(item, dict) or item.get('record_kind') != 'historical':
                raise ValueError('history entries must be marked historical')
            local_file(root, item.get('path'))
        if not isinstance(state['next_steps'], list) or not state['next_steps']:
            raise ValueError('missing next steps')
        ids = {gate['id'] for gate in gates.get('gates', []) if isinstance(gate, dict) and 'id' in gate}
        for step in state['next_steps']:
            if (not isinstance(step, dict) or step.get('gate') not in ids
                    or not isinstance(step.get('action'), str) or not step['action'].strip()
                    or step.get('requires_authorization') is not True):
                raise ValueError('next step must reference a gate and retain its authorization prerequisite')
        current = state['current_verification']
        if state['phase'] == 'offline_verified':
            current = evidence_record(root, current)
            if current.get('scope') != state['scope']:
                errors.append('current verification scope mismatch')
            if current.get('source_digest') != source_digest(root):
                errors.append('current verification is stale for these executable/test/build inputs')
            tasks = plan.get('tasks')
            if (not isinstance(tasks, list) or not tasks
                    or any(not isinstance(task, dict) or task.get('status') != 'completed' for task in tasks)):
                errors.append('offline_verified requires all current work-plan tasks completed')
            complete = not errors
        elif current is not None:
            raise ValueError('unfinished work must not advertise current verification')
    except (ValueError, TypeError, KeyError, OSError, RuntimeError) as exc:
        errors.append(str(exc))
    state = state if isinstance(state, dict) else {}
    gates = gates if isinstance(gates, dict) else {}
    gate_rows = gates.get('gates') if isinstance(gates.get('gates'), list) else []
    return {'valid': not errors, 'errors': errors, 'scope': state.get('scope'), 'phase': state.get('phase'),
            'baseline_commit': state.get('baseline_commit'), 'integration_branch': state.get('integration_branch'),
            'implementation_complete': complete,
            'production_qualification_recorded': not errors and production_ready(gates),
            'pending_gates': [g.get('id') for g in gate_rows
                              if isinstance(g, dict) and g.get('status') == 'pending'],
            'next_steps': state.get('next_steps', []),
            'notice': 'Consistency checks only: no human identity, real-world truth or execution authority is certified.'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', nargs='?', choices=['show', 'check'], default='show')
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[1]
    report = check_bundle(root)
    try:
        git_root = subprocess.check_output(
            ['git', '-C', str(root), 'rev-parse', '--show-toplevel'],
            text=True, stderr=subprocess.DEVNULL).strip()
        if Path(git_root).resolve() != root:
            raise ValueError('enclosing repository is not this checkout')
        report['observed_git_head'] = subprocess.check_output(
            ['git', '-C', str(root), 'rev-parse', 'HEAD'], text=True, stderr=subprocess.DEVNULL).strip()
    except (OSError, ValueError, subprocess.CalledProcessError):
        report['observed_git_head'] = None
    if args.json or args.command == 'show':
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print('Project records ' + ('PASS' if report['valid'] else 'FAIL'))
        print(f"implementation_complete={report['implementation_complete']}; "
              f"production_qualification_recorded={report['production_qualification_recorded']}")
        for error in report['errors']:
            print('- ' + error)
    return 0 if report['valid'] else 1


if __name__ == '__main__':
    sys.exit(main())

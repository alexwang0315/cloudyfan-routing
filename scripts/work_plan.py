"""Validate declared ownership/dependency boundaries; never dispatch or merge work."""
from __future__ import annotations

import argparse
import json
from pathlib import Path, PurePosixPath
import re

ACTIVE = {'planned', 'in_progress', 'blocked'}
STATUSES = ACTIVE | {'completed', 'cancelled'}


def validate_plan(data: dict, root: Path) -> list[str]:
    """Return declaration errors without network, mutation, or Git ancestry claims."""
    errors: list[str] = []
    root = root.resolve()
    if not isinstance(data, dict):
        return ['plan must be an object']
    if type(data.get('schema_version')) is not int or data['schema_version'] != 1:
        errors.append('schema_version must be 1')
    for key in ('integration_branch', 'merger_owner'):
        if not isinstance(data.get(key), str) or not data[key].strip():
            errors.append(f'{key} must be a nonempty string')
    if not isinstance(data.get('baseline_commit'), str) or not re.fullmatch(r'[a-f0-9]{40}', data['baseline_commit']):
        errors.append('baseline_commit must be a full lowercase Git commit ID')

    def strings(value, label):
        if not isinstance(value, list) or any(not isinstance(v, str) or not v.strip() for v in value):
            errors.append(f'{label} must be a list of nonempty strings')
            return []
        if len(value) != len(set(value)):
            errors.append(f'{label} has duplicate entries')
        return value

    def paths(value, label):
        result = []
        for value in strings(value, label):
            path = PurePosixPath(value)
            if (path.is_absolute() or '\\' in value or any(p in ('', '.', '..') for p in value.split('/'))
                    or ':' in value or not (root / value).resolve().is_relative_to(root)):
                errors.append(f'{label}: unsafe project-relative path {value!r}')
            else:
                result.append(value)
        return result

    def records(key):
        value = data.get(key)
        if not isinstance(value, list):
            errors.append(f'{key} must be a list')
            return {}
        result = {}
        for item in value:
            if not isinstance(item, dict) or not isinstance(item.get('id'), str) or not item['id'].strip():
                errors.append(f'{key}: every entry needs an id')
                continue
            ident = item['id']
            if ident in result:
                errors.append(f'{key}: duplicate id {ident}')
                continue
            if not isinstance(item.get('owner'), str) or not item['owner'].strip():
                errors.append(f'{ident}: exactly one accountable owner string required')
            result[ident] = {**item, 'owned_files': paths(item.get('owned_files'), f'{ident}.owned_files')}
            if not result[ident]['owned_files']:
                errors.append(f'{ident}: owned_files must not be empty')
        return result

    tasks = records('tasks')
    interfaces = records('shared_interfaces')
    deps = {}
    for ident, task in tasks.items():
        if not isinstance(task.get('status'), str) or task['status'] not in STATUSES:
            errors.append(f'{ident}: invalid status')
        deps[ident] = strings(task.get('depends_on'), f'{ident}.depends_on')
        task['consumed_interfaces'] = strings(task.get('consumed_interfaces'), f'{ident}.consumed_interfaces')
        for dependency in deps[ident]:
            if dependency not in tasks:
                errors.append(f'{ident}: unknown dependency {dependency}')
        for interface in task['consumed_interfaces']:
            if interface not in interfaces:
                errors.append(f'{ident}: unknown shared interface {interface}')

    def ancestors(ident, stack=()):
        if ident in stack:
            errors.append('dependency cycle: ' + ' -> '.join((*stack, ident)))
            return set()
        found = set()
        for dependency in deps.get(ident, []):
            found.add(dependency)
            found.update(ancestors(dependency, (*stack, ident)))
        return found

    ancestors_by_task = {ident: ancestors(ident) for ident in tasks}

    def overlaps(left, right):
        return left == right or left.startswith(right + '/') or right.startswith(left + '/')

    active = [(ident, task) for ident, task in tasks.items() if isinstance(task.get('status'), str) and task['status'] in ACTIVE]
    for index, (left_id, left) in enumerate(active):
        for right_id, right in active[index + 1:]:
            if left_id in ancestors_by_task[right_id] or right_id in ancestors_by_task[left_id]:
                continue
            if any(overlaps(a, b) for a in left['owned_files'] for b in right['owned_files']):
                errors.append(f'unordered active ownership overlap: {left_id}, {right_id}')

    for ident, interface in interfaces.items():
        provider = interface.get('provided_by')
        if 'provided_by' not in interface:
            errors.append(f'{ident}: provided_by binding required (null means existing baseline)')
        if provider is not None and (not isinstance(provider, str) or provider not in tasks):
            errors.append(f'{ident}: unknown provider task')
            continue
        if provider is None:
            for path in interface['owned_files']:
                if not (root / path).is_file():
                    errors.append(f'{ident}: baseline interface file does not exist: {path}')
        else:
            task = tasks[provider]
            if task.get('owner') != interface.get('owner'):
                errors.append(f'{ident}: provider and interface must have the same accountable owner')
            if not set(interface['owned_files']).issubset(task['owned_files']):
                errors.append(f'{ident}: provider must own all interface files')
        for task_id, task in active:
            changes = any(overlaps(a, b) for a in task['owned_files'] for b in interface['owned_files'])
            if changes and task_id != provider:
                errors.append(f'{task_id}: editing {ident} requires explicit provider binding')
            if ident in task['consumed_interfaces'] and provider and task_id != provider:
                if provider not in ancestors_by_task[task_id]:
                    errors.append(f'{task_id}: consuming {ident} requires dependency on {provider}')
    interface_items = list(interfaces.items())
    for index, (left_id, left) in enumerate(interface_items):
        for right_id, right in interface_items[index + 1:]:
            if left.get('owner') != right.get('owner') and any(
                overlaps(a, b) for a in left['owned_files'] for b in right['owned_files']
            ):
                errors.append(f'conflicting shared-interface owners: {left_id}, {right_id}')
    return list(dict.fromkeys(errors))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--plan', type=Path)
    args = parser.parse_args()
    path = args.plan or args.root / 'docs/work-plan.json'
    try:
        errors = validate_plan(json.loads(path.read_text()), args.root)
    except (OSError, ValueError) as exc:
        errors = [f'cannot read plan: {exc}']
    print(json.dumps({'ok': not errors, 'errors': errors}, ensure_ascii=False))
    return int(bool(errors))


if __name__ == '__main__':
    raise SystemExit(main())

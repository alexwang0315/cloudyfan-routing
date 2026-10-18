"""Declared work ownership is checked offline before dispatch, not inferred from Git."""
import importlib.util
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location('work_plan', Path(__file__).resolve().parents[1] / 'scripts/work_plan.py')
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)
validate_plan = module.validate_plan


@pytest.fixture
def plan(tmp_path):
    (tmp_path / 'core.py').write_text('')
    return {
        'schema_version': 1, 'integration_branch': 'integration',
        'baseline_commit': 'a' * 40, 'merger_owner': 'integrator',
        'shared_interfaces': [{'id': 'core', 'owner': 'maintainer', 'owned_files': ['core.py'], 'provided_by': None}],
        'tasks': [
            {'id': 'a', 'owner': 'alice', 'status': 'in_progress', 'owned_files': ['a.py'],
             'depends_on': [], 'consumed_interfaces': ['core']},
            {'id': 'b', 'owner': 'bob', 'status': 'planned', 'owned_files': ['b.py'],
             'depends_on': [], 'consumed_interfaces': []},
        ],
    }


def test_independent_plan_passes(plan, tmp_path):
    assert validate_plan(plan, tmp_path) == []


def test_overlap_requires_declared_order_even_same_owner(plan, tmp_path):
    plan['tasks'][1]['owned_files'] = ['a.py']
    plan['tasks'][1]['owner'] = 'alice'
    assert any('overlap' in e for e in validate_plan(plan, tmp_path))
    plan['tasks'][1]['depends_on'] = ['a']
    assert validate_plan(plan, tmp_path) == []


@pytest.mark.parametrize('path', ['../escape', '/absolute', 'dir/../escape', 'dir\\escape', '.', 'dir//file'])
def test_unsafe_paths_rejected(plan, tmp_path, path):
    plan['tasks'][0]['owned_files'] = [path]
    assert any('unsafe' in e for e in validate_plan(plan, tmp_path))


def test_symlink_escape_rejected(plan, tmp_path):
    (tmp_path / 'escape').symlink_to(tmp_path.parent, target_is_directory=True)
    plan['tasks'][0]['owned_files'] = ['escape/other.py']
    assert any('unsafe' in e for e in validate_plan(plan, tmp_path))


def test_cycles_and_unknown_dependencies_rejected(plan, tmp_path):
    plan['tasks'][0]['depends_on'] = ['b', 'missing']
    plan['tasks'][1]['depends_on'] = ['a']
    errors = validate_plan(plan, tmp_path)
    assert any('cycle' in e for e in errors)
    assert any('unknown dependency' in e for e in errors)


def test_baseline_file_and_interface_reference_must_exist(plan, tmp_path):
    plan['shared_interfaces'][0]['owned_files'] = ['missing.py']
    plan['tasks'][1]['consumed_interfaces'] = ['missing']
    errors = validate_plan(plan, tmp_path)
    assert any('does not exist' in e for e in errors)
    assert any('unknown shared interface' in e for e in errors)


def test_single_owner_and_unique_ids(plan, tmp_path):
    plan['shared_interfaces'][0]['owner'] = ['alice', 'bob']
    plan['tasks'].append(dict(plan['tasks'][0]))
    errors = validate_plan(plan, tmp_path)
    assert any('exactly one' in e for e in errors)
    assert any('duplicate id' in e for e in errors)


def test_changed_interface_requires_provider_and_consumer_dependency(plan, tmp_path):
    plan['tasks'][0]['owned_files'] = ['core.py']
    assert any('provider binding' in e for e in validate_plan(plan, tmp_path))
    plan['shared_interfaces'][0].update(owner='alice', provided_by='a')
    plan['tasks'][1]['consumed_interfaces'] = ['core']
    assert any('requires dependency' in e for e in validate_plan(plan, tmp_path))
    plan['tasks'][1]['depends_on'] = ['a']
    assert validate_plan(plan, tmp_path) == []


def test_provider_owner_and_files_must_match(plan, tmp_path):
    plan['shared_interfaces'][0]['provided_by'] = 'a'
    errors = validate_plan(plan, tmp_path)
    assert any('same accountable owner' in e for e in errors)
    assert any('own all interface files' in e for e in errors)


def test_multiple_interface_owners_for_same_path_rejected(plan, tmp_path):
    plan['shared_interfaces'].append({'id': 'other', 'owner': 'someone_else', 'owned_files': ['core.py'], 'provided_by': None})
    assert any('conflicting shared-interface owners' in e for e in validate_plan(plan, tmp_path))


def test_transitive_order_and_completed_history(plan, tmp_path):
    plan['tasks'].append({'id': 'c', 'owner': 'carol', 'status': 'planned', 'owned_files': ['a.py'],
                          'depends_on': ['b'], 'consumed_interfaces': []})
    plan['tasks'][1]['depends_on'] = ['a']
    assert validate_plan(plan, tmp_path) == []
    plan['tasks'][0]['status'] = 'completed'
    plan['tasks'][2]['depends_on'] = []
    assert validate_plan(plan, tmp_path) == []


def test_committed_plan_is_valid():
    import json
    root = Path(__file__).resolve().parents[1]
    assert validate_plan(json.loads((root / 'docs/work-plan.json').read_text()), root) == []


def test_missing_or_unknown_provider_binding_is_an_error(plan, tmp_path):
    interface = plan['shared_interfaces'][0]
    del interface['provided_by']
    assert any('binding required' in e for e in validate_plan(plan, tmp_path))
    interface['provided_by'] = 'missing'
    assert any('unknown provider' in e for e in validate_plan(plan, tmp_path))


def test_malformed_status_and_schema_are_errors_not_exceptions(plan, tmp_path):
    plan['tasks'][0]['status'] = ['in_progress']
    plan['schema_version'] = True
    errors = validate_plan(plan, tmp_path)
    assert any('invalid status' in e for e in errors)
    assert any('schema_version' in e for e in errors)


def test_directory_overlap_counts_as_file_conflict(plan, tmp_path):
    plan['tasks'][0]['owned_files'] = ['src']
    plan['tasks'][1]['owned_files'] = ['src/module.py']
    assert any('overlap' in e for e in validate_plan(plan, tmp_path))

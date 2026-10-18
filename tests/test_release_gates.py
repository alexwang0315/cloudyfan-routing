"""Release records are structurally checked without granting execution authority."""

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('release_gates', ROOT / 'scripts/release_gates.py')
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
NOW = datetime(2026, 10, 18, tzinfo=timezone.utc)


@pytest.fixture
def pending():
    return json.loads((ROOT / 'docs/release-gates.json').read_text())


def qualify(data, root, *, status='qualified'):
    report = root / 'evidence/report.json'
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text('{"example": "test-only record"}\n')
    gate = data['gates'][0]
    gate.update(status=status, owner='test executor', reviewer='test reviewer',
                rationale='Scoped application does not expose this surface.' if status == 'not_applicable' else None)
    gate['evidence'] = [{
        'kind': 'real' if status == 'qualified' else 'scope_decision',
        'method': 'observed_execution' if status == 'qualified' else 'manual_review',
        'result': 'passed' if status == 'qualified' else 'approved',
        'scope_id': data['scope']['id'], 'path': 'evidence/report.json',
        'sha256': hashlib.sha256(report.read_bytes()).hexdigest(),
        'observed_at': '2026-10-17T00:00:00+00:00', 'expires_at': '2026-11-17T00:00:00+00:00',
        'authorization_ref': 'test-only-scoped-authorization',
    }]
    return gate


def test_checked_in_pending_records_are_valid_but_not_ready(pending):
    before = deepcopy(pending)
    assert MODULE.validate_gates(pending, ROOT, now=NOW) == []
    assert MODULE.production_ready(pending) is False
    assert pending == before
    assert all(g['owner'] is None and g['status'] == 'pending' for g in pending['gates'])


@pytest.mark.parametrize('mutation', [
    lambda d: d['gates'].pop(),
    lambda d: d['gates'].append(deepcopy(d['gates'][0])),
    lambda d: d['gates'][0].update(id='G-99'),
    lambda d: d['gates'][0].update(status='issue_closed'),
    lambda d: d['gates'][0].update(status='qualified'),
    lambda d: d.update(schema_version=True),
    lambda d: d.update(production_ready=True),
    lambda d: d['gates'][0].update(scope_id='different'),
    lambda d: d.update(scope={'id': '', 'description': 'unknown'}),
])
def test_invalid_transitions_and_schema_are_rejected(pending, mutation):
    mutation(pending)
    assert MODULE.validate_gates(pending, ROOT, now=NOW)


@pytest.mark.parametrize('status', ['qualified', 'not_applicable'])
def test_scoped_valid_record_is_structurally_accepted_without_claiming_truth(pending, tmp_path, status):
    qualify(pending, tmp_path, status=status)
    assert MODULE.validate_gates(pending, tmp_path, now=NOW) == []
    assert MODULE.production_ready(pending) is False


@pytest.mark.parametrize('field,value', [
    ('owner', None), ('owner', '   '), ('owner', 'TBD'), ('reviewer', 'unknown'),
    ('evidence', []), ('rationale', 'fixture passed'),
])
def test_qualification_requires_named_people_and_evidence(pending, tmp_path, field, value):
    gate = qualify(pending, tmp_path)
    gate[field] = value
    assert MODULE.validate_gates(pending, tmp_path, now=NOW)


@pytest.mark.parametrize('field,value', [
    ('kind', 'fixture'), ('kind', 'synthetic'), ('method', 'issue_closed'), ('method', []),
    ('result', 'unknown'), ('scope_id', 'other'), ('sha256', '0' * 64),
    ('expires_at', '2026-10-18T00:00:00+00:00'), ('observed_at', '2026-10-19T00:00:00+00:00'),
    ('observed_at', '2026-10-17T00:00:00'), ('expires_at', 'not-a-date'),
    ('authorization_ref', ''), ('path', 'evidence/missing.json'), ('path', '../outside.json'),
    ('path', '/absolute.json'), ('path', 'evidence/../evidence/report.json'),
])
def test_malformed_fixtures_stale_and_unscoped_evidence_cannot_qualify(pending, tmp_path, field, value):
    gate = qualify(pending, tmp_path)
    gate['evidence'][0][field] = value
    assert MODULE.validate_gates(pending, tmp_path, now=NOW)


def test_symlink_outside_root_cannot_qualify(pending, tmp_path):
    root = tmp_path / 'repo'
    gate = qualify(pending, root)
    (tmp_path / 'outside.json').write_bytes((root / 'evidence/report.json').read_bytes())
    (root / 'escaped.json').symlink_to(tmp_path / 'outside.json')
    gate['evidence'][0]['path'] = 'escaped.json'
    assert MODULE.validate_gates(pending, root, now=NOW)


def test_not_applicable_requires_scoped_reason_and_approval(pending, tmp_path):
    gate = qualify(pending, tmp_path, status='not_applicable')
    gate['rationale'] = None
    assert MODULE.validate_gates(pending, tmp_path, now=NOW)


def test_every_gate_must_qualify_before_readiness(pending, tmp_path):
    example = qualify(pending, tmp_path)
    for gate in pending['gates'][1:]:
        gate.update({k: deepcopy(v) for k, v in example.items() if k != 'id'})
    assert MODULE.validate_gates(pending, tmp_path, now=NOW) == []
    assert MODULE.production_ready(pending) is True


@pytest.mark.parametrize('data', [None, [], {'schema_version': 1, 'scope': [], 'gates': [None]},
                                 {'schema_version': 1, 'scope': {}, 'gates': None}])
def test_bad_shapes_report_errors_without_traceback(data, tmp_path):
    assert MODULE.validate_gates(data, tmp_path, now=NOW)
    assert MODULE.production_ready(data) is False

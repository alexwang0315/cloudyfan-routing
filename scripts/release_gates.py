"""Read-only structural release-evidence checks; never execution authorization.

Call production_ready only after validate_gates returns no errors, using the same
data/root and current time. Neither function authenticates people, authorizations,
or real-world evidence. A hash only detects a changed local evidence file.
"""

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path, PurePosixPath
import re


GATE_IDS = frozenset([f'G-{i:02}' for i in range(1, 4)] + [f'RG-{i:02}' for i in range(1, 8)])
STATUSES = {'pending', 'qualified', 'not_applicable'}
GATE_KEYS = {'id', 'status', 'scope_id', 'owner', 'reviewer', 'rationale', 'evidence'}
EVIDENCE_KEYS = {'kind', 'method', 'result', 'scope_id', 'path', 'sha256', 'observed_at',
                 'expires_at', 'authorization_ref'}
PLACEHOLDERS = {'unknown', 'pending', 'tbd', 'unassigned', 'none', 'n/a', '未指定', '待指定'}


def _text(value):
    return isinstance(value, str) and bool(value.strip()) and value.strip().lower() not in PLACEHOLDERS


def _timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None and parsed.utcoffset() is not None else None


def _evidence_errors(evidence, gate, root, now):
    errors = []
    label = f"{gate['id']} evidence"
    if not isinstance(evidence, dict) or set(evidence) != EVIDENCE_KEYS:
        return [f'{label}: expected exactly {sorted(EVIDENCE_KEYS)}']
    qualified = gate['status'] == 'qualified'
    if evidence['kind'] != ('real' if qualified else 'scope_decision'):
        errors.append(f'{label}: qualified needs real evidence; N/A needs scope_decision (fixtures are not evidence)')
    methods = {'observed_execution', 'manual_review'} if qualified else {'manual_review'}
    if not isinstance(evidence['method'], str) or evidence['method'] not in methods:
        errors.append(f'{label}: invalid verification method')
    if evidence['result'] != ('passed' if qualified else 'approved'):
        errors.append(f'{label}: requires a passing result or scoped approval')
    if evidence['scope_id'] != gate['scope_id']:
        errors.append(f'{label}: scope_id does not match the release scope')
    if not _text(evidence['authorization_ref']):
        errors.append(f'{label}: missing scoped authorization reference (not verified by this checker)')
    observed = _timestamp(evidence['observed_at'])
    expires = _timestamp(evidence['expires_at'])
    if observed is None or expires is None:
        errors.append(f'{label}: observed_at/expires_at must be timezone-aware ISO timestamps')
    elif not observed <= now < expires:
        errors.append(f'{label}: evidence is future-dated, expired, or has invalid validity interval')
    digest = evidence['sha256']
    if not isinstance(digest, str) or re.fullmatch('[0-9a-f]{64}', digest) is None:
        errors.append(f'{label}: sha256 must contain 64 lowercase hex characters')
    raw = evidence['path']
    if (not isinstance(raw, str) or not raw or '\\' in raw or '\x00' in raw
            or PurePosixPath(raw).is_absolute() or '..' in PurePosixPath(raw).parts):
        errors.append(f'{label}: path must be a confined local relative file without traversal')
        return errors
    try:
        path = (root / raw).resolve(strict=True)
        if not path.is_relative_to(root.resolve()) or not path.is_file():
            errors.append(f'{label}: path must resolve to a file inside the repository')
        elif hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            errors.append(f'{label}: evidence hash mismatch')
    except (OSError, RuntimeError, ValueError):
        errors.append(f'{label}: missing or unreadable local evidence file')
    return errors


def validate_gates(data: dict, root: Path, *, now: datetime | None = None) -> list[str]:
    """Return actionable structural errors without mutation, network, or execution."""
    now = datetime.now(timezone.utc) if now is None else now
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        return ['now must be a timezone-aware datetime']
    if not isinstance(data, dict) or set(data) != {'schema_version', 'scope', 'gates'}:
        return ['release record must contain exactly schema_version, scope, gates']
    errors = []
    if type(data['schema_version']) is not int or data['schema_version'] != 1:
        errors.append('schema_version must be integer 1')
    scope = data['scope']
    if (not isinstance(scope, dict) or set(scope) != {'id', 'description'}
            or not _text(scope.get('id')) or not _text(scope.get('description'))):
        return errors + ['scope must contain a non-placeholder id and explicit description']
    gates = data['gates']
    if not isinstance(gates, list):
        return errors + ['gates must be a list']
    seen = set()
    for index, gate in enumerate(gates):
        if not isinstance(gate, dict) or set(gate) != GATE_KEYS:
            errors.append(f'gate[{index}] must contain exactly {sorted(GATE_KEYS)}')
            continue
        identity = gate['id']
        if not isinstance(identity, str) or identity not in GATE_IDS:
            errors.append(f'gate[{index}]: unknown gate ID')
            continue
        if identity in seen:
            errors.append(f'{identity}: duplicate gate ID')
        seen.add(identity)
        status = gate['status']
        if not isinstance(status, str) or status not in STATUSES:
            errors.append(f'{identity}: unknown status')
            continue
        if gate['scope_id'] != scope['id']:
            errors.append(f'{identity}: scope_id must match release scope')
        for role in ('owner', 'reviewer'):
            if gate[role] is not None and not _text(gate[role]):
                errors.append(f'{identity}: {role} must be named or null')
        if not isinstance(gate['evidence'], list):
            errors.append(f'{identity}: evidence must be a list')
            continue
        if status == 'pending':
            if gate['evidence'] or gate['rationale'] is not None:
                errors.append(f'{identity}: pending records must keep evidence empty and rationale null; '
                              'retain attempts in separate historical reports')
            continue
        if not _text(gate['owner']) or not _text(gate['reviewer']):
            errors.append(f'{identity}: qualified/N/A requires named owner and reviewer')
        if status == 'not_applicable' and not _text(gate['rationale']):
            errors.append(f'{identity}: N/A requires a scoped rationale')
        if status == 'qualified' and gate['rationale'] is not None:
            errors.append(f'{identity}: rationale is reserved for N/A; put verification details in evidence')
        if not gate['evidence']:
            errors.append(f'{identity}: qualified/N/A requires local scoped evidence')
        for evidence in gate['evidence']:
            errors.extend(_evidence_errors(evidence, gate, Path(root), now))
    if seen != GATE_IDS:
        errors.append(f'missing gate IDs: {sorted(GATE_IDS - seen)}')
    return errors


def production_ready(data) -> bool:
    """Aggregate statuses ONLY AFTER a clean validate_gates call; not an authorization."""
    if not isinstance(data, dict) or not isinstance(data.get('gates'), list):
        return False
    gates = data['gates']
    return (len(gates) == len(GATE_IDS)
            and all(isinstance(g, dict) and isinstance(g.get('id'), str) for g in gates)
            and {g['id'] for g in gates} == GATE_IDS
            and all(g.get('status') in ('qualified', 'not_applicable') for g in gates))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args(argv)
    try:
        data = json.loads((args.root / 'docs/release-gates.json').read_text())
    except (OSError, ValueError) as exc:
        print(json.dumps({'valid': False, 'production_ready': False, 'errors': [str(exc)]}))
        return 1
    errors = validate_gates(data, args.root)
    print(json.dumps({'valid': not errors, 'production_ready': not errors and production_ready(data),
                      'errors': errors}, indent=2))
    return int(bool(errors))


if __name__ == '__main__':
    raise SystemExit(main())

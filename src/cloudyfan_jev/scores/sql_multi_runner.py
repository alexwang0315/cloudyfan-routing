"""Opt-in exact-setting SQL exams for main/backup candidates, never activation.

Pool slots reference a single captured cohort for each exact configuration. This
module does not load historical answers, choose candidates or create spending
authority. The frozen SQL suite and independent assessor remain unchanged.
"""
from copy import deepcopy
import hashlib
from pathlib import Path

from ..budget import BudgetError
from ..client import CHAT
from ..generation_request import reasoning_fields
from .catalog import digest
from .configuration import configuration_digest
from .sql_exam import manifest
from .sql_runner import CaptureStopped, QualificationRunner

SCHEMA = 'sql-multi-candidate-exam-v1'
VERSION = 'sql-model-capture-v2-multi'
POOLS = ('open_source', 'low', 'mid', 'high')
EFFORTS = {'none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max'}


def _wire_identity(profile):
    return digest({'model': profile['model_version'], 'provider': profile['provider'],
                   'reasoning': reasoning_fields(profile)})


def prepare_multi_candidate_table(entries, *, slots=None):
    """Freeze explicit executable exam settings; deduplicate by full identity.

    This is exam preparation, not model qualification. Missing identities and
    provider-default reasoning proposals must remain in the review inventory.
    Slots contain configuration SHA256s and never grant dispatch permission.
    """
    if not isinstance(entries, list) or not entries:
        raise ValueError('Explicit candidate entries required')
    unique, wire = {}, {}
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get('profile'), dict):
            raise ValueError('Each candidate needs an exact profile')
        profile = entry['profile']
        for key in ('model', 'model_version', 'provider', 'provider_name'):
            if (not isinstance(profile.get(key), str) or not profile[key].strip()
                    or profile[key].lower() in {'unknown', 'pending', 'provider_default'}):
                raise ValueError('Exact model/provider identity required')
        if profile.get('effort') not in EFFORTS:
            raise ValueError('Explicit supported effort required')
        reasoning_fields(profile)
        if (profile.get('backend') == 'local' or profile.get('endpoint', CHAT) != CHAT
                or profile.get('accepted_models') != [profile['model_version']]):
            raise ValueError('One pinned Chat Completions response identity required')
        if (profile['model'].endswith(':free') != profile['model_version'].endswith(':free')):
            raise ValueError('Free request route cannot be replaced by paid canonical identity')
        identity = configuration_digest(entry)
        request = _wire_identity(profile)
        if request in wire and wire[request] != identity:
            raise ValueError('Different configurations have indistinguishable wire identity')
        wire[request] = identity
        unique.setdefault(identity, deepcopy(entry))
    if len(unique) > 12:
        raise ValueError('The SQL backup exam has at most twelve unique settings')
    if slots is None:
        slots = {}
    if not isinstance(slots, dict) or set(slots) - set(POOLS):
        raise ValueError('Unknown SQL pool slots')
    for pool, identities in slots.items():
        if (not isinstance(identities, list) or len(identities) > 3
                or any(not isinstance(cid, str) or cid not in unique for cid in identities)
                or len(set(identities)) != len(identities)):
            raise ValueError('Slots must reference distinct prepared configuration hashes')
        models = [unique[cid]['profile']['model'] for cid in identities]
        versions = [unique[cid]['profile']['model_version'] for cid in identities]
        if len(set(models)) != len(models) or len(set(versions)) != len(versions):
            raise ValueError('Each pool needs different models, not provider copies')
    return {'schema_version': SCHEMA, 'suite_id': manifest()['suite_id'],
            'configurations': [{'role': 'c' + cid[:24], 'entry': entry,
                                'configuration_sha256': cid} for cid, entry in unique.items()],
            'slots': deepcopy(slots)}


class MultiCandidateQualificationRunner(QualificationRunner):
    """Capture 1..12 exact settings with configuration-bound fee reservations.

    Legacy four-role manifests, request IDs, grading and fees are not converted.
    A real caller still supplies authorization and verified all-path bounds.
    Synthetic mode requires MockTransport in the parent safety boundary.
    """
    version = VERSION
    exam_directory = 'candidate-exams'

    def __init__(self, client, store, table, *, model_bounds, verify_bound, synthetic=False,
                 environment_factory=None, environment_version=None):
        if not isinstance(table, dict) or table.get('schema_version') != SCHEMA:
            raise ValueError('Explicit versioned multi-candidate table required')
        rows = table.get('configurations')
        if not isinstance(rows, list) or any(not isinstance(row, dict) or 'entry' not in row for row in rows):
            raise ValueError('Explicit candidate configurations required')
        expected = prepare_multi_candidate_table([row['entry'] for row in rows], slots=table.get('slots'))
        if table != expected:
            raise ValueError('Candidate table identity, ordering or slots changed')
        self.roles = tuple(row['role'] for row in rows)
        if len(set(self.roles)) != len(self.roles):
            raise ValueError('Candidate ID collision')
        ids = {row['configuration_sha256'] for row in rows}
        if set(model_bounds) != ids:
            raise BudgetError('Complete bounds keyed by exact configuration SHA256 required')
        self._wire_configurations = {_wire_identity(row['entry']['profile']): row['configuration_sha256']
                                     for row in rows}
        super().__init__(client, store, table, model_bounds=model_bounds, verify_bound=verify_bound,
                         synthetic=synthetic, environment_factory=environment_factory,
                         environment_version=environment_version)

    def _prefix(self, run_id):
        return f'exam-multi-{digest(run_id)[:16]}-'

    def _budget_run(self, run_id):
        return 'sql-multi-' + digest({'root': str(self.client.root.resolve()), 'run': run_id})[:24]

    def _bound_for_entry(self, entry):
        try:
            return self.bounds[configuration_digest(entry)]
        except KeyError:
            raise BudgetError('Missing exact configuration bound') from None

    def _bound_for_payload(self, payload):
        identity = digest({'model': payload.get('model'),
                           'provider': payload.get('provider', {}).get('only', [None])[0],
                           'reasoning': {'reasoning': payload['reasoning']} if 'reasoning' in payload else {}})
        try:
            return self.bounds[self._wire_configurations[identity]]
        except KeyError:
            raise BudgetError('Unknown exact request bound') from None

    def _frozen(self):
        return {**super()._frozen(),
                'multi_runner_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                'bound_key': 'configuration_sha256', 'activation': False}

    def inspect(self, run_id, *, status=None, reason=None):
        return {**super().inspect(run_id, status=status, reason=reason),
                'candidate_count': len(self.roles),
                'slot_count': sum(len(ids) for ids in self.table['slots'].values()),
                'slot_configurations': deepcopy(self.table['slots']), 'pool_activated': False}

    def grade(self, run_id, *, now):
        # Fail before consulting the assessor, even for an incomplete/absent exam.
        directory = self._directory(run_id, create=False)
        if not (directory / 'capture-complete.json').is_file():
            raise CaptureStopped('capture_must_complete_before_grading')
        return {**super().grade(run_id, now=now),
                'candidate_configurations': {row['role']: row['configuration_sha256']
                                             for row in self.table['configurations']},
                'slot_configurations': deepcopy(self.table['slots']),
                'candidate_pass_is_not_workflow_or_pool_qualification': True}

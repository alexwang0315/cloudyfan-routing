"""Opt-in category-local four-pool admission; never classify a task implicitly."""
from copy import deepcopy

from .categories import resolve_category, verify_category_matrix
from .integration import ScoreCore, utc_now
from .qcs import prepare_qcs

VERSION = 'category-qcs-v1'


def _category_provenance(value, category):
    return isinstance(value, dict) and value.get('category_id') == category


def prepare_category_selection(matrix, category, requirements, evidence, policy, *, now):
    """Prepare one category with independently calibrated full-path QCS data.

    Benchmark thresholds provide priors, not correctness probabilities. Bad
    optional candidate evidence excludes that candidate; no other category is
    borrowed. None-time is reserved for integrity-only replay of a frozen run.
    """
    category = resolve_category(category)
    verify_category_matrix(matrix)
    view = matrix['categories'][category]
    if view['status'] != 'ready':
        raise ValueError('Selected category has no prepared four-pool continuation')
    if requirements.get('domain') != category or policy.get('category_id') != category:
        raise ValueError('Explicit matching category requirements and policy required')
    if (not _category_provenance(policy.get('calibration'), category)
            or not _category_provenance(policy.get('complete_policy_flow', {}).get('provenance'), category)):
        raise ValueError('Independent category calibration required')
    for stage in policy.get('stages', {}).values():
        calibration = stage.get('classified_calibration')
        if calibration is not None and not _category_provenance(calibration, category):
            raise ValueError('Classifier calibration category mismatch')
    snapshot = deepcopy(view['snapshot'])
    plan = prepare_qcs(snapshot, requirements, evidence, policy, now=now)
    return {'schema_version': VERSION, 'category_id': category, 'matrix_id': matrix['matrix_id'],
            'snapshot': snapshot, 'qcs_plan': plan}


class CategoryCore(ScoreCore):
    """Freeze explicit category, pool matrix and evidence for one resumable run."""
    def __init__(self, client, matrix, category, requirements, evidence, policy, decision_profile,
                 *, run_id, clock=utc_now, **kwargs):
        from pathlib import Path
        replay = (Path(client.root) / 'runs' / run_id / 'manifest.json').exists()
        prepared = prepare_category_selection(matrix, category, requirements, evidence, policy,
                                               now=None if replay else clock())
        self._category_binding = {key: prepared[key] for key in ('schema_version', 'category_id', 'matrix_id')}
        self._category_binding['snapshot_id'] = prepared['snapshot']['snapshot_id']
        super().__init__(client, prepared['snapshot'], requirements, decision_profile, run_id=run_id,
                         clock=clock, qcs_plan=prepared['qcs_plan'], **kwargs)

    def run(self, run_id, task, environment_factory, *, contract_version, mode='cloudyfan'):
        task = deepcopy(task)
        category = self._category_binding['category_id']
        if 'category' in task and resolve_category(task['category']) != category:
            raise ValueError('Task category differs from frozen category')
        if 'category_binding' in task and task['category_binding'] != self._category_binding:
            raise ValueError('Task category binding differs from prepared matrix')
        task['category'] = category
        task['category_binding'] = deepcopy(self._category_binding)
        return super().run(run_id, task, environment_factory, contract_version=contract_version, mode=mode)

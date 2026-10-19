"""Candidate-specific evidence inventory, deliberately not a fee verifier."""
from ..client import CHAT, DECISIONS
from ..component_bounds import COMPONENTS
from ..rejection_fees import Text429FeePolicy
from .catalog import digest
from .configuration import configuration_digest

PATHS = ['success', 'rejection', 'failure', 'cancellation', 'disconnect',
         'upstream_retry_child', 'price_change_inflight']


def fee_gap_matrix(profile):
    """List missing guarantees without assigning a numeric cap or approving calls.

    This report is scoped to fixed-endpoint, tool-free text. Receipts and scoped
    rejection attribution remain historical evidence, not provider upper bounds.
    All source statements must be reviewed separately for each exact candidate.
    """
    registry = profile['registry']
    identities = [('open_source', registry['open_source']), *[
        (entry.get('intended_tier', 'candidate'), entry) for entry in registry['candidates']]]
    roles = []
    for role, entry in identities:
        p = entry['profile']
        roles.append({'role': role, 'config_id': entry['config_id'],
                      'configuration_sha256': configuration_digest(entry),
                      'model_version': p['model_version'], 'effort': p['effort'],
                      'provider': p['provider'], 'quantization': p.get('quantization'),
                      'endpoint': p.get('endpoint', CHAT)})
    decision = profile['decision']
    roles.append({'role': 'classification', 'config_id': 'decision',
                  'configuration_sha256': digest(decision), 'model_version': decision['version'],
                  'provider': decision['provider'], 'effort': None, 'quantization': None,
                  'endpoint': DECISIONS})
    for row in roles:
        row.update(account_period=None, upper_bound_usd=None, paths=list(PATHS),
                   components={name: {'status': 'pending', 'bound_or_zero_guarantee': None,
                                      'missing': 'Exact all-path quantity/rate or binding zero guarantee; independent review'}
                               for name in sorted(COMPONENTS)})
    return {'schema_version': 'cloudyfan-first-pool-fee-gaps-v1', 'profile_sha256': digest(profile),
            'roles': roles, 'dispatch_qualified': False, 'workflow_upper_bound_usd': None,
            'workflow_reservation': 'open_source + 2 * classification + max(low, mid, high)',
            'scope': 'Tool-free fixed endpoint text; one generation request per phase; no plugins/fallback',
            'rejection_policy': {'version': Text429FeePolicy.VERSION,
                                 'is_supplier_cap': False, 'actual_receipt_takes_priority': True,
                                 'unknown_stops': True},
            'historical_accounting': {'cohort': 'fresh50-recovery-20261020', 'calls': 221,
                                      'cost_usd': '5.805611568', 'unknown_in_cohort': 0,
                                      'evidence': 'docs/evidence/fresh50-recovery-20261020.json',
                                      'clears_earlier_unknowns': False},
            'priorities': ['Cumulative Jev billed input including internal attempts',
                           'All eight components and seven paths for each exact candidate',
                           'Account/period, settlement finality, revocation and in-flight coverage']}

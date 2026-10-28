"""Host-side identity binding for original ordinary/QCS evidence families."""
from cloudyfan_jev.scores.configuration import configuration_digest

from .saved_comparisons import SourceUnavailable, identifier


def attempt_identity(manifest, attempt):
    tier = attempt['tier']
    profile = manifest['profiles'][tier]
    card = profile.get('capability_card', {})
    config_id = card.get('score_selection', {}).get('config_id')
    if identifier(config_id) is None:
        raise SourceUnavailable('original_configuration_identity_unavailable')
    configuration_sha = configuration_digest({'profile': profile})
    plan = manifest.get('selection_policy')
    if plan is not None and (plan['profiles'][tier] != profile
            or card['qcs']['configuration_sha256'] != configuration_sha
            or plan['records'][tier]['configuration_sha256'] != configuration_sha):
        raise SourceUnavailable('original_configuration_identity_invalid')
    return {'config_id': config_id, 'configuration_sha256': configuration_sha}


def reproduce_saved_policy(source, binding, job, manifest, result, run, trusted, *, original_path, receipts):
    raise SourceUnavailable('original_decision_loader_pending')

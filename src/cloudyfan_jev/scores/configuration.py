"""Shared executable-identity binding for model qualification and fee inventory."""
from ..generation_request import reasoning_fields
from .catalog import digest


def configuration_digest(entry):
    """Bind evidence to executable identity, not a family name or mutable card."""
    profile = entry['profile']
    identity = {k: profile.get(k) for k in (
        'model', 'model_version', 'accepted_models', 'effort', 'provider', 'provider_name',
        'endpoint', 'quantization', 'engine', 'weights_digest')}
    if 'reasoning_parameter' in profile:
        reasoning_fields(profile)
        identity['reasoning_parameter'] = profile['reasoning_parameter']
    return digest(identity)

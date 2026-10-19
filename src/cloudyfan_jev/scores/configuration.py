"""Shared executable-identity binding for model qualification and fee inventory."""
from .catalog import digest


def configuration_digest(entry):
    """Bind evidence to executable identity, not a family name or mutable card."""
    profile = entry['profile']
    return digest({k: profile.get(k) for k in (
        'model', 'model_version', 'accepted_models', 'effort', 'provider', 'provider_name',
        'endpoint', 'quantization', 'engine', 'weights_digest')})

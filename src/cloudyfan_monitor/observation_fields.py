"""Shared safe public identity fields for operator evidence projections."""
import re

PROFILE_FIELDS = ("model", "model_version", "effort", "provider", "provider_name", "quantization", "backend")


def identifier(value):
    if (isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/@+ -]{0,179}", value)
            and "://" not in value and ".." not in value
            and not value.lower().startswith(("users/", "private/", "tmp/", "bearer ", "https:", "http:"))):
        return value
    return None


def configuration(profile):
    profile = profile if isinstance(profile, dict) else {}
    return {key: identifier(profile.get(key)) for key in PROFILE_FIELDS}

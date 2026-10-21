"""Explicit reasoning wire contract shared by generation and its evidence checks.

An absent parameter selector retains historical effort requests. Omitting reasoning
requires an explicit nonreasoning configuration; it never waives billing controls.
"""


def reasoning_fields(profile):
    """Return the exact wire fields, rejecting unknown or conflicting selectors."""
    mode = profile.get('reasoning_parameter', 'effort')
    if mode not in ('effort', 'omit'):
        raise ValueError('Unknown reasoning_parameter')
    effort = profile['effort']
    if mode == 'omit':
        if effort != 'none':
            raise ValueError('Omitting reasoning requires effort none')
        return {}
    return {'reasoning': {'effort': effort}}


def reasoning_matches(profile, payload):
    """Require exact presence as well as value; null is not an omitted field."""
    expected = reasoning_fields(profile)
    if 'reasoning_parameter' in payload or 'reasoning_effort' in payload:
        return False
    return ({'reasoning': payload['reasoning']} if 'reasoning' in payload else {}) == expected


def request_model(profile):
    """Retain the exact configured route, including a free-route suffix."""
    return profile['model_version']

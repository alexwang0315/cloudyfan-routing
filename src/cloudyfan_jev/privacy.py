"""Explicit execution-site policies; administrator declarations are trust inputs.

This gate does not sandbox arbitrary Python tools. Tool locations must cover all
of their execution and egress, and tools must be isolated by the embedding app.
"""
from copy import deepcopy
from dataclasses import dataclass, field
import re

from .errors import RoutingError


def known_location(location):
    return isinstance(location, str) and re.fullmatch(r'(home|company|cloud):[A-Za-z0-9][A-Za-z0-9_.-]{0,79}', location) is not None


class PolicyDenied(RoutingError):
    def __init__(self, stage, reason):
        super().__init__('location_policy_denied', 'Execution location policy denied dispatch',
                         status=403, details={'stage': stage, 'reason': reason})


@dataclass(frozen=True)
class ExecutionPolicy:
    """Task policy with exact origin and server-registered tool execution sites.

    local_only means the same home/company site, never simply 'not cloud'.
    Unknown locations are denied even when mode is unrestricted.
    """
    mode: str
    origin: str
    classifier: str | None = None
    tools: dict[str, str] = field(default_factory=dict)
    version: str = 'execution-policy-v1'

    def __post_init__(self):
        if self.mode not in ('local_only', 'unrestricted') or not known_location(self.origin):
            raise ValueError('Explicit execution policy mode and origin are required')
        if self.mode == 'local_only' and self.origin.startswith('cloud:'):
            raise ValueError('local_only requires a home or company origin')
        if not isinstance(self.version, str) or not self.version.strip():
            raise ValueError('Execution policy version is required')
        if not isinstance(self.tools, dict):
            raise ValueError('Tool locations require a registry')
        object.__setattr__(self, 'tools', deepcopy(self.tools))

    def to_dict(self):
        return {'version': self.version, 'mode': self.mode, 'origin': self.origin,
                'classifier': self.classifier, 'tools': deepcopy(self.tools)}

    def require(self, stage, location):
        if not known_location(location):
            raise PolicyDenied(stage, 'unknown_location')
        if self.mode == 'local_only' and location != self.origin:
            raise PolicyDenied(stage, 'outside_origin')

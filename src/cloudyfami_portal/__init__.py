"""Cloudyfami platform relay (ADR 0039, ADR 0040)."""
__version__ = '0.0.1'

from .connectivity import Connectivity, Mode
from .envelope import COMPACT_MAX_BYTES, Envelope, EnvelopeError, Kind, TooLargeForCompact
from .relay import Conflict, DeliveryPolicy, NotFound, Relay, State

__all__ = [
    'COMPACT_MAX_BYTES', 'Conflict', 'Connectivity', 'DeliveryPolicy', 'Envelope', 'EnvelopeError', 'Kind',
    'Mode', 'NotFound', 'Relay', 'State',
    'TooLargeForCompact',
]

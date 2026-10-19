"""Opt-in, archived provider-policy evidence for a narrow no-answer rejection.

This is a billing-policy attribution, never a fabricated generation receipt or
production qualification. No HTTP is performed here. Applications own admitting
an authoritative source and its exact scope before dispatch.
"""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
from pathlib import Path

from .receipt_store import _encoded


class Text429FeePolicy:
    VERSION = 'openrouter-fixed-text-upstream429-v2'
    SOURCE = 'https://openrouter.ai/pricing'
    EXCERPT = 'An attempt that errors is not charged for its model tokens'

    def __init__(self, source_path, source_sha256, profiles, *, valid_until, clock=None):
        self.source_path = Path(source_path)
        self.source_sha256 = source_sha256
        self.profiles = deepcopy(profiles)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.valid_until = valid_until
        expires = datetime.fromisoformat(valid_until)
        if expires.tzinfo is None or not profiles:
            raise ValueError('Policy needs explicit expiry and fixed profiles')
        for p in profiles:
            if set(p) != {'model', 'provider', 'provider_name'} or any(not isinstance(v, str) or not v for v in p.values()):
                raise ValueError('Policy scope requires exact model/provider identities')
        self._check_source()

    def _check_source(self):
        raw = self.source_path.read_bytes()
        if (hashlib.sha256(raw).hexdigest() != self.source_sha256
                or self.EXCERPT not in raw.decode('utf-8')
                or self.clock() >= datetime.fromisoformat(self.valid_until)):
            raise ValueError('Changed, missing or expired fee-policy evidence')

    def descriptor(self):
        return {'version': self.VERSION, 'source': self.SOURCE, 'source_sha256': self.source_sha256,
                'profiles': deepcopy(self.profiles), 'valid_until': self.valid_until,
                'cost_source': 'provider_error_no_charge_policy'}

    def evidence(self, intent, record):
        from .client import CHAT, usage, rate_limit_rejection, _strict_json
        self._check_source()
        payload = intent['payload']
        profile = next((p for p in self.profiles if p['model'] == payload.get('model')), None)
        if not profile or intent.get('url') != CHAT:
            return None
        if set(payload) - {'model', 'messages', 'max_tokens', 'provider', 'reasoning', 'session_id'}:
            return None
        messages = payload.get('messages')
        if not isinstance(messages, list) or not messages or not all(
                isinstance(m, dict) and set(m) <= {'role', 'content'}
                and m.get('role') in {'system', 'user', 'assistant'} and isinstance(m.get('content'), str)
                for m in messages):
            return None
        if payload.get('provider') != {'only': [profile['provider']], 'allow_fallbacks': False,
                                      'require_parameters': True}:
            return None
        if not rate_limit_rejection(record, profile['model'], [profile['model']], profile['provider_name']):
            return None
        data = _strict_json(record['raw']); metadata = data['error'].get('metadata', {})
        if (metadata.get('is_byok') is not False
                or metadata.get('limit_source') != 'upstream_provider_shared_pool'):
            return None
        info = usage(record)
        if not info['generation_id'] or record.get('duplicate_generation_id'):
            return None
        if not info['cost_unknown']:
            return None  # An actual usage/receipt cost always wins, including nonzero error fees.
        if intent.get('request_sha256') != record.get('request_sha256') or intent['request_sha256'] != hashlib.sha256(
                _encoded({'url': intent['url'], 'payload': payload})).hexdigest():
            return None
        return {'verified': True, 'generation_id': info['generation_id'], 'cost_usd': '0',
                'cost_source': 'provider_error_no_charge_policy', 'not_a_generation_receipt': True,
                'request_sha256': intent['request_sha256'],
                'response_sha256': hashlib.sha256(_encoded(record)).hexdigest(),
                'policy': self.descriptor()}

    def validate_saved(self, intent, record, evidence):
        return evidence == self.evidence(intent, record)

"""Reviewed component bounds, not supplier-issued USD cap certificates.

No real qualifications ship here. Trusted application configuration supplies
independently reviewed quantity/rate guarantees. A price snapshot is insufficient.
Constructors and verification perform no network I/O. See docs/component-bounds.md.
"""
from copy import deepcopy
from decimal import Decimal, localcontext
import hashlib
import math
from pathlib import Path
import re
from time import time
import uuid

from .budget import EnforcedBound, money
from .client import CHAT, DECISIONS, Client
from .provider_bounds import credential_fingerprint
from .receipt_store import ReceiptStore, _encoded, _immutable, _read

COMPONENTS = frozenset({'input', 'output', 'reasoning', 'cache_read', 'cache_write',
                        'request', 'classification', 'failure'})
REQUIRED_CLAIMS = frozenset({
    'billed_input_cap_includes_serialization_and_rejected_requests',
    'effective_rate_ceilings_cover_cache_tiers_and_price_changes',
    'cache_read_and_write_units_each_bounded_by_input',
    'output_and_reasoning_controls_enforced_by_exact_endpoint',
    'classification_fee_and_endpoint_output_cap_qualified',
    'complete_inventory_no_unlisted_or_embedded_charges',
    'no_advisor_plugins_media_tools_byok_or_implicit_features',
    'single_post_includes_all_upstream_attempt_charges',
    'failure_cancellation_and_inflight_charges_bounded',
    'guarantees_survive_expiry_or_revocation_for_dispatched_requests',
})
_FIELDS = {'schema_version', 'mechanism', 'model_version', 'endpoint', 'credential_sha256',
           'configuration_sha256', 'max_payload_bytes', 'output_control', 'reasoning_mode',
           'components', 'claims', 'sources', 'issued_at', 'expires_at'}
_REVIEW_FIELDS = {'evidence_sha256', 'reviewer', 'reference', 'reviewed_at', 'expires_at',
                  'withdrawal_registry_sha256'}
_SOURCES = {'quantities', 'rates', 'inventory', 'failures'}
_DIGEST = re.compile(r'[0-9a-f]{64}\Z')
_RATE = re.compile(r'(?:0|[1-9][0-9]*)(?:\.[0-9]+)?\Z')


def _sha(value):
    return hashlib.sha256(_encoded(value)).hexdigest()


def _instant(value):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError('Finite timestamp required')
    return value


def _units(value):
    if type(value) is not int or not 0 <= value <= 2 ** 31:
        raise ValueError('Bounded nonnegative integer units required')
    return value


def _rate(value):
    if not isinstance(value, str) or len(value) > 128 or not _RATE.fullmatch(value):
        raise ValueError('Finite plain decimal USD rate required')
    return money(value)


def _text_json(value, depth=0):
    """Restrictive Decisions subset; no media-bearing structure or non-JSON value."""
    media = {'image', 'images', 'image_url', 'input_audio', 'audio', 'video', 'file',
             'files', 'file_url', 'attachments', 'plugins', 'tools', 'server_tools'}
    if depth > 32:
        return False
    if isinstance(value, dict):
        return (all(isinstance(k, str) and k.lower() not in media for k in value)
                and all(_text_json(v, depth + 1) for v in value.values()))
    if isinstance(value, list):
        return all(_text_json(v, depth + 1) for v in value)
    return value is None or type(value) in (str, bool, int) or (type(value) is float and math.isfinite(value))


class ComponentBoundVerifier:
    """Exact cloud-client verifier for qualified, tool-free text endpoints.

    Reviews are trusted operator input, never request input. Their hashes bind the
    entire evidence artifact; hashing alone cannot establish its real-world truth.
    No default approvals, catalog-to-qualification conversion or synthetic key GET.
    """
    def __init__(self, client, *, configurations=None, schedules=(), reviews=(), clock=time,
                 withdrawal_root=None):
        if not isinstance(client, Client):
            raise ValueError('Exact OpenRouter Client required')
        self._client, self._http = client, client.http
        self._credential = credential_fingerprint(client.key)
        self._configurations = deepcopy(configurations or {})
        self._schedules = deepcopy(tuple(schedules))
        self._reviews = deepcopy(tuple(reviews))
        self._withdrawals = None
        if withdrawal_root is not None:
            withdrawal_root = Path(withdrawal_root)
            if withdrawal_root.is_symlink():
                raise ValueError('Withdrawal registry must not be a symlink')
            self._withdrawals = ReceiptStore(withdrawal_root)
            with self._withdrawals._lock():
                marker = withdrawal_root / 'registry.json'
                if marker.is_symlink():
                    raise ValueError('Withdrawal registry identity must not be a symlink')
                if not marker.exists():
                    _immutable(marker, {'schema_version': 1, 'incarnation': uuid.uuid4().hex})
        self._leases = {}
        self.clock = clock
        self.last_check = {'allowed': False, 'reason': 'not_checked'}

    def descriptor(self):
        return {'version': 'qualified-components-v1', 'credential_sha256': self._credential,
                'configurations_sha256': _sha(self._configurations),
                'schedules_sha256': _sha(self._schedules), 'reviews_sha256': _sha(self._reviews),
                'withdrawal_root_sha256': self._registry_hash(),
                'revoked_sha256': _sha(sorted(p.name for p in self._withdrawals.root.glob('withdrawn-*.json'))
                                       if self._withdrawals else [])}

    def _registry_hash(self):
        if self._withdrawals is None:
            return None
        marker = self._withdrawals.root / 'registry.json'
        if marker.is_symlink():
            raise ValueError('Withdrawal registry identity changed')
        identity = _read(marker)
        if (not isinstance(identity, dict) or set(identity) != {'schema_version', 'incarnation'}
                or type(identity['schema_version']) is not int or identity['schema_version'] != 1
                or not isinstance(identity['incarnation'], str)
                or not re.fullmatch('[0-9a-f]{32}', identity['incarnation'])):
            raise ValueError('Malformed withdrawal registry identity')
        return _sha({'root': str(self._withdrawals.root.resolve()), 'identity': identity})

    def revoke(self, evidence_sha256):
        """Privileged local review withdrawal; not exposed by the task API."""
        if not isinstance(evidence_sha256, str) or not _DIGEST.fullmatch(evidence_sha256):
            raise ValueError('Exact evidence hash required')
        if self._withdrawals is None:
            raise ValueError('Persistent withdrawal registry required')
        path = self._withdrawals.root / ('withdrawn-' + evidence_sha256 + '.json')
        with self._withdrawals._lock():
            if not path.exists():
                _immutable(path, {'evidence_sha256': evidence_sha256, 'withdrawn_at': _instant(self.clock())})
        self._leases.clear()

    def _deny(self, reason):
        self.last_check = {'allowed': False, 'reason': reason}
        return False

    def _pinned(self):
        if (self._client.http is not self._http
                or credential_fingerprint(self._client.key) != self._credential):
            raise ValueError('Transport or credential changed')

    def _qualified(self, identity):
        self._pinned()
        if self._withdrawals is None:
            raise ValueError('Persistent reviewed withdrawal registry required')
        matches = [s for s in self._schedules if s.get('model_version') == identity]
        if len(matches) != 1 or identity not in self._configurations:
            raise ValueError('Exactly one qualified component artifact required')
        s = matches[0]
        digest, now = _sha(s), _instant(self.clock())
        reviews = [r for r in self._reviews if r.get('evidence_sha256') == digest]
        if len(reviews) != 1 or (self._withdrawals.root / ('withdrawn-' + digest + '.json')).exists():
            raise ValueError('Independent current component review required')
        review = reviews[0]
        if (set(review) != _REVIEW_FIELDS
                or review['withdrawal_registry_sha256'] != self._registry_hash()
                or any(not isinstance(review[k], str) or not review[k].strip()
                       for k in ('reviewer', 'reference'))
                or not _instant(review['reviewed_at']) <= now < _instant(review['expires_at'])):
            raise ValueError('Invalid or expired review')
        config = self._configurations[identity]
        if (set(s) != _FIELDS or type(s['schema_version']) is not int or s['schema_version'] != 1
                or s['mechanism'] != 'qualified_components_v1'
                or s['credential_sha256'] != self._credential
                or s['configuration_sha256'] != _sha(config)
                or s['endpoint'] not in (CHAT, DECISIONS)
                or config.get('backend') == 'local'
                or any(marker in identity for marker in (':', '~', '*'))
                or identity.startswith(('openrouter/', 'typesafe/jev-router'))
                or not _instant(s['issued_at']) <= review['reviewed_at'] <= now < _instant(s['expires_at'])
                or review['expires_at'] > s['expires_at']
                or not 0 < _units(s['max_payload_bytes']) <= 2 ** 20
                or not isinstance(s['claims'], list) or set(s['claims']) != REQUIRED_CLAIMS
                or len(s['claims']) != len(REQUIRED_CLAIMS)
                or not isinstance(s['sources'], dict) or set(s['sources']) != _SOURCES
                or any(not isinstance(v, str) or not _DIGEST.fullmatch(v) for v in s['sources'].values())):
            raise ValueError('Incomplete qualified component evidence')
        c = s['components']
        if not isinstance(c, dict) or set(c) != COMPONENTS:
            raise ValueError('Every billing dimension must be explicit')
        for component in c.values():
            if not isinstance(component, dict) or set(component) != {'max_units', 'unit_usd'}:
                raise ValueError('Complete quantity and rate pair required')
            _units(component['max_units'])
            _rate(component['unit_usd'])
        units = {k: v['max_units'] for k, v in c.items()}
        decision = s['endpoint'] == DECISIONS
        if (units['input'] <= 0 or units['output'] <= 0
                or units['cache_read'] != units['input'] or units['cache_write'] != units['input']
                or units['request'] != 1 or units['failure'] != 1
                or units['classification'] != int(decision)
                or (not decision and _rate(c['classification']['unit_usd']) != 0)
                or s['output_control'] != ('qualified_endpoint_cap' if decision else 'request_max_tokens')
                or s['reasoning_mode'] not in ('included_in_output', 'separate_endpoint_cap')
                or (s['reasoning_mode'] == 'included_in_output'
                    and (units['reasoning'] != 0 or _rate(c['reasoning']['unit_usd']) != 0))):
            raise ValueError('Inconsistent endpoint or billing controls')
        with localcontext() as context:
            # Rates have <=128 characters and quantities <=10 digits: this sum
            # remains exact even under an application's low Decimal precision.
            context.prec = 512
            ceiling = sum((_rate(v['unit_usd']) * v['max_units'] for v in c.values()), Decimal(0))
        if (self._withdrawals.root / ('withdrawn-' + digest + '.json')).exists():
            raise ValueError('Component review withdrawn during verification')
        now = _instant(self.clock())
        if not s['issued_at'] <= now < s['expires_at'] or not review['reviewed_at'] <= now < review['expires_at']:
            raise ValueError('Component evidence expired during verification')
        return s, review, digest, str(ceiling)

    def bound_for(self, identity):
        """Return a derived bound only after full trusted-artifact validation."""
        _, _, digest, ceiling = self._qualified(identity)
        return EnforcedBound(ceiling, 'component-cap:' + digest)

    def _bound(self, identity, bound):
        s, review, digest, ceiling = self._qualified(identity)
        if bound.evidence != 'component-cap:' + digest or money(bound.usd) < money(ceiling):
            raise ValueError('Bound does not cover the qualified component sum')
        return s, review, digest, ceiling

    def __call__(self, kind, identity, payload, bound):
        try:
            self._bound(identity, bound)
            if kind != 'model' or _encoded(payload) != _encoded(self._configurations[identity]):
                raise ValueError('Pinned model configuration required')
            return True
        except Exception:
            return self._deny('component_preflight_unqualified')

    def verify_dispatch(self, client, endpoint, payload, bound):
        try:
            lease_id = _sha({'endpoint': endpoint, 'payload': payload})
            self._leases.pop(lease_id, None)
            identity = payload['model']
            s, review, digest, ceiling = self._bound(identity, bound)
            config = self._configurations[identity]
            if (client is not self._client or endpoint != s['endpoint']
                    or len(_encoded(payload)) > s['max_payload_bytes']):
                raise ValueError('Pinned endpoint and bounded request required')
            provider = {'only': [config['provider']], 'allow_fallbacks': False}
            if endpoint == CHAT:
                provider['require_parameters'] = True
                if (set(payload) - {'session_id'} != {'model', 'provider', 'messages', 'max_tokens', 'reasoning'}
                        or type(payload.get('max_tokens')) is not int
                        or not 0 < payload['max_tokens'] <= s['components']['output']['max_units']
                        or payload['reasoning'] != {'effort': config['effort']}
                        or ('session_id' in payload and not isinstance(payload['session_id'], str))
                        or not isinstance(payload['messages'], list) or not payload['messages']
                        or any(not isinstance(m, dict) or set(m) != {'role', 'content'}
                               or m['role'] not in ('system', 'user', 'assistant')
                               or not isinstance(m['content'], str) for m in payload['messages'])):
                    raise ValueError('Only bounded text chat supported')
            else:
                if (set(payload) != {'model', 'provider', 'state', 'questions'}
                        or not isinstance(payload['state'], dict) or not _text_json(payload['state'])
                        or not isinstance(payload['questions'], dict) or len(payload['questions']) != 1):
                    raise ValueError('Only one bounded text decision supported')
                question = next(iter(payload['questions'].values()))
                if (not isinstance(question, dict) or set(question) != {'type', 'instructions', 'criteria'}
                        or question['type'] != 'choice' or not isinstance(question['instructions'], str)
                        or not isinstance(question['criteria'], dict) or len(question['criteria']) != 2
                        or any(not isinstance(k, str) or not isinstance(v, str)
                               for k, v in question['criteria'].items())):
                    raise ValueError('Only binary choice fee scope qualified')
            if payload['provider'] != provider:
                raise ValueError('Provider fallback or changes forbidden')
            if len(self._leases) >= 1024:
                self._leases.pop(next(iter(self._leases)))
            self._leases[lease_id] = {'identity': identity, 'evidence': digest,
                                      'bound_usd': bound.usd, 'bound_evidence': bound.evidence}
            self.last_check = {'allowed': True, 'reason': 'qualified_components', 'ceiling_usd': ceiling}
            return True
        except Exception:
            return self._deny('component_dispatch_unqualified')

    def verify_post(self, client, endpoint, payload, bound):
        """One-use exact request lease, checked after intent with no external I/O."""
        try:
            lease = self._leases.pop(_sha({'endpoint': endpoint, 'payload': payload}))
            _, _, digest, _ = self._bound(lease['identity'], bound)
            if (client is not self._client or digest != lease['evidence']
                    or bound.usd != lease['bound_usd'] or bound.evidence != lease['bound_evidence']):
                raise ValueError('Dispatch lease changed')
            return True
        except Exception:
            return self._deny('component_lease_expired_revoked_or_changed')

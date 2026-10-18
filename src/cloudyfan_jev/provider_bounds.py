"""Supplier enforcement verification; account credit is not a per-call hard cap.

No supplier contracts or credentials ship here. The signed contract/control-state
seam requires an independently reviewed supplier integration; OpenRouter's normal
key metadata alone never authorizes paid execution. Constructors perform no I/O.
"""
from copy import deepcopy
from datetime import datetime
import hashlib
import hmac
import math
from time import time

from .client import Client, CHAT, DECISIONS, _strict_json
from .budget import money
from .receipt_store import _encoded

KEY = 'https://openrouter.ai/api/v1/key'
ALL_CHARGES = frozenset({'generation', 'reasoning', 'classification', 'advisor', 'plugins', 'failure'})


def _sha(value):
    return hashlib.sha256(_encoded(value)).hexdigest()


def _instant(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError('Finite timestamp required')
    return value


def credential_fingerprint(key):
    return hashlib.sha256(('cloudyfan-provider-key-v1:' + key).encode()).hexdigest()


def _amount(value):
    if type(value) is int:
        value = str(value)
    return money(value)


def _timestamp(value):
    if not isinstance(value, str):
        raise ValueError('Explicit timezone required')
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('Explicit timezone required')
    return parsed.timestamp()


class OpenRouterBoundVerifier:
    """Fail-closed verifier pinned to an exact Client and its credential.

    Trusted supplier contracts and an authenticated current enforcement probe are
    separate from the ordinary key-credit check. See docs/provider-bounds.md.
    """
    def __init__(self, client, *, clock=time, contracts=(), issuer_keys=None,
                 configurations=None, request_policies=None, enforcement_probe=None,
                 max_state_age_seconds=30):
        if not isinstance(client, Client):
            raise ValueError('Exact OpenRouter Client required')
        self._client, self._http = client, client.http
        self._credential = credential_fingerprint(client.key)
        self.clock = clock
        self._contracts = deepcopy(tuple(contracts))
        self._issuer_keys = dict(issuer_keys or {})
        self._configurations = deepcopy(configurations or {})
        self._policies = deepcopy(request_policies or {})
        self._probe = enforcement_probe
        self._leases = {}
        if (isinstance(max_state_age_seconds, bool) or not math.isfinite(max_state_age_seconds)
                or not 0 < max_state_age_seconds <= 60):
            raise ValueError('Finite short enforcement freshness required')
        if any(not isinstance(k, bytes) or len(k) < 32 for k in self._issuer_keys.values()):
            raise ValueError('Supplier verification keys must be at least 32 bytes')
        self._max_age = max_state_age_seconds
        self.last_check = {'allowed': False, 'reason': 'not_checked'}

    def _deny(self, reason):
        self.last_check = {'allowed': False, 'reason': reason}
        return False

    def key_status(self):
        """Explicit read-only key query, never a model POST or a cap certificate."""
        try:
            if credential_fingerprint(self._client.key) != self._credential:
                raise ValueError('Credential changed')
            response = self._client.http.get(KEY,
                headers={'Authorization': 'Bearer ' + self._client.key}, timeout=15, follow_redirects=False)
            if response.status_code != 200 or len(response.content) > 65536:
                raise ValueError('Unavailable key state')
            data = _strict_json(response.text)['data']
            limit = None if data['limit'] is None else str(_amount(data['limit']))
            remaining = None if data['limit_remaining'] is None else str(_amount(data['limit_remaining']))
            usage = str(_amount(data['usage']))
            if (data.get('disabled') is True or data['limit_reset'] is not None
                    or data.get('include_byok_in_limit') is not True
                    or (data.get('expires_at') is not None and _timestamp(data['expires_at']) <= self.clock())
                    or limit is None or remaining is None or money(remaining) <= 0
                    or money(remaining) > money(limit) or money(usage) > money(limit)):
                raise ValueError('Unqualified credential budget')
            return {'available': True, 'credit_limit_usd': limit, 'remaining_usd': remaining,
                    'credential_sha256': self._credential, 'hard_per_request_cap': False}
        except Exception:
            return {'available': False, 'reason': 'key_state_unavailable_or_unqualified',
                    'hard_per_request_cap': False}

    def descriptor(self):
        return {'version': 'supplier-ceiling-v1', 'credential_sha256': self._credential,
                'contracts_sha256': _sha(self._contracts),
                'configurations_sha256': _sha(self._configurations),
                'request_policies_sha256': _sha(self._policies),
                'issuer_keys_sha256': _sha({k: hashlib.sha256(v).hexdigest()
                                           for k, v in self._issuer_keys.items()}),
                'max_state_age_seconds': self._max_age}

    def _authenticated(self, envelope, issuer=None):
        if not isinstance(envelope, dict) or set(envelope) != {'body', 'signature'}:
            raise ValueError('Authenticated supplier statement required')
        body = envelope['body']
        if not isinstance(body, dict) or not isinstance(body.get('issuer'), str):
            raise ValueError('Supplier issuer required')
        if issuer is not None and body['issuer'] != issuer:
            raise ValueError('Statement issuer mismatch')
        key = self._issuer_keys.get(body['issuer'])
        signature = envelope['signature']
        if key is None or not isinstance(signature, str) or not hmac.compare_digest(
                hmac.new(key, _encoded(body), hashlib.sha256).hexdigest(), signature):
            raise ValueError('Supplier statement authentication failed')
        return body

    def _contract(self, identity, bound):
        fields = {'schema_version', 'id', 'issuer', 'credential_sha256', 'model_version',
                  'configuration_sha256', 'request_policy_sha256', 'endpoint', 'mechanism',
                  'ceiling_usd', 'covers', 'issued_at', 'expires_at', 'enforcement_sha256', 'evidence_sha256'}
        matches = []
        for envelope in self._contracts:
            body = self._authenticated(envelope)
            if body.get('model_version') != identity:
                continue
            if set(body) != fields or type(body['schema_version']) is not int or body['schema_version'] != 1:
                raise ValueError('Supplier contract shape invalid')
            now = _instant(self.clock())
            if (not isinstance(body['id'], str) or not body['id'].strip()
                    or body['credential_sha256'] != self._credential
                    or body['configuration_sha256'] != _sha(self._configurations[identity])
                    or body['request_policy_sha256'] != _sha(self._policies[identity])
                    or body['endpoint'] not in (CHAT, DECISIONS)
                    or body['mechanism'] != 'provider_all_charge_per_request_ceiling'
                    or set(body['covers']) != ALL_CHARGES or len(body['covers']) != len(ALL_CHARGES)
                    or not _instant(body['issued_at']) <= now < _instant(body['expires_at'])
                    or _amount(body['ceiling_usd']) > money(bound.usd)
                    or bound.evidence != 'supplier-cap:' + body['id']
                    or any(not isinstance(body[k], str) or len(body[k]) != 64
                           or any(c not in '0123456789abcdef' for c in body[k])
                           for k in ('enforcement_sha256', 'evidence_sha256'))):
                raise ValueError('Unqualified supplier enforcement contract')
            matches.append(body)
        if len(matches) != 1:
            raise ValueError('Exactly one matching supplier contract required')
        return matches[0]

    def _current(self, contract):
        # The configured probe must read an independently qualified supplier
        # enforcement control plane, not synthesize a statement from local prices.
        if self._probe is None:
            raise ValueError('Supplier enforcement probe unavailable')
        state = self._authenticated(self._probe(deepcopy(contract)), contract['issuer'])
        fields = {'schema_version', 'issuer', 'contract_sha256', 'active', 'credential_sha256',
                  'enforcement_sha256', 'ceiling_usd', 'covers', 'observed_at', 'expires_at'}
        now = _instant(self.clock())
        if (set(state) != fields or type(state['schema_version']) is not int or state['schema_version'] != 1
                or state['active'] is not True or state['contract_sha256'] != _sha(contract)
                or state['credential_sha256'] != self._credential
                or state['enforcement_sha256'] != contract['enforcement_sha256']
                or _amount(state['ceiling_usd']) != _amount(contract['ceiling_usd'])
                or set(state['covers']) != ALL_CHARGES or len(state['covers']) != len(ALL_CHARGES)
                or not 0 <= now - _instant(state['observed_at']) <= self._max_age
                or not now < _instant(state['expires_at']) <= contract['expires_at']):
            raise ValueError('Enforcement inactive, stale or mismatched')
        if self.key_status().get('available') is not True:
            raise ValueError('Credential budget unavailable')
        now = _instant(self.clock())
        if (not contract['issued_at'] <= now < contract['expires_at']
                or not 0 <= now - state['observed_at'] <= self._max_age
                or now >= state['expires_at']):
            raise ValueError('Supplier ceiling expired during verification')
        self.last_check = {'allowed': True, 'reason': 'authenticated_supplier_ceiling',
                           'contract_id': contract['id'], 'ceiling_usd': contract['ceiling_usd']}
        return deepcopy(state)

    def __call__(self, kind, identity, payload, bound):
        if not self._contracts or self._probe is None:
            return self._deny('supplier_ceiling_unconfirmed')
        try:
            if (kind != 'model' or identity not in self._configurations
                    or _encoded(payload) != _encoded(self._configurations[identity])
                    or payload.get('backend') == 'local'
                    or credential_fingerprint(self._client.key) != self._credential
                    or self._client.http is not self._http):
                raise ValueError('Pinned cloud preflight configuration required')
            self._current(self._contract(identity, bound))
            return True
        except Exception:
            return self._deny('supplier_enforcement_unqualified')

    def verify_dispatch(self, client, endpoint, payload, bound):
        lease_id = _sha({'endpoint': endpoint, 'payload': payload})
        self._leases.pop(lease_id, None)
        try:
            identity = payload['model']
            contract = self._contract(identity, bound)
            config, policy = self._configurations[identity], self._policies[identity]
            if (client is not self._client or client.http is not self._http
                    or credential_fingerprint(client.key) != self._credential
                    or endpoint != contract['endpoint'] or endpoint != policy['endpoint']
                    or set(policy) != {'endpoint', 'max_tokens', 'max_payload_bytes', 'tools'}
                    or type(policy['max_payload_bytes']) is not int or policy['max_payload_bytes'] <= 0
                    or len(_encoded(payload)) > policy['max_payload_bytes']):
                raise ValueError('Pinned transport/endpoint/request required')
            provider = {'only': [config['provider']], 'allow_fallbacks': False}
            if endpoint == CHAT:
                provider['require_parameters'] = True
                if (set(payload) - {'model', 'messages', 'max_tokens', 'reasoning', 'session_id', 'tools', 'tool_choice'}
                        != {'provider'} or 'provider' not in payload
                        or type(payload.get('max_tokens')) is not int
                        or type(policy['max_tokens']) is not int
                        or not 0 < payload['max_tokens'] <= policy['max_tokens']
                        or payload.get('reasoning') != {'effort': config['effort']}
                        or payload.get('tools', []) != policy['tools']
                        or ('tool_choice' in payload and payload['tool_choice'] != 'auto')
                        or not isinstance(payload.get('messages'), list)):
                    raise ValueError('Unqualified completion parameters')
            elif (set(payload) != {'model', 'state', 'questions', 'provider'}
                  or policy['max_tokens'] is not None or policy['tools']):
                raise ValueError('Unqualified decision parameters')
            if payload.get('provider') != provider:
                raise ValueError('Provider changes and fallback forbidden')
            state = self._current(contract)
            if len(self._leases) >= 1024:
                self._leases.pop(next(iter(self._leases)))
            self._leases[lease_id] = {'contract': deepcopy(contract), 'state': state,
                                      'bound_usd': bound.usd, 'evidence': bound.evidence}
            return True
        except Exception:
            return self._deny('supplier_dispatch_unqualified')


    def verify_post(self, client, endpoint, payload, bound):
        """Consume a request lease after durable intent, immediately before POST.

        No I/O is allowed here: a blocking control-plane query could itself age
        the evidence. Actual supplier enforcement must also cover in-flight calls.
        """
        try:
            lease = self._leases.pop(_sha({'endpoint': endpoint, 'payload': payload}))
            contract, state = lease['contract'], lease['state']
            if (client is not self._client or client.http is not self._http
                    or credential_fingerprint(client.key) != self._credential
                    or bound.usd != lease['bound_usd'] or bound.evidence != lease['evidence']):
                raise ValueError('Transport lease changed')
            now = _instant(self.clock())
            if (not contract['issued_at'] <= now < contract['expires_at']
                    or not 0 <= now - state['observed_at'] <= self._max_age
                    or now >= state['expires_at']):
                raise ValueError('Dispatch lease expired')
            return True
        except Exception:
            return self._deny('supplier_dispatch_lease_expired_or_changed')

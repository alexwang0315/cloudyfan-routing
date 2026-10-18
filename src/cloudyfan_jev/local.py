"""Operator-pinned loopback OpenAI text-completions subset, never cloud billing.

A configured no-fee evidence reference is an operator attestation, not proof of
model quality, physical location or hardware cost. The endpoint cannot redirect.
"""
from copy import deepcopy
import hashlib
import re
from time import perf_counter
from urllib.parse import urlsplit

from .client import UnresolvedCallsError, _now, _strict_json
from .receipt_store import ReceiptStore, _encoded, _immutable, _read


class LocalBackend(ReceiptStore):
    def __init__(self, http, root, *, endpoint, model_version, quantization, engine,
                 execution_location, no_model_fee_evidence):
        parsed = urlsplit(endpoint)
        if (parsed.scheme != 'http' or parsed.hostname not in {'localhost', '127.0.0.1', '::1'}
                or parsed.path != '/v1/chat/completions' or parsed.username or parsed.password
                or parsed.query or parsed.fragment):
            raise ValueError('Local backend requires a loopback HTTP text-completions endpoint')
        config = dict(endpoint=endpoint, model_version=model_version, quantization=quantization,
                      engine=engine, execution_location=execution_location,
                      no_model_fee_evidence=no_model_fee_evidence, protocol='openai-text-nonstream-v1')
        if any(not isinstance(v, str) or not v.strip() for v in config.values()):
            raise ValueError('Local backend configuration and no-fee evidence must be explicit')
        if not re.fullmatch(r'(home|company):[A-Za-z0-9_.-]+', execution_location):
            raise ValueError('Local execution location requires an explicit home or company site')
        super().__init__(root)
        self.http, self.config = http, config

    def descriptor(self):
        return deepcopy(self.config)

    def matches_profile(self, profile):
        return all(profile.get(k) == self.config[k] for k in
                   ('model_version', 'quantization', 'engine', 'execution_location'))

    def call(self, cid, payload):
        if payload.get('model') != self.config['model_version']:
            raise ValueError('Local request model does not match pinned configuration')
        if payload.get('tools') or any(m.get('role') not in {'system', 'user', 'assistant'} or
                                      not isinstance(m.get('content'), str) for m in payload['messages']):
            raise ValueError('Local text subset does not support tools, media or nontext messages')
        request = {'model': payload['model'], 'messages': payload['messages'],
                   'max_tokens': payload['max_tokens'], 'stream': False}
        directory = self._directory(cid)
        fingerprint = hashlib.sha256(_encoded({'configuration': self.config, 'request': request})).hexdigest()
        with self._lock():
            if (directory / 'intent.json').exists():
                if _read(directory / 'intent.json')['request_sha256'] != fingerprint:
                    raise ValueError('Local resume configuration or request changed')
                if (directory / 'response.json').exists():
                    return self._load(directory)
                raise UnresolvedCallsError('Local request has unresolved execution; not resent')
            directory.mkdir(mode=0o700)
            common = {'execution_kind': 'local', 'call_id': cid, 'local_call_id': cid,
                      'run_id': cid.rsplit('-a', 1)[0].removeprefix('run-'),
                      'request_sha256': fingerprint, 'configuration': self.descriptor()}
            _immutable(directory / 'intent.json', {**common, 'url': self.config['endpoint'],
                       'payload': request, 'started_at': _now()})
            started = perf_counter()
            try:
                response = self.http.post(self.config['endpoint'], json=request,
                                          timeout=180, follow_redirects=False)
                record = {**common, 'http_status': response.status_code, 'raw': response.text}
            except Exception:
                record = {**common, 'http_status': None, 'raw': None, 'interrupted': True,
                          'error': 'local_transport_failure'}
            record.update(elapsed_ms=(perf_counter() - started) * 1000, finished_at=_now())
            _immutable(directory / 'response.json', record)
            return record

    def checked(self, record):
        info = local_usage(record)
        if record.get('configuration') != self.config or record.get('http_status') != 200:
            raise RuntimeError('Local execution stopped: configuration or transport evidence mismatch')
        if info['model'] != self.config['model_version']:
            raise RuntimeError('Local execution stopped: model identity mismatch')
        try:
            data = _strict_json(record['raw'])
            choice = data['choices'][0]
            message = choice['message']
            if (len(data['choices']) != 1 or message.get('role') != 'assistant'
                    or not isinstance(message.get('content'), str) or message.get('tool_calls')):
                raise ValueError('Unsupported local response')
        except (ValueError, TypeError, KeyError, IndexError):
            raise RuntimeError('Local execution stopped: unsupported or malformed response') from None
        return info

    def reconcile(self, cid):
        raise ValueError('Local execution has no cloud generation receipt to reconcile')

    def usage(self, record):
        return local_usage(record)


def local_usage(record):
    config = record.get('configuration', {})
    try:
        data = _strict_json(record.get('raw'))
        if not isinstance(data, dict): data = {}
    except (ValueError, TypeError):
        data = {}
    raw_usage = data.get('usage')
    valid_usage = isinstance(raw_usage, dict) and all(type(raw_usage.get(k)) is int and raw_usage[k] >= 0
                      for k in ('prompt_tokens', 'completion_tokens'))
    known_zero = (record.get('execution_kind') == 'local'
                  and isinstance(config.get('no_model_fee_evidence'), str)
                  and bool(config['no_model_fee_evidence'].strip()))
    return {'generation_id': None, 'provider': None, 'model': data.get('model'),
            'execution_kind': 'local', 'local_call_id': record.get('local_call_id'),
            'run_id': record.get('run_id'),
            'model_version': config.get('model_version'), 'quantization': config.get('quantization'),
            'engine': config.get('engine'), 'execution_location': config.get('execution_location'),
            'evidence_source': config.get('no_model_fee_evidence'),
            'usage': raw_usage if valid_usage else {}, 'usage_unknown': not valid_usage,
            'cost_usd': '0' if known_zero else None, 'cost_unknown': not known_zero,
            'cost_source': 'local_no_model_fee_contract' if known_zero else 'unknown',
            'host_usd': None, 'electricity_usd': None, 'elapsed_ms': record.get('elapsed_ms')}

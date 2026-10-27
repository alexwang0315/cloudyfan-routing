"""Bounded creation observations; no runner, dispatch, health or model capability."""
from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import time
from types import MappingProxyType


SCHEMA = 'creation-task-features-v1'
POLICY = {
    'feature_version': 'local-deterministic-v1',
    'data_policy_version': 'creation-feature-data-v1',
    'max_input_utf8_bytes': 32768,
    'max_feature_items': 16,
    'max_reasons_per_item': 4,
    'max_reason_code_characters': 80,
    'max_record_bytes': 16384,
    'max_compute_milliseconds': 50,
}
POLICIES = {POLICY['feature_version']: POLICY,
            'local-deterministic-v2': {**POLICY, 'feature_version': 'local-deterministic-v2'}}
BINDING_FIELDS = ('client', 'run', 'fingerprint', 'pool', 'mode', 'category', 'contract', 'created')
_OWNER = ContextVar('creation_feature_owner', default=None)
_COMPUTE = ContextVar('creation_feature_compute_budget', default=None)


class _ComputeBudget:
    def __init__(self, clock):
        self.clock, self.elapsed, self.depth, self.failed = clock, 0.0, 0, False

    @contextmanager
    def measure(self):
        outer, started = self.depth == 0, None
        self.depth += 1
        if outer:
            try:
                started = self.clock()
                if not _finite(started):
                    raise ValueError('Invalid compute clock')
            except Exception:
                self.failed = True
        try:
            yield
        finally:
            self.depth -= 1
            if outer:
                try:
                    finished = self.clock()
                    if started is None or not _finite(finished) or finished < started:
                        raise ValueError('Invalid compute elapsed time')
                    self.elapsed += finished - started
                except Exception:
                    self.failed = True

    def available(self):
        return not self.failed and math.isfinite(self.elapsed) and 0 <= self.elapsed <= POLICY['max_compute_milliseconds'] / 1000


@contextmanager
def feature_computation():
    """Measure only new pure metadata work, excluding database/file I/O."""
    budget = _COMPUTE.get()
    if budget is None:
        yield
    else:
        with budget.measure():
            yield


def feature_budget_available():
    budget = _COMPUTE.get()
    return budget is None or budget.available()


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False,
                      separators=(',', ':')).encode()


def feature_digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def original_digest(value):
    """Match the original TaskService request/registered serialization."""
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     allow_nan=False).encode()).hexdigest()


def _hash(value):
    return isinstance(value, str) and re.fullmatch(r'[a-f0-9]{64}', value) is not None


def _identifier(value):
    return isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}', value) is not None


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def unavailable():
    return {'schema_version': SCHEMA, 'status': 'unavailable',
            'reason': 'creation_feature_evidence_unavailable'}


def not_provided():
    return {'schema_version': SCHEMA, 'status': 'not_provided',
            'reason': 'creation_feature_protocol_not_configured'}


def fixed_policy(value):
    if not isinstance(value, dict):
        raise ValueError('Invalid feature policy')
    expected = POLICIES.get(value.get('feature_version'))
    if (expected is None or set(value) != set(expected)
            or any(type(value[key]) is not type(expected[key]) or value[key] != expected[key] for key in expected)):
        raise ValueError('Unregistered feature policy')
    return value


def validate_adoption(value):
    keys = {'schema_version', 'binding', 'policy', 'policy_sha256',
            'request_sha256', 'registered_sha256'}
    if not isinstance(value, dict) or set(value) != keys or value['schema_version'] != 'creation-feature-adoption-v1':
        raise ValueError('Invalid creation feature adoption')
    binding = value['binding']
    if (not isinstance(binding, dict) or set(binding) != set(BINDING_FIELDS)
            or any(not _identifier(binding[key]) for key in ('client', 'run', 'mode', 'category', 'contract'))
            or any(not _hash(binding[key]) for key in ('pool', 'fingerprint'))
            or not _finite(binding['created'])
            or value['policy_sha256'] != feature_digest(fixed_policy(value['policy']))
            or not _hash(value['request_sha256']) or not _hash(value['registered_sha256'])
            or len(encoded(value)) > POLICY['max_record_bytes']):
        raise ValueError('Unconfirmed creation feature adoption')
    return value


def public_features(value):
    """Validate an already saved safe projection; never construct missing facts."""
    if value == unavailable() or value == not_provided():
        return value
    keys = {'schema_version', 'status', 'feature_version', 'data_policy_version', 'captured_at',
            'policy_sha256', 'adoption_sha256', 'binding', 'features'}
    policy = POLICIES.get(value.get('feature_version')) if isinstance(value, dict) else None
    if (not isinstance(value, dict) or set(value) != keys or value['schema_version'] != SCHEMA
            or value['status'] != 'available' or policy is None
            or value['data_policy_version'] != POLICY['data_policy_version']
            or not _finite(value['captured_at']) or value['policy_sha256'] != feature_digest(policy)
            or not _hash(value['adoption_sha256']) or len(encoded(value)) > POLICY['max_record_bytes']):
        raise ValueError('Invalid saved feature projection')
    binding = value['binding']
    if (not isinstance(binding, dict)
            or set(binding) != {*BINDING_FIELDS, 'internal_id', 'request_sha256', 'registered_sha256'}
            or not re.fullmatch(r'http-[a-f0-9]{48}', binding.get('internal_id', ''))
            or any(not _identifier(binding[key]) for key in ('client', 'run', 'mode', 'category', 'contract'))
            or any(not _hash(binding[key]) for key in ('pool', 'fingerprint', 'request_sha256', 'registered_sha256'))
            or not _finite(binding['created'])):
        raise ValueError('Invalid saved feature binding')
    items = value['features']
    required_count = 3 if value['feature_version'] == POLICY['feature_version'] else 16
    if not isinstance(items, list) or len(items) != required_count:
        raise ValueError('Unknown feature items')
    expected = {
        'input_utf8_bytes': ('deterministic', 'utf8_bytes', 'accepted_request'),
        'input_codepoints': ('deterministic', 'unicode_codepoints', 'accepted_request'),
        'input_tokens': ('unknown', 'tokens', 'not_provided'),
    }
    seen = set()
    for item in items:
        if not isinstance(item, dict) or set(item) != {'id', 'kind', 'unit', 'source', 'value', 'reasons'}:
            raise ValueError('Invalid safe feature item')
        key = item['id']
        if (not isinstance(key, str) or key in seen or not isinstance(item['reasons'], list)
                or not 1 <= len(item['reasons']) <= POLICY['max_reasons_per_item']
                or any(not isinstance(reason, str) or re.fullmatch(r'[a-z0-9_]{1,80}', reason) is None
                       for reason in item['reasons'])):
            raise ValueError('Unknown safe feature item')
        seen.add(key)
        if key not in expected:
            if required_count != 16 or not _valid_additional(item, binding):
                raise ValueError('Unknown safe feature item')
        elif (item['kind'], item['unit'], item['source']) != expected[key]:
            raise ValueError('Invalid safe feature source')
        elif key == 'input_tokens':
            if item['value'] is not None or item['reasons'] != ['exact_tokenizer_not_provided']:
                raise ValueError('Unknown tokenizer must remain unknown')
        elif (type(item['value']) is not int or not 0 <= item['value'] <= POLICY['max_input_utf8_bytes']
                or item['reasons'] != ['measured_original_text']):
            raise ValueError('Invalid measured original text')
    return value


UNKNOWN_FIELDS = {
    'messages_count': ('messages', 'structured_messages_not_provided'),
    'reference_blocks_count': ('reference_blocks', 'structured_references_not_provided'),
    'output_schema': ('schema', 'output_schema_not_provided'),
    'modality_requirements': ('modalities', 'required_modalities_not_provided'),
    'deadline_requirements': ('seconds', 'execution_deadline_not_provided'),
    'fee_requirements': ('usd', 'whole_task_fee_requirement_not_provided'),
}


def _valid_additional(item, binding):
    key, value = item['id'], item['value']
    triple = item['kind'], item['unit'], item['source']
    if key in UNKNOWN_FIELDS:
        unit, reason = UNKNOWN_FIELDS[key]
        return triple == ('unknown', unit, 'not_provided') and value is None and item['reasons'] == [reason]
    if key in {'original_category', 'original_mode'}:
        field = 'category' if key == 'original_category' else 'mode'
        return (triple == ('explicit', field, 'trusted_binding') and value == binding[field]
                and item['reasons'] == ['original_binding'])
    if key == 'input_modality':
        return triple == ('deterministic', 'modality', 'accepted_request') and value == 'text' and item['reasons'] == ['plain_text_request']
    if key == 'tools_required':
        return ((triple == ('explicit', 'boolean', 'frozen_requirements') and type(value) is bool
                 and item['reasons'] == ['required_capabilities_only']) or
                (triple == ('unknown', 'boolean', 'not_provided') and value is None
                 and item['reasons'] == ['frozen_requirements_not_provided']))
    if key == 'context_requirements':
        return ((triple == ('explicit', 'context_tokens', 'frozen_requirements')
                 and type(value) is int and 0 < value <= 2**53-1 and item['reasons'] == ['required_context_not_measured_usage']) or
                (triple == ('unknown', 'context_tokens', 'not_provided') and value is None
                 and item['reasons'] == ['frozen_requirements_not_provided']))
    if key == 'data_location_policy':
        return triple == ('explicit', 'location_policy', 'frozen_contract') and value in ('local_only', 'unrestricted') and item['reasons'] == ['original_execution_policy']
    if key == 'runtime_limits':
        fields = {'max_context_bytes', 'max_tokens', 'max_tool_calls', 'max_turns'}
        return (triple == ('explicit', 'runtime_limits', 'frozen_contract') and isinstance(value, dict)
                and set(value) == fields and all(type(v) is int and 0 < v <= 2**53-1 for v in value.values())
                and item['reasons'] == ['caps_are_not_requirements_or_measured_usage'])
    return False


def _items(prompt, adoption, registered):
    count = len(prompt.encode('utf-8'))
    if count > POLICY['max_input_utf8_bytes']:
        raise ValueError('Feature input exceeds fixed bound')
    result = [{'id': key, 'kind': 'deterministic', 'unit': unit, 'source': 'accepted_request',
               'value': value, 'reasons': ['measured_original_text']}
              for key, unit, value in [('input_utf8_bytes', 'utf8_bytes', count),
                                      ('input_codepoints', 'unicode_codepoints', len(prompt))]]
    result.append({'id': 'input_tokens', 'kind': 'unknown', 'unit': 'tokens', 'source': 'not_provided',
                   'value': None, 'reasons': ['exact_tokenizer_not_provided']})
    if adoption['policy']['feature_version'] == POLICY['feature_version']:
        return result
    def add(key, kind, unit, source, value, reason):
        result.append({'id': key, 'kind': kind, 'unit': unit, 'source': source,
                       'value': value, 'reasons': [reason]})
    for key, field in (('original_category', 'category'), ('original_mode', 'mode')):
        add(key, 'explicit', field, 'trusted_binding', adoption['binding'][field], 'original_binding')
    for key, (unit, reason) in UNKNOWN_FIELDS.items():
        add(key, 'unknown', unit, 'not_provided', None, reason)
    add('input_modality', 'deterministic', 'modality', 'accepted_request', 'text', 'plain_text_request')
    plans = [registered.get(key, {}).get('requirements') for key in ('backup_plan', 'selection_policy')]
    present = [value for value in plans if isinstance(value, dict)]
    requirements = present[0] if len(present) == 1 else None
    capabilities = requirements.get('capabilities') if requirements is not None else None
    if isinstance(capabilities, list) and all(isinstance(value, str) for value in capabilities):
        add('tools_required', 'explicit', 'boolean', 'frozen_requirements', 'tools' in capabilities, 'required_capabilities_only')
    else:
        add('tools_required', 'unknown', 'boolean', 'not_provided', None, 'frozen_requirements_not_provided')
    context = requirements.get('context_tokens') if requirements is not None else None
    if type(context) is int and 0 < context <= 2**53-1:
        add('context_requirements', 'explicit', 'context_tokens', 'frozen_requirements', context, 'required_context_not_measured_usage')
    else:
        add('context_requirements', 'unknown', 'context_tokens', 'not_provided', None, 'frozen_requirements_not_provided')
    add('data_location_policy', 'explicit', 'location_policy', 'frozen_contract', registered['execution_policy']['mode'], 'original_execution_policy')
    add('runtime_limits', 'explicit', 'runtime_limits', 'frozen_contract',
        {key: registered['limits'][key] for key in ('max_context_bytes', 'max_tokens', 'max_tool_calls', 'max_turns')},
        'caps_are_not_requirements_or_measured_usage')
    return result


def _read_json(path):
    path = Path(path).absolute()
    if path != path.resolve():
        raise ValueError('Unsafe feature path')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > POLICY['max_record_bytes']:
            raise ValueError('Unsafe or oversized feature record')
        body = stream.read(POLICY['max_record_bytes'] + 1)
    if len(body) > POLICY['max_record_bytes']:
        raise ValueError('Oversized feature record')
    def unique(pairs):
        result = {}
        for key, item in pairs:
            if key in result:
                raise ValueError('Duplicate feature field')
            result[key] = item
        return result
    return body, json.loads(body, object_pairs_hook=unique)


def _write_new(path, body):
    path = Path(path).absolute()
    if len(body) > POLICY['max_record_bytes'] or path != path.resolve():
        raise ValueError('Unsafe feature output')
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError('Unsafe feature output')
        stream.write(body)
        stream.flush()
        os.fsync(stream.fileno())
    descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class FeatureRecorder:
    """One first-owner capture, independent host seal, then read-only evidence."""
    def __init__(self, root, configuration, contracts):
        self.root = Path(root).absolute()
        self.records = self.root / '.task-features'
        self.policy, self.port, self.registered = None, None, {}
        self.compute_clock = time.monotonic
        try:
            if (not {'policy_bundle', 'binding_port'} <= set(configuration)
                    or set(configuration) - {'policy_bundle', 'binding_port', 'compute_clock'}):
                raise ValueError('Unknown host feature configuration')
            clock = configuration.get('compute_clock', time.monotonic)
            if not callable(clock):
                raise ValueError('External compute clock must be callable')
            policy = dict(configuration['policy_bundle'])
            fixed_policy(policy)
            port = configuration['binding_port']
            if not callable(port.snapshot) or not callable(port.seal):
                raise ValueError('Controlled feature binding port required')
            self.registered = MappingProxyType({name: original_digest(contract.descriptor())
                                               for name, contract in contracts.items()})
            self.policy, self.port = MappingProxyType(policy), port
            self.compute_clock = clock
        except Exception:
            # Invalid auxiliary configuration does not reject an original task.
            pass

    @contextmanager
    def operation(self):
        token = _COMPUTE.set(_ComputeBudget(self.compute_clock))
        try:
            yield
        finally:
            _COMPUTE.reset(token)

    @contextmanager
    def submitting(self, binding, created):
        owner = None
        if created is True and self.port is not None:
            try:
                snapshot = self.port.snapshot(binding['client'], binding['run'])
                with feature_computation():
                    adoption = validate_adoption(snapshot['adoption'])
                    if (adoption['binding'] != {key: binding[key] for key in BINDING_FIELDS}
                            or snapshot['adoption_sha256'] != feature_digest(adoption) or snapshot['seal'] is not None):
                        raise ValueError('First binding is not confirmed')
                owner = {'recorder': self, 'adoption': adoption, 'adoption_sha256': snapshot['adoption_sha256'],
                         'queue_new': None, 'accepted_at': None, 'attempted': False, 'seal_attempted': False}
            except Exception:
                pass
        token = _OWNER.set(owner)
        try:
            yield
        finally:
            _OWNER.reset(token)

    @contextmanager
    def accepting(self, created, accepted_at):
        owner = _OWNER.get()
        if owner is None or owner['recorder'] is not self:
            yield
            return
        previous = owner['queue_new'], owner['accepted_at']
        owner['queue_new'], owner['accepted_at'] = created is True, accepted_at
        try:
            yield
        finally:
            owner['queue_new'], owner['accepted_at'] = previous

    def capture(self, principal, run_id, directory, job, accepted_at, *, controlled_queue_required=False):
        """Called only inside the original new-job task lock, before dispatch."""
        owner = _OWNER.get()
        if (owner is None or owner['recorder'] is not self or owner['attempted']
                or owner['queue_new'] is False
                or controlled_queue_required and owner['queue_new'] is not True):
            return
        owner['attempted'] = True
        try:
            with feature_computation():
                adoption = owner['adoption']
                if (adoption['binding']['client'] != principal or adoption['binding']['run'] != run_id
                        or adoption['request_sha256'] != original_digest(job['request'])
                        or adoption['registered_sha256'] != original_digest(job['registered'])):
                    raise ValueError('Original task differs from first adoption')
                captured = owner['accepted_at'] if owner['queue_new'] is True else accepted_at
                if not _finite(captured):
                    raise ValueError('Original acceptance time unavailable')
                items = _items(job['request']['prompt'], adoption, job['registered'])
                record = {'schema_version': SCHEMA, 'status': 'available',
                    'feature_version': adoption['policy']['feature_version'],
                    'data_policy_version': adoption['policy']['data_policy_version'], 'captured_at': captured,
                    'policy_sha256': adoption['policy_sha256'], 'adoption_sha256': owner['adoption_sha256'],
                    'binding': {**adoption['binding'], 'internal_id': directory.name,
                        'request_sha256': adoption['request_sha256'], 'registered_sha256': adoption['registered_sha256']},
                    'features': items}
                body = encoded(public_features(record))
                record_sha = hashlib.sha256(body).hexdigest()
                anchor_body = encoded({'schema_version': 'creation-feature-anchor-v1', 'internal_id': directory.name,
                    'adoption_sha256': owner['adoption_sha256'], 'request_sha256': adoption['request_sha256'],
                    'registered_sha256': adoption['registered_sha256'], 'record_sha256': record_sha})
            if not feature_budget_available():
                raise ValueError('Feature compute deadline exceeded')
            if self.root != self.root.resolve() or directory.parent.absolute() != self.root:
                raise ValueError('Unsafe feature root')
            self.records.mkdir(mode=0o700, exist_ok=True)
            if self.records != self.records.resolve() or not self.records.is_dir():
                raise ValueError('Unsafe feature metadata root')
            _write_new(directory / 'creation-feature-anchor.json', anchor_body)
            _write_new(self.records / (directory.name + '.json'), body)
            if _OWNER.get() is not owner or owner['seal_attempted']:
                raise ValueError('Original capture owner no longer available')
            owner['seal_attempted'] = True
            self.port.seal(principal, run_id, owner['adoption_sha256'],
                           adoption['registered_sha256'], record_sha)
        except Exception:
            # Preserve partial evidence. Neither recovery nor GET repairs it.
            pass

    def observe(self, principal, run_id, internal_id, job=None, *, require_anchor=True):
        try:
            if self.port is None or not re.fullmatch(r'http-[a-f0-9]{48}', internal_id):
                raise ValueError('Feature protocol unavailable')
            snapshot = self.port.snapshot(principal, run_id)
            adoption = validate_adoption(snapshot['adoption'])
            if (adoption['binding']['client'] != principal or adoption['binding']['run'] != run_id
                    or snapshot['adoption_sha256'] != feature_digest(adoption)):
                raise ValueError('Feature adoption mismatch')
            seal = snapshot['seal']
            if (not isinstance(seal, dict) or set(seal) != {'request_sha256', 'registered_sha256', 'record_sha256'}
                    or seal['request_sha256'] != adoption['request_sha256']
                    or seal['registered_sha256'] != adoption['registered_sha256'] or not _hash(seal['record_sha256'])):
                raise ValueError('Independent feature seal missing')
            body, record = _read_json(self.records / (internal_id + '.json'))
            if hashlib.sha256(body).hexdigest() != seal['record_sha256']:
                raise ValueError('Saved feature record differs from original seal')
            public_features(record)
            expected = {**adoption['binding'], 'internal_id': internal_id,
                'request_sha256': adoption['request_sha256'], 'registered_sha256': adoption['registered_sha256']}
            if record['binding'] != expected or record['adoption_sha256'] != snapshot['adoption_sha256']:
                raise ValueError('Saved feature binding mismatch')
            if require_anchor or job is not None:
                _, anchor = _read_json(self.root / internal_id / 'creation-feature-anchor.json')
                if (anchor != {'schema_version': 'creation-feature-anchor-v1', 'internal_id': internal_id,
                        'adoption_sha256': snapshot['adoption_sha256'], 'request_sha256': adoption['request_sha256'],
                        'registered_sha256': adoption['registered_sha256'], 'record_sha256': seal['record_sha256']}):
                    raise ValueError('Original acceptance anchor mismatch')
            if job is not None and (original_digest(job['request']) != adoption['request_sha256']
                                     or original_digest(job['registered']) != adoption['registered_sha256']):
                raise ValueError('Original accepted task mismatch')
            return record
        except Exception:
            return unavailable()

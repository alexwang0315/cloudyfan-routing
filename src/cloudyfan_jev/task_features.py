"""Bounded creation observations; no runner, dispatch, health or model capability."""
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import time
from types import MappingProxyType

from . import task_recommendations as advice

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


class FeaturesNotProvided(LookupError):
    """The original host binding explicitly recorded no creation capture."""


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
    if isinstance(value, dict) and value.get('schema_version') == 'creation-feature-adoption-v2':
        keys = keys | {'recommendation'}
        advice.adoption(value.get('recommendation'))
    if (not isinstance(value, dict) or set(value) != keys
            or value['schema_version'] not in ('creation-feature-adoption-v1', 'creation-feature-adoption-v2')):
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


def _creation_projection(record, adoption):
    if adoption['schema_version'] == 'creation-feature-adoption-v1':
        return {'task_features': public_features(record), 'routing_recommendation': advice.not_provided()}
    keys = {'schema_version', 'task_features', 'recommendation_input', 'routing_recommendation'}
    if (not isinstance(record, dict) or set(record) != keys or record['schema_version'] != advice.ENVELOPE
            or len(encoded(record)) > advice.BOUNDS['new_envelope_bytes']):
        raise ValueError('Invalid creation observation envelope')
    features = public_features(record['task_features'])
    source = advice.frozen_input(record['recommendation_input'])
    recommendation = advice.public_recommendation(record['routing_recommendation'])
    expected = adoption['recommendation']
    binding = features['binding']
    scope = {'category': binding['category'], 'mode': binding['mode'],
             'contract_id': binding['contract'], 'registered_sha256': binding['registered_sha256']}
    if (source['binding'] != scope or source['feature_sha256'] != feature_digest(features)
            or source['captured_at'] != features['captured_at']
            or source['rule_version'] != expected['rule_version']
            or source['data_policy_version'] != expected['data_policy_version']
            or advice.digest(source['context']) != expected['context_sha256']):
        raise ValueError('Saved recommendation input differs from original adoption')
    if source['schema_version'] == 'creation-routing-input-v2' and source['feature_summary'] != features['features']:
        raise ValueError('Saved recommendation differs from original feature values')
    if recommendation != advice.unavailable() and (
            recommendation['feature_sha256'] != source['feature_sha256']
            or recommendation['context_sha256'] != expected['context_sha256']
            or recommendation['input_sha256'] != advice.digest(source)
            or recommendation['captured_at'] != features['captured_at']):
        raise ValueError('Saved recommendation differs from original features')
    return {'task_features': features, 'routing_recommendation': recommendation}


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


def _read_json(path, *, max_bytes=16384):
    path = Path(path).absolute()
    if path != path.resolve():
        raise ValueError('Unsafe feature path')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > max_bytes:
            raise ValueError('Unsafe or oversized feature record')
        body = stream.read(max_bytes + 1)
    if len(body) > max_bytes:
        raise ValueError('Oversized feature record')
    def unique(pairs):
        result = {}
        for key, item in pairs:
            if key in result:
                raise ValueError('Duplicate feature field')
            result[key] = item
        return result
    with feature_computation() if max_bytes > POLICY['max_record_bytes'] else nullcontext():
        value = json.loads(body, object_pairs_hook=unique)
    return body, value


def _write_new(path, body, *, max_bytes=16384):
    path = Path(path).absolute()
    if len(body) > max_bytes or path != path.resolve():
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


def _recommendation_plan_bounds(plan):
    pools = plan.get('pools')
    if not isinstance(pools, dict) or set(pools) != {'open_source', 'low', 'mid', 'high'}:
        raise ValueError('Unsupported original candidate structure')
    if any(not isinstance(rows, list) or not rows for rows in pools.values()):
        raise ValueError('Unsupported original candidate pool')
    sizes = {name: len(rows) for name, rows in pools.items()}
    combinations = (sizes['open_source'] * sizes['low'] * sizes['mid']
                    + (sizes['low'] + sizes['mid']) * sizes['mid'] * sizes['high'])
    if sum(sizes.values()) > 12 or combinations > 128:
        raise ValueError('Exact calibration work exceeds bounds')
    stages = plan.get('qcs_policy', {}).get('stages', {})
    if (not isinstance(stages, dict) or any(not isinstance(stage, dict) or not isinstance(stage.get('variants'), dict)
            for stage in stages.values()) or sum(len(stage['variants']) for stage in stages.values()) > 128):
        raise ValueError('Exact calibration variant work exceeds bounds')


def _calibration_names(plan):
    _recommendation_plan_bounds(plan)  # Before any cartesian-product helper.
    from .scores.selector_research import required_contracts
    required = required_contracts(plan, {'version': plan['qcs_policy']['classifier_version']})
    provided = {(stage, key) for stage, value in plan['qcs_policy']['stages'].items() for key in value['variants']}
    if provided != {(needed['stage'], key) for key, needed in required.items()}:
        raise ValueError('Required exact calibration scope is incomplete or extra')
    names = {}
    for stage in plan['qcs_policy']['stages'].values():
        for variant in stage['variants'].values():
            files = variant.get('calibration', {}).get('source_files', {})
            if not isinstance(files, dict) or not 1 <= len(files) <= 16:
                raise ValueError('Exact source names missing')
            raw = variant.get('calibration', {}).get('raw_sha256')
            if not _hash(raw) or raw not in files.values():
                raise ValueError('Raw calibration SHA is not declared in source_files')
            for name, expected in files.items():
                if not isinstance(name, str) or not _hash(expected) or name in names and names[name] != expected:
                    raise ValueError('Exact source binding differs')
                names[name] = expected
                if len(names) > 16:
                    raise ValueError('Exact source union exceeds bounds')
    return names


def _bounded_calibration_sources(names, root):
    """Host-only immutable bytes, one bounded union; no product GET calls here."""
    root = Path(root)
    forbidden = {'private', 'authoring', 'evaluation', 'credentials', 'credential', 'secrets', 'secret', 'tokens'}
    def public(path):
        return not any(part.casefold() in forbidden or Path(part).stem.casefold() in forbidden or part.casefold().startswith('.env')
                       for part in path.parts)
    if (not names or len(names) > 16 or not root.is_absolute() or root != root.resolve()
            or not public(root) or not stat.S_ISDIR(root.lstat().st_mode)):
        raise ValueError('Controlled public calibration root required')
    root_info = root.lstat()
    checked, total = [], 0
    # Complete union size/type checks precede the first source open.
    for name, expected in names.items():
        path = Path(name)
        if not path.is_absolute() or str(path) != name or path != path.resolve() or not public(path):
            raise ValueError('Unsafe declared calibration source')
        path.relative_to(root)
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or not 1 <= info.st_size <= 262144:
            raise ValueError('Unsafe or oversized calibration source')
        total += info.st_size
        if total > 1048576:
            raise ValueError('Calibration source union too large')
        checked.append((path, info, expected))
    result, actual_total = {}, 0
    for path, before, expected in checked:
        # Bind every ancestor through a directory descriptor. A parent swapped
        # for a symlink cannot redirect even the open to an unrelated source.
        directory = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            for part in root.parts[1:]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                dir_fd=directory)
                os.close(directory)
                directory = child
            bound_root = os.fstat(directory)
            if (bound_root.st_dev, bound_root.st_ino) != (root_info.st_dev, root_info.st_ino):
                raise ValueError('Calibration root changed before descriptor binding')
            relative = path.relative_to(root)
            for part in relative.parts[:-1]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                dir_fd=directory)
                os.close(directory)
                directory = child
            fd = os.open(relative.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        finally:
            os.close(directory)
        try:
            opened = os.fstat(fd)
            if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1
                    or (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns)
                    != (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)):
                raise ValueError('Calibration source changed before read')
            chunks, count = [], 0
            while count <= 262144:
                allowance = min(262145-count, 1048577-actual_total)
                if allowance <= 0:
                    break
                chunk = os.read(fd, min(65536, allowance))
                if not chunk:
                    break
                chunks.append(chunk)
                count += len(chunk)
                actual_total += len(chunk)
            after = os.fstat(fd)
            current = path.lstat()
            if (count != before.st_size or count > 262144 or actual_total > 1048576 or path != path.resolve()
                    or (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns, after.st_nlink)
                    != (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns, 1)
                    or (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino)):
                raise ValueError('Calibration source changed during bounded read')
            data = b''.join(chunks)
            if hashlib.sha256(data).hexdigest() != expected:
                raise ValueError('Exact actual source SHA differs')
            result[str(path)] = data
        finally:
            os.close(fd)
    current_root = root.lstat()
    if (current_root.st_dev, current_root.st_ino) != (root_info.st_dev, root_info.st_ino) or root != root.resolve():
        raise ValueError('Controlled source root changed')
    return MappingProxyType(result)


def _recommendation_calibration(registered, contract, source_config, source_bytes, now, bounds_error=False,
                                source_unsupported=False):
    from .scores.catalog import timestamp
    from .scores.backup_pools import verify_backup_pools
    from .scores.backup_runtime import BackupCategoryCore
    from .scores.selector_research import required_contracts, require_mock_transport, validate_selector_calibration
    plan = registered.get('backup_plan', {})
    policy = plan.get('qcs_policy', {})
    stages = policy.get('stages')
    summary = {'reason': 'exact_classifier_calibration_missing', 'valid_from': None, 'valid_until': None,
        'sources': [], 'variants': [], 'minimum_saving_usd': '0', 'minimum_saving_fraction': '0', 'max_flow_ms': '0',
        'candidate_validity': None}
    if registered['mode'] != 'cloudyfan':
        return 'missing', 'not_provided', summary
    if source_unsupported:
        summary['reason'] = 'exact_calibration_source_schema_unsupported'
        return 'unsupported', 'not_provided', summary
    if isinstance(stages, dict) and not stages:
        return 'missing', 'not_provided', summary
    if bounds_error:
        summary['reason'] = 'exact_calibration_bounds_unsupported'
        return 'unsupported', 'not_provided', summary
    summary['reason'] = 'exact_classifier_calibration_unverified'
    if source_config is None:
        return 'unverified', 'not_provided', summary
    kind = source_config['data_kind']
    if source_bytes is None:
        summary['reason'] = 'exact_calibration_source_unavailable'
        return 'unverified', kind, summary
    try:
        names = _calibration_names(plan)
        selected = MappingProxyType({name: source_bytes[name] for name in names})
        if kind == 'offline_fixture':
            require_mock_transport(contract.runner.core.client)
            if contract.runner.core.local_backend is not None:
                raise ValueError('Every offline route must have a verified MockTransport')
        if not _finite(now):
            raise ValueError('Trusted bootstrap time unavailable')
        core = contract.runner.core
        template_port = getattr(core, 'queue_template', None)
        template = template_port() if callable(template_port) else core
        if type(template) is not BackupCategoryCore or template.configuration_descriptor() != core.configuration_descriptor():
            summary['reason'] = 'exact_calibration_execution_adapter_unsupported'
            return 'unverified', kind, summary
        if policy.get('execution_contract') != template.execution_contract_descriptor(registered['version']):
            summary['reason'] = 'exact_calibration_execution_contract_mismatch'
            return 'unverified', kind, summary
        validate_selector_calibration(plan, registered['decision'], data_kind=kind,
            now=datetime.fromtimestamp(now, timezone.utc).isoformat(), source_bytes=selected)
        verify_backup_pools(plan, now=datetime.fromtimestamp(now, timezone.utc).isoformat())
        required = required_contracts(plan, registered['decision'])
        windows, variants = [], []
        for key, needed in required.items():
            row = policy['stages'][needed['stage']]['variants'][key]
            cal = row['calibration']
            windows.append((timestamp(cal['retrieved_at']).timestamp(), timestamp(cal['valid_until']).timestamp()))
            variants.append({**{field: row[field] for field in ('paths', 'classified_paths', 'classifier_cost_usd',
                'lower_probability', 'classify_flow_ms_upper')}, 'contract_sha256': key,
                'stage': needed['stage'], 'previous_tier': needed['previous_tier']})
        provenance = policy['complete_policy_flow']['provenance']
        windows.append((timestamp(provenance['retrieved_at']).timestamp(), timestamp(provenance['valid_until']).timestamp()))
        snapshot = plan['snapshot']
        candidate_windows = [(timestamp(snapshot['created_at']).timestamp(), timestamp(snapshot['valid_until']).timestamp())]
        source_ends = []
        catalog_age = timedelta(hours=snapshot['policy']['max_catalog_age_hours'])
        source_age = float(Decimal(policy.get('max_source_age_hours', '24')) * 3600)
        for rows in plan['pools'].values():
            for row in rows:
                entry = row['entry']
                provenance = row['evidence']['provenance']
                candidate_windows.append((timestamp(provenance['retrieved_at']).timestamp(), timestamp(provenance['valid_until']).timestamp()))
                checked = timestamp(entry['catalog_checked_at'])
                candidate_windows.append((checked.timestamp(), (checked + catalog_age).timestamp()))
                role = snapshot['policy'].get('score_role')
                qualifications = ['category_qualification'] if role == 'category_family_prior' else []
                if role == 'family_effort_prior' or any(cap.startswith('sql') for cap in plan['requirements']['capabilities']):
                    qualifications.append('sql_qualification')
                for field in qualifications:
                    candidate_windows.append((0, timestamp(entry[field]['valid_until']).timestamp()))
                for metric in row['evidence']['source_metrics']:
                    for field in ('retrieved_at', 'as_of'):
                        if metric.get(field) is not None:
                            lower = timestamp(metric[field]).timestamp()
                            candidate_windows.append((lower, float('inf')))
                            source_ends.append(lower + source_age)
        summary.update(reason='exact_classifier_calibration_verified',
            valid_from=max(row[0] for row in windows), valid_until=min(row[1] for row in windows), variants=variants,
            candidate_validity={'valid_from': max(row[0] for row in candidate_windows),
                'valid_until': min(row[1] for row in candidate_windows),
                'valid_through': min(source_ends) if source_ends else None},
            sources=[{'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data)} for data in selected.values()],
            minimum_saving_usd=policy['minimum_saving_usd'], minimum_saving_fraction=policy['minimum_saving_fraction'],
            max_flow_ms=policy['max_flow_ms'])
        advice.calibration_summary(summary, candidate_time=True)
        return 'verified', kind, summary
    except Exception:
        summary['reason'] = 'exact_calibration_scope_or_cost_invalid'
        return 'unverified', kind, summary


class FeatureRecorder:
    """One first-owner capture, independent host seal, then read-only evidence."""
    def __init__(self, root, configuration, contracts):
        self.root = Path(root).absolute()
        self.records = self.root / '.task-features'
        self.policy, self.port, self.registered = None, None, {}
        self.recommendation_adoptions = MappingProxyType({})
        self._recommendation_contexts = MappingProxyType({})
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

    def configure_recommendations(self, configuration, contracts, *, now=None):
        """Explicit producer bootstrap only; readers never build a context."""
        self.recommendation_adoptions = MappingProxyType({})
        self._recommendation_contexts = MappingProxyType({})
        self.recommendation_policy = None
        if configuration is None:
            return False
        if not isinstance(configuration, dict) or set(configuration) - (set(advice.enabled_policy()) | {'calibration_sources'}):
            raise ValueError('Unsupported host recommendation configuration')
        policy = advice.policy({key: value for key, value in configuration.items() if key != 'calibration_sources'})
        source_config = configuration.get('calibration_sources')
        if source_config is not None and (not isinstance(source_config, dict)
                or set(source_config) != {'data_kind', 'public_root'}
                or not isinstance(source_config['data_kind'], str) or not 1 <= len(source_config['data_kind']) <= 160
                or not isinstance(source_config['public_root'], str)):
            raise ValueError('Explicit controlled calibration source configuration required')
        source_unsupported = source_config is not None and source_config['data_kind'] not in ('observed_calibration', 'offline_fixture')
        if not policy['enabled'] or self.policy is None or self.port is None:
            return False
        contexts, adoptions, prepared, source_names = {}, {}, [], {}
        source_error = False
        for name, contract in contracts.items():
            registered = contract.descriptor()
            original_sha = original_digest(registered)
            if original_sha != self.registered[name]:
                raise ValueError('Original feature contract changed during bootstrap')
            plan = registered.get('backup_plan')
            plan = plan if isinstance(plan, dict) else {}
            bounded = True
            if source_config is not None and not source_unsupported and registered['mode'] == 'cloudyfan' and plan:
                try:
                    names = _calibration_names(plan)
                    for filename, expected in names.items():
                        if filename in source_names and source_names[filename] != expected:
                            raise ValueError('Conflicting source digests')
                        source_names[filename] = expected
                except Exception:
                    bounded = False
            prepared.append((name, contract, registered, original_sha, plan, bounded))
        actual_sources = None
        if source_config is not None and not source_unsupported:
            try:
                actual_sources = _bounded_calibration_sources(source_names, source_config['public_root'])
            except Exception:
                source_error = True
        for name, contract, registered, original_sha, plan, bounded in prepared:
            status, kind, summary = _recommendation_calibration(registered, contract, source_config,
                None if source_error else actual_sources, now, bounds_error=not bounded,
                source_unsupported=source_unsupported)
            context = advice.context({
                'schema_version': 'creation-routing-context-v3', 'contract_id': name,
                'registered_sha256': original_sha, 'category': plan.get('category_id'), 'mode': registered['mode'],
                'calibration_status': status, 'source_kind': kind, 'calibration_summary': summary})
            body = encoded(context)
            contexts[name] = body
            adoptions[name] = MappingProxyType({
                'rule_version': policy['rule_version'], 'data_policy_version': policy['data_policy_version'],
                'policy_sha256': advice.digest(policy), 'context_sha256': hashlib.sha256(body).hexdigest()})
        self._recommendation_contexts = MappingProxyType(contexts)
        self.recommendation_adoptions = MappingProxyType(adoptions)
        self.recommendation_policy = encoded(policy)
        return True

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
                    context = None
                    if adoption['schema_version'] == 'creation-feature-adoption-v2':
                        if dict(self.recommendation_adoptions.get(binding['contract'], {})) != adoption['recommendation']:
                            raise ValueError('Original recommendation bootstrap is unavailable')
                        context = advice.context(json.loads(self._recommendation_contexts[binding['contract']]))
                owner = {'recorder': self, 'adoption': adoption, 'adoption_sha256': snapshot['adoption_sha256'],
                         'queue_new': None, 'accepted_at': None, 'attempted': False, 'seal_attempted': False,
                         'recommendation_context': context}
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
                limit = POLICY['max_record_bytes']
                if adoption['schema_version'] == 'creation-feature-adoption-v2':
                    source = advice.frozen_input({
                        'schema_version': 'creation-routing-input-v2',
                        'rule_version': adoption['recommendation']['rule_version'],
                        'data_policy_version': adoption['recommendation']['data_policy_version'],
                        'captured_at': captured, 'feature_sha256': feature_digest(record),
                        'binding': {'category': adoption['binding']['category'], 'mode': adoption['binding']['mode'],
                            'contract_id': adoption['binding']['contract'], 'registered_sha256': adoption['registered_sha256']},
                        'context': owner['recommendation_context'], 'feature_summary': items})
                    try:
                        recommendation = advice.recommend(source)
                    except Exception:
                        recommendation = advice.unavailable()
                    record = {'schema_version': advice.ENVELOPE, 'task_features': record,
                              'recommendation_input': source, 'routing_recommendation': recommendation}
                    _creation_projection(record, adoption)
                    body = encoded(record)
                    limit = advice.BOUNDS['new_envelope_bytes']
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
            _write_new(self.records / (directory.name + '.json'), body, max_bytes=limit)
            if _OWNER.get() is not owner or owner['seal_attempted']:
                raise ValueError('Original capture owner no longer available')
            owner['seal_attempted'] = True
            self.port.seal(principal, run_id, owner['adoption_sha256'],
                           adoption['registered_sha256'], record_sha)
        except Exception:
            # Preserve partial evidence. Neither recovery nor GET repairs it.
            pass

    def observe(self, principal, run_id, internal_id, job=None, *, require_anchor=True, include_recommendation=False):
        adoption = None
        try:
            if self.port is None or not re.fullmatch(r'http-[a-f0-9]{48}', internal_id):
                raise ValueError('Feature protocol unavailable')
            snapshot = self.port.snapshot(principal, run_id)
            new = snapshot['adoption'].get('schema_version') == 'creation-feature-adoption-v2'
            with feature_computation() if new else nullcontext():
                adoption = validate_adoption(snapshot['adoption'])
                if (adoption['binding']['client'] != principal or adoption['binding']['run'] != run_id
                        or snapshot['adoption_sha256'] != feature_digest(adoption)):
                    raise ValueError('Feature adoption mismatch')
                seal = snapshot['seal']
                if (not isinstance(seal, dict) or set(seal) != {'request_sha256', 'registered_sha256', 'record_sha256'}
                        or seal['request_sha256'] != adoption['request_sha256']
                        or seal['registered_sha256'] != adoption['registered_sha256'] or not _hash(seal['record_sha256'])):
                    raise ValueError('Independent feature seal missing')
            limit = (advice.BOUNDS['new_envelope_bytes'] if adoption['schema_version'] == 'creation-feature-adoption-v2'
                     else POLICY['max_record_bytes'])
            body, record = _read_json(self.records / (internal_id + '.json'), max_bytes=limit)
            with feature_computation() if new else nullcontext():
                if hashlib.sha256(body).hexdigest() != seal['record_sha256']:
                    raise ValueError('Saved feature record differs from original seal')
                projected = _creation_projection(record, adoption)
                features = projected['task_features']
                expected = {**adoption['binding'], 'internal_id': internal_id,
                    'request_sha256': adoption['request_sha256'], 'registered_sha256': adoption['registered_sha256']}
                if features['binding'] != expected or features['adoption_sha256'] != snapshot['adoption_sha256']:
                    raise ValueError('Saved feature binding mismatch')
            if require_anchor or job is not None:
                _, anchor = _read_json(self.root / internal_id / 'creation-feature-anchor.json')
                with feature_computation() if new else nullcontext():
                    if (anchor != {'schema_version': 'creation-feature-anchor-v1', 'internal_id': internal_id,
                            'adoption_sha256': snapshot['adoption_sha256'], 'request_sha256': adoption['request_sha256'],
                            'registered_sha256': adoption['registered_sha256'], 'record_sha256': seal['record_sha256']}):
                        raise ValueError('Original acceptance anchor mismatch')
            with feature_computation() if new else nullcontext():
                if job is not None and (original_digest(job['request']) != adoption['request_sha256']
                                         or original_digest(job['registered']) != adoption['registered_sha256']):
                    raise ValueError('Original accepted task mismatch')
            if new and not feature_budget_available():
                raise ValueError('Creation observation compute budget exhausted')
            return projected if include_recommendation else features
        except FeaturesNotProvided:
            return ({'task_features': not_provided(), 'routing_recommendation': advice.not_provided()}
                    if include_recommendation else not_provided())
        except Exception:
            if include_recommendation:
                result = {'task_features': unavailable()}
                if adoption is not None and adoption['schema_version'] == 'creation-feature-adoption-v2':
                    result['routing_recommendation'] = advice.unavailable()
                return result
            return unavailable()

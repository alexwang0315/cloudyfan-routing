"""Verify a recorded capacity-window gate; never rebuild it at a new time.

Only selected, still-readable original tasks may supply response evidence.
All reads/SQLite projections and locks use the comparison loader's existing
bounded source protocol. No window backend, controller or execution callback is
constructed. The only result is open or one original allowlisted exclusion code.
"""
from contextlib import nullcontext
from email.utils import format_datetime, parsedate_to_datetime
import errno
import hashlib
import json
from pathlib import Path
import re
import sqlite3

from .saved_comparisons import (
    HASH, LIMITS, SourceUnavailable, _original_receipts, _shared, _task_id,
    digest, finite, identifier, original_digest, strict_json,
)

FIELDS = {'status', 'not_before', 'evidence_refs', 'revision', 'reason_code'}
ROW_FIELDS = {'id', 'physical', 'group', 'scope', 'status', 'not_before', 'observed_at',
              'source_kind', 'header', 'constraint', 'version', 'policy_version',
              'configuration_version', 'reason_code', 'reported_not_before'}
HEADERS = {'retry-after', 'ratelimit-reset', 'x-ratelimit-reset',
           'x-ratelimit-reset-requests', 'x-ratelimit-reset-tokens', 'date'}
UNKNOWN_REASONS = {'capacity_window_header_invalid', 'capacity_window_header_missing',
                   'capacity_window_clock_reference_unknown', 'capacity_window_time_invalid',
                   'capacity_window_source_contradictory'}
INVALID = 'original_window_evidence_invalid'


def _require(condition, code=INVALID):
    if not condition:
        raise SourceUnavailable(code)


def _hash(value):
    # CapacityStore/CapacityController use JSON's default ensure_ascii=True.
    # Monitor.digest intentionally uses another serializer and is not a substitute.
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                    allow_nan=False).encode()).hexdigest()


def _capacity_time(value):
    return type(value) in (int, float) and 0 <= value <= 10**12 and finite(value)


def _memo(source, key, load):
    """Memoize only this fresh report and cite captured hashes in every item."""
    cache = getattr(source, '_saved_window_verified', None)
    if cache is None:
        cache = source._saved_window_verified = {'values': {}, 'marker': None, 'cited': set()}
    marker = source.current_database_refs
    if cache['marker'] is not marker:
        cache.update(marker=marker, cited=set())
    if key in cache['values']:
        value, evidence = cache['values'][key]
        for row in evidence:
            identity = tuple(sorted(row.items()))
            if identity not in cache['cited']:
                source.evidence.append(dict(row))
                cache['cited'].add(identity)
        return value
    start = len(source.evidence)
    value = load()
    evidence = [dict(row) for row in source.evidence[start:]]
    cache['values'][key] = value, evidence
    cache['cited'].update(tuple(sorted(row.items())) for row in evidence)
    return value


def _one(source, path, query, params, kind):
    rows = source.rows(path, query, params, kind=kind, max_rows=1)
    _require(len(rows) == 1)
    return rows[0]


def _locks(access, internal, registered=None):
    # Never hold Core's exclusive non-blocking task/retention locks; see
    # saved_comparisons._unchanged. The legal check reads only job.json, once
    # and after expiry, so _read's own change check suffices. Run receipts keep
    # the run lock, which Core waits for instead of failing.
    if registered is None:
        return nullcontext()
    return _shared(Path(registered['receipt_root']) / 'runs' / internal / '.lock')


def _legal_task(source, access, task):
    """Authorize before opening job, manifest, result or any original call."""
    internal = _task_id(task['client'], task['run_id'])
    key = ('legal', _hash(access), internal)
    def load():
        _require(access['permission_reader_known'] is True
            and task['client'] in access['allowed_principals']
            and access['identity_path'] is not None and access['retention_path'] is not None,
            'original_window_reference_permission_unavailable')
        enabled = _one(source, access['monitor_path'], 'SELECT enabled FROM clients WHERE id=?',
                       (task['client'],), 'original_window_client_permission')
        active = _one(source, access['identity_path'], 'SELECT active FROM principals WHERE id=?',
                      (task['client'],), 'original_window_identity_permission')
        _require(enabled['enabled'] == 1 and active['active'] == 1,
                 'original_window_reference_permission_unavailable')
        with _locks(access, internal):
            lifecycle = _one(source, access['retention_path'],
                'SELECT created,expires,policy,state FROM tasks WHERE id=?', (internal,), 'original_window_lifecycle')
            clock = source.rows(access['retention_path'], 'SELECT observed FROM clock WHERE singleton=1',
                                kind='original_window_retention_clock', max_rows=1)
            _require(finite(access['now']) and (not clock or finite(clock[0]['observed'])
                and access['now'] >= clock[0]['observed']), 'original_window_reference_clock_unavailable')
            _require(finite(lifecycle['created']) and finite(lifecycle['expires'])
                and lifecycle['created'] <= access['now'] < lifecycle['expires']
                and lifecycle['state'] == 'active', 'original_window_reference_access_expired')
            _require(strict_json(lifecycle['policy']) == access['retention_policy'],
                     'original_window_reference_permission_unavailable')
            binding = _one(source, access['monitor_path'],
                'SELECT * FROM bindings WHERE client=? AND run=?',
                (task['client'], task['run_id']), 'original_window_task_binding')
            _require(binding['client'] == task['client'] and binding['run'] == task['run_id'])
            trusted = access['contracts'].get(binding['contract'])
            _require(type(trusted) is dict and type(trusted.get('registered')) is dict)
            registered = trusted['registered']
            job = source.read(Path(access['tasks_root']) / internal / 'job.json', 'original_window_task_job')
            request = job['request']
            _require(job['registered'] == registered and type(request) is dict
                and set(request) == {'run_id', 'contract_id', 'prompt'}
                and request['run_id'] == task['run_id'] and request['contract_id'] == binding['contract']
                and digest({'run_id': request['run_id'], 'category': binding['category'],
                            'prompt': request['prompt']}) == binding['fingerprint'])
            return {'task': task, 'internal': internal, 'binding': binding, 'registered': registered, 'job': job}
    return _memo(source, key, load)


def _bundle(source, access, legal):
    key = ('capacity', _hash(access), legal['internal'], _hash(legal['registered']))
    def load():
        path = access['capacity_path']
        row = _one(source, path, 'SELECT run,payload,terminal FROM capacity_adoptions WHERE run=?',
                   (legal['internal'],), 'original_window_capacity_adoption')
        adoption = strict_json(row['payload'])
        _require(row['run'] == adoption['run'] == legal['internal']
            and row['terminal'] in (0, 1) and adoption['principal'] == legal['task']['client']
            and adoption['descriptor_sha256'] == _hash(legal['registered'])
            and adoption['protocol'] == 'httpx-prepared-json-v1' and _capacity_time(adoption['accepted_at']))
        policy = strict_json(_one(source, path, 'SELECT payload FROM capacity_policies WHERE version=?',
            (adoption['policy_version'],), 'original_window_dispatch_policy')['payload'])
        _require(policy['version'] == adoption['policy_version']
            and policy['configuration_version'] == adoption['configuration_version']
            and policy['protocol'] == adoption['protocol'] and type(policy['routes']) is list
            and 1 <= len(policy['routes']) <= 512
            and (policy.get('windows_version') is None or identifier(policy['windows_version']) is not None))
        saved = _one(source, path, 'SELECT version,digest,payload FROM capacity_configurations WHERE version=?',
                     (adoption['configuration_version'],), 'original_window_capacity_configuration')
        config = strict_json(saved['payload'])
        _require(saved['version'] == config['version'] == adoption['configuration_version']
            and saved['digest'] == policy['configuration_sha256'] == _hash(config)
            and config['schema_version'] == 1 and type(config['bindings']) is list and type(config['groups']) is list)
        if policy.get('windows_version') is None:
            return adoption, policy, config, None
        definition = strict_json(_one(source, path, 'SELECT payload FROM capacity_window_policies WHERE version=?',
            (policy['windows_version'],), 'original_window_definition')['payload'])
        _require(definition['protocol'] == 'capacity-windows-v1' and definition['version'] == policy['windows_version']
            and definition['configuration_sha256'] == saved['digest']
            and type(definition['rules']) is list and len(definition['rules']) <= 128
            and type(definition['not_applicable']) is list
            and type(definition['max_delay_seconds']) is int and 1 <= definition['max_delay_seconds'] <= 86400)
        # Explicit controlled imports have a separate obligations protocol. They
        # cannot be replaced with fabricated original-response receipt locators.
        _require(not definition.get('controlled_sources'), 'original_window_inputs_unavailable')
        return adoption, policy, config, definition
    return _memo(source, key, load)


def _scope(bundle, profile, registered):
    _, policy, config, definition = bundle
    # Model transports use the original registered receipt root and no tool
    # configuration. A policy may include other lanes for the same provider.
    transport = _hash({'root': str(Path(registered['receipt_root']).absolute()), 'configuration': None})
    routes = [r for r in policy['routes'] if r['model'] == profile['model_version']
              and r['provider_name'] == profile['provider_name'] and r['transport_sha256'] == transport]
    _require(len(routes) == 1)
    route = routes[0]
    bindings = [b for b in config['bindings'] if b['id'] == route['binding']]
    _require(len(bindings) == 1 and HASH.fullmatch(route['transport_sha256']) is not None)
    binding = bindings[0]
    groups = [g for g in config['groups'] if g['id'] in binding['groups']]
    _require(len(groups) == len(set(binding['groups'])) and groups)
    physical = {_hash(g['scope']): g for g in groups}
    _require(len(physical) == len(groups))
    rules = [] if definition is None else [r for r in definition['rules'] if r['binding'] == binding['id']]
    for rule in rules:
        _require(set(rule) == {'binding', 'group', 'header', 'format', 'constraint'}
            and rule['group'] in binding['groups'] and rule['header'] in HEADERS - {'date'}
            and rule['format'] in ('retry_after', 'unix_seconds', 'delta_seconds')
            and identifier(rule['constraint']) is not None)
    return route, physical, rules


def _prefix(source, access, bundle, physical, revision):
    key = ('prefix', _hash(access), bundle[1]['version'], tuple(sorted(physical)), revision)
    def load():
        path = access['capacity_path']
        count = _one(source, path, 'SELECT COUNT(*) AS count,MIN(rowid) AS first,MAX(rowid) AS last '
            'FROM capacity_window_observations WHERE rowid<=?', (revision,), 'original_window_revision_prefix')
        _require(count['count'] == revision and (revision == 0 or count['first'] == 1 and count['last'] == revision))
        rows = source.rows(path, 'SELECT rowid AS sequence,id,physical,status,not_before,payload '
            'FROM capacity_window_observations WHERE rowid<=? AND physical IN ('
            + ','.join('?' for _ in physical) + ') ORDER BY rowid LIMIT 129',
            (revision, *sorted(physical)), kind='original_window_related_prefix', max_rows=128)
        result = []
        for row in rows:
            value = strict_json(row['payload'])
            _require(type(value) is dict and set(value) == ROW_FIELDS
                and type(row['sequence']) is int and 1 <= row['sequence'] <= revision
                and all(row[k] == value[k] for k in ('id', 'physical', 'status', 'not_before'))
                and HASH.fullmatch(value['id']) is not None and value['physical'] in physical
                and value['physical'] == _hash(value['scope'])
                and value['group'] == physical[value['physical']]['id']
                and value['scope'] == physical[value['physical']]['scope']
                and value['version'] == bundle[3]['version']
                and value['configuration_version'] == bundle[2]['version']
                and value['source_kind'] == 'original_response'
                and value['status'] in ('wait', 'unknown'))
            raw_policy = strict_json(_one(source, path, 'SELECT payload FROM capacity_policies WHERE version=?',
                (value['policy_version'],), 'original_window_observation_policy')['payload'])
            _require(raw_policy['configuration_version'] == bundle[2]['version']
                and raw_policy['configuration_sha256'] == bundle[1]['configuration_sha256']
                and raw_policy['windows_version'] == bundle[3]['version'])
            _require(value['observed_at'] is None or _capacity_time(value['observed_at']))
            if value['status'] == 'wait':
                _require(_capacity_time(value['not_before']) and _capacity_time(value['reported_not_before'])
                    and value['reported_not_before'] == value['not_before']
                    and _capacity_time(value['observed_at']) and value['observed_at'] <= value['not_before']
                    <= value['observed_at'] + bundle[3]['max_delay_seconds']
                    and value['reason_code'] == 'capacity_window_wait')
            else:
                _require(value['not_before'] is None and value['reason_code'] in UNKNOWN_REASONS
                    and (value['reported_not_before'] is None or _capacity_time(value['reported_not_before'])))
            result.append(dict(value, sequence=row['sequence']))
        return result
    return _memo(source, key, load)


def _calls(source, access, legal):
    key = ('calls', _hash(access), legal['internal'])
    def load():
        registered, job, binding = legal['registered'], legal['job'], legal['binding']
        run = Path(registered['receipt_root']) / 'runs' / legal['internal']
        with _locks(access, legal['internal'], registered):
            manifest = source.read(run / 'manifest.json', 'original_window_source_manifest')
            result = source.read(run / 'result.json', 'original_window_source_result')
            _require(manifest['mode'] == binding['mode'] and manifest['task']['prompt'] == job['request']['prompt']
                and manifest['contract_version'] == registered['version']
                and all(manifest.get(k) == v for k, v in registered.items() if k not in (
                    'version', 'mode', 'tool_names', 'model_bounds', 'tool_bounds', 'receipt_root', 'budget_store', 'enforcement'))
                and result['run_id'] == legal['internal'] and result['mode'] == binding['mode']
                and result['config_sha256'] == (digest(manifest) if manifest['routing_version'] == 'qcs-backup-policy-v1'
                                               else original_digest(manifest)))
            calls = [dict(call, legal=legal) for row in _original_receipts(
                source, registered, manifest, result, legal['internal'], run) for call in row['calls']]
            _require(not source.limited, 'source_work_limit')
            return calls
    return _memo(source, key, load)


def _reported_until(headers, rule, maximum):
    """Check original header grammar against its saved observation time only."""
    observed = headers['observed_at']
    if headers['invalid'] or not _capacity_time(observed):
        return None, 'capacity_window_header_invalid'
    values = dict(headers['values'])
    value, grammar = values.get(rule['header']), rule['format']
    if type(value) is not str:
        return None, 'capacity_window_header_missing'
    def date(text):
        try:
            parsed = parsedate_to_datetime(text)
            number = parsed.timestamp()
            if format_datetime(parsed, usegmt=True) != text or not _capacity_time(number):
                raise ValueError
            return number
        except (ValueError, TypeError, OverflowError, AttributeError):
            raise SourceUnavailable('capacity_window_clock_reference_unknown') from None
    try:
        if 'date' in values and abs(date(values['date']) - observed) > 1:
            return None, 'capacity_window_clock_reference_unknown'
        digits = re.fullmatch(r'[0-9]{1,13}', value)
        if grammar == 'unix_seconds' or grammar == 'retry_after' and not digits:
            if 'date' not in values:
                return None, 'capacity_window_clock_reference_unknown'
            if grammar == 'unix_seconds' and not digits:
                return None, 'capacity_window_time_invalid'
            until = int(value) if grammar == 'unix_seconds' else date(value)
        elif grammar in ('retry_after', 'delta_seconds') and digits:
            until = observed + int(value)
        else:
            return None, 'capacity_window_time_invalid'
        if not _capacity_time(until) or not observed <= until <= observed + maximum:
            return None, 'capacity_window_time_invalid'
        return until, 'capacity_window_wait'
    except SourceUnavailable:
        return None, 'capacity_window_clock_reference_unknown'


def _receipt(source, access, row, call):
    key = ('receipt', _hash(access), row['id'], call['response_sha256'])
    def load():
        legal = call['legal']
        bundle = _bundle(source, access, legal)
        _require(row['policy_version'] == bundle[0]['policy_version']
            and row['version'] == bundle[3]['version'] and row['configuration_version'] == bundle[2]['version'])
        root = Path(legal['registered']['receipt_root'])
        with _locks(access, legal['internal'], legal['registered']):
            intent = source.read(root / 'calls' / call['call_id'] / 'intent.json', 'original_window_response_intent')
            response = source.read(root / 'calls' / call['call_id'] / 'response.json', 'original_window_response_receipt')
            _require(source.evidence[-1]['sha256'] == call['response_sha256']
                and intent['call_id'] == response['call_id'] == call['call_id']
                and response['request_sha256'] == intent['request_sha256'] == call['request_sha256']
                and original_digest({'url': intent['url'], 'payload': intent['payload']}) == intent['request_sha256']
                and type(response['http_status']) is int
                and (response['http_status'] == 429 or 500 <= response['http_status'] <= 599))
            routes = [r for r in bundle[1]['routes'] if r['endpoint'] == intent['url']
                and r['model'] == intent['payload']['model']
                and r['transport_sha256'] == _hash({'root': str(root.absolute()), 'configuration': intent.get('configuration')})]
            _require(len(routes) == 1)
            rules = [rule for rule in bundle[3]['rules'] if rule['binding'] == routes[0]['binding']
                     and rule['group'] == row['group'] and rule['constraint'] == row['constraint']]
            own_rule = [rule for rule in rules if rule['header'] == row['header']]
            _require(len(own_rule) == 1)
            headers = response['window_headers']
            _require(type(headers) is dict and headers['protocol'] == 'capacity-window-headers-v1'
                and type(headers['invalid']) is bool and type(headers['values']) is list
                and len(headers['values']) <= 8 and headers['observed_at'] == row['observed_at'])
            names = set()
            for pair in headers['values']:
                _require(type(pair) is list and len(pair) == 2 and pair[0] in HEADERS
                    and pair[0] not in names and type(pair[1]) is str and len(pair[1]) <= 160
                    and not any(ord(c) < 32 or ord(c) == 127 for c in pair[1]))
                names.add(pair[0])
            _require(sum(len(k.encode()) + len(v.encode()) for k, v in headers['values']) <= 1024)
            _require(row['status'] != 'wait' or headers['invalid'] is False and row['header'] in names)
            until, reason = _reported_until(headers, own_rule[0], bundle[3]['max_delay_seconds'])
            peers = [_reported_until(headers, rule, bundle[3]['max_delay_seconds'])[0] for rule in rules]
            known = [value for value in peers if value is not None]
            contradictory = bool(known) and max(known) - min(known) > 1
            _require(row['reported_not_before'] == until
                and row['status'] == ('unknown' if until is None or contradictory else 'wait')
                and row['not_before'] == (None if contradictory else until)
                and row['reason_code'] == ('capacity_window_source_contradictory' if contradictory else reason))
        return True
    return _memo(source, key, load)


def _references(source, access, current, rows):
    remaining = {row['id']: row for row in rows}
    tasks = [current['task'], *(task for task in access['tasks'] if task != current['task'])]
    blocked = None
    for task in tasks:
        try:
            legal = _legal_task(source, access, task)
            calls = _calls(source, access, legal)
        except SourceUnavailable as error:
            if source.limited or error.args[0] in ('source_work_limit', 'comparison_compute_limit'):
                raise
            blocked = error.args[0]
            continue
        except (OSError, ValueError, TypeError, KeyError, AttributeError, sqlite3.Error) as error:
            # A broken unrelated selected task is not an authority for, or a
            # prerequisite of, another task's reference. Global limits remain fatal.
            if (source.limited
                    or isinstance(error, OSError) and error.errno in (
                        errno.ENOMEM, errno.EMFILE, errno.ENFILE, errno.ENOSPC, errno.EDQUOT,
                        errno.EFBIG, errno.EINTR)
                    or isinstance(error, sqlite3.Error) and getattr(error, 'sqlite_errorcode', 0) & 255 in (
                        sqlite3.SQLITE_NOMEM, sqlite3.SQLITE_FULL, sqlite3.SQLITE_INTERRUPT, sqlite3.SQLITE_TOOBIG)):
                raise
            blocked = INVALID
            continue
        for call in calls:
            for identity, row in list(remaining.items()):
                expected = _hash({'receipt': call['response_sha256'], 'version': row['version'],
                    'physical': row['physical'], 'constraint': row['constraint'], 'header': row['header']})
                if expected == identity:
                    _receipt(source, access, row, call)
                    del remaining[identity]
        if not remaining:
            return
    raise SourceUnavailable(blocked or 'original_window_reference_unselected')


def verify_recorded_window(source, trusted, window, profile):
    """Return the frozen safe exclusion, or None for the verified saved open gate."""
    try:
        _require(type(window) is dict and set(window) == FIELDS
            and window['status'] in ('open', 'wait', 'unknown')
            and (window['not_before'] is None or _capacity_time(window['not_before']))
            and type(window['revision']) is int and 0 <= window['revision'] <= 2**63 - 1
            and type(window['evidence_refs']) is list and len(window['evidence_refs']) <= 32
            and len(window['evidence_refs']) == len(set(window['evidence_refs']))
            and all(type(ref) is str and HASH.fullmatch(ref) is not None for ref in window['evidence_refs']))
        access = trusted['comparison_access']
        _require(type(access) is dict and access['capacity_path'] is not None
            and type(access['tasks']) is list and 1 <= len(access['tasks']) <= LIMITS['max_tasks'])
        selected = [task for task in access['tasks'] if _task_id(task['client'], task['run_id']) == trusted['internal_run']]
        _require(len(selected) == 1, 'original_window_reference_unselected')
        legal = _legal_task(source, access, selected[0])
        _require(legal['registered'] == trusted['registered'])
        bundle = _bundle(source, access, legal)
        route, physical, rules = _scope(bundle, profile, legal['registered'])
        if bundle[3] is None:
            _require(window == {'status': 'open', 'not_before': None, 'evidence_refs': [],
                                'revision': 0, 'reason_code': 'capacity_window_not_applicable'})
            return None
        prefix = _prefix(source, access, bundle, physical, window['revision'])
        unknown = sorted((r for r in prefix if r['status'] == 'unknown'), key=lambda r: -r['sequence'])
        if window['status'] == 'open':
            _require(window['reason_code'] == 'capacity_window_open' and window['not_before'] is None
                and not window['evidence_refs'] and not unknown
                and (rules or route['binding'] in bundle[3]['not_applicable']))
            # No historical evaluation-now was saved. We validate the recorded
            # open gate/prefix, without reopening unreferenced raw or healing Health.
            return None
        if not window['evidence_refs']:
            _require(window['status'] == 'unknown' and window['not_before'] is None
                and window['reason_code'] == 'capacity_window_scope_unsupported'
                and not rules and route['binding'] not in bundle[3]['not_applicable'] and not unknown)
            return 'capacity_window_scope_unsupported'
        by_id = {r['id']: r for r in prefix}
        _require(all(ref in by_id for ref in window['evidence_refs']))
        referenced = [by_id[ref] for ref in window['evidence_refs']]
        waits = [r for r in referenced if r['status'] == 'wait']
        cutoff = min((r['not_before'] for r in waits), default=None)
        expected = unknown + sorted((r for r in prefix if r['status'] == 'wait'
            and cutoff is not None and r['not_before'] >= cutoff), key=lambda r: (-r['not_before'], -r['sequence']))
        _require([r['id'] for r in expected[:32]] == window['evidence_refs'])
        if unknown:
            _require(window['status'] == 'unknown' and window['not_before'] is None
                and window['reason_code'] == unknown[0]['reason_code'])
        else:
            _require(window['status'] == 'wait' and _capacity_time(window['not_before']) and waits
                and window['not_before'] == max(r['not_before'] for r in waits)
                and window['reason_code'] == 'capacity_window_wait')
        _references(source, access, legal, referenced)
        return window['reason_code']
    except SourceUnavailable:
        raise
    except (OSError, ValueError, TypeError, KeyError, AttributeError, sqlite3.Error):
        raise SourceUnavailable(INVALID) from None

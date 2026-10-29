"""從實際已送出的請求與回應，查看按身分隔離的前綴匹配提示與快取用量觀測（#46）。

只讀既有的 intent／response 收據，不新增 cache-control、不改 TTL、不重排訊息、
不預熱或探測，也不改變原路由排序。前綴身分是伺服器端、按客戶端帶密鑰的不可逆
摘要；公開投影不含原文、摘要材料或跨客戶可對照的內容指紋。
"""
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets

from .saved_comparisons import RUN, SourceUnavailable, _database, _read, _task_id, identifier, strict_json


SCHEMA = 'prefix-cache-observation-v1'
SERIALIZATION = 'jev-client-json-v1'
# 本機保守提示窗口；不是供應商 TTL，也不保證快取仍存在。
HINT_WINDOW_SECONDS = 300
LIMITS = {'tasks': 64, 'calls_per_task': 32, 'file_bytes': 1048576}
# 不屬於前綴設定身分的請求欄位：訊息本身另算，session_id 每個任務不同，max_tokens 不影響前綴。
EXCLUDED_SETTINGS = {'messages', 'session_id', 'max_tokens'}
PROFILE_FIELDS = ('model_version', 'provider', 'effort', 'execution_location', 'quantization')
HINTS = ('matchable_hint', 'expired', 'no_prior_match', 'not_applicable', 'unsupported')


def _seconds(value):
    """intent 的 started_at 是 ISO 8601（UTC）；無法解析就不參與時間比較。"""
    from datetime import datetime
    try:
        moment = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return moment.timestamp() if moment.tzinfo is not None else None


def _canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode()


def _secret(store_path):
    """每個 Monitor 主機一把私密金鑰，0600；不出現在任何回應。"""
    path = Path(store_path).parent / '.cache-observation.key'
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        value = _read(path, 64)
    else:
        value = secrets.token_bytes(32)
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
    if len(value) != 32:
        raise SourceUnavailable('cache_observation_key_invalid')
    return value


def _client_key(secret, client):
    return hmac.new(secret, b'client:' + client.encode(), hashlib.sha256).digest()


def _usage(response):
    """只有實際回應提供的快取欄位才算已觀測；缺欄位為未知，不填零。"""
    if type(response.get('http_status')) is not int or response.get('interrupted') or response.get('not_dispatched'):
        return {'status': 'not_applicable', 'reason': 'request_not_completed',
                'cache_read_tokens': None, 'cache_write_tokens': None}
    try:
        usage = json.loads(response['raw']).get('usage')
    except (TypeError, ValueError, AttributeError):
        usage = None
    if not isinstance(usage, dict):
        return {'status': 'unknown', 'reason': 'usage_missing', 'cache_read_tokens': None, 'cache_write_tokens': None}
    details = usage.get('prompt_tokens_details') if isinstance(usage.get('prompt_tokens_details'), dict) else {}
    read = details.get('cached_tokens', usage.get('cache_read_input_tokens'))
    write = details.get('cache_write_tokens', usage.get('cache_creation_input_tokens'))
    valid = [value for value in (read, write) if type(value) is int and value >= 0]
    if not valid:
        return {'status': 'unknown', 'reason': 'cache_usage_not_reported', 'cache_read_tokens': None,
                'cache_write_tokens': None}
    return {'status': 'observed', 'reason': 'response_usage_reported',
            'cache_read_tokens': read if type(read) is int and read >= 0 else None,
            'cache_write_tokens': write if type(write) is int and write >= 0 else None}


def _contracts(app):
    from cloudyfan_jev.budget import BudgetedCore
    from cloudyfan_jev.http_api import RegisteredContract
    from .integration import RunBoundCore
    result = {}
    for key, contract in app.gateway.service.contracts.items():
        if type(contract) is not RegisteredContract or type(contract.runner) is not BudgetedCore:
            continue
        core = contract.runner.core
        template = core._template if type(core) is RunBoundCore else core
        client = getattr(template, 'client', None)
        if client is None:
            continue
        result[key] = {'receipt_root': Path(client.root).absolute(), 'profiles': contract.descriptor().get('profiles') or {}}
    return result


def _calls(root, internal):
    calls = root / 'calls'
    prefix = 'run-' + internal + '-'
    try:
        with os.scandir(calls) as entries:
            names = sorted(entry.name for entry in entries if entry.name.startswith(prefix)
                           and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,159}', entry.name))
    except FileNotFoundError:
        return [], False
    rows = []
    for name in names[:LIMITS['calls_per_task']]:
        intent = strict_json(_read(calls / name / 'intent.json', LIMITS['file_bytes']))
        try:
            response = strict_json(_read(calls / name / 'response.json', LIMITS['file_bytes']))
        except FileNotFoundError:
            response = {'http_status': None, 'not_dispatched': True}  # 只有 intent：不能形成熱快取
        if intent.get('call_id') != name or not isinstance(intent.get('payload'), dict):
            raise SourceUnavailable('cache_observation_receipt_invalid')
        rows.append((intent, response))
    return rows, len(names) > LIMITS['calls_per_task']


def _scope(intent, profiles, root, client_key):
    payload = intent['payload']
    profile = next((row for row in profiles.values() if row.get('model_version') == payload.get('model')), None)
    if profile is None:
        return None
    return {'protocol': intent['url'], 'serialization': SERIALIZATION,
            'settings': {key: value for key, value in payload.items() if key not in EXCLUDED_SETTINGS},
            'profile': {key: profile.get(key) for key in PROFILE_FIELDS},
            # 帳號身分：同一個執行器收據根目錄；只以帶密鑰摘要比對，不輸出路徑。
            'account': hmac.new(client_key, b'account:' + str(root).encode(), hashlib.sha256).hexdigest()}


def _chain(client_key, scope, messages):
    """逐項累積的帶密鑰摘要；第 k 項代表 scope 加上前 k 則訊息。"""
    result, state = [], hmac.new(client_key, b'scope:' + _canonical(scope), hashlib.sha256).digest()
    for message in messages:
        state = hmac.new(client_key, state + _canonical(message), hashlib.sha256).digest()
        result.append(state.hex())
    return result


def _static(messages, prompt):
    """位於動態內容（任務 prompt）之前、逐項完整的連續訊息。"""
    static = []
    for message in messages:
        if not isinstance(message, dict) or prompt in _canonical(message).decode():
            break
        static.append(message)
    return static


def _load(app, client, run, now, contracts, client_key):
    with _database(app.store.path, 32768) as db:
        binding = db.execute('SELECT client,run,contract,created FROM bindings WHERE client=? AND run=?',
                             (client, run)).fetchone()
    if binding is None:
        return None
    internal = _task_id(client, run)
    service = app.gateway.service
    lifecycle = None
    if service.retention is not None:
        with _database(service.retention.path, 32768) as db:
            lifecycle = db.execute('SELECT expires,state FROM tasks WHERE id=?', (internal,)).fetchone()
    task = {'client': client, 'run_id': run, 'created_at': binding['created']}
    if lifecycle is None or lifecycle['state'] != 'active' or not now < lifecycle['expires']:
        return {**task, 'status': 'raw_access_expired', 'calls': []}
    contract = contracts.get(binding['contract'])
    if contract is None:
        return {**task, 'status': 'contract_unsupported', 'calls': []}
    job = strict_json(_read(Path(service.root) / internal / 'job.json', LIMITS['file_bytes']))
    prompt = job['request']['prompt']
    rows, truncated = _calls(contract['receipt_root'], internal)
    calls = []
    for index, (intent, response) in enumerate(rows):
        messages = intent['payload'].get('messages')
        scope = _scope(intent, contract['profiles'], contract['receipt_root'], client_key)
        static = _static(messages, prompt) if isinstance(messages, list) else []
        calls.append({'index': index, 'scope': scope, 'static': static,
                      'chain': _chain(client_key, scope, static) if scope is not None else [],
                      'started_at': intent.get('started_at'), 'moment': _seconds(intent.get('started_at')),
                      'response': response,
                      'sent': type(response.get('http_status')) is int and not response.get('interrupted')
                              and not response.get('not_dispatched'),
                      'model': intent['payload'].get('model'),
                      'provider': (scope or {}).get('profile', {}).get('provider')})
    return {**task, 'status': 'available', 'calls': calls, 'calls_truncated': truncated}


def observe(app, client, run):
    """操作員或該客戶端自己的單一任務觀測；不送出任何上游請求。"""
    if identifier(client) is None or not isinstance(run, str) or RUN.fullmatch(run) is None:
        raise SourceUnavailable('cache_observation_scope_invalid')
    now = app.store.clock()
    secret = _secret(app.store.path)
    client_key = _client_key(secret, client)
    contracts = _contracts(app)
    target = _load(app, client, run, now, contracts, client_key)
    if target is None:
        return None
    support = getattr(app, 'cache_support', None) or {}
    with _database(app.store.path, 32768) as db:
        others = [row['run'] for row in db.execute(
            'SELECT run FROM bindings WHERE client=? AND run<>? ORDER BY created DESC, run LIMIT ?',
            (client, run, LIMITS['tasks']))]
    # 只比對同一客戶端、仍在合法原文存取期內、實際送出且有回應的先前請求。
    earlier = []
    for other in others:
        try:
            loaded = _load(app, client, other, now, contracts, client_key)
        except (OSError, ValueError, KeyError, TypeError, SourceUnavailable):
            continue
        if loaded and loaded['status'] == 'available':
            earlier.extend(call for call in loaded['calls'] if call['sent'])
    earlier.extend(call for call in target['calls'] if call['sent'])
    projected = []
    for call in target['calls']:
        provider = call['provider']
        registered = support.get(provider) if isinstance(provider, str) else None
        usage = _usage(call['response'])
        prefix = {'messages': len(call['static']), 'bytes': len(_canonical(call['static'])) if call['static'] else 0,
                  'identity': call['chain'][-1][:32] if call['chain'] else None}
        hint = {'status': 'not_applicable', 'reason': None, 'matched_messages': 0, 'previous_started_at': None,
                'local_window_seconds': HINT_WINDOW_SECONDS,
                'source_ttl_seconds': registered.get('source_ttl_seconds') if registered else None}
        if registered is None or registered.get('data_policy') != 'compatible':
            hint.update(status='unsupported', reason='provider_cache_support_not_registered')
            usage = {**usage, 'status': 'unsupported', 'reason': 'provider_cache_support_not_registered'}
        elif call['scope'] is None:
            hint['reason'] = 'configuration_unmatched'
        elif not call['static']:
            hint['reason'] = 'no_static_prefix'
        else:
            best, when = 0, None
            for previous in earlier:
                if (previous is call or previous['moment'] is None or call['moment'] is None
                        or previous['moment'] >= call['moment']):
                    continue
                length = 0
                for mine, theirs in zip(call['chain'], previous['chain']):
                    if mine != theirs:
                        break
                    length += 1
                if length > best or length == best and length and previous['moment'] > when[0]:
                    best, when = length, (previous['moment'], previous['started_at'])
            if not best:
                hint.update(status='no_prior_match', reason='no_earlier_sent_request_with_identical_prefix')
            elif call['moment'] - when[0] > HINT_WINDOW_SECONDS:
                hint.update(status='expired', reason='outside_local_hint_window', matched_messages=best,
                            previous_started_at=when[1])
            else:
                hint.update(status='matchable_hint', reason='identical_leading_messages_in_same_scope',
                            matched_messages=best, previous_started_at=when[1])
        projected.append({'index': call['index'], 'sent': call['sent'], 'started_at': call['started_at'],
                          'model': call['model'], 'provider': provider, 'prefix': prefix, 'hint': hint,
                          'cache_usage': usage})
    return {'schema_version': SCHEMA, 'task': {key: target[key] for key in ('client', 'run_id', 'created_at')},
            'status': target['status'], 'calls': projected, 'calls_truncated': target.get('calls_truncated', False),
            'serialization': SERIALIZATION, 'local_hint_window_seconds': HINT_WINDOW_SECONDS,
            'note': 'hint_only_no_reordering_no_guarantee', 'adopted': False}


def catalog(app):
    """操作員可選的任務身分與已登記的供應商支援；不讀任何收據內容。"""
    with _database(app.store.path, 32768) as db:
        rows = [dict(row) for row in db.execute(
            'SELECT client,run,mode,created FROM bindings ORDER BY created DESC, client, run LIMIT 101')]
    support = getattr(app, 'cache_support', None) or {}
    return {'schema_version': 'prefix-cache-observation-catalog-v1',
            'tasks': [{'client': row['client'], 'run_id': row['run'], 'mode': row['mode'], 'created_at': row['created']}
                      for row in rows[:100] if identifier(row['client']) and isinstance(row['run'], str)
                      and RUN.fullmatch(row['run'])],
            'tasks_truncated': len(rows) > 100,
            'supported_providers': [{'provider': name, 'source_ttl_seconds': value.get('source_ttl_seconds')}
                                    for name, value in sorted(support.items())
                                    if isinstance(value, dict) and value.get('data_policy') == 'compatible'],
            'local_hint_window_seconds': HINT_WINDOW_SECONDS}

"""參考區塊準備的安全明細（#50）：只呈長度、方法、保留／省略理由與送出對照，不含原文。"""
import hashlib
import hmac
from pathlib import Path

from . import cache_observations as observe
from .saved_comparisons import RUN, SourceUnavailable, _database, _read, _task_id, identifier, strict_json


SCHEMA = 'context-preparation-observation-v1'
BLOCK_FIELDS = ('index', 'bytes', 'priority', 'requested_omittable', 'omittable', 'decision', 'reason')


def _user_content_sha(intent):
    messages = intent.get('payload', {}).get('messages')
    users = [m.get('content') for m in messages if isinstance(m, dict) and m.get('role') == 'user'] \
        if isinstance(messages, list) else []
    return hashlib.sha256(users[0].encode()).hexdigest() if users and isinstance(users[0], str) else None


def observe_preparation(app, client, run):
    if identifier(client) is None or not isinstance(run, str) or RUN.fullmatch(run) is None:
        raise SourceUnavailable('context_observation_scope_invalid')
    with _database(app.store.path, 32768) as db:
        binding = db.execute('SELECT contract FROM bindings WHERE client=? AND run=?', (client, run)).fetchone()
    if binding is None:
        return None
    internal, service = _task_id(client, run), app.gateway.service
    task = {'client': client, 'run_id': run}
    lifecycle = None
    if service.retention is not None:
        with _database(service.retention.path, 32768) as db:
            lifecycle = db.execute('SELECT expires,state FROM tasks WHERE id=?', (internal,)).fetchone()
    if lifecycle is None or lifecycle['state'] != 'active' or not app.store.clock() < lifecycle['expires']:
        # 到期不重開原文；安全中繼另由既有保留政策處理。
        return {'schema_version': SCHEMA, 'task': task, 'status': 'raw_access_expired', 'preparation': None}
    try:
        job = strict_json(_read(Path(service.root) / internal / 'job.json', 4 * 1048576))
    except FileNotFoundError:
        return {'schema_version': SCHEMA, 'task': task, 'status': 'not_created', 'preparation': None}
    prepared = job.get('prepared')
    if not isinstance(prepared, dict):
        return {'schema_version': SCHEMA, 'task': task, 'status': 'not_requested', 'preparation': None}
    if (hashlib.sha256(prepared['content'].encode()).hexdigest() != prepared['content_sha256']
            or job['registered'].get('context_policy', {}).get('version') != prepared['version']):
        return {'schema_version': SCHEMA, 'task': task, 'status': 'unverifiable', 'preparation': None}
    key = observe._client_key(observe._secret(app.store.path), client)
    sent = None
    contract = observe._contracts(app).get(binding['contract'])
    if contract is not None:
        calls, _ = observe._calls(contract['receipt_root'], internal)
        first = next((intent for intent, response in calls
                      if intent.get('url', '').endswith('/chat/completions')), None)
        if first is not None:
            sent = _user_content_sha(first) == prepared['content_sha256']
    return {'schema_version': SCHEMA, 'task': task, 'status': 'prepared', 'preparation': {
        'version': prepared['version'], 'format': prepared['format'], 'measure': prepared['measure'],
        'budget_bytes': prepared['budget_bytes'], 'original_bytes': prepared['original_bytes'],
        'prepared_bytes': prepared['prepared_bytes'],
        'qualification': {name: prepared['qualification'][name] for name in ('content_preparation_version', 'scope',
                                                                           'synthetic')},
        # 區塊識別由呼叫端提供，可能帶語意；只給每客戶端隔離的摘要。
        'blocks': [{**{name: row[name] for name in BLOCK_FIELDS},
                    'id_digest': hmac.new(key, b'block:' + row['id'].encode(), hashlib.sha256).hexdigest()[:16]}
                   for row in prepared['blocks']],
        'sent_matches_prepared': sent, 'local_only': True,
        'note': 'extractive_whole_blocks_no_summary_no_model_calls'}}

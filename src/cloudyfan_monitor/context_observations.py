"""參考區塊準備的安全明細（#50）：只呈長度、方法、保留／省略理由與送出對照，不含原文。"""
import hashlib
import hmac
from pathlib import Path

from . import cache_observations as observe
from .saved_comparisons import RUN, SourceUnavailable, _database, _read, _task_id, identifier, strict_json


SCHEMA = 'context-preparation-observation-v1'
# 付費前、建立紀錄前的拒絕；Core 不寫任務紀錄（零 POST），由 Monitor 保存安全原因。
REJECTION_CODES = {'required_context_over_limit', 'token_method_untrusted', 'context_preparation_not_qualified',
                   'context_preparation_timeout', 'preparation_unavailable_original_over_limit',
                   'output_reserve_exceeds_context', 'context_preparation_not_registered',
                   'context_preparation_version_not_registered', 'unsupported_context_fields',
                   'unsupported_context_format', 'invalid_context_block', 'duplicate_context_block_id',
                   'context_block_too_large', 'context_input_too_large', 'context_block_count_invalid'}


def _rejections(store):
    import sqlite3
    path = Path(store.path).parent / 'context-decisions.sqlite'
    db = sqlite3.connect(path, timeout=5, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute('CREATE TABLE IF NOT EXISTS rejections (client TEXT NOT NULL, run TEXT NOT NULL, code TEXT NOT NULL, '
               'request_sha256 TEXT NOT NULL, rejected_at REAL NOT NULL, PRIMARY KEY (client, run, request_sha256))')
    path.chmod(0o600)
    return db


def record_rejection(store, client, run, code, payload):
    """只存安全識別：客戶端、run_id、原因碼、請求雜湊與時間；不存原文。"""
    if code not in REJECTION_CODES:
        return
    request = hashlib.sha256(repr(sorted(payload.items())).encode()).hexdigest() if isinstance(payload, dict) else ''
    with _rejections(store) as db:
        db.execute('INSERT OR IGNORE INTO rejections VALUES (?, ?, ?, ?, ?)',
                   (client, run, code, request, store.clock()))
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
    if lifecycle is None:
        # 付費前拒絕的任務沒有生命週期紀錄；只讀安全拒絕紀錄，不涉及原文。
        with _rejections(app.store) as db:
            rows = db.execute('SELECT code, rejected_at FROM rejections WHERE client=? AND run=? ORDER BY rejected_at',
                              (client, run)).fetchall()
        if rows:
            return {'schema_version': SCHEMA, 'task': task, 'status': 'rejected_before_dispatch', 'preparation': None,
                    'rejections': [{'code': row['code'], 'rejected_at': row['rejected_at']} for row in rows]}
        return {'schema_version': SCHEMA, 'task': task, 'status': 'not_created', 'preparation': None}
    if lifecycle['state'] != 'active' or not app.store.clock() < lifecycle['expires']:
        # 到期不重開原文；安全中繼另由既有保留政策處理。
        return {'schema_version': SCHEMA, 'task': task, 'status': 'raw_access_expired', 'preparation': None}
    try:
        job = strict_json(_read(Path(service.root) / internal / 'job.json', 4 * 1048576))
    except FileNotFoundError:
        with _rejections(app.store) as db:
            rows = db.execute('SELECT code, rejected_at FROM rejections WHERE client=? AND run=? ORDER BY rejected_at',
                              (client, run)).fetchall()
        if rows:
            return {'schema_version': SCHEMA, 'task': task, 'status': 'rejected_before_dispatch', 'preparation': None,
                    'rejections': [{'code': row['code'], 'rejected_at': row['rejected_at']} for row in rows]}
        return {'schema_version': SCHEMA, 'task': task, 'status': 'not_created', 'preparation': None}
    prepared = job.get('prepared')
    if not isinstance(prepared, dict):
        return {'schema_version': SCHEMA, 'task': task, 'status': 'not_requested', 'preparation': None}
    if (hashlib.sha256(prepared['content'].encode()).hexdigest() != prepared['content_sha256']
            or job['registered'].get('context_policy', {}).get('version') != prepared['version']):
        return {'schema_version': SCHEMA, 'task': task, 'status': 'unverifiable', 'preparation': None}
    key = observe._client_key(observe._secret(app.store.path), client)
    sent, provider_prompt_tokens, generation_requests = None, None, 0
    contract = observe._contracts(app).get(binding['contract'])
    if contract is not None:
        calls, _ = observe._calls(contract['receipt_root'], internal)
        generations = [(intent, response) for intent, response in calls
                       if intent.get('url', '').endswith('/chat/completions')]
        generation_requests = len(generations)
        if generations:
            sent = _user_content_sha(generations[0][0]) == prepared['content_sha256']
            # 供應商實際回報的輸入 token 與本機計數分開；沒有就是未知。
            provider_prompt_tokens = observe._tokens(generations[0][1])['prompt_tokens']
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
        'outcome': prepared.get('outcome', 'prepared'), 'outcome_reason': prepared.get('outcome_reason'),
        'fallback_policy': prepared.get('fallback_policy', 'reject'), 'sent_bytes': prepared.get('sent_bytes'),
        'token_estimate': prepared.get('token_estimate'), 'token_method': prepared.get('token_method', 'unavailable'),
        'provider_prompt_tokens': provider_prompt_tokens, 'generation_requests': generation_requests,
        'note': 'extractive_whole_blocks_no_summary_no_model_calls'}}

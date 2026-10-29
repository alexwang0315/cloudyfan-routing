"""明示採用的快取親和性政策（#47）：只在 Core 交來的等價候選中，以可信前綴提示作次要排序。

- 政策有版本；明示啟用只影響之後建立的任務，任務建立時凍結版本，重送沿用原版本。
- 每個任務、每個決策點只決定一次並持久保存；排隊與實際執行、重播都讀同一筆。
- 提示只用同一客戶端、同一設定、實際送出且有回應的先前請求（見 cache_observations）。
- 不預熱、不探測、不競速，不改成本估計或品質；沒有提示就沿原排序。
"""
import json
import os
from pathlib import Path
import sqlite3
import threading
import time

from cloudyfan_jev.client import CHAT

from . import cache_observations as observe
from .saved_comparisons import _task_id
from .store import MonitorError, digest


VERSIONS = {'prefix-affinity-v1': {
    'equivalence': 'core_identical_complete_cost_and_speed_intervals_after_all_gates',
    'hint': 'same_client_sent_identical_leading_messages_within_local_window',
    'local_window_seconds': observe.HINT_WINDOW_SECONDS,
    'data_use': 'same_client_sent_requests_within_raw_access_period'}}
SCHEMA = 'cache-affinity-v1'


class CacheAffinity:
    def __init__(self, app):
        self.app = app
        self.path = Path(app.store.path).parent / 'cache-affinity.sqlite'
        self._lock = threading.Lock()
        with self._db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS control (singleton INTEGER PRIMARY KEY CHECK (singleton=1), '
                       'revision INTEGER NOT NULL, version TEXT, fingerprint TEXT NOT NULL, changed_at REAL NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS bindings (internal TEXT PRIMARY KEY, client TEXT NOT NULL, '
                       'run TEXT NOT NULL, version TEXT, bound_at REAL NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS decisions (internal TEXT NOT NULL, decision_key TEXT NOT NULL, '
                       'answer TEXT NOT NULL, decided_at REAL NOT NULL, PRIMARY KEY (internal, decision_key))')
        os.chmod(self.path, 0o600)
        app.cache_affinity = self
        # 只把 port 掛到受控的 RunBoundCore；其他 runner 不受影響。
        from .integration import RunBoundCore
        for contract in app.gateway.service.contracts.values():
            core = getattr(getattr(contract, 'runner', None), 'core', None)
            if type(core) is RunBoundCore:
                core.__dict__['affinity_provider'] = self.port_for

    def _db(self):
        db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        db.row_factory = sqlite3.Row
        return db

    @staticmethod
    def _fingerprint(version):
        return digest({'version': version, 'definition': VERSIONS.get(version)})

    def snapshot(self):
        with self._db() as db:
            row = db.execute('SELECT * FROM control WHERE singleton=1').fetchone()
        if row is not None and row['fingerprint'] != self._fingerprint(row['version']):
            raise MonitorError('cache_affinity_policy_unavailable', 503)
        return {'schema_version': SCHEMA, 'revision': row['revision'] if row else 0,
                'active_version': row['version'] if row else None,
                'available_versions': [{'version': name, **definition} for name, definition in VERSIONS.items()],
                'scope': 'New tasks only; existing, resent and historical tasks keep their frozen version.'}

    def activate(self, revision, version):
        if type(revision) is not int or revision < 0:
            raise MonitorError('invalid_cache_affinity_revision')
        if version is not None and (type(version) is not str or version not in VERSIONS):
            raise MonitorError('cache_affinity_policy_not_registered', 409)
        with self._lock, self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT revision FROM control WHERE singleton=1').fetchone()
            if (row['revision'] if row else 0) != revision:
                db.execute('ROLLBACK')
                raise MonitorError('stale_cache_affinity_revision', 409)
            db.execute('INSERT INTO control VALUES (1, ?, ?, ?, ?) ON CONFLICT(singleton) DO UPDATE SET '
                       'revision=excluded.revision, version=excluded.version, fingerprint=excluded.fingerprint, '
                       'changed_at=excluded.changed_at',
                       (revision + 1, version, self._fingerprint(version), self.app.store.clock()))
            db.execute('COMMIT')
        return self.snapshot()

    def bind(self, client, body):
        """任務接受前凍結當時的政策版本；同一任務重送保留第一次的版本。"""
        run = body.get('run_id') if isinstance(body, dict) else None
        if not isinstance(run, str) or observe.RUN.fullmatch(run) is None:
            return
        version = self.snapshot()['active_version']
        with self._db() as db:
            db.execute('INSERT OR IGNORE INTO bindings VALUES (?, ?, ?, ?, ?)',
                       (_task_id(client, run), client, run, version, self.app.store.clock()))

    def port_for(self, internal):
        with self._db() as db:
            row = db.execute('SELECT client, run, version FROM bindings WHERE internal=?', (internal,)).fetchone()
        if row is None or row['version'] is None:
            return None  # 未採用政策：Core 完全沿用原排序
        client, run, version = row['client'], row['run'], row['version']

        def port(request):
            key = request['decision_key']
            with self._db() as db:
                saved = db.execute('SELECT answer FROM decisions WHERE internal=? AND decision_key=?',
                                   (internal, key)).fetchone()
            if saved is not None:
                return json.loads(saved['answer'])
            answer = self._decide(client, run, version, request)
            with self._db() as db:
                db.execute('INSERT OR IGNORE INTO decisions VALUES (?, ?, ?, ?)',
                           (internal, key, json.dumps(answer, sort_keys=True), self.app.store.clock()))
                stored = db.execute('SELECT answer FROM decisions WHERE internal=? AND decision_key=?',
                                    (internal, key)).fetchone()
            return json.loads(stored['answer'])
        return port

    def _decide(self, client, run, version, request):
        """回傳等價集合內最長的可信相同前綴；沒有就不偏好任何候選。"""
        # 提示窗口與收據的 started_at 用同一個真實時鐘；原文存取到期仍依服務時鐘。
        app, now = self.app, time.time()
        service_now = app.store.clock()
        answer = {'policy_version': version, 'preferred': None, 'reason': 'no_earlier_sent_request_with_identical_prefix',
                  'observed_at': now, 'matched_messages': 0, 'previous_started_at': None}
        support = getattr(app, 'cache_support', None) or {}
        secret = observe._secret(app.store.path)
        client_key = observe._client_key(secret, client)
        contracts = observe._contracts(app)
        with observe._database(app.store.path, 32768) as db:
            binding = db.execute('SELECT contract FROM bindings WHERE client=? AND run=?', (client, run)).fetchone()
            others = [row['run'] for row in db.execute(
                'SELECT run FROM bindings WHERE client=? AND run<>? ORDER BY created DESC, run LIMIT ?',
                (client, run, observe.LIMITS['tasks']))]
        contract = contracts.get(binding['contract']) if binding is not None else None
        if contract is None:
            return {**answer, 'reason': 'contract_unsupported'}
        job = observe.strict_json(observe._read(Path(app.gateway.service.root) / _task_id(client, run) / 'job.json',
                                                observe.LIMITS['file_bytes']))
        earlier = []
        for other in others:
            try:
                loaded = observe._load(app, client, other, service_now, contracts, client_key)
            except (OSError, ValueError, KeyError, TypeError, observe.SourceUnavailable):
                continue
            if loaded and loaded['status'] == 'available':
                earlier.extend(call for call in loaded['calls'] if call['sent'] and call['moment'] is not None)
        best = None
        for candidate in request['candidates']:
            intent = {'url': CHAT, 'payload': candidate['payload']}
            scope = observe._scope(intent, contract['profiles'], contract['receipt_root'], client_key)
            provider = (scope or {}).get('profile', {}).get('provider')
            registered = support.get(provider) if isinstance(provider, str) else None
            if scope is None or registered is None or registered.get('data_policy') != 'compatible':
                continue
            static = observe._static(candidate['payload'].get('messages') or [], job['request']['prompt'])
            chain = observe._chain(client_key, scope, static)
            for previous in earlier:
                if now - previous['moment'] > observe.HINT_WINDOW_SECONDS or previous['moment'] > now:
                    continue
                length = 0
                for mine, theirs in zip(chain, previous['chain']):
                    if mine != theirs:
                        break
                    length += 1
                if length and (best is None or length > best[1]):
                    best = (candidate['config_id'], length, previous['started_at'])
        if best is None:
            return answer
        return {**answer, 'preferred': best[0], 'reason': 'identical_leading_messages_in_same_scope',
                'matched_messages': best[1], 'previous_started_at': best[2]}

    def decisions(self, client, run):
        """任務當時的凍結版本、主機提示，以及 Core 實際採用／略過與實際送出的模型。"""
        internal = _task_id(client, run)
        with self._db() as db:
            binding = db.execute('SELECT version, bound_at FROM bindings WHERE internal=?', (internal,)).fetchone()
            rows = db.execute('SELECT decision_key, answer, decided_at FROM decisions WHERE internal=? '
                              'ORDER BY decision_key', (internal,)).fetchall()
        hints = {row['decision_key']: {**json.loads(row['answer']), 'decided_at': row['decided_at']} for row in rows}
        applied, sent = {}, []
        contracts = observe._contracts(self.app)
        with observe._database(self.app.store.path, 32768) as db:
            row = db.execute('SELECT contract FROM bindings WHERE client=? AND run=?', (client, run)).fetchone()
        contract = contracts.get(row['contract']) if row is not None else None
        if contract is not None:
            directory = contract['receipt_root'] / 'runs' / internal
            for index in range(9):
                try:
                    saved = observe.strict_json(observe._read(directory / f'backup-selection-{index}.json', 1048576))
                except FileNotFoundError:
                    break
                applied.update(saved.get('affinity') or {})
            calls, _ = observe._calls(contract['receipt_root'], internal)
            sent = [{'call_index': number, 'model': intent['payload'].get('model'),
                     'sent': type(response.get('http_status')) is int and not response.get('not_dispatched')}
                    for number, (intent, response) in enumerate(calls)]
        keys = sorted(set(hints) | set(applied))
        return {'frozen_version': binding['version'] if binding else None,
                'bound_at': binding['bound_at'] if binding else None,
                'decisions': [{'decision_key': key, 'hint': hints.get(key),
                               'applied': {name: applied[key].get(name) for name in (
                                   'tier', 'equivalent_config_ids', 'original_config_id', 'selected_config_id',
                                   'adopted', 'reason')} if key in applied else None} for key in keys],
                'sent_models': sent}

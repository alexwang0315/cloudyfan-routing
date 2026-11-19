"""主服務接上契約經驗庫檔案與改寫器（ADR 0036 第 5 點）。

- ``open_experience(path)``：主服務組裝用。讀既有經驗庫檔案（沒有就建新的），改寫一律延後
  （``defer_rewrite``），回傳帶檔案路徑的 ``ExperienceLink``；之後每次回寫都持檔案鎖寫回。
- ``rewrite_pending(runner, path, profile, ...)``：批次改寫。用開源顧問把待改寫的共用事實
  改寫成通用敘述，先以 ``BudgetedCore`` 的上界預留「筆數 × 上界」，呼叫編號由事實與支持集合
  決定（續跑同一批讀舊收據不重付），結果經去識別檢查後持鎖寫回。

作答流程中永遠不呼叫改寫模型，所以每題的最壞預留不必包含改寫。
"""
from copy import copy
import json
from pathlib import Path

from . import client as transport
from . import routing
from .budget import _BudgetClient, _exact, _fingerprint
from .experience import ExperienceStore, defer_rewrite
from .experience_card import ExperienceLink
from .privacy import PolicyDenied

REWRITE_VERSION = 'experience-rewrite-v1'
MAX_STATEMENT_CHARS = 400
REWRITE_SYSTEM = (
    'You rewrite one structured solving fact from a contract experience library into a single '
    'general sentence that helps future tasks of the same contract. The input has only the contract '
    'scope, the fact kind and its code. Reply with the sentence only. Do not mention any customer, '
    'task, table, column or identifier, and do not add instructions beyond the fact itself.')


def open_experience(path):
    """主服務組裝：回傳接上 ``path`` 的經驗庫連結；檔案不存在時第一次回寫才建立。"""
    path = Path(path)
    if path.is_symlink():
        raise ValueError('Experience store path cannot be a symlink')
    store = (ExperienceStore.open(path, rewriter=defer_rewrite) if path.exists()
             else ExperienceStore(rewriter=defer_rewrite))
    return ExperienceLink(store, path)


def rewrite_call_id(fid, support_sha256):
    return f'experience-rewrite-{fid}-{support_sha256[:16]}'


def rewrite_messages(item):
    content = {'scope': item['scope'], 'kind': item['kind'], 'code': item['code']}
    routing._reject_private(content)
    return [{'role': 'system', 'content': REWRITE_SYSTEM},
            {'role': 'user', 'content': json.dumps(content, sort_keys=True, ensure_ascii=False)}]


def _statement(content):
    """回覆只收單行、長度有限的敘述；其他一律交給 apply_rewrite 當成不合格（rejected）。"""
    if not isinstance(content, str):
        return None
    text = content.strip()
    if not text or '\n' in text or len(text) > MAX_STATEMENT_CHARS:
        return None
    return text


def _require(profile, core):
    if not isinstance(profile, dict) or profile.get('open_source') is not True:
        raise ValueError('Experience rewriter must be an explicitly open-source advisor profile')
    if core.execution_policy is not None:
        location = profile.get('execution_location')
        core.execution_policy.require('generation', location)
        if not isinstance(location, str) or not location.startswith('cloud:'):
            raise PolicyDenied('generation', 'backend_location_mismatch')


def rewrite_pending(runner, path, profile, *, run_id, limit):
    """改寫最多 ``limit`` 條待改寫事實，回傳摘要。沒有待改寫時不預留也不呼叫。"""
    if type(limit) is not int or limit < 1:
        raise ValueError('limit must be a positive integer')
    path = Path(path)
    _require(profile, runner.core)
    if not path.exists():
        return {'run_id': run_id, 'attempted': 0, 'accepted': [], 'rejected': [], 'stale': [],
                'provider_rejected': []}
    pending = ExperienceStore.open(path, rewriter=defer_rewrite).pending_rewrites()[:limit]
    summary = {'run_id': run_id, 'attempted': len(pending), 'accepted': [], 'rejected': [], 'stale': [],
               'provider_rejected': []}
    if not pending:
        return summary
    cap = runner._model_cap(profile, 'model_version')
    frozen = {'version': REWRITE_VERSION, 'system': REWRITE_SYSTEM, 'profile': profile,
              'store': str(path.resolve()), 'pending': pending,
              'bound': str(cap), 'root': str(runner.core.client.root.resolve())}
    runner.sync_client(runner.core.client)
    runner.store.reserve(run_id, _fingerprint(frozen), _exact((cap,), len(pending)))
    engine = copy(runner.core)
    engine.client = _BudgetClient(runner.core.client, runner, run_id)
    results = []
    for item in pending:
        cid = rewrite_call_id(item['fact_id'], item['support_sha256'])
        payload = engine.generation_payload(rewrite_messages(item), profile, cid, None)
        record, info = engine._checked(cid, transport.CHAT, payload, profile['accepted_models'],
                                       profile['provider_name'], allow_rejection=True)
        if info.get('provider_rejected'):
            summary['provider_rejected'].append(item['fact_id'])
            continue
        content = json.loads(record['raw'])['choices'][0]['message'].get('content')
        results.append((item, _statement(content)))
    # 與 BudgetedCore.run 相同：只有整批呼叫都完成才釋放預留；中途失敗時同一 run_id 可續跑。
    runner.store.finish(run_id)

    def apply(store):
        return [(item['fact_id'], store.apply_rewrite(item['fact_id'], item['support_sha256'], statement))
                for item, statement in results]
    if results:
        _, outcomes = ExperienceStore.update(path, apply, rewriter=defer_rewrite)
        for fid, outcome in outcomes:
            summary[outcome].append(fid)
    return summary

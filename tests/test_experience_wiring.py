"""主服務接上契約經驗庫檔案與改寫器（ADR 0036 第 5 點）。

作答時只把新達共用門檻的事實標成待改寫，不呼叫模型；改寫由另外的批次步驟用開源顧問
完成，先預留預算，呼叫編號固定。全部離線：只用 httpx.MockTransport。
"""
from datetime import datetime, timezone
import json

import httpx
import pytest

from cloudyfan_jev.advisor import ADVISOR_FIRST
from cloudyfan_jev.budget import Authorization, BudgetedCore, BudgetError, BudgetStore, EnforcedBound
from cloudyfan_jev.client import Client
from cloudyfan_jev.demo import ArithmeticEnvironment, example_config
from cloudyfan_jev.engine import Core
from cloudyfan_jev.experience import ExperienceStore, defer_rewrite, fact_id
from cloudyfan_jev.experience_card import ExperienceLink
from cloudyfan_jev.experience_rewrite import open_experience, rewrite_call_id, rewrite_pending
from test_advisor_first import API, TASK, config, decision, role_profile

SCOPE = 'arith_v1'
FACT = {'kind': 'check_code', 'code': 'integer_sum_contract_failed'}
FID = fact_id(SCOPE, FACT['kind'], FACT['code'])
ADVISOR = role_profile('advisor')
STATEMENT = 'Tasks of this kind often fail the integer sum check; verify the final number before answering.'


def observe(store, task, client, day=1):
    store.record({'task_id': task, 'client_id': client, 'scope': SCOPE,
                  'settings': store.freeze_settings(client), 'created_at': f'2026-11-{day:02d}',
                  'supports': [FACT], 'outcomes': []})


def shared_ready(path, clients=('c1', 'c2', 'c3')):
    """三個客戶端各一題支持同一事實：達到共用門檻，改寫延後。"""
    link = open_experience(path)
    for i, client in enumerate(clients):
        link.record({'task_id': f't{i}', 'client_id': client, 'scope': SCOPE,
                     'settings': link.store.freeze_settings(client), 'created_at': '2026-11-01',
                     'supports': [FACT], 'contradicts': [], 'outcomes': []})
    return link


class RewriteAPI:
    def __init__(self, content=STATEMENT):
        self.content, self.requests = content, []

    def __call__(self, request):
        body = json.loads(request.content)
        self.requests.append(body)
        return httpx.Response(200, json={'id': f'gen-{len(self.requests)}', 'model': body['model'],
            'provider': 'Example', 'usage': {'cost': .001},
            'choices': [{'message': {'role': 'assistant', 'content': self.content}, 'finish_reason': 'stop'}]})


def runner(tmp_path, api, *, bounds=None):
    http = httpx.Client(transport=httpx.MockTransport(api))
    profiles, decision_profile = example_config()
    core = Core(Client(http, 'mock-only-key', tmp_path / 'client'), profiles, decision_profile)
    store = BudgetStore(tmp_path / 'budget.sqlite', clock=lambda: datetime(2026, 11, 19, tzinfo=timezone.utc))
    store.authorize(Authorization('approved', '1', '2026-11-20T00:00:00+00:00'))
    usd = bounds if bounds is not None else {'oss/advisor-v1': '.002'}
    return BudgetedCore(core, store, model_bounds={k: EnforcedBound(v, 'offline fixture') for k, v in usd.items()},
                        tool_bounds={}, verify_bound=lambda *a: True)


def test_writes_mark_newly_shared_fact_pending_without_calling_any_model(tmp_path):
    shared_ready(tmp_path / 'experience.json')
    saved = ExperienceStore.open(tmp_path / 'experience.json', rewriter=defer_rewrite)
    assert [(p['fact_id'], p['kind'], p['code']) for p in saved.pending_rewrites()] == [
        (FID, FACT['kind'], FACT['code'])]
    # 尚未改寫：共用層沒有任何事實。
    assert saved.facts_for('c9', SCOPE)['shared'] == []


def test_open_experience_creates_store_once_and_reuses_file(tmp_path):
    path = tmp_path / 'experience.json'
    first = open_experience(path)
    assert not path.exists()
    observe_link = shared_ready(path)
    salt = ExperienceStore.open(path, rewriter=defer_rewrite).export()['body']['salt']
    again = open_experience(path)
    assert isinstance(again, ExperienceLink) and again.path == path
    assert again.store.export()['body']['salt'] == salt
    assert first is not observe_link


def test_rewrite_pending_calls_open_source_advisor_and_publishes_statement(tmp_path):
    path = tmp_path / 'experience.json'
    shared_ready(path)
    api = RewriteAPI()
    summary = rewrite_pending(runner(tmp_path, api), path, ADVISOR, run_id='rewrite-20261119', limit=5)
    assert summary['attempted'] == 1 and summary['accepted'] == [FID]
    assert [r['model'] for r in api.requests] == ['oss/advisor-v1']
    # 改寫器只看到範圍與代碼。
    user = json.loads(api.requests[0]['messages'][1]['content'])
    assert user == {'scope': SCOPE, 'kind': FACT['kind'], 'code': FACT['code']}
    saved = ExperienceStore.open(path, rewriter=defer_rewrite)
    assert saved.pending_rewrites() == []
    assert [f['statement'] for f in saved.facts_for('c9', SCOPE)['shared']] == [STATEMENT]
    # 再跑一次：沒有待改寫，不再呼叫也不預留。
    assert rewrite_pending(runner(tmp_path, api), path, ADVISOR, run_id='rewrite-20261119b', limit=5)['attempted'] == 0
    assert len(api.requests) == 1


def test_rewrite_reserves_budget_and_uses_fixed_call_ids(tmp_path):
    path = tmp_path / 'experience.json'
    shared_ready(path)
    app = runner(tmp_path, RewriteAPI())
    rewrite_pending(app, path, ADVISOR, run_id='rewrite-a', limit=3)
    report = app.store.report()
    reservation = next(r for r in report['reservations'] if r['run'] == 'rewrite-a')
    # 只有一條待改寫：預留 1 × 上界。
    assert reservation['amount'] == '0.002'
    support = ExperienceStore.open(path, rewriter=defer_rewrite).export()['body']['rewrites'][FID]
    assert support['status'] == 'accepted'
    calls = sorted(p.name for p in (tmp_path / 'client' / 'calls').iterdir())
    assert len(calls) == 1 and calls[0].startswith('experience-rewrite-' + FID + '-')


def test_interrupted_batch_resumes_with_same_run_id_without_paying_again(tmp_path, monkeypatch):
    path = tmp_path / 'experience.json'
    shared_ready(path)
    api = RewriteAPI()
    real_update = ExperienceStore.update

    def crash(*args, **kwargs):
        raise OSError('process stopped before write-back')
    monkeypatch.setattr(ExperienceStore, 'update', crash)
    with pytest.raises(OSError):
        rewrite_pending(runner(tmp_path, api), path, ADVISOR, run_id='rewrite-r', limit=5)
    monkeypatch.setattr(ExperienceStore, 'update', real_update)
    summary = rewrite_pending(runner(tmp_path, api), path, ADVISOR, run_id='rewrite-r', limit=5)
    assert summary['accepted'] == [FID]
    assert len(api.requests) == 1  # 讀回舊收據，不重付


def test_rewrite_call_id_is_deterministic_per_fact_and_support():
    assert rewrite_call_id(FID, 'a' * 64) == rewrite_call_id(FID, 'a' * 64)
    assert rewrite_call_id(FID, 'a' * 64) != rewrite_call_id(FID, 'b' * 64)


def test_identifying_statement_is_rejected_and_not_shared(tmp_path):
    path = tmp_path / 'experience.json'
    shared_ready(path)
    summary = rewrite_pending(runner(tmp_path, RewriteAPI('Client c2 task t1 failed this check.')),
                              path, ADVISOR, run_id='rewrite-b', limit=5)
    assert summary['rejected'] == [FID]
    saved = ExperienceStore.open(path, rewriter=defer_rewrite)
    assert saved.facts_for('c9', SCOPE)['shared'] == [] and saved.pending_rewrites() == []


def test_new_support_after_rejection_marks_fact_pending_again(tmp_path):
    path = tmp_path / 'experience.json'
    link = shared_ready(path)
    rewrite_pending(runner(tmp_path, RewriteAPI('Client c2 failed.')), path, ADVISOR, run_id='rewrite-c', limit=5)
    link.record({'task_id': 't9', 'client_id': 'c4', 'scope': SCOPE, 'settings': link.store.freeze_settings('c4'),
                 'created_at': '2026-11-02', 'supports': [FACT], 'contradicts': [], 'outcomes': []})
    assert [p['fact_id'] for p in ExperienceStore.open(path, rewriter=defer_rewrite).pending_rewrites()] == [FID]


def test_stale_support_keeps_fact_pending(tmp_path):
    path = tmp_path / 'experience.json'
    link = shared_ready(path)
    store = ExperienceStore.open(path, rewriter=defer_rewrite)
    old = store.pending_rewrites()[0]
    link.record({'task_id': 't9', 'client_id': 'c4', 'scope': SCOPE, 'settings': link.store.freeze_settings('c4'),
                 'created_at': '2026-11-02', 'supports': [FACT], 'contradicts': [], 'outcomes': []})

    def apply(latest):
        return latest.apply_rewrite(old['fact_id'], old['support_sha256'], STATEMENT)
    latest, outcome = ExperienceStore.update(path, apply, rewriter=defer_rewrite)
    assert outcome == 'stale'
    assert [p['fact_id'] for p in latest.pending_rewrites()] == [FID]


def test_limit_caps_calls_and_reservation(tmp_path):
    path = tmp_path / 'experience.json'
    link = shared_ready(path)
    other = {'kind': 'check_code', 'code': 'second_failure'}
    for i, client in enumerate(('c1', 'c2', 'c3')):
        link.record({'task_id': f'u{i}', 'client_id': client, 'scope': SCOPE,
                     'settings': link.store.freeze_settings(client), 'created_at': '2026-11-03',
                     'supports': [other], 'contradicts': [], 'outcomes': []})
    api = RewriteAPI()
    app = runner(tmp_path, api)
    summary = rewrite_pending(app, path, ADVISOR, run_id='rewrite-d', limit=1)
    assert summary['attempted'] == 1 and len(api.requests) == 1
    assert next(r for r in app.store.report()['reservations'] if r['run'] == 'rewrite-d')['amount'] == '0.002'
    assert len(ExperienceStore.open(path, rewriter=defer_rewrite).pending_rewrites()) == 1


def test_rewriter_must_be_open_source_and_bounded(tmp_path):
    path = tmp_path / 'experience.json'
    shared_ready(path)
    api = RewriteAPI()
    with pytest.raises(ValueError, match='open-source'):
        rewrite_pending(runner(tmp_path, api), path, {**ADVISOR, 'open_source': False}, run_id='r1', limit=1)
    with pytest.raises(BudgetError):
        rewrite_pending(runner(tmp_path / 'b', api, bounds={}), path, ADVISOR, run_id='r2', limit=1)
    assert api.requests == []
    with pytest.raises(ValueError):
        rewrite_pending(runner(tmp_path / 'c', api), path, ADVISOR, run_id='r3', limit=0)


def test_card_rereads_file_so_opt_out_from_another_process_applies(tmp_path):
    """Monitor 在另一個程序關掉「使用」：主服務記憶體裡的經驗庫是舊的，組卡前要重讀檔案。"""
    path = tmp_path / 'experience.json'
    shared_ready(path)
    rewrite_pending(runner(tmp_path / 'rw', RewriteAPI()), path, ADVISOR, run_id='rewrite-e', limit=5)
    link = open_experience(path)  # 主服務啟動時讀到的內容：共用事實已可用

    def card_for(client, run_id):
        api = API(decision('answer', '2'))
        http = httpx.Client(transport=httpx.MockTransport(api))
        profiles, decision_profile = example_config()
        core = Core(Client(http, 'mock-only-key', tmp_path / ('client-' + run_id)), profiles, decision_profile,
                    advisor_first=config(experience=link))
        core.run(run_id, TASK, ArithmeticEnvironment, contract_version='v1', mode=ADVISOR_FIRST,
                 experience={'client_id': client, 'scope': SCOPE, 'created_at': '2026-11-19'})
        return json.loads(api.requests[0]['messages'][1]['content'])['experience_card']

    assert card_for('c8', 'before')['facts'][0]['statement'] == STATEMENT

    def opt_out(store):
        store.set_client_settings('c9', use=False)
    ExperienceStore.update(path, opt_out, rewriter=defer_rewrite)
    assert card_for('c9', 'after')['facts'] == []

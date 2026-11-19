"""任務服務把契約經驗的客戶端與契約範圍帶進路徑二（ADR 0036 第 4、8、10 點）。

客戶＝客戶端（ADR 0009）：client_id 取自驗證後的 principal；scope 是註冊契約 ID；
created_at 是建題日期。三者在建題時凍結進 job.json，重送不改、執行時原樣交給 Core。
全部離線：只用 httpx.MockTransport／WSGITransport。
"""
import json

import httpx

from cloudyfan_jev.advisor import ADVISOR_FIRST, AdvisorConfig
from cloudyfan_jev.budget import EnforcedBound
from cloudyfan_jev.experience import ExperienceStore
from cloudyfan_jev.experience_card import ExperienceLink
from cloudyfan_jev.http_api import HTTPApp, RegisteredContract, TaskService
from cloudyfan_jev.privacy import ExecutionPolicy
from test_budget import controlled
from test_engine import ArithmeticEnvironment
from test_http_api import TASK, block_real_network  # noqa: F401  (autouse：禁止真實連線)
from test_http_advisor_incomplete import role_profile

DAY = 1795046400.0  # 2026-11-19T00:00:00Z


class AnswerAPI:
    def __init__(self):
        self.requests = []

    def __call__(self, request):
        p = json.loads(request.content)
        self.requests.append(p)
        content = (json.dumps({'mode': 'answer', 'answer': '2', 'recommendations': [
            {'setting': 'mid', 'tier': 'mid', 'reason': 'r'}, {'setting': 'high', 'tier': 'high', 'reason': 'r'}]})
            if p['model'] == 'oss/advisor-v1' else '2')
        return httpx.Response(200, json={'id': f'gen-{len(self.requests)}', 'model': p['model'],
            'provider': 'Example', 'usage': {'cost': .002},
            'choices': [{'message': {'role': 'assistant', 'content': content}, 'finish_reason': 'stop'}]})


def service(tmp_path, *, experience=True, principal='client-a'):
    api = AnswerAPI()
    runner = controlled(tmp_path / 'runner', api)
    runner.core.execution_policy = ExecutionPolicy(mode='unrestricted', origin='home:fixture',
                                                   classifier='cloud:typesafe')
    for profile in runner.core.profiles.values():
        profile['execution_location'] = 'cloud:fixture'
    store = ExperienceStore(rewriter=lambda fact: '通用敘述')
    link = ExperienceLink(store, tmp_path / 'experience.json') if experience else None
    runner.core.advisor_first = AdvisorConfig(advisor=role_profile('advisor'), judge=role_profile('judge'),
                                              score_snapshot='snapshot-x', experience=link)
    for model in ('oss/advisor-v1', 'oss/judge-v1'):
        runner.model_bounds[model] = EnforcedBound('.003', 'offline hard cap')
    contract = RegisteredContract('public-v1', runner, lambda workspace: ArithmeticEnvironment(), mode=ADVISOR_FIRST)
    svc = TaskService(tmp_path / 'service', contracts={'arithmetic': contract}, allowed_principal=principal,
                      clock=lambda: DAY)
    client = httpx.Client(transport=httpx.WSGITransport(HTTPApp(svc, authenticate=lambda environ: principal)),
                          base_url='http://fixture')
    return client, svc, api, store


def job(svc, principal='client-a'):
    return json.loads((svc.root / svc.internal_id(principal, 'task-1') / 'job.json').read_text())


def test_experience_scope_is_frozen_from_principal_and_contract(tmp_path):
    client, svc, api, store = service(tmp_path)
    assert client.post('/v1/tasks', json=TASK).status_code == 200
    assert job(svc)['experience'] == {'client_id': 'client-a', 'scope': 'arithmetic', 'created_at': '2026-11-19'}


def test_execution_builds_card_and_writes_back_under_that_scope(tmp_path):
    client, svc, api, store = service(tmp_path)
    assert client.post('/v1/tasks', json=TASK).json()['status'] == 'passed'
    run_dir = next((tmp_path / 'runner').rglob('experience-card.json'))
    card = json.loads(run_dir.read_text())
    assert card['client_id'] == 'client-a' and card['view']['scope'] == 'arithmetic'
    saved = ExperienceStore.open(tmp_path / 'experience.json', rewriter=lambda fact: 'x')
    assert saved._observations  # 這題的結果已以同一範圍回寫進經驗庫檔案


def test_resend_keeps_the_original_scope(tmp_path):
    client, svc, api, store = service(tmp_path)
    client.post('/v1/tasks', json=TASK)
    first = job(svc)['experience']
    assert client.post('/v1/tasks', json=TASK).status_code == 200
    assert job(svc)['experience'] == first


def test_contract_without_experience_link_freezes_nothing(tmp_path):
    client, svc, api, store = service(tmp_path, experience=False)
    assert client.post('/v1/tasks', json=TASK).json()['status'] == 'passed'
    assert 'experience' not in job(svc)
    assert not list((tmp_path / 'runner').rglob('experience-card.json'))


def test_principal_that_is_not_a_code_is_hashed_not_exposed(tmp_path):
    client, svc, api, store = service(tmp_path, principal='ops team@example')
    assert client.post('/v1/tasks', json=TASK).status_code == 200
    scope = job(svc, 'ops team@example')['experience']
    assert scope['client_id'].startswith('client-sha256:') and '@' not in scope['client_id']

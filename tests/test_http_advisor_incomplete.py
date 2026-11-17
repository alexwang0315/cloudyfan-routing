"""顧問先答路徑（路徑二）在 HTTP 任務服務上的「顧問未完成」終局（advisor_incomplete）。

全部離線：只用 httpx.MockTransport／WSGITransport，不呼叫任何真實模型。
顧問回非 JSON 時沒有任何作答；任務仍要被當成已結束：答案為 None、費用照收據記入、
可讀結果、可觀察完成結果，且留存（retention）不把它當成未完成任務。
"""
import json

import httpx

from cloudyfan_jev.advisor import ADVISOR_FIRST, AdvisorConfig
from cloudyfan_jev.budget import EnforcedBound
from cloudyfan_jev.http_api import HTTPApp, RegisteredContract, TaskService
from cloudyfan_jev.privacy import ExecutionPolicy
from cloudyfan_jev.retention import RetentionPolicy
from test_budget import controlled
from test_engine import ArithmeticEnvironment
from test_http_api import TASK, block_real_network  # noqa: F401  (autouse：禁止真實連線)


def role_profile(name):
    return {'model': 'oss/' + name, 'model_version': 'oss/' + name + '-v1',
            'accepted_models': ['oss/' + name + '-v1'], 'provider': 'example',
            'provider_name': 'Example', 'effort': 'medium', 'open_source': True,
            'execution_location': 'cloud:fixture'}


class InvalidAdvisorAPI:
    """顧問回非 JSON；其他模型不應被呼叫（沒有可派送的建議）。"""
    def __init__(self):
        self.requests = []

    def __call__(self, request):
        p = json.loads(request.content)
        self.requests.append(p)
        content = 'definitely not json' if p['model'] == 'oss/advisor-v1' else '2'
        return httpx.Response(200, json={'id': f'gen-{len(self.requests)}', 'model': p['model'],
            'provider': 'Example', 'usage': {'cost': .002},
            'choices': [{'message': {'role': 'assistant', 'content': content}, 'finish_reason': 'stop'}]})


def advisor_service(tmp_path):
    api = InvalidAdvisorAPI()
    runner = controlled(tmp_path / 'runner', api)
    runner.core.execution_policy = ExecutionPolicy(mode='unrestricted', origin='home:fixture',
                                                   classifier='cloud:typesafe')
    for profile in runner.core.profiles.values():
        profile['execution_location'] = 'cloud:fixture'
    runner.core.advisor_first = AdvisorConfig(advisor=role_profile('advisor'), judge=role_profile('judge'),
                                              score_snapshot='snapshot-20261117-a')
    for model in ('oss/advisor-v1', 'oss/judge-v1'):
        runner.model_bounds[model] = EnforcedBound('.003', 'offline hard cap')
    contract = RegisteredContract('public-v1', runner, lambda workspace: ArithmeticEnvironment(),
                                  mode=ADVISOR_FIRST)
    clock = [1000.0]
    service = TaskService(tmp_path / 'service', contracts={'arithmetic': contract}, allowed_principal='admin',
                          retention_policy=RetentionPolicy('fixture-v1', 60), clock=lambda: clock[0])
    client = httpx.Client(transport=httpx.WSGITransport(HTTPApp(service, authenticate=lambda environ: 'admin')),
                          base_url='http://fixture')
    return client, service, api, clock


def test_advisor_incomplete_is_a_terminal_task_end_to_end(tmp_path):
    client, service, api, clock = advisor_service(tmp_path)
    submitted = client.post('/v1/tasks', json=TASK)
    assert submitted.status_code == 200
    assert submitted.json()['status'] == 'advisor_incomplete'
    assert [p['model'] for p in api.requests] == ['oss/advisor-v1']

    result = client.get('/v1/tasks/task-1/result')
    assert result.status_code == 200
    body = result.json()
    assert body['status'] == 'advisor_incomplete' and body['answer'] is None
    assert body['first_public_pass'] is None and body['final_public_pass'] is None
    assert body['costs']['complete_api_usd'] == '0.002' and body['costs']['unknown_events'] == 0
    outcome = json.loads((service.root / service.internal_id('admin', 'task-1') / 'outcome.json').read_text())
    assert outcome['answer'] is None
    assert outcome['costs']['model_and_classifier']['advisor_usd'] == '0.002'

    observed = service.observe('admin', 'task-1')
    assert observed['completed_result']['status'] == 'advisor_incomplete'

    # 重送／續跑不重付顧問費用。
    assert client.post('/v1/tasks/task-1/resume', json={}).status_code == 200
    assert len(api.requests) == 1

    clock[0] = 1061.0
    service.retention_open_files = lambda path: []
    plan = service.retention_preview('admin', 'task-1')
    assert 'task_not_completed' not in plan['blockers']

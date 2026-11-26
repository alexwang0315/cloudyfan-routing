"""HTTP 任務 API 的選填 ``overrides`` 欄位（ADR 0038 第 7 點）。

全部離線：只用 httpx.MockTransport／WSGITransport，不呼叫任何真實模型。
"""
import json

import httpx
import pytest

from cloudyfan_jev.budget import EnforcedBound
from cloudyfan_jev.http_api import HTTPApp, RegisteredContract, TaskService
from cloudyfan_jev.privacy import ExecutionPolicy
from cloudyfan_jev.triage import ADVISOR_TRIAGE, OVERRIDE_FIELDS
from test_advisor_triage import API, TRIAGER, VerifiableEnvironment, route, triage_config
from test_budget import controlled
from test_http_api import TASK, block_real_network  # noqa: F401  (autouse：禁止真實連線)

ALL = tuple(OVERRIDE_FIELDS)


def located(profile):
    return {**profile, 'execution_location': 'cloud:fixture'}


def service(tmp_path, api, *, allowed=ALL, ceiling='0.5', mode=ADVISOR_TRIAGE, extra_contracts=None):
    runner = controlled(tmp_path / 'runner', api)
    runner.core.execution_policy = ExecutionPolicy(mode='unrestricted', origin='home:fixture',
                                                   classifier='cloud:typesafe')
    for profile in runner.core.profiles.values():
        profile['execution_location'] = 'cloud:fixture'
    base = triage_config()
    runner.core.advisor_triage = triage_config(triager=located(base.triager),
                                               pool={k: located(v) for k, v in base.pool.items()})
    for p in runner.core.advisor_triage.pool.values():
        runner.model_bounds[p['model_version']] = EnforcedBound('.03', 'offline hard cap')
    runner.model_bounds[TRIAGER] = EnforcedBound('.002', 'offline hard cap')
    contracts = {'arithmetic': RegisteredContract('public-v1', runner, lambda workspace: VerifiableEnvironment(),
                                                  mode=mode, allowed_overrides=allowed,
                                                  max_usd_ceiling=ceiling if 'max_usd' in allowed else None)}
    contracts.update(extra_contracts(runner) if extra_contracts else {})
    svc = TaskService(tmp_path / 'service', contracts=contracts, allowed_principal='admin')
    client = httpx.Client(transport=httpx.WSGITransport(HTTPApp(svc, authenticate=lambda environ: 'admin')),
                          base_url='http://fixture')
    return client, svc


def outcome(svc, run_id='task-1'):
    return json.loads((svc.root / svc.internal_id('admin', run_id) / 'outcome.json').read_text())


def test_task_without_overrides_runs_triage_and_reports_one_line_routing(tmp_path):
    api = API(answers={'example/open_source': '2'})
    client, svc = service(tmp_path, api)
    response = client.post('/v1/tasks', json=TASK)
    assert response.status_code == 200 and response.json()['status'] == 'passed'
    assert api.models() == [TRIAGER, 'example/open_source']
    body = client.get('/v1/tasks/task-1/result').json()
    assert body['routing']['route'] == 'path1' and body['routing']['overridden'] == []
    assert body['routing']['model'] == 'example/open_source' and body['routing']['risk'] == 'normal'
    assert [c['configuration']['model'] for c in body['provenance']['triage_calls']] == [TRIAGER]
    job = json.loads((svc.root / svc.internal_id('admin', 'task-1') / 'job.json').read_text())
    assert 'overrides' not in job['request']


def test_overrides_are_frozen_and_both_judgments_are_kept(tmp_path):
    api = API(triage=(route('path1'),), answers={'example/fable': '2'})
    client, svc = service(tmp_path, api)
    overrides = {'route': 'path2', 'risk': 'high', 'max_usd': '0.5'}
    response = client.post('/v1/tasks', json={**TASK, 'overrides': overrides})
    assert response.status_code == 200
    assert api.models() == [TRIAGER, 'example/fable']
    routing = outcome(svc)['routing']
    assert routing['triage_route'] == 'path1' and routing['route'] == 'path2'
    assert routing['route_source'] == 'override' and routing['overridden'] == ['max_usd', 'risk', 'route']
    assert routing['model'] == 'example/fable'
    job = json.loads((svc.root / svc.internal_id('admin', 'task-1') / 'job.json').read_text())
    assert job['request']['overrides'] == overrides
    # 續跑不能改覆寫：換了內容就是不同的題目。
    changed = client.post('/v1/tasks', json={**TASK, 'overrides': {'route': 'path1'}})
    assert changed.status_code == 409 and changed.json()['error']['code'] == 'frozen_task_changed'
    same = client.post('/v1/tasks', json={**TASK, 'overrides': overrides})
    assert same.status_code == 200 and len(api.requests) == 2


@pytest.mark.parametrize('overrides,status,code', [
    ({'model': 'sonnet'}, 422, 'unsupported_override_field'),
    ({'setting': 'opus-xhigh'}, 422, 'setting_not_in_pool'),
    ({'risk': 'HIGH'}, 422, 'invalid_override_value'),
    ({'escalation': False}, 422, 'invalid_override_value'),
    ({'max_usd': '0.75'}, 422, 'max_usd_exceeds_contract'),
    ({'max_usd': '0.01'}, 409, 'max_usd_below_worst_case'),
    ({'max_usd': 1}, 422, 'invalid_override_value'),
    ({'setting': 'haiku-low', 'escalation': 'auto'}, 422, 'override_conflict'),
    ({}, 422, 'overrides_empty'),
    ('high', 422, 'overrides_not_object'),
])
def test_invalid_overrides_are_rejected_at_creation_without_records_or_calls(tmp_path, overrides, status, code):
    api = API()
    client, svc = service(tmp_path, api)
    response = client.post('/v1/tasks', json={**TASK, 'overrides': overrides})
    assert response.status_code == status and response.json()['error']['code'] == code
    assert api.requests == []
    assert not (svc.root / svc.internal_id('admin', 'task-1') / 'job.json').exists()


def test_fields_the_contract_does_not_allow_are_rejected(tmp_path):
    api = API()
    client, _ = service(tmp_path, api, allowed=('risk',))
    assert client.post('/v1/tasks', json={**TASK, 'overrides': {'risk': 'high'}}).status_code == 200
    response = client.post('/v1/tasks', json={**TASK, 'run_id': 'task-2', 'overrides': {'route': 'path2'}})
    assert response.status_code == 403 and response.json()['error']['code'] == 'override_not_allowed'


def test_contract_without_allowed_overrides_rejects_any_override(tmp_path):
    def legacy(runner):
        return {'plain': RegisteredContract('public-v1', runner, lambda workspace: VerifiableEnvironment())}
    api = API(answers={'example/open_source': '2'})
    client, svc = service(tmp_path, api, extra_contracts=legacy)
    response = client.post('/v1/tasks', json={**TASK, 'contract_id': 'plain', 'overrides': {'risk': 'high'}})
    assert response.status_code == 403 and response.json()['error']['code'] == 'override_not_allowed'
    assert api.requests == []
    # 不帶覆寫的既有契約照舊：直接走路徑一，不呼叫分流顧問。
    plain = client.post('/v1/tasks', json={**TASK, 'contract_id': 'plain'})
    assert plain.status_code == 200 and api.models() == ['example/open_source']
    assert 'routing' not in outcome(svc) and 'overrides_policy' not in svc.contracts['plain'].descriptor()


@pytest.mark.parametrize('fields,message', [
    ({'mode': 'cloudyfan', 'allowed_overrides': ('risk',)}, 'Only advisor_triage'),
    ({'mode': ADVISOR_TRIAGE, 'allowed_overrides': ('max_usd',)}, 'max_usd_ceiling'),
    ({'mode': ADVISOR_TRIAGE, 'allowed_overrides': ('risk', 'colour')}, 'distinct override fields'),
    ({'mode': ADVISOR_TRIAGE, 'allowed_overrides': ('max_usd',), 'max_usd_ceiling': '-1'}, 'positive decimal'),
])
def test_contract_override_policy_is_validated(tmp_path, fields, message):
    client, svc = service(tmp_path, API())
    runner = svc.contracts['arithmetic'].runner
    with pytest.raises(ValueError, match=message):
        RegisteredContract('public-v1', runner, lambda workspace: VerifiableEnvironment(), **fields).descriptor()

"""#80：Monitor 顯示路徑二（顧問先答）、自動換池紀錄與回復、客戶端路由模式與經驗共用設定。

全部離線：MRC 的真正 Core／AutoPoolController／ExperienceStore 搭配 httpx.MockTransport，
不呼叫任何真實模型、不讀 .env。
"""
import copy
import json

import httpx

from cloudyfan_jev.advisor import ADVISOR_FIRST, AdvisorConfig
from cloudyfan_jev.client import Client
from cloudyfan_jev.demo import ArithmeticEnvironment, example_config
from cloudyfan_jev.engine import Core

from cloudyfan_monitor.tracing import routing_trace

ADVISOR_MODEL = 'oss/advisor-v1'
PRIVATE_REASON = 'PRIVATE-ADVISOR-REASON'


def role_profile(name):
    return {'model': 'oss/' + name, 'model_version': 'oss/' + name + '-v1',
            'accepted_models': ['oss/' + name + '-v1'], 'provider': 'example',
            'provider_name': 'Example', 'effort': 'medium', 'open_source': True}


def advisor_config():
    return AdvisorConfig(advisor=role_profile('advisor'), judge=role_profile('judge'),
                         score_snapshot='snapshot-20261117-a')


def decision(mode, answer=None, recommendations=None):
    body = {'mode': mode, 'recommendations': recommendations or [
        {'setting': 'mid', 'tier': 'mid', 'reason': PRIVATE_REASON},
        {'setting': 'high', 'tier': 'high', 'reason': PRIVATE_REASON}]}
    if answer is not None:
        body['answer'] = answer
    return json.dumps(body)


class AdvisorUpstream:
    def __init__(self, advisor_content, answers):
        self.advisor_content, self.answers, self.requests = advisor_content, answers, []

    def __call__(self, request):
        payload = json.loads(request.content)
        self.requests.append(copy.deepcopy(payload))
        n = len(self.requests)
        content = (self.advisor_content if payload['model'] == ADVISOR_MODEL
                   else self.answers.get(payload['model'], '3'))
        return httpx.Response(200, json={'id': f'gen-{n}', 'model': payload['model'], 'provider': 'Example',
            'usage': {'cost': .01, 'prompt_tokens': 7, 'completion_tokens': 1},
            'choices': [{'message': {'role': 'assistant', 'content': content}, 'finish_reason': 'stop'}]})


def advisor_result(tmp_path, advisor_content, answers):
    upstream = AdvisorUpstream(advisor_content, answers)
    profiles, decision_profile = example_config()
    core = Core(Client(httpx.Client(transport=httpx.MockTransport(upstream)), 'mock-only', tmp_path),
                profiles, decision_profile, advisor_first=advisor_config())
    return core.run('r', {'prompt': 'Return only the integer sum of 1 and 1.'}, ArithmeticEnvironment,
                    contract_version='v1', mode=ADVISOR_FIRST)


def test_task_trace_shows_advisor_path_recommendations_and_split_costs(tmp_path):
    result = advisor_result(tmp_path, decision('answer', '3', [
        {'setting': 'mid', 'tier': 'mid', 'reason': PRIVATE_REASON},
        {'setting': 'nope', 'tier': 'high', 'reason': PRIVATE_REASON},
        {'setting': 'high', 'tier': 'low', 'reason': PRIVATE_REASON}]), {'example/mid': '2'})
    trace = routing_trace(result)
    advisor = trace['advisor']
    assert advisor['path'] == ADVISOR_FIRST
    assert advisor['advisor_mode'] == 'answer'
    assert advisor['end_reason'] == 'passed' and advisor['incomplete_reason'] is None
    assert advisor['score_snapshot'] == 'snapshot-20261117-a'
    assert advisor['recommendations'] == [{'rank': 0, 'setting': 'mid', 'tier': 'mid'}]
    assert advisor['voided'] == [{'rank': 1, 'reason': 'not_in_pool'}, {'rank': 2, 'reason': 'tier_mismatch'}]
    assert [c['role'] for c in advisor['calls']] == ['advisor']
    assert advisor['calls'][0]['model'] == 'oss/advisor-v1' and advisor['calls'][0]['cost_usd'] == '0.01'
    # 每次作答：顧問自答算第 0 次，改派的那次標明設定與建議順位。
    assert [(a['source'], a['setting'], a['passed']) for a in trace['attempts']] == [
        ('advisor_answer', None, False), ('delegated', 'mid', True)]
    assert trace['attempts'][1]['recommendation_rank'] == 0
    assert [c['role'] for c in trace['attempts'][1]['calls']] == ['answer']
    # 研究全額＝收據全額；正式成本不計明示開源的顧問／裁判。
    assert advisor['costs'] == {'research_usd': '0.02', 'formal_usd': '0.01', 'advisor_usd': '0.01',
                                'judge_usd': '0', 'unknown_calls': 0}
    text = json.dumps(trace)
    assert PRIVATE_REASON not in text and '"answer": "3"' not in text
    assert trace['decisions'] == []


def test_incomplete_advisor_is_shown_without_attempts(tmp_path):
    trace = routing_trace(advisor_result(tmp_path, 'not json', {}))
    assert trace['advisor']['advisor_mode'] == 'incomplete'
    assert trace['advisor']['incomplete_reason'] == 'invalid_json'
    assert trace['advisor']['end_reason'] == 'advisor_incomplete'
    assert trace['attempts'] == []


def test_path_one_trace_is_unchanged(tmp_path):
    profiles, decision_profile = example_config()
    upstream = AdvisorUpstream('', {'example/open_source': '2'})
    core = Core(Client(httpx.Client(transport=httpx.MockTransport(upstream)), 'mock-only', tmp_path),
                profiles, decision_profile)
    result = core.run('p1', {'prompt': 'Return only the integer sum of 1 and 1.'}, ArithmeticEnvironment,
                      contract_version='v1')
    trace = routing_trace(result)
    assert 'advisor' not in trace
    assert set(trace['attempts'][0]) == {'index', 'tier', 'passed', 'stop', 'models', 'calls'}


# ---- 客戶端路由模式（路徑一／加強版／路徑二）與任務頁 ----

import pytest

from cloudyfan_monitor.demo import ADMIN_TOKEN, CATEGORY, TOKENS, build_demo


@pytest.fixture
def advisor_system(tmp_path):
    app, upstream = build_demo(tmp_path, seed=False, origin='http://monitor', advisor_first=True)
    def http(token):
        return httpx.Client(transport=httpx.WSGITransport(app), base_url='http://monitor',
                            headers={'Authorization': 'Bearer ' + token})
    return app, upstream, http(ADMIN_TOKEN), http(TOKENS['studio'])


def test_client_routing_mode_path_two_freezes_and_task_view_shows_advisor(advisor_system):
    app, upstream, admin, client = advisor_system
    view = admin.get('/monitor/snapshot').json()
    assert view['modes']['advisor_first'] and view['modes']['cloudyfan_plus']
    assert 'advisor_first' in view['available_modes']
    # 加強版（#79）尚未有 MRC 契約：可辨識名稱，但不能選用。
    assert 'cloudyfan_plus' not in view['available_modes']
    revision = app.store.control()['revision']
    assert admin.post('/monitor/mode', json={'revision': revision, 'client': 'studio',
                                             'mode': 'cloudyfan_plus'}).status_code == 409
    assert admin.post('/monitor/mode', json={'revision': revision, 'client': 'studio',
                                             'mode': 'advisor_first'}).status_code == 200
    response = client.post('/v1/tasks', json={'run_id': 'p2', 'category': CATEGORY,
                                              'prompt': 'Return only the integer sum of 1 and 1.'})
    assert response.status_code == 200, response.text
    assert app.store.binding('studio', 'p2')['mode'] == 'advisor_first'
    task = next(t for t in admin.get('/monitor/snapshot').json()['tasks'] if t['run'] == 'p2')
    assert task['mode'] == 'advisor_first' and task['status'] == 'passed'
    assert task['trace']['advisor']['advisor_mode'] == 'answer'
    assert task['trace']['advisor']['costs']['formal_usd'] == '0'
    # 客戶端不能改自己的路由模式（操作員專屬）。
    assert client.post('/monitor/mode', json={'revision': app.store.control()['revision'],
                                              'client': 'studio', 'mode': 'cloudyfan'}).status_code == 401


def test_path_two_delegated_attempts_show_per_attempt_costs(advisor_system):
    app, upstream, admin, client = advisor_system
    upstream.advisor_reply = json.dumps({'mode': 'delegate', 'recommendations': [
        {'setting': 'mid', 'tier': 'mid', 'reason': PRIVATE_REASON},
        {'setting': 'high', 'tier': 'high', 'reason': PRIVATE_REASON}]})
    upstream.fail_tiers = {'mid'}
    admin.post('/monitor/mode', json={'revision': app.store.control()['revision'], 'client': 'studio',
                                      'mode': 'advisor_first'})
    response = client.post('/v1/tasks', json={'run_id': 'p2d', 'category': CATEGORY,
                                              'prompt': 'Return only the integer sum of 1 and 1.'})
    assert response.status_code == 200, response.text
    task = next(t for t in admin.get('/monitor/snapshot').json()['tasks'] if t['run'] == 'p2d')
    trace = task['trace']
    assert trace['advisor']['advisor_mode'] == 'delegate'
    assert [(a['setting'], a['passed']) for a in trace['attempts']] == [('mid', False), ('high', True)]
    assert [a['calls'][0]['cost_usd'] for a in trace['attempts']] == ['0.001', '0.004']
    assert trace['advisor']['costs']['research_usd'] == '0.0051'
    assert trace['advisor']['costs']['formal_usd'] == '0.005'
    assert PRIVATE_REASON not in json.dumps(task)


# ---- 換池紀錄頁：前後快照、分數、理由；回復；自動換池總開關（操作員專屬）----

from cloudyfan_jev.scores import compile_snapshot
from cloudyfan_jev.scores.auto_pool import AutoPoolController
from cloudyfan_jev.scores.catalog import digest
from cloudyfan_jev.scores.checkup import VERSION as CHECKUP_VERSION, wilson_interval

from cloudyfan_monitor.auto_pool_panel import AutoPoolPanel

NOW, T1, T2 = '2026-10-18T10:00:00+00:00', '2026-10-18T11:00:00+00:00', '2026-10-18T12:00:00+00:00'
POOL_POLICY = {'schema_version': 'auto-pool-policy-v1', 'hysteresis': 0.05, 'min_graded': 20,
               'max_checkup_age_days': 14}
LEADERBOARD = {'low-a': 60, 'low-b': 65, 'mid-a': 90, 'mid-b': 92, 'high-a': 110, 'high-b': 120}


def leaderboard_snapshot():
    """與 MRC tests/test_auto_pool.py 相同結構的合成排行榜快照。"""
    profiles, _ = example_config()
    registry, observations = {'candidates': [], 'open_source': None}, []
    for config_id, score in [('open_source', None), *LEADERBOARD.items()]:
        tier = config_id.split('-')[0] if config_id != 'open_source' else 'open_source'
        profile = copy.deepcopy(profiles[tier])
        if config_id != 'open_source':
            profile['model'] = 'model/' + config_id
        entry = {'config_id': config_id, 'profile': profile, 'approved': True, 'context_tokens': 8192,
                 'capabilities': ['tools'], 'catalog_checked_at': NOW, 'estimated_request_usd': .001,
                 'estimated_latency_ms': 200, 'cost_basis': 'fixture_only',
                 'source_model_id': 'example/' + config_id, 'source': 'codexradar', 'source_version': 'fixture-v1'}
        if config_id == 'open_source':
            registry['open_source'] = {**entry, 'open_weight': True}
            continue
        registry['candidates'].append(entry)
        observations.append({'source': 'codexradar', 'source_version': 'fixture-v1',
            'source_model_id': 'example/' + config_id, 'metric': 'overall', 'score': score, 'effort': 'low',
            'sample_count': 64, 'evaluated_at': '2026-10-18', 'retrieved_at': NOW,
            'source_url': 'https://deng.codexradar.com/', 'raw_sha256': 'a' * 64})
    policy = {'id': 'coding-fixture-v1', 'domain': 'coding', 'source': 'codexradar',
              'source_version': 'fixture-v1', 'metric': 'overall', 'scale': 'source_native', 'low_mid': 80,
              'mid_high': 100, 'max_source_age_hours': 48, 'max_catalog_age_hours': 48,
              'snapshot_ttl_hours': 24, 'min_samples': 64, 'max_evaluation_age_days': 30}
    return compile_snapshot(observations, registry, policy, now=NOW)


def checkup(config_id, passed, graded=100):
    body = {'schema_version': CHECKUP_VERSION, 'created_at': T1, 'configuration_id': config_id,
            'graded': graded, 'passed': passed, 'score': round(passed / graded, 4),
            'confidence_interval': wilson_interval(passed, graded),
            'complete_task_cost_usd': str(0.1 * graded), 'cost_per_task_usd': '0.10'}
    return {**body, 'record_id': digest(body)}


def promote_mid_b(controller, now=T1):
    scores = {'low-a': 40, 'low-b': 45, 'mid-a': 55, 'mid-b': 99, 'high-a': 75, 'high-b': 80}
    return controller.evaluate([checkup(c, s) for c, s in scores.items()], POOL_POLICY, now=now,
                               model_bounds={'model/' + name: object() for name in LEADERBOARD})


@pytest.fixture
def pool_system(advisor_system, tmp_path):
    app, upstream, admin, client = advisor_system
    controller = AutoPoolController(tmp_path / 'auto-pool')
    controller.store.publish(leaderboard_snapshot(), now=NOW)
    app.auto_pool = AutoPoolPanel(controller, now=lambda: T2)
    return app, controller, admin, client


def test_pool_change_log_shows_before_after_scores_and_reasons(pool_system):
    app, controller, admin, client = pool_system
    before = controller.store.load()['snapshot_id']
    change = promote_mid_b(controller)
    view = admin.get('/monitor/auto-pool').json()
    assert view['configured'] is True and view['auto_switch_enabled'] is True
    assert view['current_snapshot_id'] == change['after_snapshot_id']
    assert view['rollback_available'] is True
    [row] = view['changes']
    assert (row['kind'], row['before_snapshot_id'], row['after_snapshot_id']) == (
        'auto_change', before, change['after_snapshot_id'])
    assert row['moves'] == [{'config_id': 'mid-b', 'from': 'mid', 'to': 'high', 'reason': 'significant_checkup_gain'}]
    assert row['evidence']['mid-b']['score'] == 0.99
    assert view['current_grouping']['high'] == ['high-a', 'high-b', 'mid-b']


def test_operator_rolls_back_and_toggles_auto_switch_clients_cannot(pool_system):
    app, controller, admin, client = pool_system
    change = promote_mid_b(controller)
    for path, body in (('/monitor/auto-pool', None), ('/monitor/auto-pool/rollback', {'snapshot_id': 'x'}),
                       ('/monitor/auto-pool/auto-switch', {'enabled': False})):
        response = client.get(path) if body is None else client.post(path, json=body)
        assert response.status_code == 401
    assert controller.store.load()['snapshot_id'] == change['after_snapshot_id']
    # 畫面過期（快照已變）時拒絕回復，避免回復到操作員沒看到的版本。
    stale = admin.post('/monitor/auto-pool/rollback', json={'snapshot_id': 'stale'})
    assert stale.status_code == 409 and stale.json()['error']['code'] == 'auto_pool_changed_reload'
    rolled = admin.post('/monitor/auto-pool/rollback', json={'snapshot_id': change['after_snapshot_id']}).json()
    assert rolled['status'] == 'rolled_back'
    assert rolled['restored_snapshot_id'] == change['before_snapshot_id']
    view = admin.get('/monitor/auto-pool').json()
    assert view['current_grouping']['mid'] == ['mid-a', 'mid-b']
    assert view['changes'][0]['kind'] == 'rollback' and view['changes'][0]['operator'] == 'monitor-operator'
    off = admin.post('/monitor/auto-pool/auto-switch', json={'enabled': False}).json()
    assert off['auto_switch_enabled'] is False and controller.auto_switch_enabled() is False
    assert promote_mid_b(controller, T2)['status'] == 'auto_switch_disabled'
    assert admin.post('/monitor/auto-pool/auto-switch', json={'enabled': 'no'}).status_code == 422
    actions = [a['action'] for a in admin.get('/monitor/snapshot').json()['audit']]
    assert 'auto_pool_rollback' in actions and 'auto_pool_auto_switch' in actions


def test_auto_pool_not_configured_is_explicit(advisor_system):
    app, upstream, admin, client = advisor_system
    assert admin.get('/monitor/auto-pool').json() == {'schema_version': 'monitor-auto-pool-v1', 'configured': False}
    assert admin.post('/monitor/auto-pool/auto-switch', json={'enabled': True}).status_code == 409


# ---- 客戶端經驗共用設定：「貢獻」「使用」開關（操作員專屬）----

from cloudyfan_jev.experience import ExperienceStore

from cloudyfan_monitor.experience_settings import ExperienceSettings


def no_rewrite(*args, **kwargs):
    raise AssertionError('Monitor 不呼叫改寫')


def test_operator_toggles_experience_contribute_and_use_per_client(advisor_system, tmp_path):
    app, upstream, admin, client = advisor_system
    path = tmp_path / 'experience.json'
    app.experience = ExperienceSettings(path)
    view = admin.get('/monitor/experience').json()
    assert view['configured'] is True
    assert {row['client']: (row['contribute'], row['use']) for row in view['clients']} == {
        'studio': (True, True), 'agent': (True, True), 'batch': (True, True)}
    response = admin.post('/monitor/experience/settings', json={'client': 'studio', 'contribute': False})
    assert response.status_code == 200, response.text
    assert admin.post('/monitor/experience/settings', json={'client': 'agent', 'use': False}).status_code == 200
    # MRC 自己重新開檔，看到的是同一份設定（建題時 freeze_settings 會複製它）。
    stored = ExperienceStore.open(path, rewriter=no_rewrite)
    assert stored.freeze_settings('studio') == {'contribute': False, 'use': True}
    assert stored.freeze_settings('agent') == {'contribute': True, 'use': False}
    rows = {row['client']: row for row in admin.get('/monitor/experience').json()['clients']}
    assert (rows['studio']['contribute'], rows['agent']['use']) == (False, False)
    assert admin.post('/monitor/experience/settings', json={'client': 'ghost', 'use': False}).status_code == 404
    assert admin.post('/monitor/experience/settings', json={'client': 'studio', 'use': 'off'}).status_code == 422
    assert admin.post('/monitor/experience/settings', json={'client': 'studio'}).status_code == 422
    assert client.post('/monitor/experience/settings', json={'client': 'studio', 'use': True}).status_code == 401
    assert client.get('/monitor/experience').status_code == 401
    audit = [a for a in admin.get('/monitor/snapshot').json()['audit'] if a['action'] == 'experience_settings_changed']
    assert audit[-1]['data'] == {'actor': 'operator', 'client': 'studio', 'contribute': False, 'use': None}


def test_experience_not_configured_is_explicit(advisor_system):
    app, upstream, admin, client = advisor_system
    assert admin.get('/monitor/experience').json() == {'schema_version': 'monitor-experience-settings-v1',
                                                       'configured': False}
    assert admin.post('/monitor/experience/settings', json={'client': 'studio', 'use': False}).status_code == 409

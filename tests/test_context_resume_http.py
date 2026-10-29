"""#52：升級、接續與前綴匹配都沿用凍結準備；損壞明示不可驗證。真實主服務 HTTP，上游僅 MockTransport。"""
import json

from cloudyfan_monitor.saved_comparisons import _task_id
from test_cache_observations_http import SUPPORT
from test_context_fallback_http import SMALL
from test_context_preparation_http import CONTEXT, _enable, _post
from test_local_http_integration import loopback_network as loopback_network
from test_saved_paired_outcomes_http import paired_http as paired_http


def _mode(tracer, mode, fail=()):
    operator = tracer.client()
    revision = operator.get('/monitor/snapshot').json()['control']['revision']
    assert operator.post('/monitor/mode', json={'revision': revision, 'mode': mode}).status_code == 200
    tracer.bundle.upstream.fail_tiers = set(fail)


def test_every_dispatch_after_escalation_carries_the_same_frozen_content(paired_http):
    _enable(paired_http)
    service = paired_http.bundle.app.gateway.service
    from dataclasses import replace
    service.contracts['demo-cloudyfan'] = replace(service.contracts['demo-cloudyfan'],
                                                  context_policy=service.contracts['demo-open_source_first'].context_policy)
    _mode(paired_http, 'cloudyfan', fail={'open_source'})
    response = _post(paired_http, 'ctx-escalate')
    assert response.status_code == 200 and response.json()['status'] == 'passed', response.text
    prepared = paired_http.client().get('/monitor/context-preparations/studio/ctx-escalate').json()['preparation']
    assert prepared['generation_requests'] >= 2
    assert all(row['matches_prepared'] for row in prepared['dispatches']) and prepared['sent_matches_prepared'] is True


def test_prepared_and_plain_tasks_never_borrow_each_others_prefix_hint(paired_http):
    _enable(paired_http)
    paired_http.bundle.app.cache_support = SUPPORT
    assert _post(paired_http, 'ctx-prefix-a', context=SMALL).status_code == 200
    assert _post(paired_http, 'ctx-prefix-b', context=SMALL, prompt='2+2 PRIVATE_PAIRED_CANARY').status_code == 200
    assert _post(paired_http, 'plain-prefix-c', context=None, prompt='3+3 PRIVATE_PAIRED_CANARY').status_code == 200
    same = paired_http.client().get('/monitor/cache-observations/studio/ctx-prefix-b').json()['calls'][0]
    plain = paired_http.client().get('/monitor/cache-observations/studio/plain-prefix-c').json()['calls'][0]
    assert same['hint']['status'] == 'matchable_hint', 'Same preparation version and selection may match'
    assert plain['hint']['status'] == 'no_prior_match', 'A plain task must not borrow a prepared task prefix'
    assert same['prefix']['identity'] != plain['prefix']['identity']


def test_corrupted_frozen_preparation_is_unverifiable_and_resume_sends_nothing(paired_http):
    _enable(paired_http)
    assert _post(paired_http, 'ctx-tamper').status_code == 200
    calls = len(paired_http.bundle.upstream.calls)
    path = paired_http.bundle.app.gateway.service.root / _task_id('studio', 'ctx-tamper') / 'job.json'
    job = json.loads(path.read_text())
    job['prepared']['content'] += ' tampered'
    path.chmod(0o600)
    path.write_text(json.dumps(job))
    value = paired_http.client().get('/monitor/context-preparations/studio/ctx-tamper').json()
    assert value['status'] == 'unverifiable' and value['preparation'] is None
    # 已完成的任務接續只讀原狀態；中斷任務的接續拒絕由 Core 測試涵蓋（frozen_preparation_unverifiable）。
    resumed = paired_http.client('studio').post('/v1/tasks/ctx-tamper/resume', json={})
    assert resumed.status_code >= 400 or resumed.json()['status'] == 'passed'
    assert len(paired_http.bundle.upstream.calls) == calls


def test_identical_resend_after_disabling_the_policy_keeps_the_frozen_flow(paired_http):
    _enable(paired_http)
    assert _post(paired_http, 'ctx-disabled-later', context=CONTEXT).status_code == 200
    calls = len(paired_http.bundle.upstream.calls)
    service = paired_http.bundle.app.gateway.service
    from dataclasses import replace
    service.contracts['demo-open_source_first'] = replace(service.contracts['demo-open_source_first'], context_policy=None)
    resend = _post(paired_http, 'ctx-disabled-later', context=CONTEXT)
    # 原任務已凍結；停用只影響新任務，原樣重送不重新準備或派送。
    assert resend.status_code in (200, 409)
    assert len(paired_http.bundle.upstream.calls) == calls
    assert paired_http.client().get('/monitor/context-preparations/studio/ctx-disabled-later').json()['status'] == 'prepared'

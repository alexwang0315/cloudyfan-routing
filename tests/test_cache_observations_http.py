"""#46：由實際已送出請求查看前綴與快取觀測；真實主服務 HTTP，上游僅 MockTransport。"""
import json

import httpx
import pytest

from cloudyfan_monitor import cache_observations, demo
from test_local_http_integration import loopback_network as loopback_network
from test_saved_paired_outcomes_http import paired_http as paired_http


SUPPORT = {'example': {'source_ttl_seconds': None, 'data_policy': 'compatible'}}


def _state(tracer):
    """原狀態：模型呼叫數與原檔案內容；SQLite 依資料表內容，排除新的私密金鑰與比較報告。"""
    import hashlib
    import sqlite3
    files = {}
    for path in sorted(tracer.bundle.root.rglob('*')):
        if (not path.is_file() or '.saved-comparisons' in path.parts or path.name == '.cache-observation.key'
                or path.name.endswith(('-wal', '-shm', '-journal'))):
            continue
        if path.suffix in ('.sqlite', '.sqlite3'):
            with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=5) as db:
                tables = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
                files[str(path)] = {name: sorted(map(repr, db.execute('SELECT * FROM "' + name + '"').fetchall()))
                                    for name in tables}
        else:
            files[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
    return {'model_calls': len(tracer.bundle.upstream.calls), 'files': files}


def _observe(tracer, client, run, principal='operator'):
    before = _state(tracer)
    path = (f'/monitor/cache-observations/{client}/{run}' if principal == 'operator'
            else f'/v1/tasks/{run}/cache-observations')
    response = tracer.client(principal).get(path)
    assert _state(tracer) == before, 'Cache observation called an upstream or changed original state'
    if response.status_code == 200:
        text = response.text
        assert 'PRIVATE_PAIRED_CANARY' not in text and str(tracer.bundle.root) not in text
        key = next(tracer.bundle.root.rglob('.cache-observation.key'), None)
        assert key is None or key.read_bytes().hex() not in text
        assert 'messages' not in json.loads(text)['calls'][0] if json.loads(text)['calls'] else True
    return response


def test_identical_static_prefix_in_the_same_scope_is_a_matchable_hint_without_observed_usage(paired_http):
    paired_http.bundle.app.cache_support = SUPPORT
    paired_http.run('studio', 'prefix-first', 'open_source_first')
    paired_http.run('studio', 'prefix-second', 'open_source_first', prompt='2+2 PRIVATE_PAIRED_CANARY')
    value = _observe(paired_http, 'studio', 'prefix-second').json()
    call = value['calls'][0]
    assert value['schema_version'] == 'prefix-cache-observation-v1' and value['adopted'] is False
    assert call['sent'] is True and call['prefix']['messages'] == 1 and len(call['prefix']['identity']) == 32
    assert call['hint']['status'] == 'matchable_hint' and call['hint']['matched_messages'] == 1
    assert call['hint']['source_ttl_seconds'] is None and call['hint']['local_window_seconds'] == 300
    # 模擬上游沒有回報快取欄位：未知，不填零。
    assert call['cache_usage'] == {'status': 'unknown', 'reason': 'cache_usage_not_reported',
                                   'cache_read_tokens': None, 'cache_write_tokens': None}
    first = _observe(paired_http, 'studio', 'prefix-first').json()['calls'][0]
    assert first['hint']['status'] == 'no_prior_match'
    assert first['prefix']['identity'] == call['prefix']['identity']


def test_other_clients_models_and_unregistered_providers_never_borrow_an_observation(paired_http):
    paired_http.run('studio', 'scope-studio', 'open_source_first')
    unsupported = _observe(paired_http, 'studio', 'scope-studio').json()['calls'][0]
    assert unsupported['hint']['status'] == 'unsupported' and unsupported['cache_usage']['status'] == 'unsupported'
    paired_http.bundle.app.cache_support = SUPPORT
    paired_http.run('agent', 'scope-agent', 'open_source_first')
    paired_http.run('studio', 'scope-high', 'fixed_high')
    agent = _observe(paired_http, 'agent', 'scope-agent').json()['calls'][0]
    high = _observe(paired_http, 'studio', 'scope-high').json()['calls'][0]
    studio = _observe(paired_http, 'studio', 'scope-studio').json()['calls'][0]
    assert agent['hint']['status'] == 'no_prior_match', 'Another client\'s request must not be a cache source'
    assert high['hint']['status'] == 'no_prior_match', 'A different model must not be a cache source'
    # 同樣內容在不同客戶端得到不同、不可對照的身分摘要。
    assert agent['prefix']['identity'] != studio['prefix']['identity']


def test_reported_cache_usage_is_observed_only_from_the_actual_response(paired_http, monkeypatch):
    original = demo.Upstream.__call__

    def with_cache(self, request):
        response = original(self, request)
        body = json.loads(response.content)
        if 'choices' in body:
            body['usage']['prompt_tokens_details'] = {'cached_tokens': 12}
        return httpx.Response(response.status_code, json=body)

    monkeypatch.setattr(demo.Upstream, '__call__', with_cache)
    paired_http.bundle.app.cache_support = SUPPORT
    paired_http.run('studio', 'cache-observed', 'open_source_first')
    call = _observe(paired_http, 'studio', 'cache-observed').json()['calls'][0]
    assert call['cache_usage'] == {'status': 'observed', 'reason': 'response_usage_reported',
                                   'cache_read_tokens': 12, 'cache_write_tokens': None}
    assert call['hint']['status'] == 'no_prior_match', 'An observed read does not create a prior-match hint'


def test_outside_the_local_window_the_hint_is_expired_not_a_guarantee(paired_http, monkeypatch):
    paired_http.bundle.app.cache_support = SUPPORT
    paired_http.run('studio', 'window-first', 'open_source_first')
    paired_http.run('studio', 'window-second', 'open_source_first', prompt='3+3 PRIVATE_PAIRED_CANARY')
    monkeypatch.setattr(cache_observations, 'HINT_WINDOW_SECONDS', -1)
    call = _observe(paired_http, 'studio', 'window-second').json()['calls'][0]
    assert call['hint']['status'] == 'expired' and call['hint']['matched_messages'] == 1


def test_customer_reads_only_its_own_task_and_expired_raw_access_is_not_reopened(paired_http):
    paired_http.bundle.app.cache_support = SUPPORT
    paired_http.run('studio', 'own-task', 'open_source_first')
    assert _observe(paired_http, 'studio', 'own-task', principal='studio').status_code == 200
    # 客戶端第一次認證會更新既有的 last_seen；先用既有讀取讓它發生，再檢查觀測本身。
    assert paired_http.client('agent').get('/v1/monitor').status_code == 200
    assert _observe(paired_http, 'studio', 'own-task', principal='agent').status_code == 404
    assert paired_http.client('studio').get('/monitor/cache-observations/studio/own-task').status_code == 401
    assert paired_http.client().get('/monitor/cache-observations/studio/own-task?x=1').status_code == 404
    paired_http.bundle.app.store.clock.now += 31 * 86400
    # 時間前進後，既有的到期處理會在下一個操作員請求寫入；先讓它跑完，再檢查觀測本身沒有副作用。
    assert paired_http.client().get('/monitor/session').status_code == 200
    expired = _observe(paired_http, 'studio', 'own-task').json()
    assert expired['status'] == 'raw_access_expired' and expired['calls'] == []


def test_intent_only_or_interrupted_requests_cannot_form_a_warm_cache():
    for response in ({'http_status': None, 'not_dispatched': True}, {'http_status': None, 'interrupted': True},
                     {'http_status': 200, 'interrupted': True}):
        assert cache_observations._usage(response)['status'] == 'not_applicable'
    assert cache_observations._usage({'http_status': 200, 'raw': '{"usage": {}}'})['status'] == 'unknown'


@pytest.mark.parametrize('prompt', ['1+1 PRIVATE_PAIRED_CANARY'])
def test_dynamic_prompt_message_is_never_part_of_the_prefix(paired_http, prompt):
    paired_http.bundle.app.cache_support = SUPPORT
    paired_http.run('studio', 'dynamic-a', 'open_source_first', prompt=prompt)
    paired_http.run('studio', 'dynamic-b', 'open_source_first', prompt=prompt)
    call = _observe(paired_http, 'studio', 'dynamic-b').json()['calls'][0]
    # 兩個任務 prompt 相同，但含 prompt 的訊息是動態內容，不納入可借用的前綴。
    assert call['prefix']['messages'] == 1 and call['hint']['matched_messages'] == 1

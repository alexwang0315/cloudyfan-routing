"""#47：明示採用的快取親和性；真實主服務 HTTP、BackupCategoryCore、BudgetedCore，上游僅 MockTransport。"""
from saved_backup_fixture import saved_backup_http as saved_backup_http
from test_local_http_integration import loopback_network as loopback_network


def _model(tracer, config_id):
    plan = tracer.host.values['plan']
    return next(row['entry']['profile']['model_version'] for rows in plan['pools'].values()
                for row in rows if row['entry']['config_id'] == config_id)


def _support(tracer):
    plan = tracer.host.values['plan']
    tracer.host.app.cache_support = {row['entry']['profile']['provider']: {'source_ttl_seconds': None,
                                                                          'data_policy': 'compatible'}
                                     for rows in plan['pools'].values() for row in rows}


def _activate(tracer, version='prefix-affinity-v1'):
    operator = tracer.client()
    revision = operator.get('/monitor/cache-affinity').json()['revision']
    response = operator.post('/monitor/cache-affinity/activate', json={'revision': revision, 'version': version})
    assert response.status_code == 200, response.text
    return response.json()


def _create(tracer, run, *, closed=None):
    """closed：在合成健康庫對該候選加上不健康原因，經 Core 原本的健康閘門排除。"""
    health = tracer.host.health
    plan = tracer.host.values['plan']
    target = next((row['configuration_sha256'] for rows in plan['pools'].values() for row in rows
                   if row['entry']['config_id'] == closed), None)
    original = health.assess

    def assess(configuration, provider):
        result = original(configuration, provider)
        if configuration == target:
            result = {**result, 'reasons': [*result['reasons'], 'synthetic_unhealthy_for_test']}
        return result

    health.assess = assess
    try:
        before = len(tracer.host.calls)
        response = tracer.client('studio').post('/v1/tasks', json={
            'run_id': run, 'category': plan['category_id'],
            'prompt': 'Return only 1+1. PRIVATE_SAVED_BACKUP_TASK_CANARY'})
        assert response.status_code == 200, response.text
        return tracer.host.calls[before]
    finally:
        del health.assess


def test_default_is_inactive_and_keeps_the_original_order(saved_backup_http):
    tracer = saved_backup_http
    _support(tracer)
    snapshot = tracer.client().get('/monitor/cache-affinity').json()
    assert snapshot['active_version'] is None and snapshot['revision'] == 0
    first = _create(tracer, 'affinity-default-a', closed='open_source-0')
    second = _create(tracer, 'affinity-default-b')
    assert first['model'] == _model(tracer, 'open_source-1')
    assert second['model'] == _model(tracer, 'open_source-0'), 'Inactive policy must not reorder'
    detail = tracer.client().get('/monitor/cache-observations/studio/affinity-default-b').json()['affinity']
    assert detail['frozen_version'] is None and detail['decisions'] == []


def test_adopted_policy_breaks_the_tie_with_a_trusted_prefix_and_records_it(saved_backup_http):
    tracer = saved_backup_http
    _support(tracer)
    _activate(tracer)
    _create(tracer, 'affinity-a', closed='open_source-0')  # A 只能用 open_source-1
    second = _create(tracer, 'affinity-b')
    assert second['model'] == _model(tracer, 'open_source-1'), 'Equivalent candidate with a trusted prefix not preferred'
    response = tracer.client().get('/monitor/cache-observations/studio/affinity-b')
    assert 'PRIVATE_SAVED_BACKUP_TASK_CANARY' not in response.text
    detail = response.json()['affinity']
    assert detail['frozen_version'] == 'prefix-affinity-v1'
    decision = next(row for row in detail['decisions'] if row['decision_key'] == '0:open_source')
    assert decision['hint']['preferred'] == 'open_source-1' and decision['hint']['matched_messages'] == 1
    assert decision['applied'] == {'tier': 'open_source', 'equivalent_config_ids': ['open_source-0', 'open_source-1'],
                                   'original_config_id': 'open_source-0', 'selected_config_id': 'open_source-1',
                                   'adopted': True, 'reason': 'identical_leading_messages_in_same_scope'}
    assert detail['sent_models'][0]['model'] == _model(tracer, 'open_source-1')
    # 客戶端看自己的觀測時沒有操作員的親和性內部資料。
    own = tracer.client('studio').get('/v1/tasks/affinity-b/cache-observations').json()
    assert 'affinity' not in own


def test_a_health_excluded_candidate_is_never_preferred_even_with_a_hint(saved_backup_http):
    tracer = saved_backup_http
    _support(tracer)
    _activate(tracer)
    _create(tracer, 'gate-a', closed='open_source-0')
    second = _create(tracer, 'gate-b', closed='open_source-1')
    assert second['model'] == _model(tracer, 'open_source-0')
    detail = tracer.client().get('/monitor/cache-observations/studio/gate-b').json()['affinity']
    applied = next(row for row in detail['decisions'] if row['decision_key'] == '0:open_source')['applied']
    assert applied['adopted'] is False and applied['equivalent_config_ids'] == ['open_source-0']


def test_activation_only_affects_new_tasks_and_a_resend_keeps_its_frozen_version(saved_backup_http):
    tracer = saved_backup_http
    _support(tracer)
    _create(tracer, 'frozen-before')
    _activate(tracer)
    resend = tracer.client('studio').post('/v1/tasks', json={
        'run_id': 'frozen-before', 'category': tracer.host.values['plan']['category_id'],
        'prompt': 'Return only 1+1. PRIVATE_SAVED_BACKUP_TASK_CANARY'})
    assert resend.status_code in (200, 409)
    detail = tracer.client().get('/monitor/cache-observations/studio/frozen-before').json()['affinity']
    assert detail['frozen_version'] is None and detail['decisions'] == []


def test_activation_requires_operator_current_revision_and_a_registered_version(saved_backup_http):
    tracer = saved_backup_http
    operator = tracer.client()
    assert operator.post('/monitor/cache-affinity/activate', json={'revision': 0, 'version': 'other-v9'}).status_code == 409
    assert operator.post('/monitor/cache-affinity/activate', json={'revision': 5, 'version': 'prefix-affinity-v1'}).status_code == 409
    assert tracer.client('studio').post('/monitor/cache-affinity/activate', json={
        'revision': 0, 'version': 'prefix-affinity-v1'}).status_code == 401
    assert _activate(tracer)['active_version'] == 'prefix-affinity-v1'
    assert _activate(tracer, None)['active_version'] is None

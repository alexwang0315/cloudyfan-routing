"""#49：快取觀測過期、錯配、損壞、重啟或停用時，新任務安全回到原合格路由，舊任務保留當時理由。"""
import time

import pytest

from cloudyfan_monitor import cache_observations
from cloudyfan_monitor.cache_affinity import CacheAffinity
from cloudyfan_monitor.saved_comparisons import _task_id
from saved_backup_fixture import saved_backup_http as saved_backup_http
from test_cache_affinity_http import _activate, _create, _model, _support
from test_local_http_integration import loopback_network as loopback_network


def _applied(tracer, run):
    detail = tracer.client().get(f'/monitor/cache-observations/studio/{run}').json()['affinity']
    return detail, next(row for row in detail['decisions'] if row['decision_key'] == '0:open_source')


@pytest.mark.parametrize('clock,ttl,reason', [
    (lambda: time.time() + 400, None, 'outside_local_hint_window'),
    (lambda: time.time() + 5, 1, 'outside_source_ttl'),
    (lambda: time.time() - 1000, None, 'clock_anomaly'),
    (lambda: float('nan'), None, 'clock_unavailable'),
], ids=['local-window', 'source-ttl', 'clock-backwards', 'clock-unavailable'])
def test_expired_or_anomalous_observation_skips_affinity_with_its_own_reason(saved_backup_http, clock, ttl, reason):
    tracer = saved_backup_http
    _support(tracer)
    for value in tracer.host.app.cache_support.values():
        value['source_ttl_seconds'] = ttl
    _activate(tracer)
    _create(tracer, 'expire-a', closed='open_source-0')
    tracer.host.app.cache_clock = clock
    second = _create(tracer, 'expire-b')
    assert second['model'] == _model(tracer, 'open_source-0'), 'Fallback must use the original eligible order'
    _, decision = _applied(tracer, 'expire-b')
    assert decision['hint']['preferred'] is None and decision['hint']['reason'] == reason
    assert decision['applied']['adopted'] is False and decision['applied']['reason'] == reason


def test_one_damaged_earlier_source_is_skipped_while_other_trusted_sources_still_count(saved_backup_http):
    tracer = saved_backup_http
    _support(tracer)
    _activate(tracer)
    _create(tracer, 'partial-damaged', closed='open_source-0')
    _create(tracer, 'partial-intact', closed='open_source-0')
    job = tracer.host.service.root / _task_id('studio', 'partial-damaged') / 'job.json'
    job.chmod(0o600)
    job.write_text('{not json')
    second = _create(tracer, 'partial-next')
    assert second['model'] == _model(tracer, 'open_source-1')
    _, decision = _applied(tracer, 'partial-next')
    assert decision['hint']['unreadable_sources'] == 1 and decision['applied']['adopted'] is True


def test_restart_keeps_frozen_versions_and_saved_decisions_without_reordering(saved_backup_http):
    tracer = saved_backup_http
    _support(tracer)
    _activate(tracer)
    _create(tracer, 'restart-a', closed='open_source-0')
    _create(tracer, 'restart-b')
    before, decision = _applied(tracer, 'restart-b')
    # 模擬程序重啟：重新建立親和性模組，只從持久化狀態讀回。
    CacheAffinity(tracer.host.app)
    after, again = _applied(tracer, 'restart-b')
    assert after['frozen_version'] == before['frozen_version'] == 'prefix-affinity-v1'
    assert again == decision
    assert tracer.client().get('/monitor/cache-affinity').json()['active_version'] == 'prefix-affinity-v1'
    third = _create(tracer, 'restart-c')
    assert third['model'] == _model(tracer, 'open_source-1')


def test_disabling_only_affects_new_tasks_and_keeps_old_reasons(saved_backup_http):
    tracer = saved_backup_http
    _support(tracer)
    _activate(tracer)
    _create(tracer, 'disable-a', closed='open_source-0')
    _create(tracer, 'disable-b')
    _, kept = _applied(tracer, 'disable-b')
    _activate(tracer, None)
    third = _create(tracer, 'disable-c')
    assert third['model'] == _model(tracer, 'open_source-0'), 'Disabled policy must not reorder new tasks'
    new = tracer.client().get('/monitor/cache-observations/studio/disable-c').json()['affinity']
    assert new['frozen_version'] is None and new['decisions'] == []
    _, old = _applied(tracer, 'disable-b')
    assert old == kept and old['applied']['adopted'] is True


def test_mismatched_response_model_is_never_a_warm_source():
    assert cache_observations._response_model({'raw': '{"model": "other/model"}'}) == 'other/model'
    assert cache_observations._response_model({'raw': 'not json'}) is None
    assert cache_observations._response_model({'raw': None}) is None

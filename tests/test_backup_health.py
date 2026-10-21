"""Offline native-window health and durable circuit regressions; no model calls."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import multiprocessing
import sqlite3

import httpx
import pytest

from cloudyfan_jev.scores.catalog import digest
from cloudyfan_jev.scores.discovery import _provider_rows
from cloudyfan_jev.scores.health import HealthError, HealthStore
from cloudyfan_jev.scores.metrics import (normalize_endpoints, normalize_provider_health,
                                        normalize_provider_speed)

NOW = datetime(2026, 10, 21, 12, tzinfo=timezone.utc)
CONFIG = 'a' * 64
PROVIDER = 'near-ai/fp8'
POLICY = {'ttl_seconds': 300, 'failure_threshold': 2, 'cooldown_seconds': 60,
          'min_uptime_percent': 99, 'latency_percentile': 'p99', 'max_latency_ms': 10000,
          'throughput_percentile': 'p50', 'min_throughput': 10}


def endpoint():
    return {'tag': PROVIDER, 'provider_name': 'Near AI', 'status': 0,
            'uptime_last_5m': 100, 'uptime_last_30m': 99.5, 'uptime_last_1d': 99.1,
            'latency_last_30m': {'p50': 1424, 'p99': 9234},
            'throughput_last_30m': {'p50': 76, 'p99': 100},
            'pricing': {'prompt': '0.000001', 'completion': '0.000001'}}


def observation(*, now=NOW, provider=PROVIDER, configuration=CONFIG):
    raw = endpoint()
    return {'configuration_sha256': configuration, 'provider': provider,
            'observed_at': now.isoformat(), 'health': normalize_provider_health(raw),
            'speed': normalize_provider_speed(raw)}


def store(tmp_path, *, policy=None):
    time = [NOW]
    return HealthStore(tmp_path / 'health', policy=policy or POLICY, clock=lambda: time[0]), time


def _process_failure(arguments):
    root, event_id = arguments
    return HealthStore(root, policy=POLICY, clock=lambda: NOW).record(event_id, CONFIG, PROVIDER)['recorded']


def test_native_health_windows_preserve_distinct_values_and_units():
    health = normalize_provider_health(endpoint())
    assert health['status'] == 0 and health['status_meaning'] == 'operational'
    assert [health[f'uptime_last_{window}'] for window in ('5m', '30m', '1d')] == [100, 99.5, 99.1]
    assert health['uptime_unit'] == 'percent'
    assert set(health['units'].values()) == {'percent'}
    assert normalize_provider_health({})['status_meaning'] == 'unknown'
    assert normalize_provider_health({'status': 47})['status_meaning'] == 'degraded'


@pytest.mark.parametrize('field,value', [
    ('status', False), ('status', '0'), ('status', 0.0),
    ('uptime_last_5m', '100'), ('uptime_last_30m', True),
    ('uptime_last_1d', -1), ('uptime_last_1d', 100.001),
    ('uptime_last_1d', float('inf')), ('uptime_last_1d', float('nan')),
    ('uptime_unit', 'ratio'), ('units', {'uptime_last_1d': 'seconds'}),
])
def test_health_rejects_malformed_types_and_wrong_units(field, value):
    raw = endpoint()
    raw[field] = value
    with pytest.raises(ValueError):
        normalize_provider_health(raw)


def test_free_get_mock_projects_health_without_changing_speed():
    raw = {'data': {'id': 'example/model', 'endpoints': [endpoint()]}}
    calls = []

    def handler(request):
        calls.append(request.method)
        return httpx.Response(200, json=raw)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        content = client.get('https://example.org/endpoints').content
    row = normalize_endpoints(content, retrieved_at=NOW.isoformat())[0]
    discovered, rejected = _provider_rows(content, 'example/model', {'input_tokens': 2, 'output_tokens': 3})
    assert not rejected and row['health'] == discovered[0]['health']
    assert row['speed']['latency_last_30m'] == {'p50': 1424, 'p99': 9234}
    assert row['speed']['latency_unit'] == 'milliseconds'
    assert row['speed']['throughput_last_30m'] == {'p50': 76, 'p99': 100}
    assert calls == ['GET']


def test_discovery_quarantines_invalid_health_only_for_affected_provider():
    bad = endpoint()
    bad.update(tag='bad', status=True)
    content = json.dumps({'data': {'id': 'example/model', 'endpoints': [endpoint(), bad]}}).encode()
    rows, rejected = _provider_rows(content, 'example/model', {'input_tokens': 1, 'output_tokens': 1})
    assert len(rows) == 1 and rows[0]['provider'] == PROVIDER
    assert rejected == [{'row_index': 1, 'reason': 'invalid_or_ambiguous_provider_row'}]


def test_snapshot_hash_is_computed_immutable_and_persistent(tmp_path):
    health, _ = store(tmp_path)
    row = observation()
    snapshot_id = health.publish([row])
    row['health']['status'] = 4
    assessed = health.assess(CONFIG, PROVIDER)
    assert assessed['eligible'] is True
    assert assessed['observation']['health']['status'] == 0
    assert assessed['snapshot_id'] == snapshot_id
    with sqlite3.connect(health.path) as db:
        payload = json.loads(db.execute('SELECT payload FROM snapshots').fetchone()[0])
        assert digest(payload) == snapshot_id
        with pytest.raises(sqlite3.IntegrityError, match='immutable'):
            db.execute('UPDATE snapshots SET payload="{}"')
    reopened = HealthStore(health.root, policy=POLICY, clock=lambda: NOW)
    assert reopened.assess(CONFIG, PROVIDER) == assessed
    row['snapshot_id'] = 'f' * 64
    with pytest.raises(ValueError, match='Unexpected'):
        health.publish([row])


@pytest.mark.parametrize('age,reason', [(-1, 'observation_in_future'), (300, 'observation_stale'),
                                       (301, 'observation_stale')])
def test_future_and_exact_ttl_boundary_fail_closed(tmp_path, age, reason):
    health, _ = store(tmp_path)
    health.publish([observation(now=NOW - timedelta(seconds=age))])
    assert health.assess(CONFIG, PROVIDER)['reasons'] == [reason]


def test_waiting_never_legitimizes_an_observation_future_dated_at_capture(tmp_path):
    health, time = store(tmp_path)
    health.publish([observation(now=NOW + timedelta(seconds=1))])
    time[0] += timedelta(seconds=2)
    assert health.assess(CONFIG, PROVIDER)['reasons'] == ['observation_in_future']


@pytest.mark.parametrize('change,reason', [
    ('status', 'status_unknown'), ('degraded', 'status_degraded'),
    ('daily', 'uptime_last_1d_below_minimum'), ('missing_window', 'uptime_last_30m_unknown'),
    ('unit', 'uptime_last_1d_unit_unknown_or_invalid'),
    ('latency_unit', 'latency_unit_unknown_or_invalid'), ('latency', 'latency_outside_limit'),
    ('latency_percentile', 'latency_p99_unknown'), ('throughput', 'throughput_outside_limit'),
    ('throughput_unit', 'throughput_unit_unknown_or_invalid'),
    ('throughput_percentile', 'throughput_p50_unknown'), ('invalid_speed', 'latency_invalid'),
])
def test_unknown_degraded_units_and_policy_percentiles_are_never_inferred(tmp_path, change, reason):
    health, _ = store(tmp_path)
    row = observation()
    if change == 'status': row['health'].pop('status')
    if change == 'degraded': row['health']['status'] = 2
    if change == 'daily': row['health']['uptime_last_1d'] = 97
    if change == 'missing_window': row['health'].pop('uptime_last_30m')
    if change == 'unit': row['health'].pop('uptime_unit')
    if change == 'latency_unit': row['speed']['latency_unit'] = 'seconds'
    if change == 'latency': row['speed']['latency_last_30m']['p99'] = 10001
    if change == 'latency_percentile': row['speed']['latency_last_30m'].pop('p99')
    if change == 'throughput': row['speed']['throughput_last_30m']['p50'] = 9
    if change == 'throughput_unit': row['speed']['throughput_unit'] = 'tokens/minute'
    if change == 'throughput_percentile': row['speed']['throughput_last_30m'].pop('p50')
    if change == 'invalid_speed': row['speed']['latency_last_30m']['p99'] = True
    health.publish([row])
    result = health.assess(CONFIG, PROVIDER)
    assert result['eligible'] is False and reason in result['reasons']


def test_only_explicitly_selected_uptime_windows_and_disabled_speed_limits_apply(tmp_path):
    policy = {**POLICY, 'min_uptime_percent': {'uptime_last_5m': 99},
              'max_latency_ms': None, 'min_throughput': None}
    health, _ = store(tmp_path, policy=policy)
    row = observation()
    row['health']['uptime_last_1d'] = None
    row['speed'] = None
    health.publish([row])
    assert health.assess(CONFIG, PROVIDER)['eligible'] is True


def test_missing_observation_and_missing_health_are_unknown(tmp_path):
    health, _ = store(tmp_path)
    assert health.assess(CONFIG, PROVIDER)['reasons'] == ['health_observation_missing']
    row = observation()
    row.pop('health')
    row.pop('speed')
    health.publish([row])
    result = health.assess(CONFIG, PROVIDER)
    assert not result['eligible'] and 'status_unknown' in result['reasons']


def test_partial_refresh_does_not_replace_other_configurations(tmp_path):
    health, time = store(tmp_path)
    first = health.publish([observation(), observation(provider='other', configuration='b' * 64)])
    time[0] += timedelta(seconds=1)
    second = health.publish([observation(now=time[0])])
    assert first != second
    assert health.assess(CONFIG, PROVIDER)['snapshot_id'] == second
    assert health.assess('b' * 64, 'other')['snapshot_id'] == first
    assert health.assess(CONFIG, 'other')['eligible'] is False


def test_duplicate_failure_does_not_count_and_success_resets_closed_sequence(tmp_path):
    health, _ = store(tmp_path)
    health.publish([observation()])
    first = health.record('failure-1', CONFIG, PROVIDER)
    repeated = health.record('failure-1', CONFIG, PROVIDER)
    assert first['recorded'] is True and repeated['recorded'] is False
    assert repeated['circuit']['failures'] == 1
    health.record('success-1', CONFIG, PROVIDER, outcome='success')
    health.record('failure-2', CONFIG, PROVIDER)
    assert health.assess(CONFIG, PROVIDER)['eligible'] is True
    health.record('failure-3', CONFIG, PROVIDER)
    assert health.assess(CONFIG, PROVIDER)['reasons'] == ['cooldown_active']
    with sqlite3.connect(health.path) as db:
        assert db.execute('SELECT COUNT(*) FROM events').fetchone()[0] == 4
        with pytest.raises(sqlite3.IntegrityError, match='immutable'):
            db.execute('DELETE FROM events')
    for args in [('failure-1', CONFIG, 'other', 'availability_failure'),
                 ('failure-1', CONFIG, PROVIDER, 'success')]:
        with pytest.raises(HealthError, match='identity changed'):
            health.record(*args)


def test_restart_cooldown_and_controlled_fresh_recovery(tmp_path):
    health, time = store(tmp_path)
    old = health.publish([observation()])
    health.record('failure-1', CONFIG, PROVIDER)
    health.record('failure-2', CONFIG, PROVIDER)
    health = HealthStore(health.root, policy=POLICY, clock=lambda: time[0])
    time[0] += timedelta(seconds=1)
    new = health.publish([observation(now=time[0])])
    with pytest.raises(HealthError, match='cooldown'):
        health.recover(CONFIG, PROVIDER, new)
    health.record('in-flight-success', CONFIG, PROVIDER, outcome='success')
    assert health.assess(CONFIG, PROVIDER)['circuit']['open']
    time[0] += timedelta(seconds=59)
    assert health.assess(CONFIG, PROVIDER)['reasons'] == ['recovery_required']
    with pytest.raises(HealthError, match='current matching'):
        health.recover(CONFIG, PROVIDER, old)
    assert health.recover(CONFIG, PROVIDER, new)['recovered'] is True
    assert health.assess(CONFIG, PROVIDER)['eligible'] is True
    assert health.recover(CONFIG, PROVIDER, new)['recovered'] is False


def test_recovery_refuses_old_republished_stale_or_degraded_observations(tmp_path):
    health, time = store(tmp_path)
    health.record('failure-1', CONFIG, PROVIDER)
    health.record('failure-2', CONFIG, PROVIDER)
    time[0] += timedelta(seconds=60)
    old = health.publish([observation()])
    with pytest.raises(HealthError, match='newer than'):
        health.recover(CONFIG, PROVIDER, old)
    row = observation(now=time[0])
    row['health']['status'] = 1
    bad = health.publish([row])
    with pytest.raises(HealthError, match='not eligible'):
        health.recover(CONFIG, PROVIDER, bad)
    row['health']['status'] = 0
    fresh = health.publish([row])
    time[0] += timedelta(seconds=300)
    with pytest.raises(HealthError, match='observation_stale'):
        health.recover(CONFIG, PROVIDER, fresh)
    assert health.assess(CONFIG, PROVIDER)['circuit']['open'] is True


def test_concurrent_store_instances_serialize_and_deduplicate_failures(tmp_path):
    health, _ = store(tmp_path)
    health.publish([observation()])

    def record(index):
        instance = HealthStore(health.root, policy=POLICY, clock=lambda: NOW)
        return instance.record(f'failure-{index % 8}', CONFIG, PROVIDER)

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(record, range(64)))
    assert sum(result['recorded'] for result in results) == 8
    assert health.assess(CONFIG, PROVIDER)['circuit']['failures'] == 8
    assert health.assess(CONFIG, PROVIDER)['circuit']['open'] is True


def test_independent_processes_share_circuit_and_event_deduplication(tmp_path):
    health, _ = store(tmp_path)
    with multiprocessing.get_context('spawn').Pool(2) as workers:
        recorded = workers.map(_process_failure, [(str(health.root), f'process-{index % 3}') for index in range(12)])
    assert sum(recorded) == 3
    assert health.assess(CONFIG, PROVIDER)['circuit']['failures'] == 3
    assert health.assess(CONFIG, PROVIDER)['circuit']['open'] is True


@pytest.mark.parametrize('location', ['root', 'lock', 'database', 'journal'])
def test_storage_symlinks_are_refused_without_touching_target(tmp_path, location):
    target = tmp_path / 'target'
    target.mkdir()
    root = tmp_path / 'health'
    if location == 'root':
        root.symlink_to(target, target_is_directory=True)
    else:
        root.mkdir()
        name = {'lock': '.health.lock', 'database': 'health.sqlite3', 'journal': 'health.sqlite3-journal'}[location]
        (root / name).symlink_to(target / 'untouched')
    with pytest.raises(HealthError, match='symlink'):
        HealthStore(root, policy=POLICY, clock=lambda: NOW)
    assert list(target.iterdir()) == []


@pytest.mark.parametrize('field,value', [
    ('ttl_seconds', 0), ('cooldown_seconds', -1), ('failure_threshold', True),
    ('min_uptime_percent', {}), ('min_uptime_percent', {'1day': 99}),
    ('min_uptime_percent', 101), ('latency_percentile', 'p95'), ('max_latency_ms', True),
    ('throughput_percentile', None), ('min_throughput', -1),
])
def test_policy_requires_explicit_supported_thresholds(tmp_path, field, value):
    policy = {**POLICY, field: value}
    with pytest.raises(ValueError):
        HealthStore(tmp_path / 'health', policy=policy, clock=lambda: NOW)


def test_reopen_cannot_change_policy_and_invalid_identity_is_never_a_path(tmp_path):
    health, _ = store(tmp_path)
    with pytest.raises(HealthError, match='policy is immutable'):
        HealthStore(health.root, policy={**POLICY, 'ttl_seconds': 1000}, clock=lambda: NOW)
    with pytest.raises(ValueError, match='SHA256'):
        health.assess('../outside', PROVIDER)
    with pytest.raises(ValueError, match='availability'):
        health.record('bad', CONFIG, PROVIDER, outcome='wrong_answer')
    rows = [observation(), deepcopy(observation())]
    with pytest.raises(ValueError, match='Duplicate'):
        health.publish(rows)


def test_in_memory_policy_mutation_and_clock_rollback_fail_closed(tmp_path):
    health, time = store(tmp_path)
    health.publish([observation(now=NOW - timedelta(seconds=1))])
    time[0] -= timedelta(microseconds=1)
    assert health.assess(CONFIG, PROVIDER)['reasons'] == ['snapshot_in_future']
    time[0] = NOW
    health.record('failure', CONFIG, PROVIDER)
    time[0] -= timedelta(seconds=1)
    with pytest.raises(HealthError, match='clock predates'):
        health.record('another-failure', CONFIG, PROVIDER)
    health.policy['ttl_seconds'] = 100000
    with pytest.raises(HealthError, match='cannot change'):
        health.assess(CONFIG, PROVIDER)

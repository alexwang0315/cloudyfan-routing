"""依體檢自動換池（#78，ADR 0035 第 6–9 點）離線測試；體檢紀錄是測試用假紀錄。"""
import copy

import pytest

from cloudyfan_jev.demo import example_config
from cloudyfan_jev.scores import SnapshotStore, compile_snapshot, select_profiles
from cloudyfan_jev.scores.auto_pool import AutoPoolController
from cloudyfan_jev.scores.catalog import digest
from cloudyfan_jev.scores.checkup import VERSION, wilson_interval

NOW = '2026-10-18T10:00:00+00:00'
T1 = '2026-10-18T11:00:00+00:00'
T2 = '2026-10-18T12:00:00+00:00'
T3 = '2026-10-18T13:00:00+00:00'
POLICY = {'schema_version': 'auto-pool-policy-v1', 'hysteresis': 0.05, 'min_graded': 20,
          'max_checkup_age_days': 14}
REQUIREMENTS = {'domain': 'coding', 'capabilities': ['tools'], 'context_tokens': 1000}
# 排行榜分數：low-a/low-b 在低池、mid-a/mid-b 在中池、high-a/high-b 在高池。
LEADERBOARD = {'low-a': 60, 'low-b': 65, 'mid-a': 90, 'mid-b': 92, 'high-a': 110, 'high-b': 120}


def base_snapshot(extra=None):
    profiles, _ = example_config()
    registry = {'candidates': [], 'open_source': None}
    observations = []
    rows = [('open_source', None), *LEADERBOARD.items(), *(extra or {}).items()]
    for config_id, score in rows:
        tier = config_id.split('-')[0] if config_id != 'open_source' else 'open_source'
        profile = copy.deepcopy(profiles.get(tier, profiles['mid']))
        if config_id != 'open_source':
            profile['model'] = 'model/' + config_id
        entry = {'config_id': config_id, 'profile': profile, 'approved': not config_id.startswith('unapproved'),
                 'context_tokens': 8192, 'capabilities': ['tools'], 'catalog_checked_at': NOW,
                 'estimated_request_usd': .001, 'estimated_latency_ms': 200, 'cost_basis': 'fixture_only',
                 'source_model_id': 'example/' + config_id, 'source': 'codexradar', 'source_version': 'fixture-v1'}
        if config_id == 'open_source':
            entry['open_weight'] = True
            registry['open_source'] = entry
            continue
        registry['candidates'].append(entry)
        observations.append({'source': 'codexradar', 'source_version': 'fixture-v1',
            'source_model_id': 'example/' + config_id, 'metric': 'overall', 'score': score,
            'effort': 'low', 'sample_count': 64, 'evaluated_at': '2026-10-18',
            'retrieved_at': NOW, 'source_url': 'https://deng.codexradar.com/', 'raw_sha256': 'a' * 64})
    policy = {'id': 'coding-fixture-v1', 'domain': 'coding', 'source': 'codexradar',
              'source_version': 'fixture-v1', 'metric': 'overall', 'scale': 'source_native',
              'low_mid': 80, 'mid_high': 100, 'max_source_age_hours': 48,
              'max_catalog_age_hours': 48, 'snapshot_ttl_hours': 24,
              'min_samples': 64, 'max_evaluation_age_days': 30}
    return compile_snapshot(observations, registry, policy, now=NOW)


def record(config_id, passed, graded=100, *, at=T1, cost='0.10'):
    """假體檢紀錄，欄位與 checkup.run_checkup 的輸出一致（只填換池會用到的部分）。"""
    body = {'schema_version': VERSION, 'created_at': at, 'configuration_id': config_id,
            'graded': graded, 'passed': passed, 'score': round(passed / graded, 4),
            'confidence_interval': wilson_interval(passed, graded),
            'complete_task_cost_usd': None if cost is None else str(float(cost) * graded),
            'cost_per_task_usd': cost}
    return {**body, 'record_id': digest(body)}


ALL_BOUNDS = {'model/' + name: object() for name in [*LEADERBOARD, 'late-x']}


def baseline_records(at=T1):
    """與排行榜分群一致的體檢結果：不應觸發換池。"""
    scores = {'low-a': 40, 'low-b': 45, 'mid-a': 55, 'mid-b': 58, 'high-a': 75, 'high-b': 80}
    return [record(c, s, at=at) for c, s in scores.items()]


@pytest.fixture
def controller(tmp_path):
    ctl = AutoPoolController(tmp_path / 'pool')
    ctl.store.publish(base_snapshot(), now=NOW)
    return ctl


def grouping(snapshot):
    return {tier: [e['config_id'] for e in snapshot['pools'][tier]] for tier in ('low', 'mid', 'high')}


def test_significant_checkup_gap_promotes_and_records_option_a_override(controller):
    base = controller.store.load()
    records = [r for r in baseline_records() if r['configuration_id'] != 'mid-b'] + [record('mid-b', 99)]
    result = controller.evaluate(records, POLICY, now=T1, model_bounds=ALL_BOUNDS)
    assert result['status'] == 'changed'
    new = controller.store.load()
    assert new['snapshot_id'] == result['after_snapshot_id'] != base['snapshot_id']
    assert grouping(new) == {'low': ['low-a', 'low-b'], 'mid': ['mid-a'], 'high': ['high-a', 'high-b', 'mid-b']}
    override = new['pool_override']
    assert override['original_grouping'] == grouping(base)
    assert override['overridden_grouping'] == grouping(new)
    assert override['scores_combined'] is False
    [move] = override['moves']
    assert (move['config_id'], move['from'], move['to']) == ('mid-b', 'mid', 'high')
    evidence = override['evidence']['mid-b']
    assert evidence['checkup_record_id'] == records[-1]['record_id']
    assert evidence['confidence_interval']['low'] > 0.9
    # 排行榜分數原樣保留，不與體檢分數相加或平均。
    moved = next(e for e in new['pools']['high'] if e['config_id'] == 'mid-b')
    assert moved['profile']['external_score'] == 92
    assert select_profiles(new, REQUIREMENTS, now=T1)['high']['model'] in ('model/high-a', 'model/high-b', 'model/mid-b')


def test_checkups_consistent_with_leaderboard_change_nothing(controller):
    before = controller.store.load()['snapshot_id']
    result = controller.evaluate(baseline_records(), POLICY, now=T1, model_bounds=ALL_BOUNDS)
    assert result['status'] == 'unchanged'
    assert controller.store.load()['snapshot_id'] == before
    assert controller.changes() == []


def test_significant_but_within_hysteresis_does_not_move(controller):
    # mid-b 的區間下界高於高池上界，但差距小於遲滯門檻 0.05。
    records = [r for r in baseline_records() if r['configuration_id'] != 'mid-b'] + [record('mid-b', 96)]
    gap = records[-1]['confidence_interval']['low'] - max(
        r['confidence_interval']['high'] for r in records if r['configuration_id'].startswith('high'))
    assert 0 < gap <= POLICY['hysteresis']
    assert controller.evaluate(records, POLICY, now=T1, model_bounds=ALL_BOUNDS)['status'] == 'unchanged'
    # 同樣差距但門檻降為 0 才會換：證明擋下的是遲滯，不是顯著性。
    loose = {**POLICY, 'hysteresis': 0.0}
    assert controller.evaluate(records, loose, now=T1, model_bounds=ALL_BOUNDS)['status'] == 'changed'


def test_too_few_graded_items_are_not_evidence(controller):
    records = [r for r in baseline_records() if r['configuration_id'] != 'mid-b'] + [record('mid-b', 10, graded=10)]
    assert controller.evaluate(records, POLICY, now=T1, model_bounds=ALL_BOUNDS)['status'] == 'unchanged'


def test_significant_loss_demotes(controller):
    records = [r for r in baseline_records() if r['configuration_id'] != 'high-a'] + [record('high-a', 20)]
    result = controller.evaluate(records, POLICY, now=T1, model_bounds=ALL_BOUNDS)
    assert result['moves'] == [{'config_id': 'high-a', 'from': 'high', 'to': 'mid',
                                'reason': 'significant_checkup_loss'}]
    assert grouping(controller.store.load())['high'] == ['high-b']


def test_degradation_pauses_admission_without_waiting_for_new_version(controller):
    records = baseline_records() + [record('mid-a', 20, at=T2)]
    result = controller.evaluate(records, POLICY, now=T2, model_bounds=ALL_BOUNDS)
    new = controller.store.load()
    assert result['status'] == 'changed'
    assert grouping(new)['mid'] == ['mid-b']
    [paused] = new['pool_override']['paused']
    assert (paused['config_id'], paused['reason']) == ('mid-a', 'significant_checkup_drop')
    assert {'kind': 'configuration_paused', 'config_id': 'mid-a',
            'reason': 'significant_checkup_drop'} in result['alerts']
    # 暫停持續到操作員明示恢復，回到排行榜原分群。
    again = controller.evaluate(records, POLICY, now=T3, model_bounds=ALL_BOUNDS)
    assert again['status'] == 'unchanged'
    resumed = controller.evaluate([], POLICY, now=T3, model_bounds=ALL_BOUNDS, unpause=['mid-a'])
    assert resumed['status'] == 'changed'
    assert grouping(controller.store.load())['mid'] == ['mid-a', 'mid-b']


def test_pool_emptied_by_pauses_is_disabled_with_alert_not_replaced(controller):
    records = baseline_records() + [record('high-a', 20, at=T2), record('high-b', 25, at=T2)]
    result = controller.evaluate(records, POLICY, now=T2, model_bounds=ALL_BOUNDS)
    new = controller.store.load()
    assert new['pools']['high'] == [] and new['pool_override']['disabled_tiers'] == ['high']
    assert {'kind': 'tier_disabled', 'tier': 'high', 'reason': 'no_qualified_candidates_after_pause',
            'fallback_to_other_tier': False} in result['alerts']
    with pytest.raises(ValueError, match='No qualified configuration for tier: high'):
        select_profiles(new, REQUIREMENTS, now=T2)
    [entry] = controller.changes()
    assert entry['kind'] == 'auto_change' and entry['disabled_tiers'] == ['high']


def test_ordinary_publication_still_refuses_empty_pools(tmp_path):
    snapshot = copy.deepcopy(base_snapshot())
    snapshot['pools']['high'] = []
    body = {k: v for k, v in snapshot.items() if k != 'snapshot_id'}
    snapshot['snapshot_id'] = digest(body)
    with pytest.raises(ValueError, match='All three pools'):
        SnapshotStore(tmp_path / 's').publish(snapshot, now=NOW)


def test_unqualified_or_unknown_configurations_never_enter_a_pool(tmp_path):
    ctl = AutoPoolController(tmp_path / 'pool')
    base = base_snapshot(extra={'unapproved-x': 130})
    assert {'config_id': 'unapproved-x', 'reason': 'configuration_not_approved'} in base['excluded']
    ctl.store.publish(base, now=NOW)
    records = baseline_records() + [record('unapproved-x', 100), record('late-x', 100)]
    assert ctl.evaluate(records, POLICY, now=T1, model_bounds=ALL_BOUNDS)['status'] == 'unchanged'
    pooled = {e['config_id'] for tier in ('low', 'mid', 'high') for e in ctl.store.load()['pools'][tier]}
    assert pooled == set(LEADERBOARD)


def test_move_without_confirmed_fee_bound_is_blocked(controller):
    records = [r for r in baseline_records() if r['configuration_id'] != 'mid-b'] + [record('mid-b', 99)]
    bounds = {k: v for k, v in ALL_BOUNDS.items() if k != 'model/mid-b'}
    result = controller.evaluate(records, POLICY, now=T1, model_bounds=bounds)
    assert result['status'] == 'unchanged'
    assert result['blocked'] == [{'config_id': 'mid-b', 'from': 'mid', 'to': 'high',
                                  'reason': 'fee_bound_unconfirmed'}]


def test_expired_base_snapshot_cannot_be_overridden(controller):
    records = [r for r in baseline_records() if r['configuration_id'] != 'mid-b'] + [record('mid-b', 99)]
    with pytest.raises(ValueError, match='stale'):
        controller.evaluate(records, POLICY, now='2026-10-20T10:00:00+00:00', model_bounds=ALL_BOUNDS)


def test_tampered_checkup_record_is_ignored(controller):
    forged = record('mid-b', 99)
    forged['passed'] = 100
    records = [r for r in baseline_records() if r['configuration_id'] != 'mid-b'] + [forged]
    assert controller.evaluate(records, POLICY, now=T1, model_bounds=ALL_BOUNDS)['status'] == 'unchanged'


def test_old_tasks_keep_their_snapshot(controller):
    old = controller.store.load()
    records = [r for r in baseline_records() if r['configuration_id'] != 'mid-b'] + [record('mid-b', 99)]
    controller.evaluate(records, POLICY, now=T1, model_bounds=ALL_BOUNDS)
    assert controller.store.load(old['snapshot_id']) == old
    assert select_profiles(old, REQUIREMENTS, now=T1)['high']['capability_card']['score_selection'][
        'snapshot_id'] == old['snapshot_id']
    assert controller.store.load()['snapshot_id'] != old['snapshot_id']


def test_change_log_and_rollback_to_previous_snapshot(controller):
    original = controller.store.load()
    records = [r for r in baseline_records() if r['configuration_id'] != 'mid-b'] + [record('mid-b', 99)]
    changed = controller.evaluate(records, POLICY, now=T1, model_bounds=ALL_BOUNDS)
    [entry] = controller.changes()
    assert entry['kind'] == 'auto_change'
    assert (entry['before_snapshot_id'], entry['after_snapshot_id']) == (
        original['snapshot_id'], changed['after_snapshot_id'])
    assert entry['evidence']['mid-b']['confidence_interval']['method'] == 'wilson'
    assert entry['evidence']['mid-b']['capability_price_ratio']['unit'] == 'score_per_usd'
    rolled = controller.rollback(now=T2, operator='operator-1')
    restored = controller.store.load()
    assert rolled['restored_snapshot_id'] == original['snapshot_id']
    assert grouping(restored) == grouping(original)
    assert restored['pool_override']['kind'] == 'rollback'
    assert [row['kind'] for row in controller.changes()] == ['auto_change', 'rollback']


def test_rollback_requires_a_recorded_change(controller):
    with pytest.raises(ValueError, match='No recorded change'):
        controller.rollback(now=T1, operator='operator-1')


def test_global_switch_off_suppresses_changes_but_logs_them(controller):
    before = controller.store.load()['snapshot_id']
    assert controller.auto_switch_enabled() is True
    controller.set_auto_switch(False, now=T1, operator='operator-1')
    records = [r for r in baseline_records() if r['configuration_id'] != 'mid-b'] + [record('mid-b', 99)]
    result = controller.evaluate(records, POLICY, now=T1, model_bounds=ALL_BOUNDS)
    assert result['status'] == 'auto_switch_disabled'
    assert controller.store.load()['snapshot_id'] == before
    assert [row['kind'] for row in controller.changes()] == ['auto_switch', 'suppressed']
    controller.set_auto_switch(True, now=T2, operator='operator-1')
    assert controller.evaluate(records, POLICY, now=T2, model_bounds=ALL_BOUNDS)['status'] == 'changed'

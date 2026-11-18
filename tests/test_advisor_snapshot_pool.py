"""顧問候選對照評分快照（ADR 0034 第 2 點）：候選只能來自現行快照低／中／高池的已入池設定。

全部離線：快照由 compile_snapshot 以測試資料產生，模型只用 httpx.MockTransport。
"""
import copy
from datetime import datetime

import pytest

from cloudyfan_jev import advisor_flow
from cloudyfan_jev.advisor import AdvisorConfig
from cloudyfan_jev.scores.catalog import digest
from test_advisor_first import API, ADVISOR_MODEL, decision, make_core, rec, role_profile, run
import test_auto_pool

NOW = test_auto_pool.NOW
INSIDE = '2026-10-18T12:00:00+00:00'      # 快照有效期內（建立後 24 小時內）
EXPIRED = '2026-10-20T10:00:00+00:00'     # 快照已過期


def snapshot():
    return test_auto_pool.base_snapshot()


def from_snapshot(snap=None, **kw):
    return AdvisorConfig.from_snapshot(snap or snapshot(), now=INSIDE, advisor=role_profile('advisor'),
                                       judge=role_profile('judge'), **kw)


def test_pool_is_built_from_every_qualified_low_mid_high_entry():
    snap = snapshot()
    config = from_snapshot(snap)
    assert config.score_snapshot == snap['snapshot_id']
    assert set(config.pool) == {'low-a', 'low-b', 'mid-a', 'mid-b', 'high-a', 'high-b'}
    assert {k: v['tier'] for k, v in config.pool.items()} == {
        'low-a': 'low', 'low-b': 'low', 'mid-a': 'mid', 'mid-b': 'mid', 'high-a': 'high', 'high-b': 'high'}
    selection = config.pool['mid-a']['capability_card']['score_selection']
    assert selection['snapshot_id'] == snap['snapshot_id'] and selection['config_id'] == 'mid-a'
    assert config.snapshot_valid_until == snap['valid_until']
    # 開源池不是顧問可建議的品質池。
    assert 'open_source' not in config.pool


def test_requirements_filter_incompatible_entries():
    config = from_snapshot(requirements={'domain': 'coding', 'capabilities': ['tools'], 'context_tokens': 1000})
    assert len(config.pool) == 6
    with pytest.raises(ValueError, match='No qualified'):
        from_snapshot(requirements={'domain': 'coding', 'capabilities': ['vision'], 'context_tokens': 1000})
    with pytest.raises(ValueError, match='domain'):
        from_snapshot(requirements={'domain': 'sql', 'capabilities': [], 'context_tokens': 1000})


def test_disabled_tiers_from_auto_pool_are_excluded():
    snap = snapshot()
    body = {k: copy.deepcopy(v) for k, v in snap.items() if k != 'snapshot_id'}
    body['pools']['high'] = []
    body['pool_override'] = {'disabled_tiers': ['high']}
    snap = {**body, 'snapshot_id': digest(body)}
    config = from_snapshot(snap)
    assert {v['tier'] for v in config.pool.values()} == {'low', 'mid'}


def test_stale_or_tampered_snapshot_is_refused():
    with pytest.raises(ValueError, match='stale'):
        AdvisorConfig.from_snapshot(snapshot(), now=EXPIRED, advisor=role_profile('advisor'),
                                    judge=role_profile('judge'))
    tampered = snapshot()
    tampered['pools']['mid'][0]['profile']['effort'] = 'high'
    with pytest.raises(ValueError, match='digest'):
        from_snapshot(tampered)


def test_snapshot_identity_and_expiry_are_frozen_in_the_descriptor():
    snap = snapshot()
    config = from_snapshot(snap)
    described = config.descriptor({})
    assert described['score_snapshot'] == snap['snapshot_id']
    assert described['pool_source'] == {'kind': 'score_snapshot', 'snapshot_id': snap['snapshot_id'],
                                         'valid_until': snap['valid_until']}
    manual = AdvisorConfig(advisor=role_profile('advisor'), judge=role_profile('judge'), score_snapshot='manual-x')
    # 既有手動設定的凍結描述保持原樣（不新增欄位），舊題續跑不受影響。
    assert 'pool_source' not in manual.descriptor(_profiles())


def _profiles():
    from cloudyfan_jev.demo import example_config
    return example_config()[0]


def test_recommendation_dispatches_the_frozen_snapshot_profile(tmp_path, monkeypatch):
    monkeypatch.setattr(advisor_flow, '_now', lambda: datetime.fromisoformat(INSIDE))
    config = from_snapshot()
    model = config.pool['high-b']['model_version']
    api = API(decision('delegate', recommendations=[rec('high-b', 'high'), rec('mid-a', 'mid')]), {model: '2'})
    result = run(make_core(tmp_path, api, config))
    assert result['status'] == 'passed'
    assert api.models() == [ADVISOR_MODEL, model]
    assert result['attempts'][0]['setting'] == 'high-b'
    assert result['attempts'][0]['calls'][0]['tier'] == 'pool:high-b'


def test_expired_snapshot_stops_before_any_model_call(tmp_path, monkeypatch):
    monkeypatch.setattr(advisor_flow, '_now', lambda: datetime.fromisoformat(EXPIRED))
    api = API(decision('answer', '2'))
    with pytest.raises(ValueError, match='expired'):
        run(make_core(tmp_path, api, from_snapshot()))
    assert api.requests == []


def test_manual_pool_keeps_previous_behaviour(tmp_path):
    api = API(decision('answer', '2'))
    assert run(make_core(tmp_path, api))['status'] == 'passed'


def test_advisor_cannot_name_a_setting_outside_the_snapshot(tmp_path, monkeypatch):
    monkeypatch.setattr(advisor_flow, '_now',
                        lambda: datetime.fromisoformat(INSIDE))
    config = from_snapshot()
    api = API(decision('delegate', recommendations=[rec('mid', 'mid'), rec('frontier-x', 'high')]))
    result = run(make_core(tmp_path, api, config))
    assert result['status'] == 'advisor_incomplete'
    assert [v['reason'] for v in result['advisor']['decision']['voided']] == ['not_in_pool', 'not_in_pool']
    assert api.models() == [ADVISOR_MODEL]

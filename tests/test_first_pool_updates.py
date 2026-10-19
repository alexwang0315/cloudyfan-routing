"""Offline first-release update and routing workflow with actual SQLite."""
from test_first_pool import NOW


def test_pool_delivery_covers_binary_routes_update_expiry_and_saved_resume(tmp_path):
    from cloudyfan_jev.scores.pool_demo import demonstrate_pool
    result = demonstrate_pool(tmp_path / 'demo', now=NOW)
    assert result['synthetic'] is True
    assert result['real_api_calls'] == 0
    assert result['production_ready'] is False
    assert [r['selected_tier'] for r in result['routes']] == ['open_source', 'low', 'mid', 'high']
    assert [r['model_requests'] for r in result['routes']] == [1, 3, 4, 4]
    assert all(r['status'] == 'passed' for r in result['routes'])
    assert result['simulated_api_usd'] == '0.0085'
    assert result['update_status'] == 'published'
    assert result['original_snapshot_id'] != result['new_snapshot_id']
    assert result['new_task_snapshot_id'] == result['new_snapshot_id']
    assert result['resume_snapshot_id'] == result['original_snapshot_id']
    assert result['resume_posts'] == 0
    assert result['expired_new_run_blocked'] is True
    assert result['unconfirmed_fee_bound_blocked'] is True
    assert result['blocked_update_kept_current'] is True

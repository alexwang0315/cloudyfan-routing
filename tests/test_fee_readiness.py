from test_first_pool import profile


def test_fee_gaps_bind_every_reachable_role_without_turning_receipts_into_caps():
    from cloudyfan_jev.scores.fee_readiness import fee_gap_matrix
    report = fee_gap_matrix(profile())
    assert report['dispatch_qualified'] is False
    assert report['workflow_upper_bound_usd'] is None
    assert [row['role'] for row in report['roles']] == ['open_source', 'low', 'mid', 'high', 'classification']
    assert report['workflow_reservation'] == 'open_source + 2 * classification + max(low, mid, high)'
    for row in report['roles']:
        assert set(row['components']) == {'input', 'output', 'reasoning', 'cache_read', 'cache_write',
                                           'request', 'classification', 'failure'}
        assert row['account_period'] is None
        assert row['upper_bound_usd'] is None
        assert all(item['status'] == 'pending' for item in row['components'].values())
        assert row['paths'] == ['success', 'rejection', 'failure', 'cancellation', 'disconnect',
                                'upstream_retry_child', 'price_change_inflight']
    assert report['rejection_policy']['is_supplier_cap'] is False
    assert report['historical_accounting']['clears_earlier_unknowns'] is False

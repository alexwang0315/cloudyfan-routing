"""Reapply saved capacity-window gates through the original main-service HTTP."""
from decimal import Decimal

from saved_window_fixture import saved_window_http as saved_window_http
from test_local_http_integration import loopback_network as loopback_network


def test_same_task_saved_wait_and_open_revision_one_reproduce_without_dispatch(saved_window_http):
    tracer = saved_window_http
    tracer.activate()
    tracer.submit('window-original', scenario='window')
    task, original = tracer.history('window-original')
    assert task['generation_attempts'] == 2 and task['classifier_calls'] == 0
    assert [row['transition'] for row in original['attempts']] == ['initial', 'availability_fallback']
    windows = [row['capacity_window'] for row in original['stages'][1]['candidates']]
    assert any(row['status'] == 'wait' and row['revision'] == 1 and row['evidence_refs'] for row in windows)
    assert any(row['status'] == 'open' and row['revision'] == 1 for row in windows)
    assert len(tracer.fixture.requests) == 2 and len(tracer.validations) == 1
    before = tracer.snapshot('before-comparison')
    response = tracer.compare(['window-original'])
    tracer.read_twice(response)
    assert tracer.snapshot('after-comparison-and-gets') == before
    item = response.json()['items'][0]
    assert item['original']['task_status'] == 'passed' and len(item['original']['path']) == 2
    assert item['reproduction']['status'] == 'reproduced', 'Bound saved window gates were rejected as unavailable'
    assert [(row['stage'], row['action'], row['config_id'], row['match'])
        for row in item['reproduction']['decisions']] == [
        ('initial', 'direct', 'open_source-0', True), ('same_pool', 'direct', 'open_source-1', True),
        ('after_validation', 'stop', None, True)]


def test_cross_task_saved_window_wait_reproduces_only_with_both_tasks_selected(saved_window_http):
    tracer = saved_window_http
    tracer.activate()
    tracer.submit('window-source', scenario='window')
    tracer.history('window-source')
    tracer.submit('window-target')
    task, original = tracer.history('window-target')
    skipped = original['stages'][0]['candidates'][0]
    assert skipped['state'] == 'excluded_before_dispatch' and 'capacity_window_wait' in skipped['reasons']
    assert skipped['capacity_window']['status'] == 'wait' and skipped['capacity_window']['revision'] == 1
    assert len(skipped['capacity_window']['evidence_refs']) == 1
    assert task['generation_attempts'] == 1 and task['classifier_calls'] == 0
    assert len(tracer.fixture.requests) == 3 and len(tracer.validations) == 2
    before = tracer.snapshot('before-comparison')
    response = tracer.compare(['window-source', 'window-target'])
    tracer.read_twice(response)
    assert tracer.snapshot('after-comparison-and-gets') == before
    items = response.json()['items']
    assert len(items) == 2 and all(item['reproduction']['status'] == 'reproduced' for item in items)
    target = next(item for item in items if item['task']['run_id'] == 'window-target')
    assert Decimal(target['original']['costs']['complete_api_usd']) == Decimal('.01')
    assert [(row['stage'], row['config_id'], row['match']) for row in target['reproduction']['decisions']] == [
        ('initial', 'open_source-1', True), ('after_validation', None, True)]


def test_saved_expiry_open_revision_two_preserves_original_health_exclusion(saved_window_http):
    tracer = saved_window_http
    tracer.activate()
    for index in range(2):
        if index:
            tracer.fixture.clock.now += 30
        tracer.submit(f'window-failure-{index}', scenario='window')
        tracer.history(f'window-failure-{index}')
    assert len(tracer.fixture.requests) == 4 and len(tracer.validations) == 2
    first = tracer.snapshot('before-first-comparison')
    old = tracer.compare(['window-failure-0', 'window-failure-1'])
    tracer.read_twice(old)
    assert tracer.snapshot('after-first-comparison') == first
    tracer.fixture.clock.now += 60
    tracer.submit('window-expired-health')
    task, original = tracer.history('window-expired-health')
    skipped = original['stages'][0]['candidates'][0]
    assert skipped['state'] == 'excluded_before_dispatch' and 'recovery_required' in skipped['reasons']
    assert skipped['capacity_window']['status'] == 'open' and skipped['capacity_window']['revision'] == 2
    assert task['generation_attempts'] == 1 and task['classifier_calls'] == 0
    assert len(tracer.fixture.requests) == 5 and len(tracer.validations) == 3
    before = tracer.snapshot('before-comparison')
    response = tracer.compare(['window-failure-0', 'window-failure-1', 'window-expired-health'])
    tracer.read_twice(response)
    tracer.read_twice(old)
    assert tracer.snapshot('after-comparison-and-both-frozen-gets') == before
    items = response.json()['items']
    assert len(items) == 3 and all(item['reproduction']['status'] == 'reproduced' for item in items)
    target = next(item for item in items if item['task']['run_id'] == 'window-expired-health')
    assert Decimal(target['original']['costs']['complete_api_usd']) == Decimal('.01')
    assert [(row['stage'], row['config_id'], row['match']) for row in target['reproduction']['decisions']] == [
        ('initial', 'open_source-1', True), ('after_validation', None, True)]


def test_unrelated_missing_job_cannot_make_window_reference_depend_on_scope_order(saved_window_http):
    tracer = saved_window_http
    tracer.activate()
    tracer.submit('unrelated-a')
    tracer.history('unrelated-a')
    tracer.submit('window-source-b', scenario='window')
    tracer.history('window-source-b')
    tracer.submit('window-target-c')
    tracer.history('window-target-c')
    assert len(tracer.fixture.requests) == 4 and len(tracer.validations) == 3
    tracer.preserve_missing_job('unrelated-a')
    before = tracer.snapshot('before-comparison')
    reports = []
    for index, runs in enumerate((['window-target-c', 'unrelated-a', 'window-source-b'],
                                 ['window-target-c', 'window-source-b', 'unrelated-a'])):
        response = tracer.compare(runs)
        tracer.read_twice(response)
        assert tracer.snapshot(f'after-order-{index}-and-gets') == before
        reports.append(response.json())
    for report in reports:
        broken = next(item for item in report['items'] if item['task']['run_id'] == 'unrelated-a')
        assert broken['reproduction']['status'] in ('unknown', 'not_evaluable')
        assert broken['reproduction']['reasons'] == ['original_evidence_unavailable']
        target = next(item for item in report['items'] if item['task']['run_id'] == 'window-target-c')
        assert target['reproduction']['status'] == 'reproduced', 'Unrelated missing job blocked a valid selected window source'
        assert all(row['match'] is True for row in target['reproduction']['decisions'])

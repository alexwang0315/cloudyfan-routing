"""Reapply saved capacity-window gates through the original main-service HTTP."""
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

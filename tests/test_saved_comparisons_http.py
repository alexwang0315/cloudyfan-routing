"""Original saved-policy reproduction through the approved main-service HTTP seam."""
import json
from pathlib import Path
import sqlite3
import threading
from dataclasses import replace

from task_features_fixture import task_features_http as task_features_http
from test_local_http_integration import loopback_network as loopback_network


def test_operator_reproduces_a_saved_original_policy_without_dispatching_another_task(task_features_http):
    tracer = task_features_http
    operator, client = tracer.client('operator'), tracer.client('agent')
    capacity = operator.get('/monitor/capacity')
    assert capacity.status_code == 200
    assert operator.post('/monitor/capacity/activate', json={
        'revision': capacity.json()['dispatch']['revision'], 'version': tracer.controlled.policy_version}).status_code == 200
    payload = {'run_id': 'saved-policy-source', 'category': tracer.controlled.category,
               'prompt': 'PRIVATE_SAVED_POLICY_CANARY 1+1'}
    original = client.post('/v1/tasks', json=payload)
    assert original.status_code == 202
    assert tracer.dispatch() is not None
    completed = client.get('/v1/tasks/' + payload['run_id'])
    assert completed.status_code == 200 and completed.json()['status'] == 'passed'
    before = tracer.snapshot('before-original-policy-comparison')
    created = operator.post('/monitor/comparisons', json={
        'policy_version': 'original-policy-reproduction-v1',
        'tasks': [{'client': 'agent', 'run_id': payload['run_id']}]})
    assert created.status_code == 200, 'The real operator service cannot create a saved policy comparison'
    report = created.json()
    assert report['schema_version'] == 'saved-policy-comparison-v1'
    assert report['comparison_id'].startswith('cmp-')
    assert report['status'] == 'completed' and len(report['items']) == 1
    item = report['items'][0]
    assert item['task'] == {'client': 'agent', 'run_id': payload['run_id']}
    assert item['original']['task_status'] == 'passed'
    assert item['reproduction']['status'] == 'reproduced'
    assert item['reproduction']['decisions'][0]['selected_tier'] == 'open_source'
    for _ in range(2):
        response = operator.get('/monitor/comparisons/' + report['comparison_id'])
        assert response.status_code == 200 and response.json() == report
        assert 'PRIVATE_SAVED_POLICY_CANARY' not in response.text
    after = tracer.snapshot('after-original-policy-comparison-and-polls')
    for key in ('model_requests', 'source_requests', 'accounting_requests', 'health_sha256',
                'budget_sqlite_sha256', 'capacity_sha256', 'queue_sha256'):
        assert before[key] == after[key]
    assert {name: value for name, value in after['raw_files_sha256'].items()
            if not name.startswith('.saved-comparisons/')} == before['raw_files_sha256']
    assert any(name.startswith('.saved-comparisons/') for name in after['raw_files_sha256'])
    assert client.get('/monitor/comparisons/' + report['comparison_id']).status_code == 401


def _completed(tracer, run='saved-original-controls'):
    operator, client = tracer.client('operator'), tracer.client('agent')
    capacity = operator.get('/monitor/capacity')
    assert operator.post('/monitor/capacity/activate', json={
        'revision': capacity.json()['dispatch']['revision'], 'version': tracer.controlled.policy_version}).status_code == 200
    assert client.post('/v1/tasks', json={'run_id': run, 'category': tracer.controlled.category,
        'prompt': 'PRIVATE_SAVED_POLICY_CANARY original synthetic task'}).status_code == 202
    assert tracer.dispatch() is not None
    assert client.get('/v1/tasks/' + run).json()['status'] == 'passed'
    return operator, {'policy_version': 'original-policy-reproduction-v1',
                      'tasks': [{'client': 'agent', 'run_id': run}]}


def test_expired_comparison_keeps_safe_route_and_original_fees_without_raw_reopen(task_features_http):
    tracer = task_features_http
    operator, body = _completed(tracer)
    created = operator.post('/monitor/comparisons', json=body)
    assert created.status_code == 200
    frozen = created.json()
    tracer.clock.now += 31 * 86400
    # The original raw source is now unusable; preserve it before replacing the
    # synthetic file. An expired comparison must not try to parse this path.
    original = next((tracer.controlled.host.root / 'tasks').glob('*/job.json'))
    original.rename(original.with_name('job-before-expiry.json'))
    original.write_text('{invalid PRIVATE_SAVED_POLICY_CANARY')
    before = tracer.snapshot('before-expired-comparison')
    assert operator.get('/monitor/comparisons/' + frozen['comparison_id']).json() == frozen
    expired = operator.post('/monitor/comparisons', json=body)
    assert expired.status_code == 200
    item = expired.json()['items'][0]
    assert item['original']['task_status'] == 'expired'
    assert item['original']['path'][0]['tier'] == 'open_source'
    assert item['original']['costs']['complete_api_usd'] == frozen['items'][0]['original']['costs']['complete_api_usd']
    assert item['original']['costs']['complete_api_usd'] is not None
    assert item['reproduction']['status'] == 'not_evaluable'
    assert item['reproduction']['reasons'] == ['original_raw_access_expired']
    assert not {'original_task_job', 'original_manifest', 'original_call_intent', 'original_call_response'} & {
        row['kind'] for row in item['evidence']}
    assert 'PRIVATE_SAVED_POLICY_CANARY' not in expired.text
    after = tracer.snapshot('after-expired-comparison')
    for key in ('model_requests', 'source_requests', 'accounting_requests', 'health_sha256',
                'budget_sqlite_sha256', 'capacity_sha256', 'queue_sha256'):
        assert before[key] == after[key]


def test_comparison_rejects_browser_sources_and_corrupt_original_hash(task_features_http):
    tracer = task_features_http
    operator, body = _completed(tracer)
    for extra in ({'path': '/private/arbitrary'}, {'url': 'https://example.invalid'},
                  {'answer': 'PRIVATE_SAVED_POLICY_CANARY'}, {'code': 'execute'}):
        assert operator.post('/monitor/comparisons', json={**body, **extra}).status_code == 422
    assert operator.post('/monitor/comparisons', json={**body, 'tasks': body['tasks'] * 2}).status_code == 422
    assert operator.post('/monitor/comparisons', json={**body, 'tasks': [
        {'client': 'agent', 'run_id': '../escape'}]}).status_code == 422
    assert operator.get('/monitor/comparisons?path=/private/arbitrary').status_code == 422
    result = next((tracer.controlled.host.root / 'receipts-primary/runs').glob('*/result.json'))
    data = json.loads(result.read_bytes())
    result.rename(result.with_name('result-original-before-corruption.json'))
    data['config_sha256'] = '0' * 64
    result.write_text(json.dumps(data))
    compared = operator.post('/monitor/comparisons', json=body)
    assert compared.status_code == 200
    item = compared.json()['items'][0]
    assert item['reproduction']['status'] == 'not_evaluable'
    assert item['original']['costs']['complete_api_usd'] is not None
    assert not item['reproduction']['decisions']


def test_saved_report_reads_do_not_refresh_after_sources_change(task_features_http):
    tracer = task_features_http
    operator, body = _completed(tracer)
    response = operator.post('/monitor/comparisons', json=body)
    assert response.status_code == 200
    report = response.json()
    store = Path(tracer.controlled.app.store.path).parent / '.saved-comparisons'
    inputs = json.loads((store / (report['comparison_id'] + '.inputs.json')).read_bytes())
    assert inputs[0]['values'] and 'PRIVATE_SAVED_POLICY_CANARY' not in json.dumps(inputs)
    before = tracer.snapshot('before-source-change-polls')
    tracer.controlled.host.service.contracts.clear()
    tracer.clock.now += 366 * 86400
    for _ in range(2):
        actual = operator.get('/monitor/comparisons/' + report['comparison_id'])
        assert actual.status_code == 200 and actual.json() == report
    after = tracer.snapshot('after-source-change-polls')
    for key in ('model_requests', 'source_requests', 'accounting_requests', 'health_sha256',
                'budget_sqlite_sha256', 'capacity_sha256', 'queue_sha256', 'raw_files_sha256'):
        assert before[key] == after[key]


def test_comparison_reads_original_committed_wal_without_mutating_original_database(task_features_http):
    tracer = task_features_http
    operator, body = _completed(tracer)
    database = tracer.controlled.app.store.path
    with sqlite3.connect(database) as hold:
        assert hold.execute('PRAGMA journal_mode=WAL').fetchone()[0] == 'wal'
        hold.execute('SELECT count(*) FROM bindings').fetchone()
        assert tracer.client('agent').post('/v1/tasks', json={
            'run_id': 'wal-only-bound', 'category': tracer.controlled.category,
            'prompt': 'PRIVATE_WAL_CANARY synthetic queued task'}).status_code == 202
        wal = Path(str(database) + '-wal')
        before_wal = wal.read_bytes()
        before_main = database.read_bytes()
        assert before_wal
        catalog = operator.get('/monitor/comparisons')
        assert catalog.status_code == 200
        assert any(row['run_id'] == 'wal-only-bound' for row in catalog.json()['task_scopes'])
        compared = operator.post('/monitor/comparisons', json={**body, 'tasks': [
            *body['tasks'], {'client': 'agent', 'run_id': 'wal-only-bound'}]})
        assert compared.status_code == 200
        rows = {row['task']['run_id']: row for row in compared.json()['items']}
        assert rows['saved-original-controls']['reproduction']['status'] == 'reproduced'
        assert rows['wal-only-bound']['original']['task_status'] == 'queued'
        assert rows['wal-only-bound']['reproduction']['status'] == 'not_evaluable'
        assert any(row['kind'] == 'original_database_wal' and row['bytes'] > 0
                   for row in rows['wal-only-bound']['evidence'])
        assert wal.read_bytes() == before_wal and database.read_bytes() == before_main
        assert 'PRIVATE_WAL_CANARY' not in compared.text


def test_original_budget_symlink_and_revoked_principal_are_not_new_evidence_authority(task_features_http):
    tracer = task_features_http
    operator, body = _completed(tracer)
    budget = tracer.controlled.budgets['primary'].path
    original = budget.with_name('budget-before-symlink.sqlite')
    budget.rename(original)
    budget.symlink_to(original)
    compared = operator.post('/monitor/comparisons', json=body)
    assert compared.status_code == 200
    assert compared.json()['items'][0]['reproduction']['status'] == 'not_evaluable'
    assert compared.json()['items'][0]['original']['costs']['complete_api_usd'] is None
    budget.rename(budget.with_name('rejected-budget-link'))
    original.rename(budget)
    tracer.controlled.app.identity_store.set_active('agent', False)
    assert tracer.controlled.app.identity_store.is_active('agent') is False
    denied = operator.post('/monitor/comparisons', json=body)
    assert denied.status_code == 200
    assert denied.json()['items'][0]['reproduction']['reasons'] == ['original_data_permission_unavailable']
    assert not {'original_task_job', 'original_manifest', 'original_call_intent', 'original_call_response'} & {
        row['kind'] for row in denied.json()['items'][0]['evidence']}


def test_comparison_uses_no_slot_or_new_upstream_while_original_task_is_held(task_features_http):
    tracer = task_features_http
    operator, body = _completed(tracer)
    tracer.controlled.flags['hold'] = True
    assert tracer.client('studio').post('/v1/tasks', json={
        'run_id': 'held-original', 'category': tracer.controlled.category, 'prompt': 'PRIVATE_HELD_CANARY'}).status_code == 202
    outcome = []
    thread = threading.Thread(target=lambda: outcome.append(tracer.dispatch()), daemon=True)
    thread.start()
    try:
        assert tracer.controlled.held.wait(5), 'Real original upstream was not held'
        assert tracer.client('agent').post('/v1/tasks', json={
            'run_id': 'unstarted-original', 'category': tracer.controlled.category,
            'prompt': 'PRIVATE_UNSTARTED_CANARY'}).status_code == 202
        before = tracer.snapshot('held-before-comparison')
        report_response = operator.post('/monitor/comparisons', json={**body, 'tasks': [*body['tasks'],
            {'client': 'studio', 'run_id': 'held-original'}, {'client': 'agent', 'run_id': 'unstarted-original'}]})
        assert report_response.status_code == 200
        report = report_response.json()
        rows = {row['task']['run_id']: row for row in report['items']}
        assert rows['saved-original-controls']['reproduction']['status'] == 'reproduced'
        assert rows['held-original']['reproduction']['status'] in ('partial', 'unknown', 'not_evaluable')
        assert rows['unstarted-original']['reproduction']['status'] == 'not_evaluable'
        assert operator.get('/monitor/comparisons/' + report['comparison_id']).json() == report
        assert thread.is_alive() and not tracer.controlled.released.is_set()
        after = tracer.snapshot('held-after-comparison')
        for key in ('model_requests', 'source_requests', 'accounting_requests', 'health_sha256',
                    'budget_sqlite_sha256', 'capacity_sha256', 'queue_sha256'):
            assert before[key] == after[key]
    finally:
        tracer.controlled.released.set()
        thread.join(5)
    assert not thread.is_alive() and outcome
    assert operator.get('/monitor/comparisons/' + report['comparison_id']).json() == report


def test_oversized_saved_input_and_scope_are_bounded_without_dispatch(task_features_http):
    tracer = task_features_http
    operator, body = _completed(tracer)
    limits = operator.get('/monitor/comparisons').json()['limits']
    assert limits['worker_wall_seconds'] == 5 and limits['worker_cpu_seconds'] == 2
    assert operator.post('/monitor/comparisons', json={**body, 'tasks': [
        {'client': 'agent', 'run_id': 'task-' + str(index)} for index in range(limits['max_tasks'] + 1)]}).status_code == 422
    job = next((tracer.controlled.host.root / 'tasks').glob('*/job.json'))
    job.rename(job.with_name('job-before-size-control.json'))
    job.write_bytes(b' ' * (limits['max_source_file_bytes'] + 1))
    before = tracer.snapshot('before-source-size-control')
    response = operator.post('/monitor/comparisons', json=body)
    assert response.status_code == 200 and len(response.content) <= limits['max_output_bytes']
    assert response.json()['items'][0]['reproduction']['status'] == 'not_evaluable'
    assert not response.json()['items'][0]['reproduction']['decisions']
    after = tracer.snapshot('after-source-size-control')
    for key in ('model_requests', 'source_requests', 'accounting_requests', 'health_sha256',
                'budget_sqlite_sha256', 'capacity_sha256', 'queue_sha256'):
        assert before[key] == after[key]


def test_unknown_original_fee_and_missing_task_stay_in_report_without_resending(task_features_http):
    tracer = task_features_http
    operator = tracer.client('operator')
    capacity = operator.get('/monitor/capacity')
    assert operator.post('/monitor/capacity/activate', json={
        'revision': capacity.json()['dispatch']['revision'], 'version': tracer.controlled.policy_version}).status_code == 200
    tracer.controlled.flags['scenario'] = 'unknown_fee'
    assert tracer.client('agent').post('/v1/tasks', json={
        'run_id': 'unknown-fee-original', 'category': tracer.controlled.category,
        'prompt': 'PRIVATE_UNKNOWN_FEE_CANARY'}).status_code == 202
    assert tracer.dispatch() is not None
    before = tracer.snapshot('unknown-fee-before-comparison')
    assert before['budget']['primary']['unknown_events'] > 0
    response = operator.post('/monitor/comparisons', json={
        'policy_version': 'original-policy-reproduction-v1', 'tasks': [
            {'client': 'agent', 'run_id': 'unknown-fee-original'},
            {'client': 'agent', 'run_id': 'never-created'}]})
    assert response.status_code == 200
    report = response.json()
    assert report['counts']['requested'] == 2 and len(report['items']) == 2
    rows = {row['task']['run_id']: row for row in report['items']}
    original = rows['unknown-fee-original']['original']
    assert original['costs']['unknown_events'] > 0 and original['costs']['complete_api_usd'] is None
    assert rows['unknown-fee-original']['reproduction']['status'] == 'not_evaluable'
    assert rows['never-created']['reproduction']['reasons'] == ['original_binding_unavailable']
    assert all(not row['reproduction']['adopted'] for row in rows.values())
    assert 'PRIVATE_UNKNOWN_FEE_CANARY' not in response.text
    after = tracer.snapshot('unknown-fee-after-comparison')
    for key in ('model_requests', 'source_requests', 'accounting_requests', 'health_sha256',
                'budget_sqlite_sha256', 'capacity_sha256', 'queue_sha256'):
        assert before[key] == after[key]


def test_comparison_get_rejects_changed_frozen_input_without_recompute(task_features_http):
    tracer = task_features_http
    operator, body = _completed(tracer)
    response = operator.post('/monitor/comparisons', json=body)
    assert response.status_code == 200
    report = response.json()
    file = Path(tracer.controlled.app.store.path).parent / '.saved-comparisons' / (report['comparison_id'] + '.inputs.json')
    file.rename(file.with_name(report['comparison_id'] + '.inputs-before-corruption.json'))
    file.write_text('["PRIVATE_CORRUPTED_REPORT_CANARY"]')
    before = tracer.snapshot('before-corrupted-report-poll')
    rejected = operator.get('/monitor/comparisons/' + report['comparison_id'])
    assert rejected.status_code == 503
    assert rejected.json() == {'error': {'code': 'comparison_evidence_unavailable'}}
    assert 'PRIVATE_CORRUPTED_REPORT_CANARY' not in rejected.text
    after = tracer.snapshot('after-corrupted-report-poll')
    for key in ('model_requests', 'source_requests', 'accounting_requests', 'health_sha256',
                'budget_sqlite_sha256', 'capacity_sha256', 'queue_sha256', 'raw_files_sha256'):
        assert before[key] == after[key]


def test_reused_contract_cannot_replace_original_fee_authority_with_an_empty_account(task_features_http):
    from cloudyfan_jev.budget import BudgetStore, BudgetedCore
    tracer = task_features_http
    operator, body = _completed(tracer)
    baseline = operator.post('/monitor/comparisons', json=body)
    assert baseline.status_code == 200
    assert baseline.json()['items'][0]['original']['costs']['complete_api_usd'] == '0.01'
    service = tracer.controlled.host.service
    original = service.contracts['health-backup-v1']
    runner = original.runner
    different = BudgetStore(tracer.controlled.host.root / 'different-empty-account.sqlite')
    service.contracts['health-backup-v1'] = replace(original, runner=BudgetedCore(runner.core, different,
        model_bounds=runner.model_bounds, tool_bounds=runner.tool_bounds, verify_bound=runner.verify_bound))
    before = tracer.snapshot('before-reused-contract-comparison')
    response = operator.post('/monitor/comparisons', json=body)
    assert response.status_code == 200
    item = response.json()['items'][0]
    assert item['reproduction']['status'] == 'not_evaluable'
    assert item['original']['costs']['complete_api_usd'] is None
    assert item['original']['costs']['known_api_usd'] is None
    assert not any(row['kind'] == 'original_budget_events' for row in item['evidence'])
    after = tracer.snapshot('after-reused-contract-comparison')
    for key in ('model_requests', 'source_requests', 'accounting_requests', 'health_sha256',
                'budget_sqlite_sha256', 'capacity_sha256', 'queue_sha256'):
        assert before[key] == after[key]

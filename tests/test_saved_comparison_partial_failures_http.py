"""#45：局部缺損、建立失敗與重跑仍可查核；經真實主服務 HTTP，上游僅 MockTransport。"""
from pathlib import Path
import sqlite3

from task_features_fixture import task_features_http as task_features_http
from test_local_http_integration import loopback_network as loopback_network
from test_saved_comparisons_http import _completed

from cloudyfan_monitor.saved_comparisons import _task_id


def _reports_root(tracer):
    return Path(tracer.controlled.app.store.path).parent / '.saved-comparisons'


def _readonly(tracer, before, after):
    for key in ('model_requests', 'source_requests', 'accounting_requests', 'health_sha256',
                'budget_sqlite_sha256', 'capacity_sha256', 'queue_sha256'):
        assert before[key] == after[key]


def test_one_task_over_its_row_limit_does_not_turn_later_tasks_unknown(task_features_http):
    tracer = task_features_http
    operator, first = _completed(tracer, run='rows-over-limit')
    _, second = _completed(tracer, run='rows-within-limit')
    internal = _task_id('agent', 'rows-over-limit')
    stores = [store.path for store in tracer.controlled.budgets.values()]
    inserted = 0
    for path in stores:
        with sqlite3.connect(path) as db:
            if db.execute('SELECT COUNT(*) FROM events WHERE run=?', (internal,)).fetchone()[0]:
                db.executemany('INSERT INTO events (id, run, kind, cost) VALUES (?, ?, ?, ?)',
                               [(f'synthetic-extra-{index}', internal, 'generation', '0') for index in range(70)])
                inserted += 1
    assert inserted == 1, 'synthetic over-limit fixture did not find the original budget store'
    before = tracer.snapshot('before-row-limit-comparison')
    response = operator.post('/monitor/comparisons', json={
        'policy_version': 'original-policy-reproduction-v1', 'tasks': first['tasks'] + second['tasks']})
    assert response.status_code == 200
    report = response.json()
    rows = {item['task']['run_id']: item for item in report['items']}
    assert report['status'] == 'completed', 'A per-task row cap must not mark the whole report incomplete'
    assert rows['rows-over-limit']['reproduction']['reasons'] == ['original_rows_over_limit']
    assert rows['rows-within-limit']['reproduction']['status'] == 'reproduced'
    _readonly(tracer, before, tracer.snapshot('after-row-limit-comparison'))


def test_one_damaged_report_does_not_hide_the_rest_of_the_catalogue(task_features_http):
    tracer = task_features_http
    operator, body = _completed(tracer)
    damaged = operator.post('/monitor/comparisons', json=body).json()['comparison_id']
    intact = operator.post('/monitor/comparisons', json=body).json()['comparison_id']
    path = _reports_root(tracer) / (damaged + '.report.json')
    path.write_bytes(path.read_bytes().replace(b'"completed"', b'"COMPLETED"', 1))
    catalog = operator.get('/monitor/comparisons')
    assert catalog.status_code == 200
    statuses = {row['comparison_id']: row['status'] for row in catalog.json()['reports']}
    assert statuses == {damaged: 'unavailable', intact: 'completed'}
    assert operator.get('/monitor/comparisons/' + damaged).status_code == 503
    assert operator.get('/monitor/comparisons/' + intact).status_code == 200


def test_failed_creation_is_recorded_rather_than_pending_forever(task_features_http, monkeypatch):
    from cloudyfan_monitor import saved_comparisons
    tracer = task_features_http
    operator, body = _completed(tracer)

    def unavailable(*args, **kwargs):
        raise saved_comparisons.SourceUnavailable('synthetic_context_failure')

    monkeypatch.setattr(saved_comparisons, '_source_context', unavailable)
    assert operator.post('/monitor/comparisons', json=body).status_code == 503
    monkeypatch.undo()
    rows = operator.get('/monitor/comparisons').json()['reports']
    assert [row['status'] for row in rows] == ['failed']
    response = operator.get('/monitor/comparisons/' + rows[0]['comparison_id'])
    assert response.status_code == 409 and response.json()['error']['code'] == 'comparison_creation_failed'
    # A later explicit comparison still works and does not overwrite the failure.
    assert operator.post('/monitor/comparisons', json=body).status_code == 200
    statuses = sorted(row['status'] for row in operator.get('/monitor/comparisons').json()['reports'])
    assert statuses == ['completed', 'failed']


def test_rerun_gets_a_new_id_and_references_the_unchanged_original(task_features_http):
    tracer = task_features_http
    operator, body = _completed(tracer)
    original = operator.post('/monitor/comparisons', json=body)
    first = original.json()['comparison_id']
    rerun = operator.post('/monitor/comparisons', json={**body, 'rerun_of': first})
    assert rerun.status_code == 200
    report = rerun.json()
    assert report['comparison_id'] != first and report['scope']['rerun_of'] == first
    assert operator.get('/monitor/comparisons/' + first).content == original.content
    other = {**body, 'tasks': [{'client': 'agent', 'run_id': 'not-the-same-scope'}], 'rerun_of': first}
    assert operator.post('/monitor/comparisons', json=other).status_code == 422
    missing = {**body, 'rerun_of': 'cmp-' + '0' * 32}
    assert operator.post('/monitor/comparisons', json=missing).status_code == 409
    assert operator.post('/monitor/comparisons', json={**body, 'rerun_of': '../x'}).status_code == 422

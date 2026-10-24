"""Retention contention stays observable through the public guard and HTTP seams."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from cloudyfan_jev.retention import RetentionError, RetentionPolicy, RetentionStore
from test_engine import API
from test_http_api import TASK
from test_retention import retained_service


@pytest.mark.parametrize('same_task, expected', [(True, 'task_busy'), (False, 'entered')])
def test_retention_guard_reentry_keeps_competing_threads_nonblocking(tmp_path, same_task, expected):
    store = RetentionStore(tmp_path / 'retention', RetentionPolicy('monitor-v1', 60))
    task = 'http-' + 'a' * 48
    competitor = task if same_task else 'http-' + 'b' * 48

    def enter(candidate):
        try:
            with store.guard(candidate):
                return 'entered'
        except RetentionError as error:
            assert error.status == 409
            return error.code

    with ThreadPoolExecutor(max_workers=1) as workers:
        with store.guard(task):
            # TaskService.execute calls retained status in the same thread.
            with store.guard(task):
                assert workers.submit(enter, competitor).result(timeout=1) == expected
        assert workers.submit(enter, task).result(timeout=1) == 'entered'


def test_running_retained_task_returns_busy_without_waiting_or_redispatch(tmp_path):
    entered, release = Event(), Event()
    upstream = API(first='2')
    dispatched = []

    def endpoint(request):
        dispatched.append(request.method)
        entered.set()
        assert release.wait(5), 'Controlled mock upstream was not released'
        return upstream(request)

    client, _, _, _, _ = retained_service(tmp_path, api=endpoint)
    with ThreadPoolExecutor(max_workers=2) as workers:
        running = workers.submit(client.post, '/v1/tasks', json=TASK)
        try:
            assert entered.wait(2), 'Task did not reach the mock upstream'
            for method, path, body in [
                ('GET', '/v1/tasks/task-1', None),
                ('GET', '/v1/tasks/task-1/result', None),
                ('POST', '/v1/tasks/task-1/resume', {}),
            ]:
                response = workers.submit(client.request, method, path, json=body).result(timeout=1)
                assert response.status_code == 409
                assert response.json() == {'error': {'code': 'task_busy'}}
            assert dispatched == ['POST']
        finally:
            release.set()
        assert running.result(timeout=2).status_code == 200
    assert client.get('/v1/tasks/task-1').json()['status'] == 'passed'
    assert dispatched == ['POST']

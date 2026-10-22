from copy import deepcopy
import json

import httpx
import pytest

from cloudyfan_monitor.pools import OpenRouterPoolSource
from cloudyfan_monitor.store import MonitorError
from cloudyfan_jev.scores.categories import CATEGORY_SPECS
from test_sources import NOW, SYNTHETIC_SOURCE_CREDENTIAL, fixtures


def controlled_source(endpoint_failure=None, *, endpoint_limit=3):
    models, benchmarks, policies = fixtures()
    observed = []

    def respond(request):
        observed.append((request.method, str(request.url)))
        if request.url.path.endswith('/models'):
            return httpx.Response(200, json={'data': models})
        if request.url.path.endswith('/benchmarks'):
            return httpx.Response(200, json=benchmarks if not request.url.query else
                                  {'meta': {'version': 'v1'}, 'data': []})
        model = request.url.path.removeprefix('/api/v1/models/').removesuffix('/endpoints')
        if model == 'example/a' and endpoint_failure:
            return endpoint_failure(request)
        return httpx.Response(200, json={'data': {'id': model, 'endpoints': [{
            'tag': 'provider/one', 'model_id': model, 'pricing': {'prompt': '0', 'completion': '0'}
        }]}})

    source = OpenRouterPoolSource(httpx.Client(transport=httpx.MockTransport(respond)), policies,
                                 api_key=SYNTHETIC_SOURCE_CREDENTIAL, endpoint_limit=endpoint_limit,
                                 clock=lambda: NOW)
    return source, observed


def test_single_endpoint_timeout_preserves_other_candidates_and_reports_partial():
    def timeout(request):
        raise httpx.ReadTimeout(SYNTHETIC_SOURCE_CREDENTIAL, request=request)

    source, observed = controlled_source(timeout)
    view = source.refresh()

    assert view['completeness']['status'] == 'partial'
    assert view['completeness']['attempted'] == 6
    assert view['completeness']['succeeded'] == 5
    assert view['completeness']['failed'] == 1
    assert view['completeness']['deferred'] == 0
    assert view['completeness']['failures'] == [{
        'model': 'example/a', 'url': 'https://openrouter.ai/api/v1/models/example/a/endpoints',
        'code': 'source_timeout'}]
    coding = view['categories']['artificial-analysis:coding_index']['pools']
    assert coding['low'][0]['profile']['provider'] is None
    assert coding['mid'][0]['profile']['provider'] == 'provider/one'
    assert len(observed) == 6
    assert SYNTHETIC_SOURCE_CREDENTIAL not in json.dumps(view)


@pytest.mark.parametrize('failure', [
    lambda request: httpx.Response(200, content=b'not-json'),
    lambda request: httpx.Response(200, json={'data': {'endpoints': []}}),
    lambda request: httpx.Response(200, json={'data': {'id': 'example/wrong', 'endpoints': []}}),
])
def test_endpoint_json_schema_and_identity_failures_are_explicit_partial_candidates(failure):
    source, observed = controlled_source(failure)
    view = source.refresh()
    assert view['completeness']['failed'] == 1
    assert view['completeness']['endpoint_succeeded'] == 2
    assert view['completeness']['failures'][0]['code'] == 'source_schema_invalid'
    assert len(observed) == 6
    coding = view['categories']['artificial-analysis:coding_index']['pools']
    assert coding['low'][0]['profile']['provider'] is None
    assert coding['high'][0]['profile']['provider'] == 'provider/one'


@pytest.mark.parametrize('path, query', [('/models', False), ('/benchmarks', False), ('/benchmarks', True)])
def test_each_mandatory_source_schema_failure_aborts_and_retains_safe_attempt_evidence(path, query):
    models, benchmarks, policies = fixtures()
    observed = []

    def respond(request):
        observed.append(request)
        if request.url.path.endswith(path) and bool(request.url.query) == query:
            return httpx.Response(200, json={'data': None})
        if request.url.path.endswith('/models'):
            return httpx.Response(200, json={'data': models})
        return httpx.Response(200, json=benchmarks)

    source = OpenRouterPoolSource(httpx.Client(transport=httpx.MockTransport(respond)), policies,
                                 api_key=SYNTHETIC_SOURCE_CREDENTIAL, clock=lambda: NOW)
    with pytest.raises(MonitorError, match='source_schema_invalid'):
        source.refresh()
    assert source.last_completeness['status'] == 'failed'
    assert source.last_completeness['failed'] == 1
    assert source.last_completeness['endpoint_attempted'] == 0
    assert len(observed) in (1, 2, 3)
    assert SYNTHETIC_SOURCE_CREDENTIAL not in json.dumps(source.last_completeness)


def test_deferred_models_are_not_reported_as_successful_source_updates():
    source, observed = controlled_source(endpoint_limit=1)
    view = source.refresh()
    assert view['completeness']['status'] == 'partial'
    assert view['completeness']['attempted'] == 4
    assert view['completeness']['succeeded'] == 4
    assert view['completeness']['failed'] == 0
    assert view['completeness']['deferred_models'] == ['example/b', 'example/c']
    assert len(observed) == 4


def test_failed_endpoint_attempts_count_towards_fifteen_get_limit():
    originals, benchmarks, policies = fixtures()
    models, rows, observed = [], [], []
    for index, spec in enumerate(list(CATEGORY_SPECS.values())[:14]):
        model = deepcopy(originals[0])
        model['id'] = model['canonical_slug'] = f'example/model-{index:02}'
        models.append(model)
        row = deepcopy(next(row for row in benchmarks['data']
                            if row['source'] == spec['source'] and all(
                                row.get(field) == spec[field]
                                for field in ('arena', 'category', 'benchmark_type') if field in spec)))
        row['model_permaslug'] = model['id']
        rows.append(row)

    def respond(request):
        observed.append(request)
        if request.url.path.endswith('/models'):
            return httpx.Response(200, json={'data': models})
        if request.url.path.endswith('/benchmarks'):
            return httpx.Response(200, json={'meta': {'version': 'v1', 'as_of': NOW},
                                           'data': [] if request.url.query else rows})
        return httpx.Response(503)

    source = OpenRouterPoolSource(httpx.Client(transport=httpx.MockTransport(respond)), policies,
                                 clock=lambda: NOW)
    view = source.refresh()
    assert len(observed) == 15
    assert view['completeness']['attempted'] == 15
    assert view['completeness']['succeeded'] == 3
    assert view['completeness']['failed'] == 12
    assert view['completeness']['deferred'] == 2
    assert all(request.method == 'GET' for request in observed)
    assert all(max(request.extensions['timeout'].values()) <= 15 for request in observed)


def test_whole_round_deadline_stops_new_endpoint_requests_and_reports_unqueried(monkeypatch):
    monotonic = [0]
    monkeypatch.setattr('cloudyfan_monitor.pools.time.monotonic', lambda: monotonic[0])

    def deadline(request):
        monotonic[0] = 61
        return httpx.Response(200, json={'data': {'id': 'example/a', 'endpoints': []}})

    source, observed = controlled_source(deadline)
    view = source.refresh()
    assert len(observed) == 4
    assert view['completeness']['attempted'] == 4
    assert view['completeness']['failed'] == 1
    assert view['completeness']['failures'][0]['code'] == 'source_refresh_deadline'
    assert view['completeness']['deferred_models'] == ['example/b', 'example/c']


def test_source_does_not_accept_an_endpoint_limit_above_twelve():
    _, _, policies = fixtures()
    with pytest.raises(ValueError, match='bounded'):
        OpenRouterPoolSource(httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200))),
                             policies, endpoint_limit=13)


def test_response_arriving_after_single_request_deadline_is_not_accepted(monkeypatch):
    monotonic = [0]
    monkeypatch.setattr('cloudyfan_monitor.pools.time.monotonic', lambda: monotonic[0])

    def late_response(request):
        monotonic[0] = 16
        return httpx.Response(200, json={'data': {'id': 'example/a', 'endpoints': []}})

    source, observed = controlled_source(late_response)
    view = source.refresh()
    assert view['completeness']['failed'] == 1
    assert view['completeness']['failures'][0]['code'] == 'source_request_deadline'
    assert view['completeness']['succeeded'] == 5
    assert len(observed) == 6

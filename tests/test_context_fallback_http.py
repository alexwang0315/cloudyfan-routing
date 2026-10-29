"""#51：準備逾時、無縮減或超限時，付費前回退原文或安全拒絕；真實主服務 HTTP，上游僅 MockTransport。"""
import pytest

from cloudyfan_jev import context_preparation
from test_context_preparation_http import CONTEXT, POLICY, _enable, _post, _user
from test_local_http_integration import loopback_network as loopback_network
from test_saved_paired_outcomes_http import paired_http as paired_http


SMALL = {**CONTEXT, 'blocks': [CONTEXT['blocks'][0], {**CONTEXT['blocks'][2]}]}


def _slow(monkeypatch, elapsed_ms=50):
    values = iter([0.0, elapsed_ms / 1000])
    monkeypatch.setattr(context_preparation, '_now', lambda: next(values, 0.0))


def _detail(tracer, run):
    return tracer.client().get(f'/monitor/context-preparations/studio/{run}').json()


def test_timeout_falls_back_to_the_full_original_once_when_allowed_and_it_fits(paired_http, monkeypatch):
    _enable(paired_http, {**POLICY, 'fallback': 'original_if_fits', 'max_prepare_ms': 5})
    _slow(monkeypatch)
    response = _post(paired_http, 'fallback-fits', context=SMALL)
    assert response.status_code == 200 and response.json()['status'] == 'passed', response.text
    generations = [call for call in paired_http.bundle.upstream.calls if 'messages' in call]
    assert len(generations) == 1, 'Fallback must be sent exactly once'
    sent = _user(generations[0])
    assert '必要說明' in sent and 'def total' in sent
    prepared = _detail(paired_http, 'fallback-fits')['preparation']
    assert (prepared['outcome'], prepared['outcome_reason']) == ('fallback_original', 'preparation_timeout')
    assert prepared['sent_bytes'] == prepared['original_bytes'] and prepared['sent_matches_prepared'] is True
    assert prepared['token_estimate'] is None and prepared['token_method'] == 'unavailable'
    assert prepared['provider_prompt_tokens'] == 20 and prepared['generation_requests'] == 1


@pytest.mark.parametrize('policy,context,code', [
    ({'max_prepare_ms': 5}, SMALL, 'context_preparation_timeout'),
    ({'max_prepare_ms': 5, 'fallback': 'original_if_fits'}, CONTEXT, 'preparation_unavailable_original_over_limit'),
    ({'framing_reserve_bytes': 400, 'output_reserve_bytes': 400}, SMALL, 'output_reserve_exceeds_context'),
    ({'token_guarantee': 'required'}, SMALL, 'token_method_untrusted'),
], ids=['timeout-no-fallback', 'original-too-big', 'reserve-exceeds', 'token-untrusted'])
def test_unsafe_cases_are_rejected_with_zero_posts_and_a_saved_safe_reason(paired_http, monkeypatch, policy, context, code):
    _enable(paired_http, {**POLICY, **policy})
    _slow(monkeypatch)
    response = _post(paired_http, 'fallback-rejected', context=context)
    assert response.json()['error']['code'] == code
    assert paired_http.bundle.upstream.calls == []
    value = _detail(paired_http, 'fallback-rejected')
    assert value['status'] == 'rejected_before_dispatch' and [row['code'] for row in value['rejections']] == [code]
    assert '必要說明' not in str(value) and 'PRIVATE_PAIRED_CANARY' not in str(value)


def test_no_reduction_is_explained_and_the_full_original_is_sent(paired_http):
    _enable(paired_http)
    assert _post(paired_http, 'no-reduction', context=SMALL).status_code == 200
    prepared = _detail(paired_http, 'no-reduction')['preparation']
    assert (prepared['outcome'], prepared['outcome_reason']) == ('no_reduction', 'all_blocks_fit')
    assert prepared['sent_bytes'] == prepared['original_bytes']


def test_insufficient_budget_guarantee_stops_before_any_post_and_keeps_the_prepared_record(paired_http):
    _enable(paired_http)
    store = paired_http.bundle.app.gateway.service.contracts['demo-open_source_first'].runner.store
    import sqlite3
    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE authorization SET total='0.00000001'")
    response = _post(paired_http, 'budget-short')
    assert response.status_code >= 400
    assert [call for call in paired_http.bundle.upstream.calls if 'messages' in call] == []
    value = _detail(paired_http, 'budget-short')
    # 準備已在付費前完成並凍結；是預算閘門（不是準備）阻止派送。
    assert value['status'] == 'prepared' and value['preparation']['generation_requests'] == 0

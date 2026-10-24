"""OpenRouter accounting is exercised only at its read-only HTTP boundary."""
import hashlib

import httpx
import pytest
import sys
import time

from cloudyfan_monitor.openrouter_accounting import AccountingError, OpenRouterAccounting


def test_generation_uses_actual_total_cost_and_native_token_counts():
    def upstream(request):
        assert request.method == "GET"
        assert str(request.url) == "https://openrouter.ai/api/v1/generation?id=gen-one"
        assert request.headers["Authorization"] == "Bearer mock-key"
        return httpx.Response(200, json={"data": {
            "id": "gen-one", "model": "vendor/model", "total_cost": 0.0015,
            "usage": 999, "upstream_inference_cost": 888,
            "native_tokens_prompt": 120, "native_tokens_completion": 35,
            "tokens_prompt": 200, "tokens_completion": 100,
        }})

    with httpx.Client(transport=httpx.MockTransport(upstream)) as http:
        result = OpenRouterAccounting(http, api_key="mock-key").generation(
            "gen-one", expected_model="vendor/model")
    assert {key: result[key] for key in (
        "provider", "generation_id", "model", "cost_usd", "input_tokens", "output_tokens", "source"
    )} == {
        "provider": "openrouter", "generation_id": "gen-one", "model": "vendor/model",
        "cost_usd": "0.0015", "input_tokens": 120, "output_tokens": 35,
        "source": "openrouter_generation_api",
    }


def test_account_reports_key_usage_separately_from_management_account_totals():
    def upstream(request):
        assert request.method == "GET"
        if request.url.path == "/api/v1/key":
            assert request.headers["Authorization"] == "Bearer mock-key"
            return httpx.Response(200, json={"data": {
                "usage": 1.23, "limit": 10, "limit_remaining": 8.77,
                "byok_usage": 0.4, "include_byok_in_limit": True,
            }})
        assert str(request.url) == "https://openrouter.ai/api/v1/credits"
        assert request.headers["Authorization"] == "Bearer mgmt-key"
        return httpx.Response(200, json={"data": {"total_credits": 100.5, "total_usage": 25.75}})

    with httpx.Client(transport=httpx.MockTransport(upstream)) as http:
        result = OpenRouterAccounting(http, api_key="mock-key", management_key="mgmt-key").account()
    assert result["status"] == "available"
    assert {name: result["key"][name] for name in (
        "scope", "usage_usd", "limit_usd", "limit_remaining_usd", "byok_usage_usd", "include_byok_in_limit"
    )} == {"scope": "key", "usage_usd": "1.23", "limit_usd": "10", "limit_remaining_usd": "8.77",
          "byok_usage_usd": "0.4", "include_byok_in_limit": True}
    assert {name: result["account"][name] for name in (
        "scope", "total_credits_usd", "total_usage_usd"
    )} == {"scope": "account", "total_credits_usd": "100.5", "total_usage_usd": "25.75"}


@pytest.mark.parametrize("bad_cost", [None, True, -1, "NaN", "Infinity", " 0.1 ", "1_000", "+1", "1e1000", "1e-1000"])
def test_invalid_upstream_money_never_becomes_a_billable_zero_or_formatted_number(bad_cost):
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"data": {
        "id": "gen-one", "model": "vendor/model", "total_cost": bad_cost,
    }}))) as http:
        with pytest.raises(AccountingError, match="^openrouter_accounting_schema_invalid$"):
            OpenRouterAccounting(http, api_key="mock-key").generation("gen-one", expected_model="vendor/model")


@pytest.mark.parametrize("credential", ["é", "bad\x00key", "bad\nkey", "", None])
def test_invalid_credentials_fail_with_safe_configuration_error_before_any_request(credential):
    def forbidden(request):
        pytest.fail("Invalid credentials must not be sent")
    with httpx.Client(transport=httpx.MockTransport(forbidden)) as http:
        with pytest.raises(ValueError, match="^Explicit HTTP client and accounting credential required$"):
            OpenRouterAccounting(http, api_key=credential)


def test_slow_response_cannot_keep_extending_accounting_deadline():
    class Slow(httpx.SyncByteStream):
        def __iter__(self):
            yield b'{"data":'
            time.sleep(0.02)
            yield b'{"id":"gen-one","model":"vendor/model","total_cost":0.1}}'

    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=Slow()))) as http:
        with pytest.raises(AccountingError, match="^openrouter_accounting_timeout$") as error:
            OpenRouterAccounting(http, api_key="mock-key", response_deadline=0.005).generation(
                "gen-one", expected_model="vendor/model")
    assert error.value.status == 504


def test_deeply_nested_json_is_a_safe_unavailable_schema_instead_of_a_parser_crash():
    depth = sys.getrecursionlimit() + 10
    malformed = b'{"data":' + b'[' * depth + b'0' + b']' * depth + b'}'
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=malformed))) as http:
        with pytest.raises(AccountingError, match="^openrouter_accounting_schema_invalid$"):
            OpenRouterAccounting(http, api_key="mock-key").generation("gen-one", expected_model="vendor/model")


def test_native_usage_can_remain_unknown_while_precise_actual_cost_has_a_content_fingerprint():
    raw = b'{"data":{"id":"gen-one","model":"vendor/model","total_cost":0.000000000123456789,"native_tokens_prompt":null,"native_tokens_completion":null}}'
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=raw))) as http:
        result = OpenRouterAccounting(http, api_key="mock-key").generation("gen-one", expected_model="vendor/model")
    assert result["cost_usd"] == "0.000000000123456789"
    assert result["input_tokens"] is result["output_tokens"] is None
    assert result["evidence_sha256"] == "384e8f0f478b5c4ee952438f0d3896443862cf07b9bf878adc85232e2a1b90d6"
    assert result["observed_at"].endswith("+00:00")
    assert len(result["account_ref"]) == 64


@pytest.mark.parametrize("change", [{"id": "gen-other"}, {"model": "vendor/other"}])
def test_generation_identity_mismatch_cannot_be_charged_to_the_requested_model_or_client(change):
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"data": {
        "id": "gen-one", "model": "vendor/model", "total_cost": 0.1, **change,
    }}))) as http:
        with pytest.raises(AccountingError, match="^openrouter_accounting_identity_mismatch$") as error:
            OpenRouterAccounting(http, api_key="mock-key").generation("gen-one", expected_model="vendor/model")
    assert error.value.status == 409


@pytest.mark.parametrize("bad_tokens", [True, -1, "100", 1.5, 10**12 + 1])
def test_invalid_native_token_counts_never_use_other_token_counts_as_a_fallback(bad_tokens):
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"data": {
        "id": "gen-one", "model": "vendor/model", "total_cost": 0,
        "native_tokens_prompt": bad_tokens, "tokens_prompt": 100,
    }}))) as http:
        with pytest.raises(AccountingError, match="^openrouter_accounting_schema_invalid$"):
            OpenRouterAccounting(http, api_key="mock-key").generation("gen-one", expected_model="vendor/model")


@pytest.mark.parametrize("status,code", [(302, "http_failed"), (401, "unauthorized"),
    (403, "forbidden"), (404, "not_found"), (429, "rate_limited"), (503, "http_failed")])
def test_unsuccessful_requests_are_not_retried_redirected_or_reported_with_server_body(status, code):
    requests = []
    def upstream(request):
        requests.append(request)
        assert request.url.host == "openrouter.ai"
        return httpx.Response(status, headers={"Location": "https://untrusted.invalid/"},
                              text="private-upstream-error")
    with httpx.Client(transport=httpx.MockTransport(upstream), follow_redirects=True) as http:
        with pytest.raises(AccountingError) as error:
            OpenRouterAccounting(http, api_key="mock-key").generation("gen-one", expected_model="vendor/model")
    assert error.value.code == "openrouter_accounting_" + code
    assert "private-upstream-error" not in str(error.value)
    assert "mock-key" not in str(error.value)
    assert len(requests) == 1
    assert requests[0].extensions["timeout"] == {"connect": 5, "read": 10, "write": 10, "pool": 10}


def test_network_timeout_is_safe_and_never_retried():
    requests = []
    def upstream(request):
        requests.append(request)
        raise httpx.ReadTimeout("private-upstream-error", request=request)
    with httpx.Client(transport=httpx.MockTransport(upstream)) as http:
        with pytest.raises(AccountingError, match="^openrouter_accounting_timeout$") as error:
            OpenRouterAccounting(http, api_key="mock-key").generation("gen-one", expected_model="vendor/model")
    assert len(requests) == 1 and error.value.status == 504


@pytest.mark.parametrize("raw", [b'{"data":{"total_cost":0,"total_cost":1}}', b'{', b'{"data":null}', b'{"data":{"value":NaN}}'])
def test_invalid_json_evidence_is_unavailable(raw):
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=raw))) as http:
        with pytest.raises(AccountingError, match="^openrouter_accounting_schema_invalid$"):
            OpenRouterAccounting(http, api_key="mock-key").generation("gen-one", expected_model="vendor/model")


def test_excessive_response_body_is_rejected():
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"x" * 262145))) as http:
        with pytest.raises(AccountingError, match="^openrouter_accounting_response_too_large$"):
            OpenRouterAccounting(http, api_key="mock-key").generation("gen-one", expected_model="vendor/model")


def test_regular_key_can_report_its_scope_without_requesting_management_account_data():
    def upstream(request):
        assert str(request.url) == "https://openrouter.ai/api/v1/key"
        return httpx.Response(200, json={"data": {"usage": 0, "limit": None}})
    with httpx.Client(transport=httpx.MockTransport(upstream)) as http:
        result = OpenRouterAccounting(http, api_key="mock-key").account()
    assert result["status"] == "available"
    assert result["key"]["usage_usd"] == "0"
    assert result["key"]["limit_usd"] is result["key"]["byok_usage_usd"] is None
    assert result["account"] == {"scope": "account", "status": "not_configured"}


@pytest.mark.parametrize("denied_scope", ["key", "account"])
def test_denied_scope_preserves_successful_other_scope_without_inventing_account_balance(denied_scope):
    def upstream(request):
        scope = "key" if request.url.path.endswith("/key") else "account"
        if scope == denied_scope:
            return httpx.Response(403, json={"error": {"message": "private-upstream-error"}})
        return httpx.Response(200, json={"data": {"usage": 1.25, "total_credits": 10, "total_usage": 2}})
    with httpx.Client(transport=httpx.MockTransport(upstream)) as http:
        result = OpenRouterAccounting(http, api_key="mock-key", management_key="mgmt-key").account()
    assert result["status"] == "partial"
    assert result[denied_scope] == {"scope": denied_scope, "status": "unavailable",
                                   "error": {"code": "openrouter_accounting_forbidden"}}
    assert result["account" if denied_scope == "key" else "key"]["status"] == "available"
    assert "private-upstream-error" not in str(result)
    assert "mock-key" not in str(result) and "mgmt-key" not in str(result)


def test_generation_time_budget_shortens_one_request_without_changing_later_request_deadlines():
    class Slow(httpx.SyncByteStream):
        def __iter__(self):
            yield b'{"data":'
            time.sleep(0.02)
            yield b'{"id":"gen-one","model":"vendor/model","total_cost":0.1}}'

    requests = []
    def upstream(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(200, stream=Slow())
        return httpx.Response(200, json={"data": {"id": "gen-one", "model": "vendor/model", "total_cost": 0.1}})
    with httpx.Client(transport=httpx.MockTransport(upstream)) as http:
        source = OpenRouterAccounting(http, api_key="mock-key")
        with pytest.raises(AccountingError, match="^openrouter_accounting_timeout$"):
            source.generation("gen-one", expected_model="vendor/model", time_budget=0.005)
        assert source.generation("gen-one", expected_model="vendor/model")["cost_usd"] == "0.1"
    assert requests[0].extensions["timeout"] == {"connect": 0.005, "read": 0.005, "write": 0.005, "pool": 0.005}
    assert requests[1].extensions["timeout"] == {"connect": 5, "read": 10, "write": 10, "pool": 10}


@pytest.mark.parametrize("budget", [0, -1, True, "5", float("nan"), float("inf")])
def test_invalid_per_request_time_budget_fails_before_any_request(budget):
    def forbidden(request):
        pytest.fail("Invalid accounting time budget must not issue an HTTP request")
    with httpx.Client(transport=httpx.MockTransport(forbidden)) as http:
        with pytest.raises(AccountingError, match="^openrouter_accounting_time_budget_invalid$") as error:
            OpenRouterAccounting(http, api_key="mock-key").generation(
                "gen-one", expected_model="vendor/model", time_budget=budget)
    assert error.value.status == 400


def test_caller_cannot_extend_the_configured_deadline_with_a_larger_time_budget():
    def upstream(request):
        assert request.extensions["timeout"] == {"connect": 2, "read": 2, "write": 2, "pool": 2}
        return httpx.Response(200, json={"data": {"id": "gen-one", "model": "vendor/model", "total_cost": 0}})
    with httpx.Client(transport=httpx.MockTransport(upstream)) as http:
        result = OpenRouterAccounting(http, api_key="mock-key", response_deadline=2).generation(
            "gen-one", expected_model="vendor/model", time_budget=50)
    assert result["cost_usd"] == "0"


def test_original_utf8_evidence_is_preserved_exactly_for_generation_and_each_successful_account_scope():
    bodies = {
        "/api/v1/generation": '{\n  "data": {"id": "gen-one", "model": "vendor/model", "total_cost": 0.100000, "note": "帳務"}\n}\n'.encode(),
        "/api/v1/key": b' { "data": {"usage": 1.2500, "limit": null, "include_byok_in_limit": false} } ',
        "/api/v1/credits": b'{"data":\n{"total_credits": 10.0000, "total_usage": 2.5000}}\n',
    }
    requests = []
    def upstream(request):
        requests.append((request.method, request.url.path))
        return httpx.Response(200, content=bodies[request.url.path])
    with httpx.Client(transport=httpx.MockTransport(upstream)) as http:
        source = OpenRouterAccounting(http, api_key="mock-key", management_key="mgmt-key")
        generation = source.generation("gen-one", expected_model="vendor/model")
        account = source.account()
    results = [generation, account["key"], account["account"]]
    for result, path in zip(results, bodies):
        evidence = result["evidence_json"].encode("utf-8")
        assert evidence == bodies[path]
        assert hashlib.sha256(evidence).hexdigest() == result["evidence_sha256"]
    assert requests == [("GET", path) for path in bodies]


def test_tiny_slow_body_chunks_do_not_buffer_past_deadline_before_checking():
    body = b'{"data":{"id":"gen-one","model":"vendor/model","total_cost":0.1}}'
    emitted, requests = [], []
    class SlowTinyChunks(httpx.SyncByteStream):
        closed = False
        def __iter__(self):
            for offset in range(0, len(body), 4):
                time.sleep(0.01)
                emitted.append(offset)
                yield body[offset:offset + 4]
        def close(self):
            self.closed = True
    stream = SlowTinyChunks()
    def upstream(request):
        requests.append(request)
        return httpx.Response(200, stream=stream)
    with httpx.Client(transport=httpx.MockTransport(upstream)) as http:
        with pytest.raises(AccountingError, match="^openrouter_accounting_timeout$") as caught:
            OpenRouterAccounting(http, api_key="mock-key", response_deadline=0.02).generation(
                "gen-one", expected_model="vendor/model")
    assert caught.value.status == 504
    assert len(emitted) <= 3, "The deadline must be checked before consuming the entire tiny-chunk body"
    assert stream.closed is True and len(requests) == 1


@pytest.mark.parametrize("reset", ["daily", "weekly", "monthly", None])
def test_key_limit_period_is_separate_from_cumulative_and_period_usage(reset):
    def upstream(request):
        assert request.url.path == "/api/v1/key"
        return httpx.Response(200, json={"data": {"usage": 10.5, "limit": 5,
            "limit_remaining": 4.9, "limit_reset": reset,
            "usage_daily": 0.1, "usage_weekly": 1.20, "usage_monthly": 3.400}})
    with httpx.Client(transport=httpx.MockTransport(upstream)) as http:
        result = OpenRouterAccounting(http, api_key="mock-key").account()
    assert result["key"]["limit_reset"] == reset
    assert {name: result["key"][name] for name in (
        "usage_usd", "usage_daily_usd", "usage_weekly_usd", "usage_monthly_usd",
        "limit_usd", "limit_remaining_usd"
    )} == {"usage_usd": "10.5", "usage_daily_usd": "0.1", "usage_weekly_usd": "1.2",
          "usage_monthly_usd": "3.4", "limit_usd": "5", "limit_remaining_usd": "4.9"}


@pytest.mark.parametrize("extra", [{}, {"limit_reset": "yearly", "usage_daily": None}])
def test_unknown_limit_period_and_missing_period_usage_remain_unknown(extra):
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(
            200, json={"data": {"usage": 1, **extra}}))) as http:
        result = OpenRouterAccounting(http, api_key="mock-key").account()
    assert result["key"]["limit_reset"] is None
    assert all(result["key"][name] is None for name in
               ("usage_daily_usd", "usage_weekly_usd", "usage_monthly_usd"))
    assert result["key"]["usage_usd"] == "1"


@pytest.mark.parametrize("name,value", [("usage_daily", -1), ("usage_weekly", True), ("usage_monthly", "NaN")])
def test_period_usage_has_the_same_safe_money_validation_as_cumulative_usage(name, value):
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(
            200, json={"data": {"usage": 1, name: value}}))) as http:
        result = OpenRouterAccounting(http, api_key="mock-key").account()
    assert result["key"] == {"scope": "key", "status": "unavailable",
                             "error": {"code": "openrouter_accounting_schema_invalid"}}

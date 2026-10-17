"""Mock HTTP only: at-most-once dispatch and authoritative complete billing."""
import json

import httpx
import pytest


from cloudyfan_jev import client
KEY = "mock-credential-only"


def body(gid="gen-one", cost=.01):
    result = {"id": gid, "model": "vendor/model", "provider": "provider",
              "usage": {"cost": cost, "prompt_tokens": 5, "completion_tokens": 4}}
    return result


def make(tmp_path, handler):
    http = httpx.Client(transport=httpx.MockTransport(handler))
    return client.Client(http, KEY, tmp_path), http


def test_paid_response_is_saved_and_same_hash_never_resends(tmp_path):
    calls = []
    def handler(req):
        calls.append(req)
        assert (tmp_path / "calls/a/intent.json").exists()
        return httpx.Response(200, json=body())
    api, http = make(tmp_path, handler)
    with http:
        first = api.call("a", client.CHAT, {"model": "vendor/model"})
        second = api.call("a", client.CHAT, {"model": "vendor/model"})
    assert first == second and len(calls) == 1
    assert (tmp_path / "calls/a/response.json").exists()
    assert client.usage(first)["cost_usd"] == "0.01"


def test_same_id_different_payload_or_endpoint_rejected(tmp_path):
    api, http = make(tmp_path, lambda _: httpx.Response(200, json=body()))
    with http:
        api.call("a", client.CHAT, {"n": 1})
        with pytest.raises(ValueError, match="changed"):
            api.call("a", client.CHAT, {"n": 2})
        with pytest.raises(ValueError, match="changed"):
            api.call("a", client.DECISIONS, {"n": 1})


def test_crash_after_intent_never_resends_and_blocks_other_calls(tmp_path):
    calls = []
    def handler(req):
        calls.append(req)
        raise KeyboardInterrupt()
    api, http = make(tmp_path, handler)
    with http:
        with pytest.raises(KeyboardInterrupt):
            api.call("a", client.CHAT, {})
        for cid in ("a", "b"):
            with pytest.raises(client.UnresolvedCallsError):
                api.call(cid, client.CHAT, {})
    assert len(calls) == 1
    assert (tmp_path / "calls/a/intent.json").exists()
    assert not (tmp_path / "calls/a/response.json").exists()


def test_transport_timeout_is_saved_unknown_no_retry(tmp_path):
    calls = []
    def handler(req):
        calls.append(req)
        raise httpx.ReadTimeout("private failure detail", request=req)
    api, http = make(tmp_path, handler)
    with http:
        record = api.call("a", client.CHAT, {})
        assert api.call("a", client.CHAT, {}) == record
        with pytest.raises(client.UnresolvedCallsError):
            api.call("b", client.CHAT, {})
    assert len(calls) == 1 and client.usage(record)["cost_unknown"]
    assert "private failure detail" not in json.dumps(record)


def test_unknown_fee_resolves_by_get_then_allows_next_paid_call(tmp_path):
    calls = []
    def handler(req):
        calls.append((req.method, str(req.url)))
        if req.method == "GET":
            assert req.url.params["id"] == "gen-one"
            return httpx.Response(200, json={"data": {"id": "gen-one", "total_cost": .012,
                "model": "vendor/model", "provider_name": "provider"}})
        return httpx.Response(200, json=body("gen-one" if len(calls) == 1 else "gen-two", None if len(calls) == 1 else .02))
    api, http = make(tmp_path, handler)
    with http:
        api.call("a", client.CHAT, {})
        with pytest.raises(client.UnresolvedCallsError):
            api.call("b", client.DECISIONS, {})
        resolved = api.reconcile("a")
        assert client.usage(resolved)["cost_usd"] == "0.012"
        assert client.usage(resolved)["cost_source"] == "generation_total_cost"
        assert api.reconcile("a") == resolved
        api.call("b", client.DECISIONS, {})
    assert [method for method, _ in calls] == ["POST", "GET", "POST"]
    assert (tmp_path / "calls/a/reconcile-0001.json").exists()
    assert client.usage(json.loads((tmp_path / "calls/a/response.json").read_text()))["cost_usd"] is None


def test_get_failure_saved_and_unknown_remains_blocked(tmp_path):
    def handler(req):
        return httpx.Response(404, json={"error": "missing"}) if req.method == "GET" else httpx.Response(200, json=body(cost=None))
    api, http = make(tmp_path, handler)
    with http:
        api.call("a", client.CHAT, {})
        result = api.reconcile("a")
        assert client.usage(result)["cost_unknown"]
        with pytest.raises(client.UnresolvedCallsError):
            api.call("b", client.CHAT, {})
    assert (tmp_path / "calls/a/reconcile-0001.json").exists()
    assert not (tmp_path / "calls/a/reconciled.json").exists()


def test_reconciliation_identity_mismatch_cannot_resolve(tmp_path):
    def handler(req):
        return httpx.Response(200, json={"data": {"id": "gen-other", "total_cost": .01}} if req.method == "GET" else body(cost=None))
    api, http = make(tmp_path, handler)
    with http:
        api.call("a", client.CHAT, {})
        assert client.usage(api.reconcile("a"))["cost_unknown"]


@pytest.mark.parametrize("cost", [None, True, False, -1, "NaN", "Infinity", "garbage"])
def test_nonfinite_negative_boolean_or_missing_cost_unknown(cost):
    record = {"raw": json.dumps(body(cost=cost)), "elapsed_ms": 1}
    assert client.usage(record)["cost_usd"] is None


def test_included_advisor_or_router_fees_not_added_again():
    data = body(cost=.02)
    data["usage"]["advisor_cost"] = .01
    data["usage"]["router_cost"] = .005
    record = {"raw": json.dumps(data), "reconciliation": {"verified": True,
        "generation_id": "gen-one", "total_cost": .021}}
    assert client.usage(record)["cost_usd"] == "0.021"


def test_duplicate_generation_id_blocks_future_paid_requests_and_persists(tmp_path):
    api, http = make(tmp_path, lambda _: httpx.Response(200, json=body()))
    with http:
        api.call("a", client.CHAT, {})
        second = api.call("b", client.CHAT, {})
        assert second["duplicate_generation_id"]
        assert api.call("b", client.CHAT, {})["duplicate_generation_id"]
        with pytest.raises(client.UnresolvedCallsError, match="Duplicate"):
            api.call("c", client.CHAT, {})


def test_duplicate_json_cost_not_trusted():
    raw = '{"id":"gen-one","usage":{"cost":0,"cost":100}}'
    assert client.usage({"raw": raw})["cost_unknown"]


@pytest.mark.parametrize("cid", ["../secret", "..", "a/b", "", "x" * 161])
def test_invalid_call_id_no_network(tmp_path, cid):
    api, http = make(tmp_path, lambda _: pytest.fail("Unexpected HTTP"))
    with http, pytest.raises(ValueError, match="call ID"):
        api.call(cid, client.CHAT, {})


def test_other_url_no_network(tmp_path):
    api, http = make(tmp_path, lambda _: pytest.fail("Unexpected HTTP"))
    with http, pytest.raises(ValueError, match="endpoint"):
        api.call("a", "https://example.com", {})


def test_no_redirect_follow_and_redacted_receipt(tmp_path):
    calls = []
    def handler(req):
        calls.append(req)
        return httpx.Response(302, headers={"location": "https://example.com", "x-request-id": KEY}, text=KEY)
    api, http = make(tmp_path, handler)
    with http:
        record = api.call("a", client.CHAT, {})
    assert len(calls) == 1 and record["http_status"] == 302
    assert KEY not in json.dumps(record)


def test_saved_answer_can_be_read_when_another_call_unknown(tmp_path):
    count = 0
    def handler(req):
        nonlocal count
        count += 1
        return httpx.Response(200, json=body("gen-one" if count == 1 else "gen-two", .01 if count == 1 else None))
    api, http = make(tmp_path, handler)
    with http:
        first = api.call("a", client.CHAT, {})
        api.call("b", client.CHAT, {})
        assert api.call("a", client.CHAT, {}) == first
    assert count == 2

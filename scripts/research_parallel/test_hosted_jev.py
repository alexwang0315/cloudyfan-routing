"""Mock-only hosted router packet/accounting checks; no key or live network."""

import json
from copy import deepcopy

import httpx
import pytest
from hosted_jev import MODEL, POLICY, HostedJevAdapter, HostedJevError

CONTRACT = "Return exactly one JSON object with a sql string."
MESSAGES = [
    {"role": "system", "content": "Common system output contract."},
    {"role": "user", "content": "Original public SQL task and data."},
]


def response(
    *,
    model="openai/fixed-20261001",
    provider="Provider",
    fallback=None,
    answer='{"sql":"SELECT 1"}',
):
    routing = {
        "resolved_models": [model],
        "candidates": [
            {"model": model, "effort": "high", "tier": "strong", "estimated_cost": "9.99"}
        ],
        "reason": "expected capability",
        "pipeline": "default",
        "advisor_model": "advisor/model",
        "models": ["openai/*"],
        "list_fallback": fallback,
        "list_tier_cap": "middle",
        "max_fallback": "deep",
    }
    body = {
        "id": "gen-mock",
        "model": model,
        "provider": provider,
        "choices": [{"message": {"role": "assistant", "content": answer}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 20, "cost": "0.003"},
        "openrouter_metadata": {"pipeline": [{"name": "jev-router", "data": routing}]},
    }
    return {"http_status": 200, "raw": json.dumps(body)}


def verified(record, proof, request):
    assert request is not None
    return {
        "verified": True,
        "cost_usd": "0.004",
        "generation_id": "gen-mock",
        "source": "verified_fixture_total",
        "complete_request_cost": True,
    }


def test_medium_frozen_packet_and_metadata_header_no_effort_or_self_classifier():
    adapter = HostedJevAdapter(
        output_contract=CONTRACT,
        requested_models=["openai/*"],
        allowed_models=["google/*"],
        excluded_models=["*expensive*"],
        allowed_providers=["Provider"],
        provider_only=["provider/fp8"],
    )
    packet = adapter.build_request(messages=MESSAGES)
    assert packet["payload"]["model"] == MODEL
    assert packet["payload"]["plugins"] == [
        {
            "id": "jev-router",
            "cost_tier": "medium",
            "models": ["openai/*"],
            "allowed_models": ["google/*"],
            "excluded_models": ["*expensive*"],
        }
    ]
    assert packet["headers"]["X-OpenRouter-Metadata"] == "enabled"
    assert packet["payload"]["messages"] == MESSAGES
    assert packet["payload"]["provider"] == {"only": ["provider/fp8"], "allow_fallbacks": False}
    assert "reasoning" not in packet["payload"] and "reasoning_effort" not in packet["payload"]
    assert adapter.descriptor()["self_built_jev_calls"] == 0
    assert POLICY["cost_tier_is_price_cap"] is False


def test_adapter_injected_mock_transport_does_one_request_and_preserves_metadata():
    seen = []

    def handler(request):
        seen.append(
            (request.method, request.headers["X-OpenRouter-Metadata"], json.loads(request.content))
        )
        return httpx.Response(200, json=json.loads(response()["raw"]))

    class Injected:
        def __init__(self):
            self.http = httpx.Client(transport=httpx.MockTransport(handler))

        def call(self, cid, url, payload, *, headers):
            result = self.http.post(url, json=payload, headers=headers)
            return {"http_status": result.status_code, "raw": result.text, "call_id": cid}

    adapter = HostedJevAdapter(output_contract=CONTRACT, accounting_verifier=verified)
    packet = adapter.build_request(messages=MESSAGES)
    parsed = adapter.parse_response(
        adapter.submit(Injected(), "call1", packet),
        request=packet,
        accounting_proof={"fixture": True},
    )
    assert len(seen) == 1 and seen[0][:2] == ("POST", "enabled")
    assert parsed["actual_model"] == "openai/fixed-20261001"
    assert parsed["actual_provider"] == "Provider" and parsed["generation_id"] == "gen-mock"
    assert parsed["routing"]["advisor_model"] == "advisor/model"
    assert parsed["routing"]["candidates"][0]["effort"] == "high"
    assert parsed["candidate_cost_estimates"] == ["9.99"]
    assert parsed["billing"]["api_cost_usd"] == "0.004"
    assert parsed["billing"]["advisor_added_separately"] is False
    assert parsed["self_built_jev_calls"] == 0 and parsed["hosted_router_stage_count"] == 1


def test_no_answer_429_and_525_repair_restarts_original_task_without_fake_answer():
    adapter = HostedJevAdapter(output_contract=CONTRACT)
    for code in (429, 525):
        parsed = adapter.parse_response(
            {"http_status": code, "raw": json.dumps({"error": {"code": code}})}
        )
        assert parsed["answer"] is None and parsed["billing"]["cost_unknown"]
        repair = adapter.repair_messages(
            MESSAGES, parsed["answer"], {"hidden_answer": "must not be read"}
        )
        assert repair[:2] == MESSAGES and not any(m["role"] == "assistant" for m in repair)
        assert "from the beginning" in repair[-1]["content"] and CONTRACT in repair[-1]["content"]
        assert "hidden_answer" not in repair[-1]["content"]


def test_actual_answer_repair_uses_latest_public_diagnostic_only():
    adapter = HostedJevAdapter(output_contract=CONTRACT)
    repair = adapter.repair_messages(
        MESSAGES, "LATEST actual answer", {"passed": False, "checks": ["SQL_RESPONSE_JSON"]}
    )
    assert repair[-2] == {"role": "assistant", "content": "LATEST actual answer"}
    assert "SQL_RESPONSE_JSON" in repair[-1]["content"] and CONTRACT in repair[-1]["content"]
    packet = adapter.build_request(messages=repair, attempt=1)
    assert packet["attempt"] == 1
    with pytest.raises(HostedJevError, match="two answer attempts"):
        adapter.build_request(messages=repair, attempt=2)
    with pytest.raises(HostedJevError, match="public diagnostic"):
        adapter.repair_messages(MESSAGES, "answer", {"hidden_answer": "forbidden"})


def test_soft_include_fallback_is_service_comparison_not_identical_pool():
    adapter = HostedJevAdapter(output_contract=CONTRACT, requested_models=["anthropic/*"])
    parsed = adapter.parse_response(response(fallback="models_ignored"))
    assert parsed["comparison_scope"] == "hosted_service_comparison"
    assert "include_preferences_ignored_by_hosted_router" in parsed["scope_reasons"]
    assert "actual_model_outside_include_preferences" in parsed["scope_reasons"]
    assert parsed["routing"]["list_tier_cap"] == "middle"
    assert parsed["routing"]["max_fallback"] == "deep"


def test_hard_exclusion_and_provider_violation_are_explicit():
    adapter = HostedJevAdapter(
        output_contract=CONTRACT,
        excluded_models=["openai/fixed"],
        allowed_providers=["Allowed"],
        provider_only=["allowed/fp8"],
    )
    parsed = adapter.parse_response(response())
    assert set(parsed["restriction_violations"]) == {
        "actual_model_violates_hard_exclusion",
        "actual_provider_outside_frozen_restriction",
    }
    assert parsed["identity_restrictions_satisfied"] is False


def test_unresolved_moving_alias_is_not_claimed_verified():
    adapter = HostedJevAdapter(output_contract=CONTRACT, excluded_models=["~openai/family-latest"])
    parsed = adapter.parse_response(response())
    assert "hard_exclusion_alias_unresolved" in parsed["scope_reasons"]
    assert not parsed["identity_restrictions_satisfied"]


def test_response_usage_cost_and_estimate_without_verifier_remain_unknown():
    parsed = HostedJevAdapter(output_contract=CONTRACT).parse_response(
        response(), accounting_proof={"verified": True, "cost_usd": "0"}
    )
    assert parsed["usage"]["cost"] == "0.003" and parsed["candidate_cost_estimates"] == ["9.99"]
    assert parsed["billing"]["api_cost_usd"] is None and parsed["billing"]["cost_unknown"]


@pytest.mark.parametrize(
    "change",
    [
        {"cost_usd": "NaN"},
        {"cost_usd": -1},
        {"cost_usd": 0.1},
        {"cost_usd": True},
        {"complete_request_cost": False},
        {"generation_id": "wrong-id"},
        {"source": "estimated_cost"},
        {"verified": False},
    ],
)
def test_invalid_or_partial_proof_cannot_become_paid_cost(change):
    def verify(*args):
        return {**verified(None, None, {}), **change}

    parsed = HostedJevAdapter(output_contract=CONTRACT, accounting_verifier=verify).parse_response(
        response(), request={}
    )
    assert parsed["billing"]["api_cost_usd"] is None and parsed["billing"]["cost_unknown"]


def test_missing_metadata_actual_identity_unknown_and_duplicate_json_rejected():
    adapter = HostedJevAdapter(output_contract=CONTRACT)
    parsed = adapter.parse_response({"http_status": 503, "raw": '{"error":{"code":503}}'})
    assert "hosted_router_metadata_missing" in parsed["scope_reasons"]
    assert "actual_serving_identity_unverified" in parsed["scope_reasons"]
    assert parsed["model_failure"] and parsed["self_built_jev_calls"] == 0
    with pytest.raises(HostedJevError, match="Invalid response JSON"):
        adapter.parse_response({"http_status": 200, "raw": '{"model":"one","model":"two"}'})


def test_changed_packet_is_rejected_before_injected_client():
    adapter = HostedJevAdapter(output_contract=CONTRACT)
    packet = adapter.build_request(messages=MESSAGES)
    packet["payload"]["plugins"][0]["cost_tier"] = "high"

    class Never:
        def call(self, *args, **kwargs):
            pytest.fail("Changed packet may not reach client")

    with pytest.raises(HostedJevError, match="packet changed"):
        adapter.submit(Never(), "call", packet)


def test_transport_failure_is_never_retried():
    class Broken:
        def __init__(self):
            self.count = 0

        def call(self, *args, **kwargs):
            self.count += 1
            raise RuntimeError("mock transport failure")

    client = Broken()
    adapter = HostedJevAdapter(output_contract=CONTRACT)
    with pytest.raises(RuntimeError, match="mock transport failure"):
        adapter.submit(client, "call", adapter.build_request(messages=MESSAGES))
    assert client.count == 1


def test_original_messages_and_response_metadata_are_not_mutated():
    adapter = HostedJevAdapter(output_contract=CONTRACT)
    original, saved = deepcopy(MESSAGES), response()
    snapshot = deepcopy(saved)
    packet = adapter.build_request(messages=MESSAGES)
    parsed = adapter.parse_response(saved)
    packet["payload"]["messages"][0]["content"] = "changed copy"
    parsed["routing"]["candidates"][0]["model"] = "changed copy"
    assert MESSAGES == original and saved == snapshot


def test_provider_slugs_and_response_names_are_never_guessed():
    with pytest.raises(HostedJevError, match="both be explicit"):
        HostedJevAdapter(output_contract=CONTRACT, allowed_providers=["Near AI"])
    with pytest.raises(HostedJevError, match="both be explicit"):
        HostedJevAdapter(output_contract=CONTRACT, provider_only=["near-ai/fp8"])
    adapter = HostedJevAdapter(
        output_contract=CONTRACT, allowed_providers=["Near AI"], provider_only=["near-ai/fp8"]
    )
    assert adapter.build_request(messages=MESSAGES)["payload"]["provider"]["only"] == [
        "near-ai/fp8"
    ]
    assert adapter.parse_response(response(provider="Near AI"))["identity_restrictions_satisfied"]

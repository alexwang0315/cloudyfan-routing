"""Offline contracts for the isolated two-binary-stage routing policy."""
import copy
import json

import pytest


from cloudyfan_jev import routing as policy
MODEL = "typesafe/jev-1.13"


@pytest.fixture
def profiles():
    return {tier: {"model": "vendor/" + tier, "model_version": "vendor/" + tier + "-v1", "effort": "low",
                   "capability_card": {"operations": ["aggregation"], "calibration_tasks": 12},
                   "external_score": score}
            for tier, score in (("low", 70), ("mid", 90), ("high", 110))}


@pytest.fixture
def public():
    return {"task": {"prompt": "Count records", "public_schema": "CREATE TABLE t(x);"},
            "previous_answer": "SELECT x FROM t", "public_diagnostics": {"code": "OUTPUT_SHAPE"},
            "tool_results": [{"tool": "sql", "result": "wrong columns"}]}


def response(stage, choice, probability=.8):
    pair = policy.STAGES[stage]
    return {"id": "gen-test", "model": MODEL, "provider": "TypeSafe",
            "answers": {stage: {"type": "choice", "choice": choice,
                                "probabilities": {tier: probability if tier == choice else 1 - probability for tier in pair}}},
            "usage": {"input_tokens": 40, "output_tokens": 3, "cost": .0001}}


def parse(raw, stage, public, profiles):
    context, questions, _ = policy.build_request(stage, public, profiles, MODEL)
    return policy.parse_choice(raw, context, questions, MODEL)


@pytest.mark.parametrize("stage,pair", list(policy.STAGES.items()))
def test_exactly_one_binary_question_and_only_current_profiles(stage, pair, public, profiles):
    _, questions, payload = policy.build_request(stage, public, profiles, MODEL)
    assert len(questions) == 1
    assert set(questions[0].criteria) == set(pair)
    assert set(payload["state"]["candidates"]) == set(pair)
    assert set(payload["questions"]) == {stage}
    assert payload["provider"] == {"only": ["typesafe"], "allow_fallbacks": False}
    assert payload["state"]["observations"]["previous_answer"] == public["previous_answer"]


@pytest.mark.parametrize("stage,choice", [('low_mid','low'),('low_mid','mid'),('mid_high','mid'),('mid_high','high')])
def test_route_selects_exactly_one_stage(stage, choice, public, profiles):
    calls=[]
    def decide(current, context, questions, payload):
        calls.append(current)
        return response(current, choice)
    result=policy.route(False, public, profiles, MODEL, decide, stage=stage)
    assert result['selected_tier']==choice and result['classifier_calls']==1
    assert calls==[stage] and len(result['decisions'])==1


def test_public_pass_skips_all_classification(public, profiles):
    def forbidden(*args):
        pytest.fail("No classifier allowed after public pass")
    assert policy.route(True, public, profiles, MODEL, forbidden) == {
        "selected_tier": "open_source", "decisions": [], "classifier_calls": 0}


def test_unknown_public_validation_enters_route(public, profiles):
    result = policy.route(None, public, profiles, MODEL, lambda stage, *_: response(stage, "low"))
    assert result["classifier_calls"] == 1


@pytest.mark.parametrize("status", [1, "true", [], {}])
def test_public_validation_requires_explicit_status(status, public, profiles):
    with pytest.raises(ValueError):
        policy.route(status, public, profiles, MODEL, None)


@pytest.mark.parametrize("stage", policy.STAGES)
def test_missing_distribution_preserves_reported_choice_and_paid_cost(stage, public, profiles):
    lower, upper = policy.STAGES[stage]
    raw = response(stage, lower)
    del raw["answers"][stage]["probabilities"]
    outcome = parse(raw, stage, public, profiles)
    assert outcome["selected_tier"] == upper
    assert outcome["reported_choice"] == lower
    assert outcome["fallback"] and outcome["fallback_reason"] == "distribution_missing"
    assert outcome["cost_usd"] == "0.0001"


@pytest.mark.parametrize("probability", [.5, .2])
def test_tie_or_choice_inconsistent_with_distribution_falls_back(probability, public, profiles):
    outcome = parse(response("low_mid", "low", probability), "low_mid", public, profiles)
    assert outcome["selected_tier"] == "mid" and outcome["fallback"]
    assert outcome["reported_choice"] == "low"


def test_invalid_json_fallback_does_not_fabricate_choice_or_zero_cost(public, profiles):
    outcome = parse("{", "mid_high", public, profiles)
    assert outcome["selected_tier"] == "high" and outcome["fallback"]
    assert outcome["reported_choice"] is None
    assert outcome["cost_usd"] is None and outcome["cost_unknown"]


def test_paid_malformed_answer_retains_receipt(public, profiles):
    raw = response("low_mid", "low")
    raw["answers"]["low_mid"]["choice"] = "high"
    outcome = parse(raw, "low_mid", public, profiles)
    assert outcome["fallback"] and outcome["reported_choice"] is None
    assert outcome["id"] == "gen-test" and outcome["cost_usd"] == "0.0001"


def test_parse_failure_falls_back_only_inside_current_stage(public, profiles):
    calls=[]
    def decide(stage,*_):
        calls.append(stage)
        return 'not json'
    result=policy.route(False, public, profiles, MODEL, decide)
    assert calls==['low_mid'] and result['selected_tier']=='mid'
    assert result['decisions'][0]['fallback'] and result['decisions'][0]['cost_unknown']


def test_root_and_task_whitelist_drops_private_fields(public, profiles):
    public.update(hidden_answer="SENTINEL", acceptance={"expected": "SENTINEL"}, arbitrary="SENTINEL")
    public["task"].update(gold="SENTINEL", reference_sql="SENTINEL")
    _, _, payload = policy.build_request("low_mid", public, profiles, MODEL)
    assert "SENTINEL" not in json.dumps(payload)


@pytest.mark.parametrize("private", ["hidden_tests", "privateAcceptance", "gold", "reference_sql", "acceptance", "expected_answer"])
def test_nested_private_fields_rejected(private, public, profiles):
    public["public_diagnostics"][private] = "SENTINEL"
    with pytest.raises(ValueError, match="Private evaluation"):
        policy.build_request("low_mid", public, profiles, MODEL)


def test_capability_card_private_data_rejected(public, profiles):
    profiles["low"]["capability_card"]["hidden_passes"] = 50
    with pytest.raises(ValueError, match="Private evaluation"):
        policy.build_request("low_mid", public, profiles, MODEL)


def test_no_global_mean_cost_ranking_or_mutation(public, profiles):
    profiles["low"]["mean_cost_usd"] = 999
    profiles["mid"]["mean_cost_usd"] = 0
    before = copy.deepcopy(profiles)
    result = policy.route(False, public, profiles, MODEL, lambda stage, *_: response(stage, "low"))
    assert result["selected_tier"] == "low"
    assert profiles == before


def test_transport_error_propagates_without_retry(public, profiles):
    calls = []
    def failure(stage, *_):
        calls.append(stage)
        raise RuntimeError("transport unavailable")
    with pytest.raises(RuntimeError, match="transport unavailable"):
        policy.route(False, public, profiles, MODEL, failure)
    assert calls == ["low_mid"]


def test_missing_cost_stays_unknown(public, profiles):
    raw = response("low_mid", "low")
    del raw["usage"]["cost"]
    result = parse(raw, "low_mid", public, profiles)
    assert not result["fallback"]
    assert result["cost_usd"] is None and result["cost_unknown"]


def test_model_version_separates_context(public, profiles):
    context, _, _ = policy.build_request("low_mid", public, profiles, MODEL, "pinned-release")
    assert context.model_version == "pinned-release"


@pytest.mark.parametrize("stage", policy.STAGES)
def test_candidate_pinned_version_changes_classifier_payload(stage, public, profiles):
    tier = policy.STAGES[stage][0]
    _, _, before = policy.build_request(stage, public, profiles, MODEL)
    profiles[tier]["model_version"] = "vendor/pinned-v2"
    _, _, after = policy.build_request(stage, public, profiles, MODEL)
    assert before != after
    assert after["state"]["candidates"][tier]["model_version"] == "vendor/pinned-v2"
    assert before["state"]["candidates"][tier]["model"] == after["state"]["candidates"][tier]["model"]


@pytest.mark.parametrize("value", [None, "", "   ", 3, {}, []])
def test_candidate_version_must_be_explicit(value, public, profiles):
    profiles["low"]["model_version"] = value
    with pytest.raises(ValueError, match="model_version"):
        policy.build_request("low_mid", public, profiles, MODEL)


def test_missing_candidate_version_is_not_replaced_by_family(public, profiles):
    del profiles["low"]["model_version"]
    with pytest.raises(ValueError, match="model_version"):
        policy.build_request("low_mid", public, profiles, MODEL)


def test_only_safe_explicit_candidate_identity_enters_payload(public, profiles):
    profiles["low"].update(provider="fixture-provider", provider_name="Fixture Provider",
                           credentials="SECRET_SENTINEL", hidden_answers="ANSWER_SENTINEL")
    _, _, payload = policy.build_request("low_mid", public, profiles, MODEL)
    candidate = payload["state"]["candidates"]["low"]
    assert candidate["provider"] == "fixture-provider"
    assert candidate["provider_name"] == "Fixture Provider"
    assert candidate["effort"] == "low"
    assert "SECRET_SENTINEL" not in json.dumps(payload)
    assert "ANSWER_SENTINEL" not in json.dumps(payload)


@pytest.mark.parametrize("field", ["provider", "provider_name"])
@pytest.mark.parametrize("value", ["", "  ", None, {"hidden_key": "secret"}])
def test_optional_candidate_identity_must_be_explicit_if_present(field, value, public, profiles):
    profiles["low"][field] = value
    with pytest.raises(ValueError, match=field):
        policy.build_request("low_mid", public, profiles, MODEL)


def test_new_projection_does_not_resume_old_frozen_policy(tmp_path, monkeypatch):
    import httpx
    from cloudyfan_jev.client import Client
    from cloudyfan_jev.demo import ArithmeticEnvironment, example_config
    from cloudyfan_jev.engine import Core

    current_version = policy.VERSION
    assert current_version != "hierarchical50-binary-v1"
    calls = []
    def serve(request):
        calls.append(request)
        return httpx.Response(200, json={"id": "gen-frozen-policy", "model": "example/open_source", "provider": "Example",
            "usage": {"cost": .001}, "choices": [{"message": {"role": "assistant", "content": "2"}, "finish_reason": "stop"}]})
    configured, decision = example_config()
    engine = Core(Client(httpx.Client(transport=httpx.MockTransport(serve)), "fixture-only", tmp_path), configured, decision)
    monkeypatch.setattr(policy, "VERSION", "hierarchical50-binary-v1")
    engine.run("frozen", {"prompt": "Return 2"}, ArithmeticEnvironment, contract_version="v1")
    before = {path: path.read_bytes() for path in tmp_path.rglob("*.json")}
    monkeypatch.setattr(policy, "VERSION", current_version)
    with pytest.raises(ValueError, match="Resume evidence changed"):
        engine.run("frozen", {"prompt": "Return 2"}, ArithmeticEnvironment, contract_version="v1")
    assert len(calls) == 1
    assert {path: path.read_bytes() for path in tmp_path.rglob("*.json")} == before

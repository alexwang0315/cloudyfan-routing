import hashlib
import json
from dataclasses import FrozenInstanceError, replace
from decimal import Decimal

import pytest

from cloudyfan_jev.decisions import (
    ENDPOINT,
    DecisionContext,
    DecisionQuestion,
    build_payload,
    cache_key,
    parse_response,
)
from cloudyfan_jev.errors import RoutingError

MODEL = "typesafe/jev-1.13"
RESOLVED = "typesafe/jev-1.13-20260917"


@pytest.fixture
def context():
    return DecisionContext(
        tenant_id="tenant-a",
        state={"task": "Route this public ticket", "requirements": ["text"]},
        state_version="state-1",
        policy_version="policy-1",
        skill_version="skills-1",
        model_version=RESOLVED,
        required_fields=("task", "requirements"),
    )


@pytest.fixture
def questions():
    return (
        DecisionQuestion(
            "team",
            "choice",
            "Which team owns the ticket?",
            {
                "account": "Login problems",
                "frontend": "Rendering problems",
                "payments": "Billing",
            },
        ),
        DecisionQuestion(
            "is_bug",
            "noul",
            "Does the ticket describe broken behavior?",
            {"false": "Question or request", "true": "Broken behavior"},
        ),
        DecisionQuestion(
            "urgency",
            "score",
            "How urgent is the ticket?",
            [
                "Can wait for the next release",
                "Should be fixed this week",
                "Blocking revenue right now",
            ],
        ),
    )


@pytest.fixture
def response():
    # Shape and numeric example verified against the official OpenRouter reference:
    # https://openrouter.ai/docs/api/api-reference/alphadecisions/submit-a-decisions-request
    # Optional fields additionally checked against OpenRouterTeam/typescript-sdk.
    return {
        "id": "gen-dec-1789738314-X5e5eKGQdvR9rblyX250",
        "model": RESOLVED,
        "provider": "TypeSafe",
        "usage": {"input_tokens": 476, "output_tokens": 70, "cost": Decimal("0.000019992")},
        "answers": {
            "is_bug": {"type": "noul", "noul": Decimal("0.96")},
            "team": {
                "type": "choice",
                "choice": "payments",
                "confidence": Decimal("0.75"),
                "probabilities": {
                    "account": 0,
                    "frontend": Decimal("0.16"),
                    "payments": Decimal("0.84"),
                },
            },
            "urgency": {
                "type": "score",
                "score": Decimal("1.99"),
                "confidence": Decimal("0.99"),
                "legend": {
                    "0": "Can wait for the next release",
                    "1": "Should be fixed this week",
                    "2": "Blocking revenue right now",
                },
                "probabilities": {"0": 0, "1": Decimal("0.01"), "2": Decimal("0.99")},
            },
        },
    }


def parse(raw, context, questions):
    return parse_response(raw, context, questions, model=MODEL, allowed_response_models=(RESOLVED,))


def json_text(raw):
    return json.dumps(raw, default=float)


def test_official_choice_noul_score_contract(context, questions, response):
    batch = parse(json_text(response), context, questions)
    assert batch.answers["team"].choice == "payments"
    assert batch.answers["team"].probabilities["payments"] == Decimal("0.84")
    assert batch.answers["team"].confidence == Decimal("0.75")
    assert batch.answers["is_bug"].noul == Decimal("0.96")
    assert batch.answers["is_bug"].confidence is None
    assert batch.answers["urgency"].score == Decimal("1.99")
    assert batch.answers["urgency"].legend["2"] == "Blocking revenue right now"
    assert batch.usage.actual_cost_usd == Decimal("0.000019992")
    assert batch.call_cost_usd == batch.usage.actual_cost_usd
    assert not hasattr(batch, "confidence")
    assert batch.requires_hard_gate_recheck is True
    assert batch.state_hash == context.state_hash
    assert batch.question_versions == {"team": "1", "is_bug": "1", "urgency": "1"}


def test_official_request_uses_named_question_objects(context, questions):
    payload = build_payload(context, questions, model=MODEL)
    assert ENDPOINT == "/api/alpha/decisions"
    assert set(payload) == {"model", "state", "questions", "provider"}
    assert payload["model"] == MODEL
    assert payload["provider"] == {"allow_fallbacks": False}
    assert set(payload["questions"]) == {"team", "is_bug", "urgency"}
    for question in questions:
        wire = payload["questions"][question.question_id]
        assert set(wire) == {"type", "instructions", "criteria"}
        assert wire["instructions"] == question.instructions
        assert wire["type"] == question.type
    assert isinstance(payload["questions"]["team"]["criteria"], dict)
    assert set(payload["questions"]["is_bug"]["criteria"]) == {"false", "true"}
    assert isinstance(payload["questions"]["urgency"]["criteria"], list)


def test_official_minimal_answers_keep_missing_evidence_explicit(context, questions):
    # OpenRouter's wire schema requires only type/choice, type/noul, and
    # type/score. Cost, distributions, confidence, legend, ID and provider are
    # optional; SDK camelCase usage aliases are not the raw HTTP schema.
    raw = {
        "model": MODEL,
        "answers": {
            "team": {"type": "choice", "choice": "payments"},
            "is_bug": {"type": "noul", "noul": 0},
            "urgency": {"type": "score", "score": Decimal("1.25")},
        },
        "usage": {"input_tokens": 0, "output_tokens": 0},
    }
    result = parse_response(raw, context, questions, model=MODEL)
    assert result.response_id is result.provider is None
    assert result.usage.actual_cost_usd is result.call_cost_usd is None
    assert result.answers["is_bug"].noul == 0
    assert not result.answers["is_bug"].abstained
    for question_id in ("team", "urgency"):
        answer = result.answers[question_id]
        assert answer.abstain_reason == "distribution_missing"
        assert answer.probabilities is answer.confidence is answer.legend is None
    assert result.answers["urgency"].score == Decimal("1.25")


def test_optional_distributions_abstain_without_inventing_confidence(context, questions, response):
    del response["answers"]["team"]["probabilities"]
    del response["answers"]["team"]["confidence"]
    del response["answers"]["urgency"]["legend"]
    batch = parse(response, context, questions)
    assert batch.answers["team"].abstain_reason == "distribution_missing"
    assert batch.answers["team"].probabilities is None
    assert batch.answers["team"].confidence is None
    assert batch.answers["urgency"].legend is None
    assert not batch.answers["urgency"].abstained


def test_low_confidence_is_not_global_gate(context, questions, response):
    response["answers"]["team"]["confidence"] = 0
    batch = parse(response, context, questions)
    assert batch.answers["team"].confidence == 0
    assert not batch.answers["team"].abstained
    assert not batch.answers["urgency"].abstained


def test_explicit_abstain_choice(context):
    questions = [
        DecisionQuestion(
            "skill",
            "choice",
            "Which skill applies?",
            {"extract": "Extraction", "none": "No appropriate skill"},
            abstain_choices=("none",),
        )
    ]
    response = {
        "model": MODEL,
        "usage": {"input_tokens": 1, "output_tokens": 1},
        "answers": {
            "skill": {
                "type": "choice",
                "choice": "none",
                "probabilities": {"extract": 0.1, "none": 0.9},
            }
        },
    }
    result = parse(response, context, questions)
    assert result.answers["skill"].abstain_reason == "explicit_abstention_choice"
    assert result.usage.actual_cost_usd is None


@pytest.mark.parametrize(
    "kind",
    [
        "missing_question",
        "extra_question",
        "wrong_type",
        "bad_choice",
        "missing_choice",
        "extra_probability",
        "missing_probability",
        "bad_probability_sum",
        "negative_probability",
        "string_probability",
        "boolean_probability",
        "nan_probability",
        "infinite_probability",
        "nan_confidence",
        "confidence_over_one",
        "noul_boolean",
        "noul_over_one",
        "noul_confidence",
        "score_string",
        "score_out_of_range",
        "score_nan",
        "wrong_legend",
        "extra_answer_field",
        "boolean_usage",
        "missing_usage_tokens",
        "negative_usage",
        "wrong_model",
    ],
)
def test_invalid_answers_retain_billing_evidence(kind, context, questions, response):
    answers = response["answers"]
    team = answers["team"]
    probabilities = team["probabilities"]
    if kind == "missing_question":
        del answers["is_bug"]
    elif kind == "extra_question":
        answers["surprise"] = answers["is_bug"]
    elif kind == "wrong_type":
        team["type"] = "score"
    elif kind == "bad_choice":
        team["choice"] = "other"
    elif kind == "missing_choice":
        del team["choice"]
    elif kind == "extra_probability":
        probabilities["other"] = 0
    elif kind == "missing_probability":
        del probabilities["account"]
    elif kind == "bad_probability_sum":
        probabilities["payments"] = Decimal("0.90")
    elif kind.endswith("probability"):
        probabilities["payments"] = {
            "negative_probability": -1,
            "string_probability": "0.84",
            "boolean_probability": True,
            "nan_probability": float("nan"),
            "infinite_probability": float("inf"),
        }[kind]
    elif kind == "nan_confidence":
        team["confidence"] = Decimal("NaN")
    elif kind == "confidence_over_one":
        team["confidence"] = 2
    elif kind == "noul_boolean":
        answers["is_bug"]["noul"] = True
    elif kind == "noul_over_one":
        answers["is_bug"]["noul"] = 1.1
    elif kind == "noul_confidence":
        answers["is_bug"]["confidence"] = 0.9
    elif kind == "score_string":
        answers["urgency"]["score"] = "1.99"
    elif kind == "score_out_of_range":
        answers["urgency"]["score"] = 3
    elif kind == "score_nan":
        answers["urgency"]["score"] = float("nan")
    elif kind == "wrong_legend":
        answers["urgency"]["legend"]["0"] = "Different rubric"
    elif kind == "extra_answer_field":
        team["secret_answer"] = "unexpected"
    elif kind == "boolean_usage":
        response["usage"]["input_tokens"] = True
    elif kind == "missing_usage_tokens":
        del response["usage"]["output_tokens"]
    elif kind == "negative_usage":
        response["usage"]["input_tokens"] = -1
    elif kind == "wrong_model":
        response["model"] = "unexpected/model"
    with pytest.raises(RoutingError) as caught:
        parse(response, context, questions)
    error = caught.value
    assert error.code == "DECISION_RESPONSE_INVALID"
    assert error.details["cost_unknown"] is False
    assert error.details["actual_cost_usd"] == "0.000019992"
    assert error.details["response_id"] == response["id"]
    assert "unexpected/model" not in json.dumps(error.body())
    stage, reason = {
        "missing_question": ("question_ids", "question_ids_mismatch"),
        "extra_question": ("question_ids", "question_ids_mismatch"),
        "wrong_type": ("answer_schema", "answer_type_mismatch"),
        "bad_choice": ("answer_choice", "choice_not_allowed"),
        "missing_choice": ("answer_choice", "choice_not_allowed"),
        "extra_probability": ("answer_distribution", "distribution_keys_mismatch"),
        "missing_probability": ("answer_distribution", "distribution_keys_mismatch"),
        "bad_probability_sum": ("answer_distribution", "distribution_not_normalized"),
        "negative_probability": ("answer_distribution", "numeric_negative"),
        "string_probability": ("answer_distribution", "numeric_type_invalid"),
        "boolean_probability": ("answer_distribution", "numeric_type_invalid"),
        "nan_probability": ("answer_distribution", "numeric_not_finite"),
        "infinite_probability": ("answer_distribution", "numeric_not_finite"),
        "nan_confidence": ("answer_confidence", "numeric_not_finite"),
        "confidence_over_one": ("answer_confidence", "probability_out_of_range"),
        "noul_boolean": ("answer_noul", "numeric_type_invalid"),
        "noul_over_one": ("answer_noul", "probability_out_of_range"),
        "noul_confidence": ("answer_schema", "noul_fields_mismatch"),
        "score_string": ("answer_score", "numeric_type_invalid"),
        "score_out_of_range": ("answer_score", "score_out_of_range"),
        "score_nan": ("answer_score", "numeric_not_finite"),
        "wrong_legend": ("answer_legend", "legend_mismatch"),
        "extra_answer_field": ("answer_schema", "unexpected_answer_fields"),
        "boolean_usage": ("usage_schema", "usage_tokens_invalid"),
        "missing_usage_tokens": ("usage_schema", "usage_tokens_invalid"),
        "negative_usage": ("usage_schema", "usage_tokens_invalid"),
        "wrong_model": ("model_identity", "model_not_allowed"),
    }[kind]
    assert error.details["validation_stage"] == stage
    assert error.details["reason_code"] == reason
    if kind != "wrong_model":
        assert error.details["actual_model"] == RESOLVED
    else:
        assert "actual_model" not in error.details


@pytest.mark.parametrize("field", ["cost", "input_tokens", "id", "team", "payments"])
def test_duplicate_json_keys_are_rejected_without_hiding_unambiguous_charge(
    field,
    context,
    questions,
    response,
):
    raw = json_text(response)
    if field == "cost":
        raw = raw.replace('"cost":', '"cost": 1, "cost":', 1)
    elif field == "input_tokens":
        raw = raw.replace('"input_tokens":', '"input_tokens": 1, "input_tokens":', 1)
    elif field == "id":
        raw = raw.replace('"id":', '"id": "gen-other", "id":', 1)
    elif field == "team":
        raw = raw.replace('"team":', '"team": {}, "team":', 1)
    else:
        raw = raw.replace('"payments": 0.84', '"payments": 0.5, "payments": 0.84')
    with pytest.raises(RoutingError) as caught:
        parse(raw, context, questions)
    details = caught.value.details
    assert caught.value.code == "DECISION_RESPONSE_INVALID"
    assert details["validation_stage"] == "response_schema"
    assert details["reason_code"] == "duplicate_json_keys"
    assert details["response_shape"]["duplicate_key_count"] == 1
    assert details["cost_unknown"] is (field == "cost")
    assert ("response_id" in details) is (field != "id")
    if field != "cost":
        assert details["actual_cost_usd"] == "0.000019992"
    if field == "input_tokens":
        assert "input_tokens" not in details


@pytest.mark.parametrize("cost", [True, "NaN", float("inf"), Decimal("NaN"), -1])
def test_invalid_charge_is_unknown_not_zero(cost, context, questions, response):
    response["usage"]["cost"] = cost
    with pytest.raises(RoutingError) as caught:
        parse(response, context, questions)
    assert caught.value.details["cost_unknown"]
    assert "actual_cost_usd" not in caught.value.details
    assert caught.value.details["input_tokens"] == 476


def test_model_resolution_requires_explicit_allowlist(context, questions, response):
    with pytest.raises(RoutingError) as caught:
        parse_response(response, context, questions, model=MODEL)
    details = caught.value.details
    assert details["validation_stage"] == "model_identity"
    assert details["reason_code"] == "model_not_allowed"
    assert details["actual_cost_usd"] == "0.000019992"
    assert details["response_id"] == response["id"]
    identity = details["response_shape"]["model"]
    assert identity["sha256"] == hashlib.sha256(RESOLVED.encode()).hexdigest()
    assert identity["matches_requested"] is False
    assert identity["matches_allowed_alias"] is False
    assert "actual_model" not in details
    assert RESOLVED not in json.dumps(caught.value.body())
    assert parse(response, context, questions).model == RESOLVED


@pytest.mark.parametrize(
    "actual_model",
    [
        MODEL,
        RESOLVED,
    ],
)
def test_only_requested_or_frozen_canonical_model_is_accepted(
    actual_model, context, questions, response
):
    response["model"] = actual_model
    batch = parse(response, context, questions)
    assert batch.model == actual_model
    assert batch.usage.actual_cost_usd == Decimal("0.000019992")


@pytest.mark.parametrize(
    "actual_model",
    [
        "typesafe/jev-1.13.0",
        "typesafe/jev-1.13-20260918",
        "typesafe/jev-1.13-20260917-extra",
        "~typesafe/jev-latest",
        "typesafe/jev-1.13-*",
        " typesafe/jev-1.13-20260917",
    ],
)
def test_model_prefixes_versions_and_sdk_names_are_not_aliases(
    actual_model, context, questions, response
):
    response["model"] = actual_model
    with pytest.raises(RoutingError) as caught:
        parse(response, context, questions)
    details = caught.value.details
    assert details["reason_code"] == "model_not_allowed"
    assert details["response_shape"]["model"]["matches_allowed_alias"] is False
    assert "actual_model" not in details
    assert actual_model not in json.dumps(caught.value.body())
    assert details["actual_cost_usd"] == "0.000019992"
    assert details["response_id"] == response["id"]


def test_string_allowlist_cannot_promote_a_rejected_substring(context, questions, response):
    response["model"] = "typesafe/jev-1.13-2026"
    with pytest.raises(RoutingError) as caught:
        parse_response(response, context, questions, model=MODEL, allowed_response_models=RESOLVED)
    details = caught.value.details
    assert details["reason_code"] == "model_not_allowed"
    assert details["response_shape"]["model"]["matches_allowed_alias"] is False
    assert "allowlisted_value" not in details["response_shape"]["model"]
    assert "actual_model" not in details
    assert response["model"] not in json.dumps(caught.value.body())
    assert details["actual_cost_usd"] == "0.000019992"
    assert details["response_id"] == response["id"]


@pytest.mark.parametrize(
    "raw,stage,reason",
    [
        ('{"private prompt": "sk-or-v1-test-secret"', "json_decode", "invalid_json"),
        (b"\xff private prompt", "json_decode", "invalid_encoding"),
        ("null", "response_schema", "response_not_object"),
        ('["sk-or-v1-test-secret"]', "response_schema", "response_not_object"),
    ],
    ids=["invalid_json", "invalid_encoding", "null", "array"],
)
def test_decode_and_root_failures_have_safe_stage(raw, stage, reason, context, questions):
    with pytest.raises(RoutingError) as caught:
        parse(raw, context, questions)
    error = caught.value
    assert error.code == "DECISION_RESPONSE_INVALID"
    assert error.details["validation_stage"] == stage
    assert error.details["reason_code"] == reason
    assert error.details["cost_unknown"] is True
    assert "actual_cost_usd" not in error.details
    serialized = json.dumps(error.body())
    assert "private prompt" not in serialized
    assert "sk-or-v1-test-secret" not in serialized
    assert len(serialized) < 4096


@pytest.mark.parametrize(
    "path,value,stage,reason",
    [
        (("model",), [MODEL], "model_identity", "response_model_type_invalid"),
        (("answers",), [], "question_ids", "answers_not_object"),
        (("answers",), None, "question_ids", "answers_not_object"),
        (("answers", "team"), "private answer", "answer_schema", "answer_not_object"),
        (("answers", "team", "type"), [], "answer_schema", "answer_type_mismatch"),
        (
            ("answers", "team", "probabilities"),
            [0, 0.16, 0.84],
            "answer_distribution",
            "distribution_not_object",
        ),
        (("usage", "input_tokens"), "476", "usage_schema", "usage_tokens_invalid"),
        (("id",), "https://private.example/key", "response_identity", "response_id_invalid"),
        (("provider",), {"input": "private answer"}, "provider_identity", "provider_invalid"),
        (("error",), {"message": "private answer"}, "response_schema", "provider_error_present"),
    ],
)
def test_schema_failures_keep_unambiguous_amount(
    path, value, stage, reason, context, questions, response
):
    target = response
    for field in path[:-1]:
        target = target[field]
    target[path[-1]] = value
    with pytest.raises(RoutingError) as caught:
        parse(response, context, questions)
    details = caught.value.details
    assert details["validation_stage"] == stage
    assert details["reason_code"] == reason
    assert details["cost_unknown"] is False
    assert details["actual_cost_usd"] == "0.000019992"
    if path != ("id",):
        assert details["response_id"] == response["id"]
    serialized = json.dumps(caught.value.body())
    assert "private answer" not in serialized
    assert "https://private.example/key" not in serialized


@pytest.mark.parametrize(
    "field,alias,stage,reason",
    [
        ("model", "model_name", "model_identity", "response_model_missing"),
        ("answers", "choices", "question_ids", "answers_missing"),
        ("answers", "decisions", "question_ids", "answers_missing"),
        ("usage", "token_usage", "usage_schema", "usage_missing"),
    ],
)
def test_schema_aliases_are_diagnosed_without_being_accepted(
    field, alias, stage, reason, context, questions, response
):
    response[alias] = response.pop(field)
    with pytest.raises(RoutingError) as caught:
        parse(response, context, questions)
    details = caught.value.details
    assert details["validation_stage"] == stage
    assert details["reason_code"] == reason
    assert details["response_shape"]["other_key_count"] == 1
    assert field not in details["response_shape"]["known_keys"]
    assert details["response_id"] == response["id"]
    assert details["cost_unknown"] is (field == "usage")
    if field != "usage":
        assert details["actual_cost_usd"] == "0.000019992"


@pytest.mark.parametrize(
    "input_alias,output_alias",
    [("prompt_tokens", "completion_tokens"), ("inputTokens", "outputTokens")],
)
def test_usage_token_aliases_do_not_weaken_contract(
    input_alias, output_alias, context, questions, response
):
    response["usage"][input_alias] = response["usage"].pop("input_tokens")
    response["usage"][output_alias] = response["usage"].pop("output_tokens")
    with pytest.raises(RoutingError) as caught:
        parse(response, context, questions)
    details = caught.value.details
    assert details["reason_code"] == "usage_tokens_invalid"
    assert details["actual_cost_usd"] == "0.000019992"
    assert details["response_shape"]["usage"]["known_keys"] == ["cost"]
    assert details["response_shape"]["usage"]["other_key_count"] == 2


def test_model_diagnostic_uses_response_field_never_receipt(context, questions, response):
    response["receipt"] = {"model": RESOLVED, "provider": "receipt-only-provider"}
    response["model"] = "typesafe/jev-unapproved-revision"
    with pytest.raises(RoutingError) as caught:
        parse(response, context, questions)
    details = caught.value.details
    assert details["reason_code"] == "model_not_allowed"
    assert "actual_model" not in details
    assert "actual_provider" not in details
    assert (
        details["response_shape"]["model"]["sha256"]
        == hashlib.sha256(response["model"].encode()).hexdigest()
    )
    assert details["actual_cost_usd"] == "0.000019992"
    assert "receipt-only-provider" not in json.dumps(caught.value.body())


def test_question_diagnostic_uses_ordinal_and_shapes_not_values(context, questions, response):
    class SecretSentinel:
        def __repr__(self):
            return "sk-or-v1-private-score"

    response["answers"]["urgency"]["score"] = SecretSentinel()
    with pytest.raises(RoutingError) as caught:
        parse(response, context, questions)
    details = caught.value.details
    assert details["validation_stage"] == "answer_score"
    assert details["reason_code"] == "numeric_type_invalid"
    question = details["response_shape"]["question"]
    assert question["index"] == 2
    assert question["expected_type"] == "score"
    assert question["answer"]["fields"]["score"] == {"type": "other"}
    assert details["actual_model"] == RESOLVED
    assert "actual_provider" not in details
    serialized = json.dumps(caught.value.body())
    assert "private-score" not in serialized
    assert "SecretSentinel" not in serialized
    assert "Blocking revenue right now" not in serialized


def test_unknown_field_and_provider_cannot_leak_untrusted_text(context, questions, response):
    # Deliberately invalid, noncredential sentinel exercising redaction rules.
    redaction_marker = "sk-or-v1-diagnostic-secret"
    private_url = "https://private.example/path?key=" + redaction_marker
    response[private_url] = {"prompt": "private prompt"}
    response["provider"] = redaction_marker
    response["answers"]["team"][redaction_marker] = {"input": private_url}
    with pytest.raises(RoutingError) as caught:
        parse(response, context, questions)
    details = caught.value.details
    assert details["reason_code"] == "unexpected_answer_fields"
    assert details["response_shape"]["other_key_count"] == 1
    assert details["response_shape"]["question"]["answer"]["other_key_count"] == 1
    assert details["response_shape"]["provider"] == {"type": "string", "length": len(redaction_marker)}
    serialized = json.dumps(caught.value.body())
    for private in (redaction_marker, private_url, "private prompt", "payments", "Billing", "TypeSafe"):
        assert private not in serialized
    assert details["actual_cost_usd"] == "0.000019992"


@pytest.mark.parametrize("field", ["id", "model", "duplicate_key"])
def test_secret_like_identifiers_are_not_diagnostic_values(field, context, questions, response):
    # Deliberately invalid, noncredential sentinel exercising redaction rules.
    redaction_marker = "sk-or-v1-diagnostic-secret"
    if field == "id":
        response["id"] = "gen-" + redaction_marker
        raw = response
    elif field == "model":
        response["model"] = "typesafe/" + redaction_marker
        raw = response
    else:
        raw = json_text(response)[:-1] + f', "{redaction_marker}": 1, "{redaction_marker}": 2}}'
    with pytest.raises(RoutingError) as caught:
        parse(raw, context, questions)
    details = caught.value.details
    assert details["actual_cost_usd"] == "0.000019992"
    assert redaction_marker not in json.dumps(caught.value.body())
    if field == "id":
        assert details["reason_code"] == "response_id_invalid"
        assert "response_id" not in details
    elif field == "model":
        assert details["reason_code"] == "model_not_allowed"
        assert "actual_model" not in details
    else:
        assert details["reason_code"] == "duplicate_json_keys"
        assert details["response_shape"]["duplicate_known_fields"] == []


def test_duplicate_model_cannot_be_promoted_to_actual_model(context, questions, response):
    raw = json_text(response).replace('"model":', '"model": "other/model", "model":', 1)
    with pytest.raises(RoutingError) as caught:
        parse(raw, context, questions)
    details = caught.value.details
    assert details["reason_code"] == "duplicate_json_keys"
    assert details["response_shape"]["duplicate_known_fields"] == ["model"]
    assert "actual_model" not in details
    assert details["actual_cost_usd"] == "0.000019992"


def test_shape_diagnostics_are_bounded_for_many_unknown_question_ids(context, questions, response):
    private = "sk-or-v1-private-answer-" * 100
    response["answers"].update({f"{private}{i}": private for i in range(2000)})
    with pytest.raises(RoutingError) as caught:
        parse(response, context, questions)
    details = caught.value.details
    shape = details["response_shape"]["answers"]
    assert details["reason_code"] == "question_ids_mismatch"
    assert shape["count"] == 2003
    assert shape["expected_count"] == shape["matched_count"] == 3
    assert shape["unexpected_count"] == 2000
    assert shape["question_id_types"] == {"string": 64}
    assert shape["question_id_types_truncated"] is True
    serialized = json.dumps(caught.value.body())
    assert "private-answer" not in serialized
    assert len(serialized) < 4096


def test_shape_diagnostics_do_not_walk_nested_answer_values(context, questions, response):
    nested = "sk-or-v1-private-nested-answer"
    for _ in range(2000):
        nested = [nested]
    response["answers"]["team"] = nested
    with pytest.raises(RoutingError) as caught:
        parse(response, context, questions)
    details = caught.value.details
    assert details["reason_code"] == "answer_not_object"
    assert details["response_shape"]["question"]["answer"] == {"type": "array", "count": 1}
    serialized = json.dumps(caught.value.body())
    assert "private-nested-answer" not in serialized
    assert len(serialized) < 4096


def test_non_string_question_ids_are_counted_without_coercion(context, questions, response):
    response["answers"][17] = {"type": "noul", "noul": 0.5}
    with pytest.raises(RoutingError) as caught:
        parse(response, context, questions)
    shape = caught.value.details["response_shape"]["answers"]
    assert caught.value.details["reason_code"] == "question_ids_mismatch"
    assert shape["question_id_types"] == {"string": 3, "integer": 1}
    assert shape["unexpected_count"] == 1


@pytest.mark.parametrize(
    "private", ["gold", "gold_answer", "privateGrader", "hidden_tests", "split"]
)
def test_private_gold_never_enters_decision_state(context, private):
    with pytest.raises(RoutingError) as caught:
        replace(
            context,
            state={"task": "public", "sources": [{private: "private answer"}]},
            required_fields=("task",),
        )
    assert caught.value.code == "DECISION_PRIVATE_CONTEXT"
    assert "private answer" not in str(caught.value.body())


def test_context_rejects_whole_workload_objects_and_missing_requirements(context):
    with pytest.raises(RoutingError):
        replace(context, state={"task": "public", "acceptance": {"answer": "private"}})
    with pytest.raises(RoutingError) as caught:
        replace(context, state={"task": "public"})
    assert caught.value.code == "DECISION_CONTEXT_INCOMPLETE"


def test_state_and_payload_boundaries_do_not_truncate(context, questions):
    with pytest.raises(RoutingError) as caught:
        replace(
            context,
            state={"task": "條件" * 1000, "requirements": ["mandatory ending"]},
            max_state_bytes=100,
        )
    assert caught.value.code == "DECISION_CONTEXT_LIMIT"
    with pytest.raises(RoutingError) as caught:
        build_payload(context, questions, model=MODEL, max_payload_bytes=100)
    assert caught.value.code == "DECISION_CONTEXT_LIMIT"


@pytest.mark.parametrize("value", [float("nan"), float("inf"), Decimal("1")])
def test_state_rejects_non_json_or_nonfinite_numbers(context, value):
    with pytest.raises(RoutingError):
        replace(context, state={"task": "public", "observations": value}, required_fields=("task",))


def test_input_objects_and_payload_are_independently_frozen(context, questions):
    source = {"task": "original", "requirements": ["one"]}
    frozen = replace(context, state=source)
    source["requirements"].append("changed")
    assert frozen.state["requirements"] == ("one",)
    before = cache_key(frozen, questions, model=MODEL)
    payload = build_payload(frozen, questions, model=MODEL)
    payload["state"]["requirements"].append("changed again")
    payload["questions"]["team"]["criteria"]["account"] = "Changed"
    assert cache_key(frozen, questions, model=MODEL) == before
    with pytest.raises(TypeError):
        frozen.state["task"] = "changed"
    with pytest.raises(FrozenInstanceError):
        frozen.tenant_id = "other"


@pytest.mark.parametrize(
    "field",
    [
        "tenant_id",
        "state_version",
        "policy_version",
        "skill_version",
        "model_version",
    ],
)
def test_every_version_and_tenant_changes_cache_identity(field, context, questions):
    other = replace(context, **{field: "changed"})
    assert cache_key(other, questions, model=MODEL) != cache_key(context, questions, model=MODEL)


def test_canonical_cache_identity_and_contract_changes(context, questions):
    original = cache_key(context, questions, model=MODEL)
    reordered = replace(
        context, state={"requirements": ["text"], "task": "Route this public ticket"}
    )
    assert cache_key(reordered, tuple(reversed(questions)), model=MODEL) == original
    assert cache_key(context, questions, model="other/model") != original
    assert (
        cache_key(
            replace(context, state={"task": "changed", "requirements": ["text"]}),
            questions,
            model=MODEL,
        )
        != original
    )
    for q in (
        replace(questions[0], version="2"),
        replace(questions[0], instructions="Different question"),
    ):
        assert cache_key(context, (q, *questions[1:]), model=MODEL) != original


def test_duplicate_request_questions_and_invalid_question_contracts(context, questions):
    with pytest.raises(RoutingError):
        build_payload(context, [questions[0], questions[0]], model=MODEL)
    with pytest.raises(RoutingError):
        DecisionQuestion("decision", "choice", "Pick", {"only": "One option"})
    with pytest.raises(RoutingError):
        DecisionQuestion("decision", "score", "Rate", ["same", "same"])
    with pytest.raises(RoutingError):
        DecisionQuestion("decision", "noul", "True?", {"yes": "yes", "no": "no"})


@pytest.mark.parametrize("count", [2, 10])
def test_score_criteria_supplier_boundaries_round_trip(count, context):
    # TypeSafe's Score contract limits the ordered rubric to 2..10 levels:
    # https://docs.typesafe.ai/primitives/score
    criteria = [f"Level {index}" for index in range(count)]
    question = DecisionQuestion("severity", "score", "Rate severity", criteria)
    payload = build_payload(context, [question], model=MODEL)
    assert payload["questions"]["severity"]["criteria"] == criteria
    probabilities = {str(index): 0 for index in range(count)}
    probabilities[str(count - 1)] = 1
    raw = {
        "model": MODEL,
        "answers": {
            "severity": {
                "type": "score",
                "score": count - 1,
                "probabilities": probabilities,
                "legend": {str(index): text for index, text in enumerate(criteria)},
            }
        },
        "usage": {"input_tokens": 1, "output_tokens": 1, "cost": 0},
    }
    batch = parse_response(raw, context, [question], model=MODEL)
    assert batch.answers["severity"].score == count - 1
    assert not batch.answers["severity"].abstained
    assert batch.usage.actual_cost_usd == Decimal(0)


@pytest.mark.parametrize("count", [0, 1, 11, 255, 256])
def test_score_criteria_outside_supplier_limits_fail_before_dispatch(count):
    with pytest.raises(RoutingError) as caught:
        DecisionQuestion(
            "severity", "score", "Rate severity", [f"Level {index}" for index in range(count)]
        )
    assert caught.value.code == "DECISION_REQUEST_INVALID"


@pytest.mark.parametrize("count", [2, 255])
def test_choice_option_limits_remain_distinct_from_score(count):
    criteria = {f"option_{index}": f"Description {index}" for index in range(count)}
    question = DecisionQuestion("selection", "choice", "Select an option", criteria)
    assert question.payload()["criteria"] == criteria


@pytest.mark.parametrize("count", [1, 256])
def test_choice_outside_option_limits_is_rejected(count):
    with pytest.raises(RoutingError) as caught:
        DecisionQuestion(
            "selection",
            "choice",
            "Select an option",
            {f"option_{index}": f"Description {index}" for index in range(count)},
        )
    assert caught.value.code == "DECISION_REQUEST_INVALID"


@pytest.mark.parametrize("total", ["0.99", "1.01"])
def test_verified_choice_wire_rounding_keeps_original_values(total, context):
    questions = [DecisionQuestion("tier", "choice", "Choose a tier", {"low": "Lower", "mid": "Middle"})]
    upper = Decimal(total) - Decimal("0.80")
    raw = {"model": RESOLVED, "provider": "TypeSafe", "usage": {"input_tokens": 1, "output_tokens": 1, "cost": 0.001},
           "answers": {"tier": {"type": "choice", "choice": "low", "probabilities": {"low": Decimal("0.80"), "mid": upper}}}}
    batch = parse(raw, context, questions)
    assert batch.answers["tier"].probabilities == {"low": Decimal("0.80"), "mid": upper}
    assert batch.call_cost_usd == Decimal("0.001")


@pytest.mark.parametrize("provider", [None, "Other"])
def test_choice_rounding_requires_verified_provider(provider, context):
    questions = [DecisionQuestion("tier", "choice", "Choose a tier", {"low": "Lower", "mid": "Middle"})]
    raw = {"model": RESOLVED, "provider": provider, "usage": {"input_tokens": 1, "output_tokens": 1},
           "answers": {"tier": {"type": "choice", "choice": "low", "probabilities": {"low": Decimal("0.80"), "mid": Decimal("0.19")}}}}
    with pytest.raises(RoutingError):
        parse(raw, context, questions)


def test_score_distribution_does_not_use_choice_rounding(context):
    question = DecisionQuestion("rating", "score", "Rate it", ["low", "high"])
    raw = {"model": RESOLVED, "provider": "TypeSafe", "usage": {"input_tokens": 1, "output_tokens": 1},
           "answers": {"rating": {"type": "score", "score": 1, "probabilities": {"0": Decimal("0.05"), "1": Decimal("0.94")}}}}
    with pytest.raises(RoutingError):
        parse(raw, context, [question])

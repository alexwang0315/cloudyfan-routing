"""Typed, billable-call-neutral OpenRouter Decisions contracts.

Schema checked 2026-10-14 against the official Decisions reference and
OpenRouterTeam/typescript-sdk's decisions{choice,noul,score}answer models:
https://openrouter.ai/docs/api/api-reference/alphadecisions/submit-a-decisions-request
Score criteria are limited to 2..10 levels by the TypeSafe primitive contract:
https://docs.typesafe.ai/primitives/score

Noul is a probability, without confidence. Score is a number, not an integer;
its optional legend/probabilities use zero-based string keys. Optional missing
distributions are retained as missing and cause a per-question abstention.
Neither confidence nor a cache hit grants permission to execute a route.
"""

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import Any, Literal

from .errors import RoutingError

ENDPOINT = "/api/alpha/decisions"
QuestionType = Literal["choice", "noul", "score"]
_ID = re.compile(r"[a-z][a-z0-9_]{0,127}\Z")
_TRACE_ID = re.compile(r"(?:gen|req)-[A-Za-z0-9_-]{1,128}\Z")
_MODEL_ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}/[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_SECRET_MARKER = re.compile(
    r"(?:^|[^A-Za-z0-9])(?:sk[-_]|api[-_]?key|bearer|github_pat_|gh[pousr]_|xox[baprs]-|AKIA)",
    re.IGNORECASE,
)
_PRIVATE_FIELDS = frozenset(
    {
        "gold",
        "gold_answer",
        "gold_answers",
        "expected_answer",
        "expected_answers",
        "private_grader",
        "private_acceptance",
        "hidden_tests",
        "hidden_errors",
        "holdout",
        "holdout_answer",
        "split",
        "other_arm_answers",
        "other_group_answers",
    }
)
_PUBLIC_FIELDS = frozenset(
    {
        "task",
        "step",
        "requirements",
        "constraints",
        "sources",
        "evidence",
        "public_checks",
        "capabilities",
        "tools",
        "skills",
        "candidates",
        "observations",
        "missing_information",
        "artifacts",
        "history",
        "requested_output",
        "language",
        "method_options",
        "checkpoint",
        "remaining_budget_usd",
        "remaining_time_ms",
    }
)
_PROBABILITY_TOLERANCE = Decimal("0.000001")
JEV_CHOICE_PROBABILITY_DECIMALS = 2
_ROUNDED_CHOICE_MODELS = frozenset({
    "typesafe/jev-1.13", "typesafe/jev-1.13-20260917",
})
_MISSING = object()
_RESPONSE_FIELDS = frozenset({"id", "model", "provider", "usage", "answers", "error"})
_ANSWER_FIELDS = frozenset(
    {"type", "choice", "noul", "score", "probabilities", "confidence", "legend"}
)
_USAGE_FIELDS = frozenset({"input_tokens", "output_tokens", "cost"})


def _invalid(message: str, code: str = "DECISION_REQUEST_INVALID") -> RoutingError:
    return RoutingError(code, message)


def _text(value: object, name: str, *, limit: int = 256) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise _invalid(f"Invalid {name}.")
    return value


def _freeze(value: Any, depth: int = 0) -> Any:
    if depth > 32:
        raise _invalid("Decision JSON exceeds the nesting limit.")
    if isinstance(value, Mapping):
        if any(not isinstance(k, str) for k in value):
            raise _invalid("Decision JSON keys must be strings.")
        return MappingProxyType({k: _freeze(v, depth + 1) for k, v in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(v, depth + 1) for v in value)
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    raise _invalid("Decision state must contain finite JSON values.")


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return value


def _canonical(value: Any) -> str:
    return json.dumps(
        _plain(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _reject_private(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized = re.sub(r"(?<!^)(?=[A-Z])", "_", key).lower().replace("-", "_")
            if normalized in _PRIVATE_FIELDS:
                raise _invalid(
                    "Private evaluation fields cannot enter decision context.",
                    "DECISION_PRIVATE_CONTEXT",
                )
            _reject_private(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            _reject_private(child)


@dataclass(frozen=True)
class DecisionQuestion:
    question_id: str
    type: QuestionType
    instructions: str
    criteria: Mapping[str, str | None] | Sequence[str] | None = None
    version: str = "1"
    abstain_choices: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.question_id, str) or not _ID.fullmatch(self.question_id):
            raise _invalid("Invalid question ID.")
        _text(self.instructions, "question instructions", limit=4000)
        _text(self.version, "question version")
        if self.type not in {"choice", "noul", "score"}:
            raise _invalid("Unsupported question type.")
        criteria = self.criteria
        if self.type == "choice":
            if not isinstance(criteria, Mapping) or not 2 <= len(criteria) <= 255:
                raise _invalid("Choice requires 2 to 255 explicit choices.")
            for key, description in criteria.items():
                if not isinstance(key, str) or not _ID.fullmatch(key):
                    raise _invalid("Invalid choice ID.")
                if description is not None:
                    _text(description, "choice description", limit=4000)
        elif self.type == "noul":
            if criteria is not None:
                if not isinstance(criteria, Mapping) or set(criteria) != {"true", "false"}:
                    raise _invalid("Noul criteria must contain exactly true and false.")
                for description in criteria.values():
                    _text(description, "noul criterion", limit=4000)
        elif not isinstance(criteria, (list, tuple)) or not 2 <= len(criteria) <= 10:
            raise _invalid("Score requires an ordered list of 2 to 10 descriptions.")
        else:
            for description in criteria:
                _text(description, "score criterion", limit=4000)
            if len(set(criteria)) != len(criteria):
                raise _invalid("Score criteria must not repeat.")
        abstentions = tuple(self.abstain_choices)
        if len(set(abstentions)) != len(abstentions) or any(
            self.type != "choice" or choice not in criteria for choice in abstentions
        ):
            raise _invalid("Abstention choices must be unique declared choice IDs.")
        object.__setattr__(self, "abstain_choices", abstentions)
        object.__setattr__(self, "criteria", _freeze(criteria))

    def payload(self) -> dict:
        result = {"type": self.type, "instructions": self.instructions}
        if self.criteria is not None:
            result["criteria"] = _plain(self.criteria)
        return result


@dataclass(frozen=True)
class DecisionContext:
    """An explicit public projection; never pass a benchmark/workflow object wholesale.

    required_fields are top-level fields the caller needs for this decision.
    Oversize or absent requirements fail before dispatch; nothing is truncated.
    The allowlist and reserved private-field checks catch structural gold leaks,
    but the caller still owns provenance of free-form task/source text.
    """

    tenant_id: str
    state: Mapping[str, Any]
    state_version: str
    policy_version: str
    skill_version: str
    model_version: str
    required_fields: tuple[str, ...] = ()
    max_state_bytes: int = 16384
    state_hash: str = field(init=False)

    def __post_init__(self) -> None:
        for name in (
            "tenant_id",
            "state_version",
            "policy_version",
            "skill_version",
            "model_version",
        ):
            _text(getattr(self, name), name)
        if type(self.max_state_bytes) is not int or not 1 <= self.max_state_bytes <= 131072:
            raise _invalid("Invalid decision state limit.")
        if not isinstance(self.state, Mapping) or not self.state:
            raise _invalid("Decision state must be a nonempty public object.")
        state = _freeze(self.state)
        _reject_private(state)
        if not set(state) <= _PUBLIC_FIELDS:
            raise _invalid("Decision state contains a field outside its public allowlist.")
        required = tuple(self.required_fields)
        if len(set(required)) != len(required) or any(
            not isinstance(key, str) or key not in state or state[key] in (None, "", (), {})
            for key in required
        ):
            raise _invalid(
                "A required decision context field is absent or empty.",
                "DECISION_CONTEXT_INCOMPLETE",
            )
        encoded = _canonical(state).encode("utf-8")
        if len(encoded) > self.max_state_bytes:
            raise _invalid(
                "Decision context exceeds its byte limit; no content was truncated.",
                "DECISION_CONTEXT_LIMIT",
            )
        object.__setattr__(self, "state", state)
        object.__setattr__(self, "required_fields", required)
        object.__setattr__(self, "state_hash", hashlib.sha256(encoded).hexdigest())


def _questions(questions: Sequence[DecisionQuestion]) -> tuple[DecisionQuestion, ...]:
    result = tuple(questions)
    if not 1 <= len(result) <= 32 or any(not isinstance(q, DecisionQuestion) for q in result):
        raise _invalid("A decision batch requires 1 to 32 typed questions.")
    if len({q.question_id for q in result}) != len(result):
        raise _invalid("Duplicate decision question IDs.")
    return result


def cache_key(
    context: DecisionContext, questions: Sequence[DecisionQuestion], *, model: str
) -> str:
    """Exact opaque key; every input/version and tenant participates in the hash."""
    _text(model, "decision model")
    specs = _questions(questions)
    return _hash(
        {
            "adapter": "decision-v2.1",
            "tenant": context.tenant_id,
            "state_hash": context.state_hash,
            "state_version": context.state_version,
            "policy_version": context.policy_version,
            "skill_version": context.skill_version,
            "model": model,
            "model_version": context.model_version,
            "required_fields": sorted(context.required_fields),
            "questions": {
                q.question_id: {
                    **q.payload(),
                    "version": q.version,
                    "abstain_choices": list(q.abstain_choices),
                }
                for q in specs
            },
        }
    )


def build_payload(
    context: DecisionContext,
    questions: Sequence[DecisionQuestion],
    *,
    model: str,
    provider_options: Mapping[str, Any] | None = None,
    max_payload_bytes: int = 32768,
) -> dict:
    """Build without network/quotation/reservation. Runner quotes these exact bytes."""
    _text(model, "decision model")
    specs = _questions(questions)
    options = _plain(_freeze(provider_options or {}))
    if not isinstance(options, dict) or options.get("allow_fallbacks", False) is not False:
        raise _invalid("Decisions provider fallback must stay disabled.")
    options["allow_fallbacks"] = False
    payload = {
        "model": model,
        "state": _plain(context.state),
        "questions": {q.question_id: q.payload() for q in specs},
        "provider": options,
    }
    if type(max_payload_bytes) is not int or max_payload_bytes <= 0:
        raise _invalid("Invalid decision payload limit.")
    if len(_canonical(payload).encode("utf-8")) > max_payload_bytes:
        raise _invalid(
            "Decision payload exceeds its byte limit; no content was truncated.",
            "DECISION_CONTEXT_LIMIT",
        )
    return payload


@dataclass(frozen=True)
class DecisionUsage:
    input_tokens: int
    output_tokens: int
    actual_cost_usd: Decimal | None


@dataclass(frozen=True)
class DecisionAnswer:
    question_id: str
    type: QuestionType
    choice: str | None = None
    noul: Decimal | None = None
    score: Decimal | None = None
    probabilities: Mapping[str, Decimal] | None = None
    confidence: Decimal | None = None
    legend: Mapping[str, str] | None = None
    abstain_reason: str | None = None

    @property
    def abstained(self) -> bool:
        return self.abstain_reason is not None


@dataclass(frozen=True)
class DecisionBatch:
    answers: Mapping[str, DecisionAnswer]
    usage: DecisionUsage
    model: str
    response_id: str | None
    provider: str | None
    cache_key: str
    state_hash: str
    question_versions: Mapping[str, str]
    state_version: str
    policy_version: str
    skill_version: str
    model_version: str
    cache_hit: bool = False
    requires_hard_gate_recheck: Literal[True] = field(default=True, init=False)

    @property
    def call_cost_usd(self) -> Decimal | None:
        """A cache replay is free; usage retains the ORIGINAL call's receipt."""
        return Decimal(0) if self.cache_hit else self.usage.actual_cost_usd


class _ResponseIssue(ValueError):
    """Only fixed parser-owned labels; never carry a response value or exception text."""

    def __init__(self, stage: str, reason: str) -> None:
        super().__init__(reason)
        self.stage = stage
        self.reason = reason


def _value_shape(value: object) -> dict:
    """No repr, values, class names, or recursive descent into untrusted content."""
    if value is _MISSING:
        return {"type": "missing"}
    if value is None:
        return {"type": "null"}
    if isinstance(value, Mapping):
        return {"type": "object", "count": len(value)}
    if isinstance(value, (list, tuple)):
        return {"type": "array", "count": len(value)}
    if isinstance(value, str):
        return {"type": "string", "length": len(value)}
    if isinstance(value, bool):
        return {"type": "boolean"}
    if isinstance(value, int):
        return {"type": "integer"}
    if isinstance(value, (float, Decimal)):
        return {"type": "number"}
    return {"type": "other"}


def _object_shape(value: object, known_fields: frozenset[str]) -> dict:
    result = _value_shape(value)
    if isinstance(value, Mapping):
        # Only literal schema keys are emitted. Even a JSON key can contain a
        # prompt, URL, or credential, so unknown names are represented by a count.
        keys = sorted(key for key in known_fields if key in value)
        result.update(
            known_keys=keys,
            other_key_count=len(value) - len(keys),
            fields={key: _value_shape(value[key]) for key in keys},
        )
    return result


def _response_shape(
    data: object,
    specs: Sequence[DecisionQuestion],
    *,
    model: str,
    allowed_response_models: Sequence[str],
    duplicates: set[str],
    question_index: int | None,
) -> dict:
    """Bounded shallow diagnostics, independent of response size and answer text."""
    result = _object_shape(data, _RESPONSE_FIELDS)
    result["duplicate_key_count"] = len(duplicates)
    result["duplicate_known_fields"] = sorted(
        duplicates & (_RESPONSE_FIELDS | _ANSWER_FIELDS | _USAGE_FIELDS)
    )
    if not isinstance(data, Mapping):
        return result
    reported_model = data.get("model", _MISSING)
    identity = _value_shape(reported_model)
    if isinstance(reported_model, str):
        requested = reported_model == model
        # Match individual frozen IDs, as the parser does. In particular an
        # accidentally supplied string must never grant substring membership.
        allowed_alias = any(reported_model == candidate for candidate in allowed_response_models)
        identity.update(matches_requested=requested, matches_allowed_alias=allowed_alias)
        if len(reported_model) <= 256:
            # A rejected alias can be compared to frozen IDs without exposing an
            # arbitrary string. This hashes the response field, never a receipt.
            identity["sha256"] = hashlib.sha256(
                reported_model.encode("utf-8", errors="surrogatepass")
            ).hexdigest()
        if (
            (requested or allowed_alias)
            and _MODEL_ID.fullmatch(reported_model)
            and not _SECRET_MARKER.search(reported_model)
            and "model" not in duplicates
        ):
            identity["allowlisted_value"] = reported_model
    result["model"] = identity
    # There is no frozen provider allowlist in this interface. Its free-form
    # response value is therefore never promoted to a trusted provider ID.
    result["provider"] = _value_shape(data.get("provider", _MISSING))
    result["usage"] = _object_shape(data.get("usage", _MISSING), _USAGE_FIELDS)
    answers = data.get("answers", _MISSING)
    answer_shape = _value_shape(answers)
    answer_shape["expected_count"] = len(specs)
    if isinstance(answers, Mapping):
        matched = sum(q.question_id in answers for q in specs)
        key_types: dict[str, int] = {}
        # Inspect only the first 64 keys. No actual question ID is emitted,
        # including IDs supplied by the caller; the failing ordinal is enough.
        for index, key in enumerate(answers):
            if index == 64:
                break
            kind = _value_shape(key)["type"]
            key_types[kind] = key_types.get(kind, 0) + 1
        answer_shape.update(
            matched_count=matched,
            missing_count=len(specs) - matched,
            unexpected_count=len(answers) - matched,
            question_id_types=key_types,
            question_id_types_truncated=len(answers) > 64,
        )
        if question_index is not None:
            question = specs[question_index]
            answer = answers.get(question.question_id, _MISSING)
            result["question"] = {
                "index": question_index,
                "expected_type": question.type,
                "answer": _object_shape(answer, _ANSWER_FIELDS),
            }
    result["answers"] = answer_shape
    return result


def _number(value: Any, *, probability: bool = False, stage: str = "usage_cost") -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise _ResponseIssue(stage, "numeric_type_invalid")
    result = Decimal(str(value))
    if not result.is_finite():
        raise _ResponseIssue(stage, "numeric_not_finite")
    if result < 0:
        raise _ResponseIssue(stage, "numeric_negative")
    if probability and result > 1:
        raise _ResponseIssue(stage, "probability_out_of_range")
    return result


def _decode(raw: object) -> tuple[object, set[str]]:
    duplicates: set[str] = set()

    def pairs(items: list[tuple[str, Any]]) -> dict:
        result = {}
        for key, value in items:
            if key in result:
                duplicates.add(key)
            result[key] = value
        return result

    if isinstance(raw, (str, bytes, bytearray)):
        raw = json.loads(raw, parse_float=Decimal, object_pairs_hook=pairs)
    return raw, duplicates


def _evidence(raw: object, duplicates: set[str] | None = None) -> dict:
    result: dict = {"cost_unknown": True}
    duplicates = duplicates or set()
    if not isinstance(raw, Mapping):
        return result
    response_id = raw.get("id")
    if (
        "id" not in duplicates
        and isinstance(response_id, str)
        and _TRACE_ID.fullmatch(response_id)
        and not _SECRET_MARKER.search(response_id)
    ):
        result["response_id"] = response_id
    usage = raw.get("usage")
    if not isinstance(usage, Mapping) or "usage" in duplicates:
        return result
    for field_name in ("input_tokens", "output_tokens"):
        value = usage.get(field_name)
        if field_name not in duplicates and type(value) is int and value >= 0:
            result[field_name] = value
    if "cost" not in duplicates:
        try:
            cost = _number(usage.get("cost"))
            result.update(
                cost_unknown=False, actual_cost_usd=str(cost), decision_cost_usd=str(cost)
            )
        except (ValueError, TypeError, InvalidOperation):
            pass
    return result


def choice_probability_decimals(
    *, model: str, response_model: str, provider: object,
) -> int | None:
    """The verified Jev 1.13 wire contract; do not infer precision for other models."""
    if (model in _ROUNDED_CHOICE_MODELS and response_model in _ROUNDED_CHOICE_MODELS
            and isinstance(provider, str) and provider in {"TypeSafe", "typesafe"}):
        return JEV_CHOICE_PROBABILITY_DECIMALS
    return None


def probability_sum_tolerance(option_count: int, *, decimals: int | None = None) -> Decimal:
    """Sum of per-option rounding bounds, matching the AI SDK decision validator."""
    if decimals is None:
        return _PROBABILITY_TOLERANCE
    return _PROBABILITY_TOLERANCE + option_count * Decimal("0.5") * Decimal(10) ** -decimals


def _distribution(
    raw: object, expected: set[str], *, probability_decimals: int | None = None,
) -> Mapping[str, Decimal]:
    if not isinstance(raw, Mapping):
        raise _ResponseIssue("answer_distribution", "distribution_not_object")
    if set(raw) != expected:
        raise _ResponseIssue("answer_distribution", "distribution_keys_mismatch")
    result = {
        key: _number(value, probability=True, stage="answer_distribution")
        for key, value in raw.items()
    }
    if abs(sum(result.values()) - 1) > probability_sum_tolerance(
        len(expected), decimals=probability_decimals,
    ):
        raise _ResponseIssue("answer_distribution", "distribution_not_normalized")
    return MappingProxyType(result)


def _answer(
    question: DecisionQuestion, raw: object, *, choice_decimals: int | None = None,
) -> DecisionAnswer:
    if not isinstance(raw, Mapping):
        raise _ResponseIssue("answer_schema", "answer_not_object")
    if raw.get("type") != question.type:
        raise _ResponseIssue("answer_schema", "answer_type_mismatch")
    if question.type == "noul":
        if set(raw) != {"type", "noul"}:
            raise _ResponseIssue("answer_schema", "noul_fields_mismatch")
        return DecisionAnswer(
            question.question_id,
            "noul",
            noul=_number(raw["noul"], probability=True, stage="answer_noul"),
        )
    allowed = {"type", "confidence", "probabilities"}
    allowed |= {"choice"} if question.type == "choice" else {"score", "legend"}
    if not set(raw) <= allowed:
        raise _ResponseIssue("answer_schema", "unexpected_answer_fields")
    confidence = (
        _number(raw["confidence"], probability=True, stage="answer_confidence")
        if "confidence" in raw
        else None
    )
    expected = (
        set(question.criteria)
        if question.type == "choice"
        else {str(i) for i in range(len(question.criteria))}
    )
    probabilities = (
        _distribution(
            raw["probabilities"], expected,
            probability_decimals=choice_decimals if question.type == "choice" else None,
        ) if "probabilities" in raw else None
    )
    abstain = "distribution_missing" if probabilities is None else None
    if question.type == "choice":
        choice = raw.get("choice")
        if not isinstance(choice, str) or choice not in expected:
            raise _ResponseIssue("answer_choice", "choice_not_allowed")
        if choice in question.abstain_choices:
            abstain = "explicit_abstention_choice"
        return DecisionAnswer(
            question.question_id,
            "choice",
            choice=choice,
            confidence=confidence,
            probabilities=probabilities,
            abstain_reason=abstain,
        )
    score = _number(raw.get("score"), stage="answer_score")
    if score > len(question.criteria) - 1:
        raise _ResponseIssue("answer_score", "score_out_of_range")
    legend = raw.get("legend")
    if "legend" in raw:
        expected_legend = {str(i): value for i, value in enumerate(question.criteria)}
        if not isinstance(legend, Mapping) or dict(legend) != expected_legend:
            raise _ResponseIssue("answer_legend", "legend_mismatch")
        legend = MappingProxyType(dict(legend))
    return DecisionAnswer(
        question.question_id,
        "score",
        score=score,
        confidence=confidence,
        probabilities=probabilities,
        legend=legend,
        abstain_reason=abstain,
    )


def parse_response(
    raw: object,
    context: DecisionContext,
    questions: Sequence[DecisionQuestion],
    *,
    model: str,
    allowed_response_models: Sequence[str] = (),
) -> DecisionBatch:
    """Validate exact IDs/types/options; retain billing evidence even on rejection.

    Pass raw JSON for duplicate-key detection. A previously parsed dict cannot
    recover duplicate keys discarded by another decoder; pass the saved raw response.
    Errors retain DECISION_RESPONSE_INVALID and receipt evidence, adding fixed
    validation_stage/reason_code labels and a bounded response_shape. The latter
    contains only known field names, types/counts, a question ordinal, and model
    identity matching the caller's frozen allowlist (or an opaque fingerprint).
    No raw answer, unknown key, provider string, or exception message is retained.
    """
    specs = _questions(questions)
    duplicates: set[str] = set()
    data: object = _MISSING
    stage = "json_decode"
    question_index = None
    try:
        data, duplicates = _decode(raw)
        stage = "response_schema"
        if not isinstance(data, Mapping):
            raise _ResponseIssue(stage, "response_not_object")
        if duplicates:
            raise _ResponseIssue(stage, "duplicate_json_keys")
        if "error" in data:
            raise _ResponseIssue(stage, "provider_error_present")
        stage = "model_identity"
        if "model" not in data:
            raise _ResponseIssue(stage, "response_model_missing")
        if not isinstance(data["model"], str):
            raise _ResponseIssue(stage, "response_model_type_invalid")
        if data["model"] not in {model, *allowed_response_models}:
            raise _ResponseIssue(stage, "model_not_allowed")
        stage = "question_ids"
        if "answers" not in data:
            raise _ResponseIssue(stage, "answers_missing")
        answers = data["answers"]
        if not isinstance(answers, Mapping):
            raise _ResponseIssue(stage, "answers_not_object")
        if set(answers) != {q.question_id for q in specs}:
            raise _ResponseIssue(stage, "question_ids_mismatch")
        stage = "answer_schema"
        parsed = {}
        choice_decimals = choice_probability_decimals(
            model=model, response_model=data["model"], provider=data.get("provider"),
        )
        for index, question in enumerate(specs):
            question_index = index
            parsed[question.question_id] = _answer(
                question, answers[question.question_id], choice_decimals=choice_decimals,
            )
        question_index = None
        stage = "usage_schema"
        if "usage" not in data:
            raise _ResponseIssue(stage, "usage_missing")
        usage = data["usage"]
        if not isinstance(usage, Mapping):
            raise _ResponseIssue(stage, "usage_not_object")
        if any(
            type(usage.get(k)) is not int or usage[k] < 0 for k in ("input_tokens", "output_tokens")
        ):
            raise _ResponseIssue(stage, "usage_tokens_invalid")
        stage = "usage_cost"
        cost = _number(usage["cost"]) if "cost" in usage else None
        stage = "response_identity"
        response_id = data.get("id")
        if response_id is not None and (
            not isinstance(response_id, str)
            or not _TRACE_ID.fullmatch(response_id)
            or _SECRET_MARKER.search(response_id)
        ):
            raise _ResponseIssue(stage, "response_id_invalid")
        stage = "provider_identity"
        provider = data.get("provider")
        if provider is not None and (
            not isinstance(provider, str) or not 1 <= len(provider) <= 128
        ):
            raise _ResponseIssue(stage, "provider_invalid")
    except (
        KeyError,
        ValueError,
        TypeError,
        ArithmeticError,
        UnicodeError,
        RecursionError,
    ) as error:
        if isinstance(error, _ResponseIssue):
            stage, reason = error.stage, error.reason
        elif stage == "json_decode":
            reason = "invalid_encoding" if isinstance(error, UnicodeError) else "invalid_json"
            if isinstance(error, RecursionError):
                reason = "json_nesting_limit"
        else:
            reason = "validation_failed"
        details = _evidence(data, duplicates)
        details.update(
            validation_stage=stage,
            reason_code=reason,
            response_shape=_response_shape(
                data,
                specs,
                model=model,
                allowed_response_models=allowed_response_models,
                duplicates=duplicates,
                question_index=question_index,
            ),
        )
        actual_model = details["response_shape"].get("model", {}).get("allowlisted_value")
        if actual_model is not None:
            details["actual_model"] = actual_model
        raise RoutingError(
            "DECISION_RESPONSE_INVALID",
            "Decisions returned invalid structured data.",
            502,
            details,
        ) from None
    return DecisionBatch(
        answers=MappingProxyType(parsed),
        usage=DecisionUsage(usage["input_tokens"], usage["output_tokens"], cost),
        model=data["model"],
        response_id=response_id,
        provider=provider,
        cache_key=cache_key(context, specs, model=model),
        state_hash=context.state_hash,
        question_versions=MappingProxyType({q.question_id: q.version for q in specs}),
        state_version=context.state_version,
        policy_version=context.policy_version,
        skill_version=context.skill_version,
        model_version=context.model_version,
    )

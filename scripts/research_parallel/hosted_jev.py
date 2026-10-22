"""Pure request/repair/response adapter for an independent hosted-Jev arm.

No HTTP implementation, credential reader, workflow runner or retry exists here.
The caller owns durable intents, admission, public SQL validation and attempt caps.
Reference: https://openrouter.ai/docs/guides/routing/routers/jev-router
"""

import json
import re
from copy import deepcopy
from decimal import Decimal, InvalidOperation
from fnmatch import fnmatchcase
from typing import Protocol

VERSION = "hosted-jev-medium-packet-v1"
URL = "https://openrouter.ai/api/v1/chat/completions"
MODEL = "typesafe/jev-router"
POLICY = {
    "version": VERSION,
    "model": MODEL,
    "cost_tier": "medium",
    "maximum_answer_attempts": 2,
    "self_built_jev_calls": 0,
    "automatic_transport_retries": 0,
    "cost_tier_is_price_cap": False,
    "include_preferences_are_hard_allowlists": False,
    "excluded_models_are_hard": True,
    "advisor_cost_added_separately": False,
    "estimated_cost_is_billed_cost": False,
}


class HostedJevError(ValueError):
    pass


class InjectedClient(Protocol):
    """Explicit integration point; an existing fixed-model client needs a wrapper."""

    def call(self, call_id, url, payload, *, headers): ...


def _strict(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise HostedJevError("Duplicate response JSON key")
            result[key] = value
        return result

    try:
        value = json.loads(
            raw,
            object_pairs_hook=pairs,
            parse_constant=lambda _: (_ for _ in ()).throw(
                HostedJevError("Nonfinite response JSON")
            ),
        )
    except (ValueError, TypeError) as error:
        raise HostedJevError("Invalid response JSON") from error
    if not isinstance(value, dict):
        raise HostedJevError("Response must be an object")
    return value


def _patterns(value):
    if (
        not isinstance(value, (list, tuple))
        or len(value) > 1024
        or any(not isinstance(x, str) or not 1 <= len(x) <= 1024 for x in value)
        or sum(map(len, value)) > 65536
    ):
        raise HostedJevError("Invalid frozen model preference list")
    return tuple(value)


def _matches(model, pattern):
    if not isinstance(model, str):
        return False
    if pattern.startswith("~"):
        # Resolving a moving alias needs separately captured catalog evidence.
        return None
    if any(c in pattern for c in "*?["):
        return fnmatchcase(model, pattern)
    return model == pattern or bool(re.fullmatch(re.escape(pattern) + r"-\d{8}", model))


def _messages(messages):
    if not isinstance(messages, list) or not messages:
        raise HostedJevError("Nonempty common text messages required")
    result = deepcopy(messages)
    if any(
        not isinstance(m, dict)
        or set(m) != {"role", "content"}
        or m["role"] not in ("system", "user", "assistant")
        or not isinstance(m["content"], str)
        for m in result
    ):
        raise HostedJevError("Only explicit text messages are supported by this SQL adapter")
    return result


class HostedJevAdapter:
    def __init__(
        self,
        *,
        output_contract,
        requested_models=(),
        allowed_models=(),
        excluded_models=(),
        allowed_providers=(),
        provider_only=(),
        max_tokens=4096,
        accounting_verifier=None,
    ):
        if not isinstance(output_contract, str) or not output_contract.strip():
            raise HostedJevError("Explicit common output contract required")
        if type(max_tokens) is not int or max_tokens < 1:
            raise HostedJevError("Positive frozen max_tokens required")
        if accounting_verifier is not None and not callable(accounting_verifier):
            raise HostedJevError("Accounting verifier must be explicitly injected")
        self.output_contract = output_contract
        self.models, self.allowed, self.excluded = (
            _patterns(requested_models),
            _patterns(allowed_models),
            _patterns(excluded_models),
        )
        # These are exact response provider names, not guessed endpoint tags.
        self.providers = _patterns(allowed_providers)
        self.provider_only = _patterns(provider_only)
        if bool(self.providers) != bool(self.provider_only):
            raise HostedJevError(
                "Wire provider_only slugs and response provider names must both be explicit"
            )
        self.max_tokens, self.accounting_verifier = max_tokens, accounting_verifier

    def descriptor(self):
        return {
            **deepcopy(POLICY),
            "models": list(self.models),
            "allowed_models": list(self.allowed),
            "excluded_models": list(self.excluded),
            "allowed_provider_names": list(self.providers),
            "provider_only": list(self.provider_only),
            "max_tokens": self.max_tokens,
            "output_contract": self.output_contract,
        }

    def build_request(self, task=None, *, messages=None, system=None, attempt=0):
        if type(attempt) is not int or attempt not in (0, 1):
            raise HostedJevError("Hosted arm allows at most two answer attempts")
        if messages is not None and (task is not None or system is not None):
            raise HostedJevError("Provide common messages OR task/system, not both")
        if messages is None:
            prompt = task.get("prompt") if isinstance(task, dict) else task
            if not isinstance(prompt, str) or not prompt.strip():
                raise HostedJevError("Original public task required")
            messages = [{"role": "user", "content": prompt}]
            if system is not None:
                if not isinstance(system, str):
                    raise HostedJevError("Common system prompt must be text")
                messages.insert(0, {"role": "system", "content": system})
        plugin = {"id": "jev-router", "cost_tier": "medium"}
        for key, values in (
            ("models", self.models),
            ("allowed_models", self.allowed),
            ("excluded_models", self.excluded),
        ):
            if values:
                plugin[key] = list(values)
        payload = {
            "model": MODEL,
            "plugins": [plugin],
            "messages": _messages(messages),
            "max_tokens": self.max_tokens,
        }
        if self.providers:
            payload["provider"] = {"only": list(self.provider_only), "allow_fallbacks": False}
        return {
            "url": URL,
            "headers": {"Content-Type": "application/json", "X-OpenRouter-Metadata": "enabled"},
            "payload": payload,
            "attempt": attempt,
            "policy": self.descriptor(),
        }

    def submit(self, client: InjectedClient, call_id, packet):
        if packet.get("policy") != self.descriptor() or packet.get("url") != URL:
            raise HostedJevError("Frozen hosted request policy mismatch")
        expected = self.build_request(
            messages=packet["payload"]["messages"], attempt=packet["attempt"]
        )
        if packet != expected:
            raise HostedJevError("Hosted request packet changed")
        # Exactly one injected call. No fallback or retry is performed here.
        return client.call(
            call_id, packet["url"], deepcopy(packet["payload"]), headers=deepcopy(packet["headers"])
        )

    def repair_messages(self, original_messages, previous_answer, public_diagnostic=None):
        original = _messages(original_messages)
        if previous_answer is None or (
            isinstance(previous_answer, str) and not previous_answer.strip()
        ):
            return original + [
                {
                    "role": "user",
                    "content": (
                        "The previous provider returned no usable answer. "
                        "Answer the original task from the beginning. Output contract: "
                    )
                    + self.output_contract,
                }
            ]
        if not isinstance(previous_answer, str):
            raise HostedJevError("Previous answer must be actual saved text or absent")
        diagnostic = public_diagnostic or {}
        if not isinstance(diagnostic, dict) or set(diagnostic) - {
            "passed",
            "checks",
            "diagnostic_source",
            "diagnostic_version",
        }:
            raise HostedJevError("Only the public diagnostic projection is allowed")
        checks = diagnostic.get("checks", [])
        if not isinstance(checks, list) or any(
            not isinstance(code, str) or re.fullmatch(r"[A-Z0-9_]{1,100}", code) is None
            for code in checks
        ):
            raise HostedJevError("Only public diagnostic codes are allowed")
        if diagnostic.get("passed") is True:
            raise HostedJevError("A publicly passed answer does not require repair")
        public = {"checks": deepcopy(checks)}
        return original + [
            {"role": "assistant", "content": previous_answer},
            {
                "role": "user",
                "content": (
                    "Correct the saved answer using only these public validation diagnostics: "
                )
                + json.dumps(public, sort_keys=True)
                + ". Output contract: "
                + self.output_contract,
            },
        ]

    def parse_response(self, response, *, request=None, accounting_proof=None, http_status=None):
        if not isinstance(response, dict):
            raise HostedJevError("Saved response mapping required")
        saved = deepcopy(response)
        if "raw" in response:
            body = _strict(response["raw"]) if response["raw"] else {}
            status = response.get("http_status")
        else:
            body, status = deepcopy(response), http_status
        model, provider, generation_id = body.get("model"), body.get("provider"), body.get("id")
        metadata = body.get("openrouter_metadata")
        pipeline = metadata.get("pipeline", []) if isinstance(metadata, dict) else []
        stages = (
            [
                deepcopy(stage["data"])
                for stage in pipeline
                if isinstance(stage, dict)
                and stage.get("name") == "jev-router"
                and isinstance(stage.get("data"), dict)
            ]
            if isinstance(pipeline, list)
            else []
        )
        routing = stages[-1] if stages else {}
        choices = body.get("choices")
        answer = (
            choices[0].get("message", {}).get("content")
            if isinstance(choices, list)
            and choices
            and isinstance(choices[0], dict)
            and isinstance(choices[0].get("message"), dict)
            else None
        )
        if not isinstance(answer, str) or not answer.strip():
            answer = None
        scope_reasons, violations = [], []
        includes = self.models + self.allowed
        include_matches = [_matches(model, pattern) for pattern in includes]
        if includes and True not in include_matches:
            scope_reasons.append(
                "actual_model_outside_include_preferences"
                if None not in include_matches
                else "moving_alias_not_independently_resolved"
            )
        if any(stage.get("list_fallback") == "models_ignored" for stage in stages):
            scope_reasons.append("include_preferences_ignored_by_hosted_router")
        if self.providers and provider not in self.providers:
            violations.append("actual_provider_outside_frozen_restriction")
        if any(_matches(model, pattern) is True for pattern in self.excluded):
            violations.append("actual_model_violates_hard_exclusion")
        if any(_matches(model, pattern) is None for pattern in self.excluded):
            scope_reasons.append("hard_exclusion_alias_unresolved")
        if (
            not isinstance(model, str)
            or not model
            or model == MODEL
            or not isinstance(provider, str)
            or not provider
        ):
            scope_reasons.append("actual_serving_identity_unverified")
        if not stages:
            scope_reasons.append("hosted_router_metadata_missing")
        # Soft includes can never establish identical candidate-pool enforcement.
        if includes:
            scope_reasons.append("model_includes_are_soft_preferences")
        else:
            scope_reasons.append("hosted_candidate_pool_unconstrained")
        billing = {
            "api_cost_usd": None,
            "cost_unknown": True,
            "source": None,
            "proof": None,
            "estimated_cost_is_billed_cost": False,
            "advisor_added_separately": False,
            "self_built_classifier_fee_usd": None,
            "errors": [],
        }
        if self.accounting_verifier is not None:
            verified = self.accounting_verifier(saved, accounting_proof, request)
            if isinstance(verified, dict) and verified.get("verified") is True:
                try:
                    value = verified.get("cost_usd")
                    if isinstance(value, (bool, float)):
                        raise ValueError("Exact decimal proof required")
                    cost = Decimal(value)
                    source = verified.get("source")
                    if (
                        not cost.is_finite()
                        or cost < 0
                        or not isinstance(source, str)
                        or not source
                        or "estimated" in source.lower()
                        or verified.get("complete_request_cost") is not True
                        or (
                            generation_id is not None
                            and verified.get("generation_id") != generation_id
                        )
                    ):
                        raise ValueError("Incomplete or mismatched billing proof")
                except (InvalidOperation, ValueError, TypeError):
                    billing["errors"].append("verified_accounting_proof_invalid_or_incomplete")
                else:
                    billing.update(
                        api_cost_usd=str(cost),
                        cost_unknown=False,
                        source=source,
                        proof=deepcopy(verified),
                    )
            else:
                billing["errors"].append("accounting_not_verified")
        return {
            "version": VERSION,
            "http_status": status,
            "answer": answer,
            "generation_id": generation_id,
            "actual_model": model,
            "actual_provider": provider,
            "usage": deepcopy(body.get("usage")),
            "openrouter_metadata": deepcopy(metadata),
            "hosted_decisions": stages,
            "hosted_router_stage_count": len(stages),
            "self_built_jev_calls": 0,
            "routing": {
                key: deepcopy(routing.get(key))
                for key in (
                    "resolved_models",
                    "candidates",
                    "reason",
                    "pipeline",
                    "advisor_model",
                    "models",
                    "excluded_models",
                    "list_fallback",
                    "list_tier_cap",
                    "max_fallback",
                )
            },
            "candidate_cost_estimates": [
                deepcopy(candidate.get("estimated_cost"))
                for stage in stages
                for candidate in (stage.get("candidates") or [])
                if isinstance(candidate, dict)
            ],
            "comparison_scope": "hosted_service_comparison",
            "scope_reasons": list(dict.fromkeys(scope_reasons)),
            "restriction_violations": violations,
            "identity_restrictions_satisfied": not violations
            and not {
                "actual_serving_identity_unverified",
                "hard_exclusion_alias_unresolved",
            }.intersection(scope_reasons),
            "routing_accepted": not violations,
            "billing": billing,
            "model_failure": status is not None and status >= 400,
            "answer_present": answer is not None,
            "retry_performed": False,
        }

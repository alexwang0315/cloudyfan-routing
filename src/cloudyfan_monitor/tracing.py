"""Public routing evidence from verified Core results, without prompts or answers."""


def routing_trace(result):
    attempts = []
    for index, attempt in enumerate(result["attempts"], 1):
        calls = []
        for call in attempt["calls"]:
            usage = call.get("usage") or {}
            calls.append({key: call.get(key) for key in
                          ("model", "provider", "generation_id", "elapsed_ms", "cost_usd")}
                         | {"input_tokens": usage.get("prompt_tokens", usage.get("input_tokens")),
                            "output_tokens": usage.get("completion_tokens", usage.get("output_tokens"))})
        attempts.append({"index": index, "tier": attempt["tier"],
                         "passed": attempt["public"].get("passed"), "stop": attempt.get("stop"),
                         "models": sorted({c["model"] for c in calls if c["model"]}), "calls": calls})
    decisions = []
    for index, decision in enumerate((result.get("selection") or {}).get("decisions", [])):
        previous = attempts[index] if index < len(attempts) else None
        trigger = ("public_validation_failed" if previous and previous["passed"] is False
                   else "public_validation_unavailable")
        decisions.append({key: decision.get(key) for key in (
            "stage", "selected_tier", "reported_choice", "fallback", "fallback_reason",
            "probabilities", "cost_usd", "model")}
            | {"generation_id": decision.get("id"),
               "previous_tier": previous["tier"] if previous else None, "trigger": trigger})
    return {"complete": True, "attempts": attempts, "decisions": decisions,
            "basis": "verified_core_result", "distributions_are_correctness_probabilities": False}

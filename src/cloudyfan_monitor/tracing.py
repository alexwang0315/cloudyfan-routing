"""Public routing evidence from verified Core results, without prompts or answers."""

ADVISOR_FIRST = "advisor_first"
ROLES = ("advisor", "judge")


def _call(call, *, role=None):
    usage = call.get("usage") or {}
    value = ({key: call.get(key) for key in ("model", "provider", "generation_id", "elapsed_ms", "cost_usd")}
             | {"input_tokens": usage.get("prompt_tokens", usage.get("input_tokens")),
                "output_tokens": usage.get("completion_tokens", usage.get("output_tokens"))})
    if role is not None:
        # 路徑二：標明這筆呼叫是顧問、裁判或池內作答，費用才分得清楚。
        value["role"] = call.get("tier") if call.get("tier") in ROLES else role
    return value


def _experience(result):
    """這題的經驗身分：只有代碼與編號（契約範圍、經驗卡編號、經驗庫版本、有沒有附解題事實）。

    沒接經驗庫的題目回傳 None。舊結果沒有 scope 欄位時為 None。事實內容不進 Monitor。
    """
    experience = result.get("experience")
    if not isinstance(experience, dict):
        return None
    return {"scope": experience.get("scope"), "card": experience.get("card"),
            "store_version": experience.get("store_version"),
            "facts_attached": experience.get("facts_block") is not None}


def _advisor(result):
    """路徑二的公開摘要：只放決定類型、設定識別、順位與作廢原因代碼。

    顧問的回答草稿與每組建議的自由文字理由可能引用題目內容，一律不進 Monitor。
    """
    advisor = result.get("advisor") or {}
    decision = advisor.get("decision") or {}
    costs = result.get("costs") or {}
    return {"path": ADVISOR_FIRST, "advisor_mode": decision.get("outcome"),
            "incomplete_reason": decision.get("incomplete_reason"), "end_reason": result.get("end_reason"),
            "score_snapshot": advisor.get("score_snapshot"),
            "experience": _experience(result),
            "recommendations": [{key: item.get(key) for key in ("rank", "setting", "tier")}
                                for item in decision.get("recommendations") or []],
            "voided": [{key: item.get(key) for key in ("rank", "reason")} for item in decision.get("voided") or []],
            "calls": [_call(call, role="advisor") for call in advisor.get("calls") or []],
            # 研究全額＝全部收據；正式成本＝零模型費用情境（明示開源的角色不計）。
            "costs": {"research_usd": costs.get("api_usd"),
                      "formal_usd": costs.get("hypothetical_open_source_zero_usd"),
                      "advisor_usd": costs.get("advisor_usd"), "judge_usd": costs.get("judge_usd"),
                      "unknown_calls": costs.get("unknown_calls")}}


def routing_trace(result):
    path_two = result.get("mode") == ADVISOR_FIRST and isinstance(result.get("advisor"), dict)
    attempts = []
    for index, attempt in enumerate(result["attempts"], 1):
        calls = [_call(call, role="answer" if path_two else None) for call in attempt["calls"]]
        attempts.append({"index": index, "tier": attempt["tier"],
                         "passed": attempt["public"].get("passed"), "stop": attempt.get("stop"),
                         "models": sorted({c["model"] for c in calls if c["model"]}), "calls": calls})
        if path_two:
            attempts[-1].update({key: attempt.get(key) for key in
                                 ("source", "setting", "pool_tier", "recommendation_rank")})
    decisions = []
    next_attempt = 0
    for decision in (result.get("selection") or {}).get("decisions", []):
        # Backup runners save initial/same-pool economic decisions too; an
        # economic classify choice and its paid classifier share ONE previous
        # attempt. Plain/QCS Core starts its decision stream after the first
        # attempt. Advance only when the choice leads to the next saved attempt.
        if next_attempt == 0 and decision.get("stage") in ("low_mid", "mid_high"):
            next_attempt = 1
        previous = attempts[next_attempt - 1] if 0 < next_attempt <= len(attempts) else None
        trigger = ("public_validation_failed" if previous and previous["passed"] is False
                   else "public_validation_unavailable")
        decisions.append({key: decision.get(key) for key in (
            "stage", "selected_tier", "reported_choice", "fallback", "fallback_reason",
            "probabilities", "cost_usd", "model")}
            | {"generation_id": decision.get("id"),
               "previous_tier": previous["tier"] if previous else None, "trigger": trigger})
        if (decision.get("action") == "direct" or "fallback" in decision) and (
                next_attempt < len(attempts) and attempts[next_attempt]["tier"] == decision.get("selected_tier")):
            next_attempt += 1
    trace = {"complete": True, "attempts": attempts, "decisions": decisions,
             "basis": "verified_core_result", "distributions_are_correctness_probabilities": False}
    if path_two:
        trace["advisor"] = _advisor(result)
    return trace

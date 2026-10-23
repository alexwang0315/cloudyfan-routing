"""Pure public-message tests; no upstream model, hidden answer or paid call."""

from copy import deepcopy

import pytest

from cloudyfan_jev.recovery_prompts import DIRECT_ANSWER, descriptor, recovery_messages

SYSTEM = "Return only JSON."
TASK = 'Return {"sql":"..."} with original columns name,total.'
INITIAL = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": TASK}]


def previous(answer="", *, code="PROVIDER_UNAVAILABLE", passed=None, stop="provider_unavailable"):
    return {"final": answer, "stop": stop, "public": {"passed": passed, "checks": [code]}}


def test_first_answer_preserves_original_wire_messages():
    assert recovery_messages(SYSTEM, TASK) == INITIAL


@pytest.mark.parametrize(
    "code,stop",
    [
        ("PROVIDER_RATE_LIMITED", "provider_rate_limited"),
        ("PROVIDER_UNAVAILABLE", "provider_unavailable"),
        ("MODEL_TOKEN_EXHAUSTED", "empty_or_exhausted"),
    ],
)
def test_no_answer_is_fresh_original_task_without_repair_or_assistant(code, stop):
    old = previous(code=code, stop=stop)
    result = recovery_messages(SYSTEM, TASK, old)
    assert result == INITIAL + [{"role": "user", "content": DIRECT_ANSWER}]
    assert not any(m["role"] == "assistant" for m in result)
    assert "Repair the existing workspace" not in str(result)
    assert result[1]["content"] == TASK


def test_new_no_answer_drops_stale_earlier_answer_and_diagnostic():
    stale = INITIAL + [
        {"role": "assistant", "content": "older bad SQL"},
        {"role": "user", "content": "older diagnostic"},
    ]
    result = recovery_messages(SYSTEM, TASK, previous(), stale)
    assert result == INITIAL + [{"role": "user", "content": DIRECT_ANSWER}]
    assert "older" not in str(result)


@pytest.mark.parametrize("final", [False, True])
def test_real_answer_only_latest_answer_and_public_diagnostics_are_repaired(final):
    prior = previous("latest SQL answer", code="SQL_PUBLIC_RESULT_MISMATCH", passed=False, stop="stop")
    saved = deepcopy(prior)
    result = recovery_messages(SYSTEM, TASK, prior, final_attempt=final)
    assert result[:2] == INITIAL and result[2] == {"role": "assistant", "content": prior["final"]}
    assert "Correct the previous answer to satisfy the original task" in result[3]["content"]
    assert "SQL_PUBLIC_RESULT_MISMATCH" in result[3]["content"]
    assert ("This is the final attempt." in result[3]["content"]) is final
    assert prior == saved


def test_supplied_conversation_requires_latest_actual_answer_and_is_not_mutated():
    prior = previous("latest", passed=False)
    messages = INITIAL + [{"role": "assistant", "content": "latest"}]
    saved = deepcopy(messages)
    assert recovery_messages(SYSTEM, TASK, prior, messages)[:3] == messages
    assert messages == saved
    with pytest.raises(ValueError, match="latest answer"):
        recovery_messages(SYSTEM, TASK, prior, INITIAL + [{"role": "assistant", "content": "stale"}])


@pytest.mark.parametrize(
    "attempt",
    [
        {},
        {"final": None, "public": {"passed": None}},
        {"final": "answer", "public": {"passed": True}},
        {"final": "answer", "public": {"passed": "false"}},
    ],
)
def test_invalid_or_completed_attempt_does_not_create_a_repair(attempt):
    with pytest.raises(ValueError):
        recovery_messages(SYSTEM, TASK, attempt)


def test_optin_descriptor_keeps_financial_and_attempt_policies_unchanged():
    info = descriptor()
    assert info["version"] == "provider-no-answer-original-task-v1"
    assert len(info["source_sha256"]) == 64
    assert info["billing_policy_changed"] is False
    assert info["generation_and_classifier_limits_changed"] is False
    assert info["new_run_namespace_required"] is True

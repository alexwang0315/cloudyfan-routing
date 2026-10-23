"""Versioned, opt-in recovery messages for public, tool-free research workflows.

A provider failure is not an incorrect model answer. This module changes no
billing interpretation, attempt count, classifier input or acceptance rule.
"""

import hashlib
import json
from copy import deepcopy
from pathlib import Path

VERSION = "provider-no-answer-original-task-v1"
DIRECT_ANSWER = (
    "The previous provider did not produce an answer. Complete the original task "
    "directly, following its original output contract. No previous answer exists to repair."
)


def descriptor():
    return {
        "version": VERSION,
        "source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "billing_policy_changed": False,
        "generation_and_classifier_limits_changed": False,
        "new_run_namespace_required": True,
    }


def recovery_messages(
    system, task_prompt, previous_attempt=None, prior_messages=None, *, final_attempt=False
):
    """Build original task + fresh answer request, or repair a real latest answer.

    ``prior_messages`` optionally contains the existing conversation BEFORE the
    new repair instruction. Omit it to include only the latest real answer.
    Classifiers must separately consume the latest attempt and public diagnosis.
    No hidden evaluator result belongs in any argument to this function.
    """
    if not isinstance(system, str) or not isinstance(task_prompt, str) or not task_prompt.strip():
        raise ValueError("Original system and public task text required")
    initial = [{"role": "system", "content": system}, {"role": "user", "content": task_prompt}]
    if previous_attempt is None:
        return initial
    if not isinstance(previous_attempt, dict) or not isinstance(previous_attempt.get("final"), str):
        raise ValueError("Previous attempt requires its actual text answer or empty string")
    public = previous_attempt.get("public")
    if (
        not isinstance(public, dict)
        or public.get("passed") is True
        or (public.get("passed") is not None and public.get("passed") is not False)
    ):
        raise ValueError("Only a failed or unconfirmed public attempt may enter recovery")
    if not previous_attempt["final"]:
        # Even an older real answer must not be mistaken for the most recent
        # provider's nonexistent answer. Preserve the complete original contract.
        return initial + [{"role": "user", "content": DIRECT_ANSWER}]
    if prior_messages is None:
        messages = initial + [{"role": "assistant", "content": previous_attempt["final"]}]
    else:
        messages = deepcopy(prior_messages)
        if (
            messages[:2] != initial
            or not messages
            or messages[-1].get("role") != "assistant"
            or messages[-1].get("content") != previous_attempt["final"]
        ):
            raise ValueError("Prior messages must end with the actual latest answer")
    instruction = (
        "Correct the previous answer to satisfy the original task and original output contract. "
        "Return only the required answer format. "
        + (
            "This is the final attempt. "
            if final_attempt
            else "Another bounded repair may follow only if public validation still fails. "
        )
        + "Public diagnostics: "
        + json.dumps(public, ensure_ascii=False, sort_keys=True, allow_nan=False)
    )
    return messages + [{"role": "user", "content": instruction}]


class NoAnswerRecoveryMixin:
    """Optional Core mixin; source/version binding forbids silent old-run reuse.

    This adapts the standard Core ``_attempt`` extension point. The generator
    and its caller continue to own financial/identity checks and phase limits.
    """

    def configuration_descriptor(self):
        return {**super().configuration_descriptor(), "no_answer_recovery": descriptor()}

    def _attempt(self, env, messages, tier, prefix, index, actions, directory):
        states = getattr(self, "_answer_recovery_states", None)
        if states is None:
            states = self._answer_recovery_states = {}
        if index == 0:
            if len(messages) != 2 or messages[0].get("role") != "system" or messages[1].get("role") != "user":
                raise ValueError("Original system and task messages required")
            states[prefix] = {"initial": deepcopy(messages), "previous": None}
        if prefix not in states:
            raise ValueError("No-answer recovery requires replay of the prior attempt")
        state = states[prefix]
        previous = state["previous"]
        if index and previous is not None:
            from .engine import _save

            initial = state["initial"]
            final_attempt = "This is the final attempt." in messages[-1].get("content", "")
            messages[:] = recovery_messages(
                initial[0]["content"],
                initial[1]["content"],
                previous,
                messages[:-1] if previous["final"] else None,
                final_attempt=final_attempt,
            )
            _save(
                directory / f"no-answer-recovery-a{index}.json",
                {
                    "version": VERSION,
                    "attempt": index,
                    "selected_tier": tier,
                    "previous_attempt": previous["attempt"],
                    "previous_tier": previous["tier"],
                    "previous_stop": previous["stop"],
                    "previous_public": previous["public"],
                    "previous_call_ids": [c["call_id"] for c in previous["calls"]],
                    "original_messages_sha256": hashlib.sha256(
                        json.dumps(initial, ensure_ascii=False, sort_keys=True).encode()
                    ).hexdigest(),
                    "no_answer_repaired": False,
                    "recovery_kind": "answer_correction" if previous["final"] else "direct_answer",
                },
            )
        result = super()._attempt(env, messages, tier, prefix, index, actions, directory)
        state["previous"] = deepcopy(result)
        return result

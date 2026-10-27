"""Allowlisted operator history from a task's frozen, server-owned evidence.

Never asks current health, dispatches, reconciles, or accepts an evidence path
from HTTP. Existing public trace and accounting remain their own authorities.
"""
from contextlib import closing, contextmanager
from decimal import Decimal, localcontext
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3

from .store import digest
from .tracing import routing_trace
from .observation_fields import configuration, identifier

from cloudyfan_jev.scores.backup_pools import verify_backup_pools
from cloudyfan_jev.scores.backup_runtime import backup_classifier_contract_hash, backup_pool_contract_hash
from cloudyfan_jev.scores.qcs import SelectionPolicy, classifier_contract_hash
from cloudyfan_jev.task_features import public_features, unavailable

SCHEMA_VERSION = "mrc-observation-v1"
TIERS = ("open_source", "low", "mid", "high")
TERMINAL = ("passed", "failed", "unverifiable")
REASONS = frozenset(("observation_missing", "observation_stale", "observation_in_future",
    "status_unknown", "status_degraded", "circuit_open", "cooldown_active", "recovery_required",
    "candidate_qualification_stale_or_invalid", "local_backend_not_registered", "provider_failed_this_run",
    "configuration_already_dispatched", "cloud_free_trial_not_explicitly_authorized",
    "remaining_task_time_insufficient", "snapshot_in_future", "health_observation_missing",
    "latency_outside_limit", "throughput_outside_limit",
    "latency_unit_unknown_or_invalid", "throughput_unit_unknown_or_invalid",
    "uptime_last_5m_below_minimum", "uptime_last_30m_below_minimum", "uptime_last_1d_below_minimum"))


def _interval(value):
    if not isinstance(value, dict) or any(not isinstance(value.get(key), str) for key in ("lower", "upper")):
        raise ValueError("Invalid saved interval")
    lo, hi = Decimal(value["lower"]), Decimal(value["upper"])
    if not lo.is_finite() or not hi.is_finite() or lo < 0 or hi < lo:
        raise ValueError("Invalid saved interval")
    return lo, hi


def _public_interval(value):
    _interval(value)
    return {key: value[key] for key in ("lower", "upper")}


def _economic(plan, decision, stage, previous, pair):
    """Check historical arithmetic against the exact frozen calibration, never prices.

    Only saved amounts are exported. This consistency check cannot qualify a
    calibration or turn an estimate into actual spend or an enforceable bound.
    """
    if not stage:
        expected = {"action": "direct", "selected_tier": next(iter(pair)),
                    "reason": "same_pool_availability_or_initial"} if pair else {
                    "action": "stop", "reason": "no_eligible_candidate"}
        return expected, {"status": "not_applicable"}
    if not pair:
        return {"action": "stop", "reason": "no_eligible_candidate"}, {"status": "not_provided"}
    policy = plan["qcs_policy"]
    key = (backup_classifier_contract_hash({tier: row["entry"]["profile"] for tier, row in pair.items()},
        decision["version"], stage, previous["tier"], previous["profile"]) if len(pair) == 2 else None)
    variant = policy.get("stages", {}).get(stage, {}).get("variants", {}).get(key)
    paths = variant["paths"] if variant else {tier: {"cost_usd": row["evidence"]["complete_cost_usd"],
        "flow_ms_upper": row["evidence"]["speed"]["upper"]} for tier, row in pair.items()}
    direct = min(pair, key=lambda tier: (_interval(paths[tier]["cost_usd"])[1],
                                        Decimal(paths[tier]["flow_ms_upper"]), tier))
    expected = {"stage": stage, "action": "direct", "selected_tier": direct,
                "reason": "exact_classifier_calibration_missing", "contract_sha256": key}
    if variant is None:
        return expected, {"status": "not_provided", "reason": "exact_classifier_calibration_missing"}
    calibration = variant["calibration"]
    previous_row = next(row for row in plan["pools"][previous["tier"]]
                        if row["entry"]["profile"] == previous["profile"])
    if (calibration["contract_sha256"] != key
            or calibration["pool_contract_sha256"] != backup_pool_contract_hash(plan)
            or calibration["previous_configuration_sha256"] != previous_row["configuration_sha256"]
            or calibration["classifier_version"] != decision["version"]
            or calibration["stage"] != stage or calibration["previous_tier"] != previous["tier"]):
        raise ValueError("Historical calibration differs")
    lower, upper = ("low", "mid") if stage == "low_mid" else ("mid", "high")
    with localcontext() as context:
        context.prec = 512
        p0, p1 = _interval(variant["lower_probability"])
        fee = _interval(variant["classifier_cost_usd"])[1]
        costs = {tier: _interval(variant["classified_paths"][tier]["cost_usd"])[1] for tier in pair}
        expected_cost = fee + max(p * costs[lower] + (1-p) * costs[upper] for p in (p0, p1))
        direct_lower = _interval(paths[direct]["cost_usd"])[0]
        saving = direct_lower - expected_cost
        needed = max(Decimal(policy.get("minimum_saving_usd", "0")),
                     direct_lower * Decimal(policy.get("minimum_saving_fraction", "0")))
        expected.update(reason="classification_not_economic", net_saving_lower_usd=str(saving))
        if saving > needed and Decimal(variant["classify_flow_ms_upper"]) <= Decimal(policy["max_flow_ms"]):
            expected.update(action="classify", selected_tier=None, reason="conservative_net_saving_positive")
    return expected, {"status": "available", "basis": "frozen_complete_workflow_estimate",
        "direct_paths_usd": {tier: _public_interval(paths[tier]["cost_usd"]) for tier in pair},
        "classified_paths_usd": {tier: _public_interval(variant["classified_paths"][tier]["cost_usd"]) for tier in pair},
        "classifier_fee_estimate_usd": _public_interval(variant["classifier_cost_usd"]),
        "net_saving_lower_usd": expected["net_saving_lower_usd"],
        # This evidence format has no dataset version. The protocol/model-pair
        # contract and raw calibration evidence are distinct fingerprints.
        "calibration_version": None,
        "calibration_evidence_sha256": identifier(calibration.get("raw_sha256")),
        "classifier_contract_sha256": identifier(calibration["contract_sha256"]),
        "classifier_version": identifier(calibration["classifier_version"])}


def _saved_health_matches(health_store, assessment, policy, cache):
    """Read the named immutable historical batch, never latest/assess/circuits."""
    observation = assessment.get("observation")
    if observation is None:
        return assessment.get("snapshot_id") is None
    snapshot_id = assessment.get("snapshot_id")
    if not isinstance(snapshot_id, str) or not re.fullmatch(r"[a-f0-9]{64}", snapshot_id):
        return False
    if snapshot_id not in cache:
        path = Path(health_store.path)
        if any(part.is_symlink() for part in (path, *path.parents)):
            return False
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=0)) as db:
            row = db.execute("SELECT payload FROM snapshots WHERE id=?", (snapshot_id,)).fetchone()
        if row is None or len(row[0]) > 4_194_304:
            return False
        snapshot = json.loads(row[0])
        if (digest(snapshot) != snapshot_id or snapshot.get("schema_version") != "provider-health-v1"
                or snapshot.get("policy_sha256") != digest(policy)):
            return False
        cache[snapshot_id] = snapshot
    return observation in cache[snapshot_id].get("observations", [])


def _classifier_fee(completed, classified, stage, profile, *, allow_missing_profile=False):
    """QCS may omit its saved profile; backup requires the exact frozen one."""
    calls = [call for call in (completed.get("selection") or {}).get("calls", [])
             if call.get("call_id", "").endswith("-decision-" + stage)
             and call.get("generation_id") == classified.get("id")
             and (call.get("profile") == profile or allow_missing_profile and "profile" not in call)]
    if len(calls) == 1 and calls[0].get("cost_unknown") is False:
        amount = calls[0].get("cost_usd")
        if isinstance(amount, str) and Decimal(amount).is_finite() and Decimal(amount) >= 0:
            return amount
    return None


def _qcs_stages(directory, manifest, completed):
    """Link existing CategoryCore checkpoints to their original immutable plan.

    SelectionPolicy checks only the frozen calibration arithmetic. No admission,
    current health, public prices or fee reconciliation is performed here.
    """
    plan = manifest["selection_policy"]
    policy = SelectionPolicy(plan)
    if plan["policy"].get("category_id") != manifest["task"]["category"]:
        raise ValueError("Historical QCS category differs")
    attempts = completed["attempts"]
    decisions = (completed.get("selection") or {}).get("decisions", [])
    stages = []
    for index, decision in enumerate(decisions):
        stage = "low_mid" if index == 0 else "mid_high"
        try:
            previous, following = attempts[index], attempts[index + 1]
            expected = policy.decide(stage, previous["tier"])
            saved = _read(directory / ("economics-" + stage + ".json"))
            if (saved != {"outcome": expected, "previous_tier": previous["tier"],
                    "previous_answer": previous["final"], "public_diagnostics": previous["public"]}
                    or expected["action"] == "direct" and decision != expected
                    or decision.get("stage") != stage or decision.get("selected_tier") != following["tier"]):
                raise ValueError("Historical QCS decision differs")
            key = stage if stage == "low_mid" else stage + ":" + previous["tier"]
            forecast = plan["policy"]["stages"][key]
            calibration = forecast.get("classified_calibration") or plan["policy"]["calibration"]
            tiers = ("low", "mid") if stage == "low_mid" else ("mid", "high")
            rows = []
            for tier in tiers:
                profile = plan["profiles"].get(tier)
                if profile is None:
                    continue
                config_id = profile.get("capability_card", {}).get("score_selection", {}).get("config_id")
                eligible = plan["eligible"][tier]
                rows.append({"tier": tier, "config_id": identifier(config_id),
                    "configuration": configuration(profile), "health": {"status": "not_provided"},
                    "state": "dispatched" if tier == following["tier"] else
                        "considered_not_selected" if eligible else "excluded_before_dispatch",
                    "reasons": [] if eligible else ["candidate_qualification_stale_or_invalid"]})
            paths = {tier: _public_interval(forecast["paths"][tier]["cost_usd"])
                     for tier in tiers if plan["eligible"][tier]}
            classified_paths = forecast.get("classified_paths")
            economics = {"status": "partial", "basis": "frozen_complete_workflow_estimate",
                "direct_paths_usd": paths,
                "classified_paths_usd": ({tier: _public_interval(classified_paths[tier]["cost_usd"])
                    for tier in paths} if classified_paths else None),
                "classifier_fee_estimate_usd": (_public_interval(forecast["classifier_cost_usd"])
                    if forecast.get("classifier_cost_usd") else None),
                "net_saving_lower_usd": expected["net_saving_lower_usd"], "calibration_version": None,
                "calibration_evidence_sha256": identifier(calibration.get("raw_sha256")),
                "classifier_contract_sha256": identifier(calibration.get("contract_sha256")),
                "plan_id": identifier(plan["plan_id"])}
            if economics["classifier_contract_sha256"] is not None and calibration["contract_sha256"] != (
                    classifier_contract_hash(plan["profiles"], manifest["decision"]["version"], stage, previous["tier"])):
                raise ValueError("Historical classifier contract differs")
            stages.append({"index": index, "stage": stage, "state": "recorded",
                "trigger": "public_validation_failed" if previous["public"].get("passed") is False else
                    "public_validation_unavailable", "action": expected["action"], "reason": expected["reason"],
                "selected_tier": following["tier"], "classifier_called": expected["action"] == "classify",
                "classifier_api_usd": (_classifier_fee(completed, decision, stage, manifest["decision"],
                    allow_missing_profile=True)
                    if expected["action"] == "classify" else None),
                "candidates": rows, "economics": economics})
        except Exception:
            stages.append({"index": index, "stage": stage, "state": "unknown",
                "reason": "saved_selection_unverifiable", "candidates": [], "economics": {"status": "unknown"}})
            break
    return stages


def _capacity_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
        allow_nan=False).encode()).hexdigest()


def _window_context(service, internal, descriptor):
    controller = getattr(service, "capacity_controller", None)
    if controller is None:
        return None
    with controller.store.connection() as db:
        saved = db.execute("SELECT payload FROM capacity_adoptions WHERE run=?", (internal,)).fetchone()
        adoption = json.loads(saved[0]) if saved else None
        if adoption is None or adoption["policy_version"] is None:
            return None
        policy = json.loads(db.execute("SELECT payload FROM capacity_policies WHERE version=?",
            (adoption["policy_version"],)).fetchone()[0])
        if policy.get("windows_version") is None:
            return None
        configuration_row = db.execute("SELECT payload,digest FROM capacity_configurations WHERE version=?",
            (adoption["configuration_version"],)).fetchone()
        configuration = json.loads(configuration_row[0])
        if (adoption["descriptor_sha256"] != _capacity_hash(descriptor)
                or configuration_row[1] != _capacity_hash(configuration)
                or policy["configuration_sha256"] != configuration_row[1]):
            raise ValueError("Historical capacity binding differs")
    return {"store": controller.store, "configuration": configuration, "policy": policy}


def _saved_window(value, profile, context):
    if (context is None or not isinstance(value, dict)
            or set(value) != {"status", "not_before", "evidence_refs", "revision", "reason_code"}
            or value["status"] not in {"open", "wait", "unknown", "denied"}
            or type(value["revision"]) is not int or value["revision"] < 0
            or not isinstance(value["evidence_refs"], list) or len(value["evidence_refs"]) > 32
            or any(not isinstance(ref, str) or not re.fullmatch(r"[a-f0-9]{64}", ref)
                   for ref in value["evidence_refs"])
            or len(set(value["evidence_refs"])) != len(value["evidence_refs"])
            or not isinstance(value["reason_code"], str)
            or not re.fullmatch(r"capacity_window_[a-z_]{1,64}", value["reason_code"])):
        raise ValueError("Historical window decision differs")
    routes = [route for route in context["policy"]["routes"]
        if route["model"] == profile["model_version"] and route["provider_name"] == profile.get("provider_name")]
    if len(routes) != 1:
        raise ValueError("Historical window route differs")
    binding = next(row for row in context["configuration"]["bindings"] if row["id"] == routes[0]["binding"])
    physical = {_capacity_hash(row["scope"]) for row in context["configuration"]["groups"]
                if row["id"] in binding["groups"]}
    rows = []
    with context["store"].connection() as db:
        count = db.execute("SELECT COUNT(*) FROM capacity_window_observations").fetchone()[0]
        if value["revision"] > count:
            raise ValueError("Historical window revision differs")
        for ref in value["evidence_refs"]:
            record = db.execute("SELECT rowid,payload FROM capacity_window_observations WHERE id=?", (ref,)).fetchone()
            if record is None or record[0] > value["revision"]:
                raise ValueError("Historical window evidence missing")
            row = json.loads(record[1])
            if row["id"] != ref or row["physical"] not in physical or _capacity_hash(row["scope"]) != row["physical"]:
                raise ValueError("Historical window physical scope differs")
            rows.append(row)
    until = value["not_before"]
    if value["status"] == "open":
        valid = not rows and until is None and value["reason_code"] == "capacity_window_open"
    elif value["status"] == "wait":
        valid = (bool(rows) and all(row["status"] == "wait" for row in rows)
            and type(until) in (int, float) and math.isfinite(until)
            and until == max(row["not_before"] for row in rows) and value["reason_code"] == "capacity_window_wait")
    else:
        valid = until is None and any(row["status"] == "unknown"
            and row["reason_code"] == value["reason_code"] for row in rows)
    if not valid:
        raise ValueError("Historical window reason differs from saved evidence")
    return dict(value)


def _historical_candidate(row, observation, tier, health_store, policy, cache, window_context):
    result = {"tier": tier, "config_id": identifier(row["entry"]["config_id"]),
              "configuration_sha256": row["configuration_sha256"],
              "configuration": configuration(row["entry"]["profile"])}
    try:
        assessment = observation["assessment"]
        observed = assessment.get("observation")
        if (not _saved_health_matches(health_store, assessment, policy, cache)
                or observed and (observed["configuration_sha256"] != row["configuration_sha256"]
                or observed["provider"] != row["entry"]["profile"]["provider"])):
            raise ValueError("Historical observation differs")
        reasons = observation["reasons"]
        if not isinstance(reasons, list) or any(not isinstance(reason, str) for reason in reasons):
            raise ValueError("Historical reason shape differs")
        window = (_saved_window(observation["capacity_window"], row["entry"]["profile"], window_context)
                  if "capacity_window" in observation else None)
        if window is not None:
            result["capacity_window"] = window
        known_reasons = REASONS | ({window["reason_code"]} if window and window["status"] != "open" else set())
        result.update(state="excluded_before_dispatch" if reasons else "considered_not_selected",
            reasons=[reason if reason in known_reasons else "saved_reason_unknown" for reason in reasons],
            health={"snapshot_id": identifier(assessment.get("snapshot_id")),
                "observed_at": identifier(observed.get("observed_at")) if observed else None,
                "cooldown_until": identifier(assessment.get("circuit", {}).get("cooldown_until"))})
    except Exception:
        result.update(state="unknown", reasons=["saved_candidate_unverifiable"], health={})
    return result


def _backup_stages(directory, manifest, completed, health_store, window_context):
    plan = manifest["backup_plan"]
    verify_backup_pools(plan, now=None)
    attempts, decisions = completed["attempts"], completed.get("selection", {}).get("decisions", [])
    stages, attempt_index, decision_index, partial = [], 0, 0, False
    health_snapshots = {}
    for index in range(9):
        path = directory / f"backup-selection-{index}.json"
        if not path.exists():
            break
        try:
            saved = _read(path)
            tiers = tuple(saved["health"])
            stage = "low_mid" if set(tiers) == {"low", "mid"} else (
                    "mid_high" if set(tiers) == {"mid", "high"} else None)
            if stage is None and (len(tiers) != 1 or tiers[0] not in TIERS):
                raise ValueError("Historical tier scope differs")
            previous = attempts[attempt_index-1] if attempt_index else None
            public_previous = ({key: previous[key] for key in ("tier", "profile", "final", "public")}
                               if previous else None)
            if (saved["previous"] != public_previous
                    or saved["actions"] != completed["actions"][:len(saved["actions"])]):
                raise ValueError("Historical public context differs")
            pair = saved["pair"]
            if set(pair) - set(tiers):
                raise ValueError("Historical candidate scope differs")
            rows = []
            for tier in tiers:
                pool, observations = plan["pools"][tier], saved["health"][tier]
                if [item["config_id"] for item in observations] != [row["entry"]["config_id"] for row in pool]:
                    raise ValueError("Historical candidate list differs")
                eligible = [row for row, obs in zip(pool, observations) if not obs["reasons"]]
                if tier in pair:
                    ranked = min(eligible, key=lambda row: (_interval(row["evidence"]["complete_cost_usd"])[1],
                        Decimal(row["evidence"]["speed"]["upper"]), row["entry"]["config_id"]))
                    if pair[tier] != ranked:
                        raise ValueError("Historical candidate differs")
                for row, observation in zip(pool, observations):
                    rows.append(_historical_candidate(row, observation, tier, health_store,
                                                       manifest["health_policy"], health_snapshots, window_context))
            if any(row["state"] == "unknown" for row in rows):
                for row in rows:
                    if row["state"] != "unknown":
                        row["state"] = "health_recorded"
                partial = True
                stages.append({"index": index, "state": "unknown", "reason": "saved_selection_unverifiable",
                               "candidates": rows, "economics": {"status": "unknown"}})
                break  # A choice cannot be linked across an unverified candidate.
            expected, economics = _economic(plan, manifest["decision"], stage, public_previous, pair)
            if saved["economic"] != expected:
                raise ValueError("Historical economics differs")
            selected, classifier_api_usd = expected.get("selected_tier"), None
            if pair:
                if decisions[decision_index] != expected:
                    raise ValueError("Historical decision differs")
                decision_index += 1
                if expected["action"] == "classify":
                    classified = decisions[decision_index]
                    if classified.get("stage") != stage or classified.get("selected_tier") not in pair:
                        raise ValueError("Historical classifier differs")
                    selected = classified["selected_tier"]
                    classifier_api_usd = _classifier_fee(completed, classified, stage, manifest["decision"],
                        allow_missing_profile=False)
                    decision_index += 1
                attempt = attempts[attempt_index]
                if (attempt["profile"] != pair[selected]["entry"]["profile"]
                        or attempt["configuration_sha256"] != pair[selected]["configuration_sha256"]):
                    raise ValueError("Historical dispatch differs")
                for row in rows:
                    if row["tier"] == selected and row["config_id"] == attempt["config_id"]:
                        row["state"] = "dispatched"
                attempt_index += 1
            stages.append({"index": index, "stage": stage or "initial_or_availability",
                "state": "recorded", "action": expected["action"], "reason": expected["reason"],
                "trigger": "initial" if previous is None else "availability_failure" if previous.get("stop") in
                    ("provider_rate_limited", "provider_unavailable") else
                    "public_validation_failed" if previous["public"].get("passed") is False else
                    "public_validation_unavailable",
                "selected_tier": selected, "classifier_called": expected["action"] == "classify",
                "classifier_api_usd": classifier_api_usd,
                "candidates": rows, "economics": economics})
        except Exception:
            partial = True
            stages.append({"index": index, "state": "unknown", "reason": "saved_selection_unverifiable",
                           "candidates": [], "economics": {"status": "unknown"}})
            break  # Later context cannot be linked across the missing choice.
    if attempt_index != len(attempts) or decision_index != len(decisions):
        partial = True
    if not partial and completed.get("final_public_pass") is True:
        for stage in ("low_mid", "mid_high"):
            if not any(row.get("stage") == stage for row in stages):
                stages.append({"stage": stage, "state": "not_entered", "reason": "public_validation_passed",
                               "candidates": [], "economics": {"status": "not_applicable"}})
    return stages, partial


def _read(path):
    # Every path is built from controlled roots and the service's hashed run ID.
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise ValueError("Unsafe evidence")
    with path.open("rb") as stream:
        value = stream.read(4_194_305)
    if len(value) > 4_194_304:
        raise ValueError("Evidence too large")
    return json.loads(value)


@contextmanager
def _completed_evidence(directory):
    path = directory / ".lock"
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise ValueError("Unsafe evidence")
    # Open an existing lock without creating/modifying it; never wait for work.
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as stream:
        fcntl.flock(stream, fcntl.LOCK_SH | fcntl.LOCK_NB)
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


class TaskDecisionObserver:
    def __init__(self, service, store, registry):
        self.service, self.store, self.registry = service, store, dict(registry)

    def project(self, task):
        result = {"status": "unknown", "reason": "historical_evidence_unavailable",
                  "candidate_evidence": "not_provided", "binding": None,
                  "attempts": [], "stages": [], "costs": {
                      "known_api_usd": task.get("known_api_usd"),
                      "complete_api_usd": task.get("complete_api_usd"),
                      "unknown_events": task.get("unknown_events"),
                      "routing_estimates": "not_provided",
                      "customer_usage_charge": "separate_billing_ledger",
                      "enforceable_bound": "not_provided"},
                  "distributions_are_correctness_probabilities": False}
        try:
            binding = self.store.binding(task["client"], task["run"])
            if task.get('access_expired') or task.get('status') in ('expired', 'retired'):
                result['reason'] = 'historical_evidence_not_complete'
                return result  # Safe metadata stays separate; never reopen expired prompts.
            if self.registry.get((binding["pool"], binding["category"], binding["mode"])) != binding["contract"]:
                return result
            contract = self.service.contracts[binding["contract"]]
            internal = self.service.internal_id(binding["client"], binding["run"])
            job = _read(self.service.root / internal / "job.json")
            frozen = job["registered"]
            if (digest(frozen) != digest(contract.descriptor()) or job["request"]["run_id"] != binding["run"]
                    or job["request"]["contract_id"] != binding["contract"]
                    or frozen["mode"] != binding["mode"]):
                return result
            result["binding"] = {
                "pool_version": identifier(binding["pool"]), "category": identifier(binding["category"]),
                "mode": identifier(binding["mode"]), "contract_id": identifier(binding["contract"]),
                "contract_version": identifier(frozen["version"]),
                "routing_version": identifier(frozen.get("routing_version")), "bound_at": binding["created"],
            }
            result["status"], result["reason"] = "partial", "candidate_history_not_provided"
            if task.get("status") not in TERMINAL or task.get("observation") is not None:
                result["reason"] = "historical_evidence_not_complete"
                return result
            directory = Path(contract.runner.core.client.root) / "runs" / internal
            with _completed_evidence(directory):
                manifest, completed = _read(directory / "manifest.json"), _read(directory / "result.json")
                expected = contract.runner.core.configuration_descriptor()
                if (any(digest(manifest.get(key)) != digest(value) for key, value in expected.items())
                        or manifest.get("contract_version") != frozen["version"]
                        or manifest.get("mode") != binding["mode"]
                        or manifest.get("task", {}).get("category", binding["category"]) != binding["category"]
                        or manifest.get("backup_plan", {}).get("category_id", binding["category"]) != binding["category"]
                        or completed.get("run_id") != internal
                        or completed.get("config_sha256") != (digest(manifest) if "backup_plan" in manifest else
                            hashlib.sha256(json.dumps(manifest, sort_keys=True, ensure_ascii=False,
                                                     allow_nan=False).encode()).hexdigest())
                        or routing_trace(completed) != task.get("trace")):
                    raise ValueError("Historical link differs")
                for index, attempt in enumerate(completed["attempts"], 1):
                    tier = attempt.get("tier")
                    profile = attempt.get("profile") if "backup_plan" in manifest else manifest["profiles"].get(tier)
                    result["attempts"].append({"index": index, "tier": tier,
                        "state": "dispatched", "configuration": configuration(profile),
                        "passed": attempt["public"].get("passed"),
                        "transition": "initial" if index == 1 else
                            "availability_fallback" if completed["attempts"][index-2].get("stop") in
                            ("provider_rate_limited", "provider_unavailable") else "quality_recovery"})
                if "backup_plan" in manifest:
                    result["stages"], partial = _backup_stages(
                        directory, manifest, completed, contract.runner.core.health_store,
                        _window_context(self.service, internal, frozen))
                    result.update(status="partial" if partial else "available",
                        reason="saved_selection_unverifiable" if partial else "saved_historical_selection",
                        candidate_evidence="saved_selection")
                    result["costs"]["routing_estimates"] = ("available" if any(
                        row["economics"]["status"] == "available" for row in result["stages"]) else "not_provided")
                elif manifest.get("selection_policy", {}).get("schema_version") == "hierarchical-qcs-v1":
                    result["stages"] = _qcs_stages(directory, manifest, completed)
                    result.update(status="partial", reason="saved_qcs_selection",
                                  candidate_evidence="frozen_selection")
                    result["costs"]["routing_estimates"] = ("partial" if any(
                        row["economics"]["status"] == "partial" for row in result["stages"]) else "not_provided")
            return result
        except Exception:
            result.update(status="unknown", reason="historical_evidence_unavailable",
                          attempts=[], stages=[])
            return result


def task_observation(observer, task):
    features = unavailable()
    if observer is not None:
        try:
            # Independent saved metadata is legal for operator history after
            # raw client access expires; do not reopen a prompt to fill it.
            features = public_features(observer.service.feature_observation(task['client'], task['run']))
        except Exception:
            pass
    if observer is None:
        return {"schema_version": SCHEMA_VERSION, "task_decision": {
            "status": "not_provided", "reason": "runner_observation_not_provided"}, 'task_features': features}
    return {"schema_version": SCHEMA_VERSION, "task_decision": observer.project(task), 'task_features': features}

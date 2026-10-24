"""Allowlisted operator evidence; registration never grants dispatch authority.

Only registered server-side Core descriptors may supply candidate qualification
references. Public catalog flags and browser query fields cannot create evidence.
This projection does not run, refresh, reconcile or mutate any Core state.
"""
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import re

from cloudyfan_jev.scores.categories import resolve_category
from cloudyfan_jev.scores.catalog import digest
from cloudyfan_jev.scores.configuration import configuration_digest
from cloudyfan_jev.scores.health import HealthStore

from .store import MODES, TIERS, MonitorError

SCHEMA = "mrc-observation-v1"
PROFILE_FIELDS = ("model", "model_version", "effort", "provider", "quantization", "backend")
REQUIRED_IDENTITY = ("model", "model_version", "effort", "provider")
REASONS = {
    "candidate_qualification_not_provided", "candidate_identity_incomplete", "candidate_identity_mismatch",
    "contract_not_registered", "candidate_position_missing", "candidate_evidence_recorded",
    "formal_qualification_not_evaluated", "candidate_observation_not_supported", "qualification_evidence_expired",
    "qualification_category_mismatch",
    "quality_evidence_insufficient",
    "qualification_evidence_from_future",
    "duplicate_model_identity", "qualified_beyond_target", "missing_or_invalid_dimensions",
    "capability_context_or_provider_mismatch", "candidate_evidence_unavailable",
}
HEALTH_REASONS = {
    "health_observation_missing", "observation_in_future", "observation_stale", "status_unknown",
    "status_degraded", "snapshot_in_future", "cooldown_active", "recovery_required",
    *(field + suffix for field in ("uptime_last_5m", "uptime_last_30m", "uptime_last_1d")
      for suffix in ("_unit_unknown_or_invalid", "_unknown", "_below_minimum")),
    *(field + suffix for field in ("latency", "throughput")
      for suffix in ("_unit_unknown_or_invalid", "_invalid", "_outside_limit")),
    *(field + "_" + percentile + "_unknown" for field in ("latency", "throughput")
      for percentile in ("p50", "p75", "p90", "p99")),
}


def _identifier(value):
    if (isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/@+ -]{0,199}", value)
            and not value.lower().startswith(("users/", "private/", "tmp/", "bearer ", "https:", "http:"))):
        return value
    return None


def _hash(value):
    return value if isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value) else None


def _time(value):
    try:
        stamp = (value if isinstance(value, datetime) else
                 datetime.fromtimestamp(value, timezone.utc) if type(value) in (int, float)
                 else datetime.fromisoformat(value.replace("Z", "+00:00")))
        if stamp.tzinfo is None:
            return None
        return stamp.astimezone(timezone.utc).isoformat()
    except (TypeError, ValueError, AttributeError, OverflowError):
        return None


def _profile(value):
    value = value if isinstance(value, dict) else {}
    return {key: _identifier(value.get(key)) for key in PROFILE_FIELDS}


def _scope(view, profile=None, status=None):
    source = view.get("source_kind", "")
    model = (profile or {}).get("model") or ""
    if (isinstance(source, str) and any(tag in source for tag in ("synthetic", "mock", "fixture", "demo"))) or model.startswith("example/"):
        return "synthetic"
    return "research" if status == "research_admitted_only" else "unverified"


def _code(value):
    if value == "Task quality sufficiency unproven":
        return "quality_evidence_insufficient"
    return value if isinstance(value, str) and value in REASONS else "candidate_evidence_unavailable"


def _amount(value):
    if not isinstance(value, str) or len(value) > 80:
        return None
    try:
        parsed = Decimal(value)
        return value if parsed.is_finite() and parsed >= 0 else None
    except InvalidOperation:
        return None


def _interval(value):
    if not isinstance(value, dict):
        return None
    lower, upper = _amount(value.get("lower")), _amount(value.get("upper"))
    return {"lower": lower, "upper": upper} if lower is not None and upper is not None else None


def _speed(value):
    if not isinstance(value, dict):
        return None
    return {"metric": value.get("metric") if value.get("metric") in ("flow_ms", "whole_task_flow_ms") else None,
            "statistic": value.get("statistic") if value.get("statistic") in ("mean", "p50", "p95", "p99") else None,
            "upper": _amount(value.get("upper"))}


def _metric(value):
    if type(value) not in (int, float):
        return None
    try:
        return value if Decimal(str(value)).is_finite() and value >= 0 else None
    except InvalidOperation:
        return None


def _endpoint_metrics(observation):
    health, speed = observation.get("health") or {}, observation.get("speed") or {}
    metrics = {key: _metric(health[key]) for key in ("status", "uptime_last_5m", "uptime_last_30m", "uptime_last_1d")
               if key in health}
    metrics["uptime_unit"] = "percent" if health.get("uptime_unit") == "percent" else None
    for field in ("latency_last_30m", "throughput_last_30m"):
        values = speed.get(field)
        if isinstance(values, dict):
            metrics[field] = {key: _metric(values[key]) for key in ("p50", "p75", "p90", "p99") if key in values}
    for key, unit in (("latency_unit", "milliseconds"), ("throughput_unit", "tokens/second")):
        metrics[key] = unit if speed.get(key) == unit else None
    return metrics


class CandidateObserver:
    """Read current candidate metadata against the host's exact registered contract."""
    def __init__(self, store, service, registry):
        self.store, self.service, self.registry = store, service, dict(registry)

    def _versions(self, control):
        with self.store.db() as db:
            rows = [dict(row) for row in db.execute("SELECT id,created FROM pools ORDER BY created DESC,id")]
        return [{"pool_id": row["id"], "saved_at": _time(row["created"]),
                 "active": row["id"] == control["routing_pool"],
                 "catalog": row["id"] == control["catalog_pool"]} for row in rows if _hash(row["id"])]

    def _contract(self, pool, category, mode):
        contract_id = self.registry.get((pool, category, mode))
        contract = self.service.contracts.get(contract_id)
        if contract is None:
            return None, None, None
        # Validate the real registered contract; never copy paths/system/payloads
        # from the returned descriptor into a public response.
        descriptor = contract.descriptor()
        if descriptor.get("mode") != mode:
            raise ValueError("Registered mode differs")
        return _identifier(contract_id), descriptor, contract.runner.core

    def _records(self, descriptor):
        if not descriptor:
            return {}, []
        backup = descriptor.get("backup_plan")
        if isinstance(backup, dict):
            return {tier: backup.get("pools", {}).get(tier, []) for tier in TIERS}, backup.get("excluded", [])
        plan = descriptor.get("selection_policy")
        if isinstance(plan, dict):
            records = {}
            for tier, profile in plan.get("profiles", {}).items():
                selection = profile.get("capability_card", {}).get("score_selection", {})
                records[tier] = [{"entry": {"profile": profile, "config_id": selection.get("config_id")},
                                  "evidence": plan.get("records", {}).get(tier, {})}]
            return records, plan.get("excluded", [])
        return {}, []

    def _qualification(self, profile, record, observed_at, category):
        if record is None:
            return {"status": "not_provided", "formal_status": "unknown", "evidence": None,
                    "reason_codes": ["candidate_qualification_not_provided", "formal_qualification_not_evaluated"]}
        evidence = record.get("evidence", {})
        provenance = evidence.get("provenance", {})
        if provenance.get("category_id") != category:
            return {"status": "unverifiable", "formal_status": "unknown", "evidence": None,
                    "reason_codes": ["qualification_category_mismatch", "formal_qualification_not_evaluated"]}
        exact = configuration_digest({"profile": profile})
        if evidence.get("configuration_sha256") != exact:
            return {"status": "unverifiable", "formal_status": "unknown", "evidence": None,
                    "reason_codes": ["candidate_identity_mismatch", "formal_qualification_not_evaluated"]}
        valid_until = _time(provenance.get("valid_until"))
        retrieved_at = _time(provenance.get("retrieved_at"))
        if retrieved_at is not None and retrieved_at > observed_at:
            return {"status": "unverifiable", "formal_status": "unknown", "evidence": None,
                    "reason_codes": ["qualification_evidence_from_future", "formal_qualification_not_evaluated"]}
        status = "expired" if valid_until is not None and valid_until <= observed_at else "evidence_recorded"
        return {"status": status, "formal_status": "unknown",
                "evidence": {"configuration_sha256": exact,
                    "raw_sha256": _hash(provenance.get("raw_sha256")),
                    "workload_id": _identifier(provenance.get("workload_id")),
                    "category_id": _identifier(provenance.get("category_id")),
                    "observed_at": retrieved_at, "valid_until": valid_until,
                    "quality_sufficient": evidence.get("quality_sufficient") is True,
                    "complete_cost_usd": _interval(evidence.get("complete_cost_usd")),
                    "speed": _speed(evidence.get("speed")),
                    "basis": "registered_core_qcs_evidence"},
                "reason_codes": ["qualification_evidence_expired" if status == "expired" else
                                 "candidate_evidence_recorded", "formal_qualification_not_evaluated"]}

    def _health(self, profile, record, descriptor, core, category):
        health = getattr(core, "health_store", None)
        if not isinstance(health, HealthStore):
            return {"status": "not_provided"}
        exact = configuration_digest({"profile": profile})
        evidence = (record or {}).get("evidence", {})
        if (record is None or evidence.get("configuration_sha256") != exact
                or evidence.get("provenance", {}).get("category_id") != category):
            return {"status": "unverifiable", "reason_codes": ["health_identity_unverifiable"]}
        policy = descriptor.get("health_policy")
        bound_policy = descriptor.get("backup_plan", {}).get("qcs_policy", {}).get("health_policy")
        if policy != health.policy or bound_policy != policy:
            return {"status": "unverifiable", "reason_codes": ["health_policy_unverifiable"]}
        assessed = health.assess(exact, profile["provider"])
        observation = assessed["observation"] or {}
        reasons = [code if code in HEALTH_REASONS else "health_reason_unavailable" for code in assessed["reasons"]]
        stale, future = "observation_stale" in reasons, any(code in reasons for code in (
            "observation_in_future", "snapshot_in_future"))
        freshness = "future" if future else "stale" if stale else "missing" if not observation else "fresh"
        state = ("cooldown" if "cooldown_active" in reasons else
                 "recovery_required" if "recovery_required" in reasons else
                 "unverifiable" if future or "health_reason_unavailable" in reasons else
                 "missing" if not observation else "stale" if stale else
                 "unknown" if "status_unknown" in reasons else "degraded" if reasons else "healthy")
        return {"status": state, "policy_eligible": assessed["eligible"],
            "configuration_sha256": exact, "provider": _identifier(profile["provider"]),
            "policy_sha256": digest(policy), "ttl_seconds": policy["ttl_seconds"],
            "assessed_at": _time(health.clock()), "observed_at": _time(observation.get("observed_at")),
            "snapshot_id": _hash(assessed["snapshot_id"]), "freshness": freshness,
            "circuit": {"open": assessed["circuit"]["open"] is True,
                "failures": assessed["circuit"]["failures"] if type(assessed["circuit"]["failures"]) is int else None,
                "last_failure_at": _time(assessed["circuit"].get("last_failure_at")),
                "cooldown_until": _time(assessed["circuit"].get("cooldown_until"))},
            "reason_codes": reasons, "endpoint_metrics": _endpoint_metrics(observation)}

    def _candidate(self, candidate, position, tier, view, descriptor, records, observed_at, registered, category, core):
        raw_profile = candidate.get("profile") if isinstance(candidate, dict) else None
        profile = _profile(raw_profile)
        listed = profile["model"] is not None
        missing = [key for key in REQUIRED_IDENTITY if profile[key] is None]
        raw_profile = raw_profile if isinstance(raw_profile, dict) else {}
        matched = None
        for record in records.get(tier, []):
            target = record.get("entry", {}).get("profile", {})
            if listed and configuration_digest({"profile": raw_profile}) == configuration_digest({"profile": target}):
                matched = record
                break
        runtime_profile = (descriptor or {}).get("profiles", {}).get(tier)
        runtime_match = matched is not None or (listed and isinstance(runtime_profile, dict)
            and configuration_digest({"profile": raw_profile}) == configuration_digest({"profile": runtime_profile}))
        qualification = self._qualification(raw_profile, matched, observed_at, category) if listed else {
            "status": "not_applicable", "formal_status": "not_applicable", "evidence": None,
            "reason_codes": ["candidate_position_missing"]}
        reasons = list(qualification["reason_codes"])
        if listed and missing:
            reasons.append("candidate_identity_incomplete")
        if not registered:
            reasons.append("contract_not_registered")
        elif listed and not runtime_match:
            reasons.append("candidate_identity_mismatch")
        config_id = candidate.get("config_id") if isinstance(candidate, dict) else None
        if matched is not None:
            config_id = matched.get("entry", {}).get("config_id")
        if listed and not missing:
            try:
                health = self._health(raw_profile, matched, descriptor, core, category)
            except Exception:
                # One unreadable source is not health evidence for any row. Do
                # not forward private storage/transport exception text.
                health = {"status": "unavailable", "policy_eligible": None,
                          "assessed_at": observed_at, "reason_codes": ["health_evidence_unavailable"]}
        else:
            health = {"status": "not_applicable" if not listed else "unverifiable",
                      "reason_codes": [] if not listed else ["health_identity_unverifiable"]}
        return {"position": position, "role": "primary" if position == 0 else "backup",
                "listing_status": "listed" if listed else "missing", "scope": _scope(view, profile, candidate.get("status")),
                "configuration": {"config_id": _identifier(config_id),
                    "configuration_sha256": configuration_digest({"profile": raw_profile}) if listed and not missing else None,
                    **profile},
                "identity": {"status": "complete" if listed and not missing else "incomplete" if listed else "not_applicable",
                             "missing_fields": missing if listed else []},
                "registration": {"status": "registered" if registered else "not_registered",
                                 "candidate_matches_contract": runtime_match if registered and listed else None},
                "qualification": qualification, "health": health,
                "quota": {"status": "unknown"}, "dispatch_authorization": {"status": "not_checked"},
                "reason_codes": reasons}

    def project(self, view, *, category=None, mode=None):
        control = self.store.control()
        try:
            category = resolve_category(category or "artificial-analysis:coding_index")
        except ValueError:
            raise MonitorError("unknown_observation_category") from None
        mode = mode or control["mode"]
        if mode not in MODES:
            raise MonitorError("unsupported_mode")
        if category not in view["categories"]:
            raise MonitorError("unknown_observation_category")
        observed_at = _time(self.store.clock())
        contract_id, descriptor, core = self._contract(view["id"], category, mode)
        records, excluded = self._records(descriptor)
        return {"schema_version": SCHEMA, "candidates": {
            "status": "available", "observed_at": observed_at,
            "context": {"pool_id": view["id"], "active_pool_id": control["routing_pool"],
                "catalog_pool_id": control["catalog_pool"], "active": view["id"] == control["routing_pool"],
                "category": category, "mode": mode, "contract_id": contract_id,
                "contract_registered": descriptor is not None,
                "contract_version": _identifier((descriptor or {}).get("version"))},
            "versions": self._versions(control),
            "tiers": {tier: [self._candidate(candidate, index, tier, view, descriptor, records,
                                             observed_at, descriptor is not None, category, core)
                             for index, candidate in enumerate(view["categories"][category]["pools"][tier])]
                      for tier in TIERS},
            "excluded": [{"tier": row.get("tier") if row.get("tier") in TIERS else None,
                "config_id": _identifier(row.get("config_id")), "reason_codes": [_code(row.get("reason"))]}
                for row in excluded if isinstance(row, dict)],
            "notice": "registration_not_execution_authorization"}}

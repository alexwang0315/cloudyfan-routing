"""Private SQL research support extracted from the preserved S50 adapter.

Admission, ranking, health and actual/scenario accounting remain unchanged.
Fixed-direct selection and its constructors are deliberately not exposed here;
use sql_selector_research's evidence-required public constructor.
"""

import hashlib
from copy import deepcopy
from decimal import Decimal
from pathlib import Path

from cloudyfan_jev.scores.backup_runtime import BackupCategoryCore
from cloudyfan_jev.scores.catalog import digest, timestamp
from cloudyfan_jev.scores.configuration import configuration_digest
from cloudyfan_jev.scores.qcs import _d, _interval
from cloudyfan_jev.scores.workflow_research import SequentialResearchCore

VERSION = "s50-provisional-sequential-v1"
TIERS = ("open_source", "low", "mid", "high")
ZERO_SCENARIO = {
    "version": "s50-oss-initial-zero-v1",
    "modes": ["cloudyfan", "open_source_first"],
    "tier": "open_source",
    "attempt": 0,
}


def source_check(sources):
    if not isinstance(sources, dict) or not sources:
        raise ValueError("Provisional evidence requires named immutable public sources")
    for name, expected in sources.items():
        path = Path(name)
        if (
            not path.is_absolute()
            or path.is_symlink()
            or not path.is_file()
            or any(
                part in ("private", "authoring") or part.startswith(".env") for part in path.parts
            )
            or hashlib.sha256(path.read_bytes()).hexdigest() != expected
        ):
            raise ValueError("Provisional source is missing, changed or not public")


def pool_contract(plan):
    return digest({k: v for k, v in plan.items() if k not in ("qcs_policy", "backup_id")})


class _ProvisionalSQLSupport(SequentialResearchCore):
    """Versioned opt-in research only; inherits the actual sequential execution."""

    _research_version = VERSION

    def _verify_backup_plan(self):
        plan = self._backup
        if (
            plan.get("schema_version") != "s50-provisional-backup-v1"
            or plan.get("backup_id") != digest({k: v for k, v in plan.items() if k != "backup_id"})
            or set(plan.get("pools", {})) != set(TIERS)
        ):
            raise ValueError("Explicit intact provisional research pool required")
        policy = plan.get("provisional_policy", {})
        if (
            policy.get("schema_version") != "s50-provisional-evidence-v1"
            or policy.get("unknown_quality") != "allow_research_no_guarantee"
            or policy.get("unknown_speed") != "allow_without_sla"
            or policy.get("unknown_health") != "allow_unobserved_only"
            or policy.get("formal_qualification") != "pending"
        ):
            raise ValueError("Provisional unknown evidence policy must be explicit")
        timestamp(policy["valid_until"])
        source_check(policy["source_files"])
        required = plan["requirements"]
        if type(required["context_tokens"]) is not int or required["context_tokens"] < 1:
            raise ValueError("Research context requirement is invalid")
        for rows in plan["pools"].values():
            if not isinstance(rows, list):
                raise ValueError("Explicit provisional candidate list required")
            seen = set()
            for row in rows:
                entry, evidence = row["entry"], row["evidence"]
                identity = configuration_digest(entry)
                if (
                    identity in seen
                    or row["configuration_sha256"] != identity
                    or evidence["configuration_sha256"] != identity
                    or entry.get("approved") is not False
                    or entry.get("research_only") is not True
                    or entry["context_tokens"] < required["context_tokens"]
                    or not set(required["capabilities"]) <= set(entry["capabilities"])
                ):
                    raise ValueError(
                        "Provisional identity, context or capability declaration mismatch"
                    )
                seen.add(identity)
                if evidence.get("cost_basis") != "derived_posted_token_scenario":
                    raise ValueError("Explicit derived cost basis required; no hard-bound claim")
                generation = _interval(evidence["generation_cost_usd"])
                complete = _interval(evidence["complete_cost_usd"])
                if complete[1] < generation[1]:
                    raise ValueError("Complete research cost omits generation")
                risk = evidence.get("failure_probability")
                if risk != {"lower": "0", "upper": "1"}:
                    raise ValueError("This provisional version has unknown comparative success")
                quality = evidence.get("quality", {})
                if (
                    quality.get("basis") not in ("unknown", "family_reference")
                    or quality.get("exact_SQL_quality_guaranteed") is not False
                ):
                    raise ValueError("Family reference cannot assert SQL quality")
                if quality.get("score") is not None:
                    _d(str(quality["score"]))
                speed = evidence.get("speed", {})
                if speed.get("upper") is None:
                    if speed.get("metric") != "unknown" or speed.get("statistic") is not None:
                        raise ValueError("Unknown speed cannot claim a percentile")
                elif speed.get("metric") != "endpoint_latency_ms":
                    raise ValueError("Only named endpoint latency observations are comparable here")
                else:
                    _d(speed["upper"])
                    if speed.get("statistic") not in ("p50", "p75", "p90", "p99"):
                        raise ValueError("Named endpoint speed percentile required")
                for stage in ("low_mid", "mid_high"):
                    interval = _interval(evidence["remaining_cost_by_stage"][stage])
                    if interval[1] < generation[1]:
                        raise ValueError("Remaining cost omits current generation")
                    if stage == "mid_high" and interval[1] != generation[1]:
                        raise ValueError(
                            "Final selection may not reserve an imaginary fourth answer"
                        )
                source_check(evidence["source_files"])



    def _require_policy_fresh(self):
        self._verify_backup_plan()
        self._validate_research_policy(self._now().isoformat())

    def _require_candidate_fresh(self, record):
        self._require_policy_fresh()
        if not any(record in rows for rows in self._backup["pools"].values()):
            raise ValueError("Provisional candidate is outside frozen research membership")

    def _rank_record(self, record):
        evidence = record["evidence"]
        speed = evidence["speed"]["upper"]
        score = evidence["quality"].get("score")
        return (
            _interval(evidence["generation_cost_usd"])[1],
            speed is None,
            Decimal(0) if speed is None else _d(speed),
            score is None,
            Decimal(0) if score is None else -_d(str(score)),
            record["entry"]["config_id"],
        )

    def _health_reasons(self, assessment):
        # A missing observation is a research probe, not a positive health result.
        # Existing degraded/stale observations and open circuits are never erased.
        if assessment["eligible"]:
            return []
        if (
            assessment.get("observation") is None
            and not assessment.get("circuit", {}).get("open")
            and set(assessment["reasons"]) <= {"health_observation_missing"}
        ):
            return []
        return list(assessment["reasons"])

    def _health_choice(self, tier, failed_providers, excluded_configs):
        eligible, observations = [], []
        for record in self._backup["pools"][tier]:
            profile = record["entry"]["profile"]
            health = self.health_store.assess(record["configuration_sha256"], profile["provider"])
            reasons = self._health_reasons(health)
            try:
                self._require_candidate_fresh(record)
            except (ValueError, KeyError, TypeError, AttributeError):
                reasons.append("provisional_evidence_stale_or_invalid")
            if profile["provider"] in failed_providers:
                reasons.append("provider_failed_this_run")
            if record["configuration_sha256"] in excluded_configs:
                reasons.append("configuration_already_dispatched")
            observations.append(
                {
                    "config_id": record["entry"]["config_id"],
                    "assessment": health,
                    "reasons": reasons,
                    "research_probe_with_unknown_health": not health["eligible"] and not reasons,
                }
            )
            if not reasons:
                eligible.append(record)
        return (deepcopy(min(eligible, key=self._rank_record)) if eligible else None), observations

    def _require_active_health(self):
        self._require_candidate_fresh(self._active_record)
        profile = self._active_record["entry"]["profile"]
        health = self.health_store.assess(
            self._active_record["configuration_sha256"], profile["provider"]
        )
        if self._health_reasons(health):
            raise RuntimeError("Dispatch stopped: known unavailable or stale research endpoint")


    def configuration_descriptor(self):
        descriptor = super().configuration_descriptor()
        descriptor.update(
            engine_version="0.5.1",
            routing_version=VERSION,
            evidence_mode="provisional_research_not_qualified",
            cost_comparison=deepcopy(ZERO_SCENARIO),
        )
        return descriptor

    def execution_contract_descriptor(self, contract_version):
        descriptor = super().execution_contract_descriptor(contract_version)
        descriptor["core"]["research_envelope"]["zero_first_model_fee_scenario"] = deepcopy(
            ZERO_SCENARIO
        )
        return descriptor

    def _with_costs(self, result):
        # Verify immutable paid identities/accounting using the existing backup layer.
        result = BackupCategoryCore._with_costs(self, result)
        amount, unknown, waived = Decimal(0), False, []
        for index, attempt in enumerate(result["attempts"]):
            for call in attempt["calls"]:
                if (
                    index == 0
                    and attempt["tier"] == "open_source"
                    and result["mode"] in ZERO_SCENARIO["modes"]
                ):
                    waived.append(call["call_id"])
                elif call["cost_unknown"]:
                    unknown = True
                else:
                    amount += Decimal(call["cost_usd"])
        for call in result.get("selection", {}).get("calls", []):
            if call["cost_unknown"]:
                unknown = True
            else:
                amount += Decimal(call["cost_usd"])
        result["costs"].update(
            hypothetical_open_source_zero_usd=None if unknown else str(amount),
            cost_scenario_version=ZERO_SCENARIO["version"],
            waived_call_ids=waived,
            scenario=deepcopy(ZERO_SCENARIO),
        )
        result.update(
            research_policy_version=VERSION, evidence_mode="provisional_research_not_qualified"
        )
        return result

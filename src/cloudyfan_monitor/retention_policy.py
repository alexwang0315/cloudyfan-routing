"""Prompt-free evidence retention policy. This module never removes files."""
from dataclasses import dataclass


DAY = 86400


@dataclass(frozen=True)
class EvidenceRetentionPolicy:
    version: str = "monitor-evidence-retention-v1"
    task_access_days: int = 30
    raw_review_after_days: int = 30
    evidence_min_days: int = 365

    def descriptor(self):
        return {"version": self.version, "task_access_days": self.task_access_days,
                "raw_review_after_days": self.raw_review_after_days,
                "evidence_min_days": self.evidence_min_days, "automatic_deletion": False,
                "version_evidence_clock": "last_task_use", "record_evidence_clock": "creation"}

    def review(self, binding, *, now, version_last_used, references, accounting, core_preview):
        raw_due = now >= binding["created"] + self.raw_review_after_days * DAY
        retain_until = max(binding["created"], version_last_used) + self.evidence_min_days * DAY
        blockers = list(core_preview.get("blockers", []))
        if not raw_due:
            blockers.append("raw_review_not_due")
        if now < retain_until:
            blockers.append("minimum_evidence_period")
        if references:
            blockers.append("active_task_reference")
        if accounting.get("unknown_events"):
            blockers.append("unknown_accounting")
        if accounting.get("violations") or accounting.get("unbounded_history_events"):
            blockers.append("accounting_evidence_hold")
        blockers = sorted(set(blockers))
        return {"policy": self.descriptor(), "client": binding["client"], "run_id": binding["run"],
                "raw_review_due": raw_due, "raw_review_at": binding["created"] + self.raw_review_after_days * DAY,
                "evidence_retain_until": retain_until, "version_last_used": version_last_used,
                "active_references": references, "eligible": core_preview.get("eligible") is True and not blockers,
                "blockers": blockers, "core_preview": core_preview,
                "preview_only": True, "deletion_authorized": False}

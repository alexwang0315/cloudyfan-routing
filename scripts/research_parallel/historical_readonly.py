"""Offline archival linkage, never a paid launcher or SQL qualification runner.

Only public metadata and already saved request/response evidence are inspected.
The output intentionally contains no prompts, answers, headers, or credentials.
"""

import argparse
import builtins
import hashlib
import io
import json
import socket
from contextlib import ExitStack, contextmanager
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from cloudyfan_jev.client import usage
from concurrent_ledger import ConcurrentLedger
from core_bridge import ConcurrentClient
from parallel_runner import HistoricalCompletion, Job, digest, read, write_once

VERSION = "historical-readonly-linkage-v1"
WORKSPACE = Path(__file__).resolve().parents[3]
OLD = WORKSPACE / "artifacts/routing-v2-s50-20261022"
EXECUTION = OLD / "execution-unpaid-selection-v8"
BASELINE = (WORKSPACE / "artifacts/routing-parallel-four-arm-offline-20261022"
            / "preservation-before.json")
BASELINE_SHA256 = "7103df2c9fe15d8c8ae20defc45bb33761214372a0e7db8386e47bedac1175ab"
EXPORT = EXECUTION / "exports/609cd008d0b1481ca2ebea8c1f7b16f2"
PRIOR_KNOWN = "0.243455651"
HISTORICAL_RESERVE = "0.01"
PENDING_CID = "run-s50v2-S50-038-C-a0-t0"


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _sensitive(path):
    if isinstance(path, int):
        return False
    parts = Path(path).expanduser().parts
    # macOS's canonical temporary root is /private/var; it is not a bank folder.
    if parts[:3] == ("/", "private", "var"):
        parts = parts[3:]
    return any(p in {"private", "authoring", ".ssh", ".aws"} for p in parts) or any(
        p == ".env" or p.startswith(".env.") or p in {"credentials", "credentials.json"}
        for p in parts
    )


@contextmanager
def offline_guard():
    """Fail closed on network and sensitive file opens, including indirect ones."""
    original_open, original_io = builtins.open, io.open

    def guarded(opener):
        def open_file(path, *args, **kwargs):
            if _sensitive(path):
                raise PermissionError("SENSITIVE_OR_HIDDEN_SOURCE_FORBIDDEN")
            return opener(path, *args, **kwargs)
        return open_file

    def denied(*args, **kwargs):
        raise RuntimeError("OFFLINE_ARCHIVE_NETWORK_FORBIDDEN")

    with ExitStack() as stack:
        stack.enter_context(patch("builtins.open", guarded(original_open)))
        stack.enter_context(patch("io.open", guarded(original_io)))
        for name in ("socket", "create_connection", "getaddrinfo"):
            stack.enter_context(patch.object(socket, name, denied))
        yield


class Archive:
    """All reads must match the parent's pre-change preservation manifest."""

    def __init__(self, workspace=WORKSPACE, old=OLD, baseline=BASELINE,
                 baseline_sha256=BASELINE_SHA256):
        self.workspace, self.old = Path(workspace).resolve(), Path(old).resolve()
        self.baseline = Path(baseline).resolve()
        if sha(self.baseline) != baseline_sha256:
            raise ValueError("PARENT_PRESERVATION_MANIFEST_CHANGED")
        self.pins = {}
        for name, expected in read(self.baseline)["original_files"].items():
            p = Path(name)
            p = (p if p.is_absolute() else self.workspace / p).resolve()
            self.pins[str(p)] = expected
        self.declared, self.seen = {}, {str(self.baseline): baseline_sha256}

    def checked(self, path, expected=None):
        p = Path(path).resolve()
        if _sensitive(p) or not (p.is_relative_to(self.old) or str(p) in self.declared):
            raise ValueError("ARCHIVE_PATH_OUTSIDE_FROZEN_SCOPE")
        pinned = self.pins.get(str(p))
        if pinned is None and not p.is_relative_to(self.old):
            # The preserved original plan is itself the immutable authority for
            # its explicitly declared external public sources.
            pinned = self.declared.get(str(p))
        actual = sha(p)
        if pinned is None or actual != pinned or (expected is not None and actual != expected):
            raise ValueError("ARCHIVED_SOURCE_HASH_CHANGED")
        self.seen[str(p)] = actual
        return p

    def json(self, path, expected=None):
        return read(self.checked(path, expected))

    def verify_again(self):
        if any(sha(name) != expected for name, expected in self.seen.items()):
            raise ValueError("ORIGINAL_ARCHIVE_CHANGED_DURING_DEMO")


def build_historical_bindings(*, archive=None, execution=EXECUTION, export=EXPORT,
                              expected_completed=115):
    """Return verified Job/HistoricalCompletion maps without building any Core."""
    archive = archive or Archive()
    execution, export = Path(execution).resolve(), Path(export).resolve()
    plan_path = execution / "study/plan.json"
    plan = archive.json(plan_path)
    archive.declared = {str(Path(p).resolve()): h for p, h in plan["sources"].items()}
    common = {str(plan_path): archive.seen[str(plan_path)]}
    for name, expected in plan["sources"].items():
        p = archive.checked(name, expected)
        common[str(p)] = expected
    ledger_path, report_path = export / "ledger.json", export / "report.json"
    archived_ledger = archive.json(ledger_path)
    archive.checked(report_path)
    common.update({str(p): archive.seen[str(p)] for p in (ledger_path, report_path)})
    if (Decimal(archived_ledger["new_known_reconciled_api_usd"]) != Decimal("0.213780739")
            or Decimal(archived_ledger["prior_known_api_usd"]) != Decimal("0.029674912")):
        raise ValueError("ARCHIVED_COST_PREFIX_CHANGED")
    rows = archived_ledger["rows"]
    gids = [r["generation_id"] for r in rows if r.get("generation_id")]
    if len(gids) != len(set(gids)) or any(r.get("cost_unknown") for r in rows):
        raise ValueError("ARCHIVED_LEDGER_UNKNOWN_OR_DUPLICATE_GENERATION")
    jobs, links = {}, {}
    ordinals = {r["question_id"]: r["ordinal"] for r in plan["schedule"]}
    allowed_summary = ("task_id", "arm", "status", "first_public_pass", "final_public_pass",
                       "execution_interrupted", "interruption_kind", "generation_calls",
                       "classifier_calls", "known_actual_api_usd", "billing_complete")
    for terminal_path in sorted((execution / "study").glob("terminal-*.json")):
        terminal = archive.json(terminal_path)
        qid, arm, run_id = terminal["task_id"], terminal["arm"], terminal["run_id"]
        if (run_id != f"s50v2-{qid}-{arm}" or arm not in "ABC"
                or terminal_path.name != f"terminal-{qid}-{arm}.json"):
            raise ValueError("ARCHIVED_TERMINAL_IDENTITY_CHANGED")
        evidence = dict(common)
        evidence[str(terminal_path)] = archive.seen[str(terminal_path)]
        for name, expected in terminal["files"].items():
            p = archive.checked(name, expected)
            evidence[str(p)] = expected
        run_root = execution / "capture/runs" / run_id
        manifest_path, result_path = run_root / "manifest.json", run_root / "result.json"
        if not {str(manifest_path), str(result_path)} <= evidence.keys():
            raise ValueError("ARCHIVED_TERMINAL_MISSING_RUN_BINDING")
        manifest, result = archive.json(manifest_path), archive.json(result_path)
        configuration = {k: v for k, v in manifest.items()
                         if k not in {"task", "mode", "contract_version"}}
        contract = plan["public_contracts"][qid]
        if (digest(configuration) != plan["run_configuration_sha256"][run_id]
                or digest(manifest["task"]) != plan["run_public_task_sha256"][run_id]
                or manifest["contract_version"] != contract["contract_version"]
                or result["run_id"] != run_id
                or result["final_public_pass"] != terminal["final_public_pass"]):
            raise ValueError("ARCHIVED_TASK_CONFIG_CONTRACT_OR_RESULT_CHANGED")
        bindings = {"task_sha256": plan["run_public_task_sha256"][run_id],
                    "core_configuration_sha256": plan["run_configuration_sha256"][run_id],
                    "contract_version": contract["contract_version"],
                    "parent_plan_sha256": archive.seen[str(plan_path)]}
        job = Job(run_id, qid, arm, ordinals[qid], "0.05", bindings)
        summary = {k: deepcopy(terminal.get(k)) for k in allowed_summary}
        summary["original_terminal_actual_api_usd"] = terminal["known_actual_api_usd"]
        summary["archived_reconciled_api_usd"] = str(sum(
            (Decimal(r["cost_usd"]) for r in rows
             if r["call_id"].startswith("run-" + run_id + "-") and r["cost_usd"] is not None),
            Decimal(0)))
        summary["api_latency_from_replay"] = None
        jobs[run_id] = job
        links[run_id] = HistoricalCompletion(job.identity, evidence, summary,
                                             PRIOR_KNOWN, HISTORICAL_RESERVE)
    if len(jobs) != expected_completed:
        raise ValueError("ARCHIVED_COMPLETED_COUNT_CHANGED")
    return {"archive": archive, "jobs": jobs, "completions": links,
            "ledger": archived_ledger, "plan": plan, "common_sources": common,
            "execution": execution, "export": export}


def run_demo(output, *, bundle=None):
    """Two reads of the same paid C038 answer, after synthetic expiry, zero send."""
    output = Path(output).resolve()
    if output.exists():
        raise FileExistsError("NEW_OUTPUT_DIRECTORY_REQUIRED")
    with offline_guard():
        bundle = bundle or build_historical_bindings()
        archive, execution = bundle["archive"], bundle["execution"]
        if output.is_relative_to(archive.old) or _sensitive(output):
            raise ValueError("OUTPUT_MUST_NOT_MODIFY_ORIGINAL_ARCHIVE")
        output.mkdir(parents=True, mode=0o700)
        clock = [0.0]
        ledger = ConcurrentLedger(output / "ledger.sqlite", plan_id="archive-demo-v1",
                                  target_usd="1", prior_known_usd=PRIOR_KNOWN,
                                  historical_reserve_usd=HISTORICAL_RESERVE,
                                  deadline=1.0, clock=lambda: clock[0])
        for run_id, link in bundle["completions"].items():
            link.verify(bundle["jobs"][run_id], ledger)
        folder = execution / "capture/calls" / PENDING_CID
        intent_path, response_path = folder / "intent.json", folder / "response.json"
        intent, original = archive.json(intent_path), archive.json(response_path)
        row = next(r for r in bundle["ledger"]["rows"] if r["call_id"] == PENDING_CID)
        if (row["cost_unknown"] or row["generation_id"] != usage(original)["generation_id"]
                or row["cost_usd"] != usage(original)["cost_usd"]):
            raise ValueError("PENDING_ARCHIVED_FEE_OR_IDENTITY_CHANGED")
        source_evidence = dict(bundle["common_sources"])
        source_evidence.update({str(p): archive.seen[str(p)] for p in (intent_path, response_path)})
        for p in folder.glob("*.json"):
            archive.checked(p)
            source_evidence[str(p)] = archive.seen[str(p)]
        entry = {"intent_path": str(intent_path), "response_path": str(response_path),
                 "source_evidence": source_evidence, "required_prior_known_usd": PRIOR_KNOWN}
        sends = []

        def no_send(*args):
            sends.append(True)
            raise AssertionError("ARCHIVED_PAID_REQUEST_MUST_NEVER_BE_SENT")

        def verifier(record):
            if any(record.get(k) != original.get(k) for k in ("raw", "headers", "http_status")):
                raise ValueError("ARCHIVED_RAW_RESPONSE_CHANGED")
            return {"verified": True, "complete_request_cost": True,
                    "source": "hash_bound_archived_export_not_live_supplier_qualification",
                    "generation_id": row["generation_id"], "cost_usd": row["cost_usd"]}

        ledger.admit("s50v2-S50-038-C", "0.05", 2, 0)
        clock[0] = 2.0
        client = ConcurrentClient(output / "capture", workflow_id="s50v2-S50-038-C",
                                  ledger=ledger, sender=no_send, fee_verifier=verifier,
                                  forecast=lambda *args: (_ for _ in ()).throw(AssertionError()),
                                  pre_dispatch_check=lambda *args: False,
                                  historical_calls={PENDING_CID: entry}, clock=lambda: clock[0])
        records = [client.call(PENDING_CID, intent["url"], intent["payload"],
                               headers={"X-OpenRouter-Metadata": "enabled"}) for _ in range(2)]
        snapshot = ledger.snapshot()
        workflow = ledger.lookup_workflow("s50v2-S50-038-C")
        if (sends or snapshot["actual_usd"] != "0" or workflow["generations_started"] != 1
                or records[0] != records[1] or not snapshot["expired"]):
            raise AssertionError("HISTORICAL_LINKAGE_INVARIANT_FAILED")
        archive.verify_again()
        report = {"version": VERSION, "data_kind": "offline_archival_provenance_demo",
                  "completed_linked": len(bundle["jobs"]),
                  "old_pending": len(bundle["plan"]["run_configuration_sha256"])
                  - len(bundle["jobs"]),
                  "hosted_pending": len(bundle["plan"]["public_contracts"]),
                  "remaining_four_arm_workflows": len(bundle["plan"]["run_configuration_sha256"])
                  - len(bundle["jobs"]) + len(bundle["plan"]["public_contracts"]),
                  "new_model_sends": len(sends), "core_constructions": 0,
                  "sql_validations": 0, "hidden_grading": "not_read",
                  "prior_known_usd": PRIOR_KNOWN, "historical_reserve_usd": HISTORICAL_RESERVE,
                  "imported_original_call_cost_usd": row["cost_usd"],
                  "new_actual_usd": snapshot["actual_usd"],
                  "known_cumulative_usd": snapshot["known_cumulative_usd"],
                  "saved_call_reads": 2, "generation_attempts_consumed": 1,
                  "synthetic_authorization_expired": snapshot["expired"],
                  "original_sources_unchanged": True, "verified_file_count": len(archive.seen),
                  "unchanged_scope": "referenced original sources; not an entire tree audit",
                  "qualification": "not_a_live_migration_or_supplier_qualification",
                  "source_evidence": archive.seen,
                  "completed": {key: link.summary for key, link in bundle["completions"].items()}}
        write_once(output / "summary.json", report)
        return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    result = run_demo(args.output)
    print(json.dumps({k: result[k] for k in ("completed_linked", "remaining_four_arm_workflows",
                                           "new_model_sends", "new_actual_usd")}, sort_keys=True))

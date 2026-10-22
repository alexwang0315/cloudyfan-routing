"""Meter existing synchronous Cores without locking over transport latency.

This research bridge has no network transport or credentials of its own. All
model and fee verification ports are mandatory injections. The offline entry
accepts only mock ports. This is not a new paid-launch authorization adapter.
"""

import hashlib
import json
import math
import time
from copy import deepcopy
from pathlib import Path

from cloudyfan_jev.client import CHAT, DECISIONS, usage
from cloudyfan_jev.engine import _lock
from cloudyfan_jev.receipt_store import ReceiptStore, _encoded
from parallel_runner import digest, dispatch_paused, read, write_once


class UnknownCost(RuntimeError):
    pass


class ConcurrentClient(ReceiptStore):
    def __init__(
        self,
        root,
        *,
        workflow_id,
        ledger,
        sender,
        fee_verifier,
        forecast,
        pre_dispatch_check,
        historical_calls=None,
        clock=time.time,
    ):
        if not all(callable(p) for p in (sender, fee_verifier, forecast, pre_dispatch_check)):
            raise ValueError("EXPLICIT_TRANSPORT_AND_FEE_PORTS_REQUIRED")
        super().__init__(Path(root))
        self.workflow_id, self.ledger = workflow_id, ledger
        self.sender, self.fee_verifier, self.forecast, self.clock = (
            sender,
            fee_verifier,
            forecast,
            clock,
        )
        self.pre_dispatch_check = pre_dispatch_check
        self.historical_calls = deepcopy(historical_calls or {})

    def _saved(self, cid, request_sha):
        directory = self._directory(cid)
        if not (directory / "intent.json").exists():
            if any((directory / name).exists() for name in ("response.json", "verified-fee.json")):
                self.ledger.halt("saved_paid_evidence_without_intent")
                raise UnknownCost("SAVED_RESPONSE_WITHOUT_INTENT_NEVER_RESEND")
            return None
        state = self.ledger.lookup(cid)
        if state is None:
            self.ledger.halt("saved_paid_response_not_in_bound_ledger")
            raise UnknownCost("EXISTING_PAID_EVIDENCE_MISSING_LEDGER_IMPORT")
        try:
            intent = read(directory / "intent.json")
            if not isinstance(intent, dict):
                raise ValueError("Saved intent must be an object")
        except (ValueError, OSError):
            self.ledger.halt("saved_intent_unreadable")
            raise UnknownCost("SAVED_INTENT_UNREADABLE_NEVER_RESEND") from None
        if (
            intent.get("call_id") != cid
            or intent.get("workflow_id") != self.workflow_id
            or intent.get("request_sha256") != request_sha
            or state.get("request_sha256") != request_sha
            or state.get("workflow_id") != self.workflow_id
            or digest({k: intent.get(k) for k in ("url", "payload", "headers")}) != request_sha
        ):
            self.ledger.halt("saved_request_or_workflow_identity_changed")
            raise ValueError("SAVED_PAID_REQUEST_CHANGED")
        response = directory / "response.json"
        if not response.exists():
            self.ledger.mark_unknown(cid, "orphan_intent_no_response_no_resend")
            raise UnknownCost("SAVED_INTENT_WITHOUT_RESPONSE_NEVER_RESEND")
        try:
            response_bytes = response.read_bytes()
        except OSError:
            self.ledger.halt("saved_response_unreadable")
            raise UnknownCost("SAVED_RESPONSE_UNREADABLE_NEVER_RESEND") from None
        response_sha = hashlib.sha256(response_bytes).hexdigest()
        if state.get("response_sha256") not in (None, response_sha):
            self.ledger.mark_unknown(cid, "saved_response_identity_changed")
            raise ValueError("SAVED_RESPONSE_CHANGED")
        try:
            record = json.loads(response_bytes)
            if not isinstance(record, dict):
                raise ValueError("Saved response must be an object")
        except (ValueError, OSError):
            self.ledger.halt("saved_response_unreadable")
            raise UnknownCost("SAVED_RESPONSE_UNREADABLE_NEVER_RESEND") from None
        if record.get("call_id") != cid or record.get("request_sha256") != request_sha:
            self.ledger.halt("saved_response_request_identity_changed")
            raise ValueError("SAVED_RESPONSE_REQUEST_CHANGED")
        # Crash after raw persistence but before its ledger transaction: verify
        # the original response and finish accounting, never call sender again.
        self.ledger.save_response(cid, response_sha)
        if record.get("not_dispatched") is True:
            self.ledger.reconcile(cid, "0", digest(record), revision_id="not_dispatched")
            return record
        self._account(cid, record, directory)
        return record

    def _account(self, cid, record, directory):
        try:
            proof = self.fee_verifier(record)
            if (
                not isinstance(proof, dict)
                or proof.get("verified") is not True
                or proof.get("complete_request_cost") is not True
                or proof.get("cost_usd") is None
                or not proof.get("generation_id")
            ):
                raise UnknownCost("COMPLETE_VERIFIED_FEE_REQUIRED")
            actual = usage(record)
            if actual["generation_id"] != proof["generation_id"]:
                raise UnknownCost("FEE_GENERATION_IDENTITY_MISMATCH")
            self.ledger.bind_generation(cid, proof["generation_id"])
            write_once(directory / "verified-fee.json", proof)
            self.ledger.reconcile(cid, proof["cost_usd"], digest(proof), revision_id="usage")
        except Exception as error:
            self.ledger.mark_unknown(cid, "unverified_cost_or_identity:" + type(error).__name__)
            raise

    def call(self, cid, url, payload, *, before_post=None, headers=None):
        if url not in (CHAT, DECISIONS):
            raise ValueError("UNEXPECTED_MODEL_ENDPOINT")
        payload, headers = deepcopy(payload), deepcopy(headers)
        if url == CHAT:
            # Same wire projection as the original paid S50 client.
            payload.pop("session_id", None)
        packet = {
            "url": url,
            "payload": payload,
            "headers": headers or {"X-OpenRouter-Metadata": "enabled"},
        }
        request_sha = digest(packet)
        directory = self._directory(cid)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Local per-call lock only. Other workflows may wait on their model.
        with _lock(directory / ".transport.lock"):
            if cid in self.historical_calls:
                return self._import_historical(cid, packet, directory)
            saved = self._saved(cid, request_sha)
            if saved is not None:
                return saved
            if dispatch_paused():
                raise RuntimeError("COORDINATOR_PAUSED_NO_NEW_DISPATCH")
            if before_post is not None and before_post() is not True:
                raise RuntimeError("FROZEN_PREPOST_ADMISSION_DENIED")
            if self.pre_dispatch_check(cid, url, payload) is not True:
                raise RuntimeError("AUTHORITY_IDENTITY_SOURCE_OR_ACCOUNT_GATE_DENIED")
            forecast = self.forecast(url, payload)
            if digest(packet) != request_sha:
                raise ValueError("PREPOST_GATE_CHANGED_REQUEST")
            if not self.ledger.begin(
                cid,
                self.workflow_id,
                "classifier" if url == DECISIONS else "generation",
                forecast,
                request_sha,
            ):
                # A durable ledger intent without its file is also never resent.
                self.ledger.mark_unknown(cid, "ledger_intent_without_local_response")
                raise UnknownCost("EXISTING_LEDGER_INTENT_NEVER_RESEND")
            try:
                write_once(
                    directory / "intent.json",
                    {
                        "call_id": cid,
                        "workflow_id": self.workflow_id,
                        "request_sha256": request_sha,
                        **packet,
                        "started_at_epoch": self.clock(),
                    },
                )
                stopped = bool(self.ledger.snapshot().get("global_stop")) or dispatch_paused()
                changed = digest(packet) != request_sha
                final_clock = self.clock()
                if (
                    not isinstance(final_clock, (int, float))
                    or isinstance(final_clock, bool)
                    or not math.isfinite(final_clock)
                    or final_clock >= self.ledger.deadline
                    or stopped
                    or changed
                ):
                    record = {
                        "call_id": cid,
                        "request_sha256": request_sha,
                        "http_status": None,
                        "raw": None,
                        "not_dispatched": True,
                        "error": "final_dispatch_gate_denied",
                        "elapsed_ms": 0,
                    }
                    write_once(directory / "response.json", record)
                    self.ledger.save_response(
                        cid, hashlib.sha256((directory / "response.json").read_bytes()).hexdigest()
                    )
                    self.ledger.reconcile(cid, "0", digest(record), revision_id="not_dispatched")
                    return record
                started = time.perf_counter()
                response = self.sender(cid, url, deepcopy(payload), deepcopy(packet["headers"]))
                if not isinstance(response, dict):
                    raise ValueError("RAW_TRANSPORT_RECORD_REQUIRED")
                record = {
                    **response,
                    "call_id": cid,
                    "request_sha256": request_sha,
                    "elapsed_ms": (time.perf_counter() - started) * 1000,
                }
                # Never inspect identity/answer/cost before durable raw response.
                write_once(directory / "response.json", record)
                self.ledger.save_response(
                    cid, hashlib.sha256((directory / "response.json").read_bytes()).hexdigest()
                )
            except BaseException as error:
                self.ledger.mark_unknown(
                    cid, "transport_or_persistence_interruption:" + type(error).__name__
                )
                raise
            self._account(cid, record, directory)
            return record

    def _import_historical(self, cid, packet, directory):
        """Link a trusted prior paid answer, without another paid intent/send.

        Every raw/derived source must be pinned by the new operational manifest.
        The prior cumulative budget already includes its actual fee; the ledger
        records that fee and consumes its attempt slot without charging it twice.
        """
        entry = self.historical_calls[cid]
        try:
            for name, expected in entry["source_evidence"].items():
                if hashlib.sha256(Path(name).read_bytes()).hexdigest() != expected:
                    raise ValueError("HISTORICAL_RESPONSE_OR_FEE_SOURCE_CHANGED")
            intent = read(entry["intent_path"])
            original = read(entry["response_path"])
            old_projection = {"url": intent["url"], "payload": intent["payload"]}
            if (
                old_projection != {"url": packet["url"], "payload": packet["payload"]}
                or intent["request_sha256"] != hashlib.sha256(_encoded(old_projection)).hexdigest()
                or packet["headers"] != {"X-OpenRouter-Metadata": "enabled"}
            ):
                raise ValueError("HISTORICAL_WIRE_REQUEST_NOT_IDENTICAL")
            verified = deepcopy(entry.get("verified_record", original))
            if any(verified.get(k) != original.get(k) for k in ("raw", "http_status", "headers")):
                raise ValueError("HISTORICAL_ORIGINAL_RESPONSE_CHANGED")
            proof = self.fee_verifier(verified)
            if (
                not isinstance(proof, dict)
                or proof.get("verified") is not True
                or proof.get("complete_request_cost") is not True
                or proof.get("generation_id") != usage(verified)["generation_id"]
            ):
                raise UnknownCost("HISTORICAL_COMPLETE_FEE_PROOF_REQUIRED")
            request_sha = digest(packet)
            record = {
                **verified,
                "request_sha256": request_sha,
                "historical_original_request_sha256": intent["request_sha256"],
                "historical_response_sha256": entry["source_evidence"][entry["response_path"]],
                "historical_reused": True,
                "replay_api_latency_ms": None,
            }
            write_once(
                directory / "intent.json",
                {**intent, **packet, "request_sha256": request_sha, "historical_reused": True},
            )
            write_once(directory / "response.json", record)
            write_once(directory / "verified-fee.json", proof)
            self.ledger.import_historical_call(
                cid,
                self.workflow_id,
                "classifier" if packet["url"] == DECISIONS else "generation",
                request_sha,
                hashlib.sha256((directory / "response.json").read_bytes()).hexdigest(),
                proof["generation_id"],
                proof["cost_usd"],
                digest(proof),
                required_prior_known_usd=entry["required_prior_known_usd"],
            )
            return record
        except Exception:
            self.ledger.halt("historical_paid_import_not_verified")
            raise

    def reconcile(self, cid):
        """Only verify stored evidence. A GET worker is a separate opt-in port."""
        directory = self._directory(cid)
        intent = read(directory / "intent.json")
        return self._saved(cid, intent["request_sha256"])


class HostedWorkflowRunner:
    """D uses the same injected public validator and two hosted answer calls.

    A model/provider error is not a wrong answer. Repair sees only the latest
    actual answer and public diagnostics. No local Jev classification is used.
    """

    def __init__(self, adapter, client_factory, public_binding_loader, root, *, system):
        self.adapter, self.client_factory, self.load = (
            adapter,
            client_factory,
            public_binding_loader,
        )
        self.root, self.system = Path(root), system

    def __call__(self, job):
        task, contract_version, factory = self.load(job)
        if (
            digest(task) != job.bindings["task_sha256"]
            or contract_version != job.bindings["contract_version"]
        ):
            raise ValueError("HOSTED_PUBLIC_CONTRACT_CHANGED")
        client = self.client_factory(job)
        directory = self.root / digest(job.run_id)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        initial = self.adapter.build_request(task["prompt"], system=self.system)
        original_messages = initial["payload"]["messages"]
        attempts, previous, diagnostic = [], None, None
        for attempt in range(2):
            messages = (
                original_messages
                if attempt == 0
                else self.adapter.repair_messages(original_messages, previous, diagnostic)
            )
            packet = self.adapter.build_request(messages=messages, attempt=attempt)
            cid = "run-" + job.run_id + "-hosted-a" + str(attempt)
            record = client.call(cid, packet["url"], packet["payload"], headers=packet["headers"])
            proof = read(client._directory(cid) / "verified-fee.json")
            parsed = self.adapter.parse_response(record, request=packet, accounting_proof=proof)
            if parsed["billing"]["cost_unknown"]:
                client.ledger.mark_unknown(cid, "hosted_full_fee_proof_not_accepted")
                raise UnknownCost("HOSTED_COMPLETE_FEE_NOT_VERIFIED")
            previous = parsed.get("answer")
            if previous is not None and not parsed["identity_restrictions_satisfied"]:
                raise ValueError("HOSTED_ACTUAL_IDENTITY_OR_HARD_RESTRICTION_FAILED")
            row_path = directory / f"attempt-{attempt}.json"
            state = client.ledger.lookup(cid)
            if state.get("step_sha256"):
                row = read(row_path)
                if (
                    digest(row) != state["step_sha256"]
                    or row["call_id"] != cid
                    or row["answer"] != previous
                    or row.get("request_sha256") != record["request_sha256"]
                    or row.get("response_sha256") != state["response_sha256"]
                ):
                    client.ledger.halt("hosted_public_validation_seal_changed")
                    raise ValueError("HOSTED_SAVED_PUBLIC_VALIDATION_CHANGED")
                diagnostic = row["public"]
                attempts.append(row)
                if diagnostic.get("passed") is True:
                    break
                continue
            diagnostic = {"passed": None, "checks": ["NO_ANSWER_PROVIDER_OR_TRANSPORT"]}
            if isinstance(previous, str) and previous.strip():
                try:
                    diagnostic = factory().validate(previous)
                except Exception:
                    # Public projection only; never send exception contents,
                    # paths or private grader outcomes to a subsequent model.
                    diagnostic = {"passed": None, "checks": ["PUBLIC_VALIDATION_UNAVAILABLE"]}
            row = {
                "attempt": attempt,
                "call_id": cid,
                "answer": previous,
                "public": diagnostic,
                "hosted": parsed,
                "request_sha256": record["request_sha256"],
                "response_sha256": state["response_sha256"],
            }
            write_once(row_path, row)
            client.ledger.seal_step(cid, digest(row))
            attempts.append(row)
            if diagnostic.get("passed") is True:
                break
        return {
            "run_id": job.run_id,
            "status": "passed"
            if diagnostic.get("passed") is True
            else ("failed" if diagnostic.get("passed") is False else "unverifiable"),
            "final": previous,
            "final_public_pass": diagnostic.get("passed"),
            "attempts": attempts,
            "generation_calls": len(attempts),
            "selection": {"classifier_calls": 0},
            "hosted_router_requests": len(attempts),
            "comparison_scope": "hosted_service_comparison",
            "hidden_grading": "not_run",
        }

"""Four independent workflow slots; routing remains in the injected frozen Core.

No HTTP, credentials, implicit authority, paid retries or hidden grader exists in
this scheduler. A caller must supply a metered workflow runner. A normal live
intent is reserved, not a known-unknown failure; orphan intents require explicit
recovery after all workers have stopped.
"""

import hashlib
import json
import math
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from copy import deepcopy
from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path

from cloudyfan_jev.engine import _lock
from cloudyfan_jev.receipt_store import _immutable as _exclusive

VERSION = "s50-four-slot-four-arm-offline-v2"
ARMS = {"A": "cloudyfan", "B": "low_first", "C": "open_source_first", "D": "hosted_jev_router"}
GENERATION_CAPS = {"A": 3, "B": 2, "C": 2, "D": 2}
CLASSIFIER_CAPS = {"A": 2, "B": 0, "C": 0, "D": 0}
_WORKER_CONTEXT = threading.local()


def dispatch_paused():
    event = getattr(_WORKER_CONTEXT, "stop", None)
    return bool(event is not None and event.is_set())


def encoded(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode()


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def read(path):
    return json.loads(Path(path).read_text())


def write_once(path, value):
    """Exclusive creation or exact semantic reuse, never an overwrite."""
    try:
        _exclusive(path, value)
    except FileExistsError:
        if read(path) != value:
            raise ValueError("IMMUTABLE_SAVED_EVIDENCE_CHANGED:" + Path(path).name) from None


def file_evidence(directory):
    """Only immutable original intent, raw response and accepted usage proof."""
    names = {"intent.json", "response.json", "verified-fee.json"}
    return {
        str(p.resolve()): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in Path(directory).rglob("*.json")
        if p.name in names
    }


@dataclass(frozen=True)
class Job:
    run_id: str
    question_id: str
    arm: str
    ordinal: int
    forecast_usd: str
    bindings: dict

    @property
    def identity(self):
        return digest(asdict(self))

    @property
    def generation_cap(self):
        return GENERATION_CAPS[self.arm]

    @property
    def classifier_cap(self):
        return CLASSIFIER_CAPS[self.arm]


@dataclass(frozen=True)
class HistoricalCompletion:
    job_sha256: str
    source_evidence: dict
    summary: dict
    required_prior_known_usd: str
    required_historical_reserve_usd: str

    def verify(self, job, ledger):
        if job.identity != self.job_sha256:
            raise ValueError("HISTORICAL_TASK_CONFIG_OR_CONTRACT_CHANGED")
        if Decimal(ledger.binding["prior_known_usd"]) != Decimal(
            self.required_prior_known_usd
        ) or Decimal(ledger.binding["historical_reserve_usd"]) < Decimal(
            self.required_historical_reserve_usd
        ):
            raise ValueError("HISTORICAL_COST_PREFIX_OR_UNKNOWN_RESERVE_MISSING")
        for name, expected in self.source_evidence.items():
            path = Path(name)
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                raise ValueError("HISTORICAL_PAID_SOURCE_CHANGED")
        return deepcopy(self.summary)


class FourSlotScheduler:
    def __init__(
        self,
        root,
        *,
        plan_id,
        ledger,
        runner,
        evidence_provider,
        historical_links=None,
        slots=4,
        clock=time.time,
    ):
        if slots != 4 or not callable(runner) or not callable(evidence_provider):
            raise ValueError("FOUR_SLOTS_AND_EXPLICIT_RUNNER_REQUIRED")
        self.root, self.plan_id = Path(root), plan_id
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.ledger, self.runner, self.clock = ledger, runner, clock
        self.evidence_provider = evidence_provider
        self.historical_links = deepcopy(historical_links or {})
        self.slots, self.stop = slots, threading.Event()

    def directory(self, job):
        return self.root / "workflows" / digest(job.run_id)

    def _require_completed_dispatches(self, job):
        if self.ledger.has_not_dispatched_calls(job.run_id):
            raise RuntimeError("WORKFLOW_NOT_DISPATCHED")

    def _terminal(self, job):
        p = self.directory(job) / "terminal.json"
        if not p.exists():
            return None
        terminal = read(p)
        result = self.directory(job) / "result.json"
        if (
            terminal.get("plan_id") != self.plan_id
            or terminal.get("job_sha256") != job.identity
            or not result.is_file()
            or hashlib.sha256(result.read_bytes()).hexdigest() != terminal.get("result_sha256")
            or terminal.get("state") != "completed"
            or terminal.get("public_pass") != read(result)["result"].get("final_public_pass")
            or terminal.get("evidence_sha256") != self.evidence_provider(job)
        ):
            raise ValueError("COMPLETED_WORKFLOW_EVIDENCE_CHANGED")
        self._require_completed_dispatches(job)
        workflow = self.ledger.lookup_workflow(job.run_id)
        if (
            not workflow
            or workflow["status"] != "finished"
            or workflow.get("completion_sha256") != digest(terminal)
        ):
            raise ValueError("COMPLETION_LEDGER_ANCHOR_CHANGED")
        return terminal

    def _work(self, job):
        _WORKER_CONTEXT.stop = self.stop
        if job.identity != self._expected_identities[job.run_id]:
            raise ValueError("FROZEN_QUEUED_JOB_CHANGED")
        directory = self.directory(job)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        write_once(directory / "job.json", {"plan_id": self.plan_id, **asdict(job)})
        initial = job.identity
        began = self.clock()
        # Complete result without terminal means crash during sealing: no Core.
        result_path = directory / "result.json"
        has_saved_result = result_path.exists()
        if has_saved_result:
            saved = read(result_path)
            if saved.get("job_sha256") != initial or saved.get("plan_id") != self.plan_id:
                raise ValueError("PREPARED_RESULT_BINDING_CHANGED")
            result = saved["result"]
        else:
            result = self.runner(job)
            if job.identity != initial:
                raise ValueError("WORKFLOW_CONFIGURATION_CHANGED_DURING_RUN")
        self._require_completed_dispatches(job)
        if not has_saved_result:
            write_once(
                result_path, {"plan_id": self.plan_id, "job_sha256": initial, "result": result}
            )
        terminal = {
            "plan_id": self.plan_id,
            "job_sha256": initial,
            "result_sha256": hashlib.sha256(result_path.read_bytes()).hexdigest(),
            "workflow_wall_seconds": max(0, self.clock() - began),
            "state": "completed",
            "public_pass": result.get("final_public_pass"),
            "hidden_grading": "not_run",
            "replayed_result_api_latency": None,
            "evidence_sha256": self.evidence_provider(job),
        }
        prepared = directory / "prepared-terminal.json"
        if prepared.exists():
            old = read(prepared)
            if any(
                old.get(k) != value for k, value in terminal.items() if k != "workflow_wall_seconds"
            ):
                raise ValueError("PREPARED_TERMINAL_EVIDENCE_CHANGED")
            terminal = old
        else:
            write_once(prepared, terminal)
        self.ledger.finish(job.run_id)
        self.ledger.seal_completion(job.run_id, digest(terminal))
        write_once(directory / "terminal.json", terminal)
        return terminal

    def run(self, jobs):
        jobs = tuple(deepcopy(j) for j in jobs)
        if len({j.run_id for j in jobs}) != len(jobs) or any(j.arm not in ARMS for j in jobs):
            raise ValueError("UNIQUE_FOUR_ARM_JOBS_REQUIRED")
        body = {
            "version": VERSION,
            "plan_id": self.plan_id,
            "slots": self.slots,
            "jobs": [asdict(j) for j in jobs],
            "historical_links": {
                key: digest(asdict(link)) for key, link in self.historical_links.items()
            },
        }
        write_once(self.root / "plan.json", body)
        self._expected_identities = {j.run_id: j.identity for j in jobs}
        failures, reused, completed, historical = [], [], [], []
        began = self.clock()
        # A second coordinator may not race the first. Workers run within it.
        with _lock(self.root / ".coordinator.lock"):
            pending = []
            for job in jobs:
                if job.run_id in self.historical_links:
                    self.historical_links[job.run_id].verify(job, self.ledger)
                    historical.append(job.run_id)
                    continue
                terminal = self._terminal(job)
                if terminal is not None:
                    reused.append(job.run_id)
                else:
                    pending.append(job)
            cursor = 0
            exhausted = False
            active = {}
            self.stop.clear()
            with ThreadPoolExecutor(max_workers=4, thread_name_prefix="s50-workflow") as pool:
                try:
                    while active or not exhausted:
                        while len(active) < 4 and not exhausted and not self.stop.is_set():
                            snapshot = self.ledger.snapshot()
                            if snapshot.get("global_stop") or self.clock() >= self.ledger.deadline:
                                self.stop.set()
                                break
                            if cursor == len(pending):
                                exhausted = True
                                break
                            job = pending[cursor]
                            try:
                                self.ledger.admit(
                                    job.run_id,
                                    job.forecast_usd,
                                    job.generation_cap,
                                    job.classifier_cap,
                                )
                            except Exception as error:
                                if (
                                    active
                                    and getattr(error, "code", None)
                                    == "COMPLETE_WORKFLOW_RESERVATION_EXCEEDS_TARGET"
                                    and not self.ledger.snapshot().get("global_stop")
                                ):
                                    # A running workflow may release unused reserve.
                                    # Retain the next job; never skip it for a cheap one.
                                    break
                                failures.append(
                                    {
                                        "run_id": job.run_id,
                                        "phase": "admission",
                                        "error_kind": type(error).__name__,
                                        "reason": str(error),
                                    }
                                )
                                self.stop.set()
                                break
                            active[pool.submit(self._work, job)] = job
                            cursor += 1
                        if not active:
                            break
                        done, _ = wait(active, return_when=FIRST_COMPLETED)
                        for future in done:
                            job = active.pop(future)
                            try:
                                future.result()
                                completed.append(job.run_id)
                            except Exception as error:
                                self.stop.set()
                                item = {
                                    "run_id": job.run_id,
                                    "phase": "workflow",
                                    "error_kind": type(error).__name__,
                                    "reason": str(error),
                                }
                                failures.append(item)
                                directory = self.directory(job)
                                write_once(
                                    directory / ("interruption-" + digest(item) + ".json"), item
                                )
                        # No cancellation of paid/in-flight work. Remaining workers
                        # save responses; the ledger denies their next paid step.
                except BaseException:
                    self.stop.set()
                    raise

        remaining = [
            j.run_id for j in jobs if j.run_id not in historical and self._terminal(j) is None
        ]
        report = {
            "version": VERSION,
            "planned": len(jobs),
            "reused_completed": len(reused),
            "historical_completed": len(historical),
            "historical_cost_already_in_prior_known": True,
            "historical_reuse_is_not_zero_cost_or_new_api_latency": True,
            "newly_completed": len(completed),
            "remaining": len(remaining),
            "remaining_ids": remaining,
            "failures": failures,
            "wall_seconds": max(0, self.clock() - began),
            "slots": 4,
            "hidden_grading": "not_run",
            "ledger": self.ledger.snapshot(),
        }
        write_once(self.root / ("run-report-" + digest(report) + ".json"), report)
        return report


class CoreWorkflowRunner:
    """Call the supplied current Core without reimplementing its routing tree."""

    def __init__(self, public_binding_loader, core_factory, hosted_runner=None):
        self.load, self.factory, self.hosted = public_binding_loader, core_factory, hosted_runner

    def __call__(self, job):
        if job.arm == "D":
            if self.hosted is None:
                raise ValueError("HOSTED_RUNNER_NOT_BOUND")
            return self.hosted(job)
        task, contract_version, environment_factory = self.load(job)
        if (
            digest(task) != job.bindings["task_sha256"]
            or contract_version != job.bindings["contract_version"]
        ):
            raise ValueError("TASK_OR_PUBLIC_CONTRACT_CHANGED")
        core = self.factory(job)
        if digest(core.configuration_descriptor()) != job.bindings["core_configuration_sha256"]:
            raise ValueError("FROZEN_CORE_CONFIGURATION_CHANGED")
        return core.run(
            job.run_id,
            task,
            environment_factory,
            contract_version=contract_version,
            mode=ARMS[job.arm],
        )


def simulate(
    jobs,
    *,
    validation_slots=2,
    startup_seconds=0,
    grading_seconds=0,
    report_seconds=0,
    hard_limit=1800,
    stop_at=None,
    deadline=True,
):
    """Deterministic discrete-event rehearsal, not a model/quality measurement.

    Each job has sequential stages with kind/model or validation and a positive
    duration. Four workflow slots and a separate bounded validation queue are
    exercised, so neither dependent calls nor SQL resource work is parallelized
    inside a single workflow. Optional unknown stops new stages/flows; already
    running stages complete and their evidence can be saved.
    """
    import heapq

    if (
        type(validation_slots) is not int
        or validation_slots < 1
        or any(
            not math.isfinite(float(x)) or x < 0
            for x in (startup_seconds, grading_seconds, report_seconds, hard_limit)
        )
    ):
        raise ValueError("VALID_TIMING_ASSUMPTIONS_REQUIRED")
    for job in jobs:
        for stage in job["stages"]:
            if (
                stage["kind"] not in {"model", "validation", "account_gate"}
                or not math.isfinite(stage["seconds"])
                or stage["seconds"] < 0
            ):
                raise ValueError("INVALID_SIMULATED_STAGE")
    work, active, events = list(jobs), {}, []
    capacities = {"validation": validation_slots, "account_gate": 1}
    queues = {key: [] for key in capacities}
    used = {key: 0 for key in capacities}
    t, cursor, finished, peak = float(startup_seconds), 0, 0, 0
    counter, stopped, launches = 0, False, []

    def start_stage(index):
        nonlocal counter
        position = active[index]
        if position == len(work[index]["stages"]):
            return
        stage = work[index]["stages"][position]
        if stopped or (deadline and t >= hard_limit):
            return
        if stage["kind"] in capacities:
            resource = stage["kind"]
            if used[resource] >= capacities[resource]:
                queues[resource].append(index)
                return
            used[resource] += 1
        counter += 1
        launches.append(
            {"at": t, "job": work[index]["id"], "stage": position, "kind": stage["kind"]}
        )
        heapq.heappush(events, (t + stage["seconds"], counter, index, stage["kind"]))

    while True:
        stopped = stopped or (stop_at is not None and t >= stop_at)
        while (
            cursor < len(work)
            and len(active) < 4
            and not stopped
            and (not deadline or t < hard_limit)
        ):
            active[cursor] = 0
            if not work[cursor]["stages"]:
                del active[cursor]
                finished += 1
            else:
                start_stage(cursor)
            cursor += 1
            peak = max(peak, len(active))
        if not events:
            break
        end, _, index, kind = heapq.heappop(events)
        if stop_at is not None and t < stop_at <= end:
            stopped = True
        t = end
        if kind in capacities:
            used[kind] -= 1
        active[index] += 1
        if active[index] == len(work[index]["stages"]):
            finished += 1
            del active[index]
        elif not stopped:
            start_stage(index)
        for resource, queue in queues.items():
            while queue and used[resource] < capacities[resource] and not stopped:
                start_stage(queue.pop(0))
    captures_complete = finished == len(work)
    total = t + grading_seconds + report_seconds if captures_complete else t
    return {
        "kind": "offline_timing_simulation",
        "planned": len(work),
        "completed": finished,
        "remaining": len(work) - finished,
        "capture_seconds": t,
        "end_to_end_seconds": total,
        "under_30_minutes": captures_complete and total <= hard_limit,
        "peak_workflow_slots": peak,
        "stopped_on_unknown": stopped,
        "launches": launches,
        "quality_measured": False,
        "real_api_requests": 0,
        "grading_seconds_assumed": grading_seconds,
        "report_seconds_assumed": report_seconds,
    }

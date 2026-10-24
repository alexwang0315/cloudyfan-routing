"""A disposable customer fixture using the real MRC and mock upstreams only.

The public arithmetic validator reads the frozen request. Routing scenarios and
deliberately incorrect answers exist only inside the HTTP mock transport.
Constructing or reading the fixture never dispatches a task or an upstream call.
"""
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
import uuid

import httpx
from cloudyfan_jev.budget import Authorization, BudgetedCore, BudgetStore, EnforcedBound
from cloudyfan_jev.client import Client
from cloudyfan_jev.demo import example_config
from cloudyfan_jev.diagnostics import DiagnosticContract
from cloudyfan_jev.engine import Core, Limits
from cloudyfan_jev.http_api import RegisteredContract, TaskService
from cloudyfan_jev.identity import IdentityError, IdentityStore
from cloudyfan_jev.privacy import ExecutionPolicy

from .demo import ADMIN_TOKEN, CATEGORY
from .customer_simulation import local_origin, question_requests, run_customer50
from .integration import ACCESS_POLICY, assemble_monitor
from .pools import empty_view
from .store import MODES, MonitorError, Store, encode

CUSTOMER = "ollama-customer"
ALIAS = "cloudyfan/mrc:simulation"
CONTRACT = "simulation-cloudyfan"
PUBLIC_CONTRACT = "monitor-frozen-integer-sum-v1"
UNKNOWN_VALIDATION = "Public verification is unavailable for this request."


class PublicArithmeticEnvironment:
    """Independent public check, with an explicit request-declared unknown case."""
    def __init__(self, prompt):
        match = re.search(r"integer sum of (-?\d+) and (-?\d+)\.", prompt)
        if match is None:
            raise ValueError("The public integer-sum contract requires two operands")
        self.operands = tuple(int(value) for value in match.groups())
        self.unverifiable = UNKNOWN_VALIDATION in prompt

    def tools_schema(self):
        return []

    def call(self, name, arguments):
        raise ValueError("The public integer-sum contract has no tools")

    def validate(self, final_text):
        if self.unverifiable:
            return {"passed": None, "checks": ["public_verification_unavailable"]}
        text = final_text.strip()
        passed = (re.fullmatch(r"-?\d+", text) is not None
                  and int(text) == sum(self.operands))
        return {"passed": passed, "checks": [] if passed else ["integer_sum_contract_failed"]}


def public_environment(workspace):
    # TaskService owns and freezes job.json before creating this workspace. This
    # is the public request contract, not a hidden answer or intended route.
    job = json.loads((Path(workspace).parent / "job.json").read_text())
    return PublicArithmeticEnvironment(job["request"]["prompt"])


class SimulationUpstream:
    """Deterministic teaching scenarios, never a real model accuracy claim."""
    def __init__(self):
        self.calls = []
        self._lock = threading.Lock()
        self._prefix = uuid.uuid4().hex[:12]

    def __call__(self, request):
        if request.method != "POST" or request.url.path not in (
                "/api/v1/chat/completions", "/api/alpha/decisions"):
            raise AssertionError("Only synthetic generation and Jev POSTs are permitted")
        payload = json.loads(request.content)
        decision = request.url.path.endswith("/decisions")
        prompt = (payload["state"]["task"] if decision else
                  next(m["content"] for m in payload["messages"] if m["role"] == "user"))
        question = re.search(r"Simulation question (\d{2})\.", prompt)
        operands = re.search(r"integer sum of (-?\d+) and (-?\d+)\.", prompt)
        if question is None or operands is None or not 1 <= int(question[1]) <= 50:
            raise AssertionError("Only the registered public simulation questions are supported")
        number = int(question[1])
        with self._lock:
            self.calls.append({"path": request.url.path, "payload": payload})
            generation = f"gen-simulation-{self._prefix}-{len(self.calls)}"
        if decision:
            stage = next(iter(payload["questions"]))
            pair = ("low", "mid") if stage == "low_mid" else ("mid", "high")
            choice = "low" if 11 <= number <= 20 or 41 <= number <= 45 else "mid"
            if stage == "mid_high":
                choice = "high"
            # One explicit tie demonstrates the existing Core's conservative
            # fallback to mid, followed by another failed check and high repair.
            tied = number == 31 and stage == "low_mid"
            distribution = {tier: .5 if tied else .8 if tier == choice else .2 for tier in pair}
            return httpx.Response(200, json={"id": generation, "model": payload["model"],
                "provider": "TypeSafe", "usage": {"cost": .0001, "input_tokens": 10, "output_tokens": 1},
                "answers": {stage: {"type": "choice", "choice": choice, "probabilities": distribution}}})
        tier = payload["model"].removeprefix("example/")
        correct = ((number <= 10 and tier == "open_source")
                   or (11 <= number <= 20 and tier == "low")
                   or (21 <= number <= 30 and tier == "mid")
                   or (31 <= number <= 40 and tier == "high") or number >= 46)
        # The validator never reads this branch or its intended scenario.
        computed = int(operands[1]) + int(operands[2])
        answer = str(computed if correct else computed + 1)
        cost = {"open_source": 0, "low": .0003, "mid": .001, "high": .004}[tier]
        return httpx.Response(200, json={"id": generation, "model": payload["model"],
            "provider": "Example", "usage": {"cost": cost, "prompt_tokens": 20, "completion_tokens": 1},
            "choices": [{"message": {"role": "assistant", "content": answer}, "finish_reason": "stop"}]})


@dataclass
class CustomerSimulation:
    app: object
    upstream: SimulationUpstream
    identity: IdentityStore
    credentials: dict = field(repr=False)
    root: Path
    operator_token: str = field(default=ADMIN_TOKEN, repr=False)
    controller: object = field(default=None, repr=False)


class SimulationIdentityStore(IdentityStore):
    """Record bounded infrastructure failures without changing authorization.

    This instrumentation belongs to the synthetic fixture only. It neither
    retries an operation nor converts an error into a successful identity.
    """
    def __init__(self, *args, diagnostics_path, **kwargs):
        self._diagnostics_path = Path(diagnostics_path)
        self._diagnostic_lock = threading.Lock()
        self._failure_count, self._last_failure = 0, None
        super().__init__(*args, **kwargs)

    def _observe_failure(self, operation, error):
        code = ("identity_private_file_unavailable" if isinstance(error, FileNotFoundError) else
                "identity_database_unavailable" if isinstance(error, sqlite3.OperationalError) else
                "identity_control_unavailable" if isinstance(error, IdentityError) else
                "identity_operation_unavailable")
        record = {"operation": operation, "exception_class": type(error).__name__,
                  "code": code, "observed_at": datetime.now(timezone.utc).isoformat()}
        with self._diagnostic_lock:
            self._failure_count += 1
            self._last_failure = record
            try:
                path = self._diagnostics_path
                if path.is_symlink() or any(parent.is_symlink() for parent in path.parents):
                    raise OSError("Unsafe diagnostic path")
                fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
                with os.fdopen(fd, "a") as stream:
                    os.fchmod(stream.fileno(), 0o600)
                    stream.write(encode(record) + "\n")
                    stream.flush()
                    os.fsync(stream.fileno())
            except OSError:
                # Preserve the original fail-closed exception even when the
                # optional diagnostic journal is unavailable.
                self._last_failure = {**record, "journal_unavailable": True}

    def diagnostics(self):
        with self._diagnostic_lock:
            return {"scope": "current_process", "failures": self._failure_count,
                    "last": dict(self._last_failure) if self._last_failure else None}

    def authenticate(self, token, required_scope):
        try:
            return super().authenticate(token, required_scope)
        except Exception as error:
            self._observe_failure("authenticate", error)
            raise

    def is_active(self, principal):
        try:
            return super().is_active(principal)
        except Exception as error:
            self._observe_failure("principal_active", error)
            raise


def _private_json(path, value):
    if path.is_symlink() or any(parent.is_symlink() for parent in path.parents):
        raise ValueError("Unsafe simulation evidence path")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(encode(value))


class SimulationController:
    """Operator-triggered background SDK customer, available only in the fixture."""
    def __init__(self, fixture, *, customer_transport=None, delay=.15):
        if fixture.app.gateway.demo is not True:
            raise ValueError("Customer simulation requires an explicitly synthetic service")
        local_origin(fixture.app.origin)
        self._fixture, self._transport, self._delay = fixture, customer_transport, delay
        self._lock, self._thread = threading.Lock(), None
        self._state_path = fixture.root / "simulation-state.json"
        self._state = {"status": "idle", "completed": 0, "batch_id": None,
                       "outcomes": {"passed": 0, "failed": 0, "unverifiable": 0}, "error": None}
        if self._state_path.exists():
            if self._state_path.is_symlink():
                raise ValueError("Unsafe simulation state path")
            state = json.loads(self._state_path.read_text())
            if state.get("status") == "running":
                state.update(status="failed", error="simulation_interrupted_by_restart")
            self._state.update(state)

    def descriptor(self):
        with self._lock:
            return {**self._state, "outcomes": dict(self._state["outcomes"]),
                    "total": 50, "customer": CUSTOMER, "alias": ALIAS,
                    "can_start": self._state["status"] != "running", "demo": True,
                    "model_upstream": "synthetic_mock_transport", "real_supplier_requests": 0,
                    "identity_diagnostics": self._fixture.identity.diagnostics(),
                    "observation_gap_seconds": self._delay,
                    "notice": "真實 Ollama SDK 與 MRC 流程；模型回答、Jev 回覆與帳務金額均為合成測試。"}

    def start(self, batch_id=None):
        batch_id = batch_id or "sim-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:6]
        question_requests(batch_id)  # Validate before mutating state or launching.
        with self._lock:
            if self._state["status"] == "running":
                raise MonitorError("simulation_already_running", 409)
            self._state = {"status": "running", "completed": 0, "batch_id": batch_id,
                           "outcomes": {"passed": 0, "failed": 0, "unverifiable": 0}, "error": None}
            _private_json(self._state_path, self._state)
            self._thread = threading.Thread(target=self._run, args=(batch_id,), daemon=True,
                                            name="cloudyfan-synthetic-ollama-customer")
            thread = self._thread
        started = self.descriptor()
        thread.start()
        return started

    def wait(self, timeout=None):
        """Wait for an explicitly started customer; never starts one implicitly."""
        with self._lock:
            thread = self._thread
        if thread is not None:
            thread.join(timeout)
        return self.descriptor()

    def _progress(self, progress):
        with self._lock:
            self._state.update(progress)
            _private_json(self._state_path, self._state)

    def _run(self, batch_id):
        fixture = self._fixture
        report_path = fixture.root / "customer-runs" / (batch_id + "-" + uuid.uuid4().hex[:12] + ".json")
        before = len(fixture.upstream.calls)
        try:
            report = run_customer50(fixture.app.origin, fixture.credentials[CUSTOMER].token,
                                    batch_id, progress=self._progress, transport=self._transport,
                                    delay=self._delay)
            # This controlled server action is part of the named synthetic run.
            # The SDK customer never receives or uses an operator credential.
            ledger = fixture.app.gateway.ledger()
            reconciliations = []
            for _ in range((len(ledger["rows"]) + 19) // 20 + 1):
                result = fixture.app.gateway.billing.reconcile(ledger)
                reconciliations.append(result)
                if result["waiting"] == 0 or result["charged"] == 0:
                    break
            billing = fixture.app.gateway.billing.snapshot(CUSTOMER)
            report.update(reconciliations=reconciliations,
                          customer_billing_after_server_reconciliation=billing,
                          new_model_or_jev_posts=len(fixture.upstream.calls) - before)
            if billing["pending"]:
                report.update(status="failed", error="simulation_billing_remains_pending")
            _private_json(report_path, report)
            with self._lock:
                self._state.update(status=report["status"], completed=report["completed"],
                                   outcomes=report["outcomes"], error=report.get("error"),
                                   report=str(report_path.relative_to(fixture.root)),
                                   new_model_or_jev_posts=report["new_model_or_jev_posts"])
                _private_json(self._state_path, self._state)
        except Exception:
            # Keep a bounded safe failure record, not exception text or tokens.
            with self._lock:
                self._state.update(status="failed", error="simulation_customer_run_failed")
                _private_json(report_path, {**self._state, "demo": True,
                    "new_model_or_jev_posts": len(fixture.upstream.calls) - before})
                self._state["report"] = str(report_path.relative_to(fixture.root))
                _private_json(self._state_path, self._state)


def build_simulation(root, *, origin="http://127.0.0.1:8766", clock=time.time):
    """Assemble a separate single-customer dataset; never import legacy tasks."""
    root = Path(root).absolute()
    marker = root / "simulation-fixture.json"
    manifest = {"schema_version": 1, "kind": "ollama-customer50-mock-only",
                "customer": CUSTOMER, "public_contract": PUBLIC_CONTRACT}
    if root.is_symlink() or any(parent.is_symlink() for parent in root.parents):
        raise ValueError("A private simulation directory without symlinks is required")
    if root.exists() and any(root.iterdir()):
        if not marker.is_file() or marker.is_symlink() or json.loads(marker.read_text()) != manifest:
            raise ValueError("Use a separate simulation root; existing data cannot be adopted")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(root, 0o700)
    if not marker.exists():
        with marker.open("x") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(encode(manifest))
    store = Store(root / "monitor.sqlite", clock=clock)
    upstream = SimulationUpstream()
    profiles, decision = example_config()
    for profile in profiles.values():
        profile["execution_location"] = "cloud:simulation-fixture"
    client = Client(httpx.Client(transport=httpx.MockTransport(upstream)),
                    "synthetic-only-no-real-credential", root / "receipts")
    core = Core(client, profiles, decision, limits=Limits(max_turns=1, max_tokens=128),
                execution_policy=ExecutionPolicy(mode="unrestricted", origin="home:simulation-fixture",
                                                 classifier="cloud:typesafe"),
                diagnostic_contract=DiagnosticContract(PUBLIC_CONTRACT, "frozen-public-request"))
    budget = BudgetStore(root / "budget.sqlite")
    if budget.report()["authorization"] is None:
        budget.authorize(Authorization("synthetic-only-not-a-real-budget", Decimal("100"),
            (datetime.now(timezone.utc) + timedelta(days=365)).isoformat()))
    bounds = {profile["model_version"]: EnforcedBound(".01", "MockTransport only")
              for profile in profiles.values()}
    bounds[decision["version"]] = EnforcedBound(".001", "MockTransport only")
    runner = BudgetedCore(core, budget, model_bounds=bounds, tool_bounds={}, verify_bound=lambda *args: True)
    contract = RegisteredContract(PUBLIC_CONTRACT, runner, public_environment)
    contracts = {"simulation-" + mode: replace(contract, mode=mode) for mode in MODES}
    identities = SimulationIdentityStore(root / "identities.sqlite",
        diagnostics_path=root / "authentication-diagnostics.jsonl",
        pepper=b"synthetic-customer50-pepper-not-for-production", clock=clock)
    try:
        identities.principal(CUSTOMER)
    except ValueError:
        identities.create_principal(CUSTOMER)
    credentials = {CUSTOMER: identities.issue(CUSTOMER, scopes={"tasks:read", "tasks:write", "models:read"},
                                              expires_at=clock() + 365 * 86400)}
    service = TaskService(root / "tasks", contracts=contracts, allowed_principals={CUSTOMER},
                          principal_active=identities.is_active, retention_policy=ACCESS_POLICY, clock=clock,
                          retention_protected_paths=(identities.path, store.path))
    view = empty_view("synthetic_customer50_mock_only", "2026-10-23T00:00:00+00:00")
    for tier, profile in profiles.items():
        view["categories"][CATEGORY]["pools"][tier][0] = {"position": 0, "profile": profile,
            "status": "mock_only", "quality": profile["external_score"], "runtime_qualified": False}
    pool = store.save_pool(view)
    registry = {(pool, CATEGORY, mode): "simulation-" + mode for mode in MODES}
    if store.control()["routing_pool"] is None:
        store.change(revision=store.control()["revision"], routing=pool, catalog=pool)
    app = assemble_monitor(store, service, registry, identities, operator_token=ADMIN_TOKEN,
        origin=origin, demo=True, labels={CUSTOMER: "模擬客戶 · Ollama 50 題"},
        ollama_models={ALIAS: {"category": CATEGORY, "contract_id": CONTRACT}})
    billing = app.gateway.billing
    with store.db() as db:
        settings = billing.settings(db)
    if settings["revision"] == 0:
        billing.configure({"revision": 0, "enabled": True, "multiplier": "1.2", "client_multipliers": {},
            "open_source_prices": {"example/open_source": {
                "input_per_million": "0.4", "output_per_million": "0.8"}}})
    billing.credit({"client": CUSTOMER, "amount_usd": "1", "idempotency_key": "simulation-opening-credit-v1",
                    "note": "合成測試儲值；不代表真實金流"})
    fixture = CustomerSimulation(app, upstream, identities, credentials, root)
    fixture.controller = SimulationController(fixture)
    app.simulation = fixture.controller
    return fixture

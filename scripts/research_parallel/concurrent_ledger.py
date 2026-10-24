"""Local-only transactional research ledger. No model/network/credential code.

Every ordinary operation touches only its workflow/call and cached aggregate.
Constructors and recover() verify the complete append-only event chain and
rebuild that cache. Only explicit recover(), after workers stop, marks orphaned
unpriced intents unknown. Constructors never diagnose live workers as crashed.
"""

import hashlib
import json
import math
import re
import sqlite3
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path

VERSION = "four-arm-concurrent-ledger-v1"
ZERO = Decimal(0)


class LedgerStop(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class _Refuse(Exception):
    def __init__(self, code, *, persistent=False):
        self.code, self.persistent = code, persistent


def _json(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def _hash(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _amount(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise LedgerStop("MONEY_REQUIRES_EXACT_DECIMAL_STRING_OR_INTEGER")
    try:
        result = Decimal(value)
    except (InvalidOperation, ValueError):
        raise LedgerStop("INVALID_MONEY") from None
    if (
        not result.is_finite()
        or result < 0
        or result > Decimal("1e12")
        or len(result.as_tuple().digits) > 50
        or abs(result.as_tuple().exponent) > 30
    ):
        raise LedgerStop("INVALID_MONEY")
    return result


def _s(value):
    result = format(value, "f")
    return result.rstrip("0").rstrip(".") if "." in result else result


def _identifier(value, field):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,240}", value):
        raise LedgerStop("INVALID_" + field.upper())
    return value


def _sha(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value):
        raise LedgerStop("INVALID_EVIDENCE_SHA256")
    return value


class ConcurrentLedger:
    def __init__(
        self,
        path,
        *,
        plan_id,
        target_usd,
        prior_known_usd="0",
        historical_reserve_usd="0",
        deadline,
        clock,
    ):
        original_path = Path(path).expanduser()
        self.path = original_path.resolve()
        if (
            original_path.is_symlink()
            or not callable(clock)
            or isinstance(deadline, bool)
            or not isinstance(deadline, (int, float))
            or not math.isfinite(deadline)
        ):
            raise LedgerStop("INVALID_LEDGER_CONFIGURATION")
        self.clock, self.deadline = clock, float(deadline)
        self.binding = {
            "version": VERSION,
            "plan_id": _identifier(plan_id, "plan_id"),
            "target_usd": _s(_amount(target_usd)),
            "prior_known_usd": _s(_amount(prior_known_usd)),
            "historical_reserve_usd": _s(_amount(historical_reserve_usd)),
            "deadline": self.deadline,
        }
        with localcontext() as context:
            context.prec = 80
            if _amount(target_usd) <= 0 or _amount(prior_known_usd) + _amount(
                historical_reserve_usd
            ) > _amount(target_usd):
                raise LedgerStop("INITIAL_BUDGET_EXCEEDS_TARGET")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            existing = connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
            if existing:
                columns = [row[1] for row in connection.execute("PRAGMA table_info(events)")]
                if columns != ["seq", "kind", "payload", "previous_hash", "event_hash"]:
                    raise LedgerStop("UNRECOGNIZED_EXISTING_DATABASE")
            self._schema(connection)
            with self._transaction(connection):
                self._rebuild(connection)
                if not connection.execute("SELECT 1 FROM events LIMIT 1").fetchone():
                    self._event(connection, "binding", self.binding)
                elif self._meta(connection, "binding") != self.binding:
                    raise LedgerStop("PLAN_BINDING_MISMATCH")

    @contextmanager
    def _connection(self):
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("PRAGMA synchronous=FULL")
        try:
            yield connection
        finally:
            connection.close()

    @staticmethod
    def _schema(connection):
        connection.executescript("""
            CREATE TABLE IF NOT EXISTS events (
                seq INTEGER PRIMARY KEY, kind TEXT NOT NULL, payload TEXT NOT NULL,
                previous_hash TEXT NOT NULL, event_hash TEXT NOT NULL UNIQUE
            );
            CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
                BEGIN SELECT RAISE(ABORT, 'append_only_events'); END;
            CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
                BEGIN SELECT RAISE(ABORT, 'append_only_events'); END;
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS workflows (id TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS calls (
                id TEXT PRIMARY KEY, workflow_id TEXT NOT NULL, value TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS calls_by_workflow ON calls(workflow_id);
            CREATE TABLE IF NOT EXISTS revisions (
                call_id TEXT, revision_id TEXT, value TEXT NOT NULL,
                PRIMARY KEY(call_id, revision_id));
            CREATE TABLE IF NOT EXISTS generations (
                generation_id TEXT PRIMARY KEY, call_id TEXT NOT NULL);
        """)

    @staticmethod
    @contextmanager
    def _transaction(connection):
        connection.execute("BEGIN IMMEDIATE")
        try:
            with localcontext() as context:
                context.prec = 80
                yield
            connection.execute("COMMIT")
        except BaseException:
            connection.execute("ROLLBACK")
            raise

    @staticmethod
    def _meta(connection, key):
        row = connection.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    @staticmethod
    def _put_meta(connection, key, value):
        connection.execute(
            "INSERT INTO meta(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, _json(value)),
        )

    @staticmethod
    def _row(connection, table, identity):
        row = connection.execute(f"SELECT value FROM {table} WHERE id=?", (identity,)).fetchone()
        return json.loads(row[0]) if row else None

    @staticmethod
    def _put(connection, table, identity, value):
        if table == "calls":
            connection.execute(
                "INSERT INTO calls(id,workflow_id,value) VALUES(?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET value=excluded.value,"
                "workflow_id=excluded.workflow_id",
                (identity, value["workflow_id"], _json(value)),
            )
        else:
            connection.execute(
                f"INSERT INTO {table}(id,value) VALUES(?,?) "
                "ON CONFLICT(id) DO UPDATE SET value=excluded.value",
                (identity, _json(value)),
            )

    def _rebuild(self, connection):
        events = connection.execute(
            "SELECT seq,kind,payload,previous_hash,event_hash FROM events ORDER BY seq"
        ).fetchall()
        previous = "0" * 64
        for number, (seq, kind, raw, parent, digest) in enumerate(events, 1):
            payload = json.loads(raw)
            if (
                seq != number
                or raw != _json(payload)
                or parent != previous
                or digest
                != _hash({"seq": seq, "kind": kind, "payload": payload, "previous_hash": parent})
                or (number == 1 and kind != "binding")
                or (number > 1 and kind == "binding")
            ):
                raise LedgerStop("EVENT_CHAIN_CHANGED")
            previous = digest
        # All cache rows are disposable, never independent fee evidence.
        for table in ("meta", "workflows", "calls", "revisions", "generations"):
            connection.execute(f"DELETE FROM {table}")
        for seq, kind, raw, _, digest in events:
            self._apply(connection, kind, json.loads(raw))
            self._put_meta(connection, "head", {"seq": seq, "hash": digest})

    def _check_binding(self, connection):
        if (
            self.deadline != self.binding["deadline"]
            or self._meta(connection, "binding") != self.binding
        ):
            raise LedgerStop("PLAN_BINDING_MISMATCH")
        head = self._meta(connection, "head")
        last = connection.execute(
            "SELECT seq,event_hash FROM events ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        if not last or head != {"seq": last[0], "hash": last[1]}:
            raise LedgerStop("INCREMENTAL_CACHE_HEAD_CHANGED_RECOVER_REQUIRED")

    def _event(self, connection, kind, payload):
        head = self._meta(connection, "head") or {"seq": 0, "hash": "0" * 64}
        seq = head["seq"] + 1
        digest = _hash(
            {"seq": seq, "kind": kind, "payload": payload, "previous_hash": head["hash"]}
        )
        connection.execute(
            "INSERT INTO events VALUES(?,?,?,?,?)",
            (seq, kind, _json(payload), head["hash"], digest),
        )
        self._apply(connection, kind, payload)
        self._put_meta(connection, "head", {"seq": seq, "hash": digest})

    @staticmethod
    def _remaining(workflow):
        return (
            max(ZERO, Decimal(workflow["forecast_usd"]) - Decimal(workflow["actual_usd"]))
            if workflow["status"] in ("reserved", "active")
            else ZERO
        )

    def _apply(self, connection, kind, p):
        if kind == "binding":
            self._put_meta(connection, "binding", p)
            self._put_meta(
                connection,
                "aggregate",
                {
                    "actual_usd": "0",
                    "reservations_usd": "0",
                    "historical_imported_usd": "0",
                    "global_stop": None,
                },
            )
            return
        aggregate = self._meta(connection, "aggregate")
        if kind == "stop":
            if aggregate["global_stop"] is None:
                aggregate["global_stop"] = p
        elif kind == "admit":
            self._put(
                connection,
                "workflows",
                p["workflow_id"],
                {
                    **p,
                    "actual_usd": "0",
                    "historical_imported_usd": "0",
                    "status": "reserved",
                    "generations_started": 0,
                    "classifiers_started": 0,
                    "completion_sha256": None,
                },
            )
            aggregate["reservations_usd"] = _s(
                Decimal(aggregate["reservations_usd"]) + Decimal(p["forecast_usd"])
            )
        else:
            cid = p.get("call_id")
            call = self._row(connection, "calls", cid) if cid else None
            wid = p.get("workflow_id") or call["workflow_id"]
            workflow = self._row(connection, "workflows", wid)
            before = self._remaining(workflow)
            if kind == "begin":
                self._put(
                    connection,
                    "calls",
                    cid,
                    {
                        **p,
                        "response_sha256": None,
                        "generation_id": None,
                        "cost_usd": None,
                        "evidence_sha256": None,
                        "revision_id": None,
                        "unknown": False,
                        "unknown_reason": None,
                        "step_sha256": None,
                    },
                )
                workflow["status"] = "active"
                counter = (
                    "generations_started" if p["kind"] == "generation" else "classifiers_started"
                )
                workflow[counter] += 1
            elif kind == "historical_import":
                call = {
                    **p,
                    "forecast_usd": "0",
                    "historical": True,
                    "step_sha256": None,
                    "revision_id": "usage",
                    "unknown": False,
                    "unknown_reason": None,
                }
                connection.execute(
                    "INSERT INTO revisions VALUES(?,?,?)",
                    (
                        cid,
                        "usage",
                        _json({"cost_usd": p["cost_usd"], "evidence_sha256": p["evidence_sha256"]}),
                    ),
                )
                connection.execute("INSERT INTO generations VALUES(?,?)", (p["generation_id"], cid))
                workflow["status"] = "active"
                counter = (
                    "generations_started" if p["kind"] == "generation" else "classifiers_started"
                )
                workflow[counter] += 1
                workflow["historical_imported_usd"] = _s(
                    Decimal(workflow["historical_imported_usd"]) + Decimal(p["cost_usd"])
                )
                aggregate["historical_imported_usd"] = _s(
                    Decimal(aggregate["historical_imported_usd"]) + Decimal(p["cost_usd"])
                )
            elif kind == "response":
                call["response_sha256"] = p["response_sha256"]
            elif kind == "generation":
                call["generation_id"] = p["generation_id"]
                call["duplicate_of"] = p.get("duplicate_of")
                if p.get("duplicate_of"):
                    call.update(unknown=True, unknown_reason="duplicate_generation_identity")
                connection.execute(
                    "INSERT OR IGNORE INTO generations VALUES(?,?)", (p["generation_id"], cid)
                )
            elif kind == "cost":
                revision = {"cost_usd": p["cost_usd"], "evidence_sha256": p["evidence_sha256"]}
                connection.execute(
                    "INSERT INTO revisions VALUES(?,?,?)", (cid, p["revision_id"], _json(revision))
                )
                # Official canonical receipts supersede usage, never vice versa.
                if call["revision_id"] in (None, "usage") or p["revision_id"] != "usage":
                    delta = Decimal(p["cost_usd"]) - (
                        Decimal(call["cost_usd"]) if call["cost_usd"] is not None else ZERO
                    )
                    workflow["actual_usd"] = _s(Decimal(workflow["actual_usd"]) + delta)
                    aggregate["actual_usd"] = _s(Decimal(aggregate["actual_usd"]) + delta)
                    call.update(revision, revision_id=p["revision_id"], unknown=False)
            elif kind == "unknown":
                call.update(unknown=True, unknown_reason=p["reason"])
            elif kind == "step":
                call["step_sha256"] = p["step_sha256"]
            elif kind == "completion":
                workflow["completion_sha256"] = p["completion_sha256"]
            elif kind in ("finish", "release"):
                workflow["status"] = "finished" if kind == "finish" else "released"
            else:
                raise LedgerStop("UNSUPPORTED_EVENT")
            if call is not None:
                self._put(connection, "calls", cid, call)
            aggregate["reservations_usd"] = _s(
                Decimal(aggregate["reservations_usd"]) + self._remaining(workflow) - before
            )
            self._put(connection, "workflows", wid, workflow)
        self._put_meta(connection, "aggregate", aggregate)

    def _operate(self, function):
        refusal = None
        with self._connection() as connection, self._transaction(connection):
            self._check_binding(connection)
            try:
                result = function(connection)
            except _Refuse as error:
                refusal = error.code
                result = None
                if error.persistent:
                    self._event(connection, "stop", {"code": error.code, "at": self._now()})
        if refusal:
            raise LedgerStop(refusal)
        return result

    def _now(self):
        value = self.clock()
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
        ):
            raise LedgerStop("INVALID_CLOCK")
        return float(value)

    def _gate(self, connection):
        if self._meta(connection, "aggregate")["global_stop"] is not None:
            raise _Refuse("GLOBAL_DURABLE_STOP")
        if self._now() >= self.deadline:
            raise _Refuse("AUTHORIZATION_EXPIRED", persistent=True)

    def _claim(self, connection):
        aggregate = self._meta(connection, "aggregate")
        return (
            Decimal(self.binding["prior_known_usd"])
            + Decimal(self.binding["historical_reserve_usd"])
            + Decimal(aggregate["actual_usd"])
            + Decimal(aggregate["reservations_usd"])
        )

    def admit(self, workflow_id, forecast_usd, generation_cap, classifier_cap):
        wid, forecast = _identifier(workflow_id, "workflow_id"), _s(_amount(forecast_usd))
        if (
            Decimal(forecast) <= 0
            or any(
                type(c) is not int or not 0 <= c <= 1000 for c in (generation_cap, classifier_cap)
            )
            or generation_cap + classifier_cap == 0
        ):
            raise LedgerStop("INVALID_WORKFLOW_CAP_OR_FORECAST")
        payload = {
            "workflow_id": wid,
            "forecast_usd": forecast,
            "generation_cap": generation_cap,
            "classifier_cap": classifier_cap,
        }

        def action(c):
            old = self._row(c, "workflows", wid)
            if old:
                if any(old[k] != value for k, value in payload.items()):
                    raise _Refuse("WORKFLOW_CONTRACT_CHANGED", persistent=True)
                return False
            self._gate(c)
            if self._claim(c) + Decimal(forecast) > Decimal(self.binding["target_usd"]):
                raise _Refuse("COMPLETE_WORKFLOW_RESERVATION_EXCEEDS_TARGET")
            self._gate(c)
            self._event(c, "admit", payload)
            return True

        return self._operate(action)

    def begin(self, call_id, workflow_id, kind, forecast_usd, request_sha256):
        cid, wid = _identifier(call_id, "call_id"), _identifier(workflow_id, "workflow_id")
        if kind not in ("generation", "classifier"):
            raise LedgerStop("INVALID_CALL_KIND")
        payload = {
            "call_id": cid,
            "workflow_id": wid,
            "kind": kind,
            "forecast_usd": _s(_amount(forecast_usd)),
            "request_sha256": _sha(request_sha256),
        }

        def action(c):
            old = self._row(c, "calls", cid)
            if old:
                if any(old[k] != value for k, value in payload.items()):
                    raise _Refuse("CALL_ID_OR_REQUEST_CONFLICT", persistent=True)
                return False
            self._gate(c)
            workflow = self._row(c, "workflows", wid)
            if not workflow or workflow["status"] not in ("reserved", "active"):
                raise _Refuse("WORKFLOW_NOT_ADMITTED")
            counter, cap = (
                ("generations_started", "generation_cap")
                if kind == "generation"
                else ("classifiers_started", "classifier_cap")
            )
            if workflow[counter] >= workflow[cap]:
                raise _Refuse("WORKFLOW_CALL_CAP_EXCEEDED")
            pending = sum(
                (
                    Decimal(call["forecast_usd"])
                    for row in c.execute("SELECT value FROM calls WHERE workflow_id=?", (wid,))
                    if (call := json.loads(row[0]))["cost_usd"] is None
                ),
                ZERO,
            )
            if Decimal(payload["forecast_usd"]) + pending > self._remaining(workflow):
                raise _Refuse("INFLIGHT_CALLS_EXCEED_WORKFLOW_REMAINING_RESERVE")
            at = self._now()
            if at >= self.deadline:
                raise _Refuse("AUTHORIZATION_EXPIRED", persistent=True)
            self._event(c, "begin", {**payload, "at": at})
            return True

        return self._operate(action)

    def import_historical_call(
        self,
        call_id,
        workflow_id,
        kind,
        request_sha256,
        response_sha256,
        generation_id,
        cost_usd,
        evidence_sha256,
        *,
        required_prior_known_usd,
    ):
        """Link verified paid history already charged in prior_known_usd.

        This is not dispatch authority or supplier evidence verification. The
        caller must verify original immutable raw files and exact wire identity.
        It consumes a physical attempt, but preserves the complete remaining
        forecast and never adds the same historical fee to new actual spending.
        """
        cid = _identifier(call_id, "call_id")
        wid = _identifier(workflow_id, "workflow_id")
        if kind not in ("generation", "classifier"):
            raise LedgerStop("INVALID_CALL_KIND")
        payload = {
            "call_id": cid,
            "workflow_id": wid,
            "kind": kind,
            "request_sha256": _sha(request_sha256),
            "response_sha256": _sha(response_sha256),
            "generation_id": _identifier(generation_id, "generation_id"),
            "cost_usd": _s(_amount(cost_usd)),
            "evidence_sha256": _sha(evidence_sha256),
            "required_prior_known_usd": _s(_amount(required_prior_known_usd)),
        }

        def action(c):
            old = self._row(c, "calls", cid)
            if old:
                if not old.get("historical") or any(
                    old[k] != value for k, value in payload.items()
                ):
                    raise _Refuse("HISTORICAL_CALL_IDENTITY_CONFLICT", persistent=True)
                return False
            workflow = self._row(c, "workflows", wid)
            if not workflow or workflow["status"] not in ("reserved", "active"):
                raise _Refuse("WORKFLOW_NOT_ADMITTED")
            prior = Decimal(payload["required_prior_known_usd"])
            aggregate = self._meta(c, "aggregate")
            if (
                Decimal(payload["cost_usd"]) > prior
                or prior > Decimal(self.binding["prior_known_usd"])
                or Decimal(aggregate["historical_imported_usd"]) + Decimal(payload["cost_usd"])
                > prior
            ):
                raise _Refuse("HISTORICAL_IMPORT_EXCEEDS_BOUND_PRIOR_KNOWN", persistent=True)
            counter, cap = (
                ("generations_started", "generation_cap")
                if kind == "generation"
                else ("classifiers_started", "classifier_cap")
            )
            if workflow[counter] >= workflow[cap]:
                raise _Refuse("WORKFLOW_CALL_CAP_EXCEEDED")
            if c.execute(
                "SELECT 1 FROM generations WHERE generation_id=?", (payload["generation_id"],)
            ).fetchone():
                raise _Refuse("DUPLICATE_GLOBAL_GENERATION_ID", persistent=True)
            if any(
                json.loads(row[0])["response_sha256"] == payload["response_sha256"]
                for row in c.execute("SELECT value FROM calls")
            ):
                raise _Refuse("DUPLICATE_HISTORICAL_RESPONSE_IDENTITY", persistent=True)
            self._event(c, "historical_import", payload)
            return True

        return self._operate(action)

    def save_response(self, call_id, response_sha256):
        cid, proof = _identifier(call_id, "call_id"), _sha(response_sha256)

        def action(c):
            call = self._row(c, "calls", cid)
            if not call:
                raise _Refuse("CALL_INTENT_MISSING")
            if call["response_sha256"] is not None:
                if call["response_sha256"] != proof:
                    raise _Refuse("RESPONSE_IDENTITY_CONFLICT", persistent=True)
                return
            self._event(c, "response", {"call_id": cid, "response_sha256": proof})

        self._operate(action)

    def bind_generation(self, call_id, generation_id):
        cid, gid = _identifier(call_id, "call_id"), _identifier(generation_id, "generation_id")

        def action(c):
            call = self._row(c, "calls", cid)
            if not call or call["response_sha256"] is None:
                raise _Refuse("SAVE_RAW_RESPONSE_BEFORE_BINDING_GENERATION")
            if call["generation_id"] is not None:
                if call["generation_id"] != gid:
                    raise _Refuse("GENERATION_IDENTITY_CHANGED", persistent=True)
                return
            other = c.execute(
                "SELECT call_id FROM generations WHERE generation_id=?", (gid,)
            ).fetchone()
            duplicate = other[0] if other and other[0] != cid else None
            self._event(
                c, "generation", {"call_id": cid, "generation_id": gid, "duplicate_of": duplicate}
            )
            if duplicate:
                raise _Refuse("DUPLICATE_GLOBAL_GENERATION_ID", persistent=True)

        self._operate(action)

    def reconcile(self, call_id, cost_usd, evidence_sha256, revision_id="usage"):
        cid, revision = _identifier(call_id, "call_id"), _identifier(revision_id, "revision_id")
        value = {"cost_usd": _s(_amount(cost_usd)), "evidence_sha256": _sha(evidence_sha256)}

        def action(c):
            call = self._row(c, "calls", cid)
            if not call or call["response_sha256"] is None:
                raise _Refuse("RAW_RESPONSE_REQUIRED_FOR_KNOWN_COST")
            if call.get("historical"):
                if revision == call["revision_id"] and value == {
                    "cost_usd": call["cost_usd"],
                    "evidence_sha256": call["evidence_sha256"],
                }:
                    return
                raise _Refuse(
                    "HISTORICAL_COST_REVISION_REQUIRES_PRIOR_LEDGER_ADJUSTMENT", persistent=True
                )
            if call.get("duplicate_of"):
                raise _Refuse("COST_FOR_DUPLICATE_GENERATION_UNATTRIBUTABLE", persistent=True)
            old = c.execute(
                "SELECT value FROM revisions WHERE call_id=? AND revision_id=?", (cid, revision)
            ).fetchone()
            if old:
                if json.loads(old[0]) != value:
                    raise _Refuse("COST_REVISION_CONFLICT", persistent=True)
                return
            self._event(c, "cost", {"call_id": cid, "revision_id": revision, **value})
            if self._claim(c) > Decimal(self.binding["target_usd"]):
                raise _Refuse(
                    "LATE_COST_DEFICIT_OTHER_WORKFLOW_RESERVES_PRESERVED", persistent=True
                )

        self._operate(action)

    def seal_step(self, call_id, step_sha256):
        """Bind saved public-validation evidence for a known paid attempt.

        This does not assert hidden correctness or replace fee evidence. A later
        canonical fee replacement leaves validation content unchanged.
        """
        cid, proof = _identifier(call_id, "call_id"), _sha(step_sha256)

        def action(c):
            call = self._row(c, "calls", cid)
            if (
                not call
                or call["response_sha256"] is None
                or call["cost_usd"] is None
                or call["unknown"]
            ):
                raise _Refuse("KNOWN_RESPONSE_COST_REQUIRED_BEFORE_STEP_SEAL")
            old = call.get("step_sha256")
            if old is not None:
                if old != proof:
                    raise _Refuse("STEP_SEAL_IDENTITY_CHANGED", persistent=True)
                return
            self._event(c, "step", {"call_id": cid, "step_sha256": proof})

        self._operate(action)

    def halt(self, reason):
        """Persist a dispatch stop without inventing a call, response or fee."""
        if not isinstance(reason, str) or not reason or len(reason) > 500:
            raise LedgerStop("INVALID_STOP_REASON")

        def action(c):
            if self._meta(c, "aggregate")["global_stop"] is None:
                self._event(c, "stop", {"code": reason, "at": self._now()})

        self._operate(action)

    def mark_unknown(self, call_id, reason):
        cid = _identifier(call_id, "call_id")
        if not isinstance(reason, str) or not reason or len(reason) > 500:
            raise LedgerStop("INVALID_UNKNOWN_REASON")

        def action(c):
            if not self._row(c, "calls", cid):
                raise _Refuse("UNKNOWN_CALL_HAS_NO_INTENT")
            self._event(c, "unknown", {"call_id": cid, "reason": reason})
            self._event(c, "stop", {"code": "UNKNOWN_COST", "call_id": cid, "at": self._now()})

        self._operate(action)

    def finish(self, workflow_id):
        wid = _identifier(workflow_id, "workflow_id")

        def action(c):
            workflow = self._row(c, "workflows", wid)
            if not workflow:
                raise _Refuse("WORKFLOW_NOT_ADMITTED")
            if workflow["status"] == "finished":
                return
            if workflow["status"] == "released":
                raise _Refuse("WORKFLOW_ALREADY_RELEASED")
            calls = [
                json.loads(row[0])
                for row in c.execute("SELECT value FROM calls WHERE workflow_id=?", (wid,))
            ]
            if not calls or any(
                call["response_sha256"] is None or call["cost_usd"] is None or call["unknown"]
                for call in calls
            ):
                raise _Refuse("CANNOT_FINISH_UNRESOLVED_WORKFLOW")
            self._event(c, "finish", {"workflow_id": wid})

        self._operate(action)

    def seal_completion(self, workflow_id, completion_sha256):
        """Bind an immutable prepared terminal only after fees are finished.

        The caller verifies its raw evidence paths; this SHA is a durable
        cross-check against accidentally reusing completion with an empty ledger.
        It carries no model-quality or supplier-cost qualification by itself.
        """
        wid = _identifier(workflow_id, "workflow_id")
        proof = _sha(completion_sha256)

        def action(c):
            workflow = self._row(c, "workflows", wid)
            if not workflow or workflow["status"] != "finished":
                raise _Refuse("WORKFLOW_MUST_BE_FINISHED_BEFORE_COMPLETION_SEAL")
            existing = workflow.get("completion_sha256")
            if existing is not None:
                if existing != proof:
                    raise _Refuse("COMPLETION_SEAL_IDENTITY_CHANGED", persistent=True)
                return
            self._event(c, "completion", {"workflow_id": wid, "completion_sha256": proof})

        self._operate(action)

    def release(self, workflow_id):
        """Explicit release of a reserved workflow only before its first intent."""
        wid = _identifier(workflow_id, "workflow_id")

        def action(c):
            workflow = self._row(c, "workflows", wid)
            if not workflow or workflow["status"] == "released":
                return
            if workflow["status"] != "reserved":
                raise _Refuse("CANNOT_RELEASE_DISPATCHED_WORKFLOW")
            self._event(c, "release", {"workflow_id": wid})

        self._operate(action)

    def lookup_workflow(self, workflow_id):
        """One cached row, including immutable completion binding; no full scan."""
        wid = _identifier(workflow_id, "workflow_id")
        return self._operate(lambda c: self._row(c, "workflows", wid))

    def has_not_dispatched_calls(self, workflow_id):
        """Read explicit stopped-attempt evidence for this workflow only."""
        wid = _identifier(workflow_id, "workflow_id")

        def action(c):
            return any(
                json.loads(row[0]).get("revision_id") == "not_dispatched"
                for row in c.execute("SELECT value FROM calls WHERE workflow_id=?", (wid,))
            )

        return self._operate(action)

    def lookup(self, call_id):
        cid = _identifier(call_id, "call_id")

        def action(c):
            call = self._row(c, "calls", cid)
            return {**call, "state": self._call_state(call)} if call else None

        return self._operate(action)

    @staticmethod
    def _call_state(call):
        return (
            "unknown"
            if call["unknown"]
            else "known"
            if call["cost_usd"] is not None
            else "response_saved"
            if call["response_sha256"] is not None
            else "intent_in_flight"
        )

    def _snapshot(self, c):
        aggregate = self._meta(c, "aggregate")
        workflows = [
            json.loads(row[0]) for row in c.execute("SELECT value FROM workflows ORDER BY id")
        ]
        calls = [json.loads(row[0]) for row in c.execute("SELECT value FROM calls ORDER BY id")]
        for workflow in workflows:
            workflow["unused_reservation_usd"] = _s(self._remaining(workflow))
        known = Decimal(self.binding["prior_known_usd"]) + Decimal(aggregate["actual_usd"])
        unknown = sum(call["unknown"] for call in calls)
        pending = sum(call["cost_usd"] is None for call in calls)
        for call in calls:
            call["state"] = self._call_state(call)
        return {
            **self.binding,
            **aggregate,
            "known_cumulative_usd": _s(known),
            "actual_cumulative_usd": None
            if unknown or pending or Decimal(self.binding["historical_reserve_usd"])
            else _s(known),
            "budget_claim_usd": _s(self._claim(c)),
            "remaining_target_usd": _s(Decimal(self.binding["target_usd"]) - self._claim(c)),
            "unused_reservations_usd": aggregate["reservations_usd"],
            "workflows": workflows,
            "calls": calls,
            "unknown_calls": unknown,
            "active_workflows": sum(w["status"] == "active" for w in workflows),
            "in_flight_calls": sum(call["response_sha256"] is None for call in calls),
            "pending_cost_calls": pending,
            "expired": self._now() >= self.deadline,
            "event_head": self._meta(c, "head"),
            "cache_is_derived_not_fee_evidence": True,
            "generation_bindings": dict(c.execute("SELECT generation_id,call_id FROM generations")),
            "duplicate_generation_calls": [
                call["call_id"] for call in calls if call.get("duplicate_of")
            ],
        }

    def snapshot(self):
        return self._operate(self._snapshot)

    def recover(self):
        """After all workers stop, verify/rebuild and quarantine orphaned intents."""
        with self._connection() as c, self._transaction(c):
            self._rebuild(c)
            self._check_binding(c)
            pending = [json.loads(row[0]) for row in c.execute("SELECT value FROM calls")]
            for call in pending:
                if call["cost_usd"] is None and not call["unknown"]:
                    reason = (
                        "crash_intent_without_response"
                        if call["response_sha256"] is None
                        else "crash_response_without_verified_cost"
                    )
                    self._event(c, "unknown", {"call_id": call["call_id"], "reason": reason})
                    self._event(
                        c,
                        "stop",
                        {
                            "code": "RECOVERY_UNRESOLVED_COST",
                            "call_id": call["call_id"],
                            "at": self._now(),
                        },
                    )
            result = self._snapshot(c)
            result["event_chain_verified_and_cache_rebuilt"] = True
            return result

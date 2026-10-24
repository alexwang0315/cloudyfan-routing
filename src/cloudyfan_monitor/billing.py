"""Customer USD accounting, independent of routing estimates and supplier budgets."""
import json
import hashlib
from decimal import Decimal, ROUND_HALF_UP, localcontext
import re
import threading
import time

from .store import MonitorError, digest, encode, identifier


DEFAULTS = {"enabled": False, "multiplier": "1", "client_multipliers": {},
            "open_source_prices": {}}


def amount(value, *, maximum="1000000000", positive=False):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,10}(?:\.[0-9]{1,12})?", value):
        raise MonitorError("billing_invalid_amount")
    number = Decimal(value)
    if number > Decimal(maximum) or (positive and number <= 0):
        raise MonitorError("billing_invalid_amount")
    return format(number, "f").rstrip("0").rstrip(".") if "." in format(number, "f") else str(number)


def money(value):
    return format(value, "f").rstrip("0").rstrip(".") if "." in format(value, "f") else str(value)


def freeze_policy(db, binding):
    # Called in the same transaction that accepts a new task. Legacy tasks are
    # never enrolled later by a dashboard read, reconciliation, or restart.
    if db.execute("SELECT name FROM sqlite_master WHERE name='billing_control'").fetchone() is None:
        return
    row = db.execute("SELECT revision,data FROM billing_control WHERE id=1").fetchone()
    settings = json.loads(row["data"])
    policy = {"revision": row["revision"], "enabled": settings["enabled"],
              "multiplier": settings["client_multipliers"].get(binding["client"], settings["multiplier"]),
              "open_source_prices": settings["open_source_prices"]}
    db.execute("INSERT INTO billing_task_policies VALUES(?,?,?)",
               (binding["client"], binding["run"], encode(policy)))


class Billing:
    def __init__(self, store, *, demo=False, source=None):
        self.store, self.demo, self.source = store, demo, source
        self.reconcile_lock = threading.Lock()
        with store.db() as db:
            db.execute("CREATE TABLE IF NOT EXISTS billing_control "
                       "(id INTEGER PRIMARY KEY, revision INTEGER, data TEXT)")
            db.execute("INSERT OR IGNORE INTO billing_control VALUES(1,0,?)", (encode(DEFAULTS),))
            db.execute("CREATE TABLE IF NOT EXISTS billing_entries "
                       "(id TEXT PRIMARY KEY, client TEXT, run_id TEXT, kind TEXT, "
                       "amount_usd TEXT, data TEXT, created REAL)")
            db.execute("CREATE TABLE IF NOT EXISTS billing_task_policies "
                       "(client TEXT, run_id TEXT, data TEXT, PRIMARY KEY(client,run_id))")
            db.execute("CREATE TABLE IF NOT EXISTS billing_waiting (id TEXT PRIMARY KEY, data TEXT)")
            db.execute("CREATE TABLE IF NOT EXISTS billing_attempts (id TEXT PRIMARY KEY, sequence INTEGER)")
            db.execute("CREATE TABLE IF NOT EXISTS billing_source_state (id INTEGER PRIMARY KEY, data TEXT)")
            db.execute("CREATE TABLE IF NOT EXISTS billing_supplier_evidence "
                       "(sha256 TEXT PRIMARY KEY, source TEXT, evidence_json TEXT, created REAL)")

    def _retain_evidence(self, result):
        result = dict(result)
        raw = result.pop("evidence_json", None)
        if not isinstance(raw, str) or hashlib.sha256(raw.encode()).hexdigest() != result.get("evidence_sha256"):
            raise MonitorError("billing_supplier_evidence_invalid", 502)
        with self.store.db() as db:
            db.execute("INSERT OR IGNORE INTO billing_supplier_evidence VALUES(?,?,?,?)",
                       (result["evidence_sha256"], result.get("source", result.get("scope")),
                        raw, self.store.clock()))
        return result

    def evidence(self, sha):
        if not re.fullmatch(r"[a-f0-9]{64}", sha):
            raise MonitorError("billing_evidence_not_found", 404)
        with self.store.db() as db:
            row = db.execute("SELECT * FROM billing_supplier_evidence WHERE sha256=?", (sha,)).fetchone()
            if row is None:
                raise MonitorError("billing_evidence_not_found", 404)
            return dict(row)

    def read_account(self):
        if self.source is None:
            raise MonitorError("openrouter_accounting_not_configured", 409)
        result = self.source.account()
        result = {**result, **{scope: self._retain_evidence(result[scope])
                              for scope in ("key", "account")
                              if result[scope]["status"] == "available"}}
        with self.store.db() as db:
            db.execute("INSERT INTO billing_source_state VALUES(1,?) ON CONFLICT(id) "
                       "DO UPDATE SET data=excluded.data", (encode(result),))
            self.store.audit(db, "billing_supplier_account_read", {"status": result["status"]})
        return result

    def _policy(self, row):
        with self.store.db() as db:
            policy = db.execute("SELECT data FROM billing_task_policies WHERE client=? AND run_id=?",
                                (row.get("client"), row.get("task_run"))).fetchone()
            if policy is None:
                return None
            return json.loads(policy["data"])

    def _evidence(self, row, time_budget):
        if row.get("metadata_unavailable") or not isinstance(row.get("model"), str):
            raise MonitorError("billing_request_metadata_missing", 409)
        if self.demo:
            if row.get("cost") is None:
                raise MonitorError("billing_supplier_cost_pending", 409)
            tokens = row.get("tokens", {})
            return self._retain_evidence({"cost_usd": amount(row["cost"]),
                    "input_tokens": tokens.get("prompt_tokens", tokens.get("input_tokens")),
                    "output_tokens": tokens.get("completion_tokens", tokens.get("output_tokens")),
                    "source": "synthetic_core_budget", "evidence_sha256": digest(row),
                    "evidence_json": encode(row)})
        if row.get("billing_origin") == "openrouter":
            if self.source is None:
                raise MonitorError("openrouter_accounting_not_configured", 409)
            return self._retain_evidence(self.source.generation(
                row.get("generation_id"), expected_model=row["model"], time_budget=time_budget))
        if row.get("billing_origin") == "local":
            tokens = row.get("tokens", {})
            return self._retain_evidence({"cost_usd": None, "input_tokens": tokens.get("input_tokens"),
                    "output_tokens": tokens.get("output_tokens"), "source": "local_recorded_usage",
                    "evidence_sha256": digest(row), "evidence_json": encode(row)})
        raise MonitorError("billing_supplier_not_supported", 409)

    def _post_charge(self, key, row, policy, evidence):
        rule = policy["open_source_prices"].get(row["model"])
        if (row.get("open_source") or row.get("billing_origin") == "local") and rule is None:
            raise MonitorError("billing_open_source_price_missing", 409)
        if rule is not None:
            counts = [evidence.get("input_tokens"), evidence.get("output_tokens")]
            if any(type(n) is not int or not 0 <= n <= 10**12 for n in counts):
                raise MonitorError("billing_token_usage_pending", 409)
            basis = "open_source_token_price"
        else:
            if evidence.get("cost_usd") is None:
                raise MonitorError("billing_supplier_cost_pending", 409)
            basis = evidence["source"]
        # Supplier evidence permits 24 fractional places. Keep the full amount
        # through multiplication, then round once at the customer-ledger scale.
        with localcontext() as context:
            context.prec = 64
            base = ((Decimal(rule["input_per_million"]) * counts[0]
                     + Decimal(rule["output_per_million"]) * counts[1]) / 1000000
                    if rule is not None else Decimal(evidence["cost_usd"]))
            value = money((base * Decimal(policy["multiplier"])).quantize(
                Decimal("0.000000000001"), rounding=ROUND_HALF_UP))
        entry = {"id": key, "kind": "debit", "client": row["client"], "run_id": row["task_run"],
                 "amount_usd": value, "model": row["model"], "basis": basis,
                 "supplier_cost_usd": evidence.get("cost_usd"), "supplier_source": evidence["source"],
                 "supplier_account_ref": evidence.get("account_ref"),
                 "evidence_sha256": evidence["evidence_sha256"], "generation_id": row.get("generation_id"),
                 "pricing_revision": policy["revision"], "multiplier": policy["multiplier"],
                 "token_price": rule, "input_tokens": evidence.get("input_tokens"),
                 "output_tokens": evidence.get("output_tokens"), "demo": self.demo,
                 "created": self.store.clock()}
        with self.store.db() as db:
            old = db.execute("SELECT data FROM billing_entries WHERE id=?", (key,)).fetchone()
            if old:
                previous = json.loads(old["data"])
                if (previous["client"], previous["run_id"]) != (row["client"], row["task_run"]):
                    raise MonitorError("billing_generation_owner_conflict", 409)
                return False
            db.execute("INSERT INTO billing_entries VALUES(?,?,?,?,?,?,?)", (key, row["client"],
                       row["task_run"], "debit", value, encode(entry), entry["created"]))
            db.execute("DELETE FROM billing_waiting WHERE id=?", (key,))
            self.store.audit(db, "billing_debit_recorded", {"entry": key, "client": row["client"]})
            return True

    def reconcile(self, ledger):
        if not self.reconcile_lock.acquire(blocking=False):
            raise MonitorError("billing_reconciliation_busy", 409)
        charged, errors, attempted = 0, [], 0
        deadline = time.monotonic() + 30
        try:
            with self.store.db() as db:
                if not self.settings(db)["enabled"]:
                    raise MonitorError("billing_disabled", 409)
                attempts = {r["id"]: r["sequence"] for r in db.execute("SELECT * FROM billing_attempts")}
            candidates = []
            for row in ledger["rows"]:
                identity = ({"provider": "openrouter", "generation": row["generation_id"]}
                            if row.get("generation_id") else {"account": row["account"], "event": row["id"]})
                candidates.append(("debit:" + digest(identity), row))
            # Persisted order lets new and least-recently-attempted requests run
            # even when earlier supplier receipts are still unavailable.
            for key, row in sorted(candidates, key=lambda item: attempts.get(item[0], 0)):
                policy = self._policy(row)
                if policy is None or not policy["enabled"]:
                    continue
                with self.store.db() as db:
                    old = db.execute("SELECT data FROM billing_entries WHERE id=?", (key,)).fetchone()
                    task = db.execute("SELECT data FROM tasks WHERE client=? AND run=?",
                                      (row["client"], row["task_run"])).fetchone()
                if old:
                    previous = json.loads(old["data"])
                    if (previous["client"], previous["run_id"]) != (row["client"], row["task_run"]):
                        errors.append({"client": row["client"], "run_id": row["task_run"],
                                       "reason": "billing_generation_owner_conflict"})
                    continue
                try:
                    if attempted >= 20 or time.monotonic() >= deadline:
                        raise MonitorError("billing_batch_deferred", 409)
                    attempted += 1
                    with self.store.db() as db:
                        sequence = db.execute("SELECT COALESCE(MAX(sequence),0)+1 FROM billing_attempts").fetchone()[0]
                        db.execute("INSERT INTO billing_attempts VALUES(?,?) ON CONFLICT(id) "
                                   "DO UPDATE SET sequence=excluded.sequence", (key, sequence))
                    if task is None or json.loads(task["data"])["status"] in {"bound", "ready", "queued", "running"}:
                        raise MonitorError("billing_task_running", 409)
                    charged += self._post_charge(key, row, policy, self._evidence(row, deadline-time.monotonic()))
                except Exception as error:
                    reason = getattr(error, "code", "billing_reconciliation_unavailable")
                    pending = {"client": row["client"], "run_id": row["task_run"],
                               "model": row.get("model"), "reason": reason}
                    with self.store.db() as db:
                        db.execute("INSERT INTO billing_waiting VALUES(?,?) ON CONFLICT(id) "
                                   "DO UPDATE SET data=excluded.data", (key, encode(pending)))
                    errors.append(pending)
            return {"charged": charged, "waiting": len(errors), "errors": errors, "attempted": attempted}
        finally:
            self.reconcile_lock.release()

    def settings(self, db):
        row = db.execute("SELECT revision,data FROM billing_control WHERE id=1").fetchone()
        return {**json.loads(row["data"]), "revision": row["revision"]}

    def configure(self, body):
        if type(body["revision"]) is not int or type(body["enabled"]) is not bool:
            raise MonitorError("billing_invalid_configuration")
        multiplier = amount(body["multiplier"], maximum="100")
        overrides, prices = body["client_multipliers"], body["open_source_prices"]
        if not isinstance(overrides, dict) or not isinstance(prices, dict) or len(prices) > 200:
            raise MonitorError("billing_invalid_configuration")
        clean_prices = {}
        for model, price in prices.items():
            if (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_./:@+\-]{0,199}", model)
                    or not isinstance(price, dict)
                    or set(price) != {"input_per_million", "output_per_million"}):
                raise MonitorError("billing_invalid_model_price")
            clean_prices[model] = {key: amount(value, maximum="1000000") for key, value in price.items()}
        with self.store.db() as db:
            if self.settings(db)["revision"] != body["revision"]:
                raise MonitorError("billing_configuration_changed_reload", 409)
            clients = {row["id"] for row in db.execute("SELECT id FROM clients")}
            if not set(overrides) <= clients:
                raise MonitorError("client_not_found", 404)
            value = {"enabled": body["enabled"], "multiplier": multiplier,
                     "client_multipliers": {key: amount(v, maximum="100") for key, v in overrides.items()},
                     "open_source_prices": clean_prices}
            db.execute("UPDATE billing_control SET revision=revision+1,data=? WHERE id=1", (encode(value),))
            self.store.audit(db, "billing_settings_changed", {"revision": body["revision"] + 1})
            return self.settings(db)

    def credit(self, body):
        identifier(body["client"])
        identifier(body["idempotency_key"])
        value = amount(body["amount_usd"], positive=True)
        if not isinstance(body["note"], str) or not 1 <= len(body["note"].strip()) <= 240:
            raise MonitorError("billing_note_required")
        payload = {"client": body["client"], "amount_usd": value, "note": body["note"]}
        key = "credit:" + body["idempotency_key"]
        with self.store.db() as db:
            if db.execute("SELECT id FROM clients WHERE id=?", (body["client"],)).fetchone() is None:
                raise MonitorError("client_not_found", 404)
            old = db.execute("SELECT data FROM billing_entries WHERE id=?", (key,)).fetchone()
            if old:
                previous = json.loads(old["data"])
                if any(previous[k] != v for k, v in payload.items()):
                    raise MonitorError("billing_idempotency_conflict", 409)
                return previous
            entry = {"id": key, "kind": "credit", "run_id": None, "created": self.store.clock(), **payload}
            db.execute("INSERT INTO billing_entries VALUES(?,?,?,?,?,?,?)",
                       (key, body["client"], None, "credit", value, encode(entry), entry["created"]))
            self.store.audit(db, "billing_credit_recorded", {"entry": key, "client": body["client"]})
            return entry

    def snapshot(self, principal=None):
        with self.store.db() as db:
            settings = self.settings(db)
            cached = db.execute("SELECT data FROM billing_source_state WHERE id=1").fetchone()
            account = json.loads(cached["data"]) if cached else None
            pending = [json.loads(r["data"]) for r in db.execute("SELECT data FROM billing_waiting")]
            entries = [json.loads(r["data"]) for r in db.execute("SELECT data FROM billing_entries ORDER BY created DESC,id")]
            if principal is not None:
                entries = [entry for entry in entries if entry["client"] == principal]
                pending = [entry for entry in pending if entry["client"] == principal]
            clients = [{"id": c["id"], "label": c["label"], "balance_usd": "0",
                        "credits_usd": "0", "charges_usd": "0"}
                       for c in db.execute("SELECT id,label FROM clients")
                       if principal is None or c["id"] == principal]
        for client in clients:
            with localcontext() as context:
                context.prec = 64
                credits = sum((Decimal(e["amount_usd"]) for e in entries
                               if e["client"] == client["id"] and e["kind"] == "credit"), Decimal(0))
                charges = sum((Decimal(e["amount_usd"]) for e in entries
                               if e["client"] == client["id"] and e["kind"] == "debit"), Decimal(0))
                client.update(balance_usd=money(credits-charges), credits_usd=money(credits), charges_usd=money(charges))
        view = {"demo": self.demo, "currency": "USD", "settings": settings,
                "clients": clients, "entries": entries[:200], "pending": pending,
                "source": {"configured": self.source is not None, "account": account,
                           "last_checked": account.get("observed_at") if account else None, "error": None},
                "notice": "內部美元帳本只反映已入帳金額；待核項目另列。供應商實際費用、路由預算與客戶售價分別保存。"}
        if principal is not None:
            del view["settings"], view["source"]
            fields = {"id", "kind", "client", "run_id", "amount_usd", "model", "input_tokens",
                      "output_tokens", "created", "note", "pricing_revision", "basis", "demo"}
            view["entries"] = [{k: v for k, v in entry.items() if k in fields} for entry in view["entries"]]
        return view

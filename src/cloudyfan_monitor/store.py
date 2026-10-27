"""Separate durable control state; never edits a core receipt or budget database."""
from contextlib import contextmanager
from copy import deepcopy
from decimal import Decimal
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import time

from .observation_fields import identifier as observation_identifier
from cloudyfan_jev.task_features import (feature_digest, original_digest, public_features,
                                        unavailable, validate_adoption, feature_computation, feature_budget_available)

MODES = {"cloudyfan": "CloudyFan MRC", "fixed_high": "全大模型優先",
         "low_first": "全便宜模型優先", "open_source_first": "全開源模型優先"}
TIERS = ("open_source", "low", "mid", "high")
RETAINED_FIELD = '_retained_public_metadata'
RETAINED_META_FIELDS = ('model', 'provider', 'cost_source', 'elapsed_ms', 'generation_id',
                        'billing_origin', 'open_source', 'request_kind', 'tokens')


def retained_metadata(value, binding):
    """Validate bounded, task-owned history; never accept a whole raw receipt."""
    if (not isinstance(value, dict) or set(value) != {'schema_version', 'binding_sha256',
            'observed_at', 'history_binding', 'events'}
            or value['schema_version'] != 'monitor-retained-public-metadata-v1'
            or value['binding_sha256'] != digest(binding) or len(encode(value).encode()) > 65536
            or type(value['observed_at']) not in (int, float)
            or not math.isfinite(value['observed_at']) or value['observed_at'] < binding['created']):
        raise ValueError('Invalid retained public metadata')
    history = value['history_binding']
    expected = {'pool_version': binding['pool'], 'category': binding['category'], 'mode': binding['mode'],
                'contract_id': binding['contract'], 'bound_at': binding['created']}
    if (not isinstance(history, dict) or set(history) != {*expected, 'contract_version', 'routing_version'}
            or any(history[key] != (observation_identifier(item) if key != 'bound_at' else item)
                   for key, item in expected.items())
            or any(history[key] is not None and observation_identifier(history[key]) != history[key]
                   for key in ('contract_version', 'routing_version'))):
        raise ValueError('Invalid retained binding')
    if not isinstance(value['events'], list) or len(value['events']) > 64:
        raise ValueError('Retained event limit exceeded')
    seen = set()
    for row in value['events']:
        if not isinstance(row, dict) or set(row) != {'account', 'id', 'run', 'metadata'}:
            raise ValueError('Invalid retained event')
        if any(not isinstance(row[key], str) or observation_identifier(row[key]) != row[key]
               for key in ('account', 'id', 'run')):
            raise ValueError('Invalid retained event identity')
        key = row['account'], row['id'], row['run']
        if key in seen:
            raise ValueError('Duplicate retained event')
        seen.add(key)
        metadata = row['metadata']
        if not isinstance(metadata, dict) or set(metadata) != set(RETAINED_META_FIELDS):
            raise ValueError('Invalid retained event fields')
        for key in ('model', 'provider', 'cost_source', 'generation_id'):
            if metadata[key] is not None and observation_identifier(metadata[key]) != metadata[key]:
                raise ValueError('Invalid retained event value')
        elapsed = metadata['elapsed_ms']
        if (elapsed is not None and (type(elapsed) not in (int, float)
                or not math.isfinite(elapsed) or elapsed < 0)):
            raise ValueError('Invalid retained elapsed time')
        if (metadata['billing_origin'] not in (None, 'local', 'openrouter')
                or metadata['request_kind'] not in ('classification', 'generation')
                or type(metadata['open_source']) is not bool):
            raise ValueError('Invalid retained request metadata')
        tokens = metadata['tokens']
        if (not isinstance(tokens, dict) or set(tokens) - {'input_tokens', 'output_tokens',
                'prompt_tokens', 'completion_tokens', 'total_tokens'}
                or any(type(item) is not int or not 0 <= item <= 10**12 for item in tokens.values())):
            raise ValueError('Invalid retained tokens')
    return value


def public_task(value):
    return {key: item for key, item in value.items() if key != RETAINED_FIELD}


class MonitorError(Exception):
    def __init__(self, code, status=422):
        self.code, self.status = code, status
        super().__init__(code)


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False,
                      separators=(",", ":"))


def digest(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,95}", value):
        raise MonitorError("invalid_identifier")
    return value


def percentile(values, p):
    values = sorted(v for v in values if v is not None)
    return values[max(0, math.ceil(len(values) * p) - 1)] if values else None


def stats(tasks):
    n = len(tasks)
    complete = [t for t in tasks if (t.get("completed_status") or t["status"])
                in ("passed", "failed", "unverifiable")]
    passed = sum(t.get("final_pass") is True for t in tasks)
    first = sum(t.get("first_pass") is True for t in tasks)
    latency = [t.get("wall_seconds") for t in complete]
    # Proportions always retain unknown/in-progress jobs in the denominator.
    known = sum((Decimal(t["known_api_usd"]) for t in tasks if t.get("known_api_usd") is not None), Decimal(0))
    fully_accounted = all(t.get("complete_api_usd") is not None for t in tasks)
    return {"tasks": n, "completed": len(complete), "passed": passed, "first_passed": first,
            "known_api_usd": str(known), "complete_api_usd": str(known) if fully_accounted else None,
            "first_rate": first / n if n else None, "final_rate": passed / n if n else None,
            "failed": sum((t.get("completed_status") or t["status"]) == "failed" for t in tasks),
            "unknown": sum((t.get("completed_status") or t["status"])
                           in ("unverifiable", "engineering_interrupted", "accounting_interrupted") for t in tasks),
            "expired": sum(t["status"] == "expired" for t in tasks),
            "in_progress": sum(t["status"] in ("bound", "ready", "running", "queued") for t in tasks),
            "escalated": sum(t.get("generation_attempts", 0) > 1 for t in tasks),
            "jev_once": sum(t.get("classifier_calls") == 1 for t in tasks),
            "jev_twice": sum(t.get("classifier_calls") == 2 for t in tasks),
            "latency_p50": percentile(latency, .5), "latency_p95": percentile(latency, .95),
            "latency_samples": sum(v is not None for v in latency)}


class Store:
    def __init__(self, path, *, clock=time.time):
        self.path, self.clock = Path(path), clock
        if self.path.is_symlink() or any(p.is_symlink() for p in self.path.parents):
            raise ValueError("Private monitor path must not use symlinks")
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS control (id INTEGER PRIMARY KEY, revision INTEGER,
                    mode TEXT, routing_pool TEXT, catalog_pool TEXT);
                INSERT OR IGNORE INTO control VALUES (1,0,'cloudyfan',NULL,NULL);
                CREATE TABLE IF NOT EXISTS clients (id TEXT PRIMARY KEY, label TEXT, token_hash TEXT UNIQUE,
                    mode TEXT, enabled INTEGER, last_seen REAL);
                CREATE TABLE IF NOT EXISTS bindings (client TEXT, run TEXT, fingerprint TEXT, pool TEXT,
                    mode TEXT, category TEXT, contract TEXT, created REAL, PRIMARY KEY(client,run));
                CREATE TABLE IF NOT EXISTS tasks (client TEXT, run TEXT, data TEXT, PRIMARY KEY(client,run));
                CREATE TABLE IF NOT EXISTS pools (id TEXT PRIMARY KEY, data TEXT, created REAL);
                CREATE TABLE IF NOT EXISTS audit (id INTEGER PRIMARY KEY, at REAL, action TEXT, data TEXT);
                CREATE TABLE IF NOT EXISTS version_uses (pool TEXT, contract TEXT, created REAL,
                    last_used REAL, PRIMARY KEY(pool,contract));
            """)
        os.chmod(self.path, 0o600)
        try:
            with self._feature_db(write=True) as db:
                db.execute('CREATE TABLE IF NOT EXISTS creation_feature_adoptions '
                    '(client TEXT, run TEXT, adoption TEXT NOT NULL, adoption_sha TEXT NOT NULL, '
                    'record_sha TEXT, request_sha TEXT, registered_sha TEXT, PRIMARY KEY(client,run))')
        except Exception:
            pass  # An unavailable auxiliary store cannot reject original tasks.

    @contextmanager
    def _feature_db(self, *, write=False):
        """One nonwaiting auxiliary transaction; never the original ten-second db()."""
        path = self.path.absolute()
        info = path.lstat()
        if path != path.resolve() or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError('Unsafe feature binding store')
        mode = 'rw' if write else 'ro'
        db = sqlite3.connect(path.as_uri() + '?mode=' + mode, uri=True, timeout=0, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            if not write:
                db.execute('PRAGMA query_only=ON')
            db.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def audit(self, db, action, data):
        actor = "client:" + str(data["client"]) if action == "task_bound" else "operator"
        db.execute("INSERT INTO audit(at,action,data) VALUES(?,?,?)",
                   (self.clock(), action, encode({**data, "actor": actor})))

    def register_principal(self, client_id, label):
        """Register public identity metadata; credentials remain in IdentityStore."""
        identifier(client_id)
        if not isinstance(label, str) or not 1 <= len(label) <= 80:
            raise ValueError("Explicit client label required")
        with self.db() as db:
            old = db.execute("SELECT id FROM clients WHERE id=?", (client_id,)).fetchone()
            if old is None:
                db.execute("INSERT INTO clients VALUES(?,?,NULL,NULL,1,NULL)", (client_id, label))
                self.audit(db, "client_registered", {"client": client_id, "credential_owner": "identity_store"})

    def touch_client(self, principal):
        with self.db() as db:
            row = db.execute("SELECT id FROM clients WHERE id=? AND enabled=1", (principal,)).fetchone()
            if row is None:
                raise MonitorError("unauthorized", 401)
            db.execute("UPDATE clients SET last_seen=? WHERE id=?", (self.clock(), principal))
        return principal

    def register_client(self, client_id, label, token):
        identifier(client_id)
        if not isinstance(label, str) or not 1 <= len(label) <= 80 or len(token) < 24:
            raise ValueError("Explicit label and strong client token required")
        with self.db() as db:
            old = db.execute("SELECT * FROM clients WHERE id=?", (client_id,)).fetchone()
            if old:
                if old["token_hash"] != digest(token):
                    raise MonitorError("client_token_changed", 409)
                return
            db.execute("INSERT INTO clients VALUES(?,?,?,NULL,1,NULL)", (client_id, label, digest(token)))
            self.audit(db, "client_registered", {"client": client_id})

    def authenticate(self, token):
        with self.db() as db:
            row = db.execute("SELECT id FROM clients WHERE token_hash=? AND enabled=1", (digest(token),)).fetchone()
            if not row:
                raise MonitorError("unauthorized", 401)
            db.execute("UPDATE clients SET last_seen=? WHERE id=?", (self.clock(), row["id"]))
            return row["id"]

    def control(self):
        with self.db() as db:
            return dict(db.execute("SELECT * FROM control").fetchone())

    def change(self, *, revision, mode=None, client=None, catalog=None, routing=None, inherit=False):
        if type(revision) is not int:
            raise MonitorError("revision_required")
        if mode is not None and mode not in MODES:
            raise MonitorError("unsupported_mode")
        with self.db() as db:
            old = db.execute("SELECT * FROM control").fetchone()
            if revision != old["revision"]:
                raise MonitorError("configuration_changed_reload", 409)
            if client is not None:
                if db.execute("SELECT id FROM clients WHERE id=?", (client,)).fetchone() is None:
                    raise MonitorError("client_not_found", 404)
                db.execute("UPDATE clients SET mode=? WHERE id=?", (None if inherit else mode, client))
            elif mode is not None:
                db.execute("UPDATE control SET mode=?", (mode,))
            for column, value in (("catalog_pool", catalog), ("routing_pool", routing)):
                if value is not None:
                    if db.execute("SELECT id FROM pools WHERE id=?", (value,)).fetchone() is None:
                        raise MonitorError("pool_not_found", 404)
                    db.execute(f"UPDATE control SET {column}=?", (value,))
            db.execute("UPDATE control SET revision=revision+1")
            self.audit(db, "configuration_changed", {"client": client, "mode": mode, "inherit": inherit,
                       "catalog": catalog, "routing": routing, "previous_revision": revision})
            return dict(db.execute("SELECT * FROM control").fetchone())

    def save_pool(self, view):
        from cloudyfan_jev.scores.categories import CATEGORY_SPECS
        view = deepcopy(view)
        if set(view.get("categories", {})) != set(CATEGORY_SPECS):
            raise MonitorError("expected_23_categories")
        for category in view["categories"].values():
            if set(category["pools"]) != set(TIERS):
                raise MonitorError("expected_four_pools")
            if any(len(v) != 3 for v in category["pools"].values()):
                raise MonitorError("expected_primary_and_two_backup_positions")
        key = digest(view)
        with self.db() as db:
            db.execute("INSERT OR IGNORE INTO pools VALUES(?,?,?)", (key, encode(view), self.clock()))
            self.audit(db, "pool_staged", {"pool": key, "source": view.get("source_kind")})
        return key

    def pool(self, key):
        with self.db() as db:
            row = db.execute("SELECT data FROM pools WHERE id=?", (key,)).fetchone()
            if row is None:
                raise MonitorError("pool_not_found", 404)
            data = json.loads(row["data"])
            if digest(data) != key:
                raise MonitorError("pool_integrity_failed", 409)
            return {"id": key, **data}

    def bind(self, client, payload, registry, *, expected_contract=None,
             feature_policy=None, feature_contracts=None, with_creation=False):
        if not isinstance(payload, dict) or set(payload) != {"run_id", "category", "prompt"}:
            raise MonitorError("unsupported_task_fields")
        if not isinstance(payload["run_id"], str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", payload["run_id"]):
            raise MonitorError("invalid_run_id")
        if not isinstance(payload["prompt"], str) or not payload["prompt"].strip() or len(payload["prompt"].encode()) > 32768:
            raise MonitorError("invalid_prompt")
        from cloudyfan_jev.scores.categories import resolve_category
        try:
            category = resolve_category(payload["category"])
        except ValueError:
            raise MonitorError("unknown_category") from None
        fingerprint = digest({**payload, "category": category})
        created = False
        with self.db() as db:
            prior = db.execute("SELECT * FROM bindings WHERE client=? AND run=?", (client, payload["run_id"])).fetchone()
            if prior:
                if expected_contract is not None and prior["contract"] != expected_contract:
                    raise MonitorError("ollama_contract_changed", 409)
                if prior["fingerprint"] != fingerprint:
                    raise MonitorError("frozen_task_changed", 409)
                row = dict(prior)
            else:
                config = db.execute("SELECT * FROM control").fetchone()
                client_row = db.execute("SELECT * FROM clients WHERE id=? AND enabled=1", (client,)).fetchone()
                if client_row is None:
                    raise MonitorError("unauthorized", 401)
                mode = client_row["mode"] or config["mode"]
                pool = config["routing_pool"]
                contract = registry.get((pool, category, mode))
                if contract is None:
                    raise MonitorError("routing_configuration_unavailable", 409)
                if expected_contract is not None and contract != expected_contract:
                    raise MonitorError("ollama_contract_changed", 409)
                row = {"client": client, "run": payload["run_id"], "fingerprint": fingerprint,
                       "pool": pool, "mode": mode, "category": category, "contract": contract, "created": self.clock()}
                db.execute("INSERT INTO bindings VALUES(:client,:run,:fingerprint,:pool,:mode,:category,:contract,:created)", row)
                from .billing import freeze_policy
                freeze_policy(db, row)
                db.execute("INSERT INTO version_uses VALUES(?,?,?,?) ON CONFLICT(pool,contract) "
                           "DO UPDATE SET last_used=MAX(last_used,excluded.last_used)",
                           (pool, contract, row["created"], row["created"]))
                self.audit(db, "task_bound", {k: v for k, v in row.items() if k != "fingerprint"})
                self._adopt_features(db, row, payload, feature_policy, feature_contracts)
                created = True
        # No producer qualification escapes a failed original binding commit.
        return (row, created) if with_creation else row

    def _adopt_features(self, db, binding, payload, policy, contracts):
        if policy is None:
            return
        try:
            with feature_computation():
                policy = dict(policy)
                adoption = {'schema_version': 'creation-feature-adoption-v1', 'binding': dict(binding),
                    'policy': policy, 'policy_sha256': feature_digest(policy),
                    'request_sha256': original_digest({'run_id': binding['run'], 'contract_id': binding['contract'],
                                                      'prompt': payload['prompt']}),
                    'registered_sha256': contracts[binding['contract']]}
                validate_adoption(adoption)
                body, adoption_sha = encode(adoption), feature_digest(adoption)
            if not feature_budget_available():
                return
            db.execute('SAVEPOINT creation_features')
            try:
                db.execute('INSERT INTO creation_feature_adoptions '
                    '(client,run,adoption,adoption_sha) VALUES(?,?,?,?)',
                    (binding['client'], binding['run'], body, adoption_sha))
            except Exception:
                db.execute('ROLLBACK TO creation_features')
            finally:
                db.execute('RELEASE creation_features')
        except Exception:
            if not db.in_transaction:
                # SQLITE_FULL may roll back the entire original binding, not
                # just its auxiliary savepoint. Never admit that lost binding.
                raise MonitorError('monitor_store_unavailable', 503) from None
            pass  # No adoption for this first owner; replay must not fill it.

    def _feature_snapshot(self, db, client, run):
        binding = db.execute('SELECT * FROM bindings WHERE client=? AND run=?', (client, run)).fetchone()
        row = db.execute('SELECT CASE WHEN length(CAST(adoption AS BLOB))<=16384 THEN adoption END AS adoption, '
            'adoption_sha,record_sha,request_sha,registered_sha FROM creation_feature_adoptions '
            'WHERE client=? AND run=?', (client, run)).fetchone()
        if binding is None or row is None or row['adoption'] is None:
            raise ValueError('Original feature adoption unavailable')
        with feature_computation():
            adoption = validate_adoption(json.loads(row['adoption']))
            if adoption['binding'] != dict(binding) or feature_digest(adoption) != row['adoption_sha']:
                raise ValueError('Original feature binding changed')
            seal = None
            if any(row[key] is not None for key in ('record_sha', 'request_sha', 'registered_sha')):
                if (row['request_sha'] != adoption['request_sha256']
                        or row['registered_sha'] != adoption['registered_sha256']
                        or not isinstance(row['record_sha'], str) or re.fullmatch(r'[a-f0-9]{64}', row['record_sha']) is None):
                    raise ValueError('Original feature seal invalid')
                seal = {'record_sha256': row['record_sha'], 'request_sha256': row['request_sha'],
                        'registered_sha256': row['registered_sha']}
            return {'adoption': adoption, 'adoption_sha256': row['adoption_sha'], 'seal': seal}

    def feature_binding(self, client, run):
        """Read-only host port; a lookup never creates producer ownership."""
        with self._feature_db() as db:
            return self._feature_snapshot(db, client, run)

    def seal_features(self, client, run, adoption_sha, registered_sha, record_sha):
        """Called once by the trusted capture owner; no public seal endpoint."""
        if not isinstance(record_sha, str) or re.fullmatch(r'[a-f0-9]{64}', record_sha) is None:
            raise ValueError('Invalid captured record digest')
        with self._feature_db(write=True) as db:
            snapshot = self._feature_snapshot(db, client, run)
            adoption = snapshot['adoption']
            if (snapshot['adoption_sha256'] != adoption_sha or snapshot['seal'] is not None
                    or adoption['registered_sha256'] != registered_sha):
                raise ValueError('Original capture adoption or empty seal unavailable')
            if not feature_budget_available():
                raise ValueError('Feature compute deadline exceeded before seal')
            changed = db.execute('UPDATE creation_feature_adoptions SET record_sha=?,request_sha=?,registered_sha=? '
                'WHERE client=? AND run=? AND adoption_sha=? '
                'AND record_sha IS NULL AND request_sha IS NULL AND registered_sha IS NULL',
                (record_sha, adoption['request_sha256'], registered_sha, client, run, adoption_sha))
            if changed.rowcount != 1:
                raise ValueError('Original feature seal already consumed')
        return True

    def binding(self, client, run):
        with self.db() as db:
            row = db.execute("SELECT * FROM bindings WHERE client=? AND run=?", (client, run)).fetchone()
            if row is None:
                raise MonitorError("task_not_found", 404)
            return dict(row)

    def existing_binding(self, client, run):
        with self.db() as db:
            row = db.execute("SELECT * FROM bindings WHERE client=? AND run=?", (client, run)).fetchone()
            return dict(row) if row is not None else None

    def record(self, binding, summary):
        # Only allow explicit public counters/identities. No prompt, answer, diagnosis prose or raw receipts.
        fields = ("status", "first_pass", "final_pass", "generation_attempts", "classifier_calls",
                  "wall_seconds", "known_api_usd", "complete_api_usd", "unknown_events", "path", "error_code",
                  "completed_status", "access_expired", "observation", "trace", "ingress",
                  "queue", "queue_state", "reason", "queue_seconds", "end_to_end_seconds",
                  "cancellation_requested", "provider_cancellation_confirmed", "task_features")
        if 'task_features' in summary:
            summary = dict(summary)
            try:
                summary['task_features'] = public_features(summary['task_features'])
            except Exception:
                summary['task_features'] = unavailable()
        with self.db() as db:
            previous = db.execute("SELECT data FROM tasks WHERE client=? AND run=?",
                                  (binding["client"], binding["run"])).fetchone()
            data = json.loads(previous["data"]) if previous else {key: None for key in fields}
            data.update({key: summary[key] for key in fields if key in summary})
            data.update({key: binding[key] for key in ("client", "run", "pool", "mode", "category", "created")})
            data["updated"] = self.clock()
            if data["status"] in ("passed", "failed", "unverifiable"):
                data["completed_status"] = data["status"]
            for key in ("generation_attempts", "classifier_calls"):
                data[key] = data[key] or 0
            db.execute("INSERT INTO tasks VALUES(?,?,?) ON CONFLICT(client,run) DO UPDATE SET data=excluded.data",
                       (binding["client"], binding["run"], encode(data)))

    def observation(self, client, run):
        with self.db() as db:
            row = db.execute("SELECT data FROM tasks WHERE client=? AND run=?", (client, run)).fetchone()
            return public_task(json.loads(row["data"])) if row else None

    def retained_metadata(self, binding):
        try:
            with self._feature_db() as db:
                row = db.execute('SELECT data FROM tasks WHERE client=? AND run=? '
                    'AND length(CAST(data AS BLOB))<=1048576', (binding['client'], binding['run'])).fetchone()
            value = retained_metadata(json.loads(row['data'])[RETAINED_FIELD], binding) if row else None
            return value if value is not None and value['observed_at'] <= self.clock() else None
        except Exception:
            return None

    def retained_pool(self, binding):
        """Bounded nonwaiting configuration read for the auxiliary writer only."""
        with self._feature_db() as db:
            row = db.execute('SELECT data FROM pools WHERE id=? AND length(CAST(data AS BLOB))<=1048576',
                              (binding['pool'],)).fetchone()
        if row is None:
            raise ValueError('Retained pool unavailable')
        data = json.loads(row['data'])
        if digest(data) != binding['pool']:
            raise ValueError('Retained pool identity differs')
        return {'id': binding['pool'], **data}

    def retain_metadata(self, binding, value):
        """One auxiliary attempt after the original public record has committed."""
        try:
            value = retained_metadata(value, binding)
            if value['observed_at'] > self.clock():
                return False
            with self._feature_db(write=True) as db:
                original = db.execute('SELECT * FROM bindings WHERE client=? AND run=?',
                    (binding['client'], binding['run'])).fetchone()
                if original is None or dict(original) != binding:
                    return False
                row = db.execute('SELECT data FROM tasks WHERE client=? AND run=? '
                    'AND length(CAST(data AS BLOB))<=1048576', (binding['client'], binding['run'])).fetchone()
                if row is None:
                    return False
                data = json.loads(row['data'])
                data[RETAINED_FIELD] = value
                db.execute('UPDATE tasks SET data=? WHERE client=? AND run=?',
                    (encode(data), binding['client'], binding['run']))
            return True
        except Exception:
            # SQLITE_FULL can abort this entire auxiliary transaction. The
            # original public summary and previous envelope are already durable.
            return False

    def record_version_use(self, binding):
        with self.db() as db:
            db.execute("INSERT INTO version_uses VALUES(?,?,?,?) ON CONFLICT(pool,contract) "
                       "DO UPDATE SET last_used=MAX(last_used,excluded.last_used)",
                       (binding["pool"], binding["contract"], binding["created"], self.clock()))

    def version_retention_context(self, binding, access_seconds):
        with self.db() as db:
            row = db.execute("SELECT last_used FROM version_uses WHERE pool=? AND contract=?",
                             (binding["pool"], binding["contract"])).fetchone()
            references = [dict(item) for item in db.execute(
                "SELECT client,run,created FROM bindings WHERE pool=? AND contract=?",
                (binding["pool"], binding["contract"]))]
        # Existing databases remain intact; legacy bindings are conservative use evidence.
        latest = max([binding["created"], *[item["created"] for item in references],
                      *([row["last_used"]] if row else [])])
        active = [{"client": item["client"], "run_id": item["run"],
                   "access_expires_at": item["created"] + access_seconds}
                  for item in references if self.clock() < item["created"] + access_seconds]
        return latest, active

    def dashboard(self, client=None):
        with self.db() as db:
            tasks = [public_task(json.loads(r["data"])) for r in db.execute("SELECT data FROM tasks")]
            clients = [dict(r) for r in db.execute("SELECT id,label,mode,enabled,last_seen FROM clients")]
            config = dict(db.execute("SELECT * FROM control").fetchone())
            audit = [dict(r) for r in db.execute("SELECT * FROM audit ORDER BY id DESC LIMIT 100")]
        if client:
            tasks = [t for t in tasks if t["client"] == client]
            clients = [c for c in clients if c["id"] == client]
            audit = []
        for c in clients:
            c["effective_mode"] = c["mode"] or config["mode"]
            c["recently_seen"] = c["last_seen"] is not None and self.clock() - c["last_seen"] < 90
            c["stats"] = stats([t for t in tasks if t["client"] == c["id"]])
        known = sum((Decimal(t["known_api_usd"]) for t in tasks if t.get("known_api_usd") is not None), Decimal(0))
        cost_complete = all(t.get("complete_api_usd") is not None for t in tasks)
        return {"control": config, "clients": clients, "tasks": sorted(tasks, key=lambda t: t["created"], reverse=True),
                "summary": {**stats(tasks), "known_task_api_usd": str(known),
                            "complete_task_api_usd": str(known) if cost_complete else None},
                "by_mode": {mode: stats([t for t in tasks if t["mode"] == mode]) for mode in MODES},
                "audit": [{**a, "data": json.loads(a["data"])} for a in audit]}

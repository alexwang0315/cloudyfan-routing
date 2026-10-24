"""Versioned supplier capacity evidence, separate from dispatch and fee authority.

Configuration is supplied by the controlled host, never a task or browser. Reads
open SQLite read-only; importing evidence cannot qualify a model or grant a slot.
"""
from contextlib import contextmanager
from copy import deepcopy
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
import os
import stat
from pathlib import Path
import re
import sqlite3
import time


DIMENSIONS = {"rpm": "requests", "tpm": "tokens", "concurrency": "requests", "usd": "USD"}
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,95}\Z")


def _identity(value):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError("Bounded opaque capacity reference required")
    return value


def _time(value):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 10**12:
        raise ValueError("Finite capacity time required")
    return value


def _scope(value):
    if (not isinstance(value, dict) or set(value) != {"kind", "ref"}
            or value["kind"] not in {"account", "credential", "endpoint", "shared"}):
        raise ValueError("Exact capacity scope required")
    return {"kind": value["kind"], "ref": _identity(value["ref"])}


def _observation(value, scope):
    required = {"dimension", "status", "value", "unit", "window_seconds", "observed_at",
                "reset_at", "valid_until", "evidence_ref"}
    if not isinstance(value, dict) or not required <= set(value) or set(value) - required - {"scope", "reset_period", "remaining", "usage", "valid_until_basis", "include_byok_in_limit"}:
        raise ValueError("Exact capacity observation required")
    item = deepcopy(value)
    dimension = item["dimension"]
    if dimension not in DIMENSIONS or item["unit"] != DIMENSIONS[dimension]:
        raise ValueError("Capacity dimensions keep their original unit")
    if item["status"] not in {"known", "unknown", "unsupported", "not_applicable"}:
        raise ValueError("Invalid capacity observation status")
    if item["status"] == "known":
        if dimension == "usd":
            raw = item["value"]
            if isinstance(raw, bool) or not isinstance(raw, (str, int)) or len(str(raw)) > 64:
                raise ValueError("Exact nonnegative USD amount required")
            try:
                amount = Decimal(str(raw))
            except InvalidOperation:
                raise ValueError("Exact nonnegative USD amount required") from None
            if not amount.is_finite() or not 0 <= amount <= 10**12 or amount.as_tuple().exponent < -24:
                raise ValueError("Exact nonnegative USD amount required")
            item["value"] = format(amount, "f")
        elif type(item["value"]) is not int or not 0 <= item["value"] <= 10**12:
            raise ValueError("Exact nonnegative capacity quantity required")
    elif item["value"] is not None:
        raise ValueError("Unknown capacity is not a quantity")
    window = item["window_seconds"]
    if window is not None and (type(window) is not int or not 0 < window <= 366 * 86400):
        raise ValueError("Invalid capacity window")
    if dimension in {"rpm", "tpm"} and window is None:
        raise ValueError("A rate requires its original window")
    _time(item["observed_at"])
    for name in ("reset_at", "valid_until"):
        if item[name] is not None:
            _time(item[name])
    if item["valid_until"] is None or item["valid_until"] <= item["observed_at"]:
        raise ValueError("Capacity evidence requires an explicit validity period")
    _identity(item["evidence_ref"])
    if "reset_period" in item and item["reset_period"] not in {None, "daily", "weekly", "monthly"}:
        raise ValueError("Invalid supplier reset period")
    item["scope"] = _scope(item.get("scope", scope))
    item["valid_until_basis"] = item.get("valid_until_basis", "controlled_import")
    if item["valid_until_basis"] not in {"controlled_import", "local_freshness"}:
        raise ValueError("Explicit capacity validity basis required")
    if "include_byok_in_limit" in item and (dimension != "usd"
            or (item["include_byok_in_limit"] is not None and type(item["include_byok_in_limit"]) is not bool)):
        raise ValueError("Exact supplier dollar scope required")
    for name in ("remaining", "usage"):
        if name in item and item[name] is not None:
            if dimension != "usd":
                raise ValueError("USD accounting fields cannot describe token capacity")
            raw = item[name]
            if not isinstance(raw, str) or len(raw) > 64:
                raise ValueError("Exact nonnegative USD amount required")
            try:
                amount = Decimal(raw)
            except InvalidOperation:
                raise ValueError("Exact nonnegative USD amount required") from None
            if not amount.is_finite() or not 0 <= amount <= 10**12 or amount.as_tuple().exponent < -24:
                raise ValueError("Exact nonnegative USD amount required")
            item[name] = format(amount, "f")
    return item


def _configuration(value):
    if (not isinstance(value, dict) or set(value) != {"schema_version", "version", "bindings", "groups"}
            or type(value["schema_version"]) is not int or value["schema_version"] != 1):
        raise ValueError("Capacity configuration v1 required")
    result = deepcopy(value)
    _identity(result["version"])
    if any(not isinstance(result[key], list) or not 1 <= len(result[key]) <= 128 for key in ("bindings", "groups")):
        raise ValueError("Bounded capacity bindings and groups required")
    group_ids = set()
    for group in result["groups"]:
        if (not isinstance(group, dict)
                or set(group) != {"id", "scope", "source", "supplier_limits", "local_limits"}):
            raise ValueError("Exact capacity group required")
        if _identity(group["id"]) in group_ids:
            raise ValueError("Duplicate capacity group")
        group_ids.add(group["id"])
        group["scope"] = _scope(group["scope"])
        source = group["source"]
        if (not isinstance(source, dict) or not {"kind", "version"} <= set(source)
                or set(source) - {"kind", "version", "credential_ref"}
                or source["kind"] not in {"controlled_import", "openrouter_key", "unsupported"}):
            raise ValueError("Controlled capacity source required")
        _identity(source["version"])
        if source["kind"] == "openrouter_key":
            _identity(source.get("credential_ref"))
            if group["scope"] != {"kind": "credential", "ref": source["credential_ref"]}:
                raise ValueError("OpenRouter key limit requires the exact credential scope")
        elif "credential_ref" in source:
            raise ValueError("Unexpected capacity source credential")
        for name in ("supplier_limits", "local_limits"):
            if not isinstance(group[name], list) or len(group[name]) > 32:
                raise ValueError("Bounded capacity observations required")
            group[name] = [_observation(item, group["scope"]) for item in group[name]]
    binding_ids = set()
    for binding in result["bindings"]:
        if not isinstance(binding, dict) or set(binding) != {"id", "provider", "account_ref", "credential_ref", "endpoint_ref", "groups"}:
            raise ValueError("Exact server-owned capacity binding required")
        for key in ("id", "provider", "account_ref", "credential_ref", "endpoint_ref"):
            _identity(binding[key])
        if binding["id"] in binding_ids:
            raise ValueError("Duplicate capacity binding")
        binding_ids.add(binding["id"])
        if (not isinstance(binding["groups"], list) or not binding["groups"]
                or len(binding["groups"]) != len(set(binding["groups"]))
                or not set(binding["groups"]) <= group_ids):
            raise ValueError("Every binding requires its configured shared groups")
        for group_id in binding["groups"]:
            group = next(value for value in result["groups"] if value["id"] == group_id)
            kind = group["scope"]["kind"]
            if kind != "shared" and group["scope"]["ref"] != binding[
                    {"account": "account_ref", "credential": "credential_ref", "endpoint": "endpoint_ref"}[kind]]:
                raise ValueError("Capacity binding does not match its group scope")
            if group["source"]["kind"] == "openrouter_key" and binding["provider"] != "openrouter":
                raise ValueError("Capacity binding provider does not match its key source")
    if len(json.dumps(result)) > 262144:
        raise ValueError("Capacity configuration too large")
    return result


def _limits(observations, scope, now, *, unsupported=False, local=False):
    output = []
    for dimension, unit in DIMENSIONS.items():
        matching = [deepcopy(item) for item in observations if item["dimension"] == dimension]
        if matching:
            item = deepcopy(max(matching, key=lambda value: value["observed_at"]))
            item["reason_codes"] = [] if item["status"] == "known" else ["capacity_" + item["status"]]
            item["observations"] = matching
            reason, status = None, None
            if now is None:
                reason, status = "capacity_clock_invalid", "unverifiable"
            elif any(value["scope"] != scope for value in matching):
                reason, status = "capacity_scope_mismatch", "mismatched"
            elif any(value["observed_at"] > now for value in matching):
                reason, status = "capacity_observation_from_future", "unverifiable"
            else:
                fresh = [value for value in matching if now < value["valid_until"]]
                if fresh:
                    item = deepcopy(max(fresh, key=lambda value: value["observed_at"]))
                    item["reason_codes"] = [] if item["status"] == "known" else ["capacity_" + item["status"]]
                    item["observations"] = matching
                if not fresh:
                    reason, status = "capacity_evidence_expired", "stale"
                elif any(value["reset_at"] is not None and value["reset_at"] < value["observed_at"] for value in fresh):
                    reason, status = "capacity_reset_time_invalid", "unverifiable"
                elif len({json.dumps({key: value.get(key) for key in
                        ("status", "value", "unit", "window_seconds", "reset_period", "remaining")}, sort_keys=True)
                        for value in fresh}) > 1:
                    reason, status = "capacity_source_contradictory", "contradictory"
                elif any(value["dimension"] == "usd" and value["status"] == "known"
                        and value.get("remaining") is not None
                        and Decimal(value["remaining"]) > Decimal(value["value"]) for value in fresh):
                    reason, status = "capacity_source_contradictory", "contradictory"
            if status:
                item["status"], item["value"], item["reason_codes"] = status, None, [reason]
        else:
            status = "unsupported" if unsupported and not local else "unknown"
            item = {"dimension": dimension, "unit": unit, "status": status, "value": None,
                    "window_seconds": None, "observed_at": None, "reset_at": None,
                    "valid_until": None, "evidence_ref": None, "scope": scope,
                    "reason_codes": ["capacity_unsupported" if status == "unsupported" else
                                     "local_limit_not_provided" if local else "supplier_limit_not_provided"],
                    "observations": []}
        output.append(item)
    return output


def _private_path(path, *, create=False):
    path = Path(path).absolute()
    if '..' in path.parts:
        raise ValueError("Unsafe capacity path")
    for parent in reversed(path.parents):
        if create and not parent.exists() and not parent.is_symlink():
            parent.mkdir(mode=0o700)
        if not stat.S_ISDIR(parent.lstat().st_mode):
            raise ValueError("Unsafe capacity directory")
    info = path.parent.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("Capacity directory must be private and owned")
    if create:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(fd)
    for candidate in [path, *[path.with_name(path.name + suffix) for suffix in ('-journal', '-wal', '-shm')]]:
        if candidate != path and not candidate.exists() and not candidate.is_symlink():
            continue
        info = candidate.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_nlink != 1):
            raise ValueError("Capacity database must be private, regular and owned")
    return path


def _window_source_live(connection, pending_target):
    """Optional SQL protocol; missing/changed original obligations fail closed."""
    if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='capacity_window_policies'").fetchone():
        return False
    try:
        required = {}
        for version, payload in connection.execute('SELECT version,payload FROM capacity_window_policies'):
            policy = json.loads(payload)
            sources = policy.get('controlled_source_ids', [])
            if policy.get('controlled_sources') and not sources:
                return True
            if not isinstance(sources, list) or len(sources) > 128:
                return True
            for row in sources:
                if (not isinstance(row, dict) or set(row) != {'id', 'physical', 'window_version',
                        'configuration_version', 'configuration_sha256', 'source_id'}
                        or row['window_version'] != version or policy.get('configuration_sha256') != row['configuration_sha256']
                        or row['id'] in required):
                    return True
                required[row['id']] = row
        exists = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='capacity_window_source_obligations'").fetchone()
        if not exists:
            return bool(required)
        rows = connection.execute('SELECT id,physical,window_version,configuration_version,status,payload FROM capacity_window_source_obligations').fetchall()
        if {row[0] for row in rows} != set(required):
            return True
        pending = []
        for identifier, physical, window_version, configuration_version, status, payload in rows:
            expected = required[identifier]
            if (json.loads(payload) != expected or physical != expected['physical']
                    or window_version != expected['window_version'] or configuration_version != expected['configuration_version']):
                return True
            config = connection.execute('SELECT digest FROM capacity_configurations WHERE version=?', (configuration_version,)).fetchone()
            if config is None or config[0] != expected['configuration_sha256']:
                return True
            if status == 'pending':
                pending.append(expected)
            elif status == 'imported':
                saved = connection.execute('SELECT physical,payload FROM capacity_window_observations WHERE id=?', (identifier,)).fetchone()
                if saved is None or saved[0] != physical:
                    return True
                observation = json.loads(saved[1])
                if any(observation.get(key) != expected[key] for key in (
                        'id', 'physical', 'configuration_version', 'configuration_sha256', 'source_id')) or observation.get('version') != window_version:
                    return True
            else:
                return True
        if not pending:
            return False
        # Only first activation of the exact policy can establish enforcement.
        # It does not waive ordinary live adoptions/allocations/windows below.
        return pending_target is None or any(
            row['window_version'] != pending_target['window_version']
            or row['configuration_version'] != pending_target['configuration_version']
            or row['configuration_sha256'] != pending_target['configuration_sha256']
            or row['physical'] not in pending_target['physical'] for row in pending)
    except (ValueError, KeyError, TypeError, sqlite3.Error):
        return True


def _live_capacity(connection, now, *, pending_target=None):
    if _window_source_live(connection, pending_target):
        return True
    if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='capacity_window_observations'").fetchone():
        if connection.execute("SELECT 1 FROM capacity_window_observations WHERE status='unknown' "
                              "OR (status='wait' AND not_before>?) LIMIT 1", (now,)).fetchone():
            return True
    if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='capacity_adoptions'").fetchone():
        return False
    if connection.execute("SELECT 1 FROM capacity_adoptions WHERE terminal=0 LIMIT 1").fetchone():
        return True
    if connection.execute("SELECT 1 FROM capacity_allocations WHERE state IN ('prepared','sent','uncertain') "
                          "OR (state='complete' AND horizon>?) LIMIT 1", (now,)).fetchone():
        return True
    return False


class CapacityStore:
    """Shared immutable configuration/evidence store; never a fee or slot ledger."""
    def __init__(self, path, *, clock=time.time):
        from .capacity_integrity import CapacityIntegrity
        fresh = not Path(path).exists()
        self.path, self.clock = _private_path(path, create=True), clock
        self.integrity = CapacityIntegrity(self.path, fresh=fresh)
        if self.integrity.status()['affected_groups'] is None:
            return  # Retain failed established storage; the host can expose safe observation.
        with self.connection(write=True) as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS capacity_configurations (
                    version TEXT PRIMARY KEY, digest TEXT NOT NULL, created_at REAL NOT NULL, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS capacity_control (
                    id INTEGER PRIMARY KEY CHECK(id=1), version TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS capacity_source_refreshes (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT, version TEXT NOT NULL,
                    group_id TEXT NOT NULL, status TEXT NOT NULL, payload TEXT NOT NULL, digest TEXT NOT NULL);
            """)

    @contextmanager
    def connection(self, *, write=False, projection=False):
        _private_path(self.path)
        with self.integrity.transaction(write=write, projection=projection) as connection:
            if write:
                connection.execute("PRAGMA journal_mode=DELETE")
            yield connection
        _private_path(self.path)

    def configure(self, configuration):
        """Import trusted host configuration atomically; versions cannot be rewritten."""
        configuration = _configuration(configuration)
        encoded = json.dumps(configuration, sort_keys=True, separators=(",", ":"), allow_nan=False)
        digest = hashlib.sha256(encoded.encode()).hexdigest()
        self._observer_configuration = configuration, digest
        if self.integrity.status()['status'] != 'verified':
            # Reopening the exact frozen host may expose retained evidence.
            # It cannot create/change a policy or certify damaged storage.
            try:
                with self.connection(projection=True) as connection:
                    saved = connection.execute('SELECT digest,payload FROM capacity_configurations WHERE version=?',
                        (configuration['version'],)).fetchone()
                    active = connection.execute('SELECT version FROM capacity_control WHERE id=1').fetchone()
            except (RuntimeError, OSError, ValueError, sqlite3.Error):
                # Runtime host configuration is not historical capacity proof.
                # Keep the observer available; require() still denies every POST.
                return digest
            if saved is None or saved != (digest, encoded) or active != (configuration['version'],):
                self.integrity.require()
            return digest
        with self.connection(write=True) as connection:
            connection.execute("BEGIN IMMEDIATE")
            active = connection.execute("SELECT version FROM capacity_control WHERE id=1").fetchone()
            if active and active[0] != configuration['version'] and _live_capacity(connection, _time(self.clock())):
                raise ValueError("Live capacity prevents configuration change")
            existing = connection.execute("SELECT digest FROM capacity_configurations WHERE version=?",
                                          (configuration["version"],)).fetchone()
            if existing and existing[0] != digest:
                raise ValueError("A capacity version cannot change its evidence or bindings")
            connection.execute("INSERT OR IGNORE INTO capacity_configurations VALUES(?,?,?,?)",
                               (configuration["version"], digest, _time(self.clock()), encoded))
            connection.execute("INSERT OR REPLACE INTO capacity_control VALUES(1,?)", (configuration["version"],))
        return digest

    def record_refresh(self, version, group_id, *, attempted_at, status, observations=(),
                       error_code=None, request_count=0):
        """Append a safe result to the captured configuration, never rewrite history."""
        _identity(version)
        _identity(group_id)
        if status not in {"available", "unavailable"} or type(request_count) is not int or not 0 <= request_count <= 1:
            raise ValueError("Bounded capacity source result required")
        if error_code is not None and (not isinstance(error_code, str)
                or not re.fullmatch(r"(?:capacity|openrouter_accounting)_[a-z_]{1,64}", error_code)):
            raise ValueError("Safe capacity error code required")
        if (status == "available" and error_code is not None) or (status == "unavailable" and not error_code):
            raise ValueError("Exact capacity source outcome required")
        if not isinstance(observations, (tuple, list)) or len(observations) > 32:
            raise ValueError("Bounded capacity observations required")
        with self.connection(write=True) as connection:
            connection.execute("BEGIN IMMEDIATE")
            saved = connection.execute("SELECT payload,digest FROM capacity_configurations WHERE version=?",
                                       (version,)).fetchone()
            if not saved or hashlib.sha256(saved[0].encode()).hexdigest() != saved[1]:
                raise ValueError("Saved capacity configuration unavailable")
            configuration = _configuration(json.loads(saved[0]))
            groups = [group for group in configuration["groups"] if group["id"] == group_id]
            if len(groups) != 1:
                raise ValueError("Capacity source group does not belong to the captured version")
            group = groups[0]
            items = [_observation(item, group["scope"]) for item in observations]
            if status == "unavailable" and items:
                raise ValueError("An unavailable source cannot introduce evidence")
            payload = {"attempted_at": _time(attempted_at), "status": status,
                       "error_code": error_code, "request_count": request_count, "observations": items}
            encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
            connection.execute("INSERT INTO capacity_source_refreshes(version,group_id,status,payload,digest) VALUES(?,?,?,?,?)",
                (version, group_id, status, encoded, hashlib.sha256(encoded.encode()).hexdigest()))

    @staticmethod
    def _refresh(row):
        sequence, encoded, digest = row
        if hashlib.sha256(encoded.encode()).hexdigest() != digest:
            raise ValueError("Saved capacity source integrity failed")
        value = json.loads(encoded)
        return {"sequence": sequence, **value}

    def snapshot(self, version=None):
        integrity = self.integrity.status()
        if integrity['status'] != 'verified' and not integrity['retained_verified_metadata']:
            return self._unavailable_snapshot(integrity)
        try:
            return self._snapshot(version)
        except (RuntimeError, OSError, ValueError, sqlite3.Error):
            integrity = self.integrity.status()
            if integrity['status'] == 'verified':
                raise
            return self._unavailable_snapshot(integrity)

    def _unavailable_snapshot(self, integrity):
        configuration, digest = getattr(self, '_observer_configuration', (None, None))
        return {'schema_version': 'capacity-sources-v1', 'configured': configuration is not None,
            'configuration_version': configuration['version'] if configuration else None,
            'configuration_sha256': digest, 'execution_enabled': False,
            'bindings': configuration['bindings'] if configuration else [], 'groups': [],
            'integrity': integrity, 'reason_codes': [integrity['reason_code']],
            'observation_status': 'unavailable', 'retained_verified_metadata': False}

    def _snapshot(self, version=None):
        """Read only saved values; no network, accounting, health or dispatch work."""
        with self.connection(projection=True) as connection:
            saved = connection.execute("SELECT payload,digest FROM capacity_configurations " +
                ("WHERE version=(SELECT version FROM capacity_control WHERE id=1)" if version is None else "WHERE version=?"),
                () if version is None else (_identity(version),)).fetchone()
            refreshes = {}
            if saved is not None:
                version = json.loads(saved[0])["version"]
                for group in json.loads(saved[0])["groups"]:
                    history = connection.execute("SELECT sequence,payload,digest FROM capacity_source_refreshes "
                        "WHERE version=? AND group_id=? ORDER BY sequence DESC LIMIT 20", (version, group["id"])).fetchall()
                    successful = connection.execute("SELECT sequence,payload,digest FROM capacity_source_refreshes "
                        "WHERE version=? AND group_id=? AND status='available' ORDER BY sequence DESC LIMIT 1",
                        (version, group["id"])).fetchone()
                    count = connection.execute("SELECT COUNT(*) FROM capacity_source_refreshes WHERE version=? AND group_id=?",
                                               (version, group["id"])).fetchone()[0]
                    refreshes[group["id"]] = ([self._refresh(row) for row in reversed(history)],
                        self._refresh(successful) if successful else None, count)
        if saved is None:
            return {"schema_version": "capacity-sources-v1", "configured": False,
                    "execution_enabled": False, "configuration_version": None,
                    "bindings": [], "groups": [], "reason_codes": ["capacity_not_configured"]}
        encoded, digest = saved
        if hashlib.sha256(encoded.encode()).hexdigest() != digest:
            raise ValueError("Saved capacity configuration integrity failed")
        configuration = _configuration(json.loads(encoded))
        try:
            now = _time(self.clock())
        except ValueError:
            now = None
        groups = []
        integrity = self.integrity.status()
        for group in configuration["groups"]:
            history, successful, count = refreshes[group["id"]]
            supplier = successful["observations"] if successful else group["supplier_limits"]
            supplier = [_observation(item, group["scope"]) for item in supplier]
            last = history[-1] if history else None
            summaries = [{key: item[key] for key in ("sequence", "attempted_at", "status", "error_code", "request_count")}
                         for item in history]
            groups.append({"id": group["id"], "scope": group["scope"], "source": group["source"],
                "supplier_limits": _limits(supplier, group["scope"], now, unsupported=group["source"]["kind"] == "unsupported"),
                "local_limits": _limits(group["local_limits"], group["scope"], now, local=True),
                "last_refresh": summaries[-1] if summaries else None, "refresh_history": summaries,
                "refresh_history_count": count, "refresh_history_complete": count == len(summaries),
                "retained_after_refresh_failure": bool(last and last["status"] == "unavailable" and successful),
                **({'observation_status': 'unavailable', 'current_failure': integrity['reason_code'],
                    'retained_verified_metadata': True} if integrity['status'] != 'verified' and (
                        integrity['affected_groups'] is None or hashlib.sha256(json.dumps(group['scope'],
                            sort_keys=True, separators=(',', ':')).encode()).hexdigest() in integrity['affected_groups']) else {})})
        return {"schema_version": "capacity-sources-v1", "configured": True,
                "configuration_version": configuration["version"], "configuration_sha256": digest,
                "execution_enabled": False, "bindings": configuration["bindings"],
                "groups": groups, 'integrity': integrity,
                "reason_codes": ([] if now is not None else ["capacity_clock_invalid"])
                    + ([integrity['reason_code']] if integrity['status'] != 'verified' else [])}

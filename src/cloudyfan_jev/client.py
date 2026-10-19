"""Durable OpenRouter transport. No automatic paid retry or fee estimate.

The caller owns authorization, profile admission and budget reservation. This
client additionally stops NEW paid requests while any local call is unresolved.
"""
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import re
from time import perf_counter

from .receipt_store import ReceiptStore
# Keep the existing storage helper imports available to current package callers.
from .receipt_store import _encoded as _encoded, _read as _read
from .receipt_store import _fsync_dir as _fsync_dir, _immutable as _immutable

CHAT = "https://openrouter.ai/api/v1/chat/completions"
DECISIONS = "https://openrouter.ai/api/alpha/decisions"
GENERATION = "https://openrouter.ai/api/v1/generation"
_GID = re.compile(r"gen-[A-Za-z0-9_-]{1,160}\Z")


class UnresolvedCallsError(RuntimeError):
    pass


def _now():
    return datetime.now(timezone.utc).isoformat()


def _money(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or amount < 0:
            return None
        return str(amount)
    except (InvalidOperation, ValueError, TypeError):
        return None


def _strict_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=pairs, parse_float=str)


def not_dispatched(record):
    """Trusted transport evidence that the durable intent never reached POST."""
    return (record.get('not_dispatched') is True and record.get('http_status') is None
            and record.get('raw') is None and record.get('error') == 'enforcement_denied_before_post')


def usage(record):
    """Use API aggregate cost once; generation receipts supersede, never add."""
    if record.get('execution_kind') == 'local':
        from .local import local_usage
        return local_usage(record)
    result = {"generation_id": None, "model": None, "provider": None, "usage": {},
              "cost_usd": None, "cost_source": "unknown", "elapsed_ms": record.get("elapsed_ms")}
    try:
        data = _strict_json(record.get("raw"))
        if not isinstance(data, dict):
            data = {}
    except (ValueError, TypeError, RecursionError):
        data = {}
    gid = data.get("id") or record.get("headers", {}).get("x-generation-id")
    if isinstance(gid, str) and _GID.fullmatch(gid):
        result["generation_id"] = gid
    result.update(model=data.get("model"), provider=data.get("provider"))
    api_usage = data.get("usage", {})
    if isinstance(api_usage, dict):
        result["usage"] = api_usage
        result["cost_usd"] = _money(api_usage.get("cost"))
        if result["cost_usd"] is not None:
            result["cost_source"] = "response_usage_cost"
    reconciliation = record.get("reconciliation", {})
    if (isinstance(reconciliation, dict) and reconciliation.get("verified") is True
            and reconciliation.get("generation_id") == result["generation_id"]):
        cost = _money(reconciliation.get("total_cost"))
        if cost is not None:
            result.update(cost_usd=cost, cost_source="generation_total_cost")
            result["model"] = reconciliation.get("model") or result["model"]
            result["provider"] = reconciliation.get("provider_name") or result["provider"]
    if not_dispatched(record):
        result.update(cost_usd="0", cost_source="not_dispatched")
    result["cost_unknown"] = result["cost_usd"] is None
    return result


def rate_limit_rejection(record, requested_model, accepted_models, provider_name):
    """Recognize an explicit no-answer 429, never infer its billing cost.

    A rejection may omit actual model identity. Match the frozen request and
    reported provider without inventing an actual generated model. The caller
    must still require resolved fees, a unique generation ID and durable intent.
    """
    if record.get('http_status') != 429 or requested_model not in accepted_models:
        return False
    try:
        data = _strict_json(record.get('raw'))
        if not isinstance(data, dict) or data.get('choices') not in (None, []):
            return False
        error = data.get('error')
        if not isinstance(error, dict) or type(error.get('code')) is not int or error['code'] != 429:
            return False
        metadata = error.get('metadata', {})
        route = data.get('openrouter_metadata', {})
        if not isinstance(metadata, dict) or not isinstance(route, dict):
            return False
        info = usage(record)
        providers = [v for v in (info['provider'], metadata.get('provider_name')) if v is not None]
        return (bool(providers) and all(v == provider_name for v in providers)
                and (info['model'] is None or info['model'] in accepted_models)
                and (route.get('requested') is None or route['requested'] in accepted_models))
    except (ValueError, TypeError, RecursionError):
        return False


class Client(ReceiptStore):
    def __init__(self, http, key, root: Path):
        if not isinstance(key, str) or not key:
            raise ValueError("A nonempty credential must be supplied without logging it")
        super().__init__(root)
        self.http, self.key = http, key

    def _load(self, directory):
        record = super()._load(directory)
        # Successfully verified reconciliation is immutable. Failed GET attempts
        # remain separate receipts and cannot resolve unknown paid costs.
        receipt = directory / "reconciled.json"
        if receipt.exists():
            record["reconciliation"] = _read(receipt)
        if (directory / "duplicate-generation.json").exists():
            record["duplicate_generation_id"] = True
        return record

    def _assert_resolved(self):
        seen = set()
        for directory in sorted((self.root / "calls").iterdir()):
            if not directory.is_dir():
                continue
            if not (directory / "intent.json").exists() or not (directory / "response.json").exists():
                raise UnresolvedCallsError("Incomplete call evidence blocks a new paid request: " + directory.name)
            try:
                _read(directory / "intent.json")
                record = self._load(directory)
                info = usage(record)
            except (ValueError, TypeError, OSError):
                raise UnresolvedCallsError("Unreadable call evidence blocks a new paid request") from None
            if not_dispatched(record):
                continue
            gid = info["generation_id"]
            if info["cost_unknown"] or not gid:
                raise UnresolvedCallsError("Unknown cost or generation identity blocks a new paid request: " + directory.name)
            if gid in seen or record.get("duplicate_generation_id"):
                raise UnresolvedCallsError("Duplicate generation identity blocks a new paid request")
            seen.add(gid)

    def call(self, cid, url, payload, *, before_post=None):
        if url not in (CHAT, DECISIONS):
            raise ValueError("Unexpected paid endpoint")
        directory = self._directory(cid)
        request_hash = hashlib.sha256(_encoded({"url": url, "payload": payload})).hexdigest()
        with self._lock():
            intent = directory / "intent.json"
            if intent.exists():
                if _read(intent)["request_sha256"] != request_hash:
                    raise ValueError("Resume payload or endpoint changed")
                if (directory / "response.json").exists():
                    return self._load(directory)
                raise UnresolvedCallsError("Unresolved existing intent; paid request will not be resent")
            self._assert_resolved()
            directory.mkdir(exist_ok=True, mode=0o700)
            _fsync_dir(directory.parent)
            _immutable(intent, {"call_id": cid, "url": url, "payload": payload,
                                "request_sha256": request_hash, "started_at": _now()})
            started = perf_counter()
            if before_post is not None:
                try:
                    admitted = before_post() is True
                except Exception:
                    admitted = False
                if not admitted:
                    record = {"call_id": cid, "request_sha256": request_hash, "http_status": None,
                              "raw": None, "not_dispatched": True, "error": "enforcement_denied_before_post",
                              "elapsed_ms": (perf_counter() - started) * 1000, "finished_at": _now()}
                    _immutable(directory / "response.json", record)
                    return record
            try:
                response = self.http.post(url, json=payload,
                    headers={"Authorization": "Bearer " + self.key, "X-OpenRouter-Metadata": "enabled"},
                    timeout=180, follow_redirects=False)
                record = {"call_id": cid, "request_sha256": request_hash,
                          "http_status": response.status_code, "elapsed_ms": (perf_counter() - started) * 1000,
                          "raw": response.text.replace(self.key, "[REDACTED]"),
                          "headers": {k.lower(): v for k, v in response.headers.items()
                                      if k.lower() in {"x-generation-id", "x-request-id"} and self.key not in v},
                          "finished_at": _now()}
            except Exception:
                record = {"call_id": cid, "request_sha256": request_hash, "http_status": None,
                          "elapsed_ms": (perf_counter() - started) * 1000, "raw": None,
                          "interrupted": True, "error": "transport_failure", "finished_at": _now()}
            # Durable answer/receipt precedes any downstream interpretation.
            _immutable(directory / "response.json", record)
            gid = usage(record)["generation_id"]
            if gid:
                duplicate = any(usage(self._load(p.parent))["generation_id"] == gid
                                for p in (self.root / "calls").glob("*/response.json") if p.parent != directory)
                if duplicate:
                    _immutable(directory / "duplicate-generation.json", {"generation_id": gid})
                    record["duplicate_generation_id"] = True
            return record

    def reconcile(self, cid):
        """GET existing generation only; returns the original record plus receipt.

        Known failed GETs may be retried explicitly; a successful receipt is
        reused. Neither operation resends a generation/decision request.
        """
        directory = self._directory(cid)
        with self._lock():
            record = self._load(directory)
            if (directory / "reconciled.json").exists():
                return record
            gid = usage(record)["generation_id"]
            if not gid:
                raise UnresolvedCallsError("No generation identity is available for reconciliation")
            attempts = sorted(directory.glob("reconcile-*.json"))
            started = perf_counter()
            try:
                response = self.http.get(GENERATION, params={"id": gid},
                    headers={"Authorization": "Bearer " + self.key}, timeout=30, follow_redirects=False)
                attempt = {"generation_id": gid, "http_status": response.status_code,
                           "elapsed_ms": (perf_counter() - started) * 1000,
                           "raw": response.text.replace(self.key, "[REDACTED]"), "finished_at": _now()}
            except Exception:
                attempt = {"generation_id": gid, "http_status": None, "raw": None,
                           "elapsed_ms": (perf_counter() - started) * 1000,
                           "error": "transport_failure", "finished_at": _now()}
            _immutable(directory / f"reconcile-{len(attempts) + 1:04d}.json", attempt)
            try:
                data = _strict_json(attempt["raw"])["data"]
                cost = _money(data.get("total_cost"))
                if attempt["http_status"] == 200 and data.get("id") == gid and cost is not None:
                    receipt = {"verified": True, "generation_id": gid, "total_cost": cost,
                               "model": data.get("model"), "provider_name": data.get("provider_name"),
                               "original_response_cost_usd": usage(record)["cost_usd"],
                               "source": "generation_total_cost", "finished_at": _now()}
                    _immutable(directory / "reconciled.json", receipt)
                    record["reconciliation"] = receipt
            except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
                pass
            return record

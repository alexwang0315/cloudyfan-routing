"""Operator-only supplier evidence. This module cannot grant dispatch authority."""
import fcntl
import math
import time

from cloudyfan_jev.capacity import CapacityStore

from .openrouter_accounting import AccountingError, OpenRouterAccounting
from .store import MonitorError


class CapacityObserver:
    def __init__(self, store, *, sources=None, max_requests=8, max_seconds=10, source_ttl=300,
                 monotonic=time.monotonic):
        if type(store) is not CapacityStore:
            raise ValueError("Controlled persisted capacity source store required")
        self.store = store
        self.sources = dict(sources or {})
        if any(type(source) is not OpenRouterAccounting for source in self.sources.values()):
            raise ValueError("Existing controlled OpenRouter accounting sources required")
        if (type(max_requests) is not int or not 1 <= max_requests <= 32
                or type(max_seconds) not in (int, float) or not math.isfinite(max_seconds) or not 0 < max_seconds <= 30
                or type(source_ttl) is not int or not 1 <= source_ttl <= 86400):
            raise ValueError("Bounded explicit source refresh required")
        self.max_requests, self.max_seconds, self.source_ttl = max_requests, max_seconds, source_ttl
        self.monotonic = monotonic
        self._fingerprints = {ref: source.account_ref for ref, source in self.sources.items()}

    def snapshot(self):
        return self.store.snapshot()

    def refresh(self):
        with self.store.path.with_suffix(".refresh.lock").open("a+b") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise MonitorError("capacity_source_refresh_busy", 409) from None
            try:
                return self._refresh()
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def _refresh(self):
        view = self.snapshot()
        if not view["configured"]:
            raise MonitorError("capacity_source_refresh_not_configured", 409)
        observed = self.store.clock()
        if (type(observed) not in (int, float) or not math.isfinite(observed)
                or not 0 <= observed <= 10**12 - self.source_ttl):
            raise MonitorError("capacity_clock_invalid", 409)
        previous = [group["last_refresh"]["attempted_at"] for group in view["groups"] if group["last_refresh"]]
        previous += [item["observed_at"] for group in view["groups"]
                     for name in ("supplier_limits", "local_limits") for item in group[name]
                     if item["observed_at"] is not None]
        if previous and observed < max(previous):
            raise MonitorError("capacity_clock_backwards", 409)
        started = self.monotonic()
        if type(started) not in (int, float) or not math.isfinite(started):
            raise MonitorError("capacity_clock_invalid", 409)
        count, available, failed = 0, 0, 0
        for group in view["groups"]:
            if group["source"]["kind"] != "openrouter_key":
                continue
            observed = self.store.clock()
            if type(observed) not in (int, float) or not math.isfinite(observed) or observed < 0:
                raise MonitorError("capacity_clock_invalid", 409)
            ref = group["source"]["credential_ref"]
            source, items, error_code, requests = self.sources.get(ref), [], None, 0
            elapsed = self.monotonic() - started
            remaining = self.max_seconds - elapsed
            if source is None:
                error_code = "capacity_source_not_configured"
            elif count >= self.max_requests or not math.isfinite(remaining) or remaining <= 0 or elapsed < 0:
                error_code = "capacity_refresh_budget_exhausted"
            else:
                count += 1
                requests = 1
                try:
                    value = source.key_capacity(time_budget=remaining)
                    finished = self.monotonic() - started
                    if not math.isfinite(finished) or finished < 0 or finished >= self.max_seconds:
                        raise AccountingError("capacity_refresh_deadline_exceeded", 504)
                    if value["credential_fingerprint"] != self._fingerprints[ref] or value["provider"] != "openrouter":
                        raise AccountingError("openrouter_accounting_identity_mismatch", 409)
                    items = [{"dimension": "usd", "status": "known" if value["limit"] is not None else "unknown",
                        "value": value["limit"], "remaining": value["remaining"], "usage": value["usage"],
                        "include_byok_in_limit": value["include_byok_in_limit"],
                        "unit": "USD", "window_seconds": None, "reset_at": None,
                        "reset_period": value["reset_period"], "observed_at": observed,
                        "valid_until": observed + self.source_ttl, "valid_until_basis": "local_freshness",
                        "evidence_ref": value["evidence_sha256"],
                        "scope": group["scope"]}]
                except AccountingError as error:
                    error_code = error.code
                except Exception:
                    error_code = "capacity_source_unavailable"
            status = "unavailable" if error_code else "available"
            self.store.record_refresh(view["configuration_version"], group["id"], attempted_at=observed,
                status=status, observations=items, error_code=error_code, request_count=requests)
            failed += status == "unavailable"
            available += status == "available"
        return {"status": "partial" if available and failed else "unavailable" if failed else "available",
                "request_count": count, "max_requests": self.max_requests, "max_seconds": self.max_seconds,
                "elapsed_seconds": max(0, self.monotonic() - started), "execution_enabled": False,
                "snapshot": self.snapshot()}

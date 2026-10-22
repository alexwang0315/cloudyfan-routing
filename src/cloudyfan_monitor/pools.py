"""Public-source candidate refresh. Metadata proposals never grant dispatch permission."""
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re
import threading
import time
from urllib.parse import quote

import httpx

from cloudyfan_jev.scores.categories import CATEGORY_SPECS, category_matches
from cloudyfan_jev.scores.discovery import catalog_inventory
from cloudyfan_jev.scores.metrics import normalize_benchmarks, normalize_endpoints

from .store import TIERS, MonitorError

BASE = "https://openrouter.ai/api/v1"
LIMIT = 8 * 1024 * 1024


def empty_view(source, created):
    return {"schema_version": "cloudyfan-monitor-pools-v1", "source_kind": source, "created_at": created,
            "runtime_qualified": False, "categories": {
                c: {"metric": spec["metric"], "unit": spec["unit"], "pools": {
                    tier: [{"status": "missing", "profile": {}, "position": i} for i in range(3)]
                    for tier in TIERS}} for c, spec in CATEGORY_SPECS.items()}}


def from_coverage(document):
    view = empty_view("historical_local_inventory", document["created_at"])
    view["source_sha256"] = hashlib.sha256(json.dumps(document, sort_keys=True).encode()).hexdigest()
    for row in document["named_pool_positions"]["pools"]:
        category = view["categories"][row["category_id"]]
        category["thresholds"] = document["categories"][row["category_id"]]["native_thresholds"]
        category["pools"][row["tier"]] = [{
            "position": i, "status": p["status"], "profile": p.get("profile", {}),
            "quote_usd": p.get("quoted_scenario_usd"), "quote_basis": "2000 input + 1000 output tokens; historical quote",
            "endpoint_p50_ms": p.get("historical_endpoint_p50_ms"), "quality": None,
            "flow_cost_usd": p.get("complete_cost_usd"), "flow_latency_ms": p.get("whole_task_speed_ms"),
            "source_fresh_at_capture": p.get("source_fresh_under_category_policy"),
            "runtime_qualified": False, "missing_requirements": p.get("missing_requirements", [])}
            for i, p in enumerate(row["positions"])]
    return view


def amount(value):
    try:
        parsed = Decimal(str(value))
        return parsed if parsed.is_finite() and parsed >= 0 else None
    except (InvalidOperation, ValueError):
        return None


def price_scenario(pricing):
    if not isinstance(pricing, dict) or pricing.get("overrides"):
        return None
    # Catalog units are USD/token; this excludes unobserved fee components.
    first, second = amount(pricing.get("prompt")), amount(pricing.get("completion"))
    return str(first * 2000 + second * 1000) if first is not None and second is not None else None


class OpenRouterPoolSource:
    """GET-only, fixed-origin source with bounded responses and no redirect credentials.

    Runtime contracts must be installed separately by the embedding service. A
    successful refresh stages all 23 categories, including explicit shortages.
    """
    def __init__(self, http, policies, *, api_key=None, endpoint_limit=12, clock=None):
        if set(policies) != set(CATEGORY_SPECS) or type(endpoint_limit) is not int or not 0 <= endpoint_limit <= 12:
            raise ValueError("Explicit 23-category policy and bounded provider requests required")
        self.http, self.policies, self.api_key = http, policies, api_key
        self.endpoint_limit = endpoint_limit
        self.clock = clock or (lambda: datetime.now(timezone.utc).isoformat())
        self.lock = threading.Lock()
        self.deadline = None
        self.attempts = []
        self.deferred_models = []
        self.last_completeness = None

    def get(self, path, evidence):
        if path not in ("/models", "/benchmarks", "/benchmarks?task_type=search&include_run_config=true") and not re.fullmatch(
                r"/models/[A-Za-z0-9_.~-]+/[A-Za-z0-9_.:%-]+/endpoints", path):
            raise MonitorError("source_url_not_allowed")
        headers = {"Accept": "application/json", "Accept-Encoding": "identity"}
        if self.api_key:
            headers["Authorization"] = "Bearer " + self.api_key
        started = time.monotonic()
        request_deadline = min(started + 15, self.deadline) if self.deadline is not None else started + 15
        remaining = request_deadline - started
        if remaining <= 0:
            raise MonitorError("source_refresh_deadline", 504)
        if len(self.attempts) >= 15:
            raise MonitorError("source_request_limit", 429)
        attempt = {"method": "GET", "url": BASE + path, "outcome": "pending"}
        self.attempts.append(attempt)
        try:
            with self.http.stream("GET", BASE + path, headers=headers, follow_redirects=False, timeout=min(15, remaining)) as response:
                if response.status_code != 200:
                    raise MonitorError("source_get_failed", 502)
                raw = bytearray()
                for block in response.iter_bytes():
                    raw.extend(block)
                    if len(raw) > LIMIT:
                        raise MonitorError("source_too_large", 502)
                    if self.deadline is not None and time.monotonic() > self.deadline:
                        raise MonitorError("source_refresh_deadline", 504)
                    if time.monotonic() > request_deadline:
                        raise MonitorError("source_request_deadline", 504)
        except httpx.TimeoutException:
            attempt.update(outcome="failed", code="source_timeout")
            raise MonitorError("source_timeout", 504) from None
        except httpx.HTTPError:
            attempt.update(outcome="failed", code="source_network_failed")
            raise MonitorError("source_network_failed", 502) from None
        except MonitorError as error:
            attempt.update(outcome="failed", code=error.code)
            raise
        attempt["outcome"] = "succeeded"
        evidence.append({"method": "GET", "url": BASE + path, "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)})
        return bytes(raw)

    def fetch(self, path, evidence, parser, *, model=None):
        before = len(self.attempts)
        try:
            raw = self.get(path, evidence)
            try:
                if model is not None and json.loads(raw)["data"]["id"] != model:
                    raise ValueError("Endpoint response does not match the requested model")
                parsed = parser(raw)
            except (ValueError, KeyError, TypeError, AttributeError, IndexError):
                self.attempts[-1].update(outcome="failed", code="source_schema_invalid")
                evidence[-1]["schema_valid"] = False
                raise MonitorError("source_schema_invalid", 502) from None
            evidence[-1]["schema_valid"] = True
            return parsed
        finally:
            if model is not None and len(self.attempts) > before:
                self.attempts[-1]["model"] = model

    def completeness(self, *, failed=False):
        failures = [{key: attempt[key] for key in ("model", "url", "code") if key in attempt}
                    for attempt in self.attempts if attempt["outcome"] == "failed"]
        endpoints = [attempt for attempt in self.attempts if "model" in attempt]
        return {"status": "failed" if failed else "partial" if failures or self.deferred_models else "complete",
                "attempted": len(self.attempts),
                "succeeded": sum(attempt["outcome"] == "succeeded" for attempt in self.attempts),
                "failed": len(failures), "deferred": len(self.deferred_models),
                "endpoint_attempted": len(endpoints),
                "endpoint_succeeded": sum(attempt["outcome"] == "succeeded" for attempt in endpoints),
                "endpoint_failed": sum(attempt["outcome"] == "failed" for attempt in endpoints),
                "failures": failures, "deferred_models": list(self.deferred_models)}

    def refresh(self):
        if not self.lock.acquire(blocking=False):
            raise MonitorError("source_refresh_busy", 409)
        try:
            self.deadline = time.monotonic() + 60
            self.attempts, self.deferred_models = [], []
            try:
                view = self._refresh()
            except Exception:
                self.last_completeness = self.completeness(failed=True)
                raise
            self.last_completeness = view["completeness"]
            return view
        finally:
            self.deadline = None
            self.lock.release()

    def _refresh(self):
        now, requests = self.clock(), []
        catalog = self.fetch("/models", requests, lambda raw: catalog_inventory(raw, retrieved_at=now))
        benchmark_parser = lambda raw: normalize_benchmarks(raw, retrieved_at=now, quarantine_ambiguous=True)
        rows = self.fetch("/benchmarks", requests, benchmark_parser)
        rows += self.fetch("/benchmarks?task_type=search&include_run_config=true", requests, benchmark_parser)
        # Exact ID/canonical matches only. Conflicting variants are omitted, not cherry-picked.
        models = catalog["models"]
        view = empty_view("openrouter_free_get_candidate_proposal", now)
        endpoints, endpoint_errors = {}, []
        drafted = {}
        for category, spec in CATEGORY_SPECS.items():
            policy = self.policies[category]
            view["categories"][category]["thresholds"] = {k: policy[k] for k in ("low_mid", "mid_high")}
            candidates = defaultdict(list)
            for row in rows:
                if category_matches(row, category) and row.get("binding_status") != "ambiguous_source_identity":
                    candidates[row["model_id"]].append(row)
            tier_rows = {tier: [] for tier in TIERS}
            for model in models:
                observations = candidates.get(model["model_id"], []) + candidates.get(model.get("canonical_slug"), [])
                # The same row can match both identical identities.
                observations = list({(o["raw_sha256"], o["source_row_index"]): o for o in observations}.values())
                if len(observations) != 1:
                    continue
                row = observations[0]
                quality = row["quality"][spec["metric"]]
                tier = "high" if quality >= policy["mid_high"] else "mid" if quality >= policy["low_mid"] else "low"
                raw_time = row.get("as_of") or row["retrieved_at"]
                age = (datetime.fromisoformat(now) - datetime.fromisoformat(raw_time.replace("Z", "+00:00"))).total_seconds()
                stale = age > policy.get("max_source_age_hours", 48) * 3600
                candidate = {"profile": {"model": model["model_id"], "model_version": None,
                                         "effort": row.get("effort"), "provider": None},
                    "quality": quality, "source_as_of": row.get("as_of"), "source_retrieved_at": now,
                    "status": "pending_unqualified_stale_source" if stale else "pending_unqualified",
                    "quote_usd": price_scenario(model["catalog_price"]), "quote_basis": "catalog 2000 input + 1000 output; not full cost",
                    "benchmark_speed": row.get("speed"), "endpoint_p50_ms": None,
                    "flow_cost_usd": None, "flow_latency_ms": None, "runtime_qualified": False,
                    "source_row": {"sha256": row["raw_sha256"], "index": row["source_row_index"]},
                    "modalities": model["architecture"].get("output_modalities"),
                    "missing_requirements": ["exact_runtime_binding", "task_workflow_evidence", "full_cost_and_health"]}
                # Preserve source modality as-is. Text Core cannot execute image/audio proposals.
                tier_rows[tier].append(candidate)
                if model.get("hugging_face_id"):
                    tier_rows["open_source"].append(candidate)
            def key(c):
                speed = amount((c.get("benchmark_speed") or {}).get("value"))
                return (amount(c["quote_usd"]) is None, amount(c["quote_usd"]) or Decimal(0),
                        speed is None, speed or Decimal(0), c["profile"]["model"])
            drafted[category] = {tier: sorted(items, key=key)[:3] for tier, items in tier_rows.items()}
        requested = sorted({c["profile"]["model"] for tiers in drafted.values() for items in tiers.values() for c in items})
        self.deferred_models = list(requested[self.endpoint_limit:])
        for index, model in enumerate(requested[:self.endpoint_limit]):
            before = len(self.attempts)
            try:
                endpoints[model] = self.fetch("/models/" + quote(model, safe="/:") + "/endpoints", requests,
                    lambda raw: normalize_endpoints(raw, retrieved_at=now), model=model)
            except MonitorError:
                if len(self.attempts) == before:
                    self.deferred_models.extend(requested[index:self.endpoint_limit])
                    break
                endpoint_errors.append(model)
        for category, tiers in drafted.items():
            for tier, items in tiers.items():
                for i, original in enumerate(items):
                    candidate = json.loads(json.dumps(original))
                    providers = endpoints.get(candidate["profile"]["model"], [])
                    # Provider observation is separate from selecting an executable endpoint.
                    candidate["provider_observations"] = [{"provider": p.get("provider"), "speed": p.get("speed"),
                        "health": p.get("health"), "pricing": p.get("pricing")} for p in providers]
                    def provider_order(p):
                        cost = amount(price_scenario(p.get("pricing")))
                        latency = amount((p.get("speed", {}).get("latency_last_30m") or {}).get("p50")) if isinstance(p.get("speed", {}).get("latency_last_30m"), dict) else None
                        status = p.get("health", {}).get("status")
                        return (0 if status == 0 else 1 if status is None else 2,
                                cost is None, cost or Decimal(0), latency is None, latency or Decimal(0), p["provider"])
                    if providers:
                        chosen = min(providers, key=provider_order)
                        candidate["profile"]["provider"] = chosen["provider"]
                        candidate["profile"]["quantization"] = chosen.get("quantization")
                        candidate["provider_selection_basis"] = "public_candidate_quote_not_verified_runtime_binding"
                        candidate["quote_usd"] = price_scenario(chosen.get("pricing"))
                        candidate["quote_basis"] = "provider 2000 input + 1000 output; not full cost"
                        speed = chosen.get("speed", {})
                        candidate["endpoint_p50_ms"] = ((speed.get("latency_last_30m") or {}).get("p50")
                            if isinstance(speed.get("latency_last_30m"), dict) and speed.get("latency_unit") == "milliseconds" else None)
                        candidate["provider_health"] = chosen.get("health")
                    candidate["position"] = i
                    view["categories"][category]["pools"][tier][i] = candidate
        complete = self.completeness()
        view.update({"requests": requests, "attempts": list(self.attempts), "completeness": complete,
                     "endpoint_errors": endpoint_errors,
                     "endpoint_models_requested": complete["endpoint_attempted"],
                     "endpoint_models_deferred": complete["deferred"],
                     "model_posts": 0, "ranking_basis": "source-native quality tiers, advertised token-cost, same-category benchmark mean speed; full-cost runtime admission separate"})
        return view


def differences(previous, new):
    changes = []
    for category in CATEGORY_SPECS:
        for tier in TIERS:
            old = previous["categories"][category]["pools"][tier] if previous else []
            after = new["categories"][category]["pools"][tier]
            if old != after:
                changes.append({"category": category, "tier": tier,
                                "before": [c.get("profile", {}).get("model") for c in old],
                                "after": [c.get("profile", {}).get("model") for c in after]})
    return changes

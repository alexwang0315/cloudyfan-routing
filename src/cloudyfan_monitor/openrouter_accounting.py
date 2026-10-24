"""Read-only OpenRouter billing evidence; never infer a customer's balance."""
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
import re
import time

import httpx


class AccountingError(Exception):
    def __init__(self, code, status=502):
        self.code, self.status = code, status
        super().__init__(code)


def _money(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise AccountingError("openrouter_accounting_schema_invalid")
    if (len(str(value)) > 64
            or not re.fullmatch(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?", str(value))):
        raise AccountingError("openrouter_accounting_schema_invalid")
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        raise AccountingError("openrouter_accounting_schema_invalid") from None
    if (not number.is_finite() or number < 0 or number > Decimal("1000000000")
            or number.as_tuple().exponent < -24):
        raise AccountingError("openrouter_accounting_schema_invalid")
    text = format(number, "f")
    return (text.rstrip("0").rstrip(".") if "." in text else text) if number else "0"


def _tokens(value):
    if value is None:
        return None
    if type(value) is not int or not 0 <= value <= 10**12:
        raise AccountingError("openrouter_accounting_schema_invalid")
    return value


class OpenRouterAccounting:
    def __init__(self, http, *, api_key, management_key=None, response_deadline=15):
        if (not isinstance(http, httpx.Client) or not isinstance(api_key, str)
                or not api_key or len(api_key) > 4096 or any(not 33 <= ord(c) <= 126 for c in api_key)):
            raise ValueError("Explicit HTTP client and accounting credential required")
        if management_key is not None and (not isinstance(management_key, str)
                or not management_key or len(management_key) > 4096
                or any(not 33 <= ord(c) <= 126 for c in management_key)):
            raise ValueError("Invalid management credential")
        if (type(response_deadline) not in (int, float) or not math.isfinite(response_deadline)
                or not 0 < response_deadline <= 60):
            raise ValueError("Invalid accounting response deadline")
        self._http, self._api_key, self._management_key = http, api_key, management_key
        self._response_deadline = response_deadline
        self.account_ref = hashlib.sha256(api_key.encode()).hexdigest()

    def _read(self, endpoint, *, key, params=None, time_budget=None):
        if endpoint not in {"generation", "key", "credits"}:
            raise AccountingError("openrouter_accounting_endpoint_forbidden")
        budget = self._response_deadline
        if time_budget is not None:
            if (type(time_budget) not in (int, float) or not math.isfinite(time_budget)
                    or time_budget <= 0):
                raise AccountingError("openrouter_accounting_time_budget_invalid", 400)
            budget = min(budget, time_budget)
        deadline = time.monotonic() + budget
        try:
            with self._http.stream("GET", "https://openrouter.ai/api/v1/" + endpoint,
                    headers={"Authorization": "Bearer " + key, "Accept-Encoding": "identity"},
                    params=params, follow_redirects=False,
                    timeout=httpx.Timeout(min(10, budget), connect=min(5, budget))) as response:
                if time.monotonic() > deadline:
                    raise AccountingError("openrouter_accounting_timeout", 504)
                if response.status_code != 200:
                    code = {401: "unauthorized", 403: "forbidden", 404: "not_found",
                            429: "rate_limited"}.get(response.status_code, "http_failed")
                    raise AccountingError("openrouter_accounting_" + code,
                                          429 if response.status_code == 429 else 502)
                raw = bytearray()
                for chunk in response.iter_bytes():
                    if time.monotonic() > deadline:
                        raise AccountingError("openrouter_accounting_timeout", 504)
                    raw.extend(chunk)
                    if len(raw) > 262144:
                        raise AccountingError("openrouter_accounting_response_too_large")
        except httpx.TimeoutException:
            raise AccountingError("openrouter_accounting_timeout", 504) from None
        except httpx.RequestError:
            raise AccountingError("openrouter_accounting_network_failed") from None

        def unique(pairs):
            result = {}
            for name, value in pairs:
                if name in result:
                    raise ValueError
                result[name] = value
            return result

        try:
            evidence = raw.decode("utf-8")
            payload = json.loads(evidence, parse_float=Decimal, object_pairs_hook=unique,
                                 parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
            if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
                raise ValueError
        except (ValueError, UnicodeError):
            raise AccountingError("openrouter_accounting_schema_invalid") from None
        return payload["data"], hashlib.sha256(raw).hexdigest(), evidence

    def generation(self, generation_id, *, expected_model, time_budget=None):
        if (not isinstance(generation_id, str)
                or not re.fullmatch(r"gen-[A-Za-z0-9_-]{1,196}", generation_id)
                or not isinstance(expected_model, str) or not 1 <= len(expected_model) <= 256
                or any(ord(c) < 33 or ord(c) > 126 for c in expected_model)):
            raise AccountingError("openrouter_accounting_identity_invalid", 400)
        data, sha, evidence = self._read("generation", key=self._api_key, params={"id": generation_id},
                                         time_budget=time_budget)
        if data.get("id") != generation_id or data.get("model") != expected_model:
            raise AccountingError("openrouter_accounting_identity_mismatch", 409)
        return {"provider": "openrouter", "generation_id": generation_id, "model": expected_model,
                "cost_usd": _money(data.get("total_cost")),
                "input_tokens": _tokens(data.get("native_tokens_prompt")),
                "output_tokens": _tokens(data.get("native_tokens_completion")),
                "source": "openrouter_generation_api", "evidence_sha256": sha, "evidence_json": evidence,
                "observed_at": datetime.now(timezone.utc).isoformat(), "account_ref": self.account_ref}

    def key_capacity(self, *, time_budget):
        """One bounded key GET using the existing exact accounting parser.

        Credits and management credentials are deliberately outside this source.
        Raw provider fields remain private to the parser, never its projection.
        """
        data, sha, _ = self._read("key", key=self._api_key, time_budget=time_budget)
        reset = data.get("limit_reset")
        includes_byok = data.get("include_byok_in_limit")
        if includes_byok is not None and type(includes_byok) is not bool:
            raise AccountingError("openrouter_accounting_schema_invalid")
        return {"provider": "openrouter", "credential_fingerprint": self.account_ref,
                "limit": None if data.get("limit") is None else _money(data["limit"]),
                "remaining": None if data.get("limit_remaining") is None else _money(data["limit_remaining"]),
                "usage": None if data.get("usage") is None else _money(data["usage"]),
                "reset_period": reset if reset in {"daily", "weekly", "monthly"} else None,
                "include_byok_in_limit": includes_byok,
                "evidence_sha256": sha}

    def account(self):
        """Return independent key/account scopes, retaining any successful scope."""
        result = {"provider": "openrouter", "account_ref": self.account_ref,
                  "account": {"scope": "account", "status": "not_configured"}}
        try:
            data, sha, evidence = self._read("key", key=self._api_key)
            includes_byok = data.get("include_byok_in_limit")
            if includes_byok is not None and type(includes_byok) is not bool:
                raise AccountingError("openrouter_accounting_schema_invalid")
            key = {"scope": "key", "status": "available", "usage_usd": _money(data.get("usage")),
                   "evidence_sha256": sha, "evidence_json": evidence, "credential_ref": self.account_ref,
                   "include_byok_in_limit": includes_byok}
            reset = data.get("limit_reset")
            key["limit_reset"] = reset if reset in ("daily", "weekly", "monthly") else None
            for name in ("limit", "limit_remaining", "byok_usage", "usage_daily", "usage_weekly", "usage_monthly"):
                key[name + "_usd"] = None if data.get(name) is None else _money(data[name])
            result["key"] = key
        except AccountingError as error:
            result["key"] = {"scope": "key", "status": "unavailable", "error": {"code": error.code}}
        if self._management_key is not None:
            try:
                data, sha, evidence = self._read("credits", key=self._management_key)
                result["account"] = {"scope": "account", "status": "available",
                    "credential_ref": hashlib.sha256(self._management_key.encode()).hexdigest(),
                    "total_credits_usd": _money(data.get("total_credits")),
                    "total_usage_usd": _money(data.get("total_usage")), "evidence_sha256": sha,
                    "evidence_json": evidence}
            except AccountingError as error:
                result["account"] = {"scope": "account", "status": "unavailable", "error": {"code": error.code}}
        scopes = [result["key"]] + ([result["account"]] if self._management_key is not None else [])
        available = sum(scope["status"] == "available" for scope in scopes)
        result["status"] = ("available" if available == len(scopes) else "partial" if available else "unavailable")
        result["observed_at"] = datetime.now(timezone.utc).isoformat()
        return result

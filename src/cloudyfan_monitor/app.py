"""Private WSGI monitor and controlled task ingress. No model calls on dashboard reads."""
from http import HTTPStatus
import hmac
import ipaddress
import json
from pathlib import Path
import re
from urllib.parse import parse_qs

from cloudyfan_jev.http_api import ServiceError
from cloudyfan_jev.identity import IdentityStore

from .pools import differences
from .store import MonitorError, encode

STATIC = Path(__file__).with_name("static")
SECURITY_HEADERS = [("X-Content-Type-Options", "nosniff"), ("Referrer-Policy", "no-referrer"),
                   ("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; "
                    "connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'")]


class App:
    def __init__(self, gateway, *, admin_token, source=None, origin="http://127.0.0.1:8766",
                 identity_store=None, billing_source=None, ollama_models=None, simulation=None):
        if len(admin_token) < 32:
            raise ValueError("Strong operator token required")
        self.gateway, self.store, self.admin_token = gateway, gateway.store, admin_token
        self.source, self.origin = source, origin
        if billing_source is not None:
            from .openrouter_accounting import OpenRouterAccounting
            if not isinstance(billing_source, OpenRouterAccounting):
                raise ValueError("Controlled read-only OpenRouter accounting source required")
            gateway.billing.source = billing_source
        if identity_store is not None and not isinstance(identity_store, IdentityStore):
            raise ValueError("Controlled identity registry required")
        self.identity_store = identity_store
        self.simulation = simulation
        self.ollama = None
        if ollama_models is not None:
            from .ollama_gateway import OllamaGateway
            self.ollama = OllamaGateway(gateway, models=ollama_models, authenticate=self._native_authenticate)

    def _authenticate(self, token, method, environ, *, scope=None):
        if self.identity_store is None:
            return self.store.authenticate(token)
        if environ.get("wsgi.url_scheme") != "https":
            try:
                local = (environ.get("wsgi.url_scheme") == "http"
                         and ipaddress.ip_address(environ.get("REMOTE_ADDR", "")).is_loopback)
            except ValueError:
                local = False
            if not local:
                raise MonitorError("unauthorized", 401)
        try:
            principal = self.identity_store.authenticate(token, scope or ("tasks:read" if method == "GET" else "tasks:write"))
        except Exception:
            raise MonitorError("identity_authentication_unavailable", 503) from None
        if principal is None:
            raise MonitorError("unauthorized", 401)
        self.gateway.service.authorize(principal)
        self.store.touch_client(principal)
        return principal

    def _native_authenticate(self, environ):
        authorization = environ.get("HTTP_AUTHORIZATION", "")
        token = authorization.removeprefix("Bearer ") if authorization.startswith("Bearer ") else ""
        method, path = environ.get("REQUEST_METHOD"), environ.get("PATH_INFO", "")
        scope = ("models:read" if method == "GET" and path == "/api/tags" else
                 "tasks:write" if method == "POST" and path in ("/api/generate", "/api/chat") else None)
        if scope is None:
            raise MonitorError("endpoint_not_supported", 404)
        return self._authenticate(token, method, environ, scope=scope)

    def _body(self, environ):
        if environ.get("CONTENT_TYPE", "").split(";")[0] != "application/json":
            raise MonitorError("json_required", 415)
        try:
            length = int(environ.get("CONTENT_LENGTH") or 0)
            if not 1 <= length <= 65536:
                raise ValueError
            def unique(pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError
                    result[key] = value
                return result
            value = json.loads(environ["wsgi.input"].read(length), object_pairs_hook=unique,
                               parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
            if not isinstance(value, dict):
                raise ValueError
            return value
        except (ValueError, UnicodeError):
            raise MonitorError("invalid_json_body") from None

    @staticmethod
    def _fields(body, required, optional=()):
        if not set(required) <= set(body) or set(body) - set(required) - set(optional):
            raise MonitorError("unsupported_fields")

    def __call__(self, environ, start_response):
        status, content_type = 200, "application/json; charset=utf-8"
        method, path = environ.get("REQUEST_METHOD"), environ.get("PATH_INFO", "")
        try:
            if environ.get("HTTP_ORIGIN") not in (None, self.origin):
                raise MonitorError("origin_not_allowed", 403)
            if environ.get("HTTP_HOST") != self.origin.split("://", 1)[1]:
                raise MonitorError("host_not_allowed", 403)
            static = {"/": ("index.html", "text/html; charset=utf-8"),
                      "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                      "/billing.js": ("billing.js", "text/javascript; charset=utf-8"),
                      "/billing.css": ("billing.css", "text/css; charset=utf-8"),
                      "/trace.js": ("trace.js", "text/javascript; charset=utf-8"),
                      "/trace.css": ("trace.css", "text/css; charset=utf-8"),
                      "/style.css": ("style.css", "text/css; charset=utf-8")}
            if method == "GET" and path in static:
                filename, content_type = static[path]
                body = (STATIC / filename).read_bytes()
            elif path.startswith("/api/") and self.ollama is not None:
                def native_response(status_line, headers, exc_info=None):
                    return start_response(status_line, [*headers, *SECURITY_HEADERS], exc_info)
                return self.ollama(environ, native_response)
            else:
                authorization = environ.get("HTTP_AUTHORIZATION", "")
                token = authorization.removeprefix("Bearer ") if authorization.startswith("Bearer ") else ""
                if path.startswith("/monitor/"):
                    if not hmac.compare_digest(token, self.admin_token):
                        raise MonitorError("unauthorized", 401)
                    body = self._admin(method, path, environ)
                elif path.startswith("/v1/"):
                    principal = self._authenticate(token, method, environ)
                    body = self._client(principal, method, path, environ)
                elif path == "/demo/session" and method == "GET" and self.gateway.demo:
                    # Only synthetic mode exposes a disposable token, never a production token.
                    body = {"token": self.admin_token, "demo": True}
                else:
                    raise MonitorError("endpoint_not_supported", 404)
                status = {"accounting_interrupted": 409, "engineering_interrupted": 503,
                          "policy_denied": 403, "expired": 410, "retired": 410,
                          "source_refresh_failed": 503}.get(body.get("status"), 200)
                body = encode(body).encode()
        except (MonitorError, ServiceError) as error:
            detail = error.code if path.startswith("/api/") else {"code": error.code}
            status, body = error.status, encode({"error": detail}).encode()
        except Exception:
            detail = "monitor_or_source_unavailable" if path.startswith("/api/") else {"code": "monitor_or_source_unavailable"}
            status, body = 503, encode({"error": detail}).encode()
        headers = [("Content-Type", content_type), ("Content-Length", str(len(body))),
                   ("Cache-Control", "no-store"), *SECURITY_HEADERS]
        start_response(f"{status} {HTTPStatus(status).phrase}", headers)
        return [body]

    def _admin(self, method, path, environ):
        if method == "POST" and path == "/monitor/simulation/start":
            if self.gateway.demo is not True or self.simulation is None:
                raise MonitorError("simulation_not_available", 409)
            self._fields(self._body(environ), set())
            return self.simulation.start()
        if method == "GET" and path.startswith("/monitor/billing/evidence/"):
            return self.gateway.billing.evidence(path.removeprefix("/monitor/billing/evidence/"))
        if method == "GET" and path == "/monitor/billing":
            return self.gateway.billing.snapshot()
        if method == "POST" and path == "/monitor/billing/settings":
            body = self._body(environ)
            self._fields(body, {"revision", "enabled", "multiplier", "client_multipliers", "open_source_prices"})
            return self.gateway.billing.configure(body)
        if method == "POST" and path == "/monitor/billing/credits":
            body = self._body(environ)
            self._fields(body, {"client", "amount_usd", "idempotency_key", "note"})
            return self.gateway.billing.credit(body)
        if method == "POST" and path == "/monitor/billing/reconcile":
            self._fields(self._body(environ), set())
            return self.gateway.billing.reconcile(self.gateway.ledger())
        if method == "POST" and path == "/monitor/billing/source/account":
            self._fields(self._body(environ), set())
            return self.gateway.billing.read_account()
        if method == "GET" and path == "/monitor/snapshot":
            view = self.gateway.snapshot()
            view["source_refresh_available"] = self.source is not None
            if self.simulation is not None:
                view["simulation"] = self.simulation.descriptor()
            return view
        if method == "GET" and path == "/monitor/pools":
            params = parse_qs(environ.get("QUERY_STRING", ""))
            pool = params.get("id", [self.store.control()["catalog_pool"]])[0]
            if pool is None:
                raise MonitorError("pool_not_configured", 409)
            view = self.store.pool(pool)
            view["routing_available"] = any(p == pool for p, _, _ in self.gateway.registry)
            return view
        if method == "POST" and path == "/monitor/mode":
            body = self._body(environ)
            self._fields(body, {"revision", "mode"}, {"client"})
            return self.gateway.switch(body["revision"], body["mode"], body.get("client"))
        if method == "POST" and path == "/monitor/retention/preview":
            body = self._body(environ)
            self._fields(body, {"client", "run_id"})
            return self.gateway.retention_preview(body["client"], body["run_id"])
        if method == "POST" and path == "/monitor/pools/refresh":
            body = self._body(environ)
            self._fields(body, set())
            if self.source is None:
                raise MonitorError("source_refresh_not_configured", 409)
            if not self.gateway.refresh_lock.acquire(blocking=False):
                raise MonitorError("source_refresh_busy", 409)
            try:
                # Refresh stages a new version; never changes ongoing tasks or dispatch contracts.
                new = self.source.refresh()
                key = self.store.save_pool(new)
                old_key = self.store.control()["catalog_pool"]
                old = self.store.pool(old_key) if old_key else None
                completeness = new.get("completeness", {})
                return {"pool": key, "changes": differences(old, new), "model_posts": 0,
                        "activated": False, "request_count": completeness.get("attempted", len(new.get("requests", []))),
                        "completeness": completeness,
                        "demo": self.gateway.demo}
            except Exception as error:
                completeness = getattr(self.source, "last_completeness", None)
                return {"status": "source_refresh_failed",
                        "error": {"code": error.code if isinstance(error, MonitorError) else "source_refresh_failed"},
                        "completeness": completeness,
                        "request_count": (completeness or {}).get("attempted", 0),
                        "activated": False, "previous_pool": self.store.control()["catalog_pool"]}
            finally:
                self.gateway.refresh_lock.release()
        if method == "POST" and path in ("/monitor/pools/publish", "/monitor/pools/activate"):
            body = self._body(environ)
            self._fields(body, {"pool", "revision"})
            if path.endswith("activate"):
                return self.gateway.activate(body["pool"], body["revision"])
            return self.store.change(revision=body["revision"], catalog=body["pool"])
        raise MonitorError("endpoint_not_supported", 404)

    def _client(self, principal, method, path, environ):
        if method == "GET" and path == "/v1/billing":
            return self.gateway.billing.snapshot(principal)
        if method == "GET" and path == "/v1/monitor":
            return self.gateway.snapshot(principal)
        if method == "POST" and path == "/v1/tasks":
            return self.gateway.create(principal, self._body(environ))
        match = re.fullmatch(r"/v1/tasks/([A-Za-z0-9][A-Za-z0-9_-]{0,63})(/result|/resume)?", path)
        if match:
            self.store.binding(principal, match[1])
            if method == "GET" and match[2] != "/resume":
                state = self.gateway.status(principal, match[1])
                if match[2] != "/result" or state.get("status") in ("expired", "retired"):
                    return state
                return self.gateway.service.result(principal, match[1], refresh=False)
            if method == "POST" and match[2] == "/resume":
                if self._body(environ) != {}:
                    raise MonitorError("resume_cannot_change_frozen_settings")
                return self.gateway.execute(principal, match[1])
        raise MonitorError("endpoint_not_supported", 404)

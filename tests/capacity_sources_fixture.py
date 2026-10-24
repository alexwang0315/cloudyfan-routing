"""Controlled synthetic capacity configuration; never a supplier qualification."""
from copy import deepcopy
from dataclasses import dataclass, field
from uuid import uuid4

import httpx

from cloudyfan_monitor.openrouter_accounting import OpenRouterAccounting


@dataclass
class MockCapacitySources:
    requests: list = field(default_factory=list)
    failure: set = field(default_factory=set)
    before_response: object = None
    credentials: list = field(default_factory=list, repr=False)

    def source(self, label):
        credential, management = uuid4().hex, uuid4().hex
        self.credentials.extend((credential, management))
        def receive(request):
            self.requests.append({"credential_ref": label, "method": request.method,
                                  "host": request.url.host, "path": request.url.path,
                                  "timeout": request.extensions.get("timeout")})
            assert request.url == "https://openrouter.ai/api/v1/key"
            assert request.method == "GET"
            if self.before_response is not None:
                self.before_response(label)
            if label in self.failure:
                return httpx.Response(403, json={"error": {"message": "PRIVATE_UPSTREAM_ERROR_MARKER"}})
            return httpx.Response(200, json={"data": {"usage": "4", "limit": "20",
                "limit_remaining": "16", "limit_reset": "daily", "include_byok_in_limit": False,
                "private_nested": {"prompt": "PRIVATE_SOURCE_PROMPT_MARKER", "credential": credential}}})
        return OpenRouterAccounting(httpx.Client(transport=httpx.MockTransport(receive), trust_env=False),
                                    api_key=credential, management_key=management)

    def configured(self):
        return {"credential-" + suffix: self.source("credential-" + suffix) for suffix in ("a", "b")}


def limit(dimension, value, now, *, status="known", scope=None, expires=300):
    result = {"dimension": dimension, "status": status, "value": value,
              "unit": {"rpm": "requests", "tpm": "tokens", "concurrency": "requests", "usd": "USD"}[dimension],
              "window_seconds": 60 if dimension in ("rpm", "tpm") else None,
              "observed_at": now, "reset_at": now + 60 if dimension in ("rpm", "tpm") else None,
              "valid_until": now + expires, "evidence_ref": "synthetic-import-evidence-v1"}
    if scope is not None:
        result["scope"] = scope
    return result


def configuration(now):
    return {"schema_version": 1, "version": "synthetic-capacity-v1", "bindings": [
        {"id": "binding-a", "provider": "openrouter", "account_ref": "account-shared",
         "credential_ref": "credential-a", "endpoint_ref": "chat-main", "groups": ["account-shared", "key-a"]},
        {"id": "binding-b", "provider": "openrouter", "account_ref": "account-shared",
         "credential_ref": "credential-b", "endpoint_ref": "chat-main", "groups": ["account-shared", "key-b"]},
        {"id": "binding-local", "provider": "other-supplier", "account_ref": "other-account",
         "credential_ref": "other-credential", "endpoint_ref": "other-endpoint", "groups": ["unsupported-group"]},
    ], "groups": [
        {"id": "account-shared", "scope": {"kind": "account", "ref": "account-shared"},
         "source": {"kind": "controlled_import", "version": "synthetic-reviewed-import-v1"},
         "supplier_limits": [limit("rpm", 120, now), limit("tpm", 30000, now)],
         "local_limits": [limit("rpm", 10, now), limit("tpm", 5000, now), limit("concurrency", 2, now)]},
        *[{"id": "key-" + suffix, "scope": {"kind": "credential", "ref": "credential-" + suffix},
           "source": {"kind": "openrouter_key", "version": "openrouter-key-accounting-v1",
                      "credential_ref": "credential-" + suffix},
           "supplier_limits": [], "local_limits": []} for suffix in ("a", "b")],
        {"id": "unsupported-group", "scope": {"kind": "endpoint", "ref": "other-endpoint"},
         "source": {"kind": "unsupported", "version": "unsupported-supplier-v1"},
         "supplier_limits": [], "local_limits": [limit("concurrency", None, now, status="not_applicable")]},
    ]}


def attach_capacity(bundle, clock, *, config=None, sources=None, **refresh_options):
    from cloudyfan_jev.capacity import CapacityStore
    from cloudyfan_monitor.capacity import CapacityObserver
    from cloudyfan_monitor.integration import assemble_monitor

    old = bundle.app
    capacity = CapacityStore(bundle.root / "capacity.sqlite", clock=clock)
    capacity.configure(deepcopy(config or configuration(clock())))
    observer = CapacityObserver(capacity, sources=sources or {}, **refresh_options)
    bundle.app = assemble_monitor(old.store, old.gateway.service, old.gateway.registry, old.identity_store,
        operator_token=bundle.operator_token, source=old.source, origin=old.origin, demo=True,
        capacity_observer=observer)
    return observer

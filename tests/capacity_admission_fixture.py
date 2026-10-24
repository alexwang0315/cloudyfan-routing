"""Synthetic host registration; no real supplier or token qualification."""
from cloudyfan_jev.capacity import CapacityStore
from cloudyfan_jev.capacity_dispatch import CapacityController, CapacityRoute
from cloudyfan_monitor.capacity import CapacityObserver
from cloudyfan_monitor.integration import assemble_monitor
from capacity_sources_fixture import limit


def attach_admission(bundle, clock, *, rpm=1, tpm=None, concurrency=1, token_adapter=None, seconds=300, config=None):
    old = bundle.app
    scope = {"kind": "account", "ref": "synthetic-shared-account"}
    store = CapacityStore(bundle.root / "capacity.sqlite", clock=clock)
    store.configure(config or {"schema_version": 1, "version": "synthetic-admission-config-v1",
        "bindings": [{"id": "synthetic-binding", "provider": "openrouter", "account_ref": scope["ref"],
            "credential_ref": "synthetic-upstream-key", "endpoint_ref": "openrouter-chat", "groups": ["shared"]}],
        "groups": [{"id": "shared", "scope": scope,
            "source": {"kind": "controlled_import", "version": "synthetic-reviewed-v1"},
            "supplier_limits": [], "local_limits": [limit("rpm", rpm, clock()),
                limit("tpm", tpm, clock(), status="known" if tpm is not None else "not_applicable"),
                limit("concurrency", concurrency, clock())]}]})
    identity = old.identity_store
    def credential_active(principal, credential, scope, now):
        return identity.is_active(principal) and any(row["id"] == credential and row["revoked_at"] is None
            and row["created_at"] <= now < row["expires_at"] and scope in row["scopes"]
            for row in identity.credentials(principal))
    controller = CapacityController(store, credential_active=credential_active)
    routes = []
    seen = set()
    for contract in old.gateway.service.contracts.values():
        runner = contract.runner
        for profile in [*runner.profiles.values(), runner.decision]:
            model = profile.get("model_version", profile.get("version"))
            local = profile.get("backend") == "local"
            transport = runner.local_backend if local else runner.client
            endpoint = (transport.config['endpoint'] if local else
                "https://openrouter.ai/api/alpha/decisions" if profile is runner.decision else "https://openrouter.ai/api/v1/chat/completions")
            key = (str(transport.root), model, endpoint)
            if key not in seen:
                routes.append(CapacityRoute(transport, endpoint, model, "synthetic-binding", token_adapter=token_adapter, provider_name=profile.get("provider_name")))
                seen.add(key)
    controller.register("synthetic-admission-v1", configuration_version="synthetic-admission-config-v1",
        routes=routes, max_seconds=seconds)
    bundle.app = assemble_monitor(old.store, old.gateway.service, old.gateway.registry, identity,
        operator_token=bundle.operator_token, source=old.source, origin=old.origin, demo=True,
        capacity_observer=CapacityObserver(store, sources={}), capacity_controller=controller)
    return controller

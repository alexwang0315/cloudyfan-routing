"""One real backup host for current and historical observation acceptance."""
from copy import deepcopy
import httpx
import uuid

from cloudyfan_monitor.integration import assemble_monitor
from cloudyfan_monitor.openrouter_accounting import OpenRouterAccounting
from cloudyfan_monitor.pools import OpenRouterPoolSource, empty_view

from history_fixture import assemble_backup_host


def assemble_resilience_host(root, origin):
    host = assemble_backup_host(root, origin)
    plan, store = host.values["plan"], host.app.store
    view = empty_view("synthetic_resilience_no_real_qualification", host.values["now"].isoformat())
    for tier, rows in plan["pools"].items():
        for index, record in enumerate(rows):
            view["categories"][plan["category_id"]]["pools"][tier][index] = {
                "position": index, "status": "mock_only", **deepcopy(record["entry"])}
    active = store.save_pool(view)
    proposed = deepcopy(view)
    proposed["source_kind"] = "synthetic_resilience_inactive_proposal"
    proposal = store.save_pool(proposed)
    store.change(revision=store.control()["revision"], routing=active, catalog=active)
    registry = {(active, plan["category_id"], "cloudyfan"): "history-backup-v1"}
    host.app = assemble_monitor(store, host.service, registry, host.app.identity_store,
        operator_token=host.operator_token, origin=origin, demo=True)
    host.side_requests = {"source": [], "accounting": []}
    host.observation_http = []
    for kind in host.side_requests:
        def unexpected(request, source=kind):
            host.side_requests[source].append({"method": request.method, "path": request.url.path})
            return httpx.Response(503, json={"error": "PRIVATE_UPSTREAM_FAILURE_MARKER"})
        host.observation_http.append(httpx.Client(transport=httpx.MockTransport(unexpected)))
    from test_sources import fixtures
    host.app.source = OpenRouterPoolSource(host.observation_http[0], fixtures()[2])
    host.app.gateway.billing.source = OpenRouterAccounting(host.observation_http[1], api_key=uuid.uuid4().hex)
    host.pools, host.category = (active, proposal), plan["category_id"]
    return host

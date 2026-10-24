"""Synthetic host inputs shared by HTTP acceptance and the desktop demonstration."""
from copy import deepcopy
from dataclasses import dataclass

from cloudyfan_jev.scores.qcs import flow_contract_hash
from cloudyfan_monitor.integration import assemble_monitor
from cloudyfan_monitor.pools import empty_view

from test_category_host_integration import OPERATOR, assemble_category_host, category_values


@dataclass
class CandidateHost:
    app: object
    service: object
    budget: object
    credentials: dict
    calls: list
    root: object
    pools: tuple
    category: str
    clock: object
    operator_token: str = OPERATOR


def assemble_candidate_host(root, origin, *, clock, exclude_low=False):
    values, calls = category_values(), []
    if exclude_low:
        values["evidence"]["low"]["quality_sufficient"] = False
        values["policy"]["complete_policy_flow"]["contract_sha256"] = flow_contract_hash(
            values["matrix"]["categories"][values["category"]]["snapshot"], values["requirements"],
            values["evidence"], values["policy"])
    original, service, budget, credentials = assemble_category_host(root, values, calls, clock=clock)
    descriptor = service.contracts["category-host-v1"].descriptor()
    view = empty_view("synthetic_candidate_observation_no_real_qualification", values["now"])
    category = values["category"]
    for tier, profile in descriptor["profiles"].items():
        selection = profile["capability_card"]["score_selection"]
        view["categories"][category]["pools"][tier][0] = {
            "position": 0, "status": "mock_only", "profile": deepcopy(profile),
            "config_id": selection["config_id"], "runtime_qualified": True,
        }
    view["categories"][category]["pools"]["low"][1] = {
        "position": 1, "status": "pending_unqualified", "config_id": "proposal-with-missing-identity",
        "profile": {"model": "example/proposed"}, "runtime_qualified": True,
    }
    active = original.store.save_pool(view)
    proposal = deepcopy(view)
    proposal["source_kind"] = "synthetic_inactive_candidate_proposal"
    proposed = original.store.save_pool(proposal)
    original.store.change(revision=original.store.control()["revision"], routing=active, catalog=active)
    registry = {(active, category, "cloudyfan"): "category-host-v1"}
    app = assemble_monitor(original.store, service, registry, original.identity_store,
                           operator_token=OPERATOR, origin=origin, demo=True)
    return CandidateHost(app, service, budget, credentials, calls, root, (active, proposed), category, clock)

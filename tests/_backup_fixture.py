"""Private preparation of existing synthetic backup evidence for Monitor tests."""
import importlib.util
from pathlib import Path
import sys

import cloudyfan_jev
from cloudyfan_jev.demo import example_config
from cloudyfan_jev.engine import Core, Limits
from cloudyfan_jev.privacy import ExecutionPolicy
from cloudyfan_jev.scores.backup_runtime import backup_pool_contract_hash
from cloudyfan_jev.scores.catalog import digest


def _locations(value):
    if isinstance(value, dict):
        if isinstance(value.get("profile"), dict):
            value["profile"]["execution_location"] = "cloud:fixture"
        for child in value.values():
            _locations(child)
    elif isinstance(value, list):
        for child in value:
            _locations(child)


def prepare_backup_values(*, contract, diagnostics, count, classifiers=(), economic=True):
    core_tests = Path(cloudyfan_jev.__file__).resolve().parents[2] / "tests"
    spec = importlib.util.spec_from_file_location("_monitor_backup_fixture", core_tests / "test_backup_runtime.py")
    module = importlib.util.module_from_spec(spec)
    original = sys.path[:]
    try:
        sys.path.insert(0, str(core_tests))
        spec.loader.exec_module(module)
    finally:
        sys.path[:] = original
    plan = module.plan_for(classifiers=classifiers, count=count)
    _locations(plan)
    snapshot = plan["snapshot"]
    snapshot["snapshot_id"] = digest({key: value for key, value in snapshot.items() if key != "snapshot_id"})
    plan["snapshot_id"] = snapshot["snapshot_id"]
    privacy = ExecutionPolicy("unrestricted", "home:fixture", "cloud:typesafe")
    _, decision = example_config()
    profiles = {tier: rows[0]["entry"]["profile"] for tier, rows in plan["pools"].items()}
    plan["qcs_policy"]["execution_contract"] = Core(None, profiles, decision, limits=Limits(max_turns=1),
        execution_policy=privacy, diagnostic_contract=diagnostics).execution_contract_descriptor(contract)
    for stage in plan["qcs_policy"]["stages"].values():
        for variant in stage["variants"].values():
            variant["calibration"]["pool_contract_sha256"] = backup_pool_contract_hash(plan)
            if not economic:
                variant["classifier_cost_usd"] = {"lower": ".2", "upper": ".2"}
    module.rebind(plan)
    return {"plan": plan, "module": module, "privacy": privacy, "diagnostics": diagnostics,
            "decision": decision, "now": module.NOW}

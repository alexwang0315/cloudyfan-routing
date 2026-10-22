import json
import socket

import pytest
from historical_readonly import (
    PENDING_CID,
    Archive,
    build_historical_bindings,
    offline_guard,
    run_demo,
    sha,
)
from parallel_runner import digest


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True))


@pytest.fixture
def archive_fixture(tmp_path):
    old = tmp_path / "old"
    execution = old / "execution"
    export = execution / "exports/frozen"
    run_id = "s50v2-S50-001-A"
    run = execution / "capture/runs" / run_id
    manifest = {"task": {"prompt": "public task"}, "mode": "cloudyfan",
                "contract_version": "sql-public-fixed", "routing_version": "frozen-v8"}
    save(run / "manifest.json", manifest)
    save(run / "result.json", {"run_id": run_id, "final_public_pass": True})
    public = tmp_path / "public.json"
    save(public, {"contract": "public fixed"})
    save(execution / "study/plan.json", {
        "sources": {str(public): sha(public)},
        "public_contracts": {"S50-001": {"contract_version": "sql-public-fixed"}},
        "run_configuration_sha256": {run_id: digest({"routing_version": "frozen-v8"})},
        "run_public_task_sha256": {run_id: digest(manifest["task"])},
        "schedule": [{"question_id": "S50-001", "ordinal": 1}],
    })
    save(execution / "study/terminal-S50-001-A.json", {
        "task_id": "S50-001", "arm": "A", "run_id": run_id,
        "final_public_pass": True, "first_public_pass": False, "status": "passed",
        "known_actual_api_usd": "0.001", "billing_complete": True,
        "files": {str(p): sha(p) for p in run.glob("*.json")},
    })
    call = execution / "capture/calls" / PENDING_CID
    projection = {"url": "https://openrouter.ai/api/v1/chat/completions",
                  "payload": {"model": "archive/model", "messages": [
                      {"role": "user", "content": "public frozen task"}]}}
    # Original transport uses sorted JSON with spaces, unlike the new envelope.
    import hashlib
    original_sha = hashlib.sha256(json.dumps(
        projection, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()).hexdigest()
    save(call / "intent.json", {**projection, "request_sha256": original_sha,
                                "call_id": PENDING_CID})
    save(call / "response.json", {"raw": json.dumps({"id": "gen-archived-generation",
         "model": "archive/model", "provider": "Archived", "usage": {"cost": "0.00361001"},
         "choices": [{"message": {"content": "saved answer must not be printed"}}]}),
         "http_status": 200, "headers": {}, "call_id": PENDING_CID})
    save(export / "ledger.json", {"new_known_reconciled_api_usd": "0.213780739",
         "prior_known_api_usd": "0.029674912", "rows": [{"call_id": PENDING_CID,
         "generation_id": "gen-archived-generation", "cost_usd": "0.00361001",
         "cost_unknown": False}]})
    save(export / "report.json", {"status": "archived partial"})
    baseline = tmp_path / "baseline.json"

    def pin():
        files = list(old.rglob("*.json")) + [public]
        save(baseline, {"original_files": {str(p): sha(p) for p in files}})
        return Archive(tmp_path, old, baseline, sha(baseline))

    return {"old": old, "execution": execution, "export": export,
            "pin": pin, "public": public, "baseline": baseline}


def bundle(f):
    return build_historical_bindings(archive=f["pin"](), execution=f["execution"],
                                     export=f["export"], expected_completed=1)


def test_paid_import_twice_after_expiry_zero_sends_no_double_charge(tmp_path, archive_fixture):
    f = archive_fixture
    before = {str(p): sha(p) for p in f["old"].rglob("*.json")}
    result = run_demo(tmp_path / "new-output", bundle=bundle(f))
    assert result["new_model_sends"] == result["core_constructions"] == 0
    assert result["new_actual_usd"] == "0"
    assert result["known_cumulative_usd"] == "0.243455651"
    assert result["imported_original_call_cost_usd"] == "0.00361001"
    assert result["saved_call_reads"] == 2 and result["generation_attempts_consumed"] == 1
    assert result["synthetic_authorization_expired"]
    assert before == {str(p): sha(p) for p in f["old"].rglob("*.json")}
    summary = (tmp_path / "new-output/summary.json").read_text()
    assert "saved answer" not in summary and "public frozen task" not in summary


def test_existing_output_is_not_overwritten(tmp_path):
    with pytest.raises(FileExistsError, match="NEW_OUTPUT"):
        run_demo(tmp_path)


def test_terminal_and_raw_tamper_fail_closed(archive_fixture):
    f = archive_fixture
    archive = f["pin"]()
    p = f["execution"] / "study/terminal-S50-001-A.json"
    data = json.loads(p.read_text())
    data["final_public_pass"] = False
    save(p, data)
    with pytest.raises(ValueError, match="HASH_CHANGED"):
        build_historical_bindings(archive=archive, execution=f["execution"],
                                   export=f["export"], expected_completed=1)


@pytest.mark.parametrize("field", ["routing_version", "task", "contract_version"])
def test_model_or_task_drift_even_with_new_pin_rejected(archive_fixture, field):
    f = archive_fixture
    p = f["execution"] / "capture/runs/s50v2-S50-001-A/manifest.json"
    data = json.loads(p.read_text())
    data[field] = {"prompt": "changed task"} if field == "task" else "changed"
    save(p, data)
    terminal = f["execution"] / "study/terminal-S50-001-A.json"
    t = json.loads(terminal.read_text())
    t["files"][str(p)] = sha(p)
    save(terminal, t)
    with pytest.raises(ValueError, match="TASK_CONFIG_CONTRACT"):
        bundle(f)


def test_path_scope_and_sensitive_network_hard_denial(tmp_path, archive_fixture):
    f = archive_fixture
    archive = f["pin"]()
    with pytest.raises(ValueError, match="OUTSIDE_FROZEN"):
        archive.checked(tmp_path / "not-declared.json")
    for forbidden in (tmp_path / ".env", tmp_path / "private/answer.json",
                      tmp_path / "authoring/reference.json"):
        with offline_guard(), pytest.raises(PermissionError):
            forbidden.read_text()
    with offline_guard(), pytest.raises(RuntimeError, match="NETWORK_FORBIDDEN"):
        socket.create_connection(("example.com", 443))


def test_parent_manifest_and_final_source_mutation_rejected(archive_fixture):
    f = archive_fixture
    archive = f["pin"]()
    f["public"].write_text("changed")
    with pytest.raises(ValueError, match="CHANGED_DURING"):
        # Add the exact declared public source to this already trusted read set.
        archive.seen[str(f["public"])] = archive.pins[str(f["public"])]
        archive.verify_again()
    with pytest.raises(ValueError, match="PARENT_PRESERVATION"):
        Archive(f["old"].parent, f["old"], f["baseline"], "0" * 64)


def test_original_declared_external_source_can_use_preserved_plan_pin(archive_fixture):
    f = archive_fixture
    archive = f["pin"]()
    del archive.pins[str(f["public"])]
    b = build_historical_bindings(archive=archive, execution=f["execution"],
                                  export=f["export"], expected_completed=1)
    assert len(b["jobs"]) == 1
    f["public"].write_text("later drift")
    with pytest.raises(ValueError, match="HASH_CHANGED"):
        archive.checked(f["public"])


def test_demo_cannot_write_inside_old_archive(archive_fixture):
    f = archive_fixture
    with pytest.raises(ValueError, match="MUST_NOT_MODIFY"):
        run_demo(f["old"] / "new-output", bundle=bundle(f))


def test_pending_paid_raw_tamper_cannot_import_or_send(tmp_path, archive_fixture):
    f = archive_fixture
    b = bundle(f)
    raw = f["execution"] / "capture/calls" / PENDING_CID / "response.json"
    raw.write_text("{}")
    with pytest.raises(ValueError, match="HASH_CHANGED"):
        run_demo(tmp_path / "new-output", bundle=b)

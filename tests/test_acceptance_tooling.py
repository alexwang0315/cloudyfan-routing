"""Verification tools are exercised through their command-line/file boundaries."""
import json
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import shutil
import signal
import time

import pytest


ROOT = Path(__file__).resolve().parents[1]


def command(*args, env=None):
    return subprocess.run([sys.executable, str(ROOT / "scripts/acceptance.py"), *map(str, args)],
                          text=True, capture_output=True,
                          env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", **(env or {})})


def test_snapshot_freezes_inputs_and_rejects_unapproved_numbered_tests(tmp_path):
    core = tmp_path / "core"
    (core / "tests").mkdir(parents=True)
    (core / "src").mkdir()
    (core / "tests/test_current.py").write_text("def test_current(): assert True\n")
    (core / "src/current.py").write_text("VERSION = 1\n")
    (core / ".env").write_text("PRIVATE_DATA_MUST_STAY_OUT\n")
    (core / "tests/test_current 2.py").write_text("historical copy\n")
    output = tmp_path / "frozen"
    denied = command("snapshot", "--core-root", core, "--output", output)
    assert denied.returncode != 0
    assert "unapproved historical test" in denied.stderr
    assert not output.exists()


@pytest.mark.parametrize("alteration", ["additional_test", "changed_approved_test"])
def test_historical_policy_cannot_grant_new_exclusions(tmp_path, alteration):
    core = tmp_path / "core"
    (core / "tests").mkdir(parents=True)
    policy = json.loads((ROOT / "config/acceptance-scope.json").read_text())
    relative = "tests/test_unapproved 3.py" if alteration == "additional_test" else "tests/test_budget 2.py"
    historical = core / relative
    historical.write_text("def test_new_failure(): assert False\n")
    replacement = {"path": relative, "sha256": hashlib.sha256(historical.read_bytes()).hexdigest()}
    if alteration == "additional_test":
        policy["historical_tests"].append(replacement)
    else:
        policy["historical_tests"] = [replacement if item["path"] == relative else item
                                      for item in policy["historical_tests"]]
    custom = tmp_path / "self-declared-policy.json"
    custom.write_text(json.dumps(policy))
    output = tmp_path / "frozen"
    result = command("snapshot", "--core-root", core, "--output", output, "--historical-policy", custom)
    assert result.returncode != 0
    assert "approved historical scope" in result.stderr
    assert not output.exists()
    assert historical.read_text() == "def test_new_failure(): assert False\n"


def test_snapshot_preserves_approved_history_and_detects_frozen_drift(tmp_path):
    core = tmp_path / "core"
    (core / "tests").mkdir(parents=True)
    current = core / "tests/test_current.py"
    current.write_text("def test_current(): assert True\n")
    historical = core / "tests/test_first_pool_updates 2.py"
    # Exact approved historical bytes are fixture data, never collected as tests.
    approved_bytes = (ROOT / "tests/fixtures/approved-history/test_first_pool_updates.txt").read_bytes()
    historical.write_bytes(approved_bytes)
    (core / ".env").write_text("PRIVATE_DATA_MUST_STAY_OUT\n")
    policy = tmp_path / "policy.json"
    policy.write_bytes((ROOT / "config/acceptance-scope.json").read_bytes())
    output = tmp_path / "frozen"
    result = command("snapshot", "--core-root", core, "--output", output, "--historical-policy", policy)
    assert result.returncode == 0, result.stderr
    assert historical.read_bytes() == approved_bytes
    assert not (output / "core/tests/test_first_pool_updates 2.py").exists()
    assert not (output / "core/.env").exists()
    current.write_text("a later live change\n")
    assert command("verify", "--snapshot", output).returncode == 0
    (output / "core/tests/test_current.py").write_text("unexpected frozen change\n")
    result = command("verify", "--snapshot", output)
    assert result.returncode != 0 and "frozen core inputs changed" in result.stderr


def test_finalize_requires_successful_verification_before_writing_current_status(tmp_path):
    core = tmp_path / "core"
    core.mkdir()
    (core / "README.md").write_text("core fixture\n")
    frozen = tmp_path / "frozen"
    assert command("snapshot", "--core-root", core, "--output", frozen).returncode == 0
    destination = tmp_path / "delivery"
    result = command("finalize", "--snapshot", frozen, "--destination", destination)
    assert result.returncode != 0
    assert "successful verification" in result.stderr
    assert not destination.exists()


@pytest.fixture
def completed_delivery(tmp_path):
    core = tmp_path / "core"
    core.mkdir()
    (core / "README.md").write_text("core fixture\n")
    (core / "pyproject.toml").write_text("[project]\nname='fixture'\n")
    frozen = tmp_path / "frozen"
    result = command("snapshot", "--core-root", core, "--output", frozen)
    assert result.returncode == 0, result.stderr
    destination = tmp_path / "primary"
    shutil.copytree(frozen / "monitor", destination)
    # A worked example at the serialized receipt boundary, not a real model result.
    outputs, collections = [], {}
    for name, count in (("core", 2), ("monitor", 3)):
        identifiers = [f"tests/test_example.py::test_{number}" for number in range(count)]
        nodeids_sha = hashlib.sha256(json.dumps(identifiers).encode()).hexdigest()
        value = {"tests_collected": count, "nodeids": identifiers, "nodeids_sha256": nodeids_sha}
        (frozen / f"collection-{name}.json").write_text(json.dumps(value))
        (frozen / f"collection-{name}.log").write_text("\n".join(identifiers) + "\n")
        (frozen / f"{name}.log").write_text(f"{count} passed\n")
        collections[name] = {"tests_collected": count, "nodeids_sha256": nodeids_sha}
    (frozen / "dependencies.log").write_text("offline dependency fixture\n")
    for path in sorted(frozen.glob("*.log")) + sorted(frozen.glob("collection-*.json")):
        outputs.append({"path": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                        "size": path.stat().st_size})
    (frozen / "run.json").write_text(json.dumps({"passed": True, "status": "passed",
        "inputs_sha256": hashlib.sha256((frozen / "inputs.json").read_bytes()).hexdigest(),
        "steps": [{"name": name, "returncode": 0, "status": "passed", "log": name + ".log"}
                  for name in ["dependencies", "core", "monitor"]],
        "collections": collections, "evidence_manifest_version": 1, "output_files": outputs,
        "finished_at": "2026-10-22T00:00:00+00:00"}))
    (frozen / "private-failed-attempt.log").write_text("original failure must survive\n")
    return core, frozen, destination


@pytest.fixture
def successful_small_run(tmp_path, request):
    """Real pytest subprocesses; replace only external dependency preparation."""
    monitor, core = tmp_path / "mini-monitor", tmp_path / "mini-core"
    for root in (monitor, core):
        (root / "scripts").mkdir(parents=True)
        (root / "tests").mkdir()
        outside_tests = getattr(request, "param", None) == "additional_testpath"
        paths = '["tests", "scripts/qa"]' if outside_tests else '["tests"]'
        (root / "pyproject.toml").write_text(f'[tool.pytest.ini_options]\ntestpaths = {paths}\n')
        (root / "tests/test_smoke.py").write_text("def test_smoke(): assert True\n")
        if outside_tests:
            (root / "scripts/qa").mkdir()
            (root / "scripts/qa/test_additional.py").write_text(
                "def test_first(): assert True\ndef test_second(): assert True\n")
        check = root / "scripts/check"
        check.write_text(f"#!{sys.executable}\nimport subprocess,sys\n"
            "raise SystemExit(subprocess.call([sys.executable,'-m','pytest','-q','-p','no:cacheprovider']))\n")
        check.chmod(0o755)
    shutil.copy2(ROOT / "scripts/acceptance.py", monitor / "scripts/acceptance.py")
    (monitor / "config").mkdir()
    shutil.copy2(ROOT / "config/acceptance-scope.json", monitor / "config/acceptance-scope.json")
    live_core = Path(os.environ.get("CLOUDYFAN_CORE_ROOT", ROOT.parent / "cloudyfan-jev-core"))
    shutil.copy2(live_core / "scripts/check.py", core / "scripts/check.py")
    binaries = tmp_path / "mini-bin"
    binaries.mkdir()
    uv = binaries / "uv"
    wrapper = f"#!{sys.executable}\nimport os,sys\nos.execv({sys.executable!r},[{sys.executable!r},*sys.argv[1:]])\n"
    uv.write_text(f"#!{sys.executable}\nfrom pathlib import Path\nimport sys\n"
        "root=Path(sys.argv[sys.argv.index('--project')+1])\n"
        "target=root/'.venv/bin/python'\ntarget.parent.mkdir(parents=True,exist_ok=True)\n"
        f"target.write_text({wrapper!r})\ntarget.chmod(0o755)\n")
    uv.chmod(0o755)
    frozen = tmp_path / "mini-frozen"
    script = monitor / "scripts/acceptance.py"
    env = {**os.environ, "PATH": str(binaries) + os.pathsep + os.environ["PATH"],
           "PYTHONDONTWRITEBYTECODE": "1"}
    prepared = subprocess.run([sys.executable, str(script), "snapshot", "--core-root", str(core),
        "--output", str(frozen)], env=env, capture_output=True, text=True)
    assert prepared.returncode == 0, prepared.stderr
    checked = subprocess.run([sys.executable, str(script), "run", "--snapshot", str(frozen)],
                             env=env, capture_output=True, text=True)
    assert checked.returncode == 0, checked.stderr + checked.stdout
    return frozen


@pytest.mark.parametrize("successful_small_run", ["additional_testpath"], indirect=True)
def test_collection_includes_every_configured_testpath(successful_small_run):
    frozen = successful_small_run
    run = json.loads((frozen / "run.json").read_text())
    assert run["passed"] is True
    for name in ("core", "monitor"):
        collected = json.loads((frozen / f"collection-{name}.json").read_text())
        assert collected["tests_collected"] == 3
        assert collected["nodeids"] == [
            "tests/test_smoke.py::test_smoke",
            "scripts/qa/test_additional.py::test_first",
            "scripts/qa/test_additional.py::test_second",
        ]
        assert run["collections"][name]["tests_collected"] == 3
        assert "3 passed" in (frozen / f"{name}.log").read_text()


@pytest.mark.parametrize("damage", ["missing_log", "changed_log", "missing_collection"])
def test_finalize_rejects_missing_or_changed_required_evidence(successful_small_run, tmp_path, damage):
    original = successful_small_run
    missing = "core.log" if damage == "missing_log" else "collection-monitor.json" if damage == "missing_collection" else None
    damaged = tmp_path / "incomplete-copy"
    shutil.copytree(original, damaged,
                    ignore=lambda path, names: [missing] if Path(path) == original and missing else [])
    if damage == "changed_log":
        (damaged / "core.log").write_text("replaced raw evidence\n")
    destination = tmp_path / "delivery"
    shutil.copytree(original / "monitor", destination)
    result = command("finalize", "--snapshot", damaged, "--destination", destination)
    assert result.returncode != 0
    assert "verification evidence" in result.stderr
    assert not (destination / "runtime/tooling/current.json").exists()
    assert (original / "core.log").is_file()
    assert (original / "collection-monitor.json").is_file()


def test_finalize_is_idempotent_preserves_raw_failure_and_marks_changed_inputs_stale(completed_delivery):
    core, frozen, destination = completed_delivery
    result = command("finalize", "--snapshot", frozen, "--destination", destination)
    assert result.returncode == 0, result.stderr
    first = json.loads(result.stdout)
    assert first["status"] == "verified" and first["reused_existing_delivery"] is False
    preserved = destination / "runtime/deliveries/frozen/snapshot/private-failed-attempt.log"
    assert preserved.read_text() == "original failure must survive\n"
    receipt = destination / first["receipt"]
    before = receipt.read_bytes()
    repeated = command("finalize", "--snapshot", frozen, "--destination", destination)
    assert repeated.returncode == 0, repeated.stderr
    assert json.loads(repeated.stdout)["reused_existing_delivery"] is True
    assert receipt.read_bytes() == before
    (core / "pyproject.toml").write_text("[project]\nname='later-input'\n")
    status = command("status", "--root", destination)
    assert status.returncode == 2
    assert json.loads(status.stdout)["current_core_inputs_match"] is False
    assert receipt.read_bytes() == before


def test_narrative_only_changes_check_links_without_claiming_a_new_test_run(completed_delivery):
    core, frozen, destination = completed_delivery
    (destination / "docs/tooling.md").write_text("Updated navigation: [check](../scripts/check)\n")
    result = command("finalize", "--snapshot", frozen, "--destination", destination)
    assert result.returncode == 0, result.stderr
    first = json.loads(result.stdout)
    receipt = destination / first["receipt"]
    before = receipt.read_bytes()
    assert first["status"] == "verified"
    assert first["current_monitor_inputs_match"] is False
    assert first["current_monitor_verification_inputs_match"] is True
    assert first["documentation_check"]["passed"] is True
    assert first["documentation_check"]["changed_files"]["monitor"] == ["docs/tooling.md"]
    (core / "README.md").write_text("Updated [configuration](pyproject.toml)\n")
    checked = command("status", "--root", destination)
    assert checked.returncode == 0, checked.stderr
    current = json.loads(checked.stdout)
    assert current["verified_at"] == first["verified_at"]
    assert current["collections"] == first["collections"]
    assert current["current_core_inputs_match"] is False
    assert current["current_core_verification_inputs_match"] is True
    assert current["documentation_check"]["checked_local_links"] == 2
    assert receipt.read_bytes() == before
    (core / "README.md").write_text("Broken [configuration](missing.toml)\n")
    invalid = command("status", "--root", destination)
    assert invalid.returncode == 2
    assert json.loads(invalid.stdout)["documentation_check"]["passed"] is False
    assert receipt.read_bytes() == before


@pytest.mark.parametrize("package", ["core", "monitor"])
@pytest.mark.parametrize("filename, content", [
    ("pytest.ini", "[pytest]\naddopts = --ignore=tests/test_smoke.py\n"),
    ("conftest.py", "def pytest_collection_modifyitems(items):\n    items.clear()\n"),
])
def test_root_test_configuration_is_frozen_and_invalidates_current_status(
        completed_delivery, tmp_path, package, filename, content):
    core, frozen, destination = completed_delivery
    finalized = command("finalize", "--snapshot", frozen, "--destination", destination)
    assert finalized.returncode == 0, finalized.stderr
    previous = json.loads(finalized.stdout)
    changed_root = core if package == "core" else destination
    (changed_root / filename).write_text(content)
    status = command("status", "--root", destination)
    assert status.returncode == 2
    current = json.loads(status.stdout)
    assert current[f"current_{package}_verification_inputs_match"] is False
    assert current["verified_at"] == previous["verified_at"]
    new = tmp_path / "new-frozen"
    prepared = subprocess.run([sys.executable, str(destination / "scripts/acceptance.py"),
        "snapshot", "--core-root", str(core), "--output", str(new)], capture_output=True, text=True)
    assert prepared.returncode == 0, prepared.stderr
    assert (new / package / filename).read_text() == content
    (new / package / filename).write_text("# changed after freezing\n")
    assert command("verify", "--snapshot", new).returncode != 0


def test_status_rejects_changed_preserved_evidence(completed_delivery):
    _, frozen, destination = completed_delivery
    result = command("finalize", "--snapshot", frozen, "--destination", destination)
    assert result.returncode == 0, result.stderr
    preserved = destination / "runtime/deliveries/frozen/snapshot/private-failed-attempt.log"
    preserved.write_text("modified history\n")
    result = command("status", "--root", destination)
    assert result.returncode != 0 and "preserved evidence changed" in result.stderr


def test_status_rechecks_preexisting_indexed_evidence(completed_delivery):
    _, frozen, destination = completed_delivery
    old = destination / "runtime/previous-failure.log"
    old.parent.mkdir()
    old.write_text("original failure\n")
    index = old.with_suffix(".json")
    index.write_text(json.dumps({"artifact_sha256": [{"path": "runtime/previous-failure.log",
        "sha256": hashlib.sha256(old.read_bytes()).hexdigest()}]}))
    result = command("finalize", "--snapshot", frozen, "--destination", destination,
                     "--evidence-index", index)
    assert result.returncode == 0, result.stderr
    old.write_text("replaced history\n")
    invalid = command("status", "--root", destination)
    assert invalid.returncode != 0 and "evidence hash mismatch" in invalid.stderr


def test_finalize_rejects_path_escape_without_overwriting_destination(completed_delivery, tmp_path):
    _, frozen, destination = completed_delivery
    private = tmp_path / "outside.txt"
    private.write_text("outside fixture\n")
    index = tmp_path / "index.json"
    index.write_text(json.dumps({"artifact_sha256": [{"path": "../outside.txt",
        "sha256": hashlib.sha256(private.read_bytes()).hexdigest()}]}))
    result = command("finalize", "--snapshot", frozen, "--destination", destination, "--evidence-index", index)
    assert result.returncode != 0 and "declared root" in result.stderr
    assert private.read_text() == "outside fixture\n"
    assert not (destination / "runtime/tooling/current.json").exists()


def test_monitor_commit_hook_keeps_prior_guard_and_blocks_a_failing_quick_check(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {**os.environ, "GIT_CONFIG_GLOBAL": str(tmp_path / "empty-config"), "GIT_CONFIG_NOSYSTEM": "1"}

    def git(*args):
        return subprocess.run(["git", "-C", str(repo), *args], env=env, capture_output=True, text=True)

    assert git("init", "-q").returncode == 0
    git("config", "user.name", "Tooling fixture")
    git("config", "user.email", "tooling@example.invalid")
    old = tmp_path / "prior-hooks"
    old.mkdir()
    prior = old / "pre-commit"
    prior.write_text("#!/bin/sh\necho prior >> trace\nexit 44\n")
    prior.chmod(0o755)
    git("config", "core.hooksPath", str(old))
    for name in ["scripts/hooks.py", ".githooks/pre-commit"]:
        target = repo / name
        target.parent.mkdir(exist_ok=True)
        shutil.copy2(ROOT / name, target)
    check = repo / "scripts/check"
    check.write_text("#!/bin/sh\necho quick >> trace\nexit 9\n")
    check.chmod(0o755)
    installed = subprocess.run([sys.executable, str(repo / "scripts/hooks.py"), "install"],
                               cwd=repo, env=env, capture_output=True, text=True)
    assert installed.returncode == 0, installed.stderr
    assert git("commit", "--allow-empty", "-qm", "fixture").returncode != 0
    assert (repo / "trace").read_text().splitlines() == ["prior"]
    prior.write_text("#!/bin/sh\necho prior >> trace\n")
    assert git("commit", "--allow-empty", "-qm", "fixture").returncode != 0
    assert (repo / "trace").read_text().splitlines()[-2:] == ["prior", "quick"]
    check.write_text("#!/bin/sh\necho quick >> trace\nexit 0\n")
    assert git("commit", "--allow-empty", "-qm", "fixture").returncode == 0


def test_ci_requires_an_immutable_core_commit_and_input_digest():
    values = {"CLOUDYFAN_CORE_REPOSITORY": "fixture/core", "CLOUDYFAN_CORE_REF": "main",
              "CLOUDYFAN_CORE_INPUT_SHA256": "2" * 64}
    rejected = command("ci-config", env=values)
    assert rejected.returncode != 0 and "immutable" in rejected.stderr
    values["CLOUDYFAN_CORE_REF"] = "1" * 40
    accepted = command("ci-config", env=values)
    assert accepted.returncode == 0 and json.loads(accepted.stdout)["configuration_valid"] is True


def test_snapshot_rejects_a_core_digest_mismatch_before_creating_output(tmp_path):
    core = tmp_path / "core"
    core.mkdir()
    (core / "README.md").write_text("reviewed source\n")
    output = tmp_path / "frozen"
    result = command("snapshot", "--core-root", core, "--output", output,
                     "--expected-core-sha256", "0" * 64)
    assert result.returncode != 0 and "pinned CI configuration" in result.stderr
    assert not output.exists()


def test_real_offline_sync_cannot_follow_an_inherited_environment_outside_snapshot(tmp_path):
    core = tmp_path / "core"
    (core / "scripts").mkdir(parents=True)
    (core / "pyproject.toml").write_text('[project]\nname="offline-env-fixture"\n'
        'version="0.0.0"\nrequires-python=">=3.12"\n[project.optional-dependencies]\ndev=[]\n')
    live_core = Path(os.environ.get("CLOUDYFAN_CORE_ROOT", ROOT.parent / "cloudyfan-jev-core"))
    shutil.copy2(live_core / "scripts/check.py", core / "scripts/check.py")
    env = {**os.environ, "UV_OFFLINE": "1", "UV_PYTHON_DOWNLOADS": "never"}
    env.pop("UV_PROJECT_ENVIRONMENT", None)
    locked = subprocess.run(["uv", "lock", "--offline", "--project", str(core)],
                            env=env, capture_output=True, text=True)
    assert locked.returncode == 0, locked.stderr
    frozen = tmp_path / "frozen"
    prepared = command("snapshot", "--core-root", core, "--output", frozen)
    assert prepared.returncode == 0, prepared.stderr
    unrelated = tmp_path / "unrelated-environment"
    checked = command("run", "--snapshot", frozen, env={"UV_PROJECT_ENVIRONMENT": str(unrelated)})
    report = json.loads((frozen / "run.json").read_text())
    assert report["steps"][0]["status"] == "passed", checked.stdout + checked.stderr
    assert not unrelated.exists(), checked.stdout + checked.stderr
    assert (frozen / "core/.venv/bin/python").is_file()
    # No pytest was declared for this tiny real-uv fixture; stop after isolated sync.
    assert checked.returncode != 0 and report["status"] == "failed"
    assert not (frozen / "monitor.log").exists()


@pytest.mark.parametrize("outcome", ["failure", "interrupt"])
def test_failed_core_run_keeps_original_stderr_and_cannot_finalize(tmp_path, outcome):
    core = tmp_path / "core"
    (core / "scripts").mkdir(parents=True)
    live_core = Path(os.environ.get("CLOUDYFAN_CORE_ROOT", ROOT.parent / "cloudyfan-jev-core"))
    shutil.copy2(live_core / "scripts/check.py", core / "scripts/check.py")
    entry = core / "scripts/check"
    if outcome == "failure":
        entry.write_text("#!/bin/sh\necho synthetic-core-failure >&2\nexit 7\n")
    else:
        entry.write_text(f"#!{sys.executable}\nimport time\n"
                         "print('controlled core started',flush=True)\ntime.sleep(30)\n")
    entry.chmod(0o755)
    fake_python = tmp_path / "fixture-python"
    fake_python.write_text(f"#!{sys.executable}\nimport os,sys\n"
        "assert 'PYTEST_ADDOPTS' not in os.environ\n"
        "print('tests/test_fixture.py::test_fixture')\nprint('1 test collected')\n")
    fake_python.chmod(0o755)
    binaries = tmp_path / "bin"
    binaries.mkdir()
    fake_uv = binaries / "uv"
    fake_uv.write_text(f"#!{sys.executable}\nimport pathlib,shutil,sys\n"
        "root=pathlib.Path(sys.argv[sys.argv.index('--project')+1])\n"
        "target=root/'.venv/bin/python'\ntarget.parent.mkdir(parents=True,exist_ok=True)\n"
        f"shutil.copy2({str(fake_python)!r},target)\n")
    fake_uv.chmod(0o755)
    frozen = tmp_path / "frozen"
    result = command("snapshot", "--core-root", core, "--output", frozen)
    assert result.returncode == 0, result.stderr
    env = {**os.environ, "PATH": str(binaries) + os.pathsep + os.environ["PATH"],
           "PYTEST_ADDOPTS": "-k nothing", "PYTHONDONTWRITEBYTECODE": "1"}
    if outcome == "failure":
        failed = command("run", "--snapshot", frozen, env=env)
        assert failed.returncode != 0 and "exit 7" in failed.stderr
    else:
        process = subprocess.Popen([sys.executable, str(ROOT / "scripts/acceptance.py"),
            "run", "--snapshot", str(frozen)], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            # This includes dependency preparation and two collection processes.
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                log = frozen / "core.log"
                if log.is_file() and "controlled core started" in log.read_text():
                    break
                time.sleep(0.02)
            else:
                pytest.fail("controlled core did not start")
            process.send_signal(signal.SIGINT)
            process.communicate(timeout=15)
            assert process.returncode == 130
        finally:
            if process.poll() is None:
                process.send_signal(signal.SIGINT)
                process.communicate(timeout=15)
    report = json.loads((frozen / "run.json").read_text())
    expected = "failed" if outcome == "failure" else "interrupted"
    assert report["passed"] is False and report["status"] == expected
    step = report["steps"][-1]
    assert step["name"] == "core" and step["status"] == expected
    assert step["returncode"] == (7 if outcome == "failure" else 130)
    assert step["finished_at"] and step["seconds"] >= 0
    if outcome == "failure":
        assert (frozen / "core.log").read_text() == "synthetic-core-failure\n"
    else:
        assert "controlled core started" in (frozen / "core.log").read_text()
    assert not (frozen / "monitor.log").exists()
    finalized = command("finalize", "--snapshot", frozen, "--destination", tmp_path / "delivery")
    assert finalized.returncode != 0 and "successful verification" in finalized.stderr

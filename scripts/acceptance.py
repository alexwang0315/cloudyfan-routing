#!/usr/bin/env python3
"""Freeze verification inputs and preserve a single, checkable delivery state."""
import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
from urllib.parse import unquote, urlsplit
import uuid

ROOT = Path(__file__).resolve().parents[1]
DIRECTORIES = ("src", "tests", "scripts", "docs", "examples", "config", ".github", ".githooks")
ROOT_FILES = ("pyproject.toml", "uv.lock", "README.md", "AGENTS.md", ".gitignore", ".env.example", "LICENSE",
              "pytest.ini", ".pytest.ini", "pytest.toml", ".pytest.toml", "conftest.py", "tox.ini",
              "setup.cfg", "setup.py", "uv.toml", ".python-version", "hatch.toml", "MANIFEST.in",
              "ruff.toml", ".ruff.toml")
CACHES = {"__pycache__", ".pytest_cache", ".ruff_cache", ".venv"}
# The human-approved17 path/hash pairs are a fixed scope, not caller authority.
APPROVED_HISTORY_SHA256 = "c008032760bde82ee326e327b7155cef1d74ff7210e9230875d5d626b36ddeb1"


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read(path):
    return json.loads(path.read_text())


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    # Receipts and raw test identifiers stay private even with a permissive umask.
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    with open(temporary, "x", opener=lambda p, flags: os.open(p, flags, 0o600)) as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def inside(root, relative):
    relative = Path(relative)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("artifact path must stay within its declared root")
    path = root / relative
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("artifact symlink escapes its declared root")
    return path


def inventory(root, omitted=None):
    omitted = omitted or {}
    paths = [root / p for p in ROOT_FILES if (root / p).is_file()]
    paths += [p for name in DIRECTORIES for p in (root / name).rglob("*")
              if p.is_file() and not CACHES.intersection(p.relative_to(root).parts)]
    files, excluded = [], []
    for path in sorted(set(paths)):
        if path.is_symlink():
            raise ValueError("source symlinks must be reviewed before freezing")
        relative = path.relative_to(root).as_posix()
        sha = digest(path)
        if relative.startswith("tests/") and re.search(r" \d+\.py$", path.name):
            if omitted.get(relative) != sha:
                raise ValueError(f"unapproved historical test: {relative}")
            excluded.append({"path": relative, "sha256": sha})
            continue
        files.append({"path": relative, "sha256": sha, "size": path.stat().st_size,
                      "mode": stat.S_IMODE(path.stat().st_mode)})
    return {"files": files, "excluded_historical_tests": excluded}


def validate_historical_policy(policy):
    rows = policy.get("historical_tests")
    if not isinstance(rows, list) or len(rows) != 17:
        raise ValueError("policy differs from the approved historical scope")
    canonical = json.dumps(sorted(rows, key=lambda item: item["path"]), sort_keys=True, separators=(",", ":"))
    if hashlib.sha256(canonical.encode()).hexdigest() != APPROVED_HISTORY_SHA256:
        raise ValueError("policy differs from the approved historical scope; a file cannot grant new exclusions")


def snapshot(core_root, output, policy_path, expected_core_sha256=None):
    policy = read(policy_path)
    validate_historical_policy(policy)
    omitted = {item["path"]: item["sha256"] for item in policy["historical_tests"]}
    # Validate all inputs before making a partial output directory.
    initial = {"core": inventory(core_root, omitted), "monitor": inventory(ROOT)}
    core_sha256 = hashlib.sha256(json.dumps(initial["core"]["files"], sort_keys=True).encode()).hexdigest()
    if expected_core_sha256 and expected_core_sha256 != core_sha256:
        raise ValueError("core input digest differs from the pinned CI configuration")
    if output.exists():
        raise ValueError("snapshot output already exists; use a new run directory")
    if output.resolve().is_relative_to(core_root.resolve()):
        raise ValueError("snapshot must be outside the live core checkout")
    output.mkdir(parents=True, mode=0o700)
    for name, source in (("core", core_root), ("monitor", ROOT)):
        for item in initial[name]["files"]:
            destination = output / name / item["path"]
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source / item["path"], destination)
    if initial != {"core": inventory(core_root, omitted), "monitor": inventory(ROOT)}:
        raise ValueError("live inputs changed during snapshot; partial output retained")
    manifest = {"schema_version": 1, "created_at": now(), "id": output.name,
                "source_roots": {"core": str(core_root), "monitor": str(ROOT)},
                "inputs": initial, "core_input_sha256": core_sha256, "historical_scope_policy": policy,
                "scope": "Frozen local verification inputs; no production or paid authority"}
    write(output / "inputs.json", manifest)
    verify(output)
    return {"snapshot": str(output), "manifest_sha256": digest(output / "inputs.json"),
            "core_input_sha256": core_sha256,
            "core_files": len(initial["core"]["files"]),
            "monitor_files": len(initial["monitor"]["files"]),
            "preserved_historical_tests": len(initial["core"]["excluded_historical_tests"])}


def verify(output):
    manifest = read(output / "inputs.json")
    validate_historical_policy(manifest["historical_scope_policy"])
    for name in ("core", "monitor"):
        actual = inventory(output / name)
        if actual["files"] != manifest["inputs"][name]["files"]:
            raise ValueError(f"frozen {name} inputs changed")
    return manifest


def check_support(core):
    spec = importlib.util.spec_from_file_location("cloudyfan_core_check", core / "scripts/check.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def clean_environment(core, monitor):
    env = dict(os.environ)
    for key in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "PYTEST_ADDOPTS", "PYTEST_PLUGINS",
                "UV_NO_SYNC", "UV_FROZEN", "UV_NO_DEV"):
        env.pop(key, None)
    env.update(CLOUDYFAN_CORE_ROOT=str(core), PYTHONDONTWRITEBYTECODE="1",
               PYTHONPATH=str(monitor / "src") + os.pathsep + str(core / "src"),
               UV_PROJECT_ENVIRONMENT=str(core / ".venv"),
               UV_OFFLINE="1", UV_PYTHON_DOWNLOADS="never")
    return env


def collect(python, root, env, path):
    with open(path, "xb", opener=lambda p, flags: os.open(p, flags, 0o600)) as stream:
        result = subprocess.run([str(python), "-m", "pytest", "--collect-only", "-q", "--color=no",
                                 "-p", "no:cacheprovider"], cwd=root, env=env,
                                stdout=stream, stderr=subprocess.STDOUT, timeout=120)
    if result.returncode:
        raise ValueError(f"collection failed; private log: {path}")
    text = path.read_text(errors="replace")
    identifiers = [line for line in text.splitlines() if "::" in line and not line[:1].isspace()]
    if not identifiers:
        raise ValueError("collection produced no test identifiers")
    summary = re.search(r"(?m)^(\d+) tests? collected(?: in [^\n]*)?$", text)
    if summary is None or len(identifiers) != int(summary[1]):
        raise ValueError("collection identifiers do not match pytest's collected count")
    value = {"tests_collected": len(identifiers), "nodeids": identifiers,
             "nodeids_sha256": hashlib.sha256(json.dumps(identifiers).encode()).hexdigest()}
    write(path.with_suffix(".json"), value)
    return {"tests_collected": len(identifiers), "nodeids_sha256": value["nodeids_sha256"],
            "manifest": str(path.with_suffix(".json"))}


def evidence_entries(output):
    # Walk without following links, so a planted link never redirects a read.
    files, links, fifos = [], [], []
    for directory, folders, names in os.walk(output):
        folders[:] = [name for name in folders if name not in CACHES]
        for name in folders + names:
            path = Path(directory) / name
            if path.is_symlink():
                links.append(path)
            elif name in names and stat.S_ISFIFO(path.lstat().st_mode):
                fifos.append(path)
            elif name in names and path.is_file():
                files.append(path)
    return sorted(files), sorted(links), sorted(fifos)


def symlink_record(output, path):
    # Human decision (2026-10-29): security tests leave planted links as evidence.
    # Seal the link itself and its in-snapshot target; never open what it names.
    target = os.readlink(path)
    location = Path(os.path.normpath(target if os.path.isabs(target) else path.parent / target))
    if ".." in Path(target).parts or not location.is_relative_to(output):
        raise ValueError("verification evidence symlink must stay within the snapshot")
    return {"path": path.relative_to(output).as_posix(), "target": location.relative_to(output).as_posix()}


def output_symlinks(output):
    return [symlink_record(output, path) for path in evidence_entries(output)[1]]


def output_special_files(output):
    # Human decision (2026-10-29): a planted FIFO is sealed by type and path; opening
    # it could block, so its presence is the only evidence recorded.
    return [{"path": path.relative_to(output).as_posix(), "type": "fifo"}
            for path in evidence_entries(output)[2]]


def output_inventory(output, manifest):
    source_files = {f"{name}/{item['path']}" for name in ("core", "monitor")
                    for item in manifest["inputs"][name]["files"]}
    records = []
    for path in evidence_entries(output)[0]:
        relative = path.relative_to(output)
        if relative.as_posix() in source_files or relative.as_posix() in {"inputs.json", "run.json"}:
            continue
        records.append({"path": relative.as_posix(), "sha256": digest(path), "size": path.stat().st_size})
    return records


def validate_outputs(output, report, live=True):
    records = report.get("output_files")
    if report.get("evidence_manifest_version") != 1 or not isinstance(records, list) or not records:
        raise ValueError("verification evidence seal is missing; a fresh run is required")
    links = report.get("output_symlinks", [])
    if not isinstance(links, list) or (live and output_symlinks(output) != links):
        raise ValueError("verification evidence symlinks changed since sealing")
    special = report.get("output_special_files", [])
    if not isinstance(special, list) or (live and output_special_files(output) != special):
        raise ValueError("verification evidence special files changed since sealing")
    required = {f"{name}.log" for name in ("dependencies", "core", "monitor")}
    required |= {f"collection-{name}.{suffix}" for name in ("core", "monitor") for suffix in ("log", "json")}
    names = set()
    for item in records:
        relative = item["path"]
        if relative in names:
            raise ValueError("verification evidence seal contains duplicate paths")
        names.add(relative)
        path = inside(output, relative)
        if (not path.is_file() or path.is_symlink() or path.stat().st_size != item["size"]
                or digest(path) != item["sha256"]):
            raise ValueError(f"verification evidence missing or changed: {relative}")
    if not required.issubset(names):
        raise ValueError("verification evidence seal omits required logs or collection manifests")
    for step in report["steps"]:
        if step.get("log") != f"{step['name']}.log":
            raise ValueError("verification evidence log declaration differs from the required step")
    for name in ("core", "monitor"):
        collection = read(output / f"collection-{name}.json")
        declared = report["collections"][name]
        identifiers = collection.get("nodeids")
        if (not isinstance(identifiers, list) or len(identifiers) != declared["tests_collected"]
                or collection.get("tests_collected") != len(identifiers)
                or collection.get("nodeids_sha256") != declared["nodeids_sha256"]
                or hashlib.sha256(json.dumps(identifiers).encode()).hexdigest() != declared["nodeids_sha256"]):
            raise ValueError(f"verification evidence collection differs from its receipt: {name}")
    return len(records)


def run(output):
    manifest = verify(output)
    if (output / "run.json").exists():
        raise ValueError("snapshot already has a run; preserve it and create a new snapshot")
    core, monitor = output / "core", output / "monitor"
    env = clean_environment(core, monitor)
    uv = shutil.which("uv")
    if not uv:
        raise ValueError("uv is required; no network or global-package fallback")
    support = check_support(core)
    report = {"schema_version": 2, "started_at": now(), "status": "running", "passed": False,
              "inputs_sha256": digest(output / "inputs.json"), "steps": [], "collections": {}}
    write(output / "run.json", report)
    commands = [
        ("dependencies", [uv, "sync", "--project", str(core), "--locked", "--offline",
                          "--extra", "dev", "--no-python-downloads"], core),
        ("core", [str(core / "scripts/check"), "--integration"], core),
        ("monitor", [str(core / ".venv/bin/python"), str(monitor / "scripts/check")], monitor),
    ]
    try:
        for name, command, cwd in commands:
            if name == "core":
                for label, root in (("core", core), ("monitor", monitor)):
                    report["collections"][label] = collect(core / ".venv/bin/python", root, env,
                                                          output / f"collection-{label}.log")
                write(output / "run.json", report)
            code = support.run_step(report, command=command, cwd=cwd, env=env,
                                    log_path=output / f"{name}.log", receipt_path=output / "run.json",
                                    step_fields={"name": name})
            if report["steps"][-1]["status"] == "interrupted":
                raise KeyboardInterrupt
            if code:
                raise ValueError(f"{name} check failed with exit {code}; original logs retained")
        verify(output)
        for name in ("core", "monitor"):
            log = (output / f"{name}.log").read_text(errors="replace")
            matches = re.findall(r"\b(\d+) passed\b", log)
            if not matches or int(matches[-1]) != report["collections"][name]["tests_collected"]:
                raise ValueError(f"{name} test result does not match the collected scope")
        report.update(evidence_manifest_version=1, output_files=output_inventory(output, manifest),
                      output_symlinks=output_symlinks(output),
                      output_special_files=output_special_files(output))
        validate_outputs(output, report)
        report.update(status="passed", passed=True)
    except KeyboardInterrupt:
        report.update(status="interrupted", error="verification interrupted", passed=False)
        raise
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        report.update(status="failed", error=str(error), passed=False)
        raise
    finally:
        report["finished_at"] = now()
        write(output / "run.json", report)
    return {"snapshot": str(output), "passed": True, "collections": report["collections"]}


def validate_evidence(root, index_path):
    if index_path is None:
        return []
    index = read(index_path)
    records = index.get("artifact_sha256", [])
    if not isinstance(records, list):
        raise ValueError("evidence index requires an artifact_sha256 list")
    for item in records:
        path = inside(root, item["path"])
        if not path.is_file() or digest(path) != item["sha256"]:
            raise ValueError(f"evidence hash mismatch: {item['path']}")
    return records


def verification_files(files):
    # Narrative edits do not invalidate executable checks. JSON records, examples,
    # policies and build inputs remain verification inputs, including under docs/.
    return [item for item in files if not item["path"].endswith(".md")]


def documentation_check(root, before, current):
    old = {item["path"]: item for item in before if item["path"].endswith(".md")}
    new = {item["path"]: item for item in current if item["path"].endswith(".md")}
    changed = sorted(name for name in old.keys() | new.keys() if old.get(name) != new.get(name))
    removed = {(root / name).resolve() for name in old.keys() - new.keys()}
    checked, errors = 0, []
    for name in new:
        for target in re.findall(r"!?\[[^\]\n]*\]\(([^)\n]+)\)", (root / name).read_text()):
            target = target.strip()
            if not target:
                continue
            target = target[1:target.index(">")] if target.startswith("<") and ">" in target else target.split()[0]
            url = urlsplit(target)
            if url.scheme or url.netloc or not url.path:
                continue
            path = (root / name).parent / unquote(url.path)
            if name not in changed and path.resolve() not in removed:
                continue
            checked += 1
            if not path.exists():
                errors.append({"file": name, "missing_local_target": target})
    return {"passed": not errors, "changed_files": changed, "checked_local_links": checked,
            "errors": errors, "scope": "Changed Markdown local file links; external URLs and anchors are not checked"}


def delivery_status(root):
    pointer = read(root / "runtime/tooling/current.json")
    receipt_path = inside(root, pointer["receipt"])
    if digest(receipt_path) != pointer["receipt_sha256"]:
        raise ValueError("delivery receipt changed")
    receipt = read(receipt_path)
    validate_historical_policy(receipt["historical_policy"])
    indexed_evidence = validate_evidence(root, receipt_path)
    for item in receipt["preserved_files"]:
        path = inside(root, item["path"])
        if not path.is_file() or digest(path) != item["sha256"]:
            raise ValueError(f"preserved evidence changed: {item['path']}")
    evidence_sealed = receipt.get("evidence_manifest_version") == 1
    sealed_run = read(receipt_path.parent / "snapshot/run.json") if evidence_sealed else {}
    # Links and FIFOs are preserved as receipt records, not recreated in the delivery copy.
    sealed_count = validate_outputs(receipt_path.parent / "snapshot", sealed_run,
                                    live=False) if evidence_sealed else 0
    sealed_links = sealed_run.get("output_symlinks", [])
    if receipt.get("preserved_symlinks", []) != sealed_links:
        raise ValueError("preserved evidence symlink records changed")
    sealed_special = sealed_run.get("output_special_files", [])
    if receipt.get("preserved_special_files", []) != sealed_special:
        raise ValueError("preserved evidence special file records changed")
    monitor_files = inventory(root)["files"]
    monitor_matches = monitor_files == receipt["monitor_inputs"]
    monitor_checks_match = verification_files(monitor_files) == verification_files(receipt["monitor_inputs"])
    monitor_docs = documentation_check(root, receipt["monitor_inputs"], monitor_files)
    omitted = {item["path"]: item["sha256"] for item in receipt["historical_policy"]["historical_tests"]}
    core_root = Path(receipt["original_core_root"])
    try:
        core_inputs = inventory(core_root, omitted)
        core_matches = core_inputs == receipt["original_core_inputs"]
        core_checks_match = (verification_files(core_inputs["files"])
                             == verification_files(receipt["original_core_inputs"]["files"]))
        core_docs = documentation_check(core_root, receipt["original_core_inputs"]["files"], core_inputs["files"])
    except (OSError, ValueError):
        core_matches, core_checks_match = False, False
        core_docs = {"passed": False, "changed_files": [], "checked_local_links": 0,
                     "errors": ["Current core inputs could not be validated"]}
    docs = {"passed": monitor_docs["passed"] and core_docs["passed"],
            "changed_files": {"monitor": monitor_docs["changed_files"], "core": core_docs["changed_files"]},
            "checked_local_links": monitor_docs["checked_local_links"] + core_docs["checked_local_links"],
            "errors": {"monitor": monitor_docs["errors"], "core": core_docs["errors"]},
            "scope": monitor_docs["scope"], "checked_at": now(), "full_suite_rerun": False}
    valid = monitor_checks_match and core_checks_match and docs["passed"] and evidence_sealed
    return {"schema_version": 1, "status": "verified" if valid else "stale",
            "scope": "Local offline verification only; no paid or production qualification",
            "snapshot_id": receipt["snapshot_id"], "verified_at": receipt["verified_at"],
            "current_monitor_inputs_match": monitor_matches, "current_core_inputs_match": core_matches,
            "current_monitor_verification_inputs_match": monitor_checks_match,
            "current_core_verification_inputs_match": core_checks_match, "documentation_check": docs,
            "preserved_files_verified": len(receipt["preserved_files"]),
            "historical_evidence_hashes_verified": len(indexed_evidence),
            "verification_evidence_sealed": evidence_sealed, "sealed_output_files_verified": sealed_count,
            "sealed_output_symlinks_verified": len(sealed_links),
            "sealed_output_special_files_verified": len(sealed_special),
            "collections": receipt["collections"], "receipt": pointer["receipt"],
            "tracker_observation": receipt.get("tracker_observation"),
            "remaining_actions": ["Remote CI execution has not been observed; no source push performed"]}


def finalize(output, destination, evidence_index=None, tracker_receipt=None):
    manifest = verify(output)
    run_path = output / "run.json"
    if not run_path.is_file():
        raise ValueError("successful verification is required before finalization")
    result = read(run_path)
    required = ["dependencies", "core", "monitor"]
    if (result.get("passed") is not True or result.get("status") != "passed"
            or result.get("inputs_sha256") != digest(output / "inputs.json")
            or [s.get("name") for s in result.get("steps", [])] != required
            or any(s.get("returncode") != 0 or s.get("status") != "passed" for s in result["steps"])
            or not all(result.get("collections", {}).get(n, {}).get("tests_collected", 0) > 0
                       for n in ("core", "monitor"))):
        raise ValueError("successful verification matching these inputs is required")
    validate_outputs(output, result)
    current_files = inventory(destination)["files"] if destination.exists() else []
    if (not destination.exists()
            or verification_files(current_files) != verification_files(manifest["inputs"]["monitor"]["files"])):
        raise ValueError("destination inputs differ; no source files will be overwritten")
    docs = documentation_check(destination, manifest["inputs"]["monitor"]["files"], current_files)
    if not docs["passed"]:
        raise ValueError("changed destination documentation has missing local link targets")
    evidence = validate_evidence(destination, evidence_index)
    target = inside(destination, "runtime/deliveries/" + manifest["id"])
    if target.resolve().is_relative_to(output) or output.is_relative_to(target.resolve()):
        raise ValueError("delivery preservation must stay outside the frozen snapshot")
    receipt_path = target / "delivery.json"
    reused = receipt_path.exists()
    if reused:
        receipt = read(receipt_path)
        if receipt["run_sha256"] != digest(run_path) or receipt["inputs_sha256"] != digest(output / "inputs.json"):
            raise ValueError("existing immutable delivery differs; refusing overwrite")
        if evidence_index and receipt.get("artifact_sha256", []) != evidence:
            raise ValueError("existing immutable historical evidence index differs; refusing overwrite")
    else:
        paths = evidence_entries(output)[0]
        target.mkdir(parents=True, mode=0o700)
        preserved = []
        for source in sorted(paths):
            if source.is_symlink():
                raise ValueError("unreviewed runtime symlink cannot be preserved")
            relative = source.relative_to(output)
            copy = target / "snapshot" / relative
            sha = digest(source)
            if copy.exists() and digest(copy) != sha:
                raise ValueError("existing private artifact differs; refusing overwrite")
            if not copy.exists():
                copy.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, copy)
            if digest(copy) != sha or digest(source) != sha:
                raise ValueError("artifact changed while preserving delivery")
            preserved.append({"path": str(copy.relative_to(destination)), "sha256": sha,
                              "size": copy.stat().st_size})
        tracker = None
        if tracker_receipt:
            tracker = read(tracker_receipt)
            write(target / "tracker-observation.json", tracker)
            copied_tracker = target / "tracker-observation.json"
            preserved.append({"path": str(copied_tracker.relative_to(destination)),
                              "sha256": digest(copied_tracker), "size": copied_tracker.stat().st_size})
        verify(output)
        if output_symlinks(output) != result.get("output_symlinks", []):
            raise ValueError("verification evidence symlinks changed while preserving delivery")
        if output_special_files(output) != result.get("output_special_files", []):
            raise ValueError("verification evidence special files changed while preserving delivery")
        receipt = {"schema_version": 2, "snapshot_id": manifest["id"], "verified_at": result.get("finished_at"),
                   "evidence_manifest_version": 1,
                   "inputs_sha256": digest(output / "inputs.json"), "run_sha256": digest(run_path),
                   "monitor_inputs": manifest["inputs"]["monitor"]["files"],
                   "original_core_root": manifest["source_roots"]["core"],
                   "original_core_inputs": manifest["inputs"]["core"],
                   "historical_policy": manifest["historical_scope_policy"],
                   "collections": result["collections"], "preserved_files": preserved,
                   "preserved_symlinks": result.get("output_symlinks", []),
                   "preserved_special_files": result.get("output_special_files", []),
                   "artifact_sha256": evidence,
                   "historical_evidence_hashes_verified": len(evidence), "tracker_observation": tracker,
                   "source_files_overwritten": False, "data_deleted": False}
        write(receipt_path, receipt)
    for item in receipt["preserved_files"]:
        if digest(inside(destination, item["path"])) != item["sha256"]:
            raise ValueError("preserved delivery changed; refusing to publish current status")
    write(destination / "runtime/tooling/current.json",
          {"schema_version": 1, "receipt": str(receipt_path.relative_to(destination)),
           "receipt_sha256": digest(receipt_path)})
    status = delivery_status(destination)
    return {**status, "reused_existing_delivery": reused, "data_deleted": False}


def ci_config():
    values = {name: os.environ.get("CLOUDYFAN_" + name, "")
              for name in ("CORE_REPOSITORY", "CORE_REF", "CORE_INPUT_SHA256")}
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", values["CORE_REPOSITORY"]):
        raise ValueError("CI requires CLOUDYFAN_CORE_REPOSITORY=owner/repository")
    if not re.fullmatch(r"[0-9a-f]{40}", values["CORE_REF"]):
        raise ValueError("CI requires an immutable40-character CLOUDYFAN_CORE_REF commit")
    if not re.fullmatch(r"[0-9a-f]{64}", values["CORE_INPUT_SHA256"]):
        raise ValueError("CI requires CLOUDYFAN_CORE_INPUT_SHA256 from that exact checkout")
    return {"configuration_valid": True, "immutable_core_ref": values["CORE_REF"],
            "core_input_sha256": values["CORE_INPUT_SHA256"]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("snapshot", help="freeze current inputs, retaining approved history separately")
    prepare.add_argument("--core-root", type=Path, default=ROOT.parent / "cloudyfan-mrc")
    prepare.add_argument("--output", type=Path, required=True)
    prepare.add_argument("--historical-policy", type=Path, default=ROOT / "config/acceptance-scope.json",
                         help="alternate location of the unchanged approved17-row policy")
    prepare.add_argument("--expected-core-sha256", help="CI must match the independently pinned core input digest")
    for name in ("verify", "run"):
        item = commands.add_parser(name)
        item.add_argument("--snapshot", type=Path, required=True)
    finish = commands.add_parser("finalize", help="validate and preserve a finished run; never overwrite source or delete data")
    finish.add_argument("--snapshot", type=Path, required=True)
    finish.add_argument("--destination", type=Path, default=ROOT)
    finish.add_argument("--evidence-index", type=Path)
    finish.add_argument("--tracker-receipt", type=Path)
    current = commands.add_parser("status", help="check the single current pointer against source and evidence hashes")
    current.add_argument("--root", type=Path, default=ROOT)
    commands.add_parser("ci-config", help="validate CI repository, immutable core commit and input digest")
    args = parser.parse_args(argv)
    try:
        if args.command == "snapshot":
            result = snapshot(args.core_root.resolve(), args.output.resolve(), args.historical_policy,
                              args.expected_core_sha256)
        elif args.command == "verify":
            value = verify(args.snapshot.resolve())
            result = {"verified": True, "snapshot_id": value["id"]}
        elif args.command == "run":
            result = run(args.snapshot.resolve())
        elif args.command == "finalize":
            result = finalize(args.snapshot.resolve(), args.destination.resolve(), args.evidence_index, args.tracker_receipt)
        elif args.command == "status":
            result = delivery_status(args.root.resolve())
        else:
            result = ci_config()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 2 if result.get("status") == "stale" else 0
    except KeyboardInterrupt:
        print("Verification interrupted; running/failure receipts retained.", file=sys.stderr)
        return 130
    except (OSError, ValueError, KeyError, subprocess.TimeoutExpired) as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

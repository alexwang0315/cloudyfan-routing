#!/usr/bin/env python3
"""Run desktop identity acceptance against a private, synthetic loopback service.

Run from the checkout with its src/ and the approved core src/ on PYTHONPATH.
The browser receives disposable fixture credentials on stdin only. No credential,
request body, answer, source GET or real model request is written to the report.
"""
import argparse
from contextlib import ExitStack
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import threading
from unittest.mock import patch
import uuid

import httpx

from cloudyfan_monitor.demo import CATEGORY
from cloudyfan_monitor.integration import build_local_integration

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from test_local_http_integration import (  # noqa: E402
    Clock, LoopbackEvidence, QuietHandler, ThreadedServer, configure_loopback_network,
)


def projection(view):
    return {"summary": view["summary"],
            "clients": [{"id": row["id"], "stats": row["stats"]} for row in view["clients"]],
            "tasks": [{key: row.get(key) for key in ("client", "run", "status", "observation")}
                      for row in view["tasks"]],
            "ledger": {key: view["ledger"][key] for key in
                       ("known_usd", "by_client_known_usd", "unattributed_known_usd", "unknown_events")}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path,
                        default=ROOT / "runtime" / "identity-desktop" / uuid.uuid4().hex[:12])
    options = parser.parse_args()
    output = options.output.resolve()
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    report = {"schema_version": 1, "scope": "synthetic identity; actual loopback HTTP and desktop Chrome",
              "viewport": {"width": 1280, "height": 800}, "http_responses": [], "verified": False}
    network = LoopbackEvidence()
    with ExitStack() as stack:
        server = ThreadedServer(("127.0.0.1", 0), QuietHandler)
        stack.callback(server.server_close)
        origin = f"http://127.0.0.1:{server.server_port}"
        network.ports.add(server.server_port)
        configure_loopback_network(network, lambda obj, name, value:
                                   stack.enter_context(patch.object(obj, name, value)))
        clock = Clock()
        bundle = build_local_integration(output / "fixture", origin=origin, clock=clock, seed=False)
        bundle.upstream.fail_tiers = set()
        server.set_app(bundle.app)
        thread = threading.Thread(target=server.serve_forever,
                                  kwargs={"poll_interval": .01}, daemon=True)
        thread.start()
        stack.callback(thread.join, 2)
        stack.callback(server.shutdown)

        def record(response):
            report["http_responses"].append({"method": response.request.method,
                "path": response.request.url.path, "status": response.status_code})

        def connect(token):
            return stack.enter_context(httpx.Client(base_url=origin,
                headers={"Authorization": "Bearer " + token}, trust_env=False,
                timeout=10, event_hooks={"response": [record]}))

        original = connect(bundle.credentials["studio"].token)
        agent = connect(bundle.credentials["agent"].token)
        operator = connect(bundle.operator_token)
        for connection, name in ((original, "identity-desktop-studio"), (agent, "identity-desktop-agent")):
            created = connection.post("/v1/tasks", json={"run_id": name, "category": CATEGORY,
                "prompt": "Return only the integer sum of 1 and 1."})
            assert created.status_code == 200 and created.json()["status"] == "passed"
        before = operator.get("/monitor/snapshot").json()
        assert before["summary"]["passed"] == before["summary"]["tasks"] == 2
        report["before"] = projection(before)
        assert len(bundle.upstream.calls) == 2

        # IPC requests are fixture control, never product endpoints. Tokens enter
        # the child process on a pipe and are not included in saved evidence.
        process = stack.enter_context(subprocess.Popen(
            ["node", str(ROOT / "scripts" / "qa_identity_desktop.cjs"), origin, str(output)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
        process.stdin.write(json.dumps({"client_token": bundle.credentials["studio"].token}) + "\n")
        process.stdin.flush()
        try:
            for line in process.stdout:
                command = json.loads(line)
                if command != {"action": "rotate"}:
                    raise AssertionError("Unexpected desktop fixture action")
                replacement = bundle.identity.rotate(bundle.credentials["studio"].id,
                                                       expires_at=clock.now + 86400)
                assert original.get("/v1/tasks/identity-desktop-studio/result").status_code == 401
                current = connect(replacement.token)
                result = current.get("/v1/tasks/identity-desktop-studio/result")
                assert result.status_code == 200 and result.json()["answer"] == "2"
                repeat = current.post("/v1/tasks", json={"run_id": "identity-desktop-studio",
                    "category": CATEGORY, "prompt": "Return only the integer sum of 1 and 1."})
                assert repeat.status_code == 200 and repeat.json()["status"] == "passed"
                after = operator.get("/monitor/snapshot").json()
                assert projection(after) == projection(before)
                assert len(bundle.upstream.calls) == 2
                report["after"] = projection(after)
                process.stdin.write(json.dumps({"rotated": True, "mock_calls": 2}) + "\n")
                process.stdin.flush()
            assert process.wait(timeout=45) == 0, "Desktop acceptance failed; see browser-report.json"
            report["browser"] = json.loads((output / "browser-report.json").read_text())
            assert report["browser"]["verified"] is True
            assert not network.blocked
            assert network.connected
            report["verified"] = True
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=10)
            # Error text is intentionally not copied: browser or library errors
            # could contain unredacted request data.
            process.stderr.read()
            report["allowed_loopback_ports"] = sorted(network.ports)
            report["socket_attempts"] = [{"host": item[0], "port": item[1]} for item in network.connected]
            report["blocked_socket_attempts"] = [str(item) for item in network.blocked]
            report["mock_upstream_call_count"] = len(bundle.upstream.calls)
            report["source_sha256"] = {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in sorted((ROOT / "src").rglob("*")) if path.is_file() and "__pycache__" not in path.parts}
            (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"Identity desktop acceptance passed: {output / 'report.json'}")


if __name__ == "__main__":
    main()

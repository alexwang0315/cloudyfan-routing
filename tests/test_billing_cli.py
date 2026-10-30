"""CLI wiring uses its real fixture and HTTP APIs, with no listening socket."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
HARNESS = r'''
import json, os, socket, wsgiref.simple_server
import httpx

def forbidden_socket(*args, **kwargs):
    raise AssertionError("CLI test must not open a network connection")
socket.socket.connect = forbidden_socket
socket.socket.bind = forbidden_socket
socket.create_connection = forbidden_socket
original_client = httpx.Client
resources, requests, observed = [], [], {}
def unexpected_billing_request(request):
    requests.append(request)
    raise AssertionError("Preview startup or dashboard read contacted billing upstream")
class TrackingClient(original_client):
    def __init__(self, *args, **kwargs):
        billing = "transport" not in kwargs
        if billing:
            kwargs["transport"] = httpx.MockTransport(unexpected_billing_request)
        super().__init__(*args, **kwargs)
        if billing:
            resources.append(self)
httpx.Client = TrackingClient
class Server:
    def __init__(self, app):
        self.app = app
    def __enter__(self):
        return self
    def __exit__(self, *exc):
        observed["server_closed"] = True
    def serve_forever(self):
        with original_client(transport=httpx.WSGITransport(self.app), base_url=self.app.origin,
                headers={"Authorization":"Bearer " + self.app.admin_token}) as admin:
            response = admin.get("/monitor/billing")
            assert response.status_code == 200
            result = response.json()
            observed.update(demo=result["demo"], configured=result["source"]["configured"],
                            initial_entries=result["entries"], cached_account=result["source"]["account"])
        if os.environ.get("BILLING_TEST_INTERRUPT") == "1":
            raise KeyboardInterrupt
def server_factory(host, port, app, **kwargs):
    assert host == "127.0.0.1"
    return Server(app)
wsgiref.simple_server.make_server = server_factory
from cloudyfan_monitor.__main__ import main
try:
    main()
except KeyboardInterrupt:
    observed["interrupted"] = True
finally:
    observed.update(billing_clients=len(resources), billing_clients_closed=all(c.is_closed for c in resources),
                    upstream_requests=len(requests))
    print("BILLING_CLI_PROBE " + json.dumps(observed), flush=True)
'''


def invoke(tmp_path, options, *, interrupt=False):
    env = dict(PATH=os.defpath, PYTHONPATH=os.pathsep.join([str(ROOT / "src"),
                    str(ROOT.parent / "cloudyfan-mrc/src")]),
               BILLING_TEST_INTERRUPT="1" if interrupt else "0",
               OPENROUTER_BILLING_API_KEY="invalid disabled environment value")
    return subprocess.run([sys.executable, "-c", HARNESS, "--demo", "--data", str(tmp_path / "preview"),
                           *options], cwd=ROOT, env=env, capture_output=True, text=True, timeout=30)


@pytest.mark.parametrize("interrupt,legacy", [(False, False), (True, True)])
def test_explicit_billing_is_injected_without_startup_get_and_closes_on_exit(tmp_path, interrupt, legacy):
    keys = tmp_path / "keys.env"
    keys.write_text('OPENROUTER_BILLING_API_KEY="billing-key"\n')
    result = invoke(tmp_path, ["--openrouter-billing", "--billing-key-file", str(keys),
                              *(["--legacy-demo"] if legacy else [])], interrupt=interrupt)
    assert result.returncode == 0, result.stderr
    payload = json.loads(next(line.removeprefix("BILLING_CLI_PROBE ") for line in result.stdout.splitlines()
                              if line.startswith("BILLING_CLI_PROBE ")))
    assert payload["demo"] is payload["configured"] is True
    assert payload["initial_entries"] == [] and payload["cached_account"] is None
    assert payload["upstream_requests"] == 0
    assert payload["billing_clients"] == 1 and payload["billing_clients_closed"] is True
    assert payload["server_closed"] is True
    assert payload.get("interrupted", False) is interrupt
    assert "billing-key" not in result.stdout + result.stderr


def test_billing_disabled_ignores_credentials_and_opens_no_client(tmp_path):
    result = invoke(tmp_path, ["--legacy-demo"])
    assert result.returncode == 0, result.stderr
    payload = json.loads(next(line.removeprefix("BILLING_CLI_PROBE ") for line in result.stdout.splitlines()
                              if line.startswith("BILLING_CLI_PROBE ")))
    assert payload["demo"] is True and payload["configured"] is False
    assert payload["upstream_requests"] == payload["billing_clients"] == 0
    assert payload["server_closed"] is True
    assert "invalid disabled environment value" not in result.stdout + result.stderr


@pytest.mark.parametrize("enabled,body,code", [
    (False, "OPENROUTER_API_KEY=secret-one\n", "billing_key_file_requires_openrouter_billing"),
    (True, "OPENROUTER_API_KEY=secret-one\nOPENROUTER_API_KEY=secret-two\n", "billing_key_duplicate"),
    (True, "OTHER_SERVICE_KEY=secret-one\n", "billing_key_missing"),
])
def test_invalid_cli_configuration_exits_without_startup_or_secret_output(tmp_path, enabled, body, code):
    keys = tmp_path / "keys.env"
    keys.write_text(body)
    result = invoke(tmp_path, [*(["--openrouter-billing"] if enabled else []), "--billing-key-file", str(keys)])
    assert result.returncode == 2
    assert code in result.stderr
    assert all(secret not in result.stdout + result.stderr for secret in
               ("secret-one", "secret-two", "invalid disabled environment value"))
    payload = json.loads(next(line.removeprefix("BILLING_CLI_PROBE ") for line in result.stdout.splitlines()
                              if line.startswith("BILLING_CLI_PROBE ")))
    assert payload == {"billing_clients": 0, "billing_clients_closed": True, "upstream_requests": 0}
    assert not (tmp_path / "preview").exists()

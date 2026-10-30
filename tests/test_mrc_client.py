import json

import httpx
import pytest

from cloudyfan_monitor import mrc_client
from cloudyfan_monitor.demo import CATEGORY, TOKENS, build_demo
from cloudyfan_monitor.integration import build_local_integration
from cloudyfan_monitor.mrc_client import MrcClient, MrcError, SYNTHETIC_NOTICE


def _client(app, token):
    return MrcClient(app.origin, token, transport=httpx.WSGITransport(app))


def test_ask_returns_routing_path_jev_decisions_and_costs(tmp_path):
    app, upstream = build_demo(tmp_path / "demo", seed=False)
    upstream.fail_tiers = {"open_source", "mid"}
    with _client(app, TOKENS["agent"]) as client:
        summary = client.ask("Return only the integer sum of 1 and 1.", run_id="cc-path")
    assert summary["status"] == "passed" and summary["answer"] == "2"
    assert [step["tier"] for step in summary["path"]] == ["open_source", "mid", "high"]
    assert [d["selected_tier"] for d in summary["jev_decisions"]] == ["mid", "high"]
    assert summary["costs"]["known_api_usd"] == "0.0053"
    assert summary["demo"] is True and summary["notice"] == SYNTHETIC_NOTICE


def test_same_run_id_replays_without_new_dispatch(tmp_path):
    app, upstream = build_demo(tmp_path / "demo", seed=False)
    with _client(app, TOKENS["agent"]) as client:
        first = client.ask("Return only the integer sum of 1 and 1.", run_id="cc-replay")
        calls = len(upstream.calls)
        again = client.ask("Return only the integer sum of 1 and 1.", run_id="cc-replay")
    assert again == first and len(upstream.calls) == calls


def test_identity_store_credentials_work_and_client_sees_only_own_tasks(tmp_path):
    fixture = build_local_integration(tmp_path / "integration")
    with _client(fixture.app, fixture.credentials["agent"].token) as client:
        assert client.check()["client"] == "agent"
        assert client.ask("Return only the integer sum of 1 and 1.", run_id="cc-own")["status"] == "passed"
    with _client(fixture.app, fixture.credentials["batch"].token) as other:
        with pytest.raises(MrcError):
            other.show("cc-own")


def test_rejects_bad_credentials_non_loopback_origin_and_empty_prompt(tmp_path):
    app, _ = build_demo(tmp_path / "demo", seed=False)
    with _client(app, "x" * 32) as client:
        with pytest.raises(MrcError) as error:
            client.check()
    assert error.value.http_status == 401
    with pytest.raises(ValueError):
        MrcClient("http://example.com:80", TOKENS["agent"])
    with pytest.raises(MrcError, match="client_token_required"):
        MrcClient(app.origin, "")
    with _client(app, TOKENS["agent"]) as client:
        with pytest.raises(MrcError, match="prompt_required"):
            client.ask("  ")


def test_cli_never_takes_token_from_argv_and_reports_errors_as_json(capsys, monkeypatch):
    monkeypatch.delenv(mrc_client.TOKEN_ENV, raising=False)
    assert mrc_client.main(["--origin", "http://127.0.0.1:9", "check"]) == 2
    assert json.loads(capsys.readouterr().out)["code"] == "client_token_required"
    with pytest.raises(SystemExit):
        mrc_client.main(["--origin", "http://127.0.0.1:9", "--token", TOKENS["agent"], "check"])


def test_category_is_passed_through_to_service(tmp_path):
    app, _ = build_demo(tmp_path / "demo", seed=False)
    with _client(app, TOKENS["agent"]) as client:
        assert CATEGORY in client.check()["categories"]
        with pytest.raises(MrcError) as error:
            client.ask("Return only the integer sum of 1 and 1.", category="unregistered:category")
    assert error.value.http_status in (400, 404, 409, 422)

"""Synthetic source contracts and MockTransport only: no paid or live requests."""
import json

import httpx
import pytest

from cloudyfan_jev.scores.adapters import (
    AA_FREE_URL, CODEXRADAR_URL, AdapterError, discover_livebench, fetch_artificial_analysis,
    fetch_livebench, fetch_openrouter_benchmarks, import_codexradar,
    parse_artificial_analysis, parse_livebench, parse_openrouter_benchmarks,
)

NOW = "2026-10-18T10:00:00+08:00"
REV = "a" * 40
RELEASE = "2026_01_08"


def encoded(value):
    return json.dumps(value).encode()


def aa_body(page=1, total=1, score=75, version=4.1):
    return {
        "intelligence_index_version": version,
        "pagination": {"page": page, "total_pages": total, "has_more": page < total},
        "data": [{"id": f"fixture-{page}", "slug": f"fixture-{page}-high", "name": "Fixture (high)",
                  "release_date": "2026-01-01", "evaluations": {
                      "artificial_analysis_intelligence_index": score,
                      "artificial_analysis_coding_index": None}}],
    }


def or_body(score=75):
    return {"meta": {"version": "v1", "source": "artificial-analysis", "as_of": NOW},
            "data": [{"source": "artificial-analysis", "model_permaslug": "fixture/model-high",
                      "intelligence_index": score, "coding_index": 42}]}


def lb_metadata(**updates):
    body = {"release": RELEASE, "revision": REV, "models": {
        "fixture-high": {"effort": "high", "sample_counts": {"sql": 12}}}}
    body.update(updates)
    return encoded(body)


def parse_lb(table=b"model,sql,logic\nfixture-high,80,\n", **kwargs):
    return parse_livebench(table, encoded({"coding": ["sql"], "reasoning": ["logic"]}),
                          kwargs.pop("metadata", lb_metadata()), release=RELEASE, revision=REV,
                          retrieved_at=NOW, **kwargs)


def radar_body(**updates):
    body = {"kind": "model_benchmark", "source_url": CODEXRADAR_URL,
            "source_version": "fixture-v1", "rows": [{"model_id": "fixture/model",
            "score": 100, "effort": "high", "sample_count": 64, "evaluated_at": "2026-10-17"}]}
    body.update(updates)
    return body


def test_aa_missing_effort_is_not_inferred_and_release_is_not_eval_date():
    rows = parse_artificial_analysis(encoded(aa_body()), retrieved_at=NOW)
    assert len(rows) == 1
    assert rows[0]["effort"] is None
    assert rows[0]["evaluated_at"] is None
    assert rows[0]["source_version"] == "aa-index-v4.1"
    assert len(rows[0]["raw_sha256"]) == 64


@pytest.mark.parametrize("value", [True, "75", float("nan"), float("inf"), {}, []])
def test_aa_rejects_non_numeric_or_nonfinite_scores(value):
    with pytest.raises(AdapterError):
        parse_artificial_analysis(encoded(aa_body(score=value)), retrieved_at=NOW)


def test_missing_score_not_zero():
    assert parse_artificial_analysis(encoded(aa_body(score=None)), retrieved_at=NOW) == []
    assert parse_artificial_analysis(encoded(aa_body(score=0)), retrieved_at=NOW)[0]["score"] == 0


def test_aa_pagination_and_version_checked_without_extra_calls():
    requests = []
    def handler(request):
        requests.append(request)
        page = int(request.url.params["page"])
        return httpx.Response(200, json=aa_body(page, 2))
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        rows = fetch_artificial_analysis(client, api_key="fixture", retrieved_at=NOW)
    assert len(rows) == 2
    assert all(str(r.url).startswith(AA_FREE_URL) and r.method == "GET" for r in requests)
    assert [r.url.params["page"] for r in requests] == ["1", "2"]


@pytest.mark.parametrize("mutation", ["version", "duplicate", "pagination"])
def test_aa_pagination_fails_atomically(mutation):
    def handler(request):
        page = int(request.url.params["page"])
        body = aa_body(page, 2)
        if page == 2:
            if mutation == "version":
                body["intelligence_index_version"] = 4.2
            elif mutation == "duplicate":
                body["data"][0]["id"] = "fixture-1"
            else:
                body["pagination"]["page"] = 1
        return httpx.Response(200, json=body)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client, pytest.raises(AdapterError):
        fetch_artificial_analysis(client, api_key="fixture", retrieved_at=NOW)


def test_aa_page_limit_checked_before_fetching_more():
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=aa_body(1, 99))
    with httpx.Client(transport=httpx.MockTransport(handler)) as client, pytest.raises(AdapterError):
        fetch_artificial_analysis(client, api_key="fixture", retrieved_at=NOW, max_pages=2)
    assert len(calls) == 1


def test_openrouter_relay_not_independent_aa_evidence():
    rows = parse_openrouter_benchmarks(encoded(or_body()), retrieved_at=NOW)
    assert rows[0]["source"] == "artificial_analysis"
    assert rows[0]["channel"] == "openrouter"
    assert rows[0]["source_version"] == "aa-index-unknown:openrouter-v1"
    assert rows[0]["effort"] is None and rows[0]["evaluated_at"] is None
    assert rows[0]["metric"] == "artificial_analysis_intelligence_index"


def test_openrouter_fixed_get_query_only():
    def handler(request):
        assert request.method == "GET"
        assert request.url.path == "/api/v1/benchmarks"
        assert dict(request.url.params) == {"source": "artificial-analysis"}
        return httpx.Response(200, json=or_body())
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert len(fetch_openrouter_benchmarks(client, api_key="fixture", retrieved_at=NOW)) == 2


@pytest.mark.parametrize("status", [302, 401, 429, 500])
def test_fetch_no_redirect_or_retry_or_sensitive_error_body(status):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(status, text="PRIVATE RESPONSE MARKER", headers={"Location": "https://evil.test/"})
    with httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True) as client:
        with pytest.raises(AdapterError) as caught:
            fetch_openrouter_benchmarks(client, api_key="fixture", retrieved_at=NOW)
    assert len(calls) == 1
    assert "PRIVATE" not in str(caught.value)


def test_transport_error_is_sanitized():
    def handler(request):
        raise httpx.ConnectError("PRIVATE TRANSPORT MARKER", request=request)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(AdapterError, match="Benchmark transport failed") as caught:
            fetch_openrouter_benchmarks(client, api_key="fixture", retrieved_at=NOW)
    assert "PRIVATE" not in str(caught.value)


def test_livebench_preserves_raw_subtasks_and_missingness():
    rows = parse_lb()
    assert len(rows) == 1
    assert rows[0]["metric"] == "sql" and rows[0]["score"] == 80
    assert rows[0]["sample_count"] == 12 and rows[0]["effort"] == "high"
    assert rows[0]["evaluated_at"] is None
    assert rows[0]["source_revision"] == REV
    assert rows[0]["source_version"] == "livebench-v1:2026_01_08"


def test_livebench_unknown_metadata_does_not_parse_model_name():
    assert parse_lb(metadata=lb_metadata(models={}))[0]["effort"] is None


@pytest.mark.parametrize("table", [
    b"model,sql,logic\nx,NaN,50\n", b"model,sql,logic\nx,101,50\n",
    b"model,sql,logic\nx,-1,50\n", b"model,sql,logic\nx,1\n",
    b"model,sql,logic\nx,1,2,3\n", b"model,sql,sql\nx,1,2\n",
    b"model,sql,logic\nx,1,2\nx,3,4\n", b"model,sql,logic\n,1,2\n",
])
def test_livebench_rejects_malformed_table(table):
    with pytest.raises(AdapterError):
        parse_lb(table)


def test_livebench_metadata_pinned_and_in_digest():
    with pytest.raises(AdapterError):
        parse_lb(metadata=lb_metadata(revision="b" * 40))
    assert parse_lb()[0]["raw_sha256"] != parse_lb(metadata=lb_metadata(models={}))[0]["raw_sha256"]


def test_livebench_fetch_two_commit_pinned_official_paths():
    requests = []
    def handler(request):
        requests.append(request)
        assert request.url.host == "raw.githubusercontent.com"
        assert f"/{REV}/public/" in request.url.path
        if request.url.path.endswith(".csv"):
            return httpx.Response(200, content=b"model,sql\nfixture,50\n")
        return httpx.Response(200, json={"coding": ["sql"]})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        rows = fetch_livebench(client, release=RELEASE, revision=REV, metadata_json=lb_metadata(), retrieved_at=NOW)
    assert len(rows) == 1 and len(requests) == 2


def test_livebench_does_not_forward_inherited_client_credentials():
    def handler(request):
        assert request.headers["User-Agent"] == "cloudyfan-mrc-score-reader/0.1"
        assert all(name not in request.headers for name in (
            "authorization", "cookie", "x-api-key", "x-custom-access-token"))
        assert not request.url.query
        assert request.extensions["timeout"] == {"connect": 30, "read": 30, "write": 30, "pool": 30}
        if request.url.path.endswith(".csv"):
            return httpx.Response(200, content=b"model,sql\nfixture,50\n")
        return httpx.Response(200, json={"coding": ["sql"]})
    with httpx.Client(transport=httpx.MockTransport(handler), auth=("fixture", "fixture"),
                      headers={"x-api-key": "fixture", "Authorization": "fixture",
                               "x-custom-access-token": "fixture"}, params={"api_key": "fixture"},
                      cookies={"fixture": "fixture"}) as client:
        fetch_livebench(client, release=RELEASE, revision=REV, metadata_json=lb_metadata(), retrieved_at=NOW)


@pytest.mark.parametrize("revision", ["main", "../main", "a" * 39, "https://evil.test"])
def test_livebench_rejects_unpinned_or_arbitrary_location_before_get(revision):
    with httpx.Client(transport=httpx.MockTransport(lambda r: pytest.fail("unexpected fetch"))) as client:
        with pytest.raises(AdapterError):
            fetch_livebench(client, release=RELEASE, revision=revision,
                            metadata_json=lb_metadata(), retrieved_at=NOW)


def test_codexradar_is_reviewed_import_with_coverage():
    row = import_codexradar(encoded(radar_body()), retrieved_at=NOW)[0]
    assert row["channel"] == "reviewed_import"
    assert row["sample_count"] == 64 and row["metric"] == "overall"
    assert row["evaluated_at"] == "2026-10-17"


@pytest.mark.parametrize("updates", [{"source_url": "https://codexradar.com/account/"},
                                       {"kind": "account_points"}, {"source_version": None}])
def test_codexradar_rejects_account_points_or_unversioned_import(updates):
    with pytest.raises(AdapterError):
        import_codexradar(encoded(radar_body(**updates)), retrieved_at=NOW)


@pytest.mark.parametrize("change", [{"sample_count": True}, {"score": 151},
                                      {"evaluated_at": "2026-01-01T00:00:00Z"}, {"effort": ""}])
def test_codexradar_rejects_invalid_evidence_fields(change):
    body = radar_body()
    body["rows"][0].update(change)
    with pytest.raises(AdapterError):
        import_codexradar(encoded(body), retrieved_at=NOW)


def test_json_duplicate_keys_and_naive_timestamp_rejected():
    with pytest.raises(AdapterError):
        parse_artificial_analysis(b'{"data":[],"data":[]}', retrieved_at=NOW)
    with pytest.raises(AdapterError):
        import_codexradar(encoded(radar_body()), retrieved_at="2026-10-18T10:00:00")


def test_livebench_discovery_pins_commit_before_loading_release_manifest():
    calls = []
    manifest = b'// Published releases\nexport const RELEASES = [\n"2025-12-23", "2026-01-08",\n];\n'
    def handler(request):
        calls.append(request)
        assert request.headers["User-Agent"] == "cloudyfan-mrc-score-reader/0.1"
        if request.url.host == "api.github.com":
            assert request.url.path == "/repos/LiveBench/new-livebench/commits/main"
            return httpx.Response(200, json={"sha": REV})
        assert request.url.host == "raw.githubusercontent.com"
        assert request.url.path == f"/LiveBench/new-livebench/{REV}/src/lib/constants.js"
        return httpx.Response(200, content=manifest)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert discover_livebench(client) == {"revision": REV, "release": RELEASE}
    assert len(calls) == 2 and all(r.method == "GET" for r in calls)


@pytest.mark.parametrize("manifest", [
    b"", b"export const RELEASES = [];", b"export const RELEASES = getDates();",
    b'export const RELEASES = ["2026-01-08", sideEffect()];',
    b'export const RELEASES = ["2026-02-30"];',
    b'export const RELEASES = ["2026-01-08", "2025-01-01"];',
    b'export const RELEASES = ["2026-01-08", "2026-01-08"];',
    b'export const RELEASES = ["2026_01_08"];',
    b'export const RELEASES = ["2026-01-08"];\nexport const RELEASES = ["2026-02-01"];',
    b'/*\nexport const RELEASES = ["2026-01-08"];\n*/',
    b'const hidden = `\nexport const RELEASES = ["2026-01-08"];\n`;',
    b'// export const RELEASES = ["2026-01-08"];',
    b'export const RELEASES = ["2026-01-08"] + getMore();', b"\xff",
])
def test_livebench_discovery_rejects_ambiguous_or_dynamic_manifests(manifest):
    def handler(request):
        if request.url.host == "api.github.com":
            return httpx.Response(200, json={"sha": REV})
        return httpx.Response(200, content=manifest)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client, pytest.raises(AdapterError):
        discover_livebench(client)


@pytest.mark.parametrize("sha", [None, "main", "../main", "b" * 39])
def test_livebench_discovery_rejects_invalid_commit_without_second_request(sha):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"sha": sha})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client, pytest.raises(AdapterError):
        discover_livebench(client)
    assert len(calls) == 1

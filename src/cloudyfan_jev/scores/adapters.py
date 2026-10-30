"""Read-only benchmark adapters. Scores are evidence, never task validation.

No effort is inferred from display names. LiveBench emits source-native subtask
scores, not a new average; v1 releases are separate from the LiveBench v2 board.
CodexRadar is a reviewed import contract, not a claim of a supported remote API.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import re
from datetime import date, datetime
from typing import Any

import httpx

AA_FREE_URL = "https://artificialanalysis.ai/api/v2/language/models/free"
OPENROUTER_URL = "https://openrouter.ai/api/v1/benchmarks"
LIVEBENCH_ROOT = "https://raw.githubusercontent.com/LiveBench/new-livebench"
LIVEBENCH_HEAD_URL = "https://api.github.com/repos/LiveBench/new-livebench/commits/main"
CODEXRADAR_URL = "https://deng.codexradar.com/"
MAX_BYTES = 16 * 1024 * 1024


class AdapterError(ValueError):
    """Safe diagnostics: never include remote response bodies or credentials."""


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 512:
        raise AdapterError(f"Invalid {field}")
    return value


def _object(value: Any) -> dict:
    if not isinstance(value, dict):
        raise AdapterError("Expected object")
    return value


def _array(value: Any) -> list:
    if not isinstance(value, list):
        raise AdapterError("Expected list")
    return value


def _finite(value: Any, *, csv_value: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, str) if csv_value else (int, float)):
        raise AdapterError("Invalid numeric score")
    try:
        number = float(value)
    except (ValueError, OverflowError):
        raise AdapterError("Invalid numeric score") from None
    if not math.isfinite(number):
        raise AdapterError("Non-finite score")
    return number


def _count(value: Any) -> int | None:
    if value is None:
        return None
    if type(value) is not int or value < 0:
        raise AdapterError("Invalid sample count")
    return value


def _date(value: Any) -> str | None:
    if value is None:
        return None
    try:
        if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
            raise ValueError
    except ValueError:
        raise AdapterError("Expected ISO evaluation date") from None
    return value


def _timestamp(value: Any) -> str:
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError
    except (TypeError, ValueError):
        raise AdapterError("Expected timezone-aware retrieval timestamp") from None
    return value


def _json(raw: bytes) -> dict:
    if not isinstance(raw, bytes) or len(raw) > MAX_BYTES:
        raise AdapterError("Invalid or oversized source document")
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise AdapterError("Duplicate JSON key")
            result[key] = value
        return result
    try:
        result = json.loads(raw, object_pairs_hook=unique)
    except (ValueError, UnicodeError):
        raise AdapterError("Invalid source JSON") from None
    return _object(result)


def _observation(*, source, version, model, metric, score, retrieved_at, source_url,
                 digest, effort=None, evaluated_at=None, sample_count=None, **extra) -> dict:
    if effort is not None:
        effort = _text(effort, "effort")
    return {
        "source": source, "source_version": _text(version, "source version"),
        "source_model_id": _text(model, "source model ID"), "metric": _text(metric, "metric"),
        "score": _finite(score), "evaluated_at": _date(evaluated_at),
        "retrieved_at": _timestamp(retrieved_at), "source_url": source_url,
        "raw_sha256": digest, "effort": effort, "sample_count": _count(sample_count), **extra,
    }


def _unique(rows: list[dict]) -> list[dict]:
    seen = set()
    for row in rows:
        key = (row["source"], row["source_version"], row["source_model_id"], row["effort"], row["metric"])
        if key in seen:
            raise AdapterError("Duplicate score observation")
        seen.add(key)
    return rows


def parse_artificial_analysis(raw: bytes, *, retrieved_at: str, source_url: str = AA_FREE_URL) -> list[dict]:
    """Parse one official v2 free page; release_date is not an evaluation date."""
    _timestamp(retrieved_at)
    if source_url != AA_FREE_URL:
        raise AdapterError("Unsupported AA endpoint")
    body = _json(raw)
    version = _finite(body.get("intelligence_index_version"))
    rows = []
    for item in _array(body.get("data")):
        item = _object(item)
        model = _text(item.get("id"), "AA model ID")
        for metric, score in _object(item.get("evaluations")).items():
            if score is None:
                continue
            rows.append(_observation(
                source="artificial_analysis", version=f"aa-index-v{version:g}", model=model,
                metric=metric, score=score, retrieved_at=retrieved_at, source_url=source_url,
                digest=hashlib.sha256(raw).hexdigest(), effort=item.get("reasoning_effort"),
                evaluated_at=item.get("evaluated_at"), sample_count=item.get("sample_count"),
                channel="direct", source_slug=item.get("slug"),
            ))
    return _unique(rows)


def parse_openrouter_benchmarks(raw: bytes, *, retrieved_at: str) -> list[dict]:
    """Parse AA evidence relayed by OpenRouter, preserving unknown index version."""
    _timestamp(retrieved_at)
    body = _json(raw)
    meta = _object(body.get("meta"))
    schema = _text(meta.get("version"), "OpenRouter schema version")
    if schema != "v1" or meta.get("source") != "artificial-analysis":
        raise AdapterError("Unsupported benchmark schema or source")
    rows = []
    for item in _array(body.get("data")):
        item = _object(item)
        if item.get("source") != "artificial-analysis":
            raise AdapterError("Unexpected benchmark source")
        for name in ("intelligence", "coding", "agentic"):
            score = item.get(f"{name}_index")
            if score is None:
                continue
            rows.append(_observation(
                source="artificial_analysis", version="aa-index-unknown:openrouter-v1",
                model=item.get("model_permaslug"), metric=f"artificial_analysis_{name}_index",
                score=score, retrieved_at=retrieved_at, source_url=OPENROUTER_URL,
                digest=hashlib.sha256(raw).hexdigest(), effort=item.get("reasoning_effort"),
                channel="openrouter", upstream_as_of=meta.get("as_of"),
                upstream_source_url=meta.get("source_url"), index_version_known=False,
            ))
    return _unique(rows)


def _livebench_version(release: str, revision: str) -> None:
    if not isinstance(release, str) or not re.fullmatch(r"\d{4}_\d{2}_\d{2}", release):
        raise AdapterError("Invalid LiveBench release")
    _date(release.replace("_", "-"))
    if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise AdapterError("LiveBench requires a pinned 40-character commit")


def parse_livebench(table_csv: bytes, categories_json: bytes, metadata_json: bytes, *,
                   release: str, revision: str, retrieved_at: str) -> list[dict]:
    """Parse a v1 release with reviewed JSON metadata (never execute modelLinks.js).

    Metadata contract: {release, revision, models: {CSV_ID: {effort?,
    evaluated_at?, sample_counts?: {SUBTASK: count}}}}. Missing metadata remains
    unknown. JSON is a local reviewed import, not an official metadata API.
    """
    _livebench_version(release, revision)
    _timestamp(retrieved_at)
    categories, metadata = _json(categories_json), _json(metadata_json)
    if metadata.get("release") != release or metadata.get("revision") != revision:
        raise AdapterError("LiveBench metadata version mismatch")
    models = _object(metadata.get("models"))
    subtasks = {}
    for category, tasks in categories.items():
        _text(category, "category")
        for task in _array(tasks):
            task = _text(task, "subtask")
            if task in subtasks:
                raise AdapterError("Duplicate LiveBench subtask")
            subtasks[task] = category
    if not subtasks or not isinstance(table_csv, bytes) or len(table_csv) > MAX_BYTES:
        raise AdapterError("Invalid LiveBench table or categories")
    digest = hashlib.sha256()
    for part in (table_csv, categories_json, metadata_json):
        digest.update(len(part).to_bytes(8, "big"))
        digest.update(part)
    try:
        table = csv.DictReader(io.StringIO(table_csv.decode("utf-8-sig")), strict=True)
        header = table.fieldnames or []
        if len(header) != len(set(header)) or set(header) != {"model", *subtasks}:
            raise AdapterError("LiveBench table columns disagree with categories")
        rows = []
        seen_models = set()
        for row in table:
            if None in row or any(value is None for value in row.values()):
                raise AdapterError("Malformed CSV row")
            model = _text(row["model"], "LiveBench model")
            if model in seen_models:
                raise AdapterError("Duplicate LiveBench model")
            seen_models.add(model)
            info = _object(models.get(model, {}))
            counts = _object(info.get("sample_counts", {}))
            for task, category in subtasks.items():
                if row[task].strip() == "":
                    continue  # missing score is not zero
                score = _finite(row[task], csv_value=True)
                if not 0 <= score <= 100:
                    raise AdapterError("LiveBench score outside source scale")
                rows.append(_observation(
                    source="livebench", version=f"livebench-v1:{release}", model=model,
                    metric=task, score=score, retrieved_at=retrieved_at,
                    source_url=f"{LIVEBENCH_ROOT}/{revision}/public/table_{release}.csv",
                    digest=digest.hexdigest(), effort=info.get("effort"),
                    evaluated_at=info.get("evaluated_at"), sample_count=counts.get(task),
                    channel="official_csv", category=category, source_revision=revision,
                    metadata_channel="reviewed_import",
                ))
    except (csv.Error, UnicodeError):
        raise AdapterError("Invalid LiveBench CSV") from None
    return _unique(rows)


def import_codexradar(raw: bytes, *, retrieved_at: str) -> list[dict]:
    """Import reviewed model benchmark rows; reject account points and unknown contracts.

    Contract: {kind: 'model_benchmark', source_url: official benchmark URL,
    source_version, rows: [{model_id, effort, score, evaluated_at?, sample_count?}]}.
    Caller must verify coverage before eligibility; this does not scrape accounts.
    """
    _timestamp(retrieved_at)
    body = _json(raw)
    _text(body.get("source_version"), "source version")
    if body.get("kind") != "model_benchmark" or body.get("source_url") != CODEXRADAR_URL:
        raise AdapterError("Expected a reviewed CodexRadar model benchmark import")
    rows = []
    for item in _array(body.get("rows")):
        item = _object(item)
        score = _finite(item.get("score"))
        if not 0 <= score <= 150:
            raise AdapterError("CodexRadar score outside source scale")
        rows.append(_observation(
            source="codexradar", version=body.get("source_version"), model=item.get("model_id"),
            metric="overall", score=score, retrieved_at=retrieved_at, source_url=CODEXRADAR_URL,
            digest=hashlib.sha256(raw).hexdigest(), effort=item.get("effort"),
            evaluated_at=item.get("evaluated_at"), sample_count=item.get("sample_count"),
            channel="reviewed_import",
        ))
    return _unique(rows)


def _get(client: httpx.Client, url: str, *, headers: dict | None = None,
         params: dict | None = None) -> bytes:
    # URL constructed exclusively by fetch functions, never from upstream links.
    try:
        # Standalone request avoids *all* inherited query/header/cookie state,
        # including application-specific credential names we cannot enumerate.
        explicit_headers = {"User-Agent": "cloudyfan-mrc-score-reader/0.1", **(headers or {})}
        request = httpx.Request("GET", url, params=params, headers=explicit_headers,
                                extensions={"timeout": httpx.Timeout(30).as_dict()})
        r = client.send(request, stream=True, auth=None, follow_redirects=False)
        try:
            if r.status_code != 200:
                raise AdapterError(f"Benchmark fetch failed (HTTP {r.status_code})")
            chunks, size = [], 0
            for chunk in r.iter_bytes():
                size += len(chunk)
                if size > MAX_BYTES:
                    raise AdapterError("Benchmark response exceeded size limit")
                chunks.append(chunk)
        finally:
            r.close()
        return b"".join(chunks)
    except httpx.HTTPError:
        raise AdapterError("Benchmark transport failed") from None


def fetch_artificial_analysis(client: httpx.Client, *, api_key: str, retrieved_at: str,
                              max_pages: int = 20) -> list[dict]:
    """Fetch the free v2 endpoint with bounded, consistency-checked pagination."""
    _timestamp(retrieved_at)
    if not isinstance(api_key, str) or not api_key or type(max_pages) is not int or not 1 <= max_pages <= 100:
        raise AdapterError("Invalid fetch configuration")
    rows, expected_version, expected_total = [], None, None
    for page in range(1, max_pages + 1):
        raw = _get(client, AA_FREE_URL, headers={"x-api-key": api_key}, params={"page": page})
        body = _json(raw)
        pagination = _object(body.get("pagination"))
        total = pagination.get("total_pages")
        if (type(total) is not int or not 1 <= total <= max_pages
                or type(pagination.get("page")) is not int or pagination["page"] != page
                or type(pagination.get("has_more")) is not bool
                or pagination["has_more"] != (page < total)):
            raise AdapterError("Invalid or excessive AA pagination")
        version = _finite(body.get("intelligence_index_version"))
        if expected_version is not None and (version != expected_version or total != expected_total):
            raise AdapterError("AA version or pagination changed during fetch")
        expected_version, expected_total = version, total
        rows.extend(parse_artificial_analysis(raw, retrieved_at=retrieved_at))
        if not pagination["has_more"]:
            return _unique(rows)
    raise AdapterError("Incomplete AA pagination")


def fetch_openrouter_benchmarks(client: httpx.Client, *, api_key: str, retrieved_at: str) -> list[dict]:
    """Read AA benchmarks only; this never invokes generation or Jev classification."""
    _timestamp(retrieved_at)
    if not isinstance(api_key, str) or not api_key:
        raise AdapterError("Missing benchmark API credential")
    raw = _get(client, OPENROUTER_URL, headers={"Authorization": f"Bearer {api_key}"},
               params={"source": "artificial-analysis"})
    return parse_openrouter_benchmarks(raw, retrieved_at=retrieved_at)


def fetch_livebench(client: httpx.Client, *, release: str, revision: str,
                   metadata_json: bytes, retrieved_at: str) -> list[dict]:
    """Fetch two files from one pinned official commit; metadata is reviewed local JSON."""
    _livebench_version(release, revision)
    _timestamp(retrieved_at)
    metadata = _json(metadata_json)
    if metadata.get("release") != release or metadata.get("revision") != revision:
        raise AdapterError("LiveBench metadata version mismatch")
    base = f"{LIVEBENCH_ROOT}/{revision}/public"
    table = _get(client, f"{base}/table_{release}.csv")
    categories = _get(client, f"{base}/categories_{release}.json")
    return parse_livebench(table, categories, metadata_json, release=release, revision=revision,
                          retrieved_at=retrieved_at)


def discover_livebench(client: httpx.Client) -> dict[str, str]:
    """Discover a publication, then pin its commit; never execute upstream JS.

    Accept only one exported static RELEASES array of ordered ISO date literals.
    A changed JavaScript format requires adapter review instead of evaluation.
    """
    commit = _json(_get(client, LIVEBENCH_HEAD_URL))
    revision = commit.get("sha")
    if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise AdapterError("Invalid LiveBench commit response")
    raw = _get(client, f"{LIVEBENCH_ROOT}/{revision}/src/lib/constants.js")
    try:
        source = raw.decode("utf-8")
    except UnicodeError:
        raise AdapterError("Invalid LiveBench release manifest encoding") from None
    # A line-comment is inert; block comments/templates may impersonate a
    # declaration and are not part of this deliberately small import grammar.
    if "/*" in source or "`" in source:
        raise AdapterError("Unsupported LiveBench release manifest syntax")
    assignment = r"(?m)^[ \t]*export[ \t]+const[ \t]+RELEASES[ \t]*="
    if len(re.findall(assignment, source)) != 1:
        raise AdapterError("Missing or ambiguous LiveBench RELEASES declaration")
    matches = re.findall(assignment + r"[ \t]*(\[[\s\S]*?\])[ \t]*;", source)
    if len(matches) != 1:
        raise AdapterError("Malformed LiveBench RELEASES declaration")
    array = matches[0]
    literal = r'"\d{4}-\d{2}-\d{2}"'
    if not re.fullmatch(r"\[\s*" + literal + r"(?:\s*,\s*" + literal + r")*\s*,?\s*\]", array):
        raise AdapterError("LiveBench RELEASES must contain static date literals")
    dates = json.loads(re.sub(r",\s*\]$", "]", array))
    for value in dates:
        _date(value)
    if dates != sorted(set(dates)):
        raise AdapterError("LiveBench RELEASES must be unique and chronological")
    return {"revision": revision, "release": dates[-1].replace("-", "_")}

"""GhostReader benchmark runner.

Executes frozen-corpus and/or live-smoke fixtures through the GhostReader
pipeline, scores quality and efficiency as separate questions, and emits
both a human-readable summary and a machine-readable JSON artifact.

Modes:
    frozen  — loads stored HTML from benchmarks/corpus/<id>/page.html,
              calls extract_from_html(...) with browser_fallback=False.
              This mode is the regression gate: it can fail the run.
    live    — fetches live URLs via the full pipeline (read_text).
              Reality check only. Never gates release.
    both    — runs frozen then live. Exit code is derived from frozen only.

Quality checks (per fixture):
    title_ok          expected_title_contains appears in returned title
    required_recall   every required_phrase is present in extracted text
    forbidden_clean   no forbidden_phrase is present
    word_count_ok     extracted word count within [min_word_count, max_word_count]

Efficiency checks:
    method_ok                 result.method is in fixture.preferred_methods
    latency_band_ok           latency within the mode's ceiling (overridable
                              per fixture via latency_ms_ceiling)
    cheaper_path_sufficient   live-only: if the full pipeline picked
                              browser_fallback and quality passed, a second
                              pass with browser_fallback=False also satisfies
                              quality. Raised only as an observation on that
                              specific fixture; does NOT claim Stage 3 is
                              universally unnecessary.

Diagnostic (not gated): confidence, method, text_length, hints.

Usage (from the project root):
    python -m benchmarks.run                        full run (both modes)
    python -m benchmarks.run --mode frozen          regression gate
    python -m benchmarks.run --mode live            live smoke only
    python -m benchmarks.run --filter wikipedia     category substring filter
    python -m benchmarks.run --id example_com_landing   single-fixture run
    python -m benchmarks.run --url https://...      ad-hoc; no scoring
    python -m benchmarks.run --baselines            also run baselines (step 7)
    python -m benchmarks.run --tune-thresholds      threshold sweep (step 7)
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import math
import re
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import httpx
from fastapi import HTTPException

from benchmark_target import (
    ResultType,
    extract_from_html,
    read_text,
)
# APP_VERSION is useful metadata but lives in app.py, not the shim. The
# runner is allowed to reach past the shim for non-logic metadata.
from app import APP_VERSION

BENCHMARKS_DIR = Path(__file__).resolve().parent
FROZEN_FIXTURES_PATH = BENCHMARKS_DIR / "fixtures_frozen.json"
LIVE_FIXTURES_PATH = BENCHMARKS_DIR / "fixtures_live.json"
RESULTS_DIR = BENCHMARKS_DIR / "results"
BASELINE_PATH = RESULTS_DIR / "v0.1.0-frozen-baseline.json"

# Latency ceilings — mode-wide defaults, overridable per fixture via
# fixture["latency_ms_ceiling"]. Frozen is CPU-only (no network, no
# Playwright); live includes network and potentially Stage 3.
FROZEN_LATENCY_MS_CEILING = 2000
LIVE_LATENCY_MS_CEILING = 15000


# ----------------------------
# Result dataclasses
# ----------------------------


@dataclass
class CheckResult:
    name: str
    passed: bool
    reason: str = ""  # populated on failure
    # When False, this check is recorded and printed but does NOT contribute
    # to quality_pass / efficiency_pass. Used for partial / graceful_failure
    # fixtures where some checks are observational rather than gating.
    gating: bool = True


@dataclass
class FixtureResult:
    fixture_id: str
    category: str
    mode: str  # "frozen" | "live"
    source_url: str
    # Execution outcome
    ok: bool  # pipeline didn't crash
    error: Optional[str] = None
    # Approach identity — None for the production runner; baselines set
    # this to e.g. "baseline_raw_html" so per-fixture JSON disambiguates
    # rows that share a method bucket (trafilatura_only and raw_html
    # baselines both report method="raw_html" because they reuse the
    # production scorer's method_ok semantics).
    approach: Optional[str] = None
    # Extracted data (None on error)
    method: Optional[str] = None
    confidence: Optional[float] = None
    title: Optional[str] = None
    text_length: Optional[int] = None
    word_count: Optional[int] = None
    latency_ms: Optional[float] = None
    hints: Optional[dict[str, Any]] = None
    # Scoring
    quality_checks: list[CheckResult] = field(default_factory=list)
    efficiency_checks: list[CheckResult] = field(default_factory=list)
    quality_pass: bool = False
    efficiency_pass: bool = False
    overall_pass: bool = False


# ----------------------------
# Fixture loading
# ----------------------------


def load_fixtures(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    if data.get("schema_version") != 1:
        raise SystemExit(f"Unsupported schema_version in {path.name}: {data.get('schema_version')!r}")
    fixtures = data.get("fixtures")
    if not isinstance(fixtures, list):
        raise SystemExit(f"{path.name} is missing a top-level 'fixtures' list")
    # Unknown top-level keys (e.g. _comment) are silently ignored by design.
    return fixtures


def filter_fixtures(
    fixtures: list[dict[str, Any]],
    *,
    category_substring: Optional[str],
    fixture_id: Optional[str],
) -> list[dict[str, Any]]:
    out = fixtures
    if fixture_id:
        out = [f for f in out if f["id"] == fixture_id]
    if category_substring:
        out = [f for f in out if category_substring.lower() in f.get("category", "").lower()]
    return out


# ----------------------------
# Scoring
# ----------------------------


_WORD_RE = re.compile(r"\b\w+\b")


def count_words(text: str) -> int:
    return len(_WORD_RE.findall(text or ""))


# Gating policy per (check_name, expected_outcome). forbidden_clean,
# method_allowed, and latency_band_ok always gate — they encode
# correctness invariants that hold regardless of outcome class.
# Everything else is gating only for "success".
_ALWAYS_GATING = {"forbidden_clean", "method_allowed", "latency_band_ok"}
_VALID_OUTCOMES = {"success", "partial", "graceful_failure"}


def _fixture_outcome(fixture: dict[str, Any]) -> str:
    """Read and validate expected_outcome. Hard-fails on typos so a
    misspelled value (e.g. 'graceful-failure') can't silently fall into
    the non-success path and quietly disable gating checks."""
    outcome = fixture.get("expected_outcome", "success")
    if outcome not in _VALID_OUTCOMES:
        raise SystemExit(
            f"Invalid expected_outcome={outcome!r} for fixture {fixture.get('id')!r}; "
            f"expected one of {sorted(_VALID_OUTCOMES)}"
        )
    return outcome


def _is_gating(check_name: str, expected_outcome: str) -> bool:
    if check_name in _ALWAYS_GATING:
        return True
    return expected_outcome == "success"


def score_quality(result: ResultType, fixture: dict[str, Any]) -> list[CheckResult]:
    outcome = _fixture_outcome(fixture)
    checks: list[CheckResult] = []
    text = result.text_markdown or ""
    text_lower = text.lower()

    # title_ok
    expected_title = fixture.get("expected_title_contains")
    if expected_title:
        title = result.title or ""
        passed = expected_title.lower() in title.lower()
        checks.append(CheckResult(
            "title_ok",
            passed,
            "" if passed else f"title={title!r} does not contain {expected_title!r}",
            gating=_is_gating("title_ok", outcome),
        ))

    # required_recall
    required = fixture.get("required_phrases", []) or []
    if required:
        missing = [p for p in required if p.lower() not in text_lower]
        checks.append(CheckResult(
            "required_recall",
            not missing,
            "" if not missing else f"missing phrases: {missing}",
            gating=_is_gating("required_recall", outcome),
        ))

    # forbidden_clean
    forbidden = fixture.get("forbidden_phrases", []) or []
    if forbidden:
        hits = [p for p in forbidden if p.lower() in text_lower]
        checks.append(CheckResult(
            "forbidden_clean",
            not hits,
            "" if not hits else f"forbidden phrases present: {hits}",
            gating=_is_gating("forbidden_clean", outcome),
        ))

    # word_count_ok
    min_wc = fixture.get("min_word_count")
    max_wc = fixture.get("max_word_count")
    if min_wc is not None or max_wc is not None:
        wc = count_words(text)
        lo = min_wc if min_wc is not None else 0
        hi = max_wc if max_wc is not None else 10**9
        passed = lo <= wc <= hi
        checks.append(CheckResult(
            "word_count_ok",
            passed,
            "" if passed else f"word_count={wc} outside [{lo},{hi}]",
            gating=_is_gating("word_count_ok", outcome),
        ))

    # method_allowed — hard correctness gate. Distinct from method_ok
    # (the soft preferred-method efficiency hint). Inert when the field
    # is absent, which is the default for "success" fixtures.
    allowed = fixture.get("allowed_methods")
    if allowed:
        passed = result.method in allowed
        checks.append(CheckResult(
            "method_allowed",
            passed,
            "" if passed else f"method={result.method!r} not in allowed_methods={allowed}",
            gating=True,  # always gates when configured
        ))

    return checks


def score_efficiency(
    result: ResultType,
    fixture: dict[str, Any],
    *,
    mode: str,
    latency_ms: float,
    cheaper_path_sufficient_result: Optional[bool],
) -> list[CheckResult]:
    outcome = _fixture_outcome(fixture)
    checks: list[CheckResult] = []

    # method_ok — soft efficiency hint, gating only for "success" fixtures.
    # For partial/graceful_failure, "extracted via best_effort instead of
    # the cheap path I hoped for" is not a correctness failure; that's
    # what allowed_methods is for.
    preferred = fixture.get("preferred_methods") or []
    if preferred:
        passed = result.method in preferred
        checks.append(CheckResult(
            "method_ok",
            passed,
            "" if passed else f"method={result.method!r} not in preferred={preferred}",
            gating=_is_gating("method_ok", outcome),
        ))

    # latency_band_ok — always gates: a pipeline that takes 60s on a 404
    # is broken regardless of outcome class.
    default_ceiling = FROZEN_LATENCY_MS_CEILING if mode == "frozen" else LIVE_LATENCY_MS_CEILING
    ceiling = fixture.get("latency_ms_ceiling", default_ceiling)
    passed = latency_ms <= ceiling
    checks.append(CheckResult(
        "latency_band_ok",
        passed,
        "" if passed else f"latency_ms={latency_ms:.1f} > ceiling={ceiling}",
        gating=_is_gating("latency_band_ok", outcome),
    ))

    # cheaper_path_sufficient — live only, and only relevant when
    # full pipeline picked browser_fallback.
    if mode == "live" and result.method == "browser_fallback":
        if cheaper_path_sufficient_result is True:
            checks.append(CheckResult(
                "cheaper_path_sufficient",
                False,  # cheap path also worked → Stage 3 was arguably wasted here
                "Stage 3 chosen, but a browser_fallback=False pass also satisfied quality",
                gating=_is_gating("cheaper_path_sufficient", outcome),
            ))
        elif cheaper_path_sufficient_result is False:
            checks.append(CheckResult(
                "cheaper_path_sufficient",
                True,
                "",
                gating=_is_gating("cheaper_path_sufficient", outcome),
            ))
        # None = not evaluated (e.g. first-pass quality failed). Skip the check.

    return checks


def finalize_result(r: FixtureResult) -> None:
    # Errored fixtures must NOT be recorded as passing. An empty checks list
    # on a successful (ok=True) run with no applicable assertions is
    # defensibly a pass; on a crashed run it would be a bug.
    if not r.ok:
        r.quality_pass = False
        r.efficiency_pass = False
        r.overall_pass = False
        return

    gating_q = [c for c in r.quality_checks if c.gating]
    gating_e = [c for c in r.efficiency_checks if c.gating]
    r.quality_pass = all(c.passed for c in gating_q) if gating_q else True
    r.efficiency_pass = all(c.passed for c in gating_e) if gating_e else True
    r.overall_pass = r.quality_pass and r.efficiency_pass


# ----------------------------
# Execution — frozen
# ----------------------------


def _resolve_frozen_source_url(fixture: dict[str, Any], html_path: Path) -> str:
    """Prefer post-redirect final_url from meta.json when available.

    The stored page.html is the body of a response that may have come from
    a redirect chain. Feeding extraction the pre-redirect URL would mean
    trafilatura resolves relative links against the wrong base, producing
    a less-faithful reproduction of capture-time conditions. When meta.json
    is present and has final_url, use it; otherwise fall back to the
    declared source_url.
    """
    meta_path = html_path.parent / "meta.json"
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            final_url = meta.get("final_url")
            if isinstance(final_url, str) and final_url:
                return final_url
        except Exception:
            # Corrupt meta.json shouldn't kill the run — fall back silently.
            pass
    return fixture["source_url"]


async def run_frozen_fixture(fixture: dict[str, Any]) -> FixtureResult:
    fr = FixtureResult(
        fixture_id=fixture["id"],
        category=fixture.get("category", ""),
        mode="frozen",
        source_url=fixture["source_url"],
        ok=False,
    )
    html_path = BENCHMARKS_DIR / fixture["html_path"]
    if not html_path.exists():
        fr.error = f"missing snapshot: {html_path.relative_to(BENCHMARKS_DIR)} (run `python -m benchmarks.capture --id {fixture['id']}` to populate)"
        finalize_result(fr)
        return fr

    try:
        html = html_path.read_bytes().decode("utf-8", errors="replace")
    except Exception as exc:
        fr.error = f"{type(exc).__name__}: {exc}"
        finalize_result(fr)
        return fr

    source_url_for_extraction = _resolve_frozen_source_url(fixture, html_path)

    t0 = time.perf_counter()
    try:
        result = await extract_from_html(
            html,
            source_url=source_url_for_extraction,
            browser_fallback=False,  # frozen mode NEVER invokes Stage 3
            respect_robots=True,
        )
    except Exception as exc:
        fr.error = f"{type(exc).__name__}: {exc}"
        finalize_result(fr)
        return fr
    latency_ms = (time.perf_counter() - t0) * 1000.0

    fr.ok = True
    fr.method = result.method
    fr.confidence = result.confidence
    fr.title = result.title
    fr.text_length = len(result.text_markdown or "")
    fr.word_count = count_words(result.text_markdown or "")
    fr.latency_ms = latency_ms
    fr.hints = result.hints
    fr.quality_checks = score_quality(result, fixture)
    fr.efficiency_checks = score_efficiency(
        result, fixture, mode="frozen", latency_ms=latency_ms,
        cheaper_path_sufficient_result=None,
    )
    finalize_result(fr)
    return fr


# ----------------------------
# Fetch-failure classification (for expected_fetch_failure fixtures)
# ----------------------------


def _classify_fetch_failure(exc: Exception) -> tuple[Optional[int], str]:
    """Return (status_code, kind) for a fetch-layer exception.

    kind is one of:
      "http_status"     — origin returned a 4xx/5xx httpx.HTTPStatusError
                           (e.g. real 404 on a dead URL).
      "http_exception"  — production code refused to proceed via FastAPI
                           HTTPException (e.g. 415 unsupported content type
                           for PDFs, 403 robots, 413 oversized).
      "unknown"         — anything else; status is None.

    Status is the integer the user can match against expected_status_code.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code, "http_status"
    if isinstance(exc, HTTPException):
        return exc.status_code, "http_exception"
    return None, "unknown"


# ----------------------------
# Execution — live
# ----------------------------


async def run_live_fixture(fixture: dict[str, Any]) -> FixtureResult:
    fr = FixtureResult(
        fixture_id=fixture["id"],
        category=fixture.get("category", ""),
        mode="live",
        source_url=fixture["source_url"],
        ok=False,
    )

    t0 = time.perf_counter()
    try:
        result = await read_text(
            fixture["source_url"],
            browser_fallback=True,
            respect_robots=True,
        )
    except Exception as exc:
        # expected_fetch_failure fixtures EXPECT read_text to raise. The
        # exception is the assertion target, not an error. Match it against
        # the optional expected_status_code; if it matches (or no status was
        # specified), record a synthetic fetch_failure_ok check and pass.
        # If it doesn't match, fall through and record the exception
        # normally — a fixture that asked for 404 but got 500 should not
        # quietly pass on "well, it failed somehow".
        if fixture.get("expected_fetch_failure"):
            status, kind = _classify_fetch_failure(exc)
            expected_status = fixture.get("expected_status_code")
            matched = expected_status is None or status == expected_status
            if matched:
                fr.ok = True
                fr.latency_ms = (time.perf_counter() - t0) * 1000.0
                fr.method = None
                fr.quality_checks = [CheckResult(
                    "fetch_failure_ok",
                    True,
                    f"fetch failed as expected: {kind} status={status}",
                    gating=True,
                )]
                # latency_band_ok still applies — a 60s timeout on a 404 is
                # broken even when the 404 itself is expected.
                ceiling = fixture.get("latency_ms_ceiling", LIVE_LATENCY_MS_CEILING)
                lat_ok = fr.latency_ms <= ceiling
                fr.efficiency_checks = [CheckResult(
                    "latency_band_ok",
                    lat_ok,
                    "" if lat_ok else f"latency_ms={fr.latency_ms:.1f} > ceiling={ceiling}",
                    gating=True,
                )]
                finalize_result(fr)
                return fr
            fr.error = (
                f"expected_fetch_failure with expected_status_code={expected_status}, "
                f"but got {type(exc).__name__} status={status} kind={kind}: {exc}"
            )
            finalize_result(fr)
            return fr
        fr.error = f"{type(exc).__name__}: {exc}"
        finalize_result(fr)
        return fr
    latency_ms = (time.perf_counter() - t0) * 1000.0

    # Symmetric guard for expected_fetch_failure: if the fixture declares
    # the request must fail and read_text returned cleanly, that's a hard
    # failure. The whole point of these fixtures is to assert a specific
    # fetch-layer refusal mode — silently treating "extracted fine after
    # all" as a pass would defeat the assertion (e.g. a PDF URL starting
    # to return an HTML wrapper, or a dead URL redirecting to a branded
    # search page).
    if fixture.get("expected_fetch_failure"):
        fr.error = (
            f"expected_fetch_failure=True, but read_text succeeded "
            f"(method={result.method!r}, final_url={result.final_url!r})"
        )
        finalize_result(fr)
        return fr

    fr.ok = True
    fr.method = result.method
    fr.confidence = result.confidence
    fr.title = result.title
    fr.text_length = len(result.text_markdown or "")
    fr.word_count = count_words(result.text_markdown or "")
    fr.latency_ms = latency_ms
    fr.hints = result.hints
    fr.quality_checks = score_quality(result, fixture)

    # Second-pass cheaper-path check: only when primary run picked
    # browser_fallback AND primary quality passed. Otherwise the check
    # is either inapplicable or would be comparing against a broken baseline.
    cheaper_ok: Optional[bool] = None
    primary_quality_pass = all(c.passed for c in fr.quality_checks) if fr.quality_checks else True
    if result.method == "browser_fallback" and primary_quality_pass:
        try:
            cheaper = await read_text(
                fixture["source_url"],
                browser_fallback=False,
                respect_robots=True,
            )
            cheaper_quality = score_quality(cheaper, fixture)
            cheaper_ok = all(c.passed for c in cheaper_quality) if cheaper_quality else True
        except Exception:
            # Exception on the cheap path means the cheap path was
            # definitely NOT sufficient — record that, don't skip.
            cheaper_ok = False

    fr.efficiency_checks = score_efficiency(
        result, fixture, mode="live", latency_ms=latency_ms,
        cheaper_path_sufficient_result=cheaper_ok,
    )
    finalize_result(fr)
    return fr


# ----------------------------
# Aggregation & summary
# ----------------------------


def aggregate(results: list[FixtureResult]) -> dict[str, Any]:
    if not results:
        return {"count": 0}

    okays = [r for r in results if r.ok]
    latencies = [r.latency_ms for r in okays if r.latency_ms is not None]
    methods: dict[str, int] = {}
    for r in okays:
        if r.method:
            methods[r.method] = methods.get(r.method, 0) + 1

    def pct(n: int, total: int) -> float:
        return (100.0 * n / total) if total else 0.0

    browser_launches = methods.get("browser_fallback", 0)

    # Sort method distribution keys so summaries are stable across runs
    # (dict insertion order reflects whatever fixtures happened to come
    # first; sorting makes diffs between runs comparable).
    sorted_methods = {m: methods[m] for m in sorted(methods)}

    agg = {
        "count": len(results),
        "ok_count": len(okays),
        "error_count": len(results) - len(okays),
        "quality_pass_rate_pct": round(pct(sum(1 for r in okays if r.quality_pass), len(okays)), 1),
        "efficiency_pass_rate_pct": round(pct(sum(1 for r in okays if r.efficiency_pass), len(okays)), 1),
        "overall_pass_rate_pct": round(pct(sum(1 for r in okays if r.overall_pass), len(okays)), 1),
        "method_distribution": {m: {"count": n, "pct": round(pct(n, len(okays)), 1)} for m, n in sorted_methods.items()},
        "browser_launch_count": browser_launches,
    }
    if latencies:
        sorted_latencies = sorted(latencies)
        # Nearest-rank p95 using ceil — safer than floor-based indexing,
        # which tends to understate p95 on small samples.
        p95_index = max(0, math.ceil(0.95 * len(sorted_latencies)) - 1)
        agg["latency_ms"] = {
            "p50": round(statistics.median(sorted_latencies), 1),
            "p95": round(sorted_latencies[p95_index], 1),
            "max": round(sorted_latencies[-1], 1),
            "mean": round(statistics.mean(sorted_latencies), 1),
        }
    return agg


def print_summary(mode_label: str, results: list[FixtureResult], agg: dict[str, Any]) -> None:
    print(f"\n=== {mode_label} mode — {agg.get('count', 0)} fixtures ===")
    if agg.get("count", 0) == 0:
        print("  (no fixtures)")
        return
    print(f"  quality pass:    {agg['quality_pass_rate_pct']}%  ({sum(1 for r in results if r.ok and r.quality_pass)}/{agg['ok_count']})")
    print(f"  efficiency pass: {agg['efficiency_pass_rate_pct']}%")
    print(f"  overall pass:    {agg['overall_pass_rate_pct']}%")
    if agg.get("method_distribution"):
        dist = ", ".join(f"{m}={v['pct']}%" for m, v in agg["method_distribution"].items())
        print(f"  method mix:      {dist}")
    print(f"  browser launches: {agg['browser_launch_count']}")
    if "latency_ms" in agg:
        lat = agg["latency_ms"]
        print(f"  latency ms:      p50={lat['p50']}  p95={lat['p95']}  max={lat['max']}")

    failures = [r for r in results if not r.overall_pass or not r.ok]
    if failures:
        print(f"\n  Failures ({len(failures)}):")
        for r in failures:
            if not r.ok:
                print(f"    - [{r.category}] {r.fixture_id}: ERROR {r.error}")
                continue
            reasons: list[str] = []
            for c in r.quality_checks + r.efficiency_checks:
                if not c.passed:
                    tag = "" if c.gating else "[diag] "
                    reasons.append(f"{tag}{c.name}({c.reason})" if c.reason else f"{tag}{c.name}")
            print(f"    - [{r.category}] {r.fixture_id}: {'; '.join(reasons) or 'no reason recorded'}")

    # Surface diag-only observations on otherwise-passing fixtures too —
    # for partial/graceful_failure fixtures these are the actual signal.
    diag_only = [
        r for r in results if r.ok and r.overall_pass
        and any(not c.passed and not c.gating for c in r.quality_checks + r.efficiency_checks)
    ]
    if diag_only:
        print(f"\n  Diagnostic-only observations ({len(diag_only)}):")
        for r in diag_only:
            notes: list[str] = []
            for c in r.quality_checks + r.efficiency_checks:
                if not c.passed and not c.gating:
                    notes.append(f"{c.name}({c.reason})" if c.reason else c.name)
            print(f"    - [{r.category}] {r.fixture_id}: {'; '.join(notes)}")


# ----------------------------
# Regression check
# ----------------------------


def compare_against_baseline(current: list[FixtureResult]) -> list[str]:
    """Return a list of human-readable regression strings vs committed baseline.

    Regression = a fixture that passed quality in the baseline but fails now.
    A new fixture not in the baseline is not a regression.
    """
    if not BASELINE_PATH.exists():
        return []
    try:
        baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        return [f"baseline load error ({type(exc).__name__}): {exc}"]

    base_by_id: dict[str, dict[str, Any]] = {
        f["fixture_id"]: f for f in baseline.get("fixtures", []) if f.get("mode") == "frozen"
    }
    regressions: list[str] = []
    for r in current:
        if r.mode != "frozen":
            continue
        prior = base_by_id.get(r.fixture_id)
        if not prior:
            continue
        if prior.get("quality_pass") and not r.quality_pass:
            regressions.append(f"{r.fixture_id}: quality_pass True → False")
    return regressions


# ----------------------------
# JSON output
# ----------------------------


def _to_jsonable(obj: Any) -> Any:
    if dataclasses.is_dataclass(obj):
        return {k: _to_jsonable(v) for k, v in dataclasses.asdict(obj).items()}
    if isinstance(obj, dict):
        return {k: _to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_to_jsonable(v) for v in obj]
    return obj


def write_json_results(
    results: list[FixtureResult],
    agg_by_mode: dict[str, dict[str, Any]],
    mode_label: str,
    duration_seconds: float,
    regressions: list[str],
) -> Path:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    path = RESULTS_DIR / f"{timestamp}.json"
    payload = {
        "run_meta": {
            "app_version": APP_VERSION,
            "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "mode": mode_label,
            "fixture_count": len(results),
            "duration_seconds": round(duration_seconds, 2),
        },
        "aggregate": agg_by_mode,
        "regressions": regressions,
        "fixtures": [_to_jsonable(r) for r in results],
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


# ----------------------------
# Ad-hoc URL mode
# ----------------------------


async def run_adhoc_url(url: str) -> int:
    t0 = time.perf_counter()
    try:
        result = await read_text(url, browser_fallback=True, respect_robots=True)
    except Exception as exc:
        print(f"[ERR] {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    elapsed = (time.perf_counter() - t0) * 1000.0
    print(f"URL:        {url}")
    print(f"final_url:  {result.final_url}")
    print(f"title:      {result.title}")
    print(f"method:     {result.method}")
    print(f"confidence: {result.confidence:.3f}")
    print(f"latency_ms: {elapsed:.1f}")
    print(f"text_length: {len(result.text_markdown or '')}")
    print(f"word_count:  {count_words(result.text_markdown or '')}")
    print(f"hints:      {json.dumps(result.hints, indent=2)}")
    return 0


# ----------------------------
# CLI
# ----------------------------


async def main_async(args: argparse.Namespace) -> int:
    if args.url:
        return await run_adhoc_url(args.url)

    # --baselines / --tune-thresholds — implemented in benchmarks/baselines.py.
    if args.baselines or args.tune_thresholds:
        try:
            from benchmarks import baselines as bl
        except ImportError as exc:
            print(f"Failed to import benchmarks.baselines: {exc}", file=sys.stderr)
            return 2

        modes = ["frozen", "live"] if args.mode == "both" else [args.mode]

        if args.baselines:
            by_mode = await bl.run_baselines_comparison(
                modes=modes,
                category_substring=args.filter,
                fixture_id=args.id,
            )
            for mode in modes:
                if mode in by_mode:
                    bl.print_comparison_table(mode, by_mode[mode])

            sweep_rows = None
            if args.tune_thresholds and "frozen" in modes:
                sweep_rows = await bl.run_threshold_sweep(
                    category_substring=args.filter, fixture_id=args.id,
                )
                bl.print_threshold_sweep(sweep_rows)

            out_path = bl.write_baselines_json(by_mode, sweep_rows)
            print(f"\nBaselines JSON: {out_path.relative_to(BENCHMARKS_DIR.parent)}")
            return 0

        # --tune-thresholds without --baselines: sweep only.
        if "frozen" not in modes:
            print("--tune-thresholds requires frozen mode (use --mode frozen or --mode both)", file=sys.stderr)
            return 2
        sweep_rows = await bl.run_threshold_sweep(
            category_substring=args.filter, fixture_id=args.id,
        )
        bl.print_threshold_sweep(sweep_rows)
        out_path = bl.write_baselines_json({}, sweep_rows)
        print(f"\nBaselines JSON: {out_path.relative_to(BENCHMARKS_DIR.parent)}")
        return 0

    modes = ["frozen", "live"] if args.mode == "both" else [args.mode]

    all_results: list[FixtureResult] = []
    agg_by_mode: dict[str, dict[str, Any]] = {}
    start = time.perf_counter()

    for mode in modes:
        path = FROZEN_FIXTURES_PATH if mode == "frozen" else LIVE_FIXTURES_PATH
        fixtures = filter_fixtures(
            load_fixtures(path),
            category_substring=args.filter,
            fixture_id=args.id,
        )
        if not fixtures:
            print(f"[{mode}] no fixtures match the filter — skipping")
            agg_by_mode[mode] = {"count": 0}
            continue

        runner = run_frozen_fixture if mode == "frozen" else run_live_fixture
        # Serial on purpose: keeps timing honest and avoids Chromium
        # contention in live mode. Frozen set (target ~25-30) is fast enough.
        results = [await runner(f) for f in fixtures]
        all_results.extend(results)
        agg_by_mode[mode] = aggregate(results)
        print_summary(mode, results, agg_by_mode[mode])

    duration = time.perf_counter() - start

    # Regression check only makes sense for frozen-mode results.
    frozen_results = [r for r in all_results if r.mode == "frozen"]
    regressions = compare_against_baseline(frozen_results) if frozen_results else []
    if regressions:
        print("\nRegressions vs baseline:")
        for msg in regressions:
            print(f"  - {msg}")

    # Write JSON artifact even on partial runs.
    mode_label = args.mode
    out_path = write_json_results(all_results, agg_by_mode, mode_label, duration, regressions)
    print(f"\nResults JSON: {out_path.relative_to(BENCHMARKS_DIR.parent)}")
    print(f"Duration:     {duration:.2f}s")

    # Exit-code policy: frozen mode is the only gate.
    if "frozen" in modes:
        frozen_agg = agg_by_mode.get("frozen", {})
        if frozen_agg.get("count", 0) > 0:
            if frozen_agg.get("error_count", 0) > 0:
                return 1
            if frozen_agg.get("quality_pass_rate_pct", 0.0) < 85.0:
                return 1
            if regressions:
                return 1
    return 0


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="GhostReader benchmark runner.")
    p.add_argument("--mode", choices=["frozen", "live", "both"], default="both",
                   help="Which fixture set to run (default: both).")
    p.add_argument("--filter", help="Case-insensitive substring match against fixture.category.")
    p.add_argument("--id", help="Run a single fixture by id.")
    p.add_argument("--url", help="Ad-hoc single URL through the full pipeline; no scoring.")
    p.add_argument("--baselines", action="store_true",
                   help="Also run baselines for comparison (requires benchmarks/baselines.py).")
    p.add_argument("--tune-thresholds", action="store_true",
                   help="Sweep confidence thresholds and report curves (requires benchmarks/baselines.py).")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    sys.exit(asyncio.run(main_async(args)))


if __name__ == "__main__":
    main()

"""Baseline comparison & threshold sweep for the GhostReader benchmark.

Two responsibilities, deliberately kept in one module so the runner has
a single integration point:

1. Baselines (mode-split, because static snapshots cannot fairly evaluate
   a JS-rendering baseline):
     frozen-mode rows:
       - baseline_raw_html        — raw HTML body bytes after truncation.
                                    Token-size straw man.
       - baseline_trafilatura_only — Stage 1 only (no embedded-data merge).
       - ghostreader_stages_1_2   — full GhostReader, browser_fallback=False.
     live-mode rows:
       - baseline_playwright_only — always render with Playwright. Uses the
                                    helper below to extract a title from the
                                    rendered page so the quality comparison
                                    is fair (otherwise title_ok would always
                                    fail and the row would understate).
       - ghostreader_full         — full GhostReader incl. Stage 3.

2. Post-hoc threshold sweep over the frozen corpus. Records each fixture's
   per-stage texts and scores ONCE, then evaluates a grid of candidate
   (raw_threshold, combined_threshold) pairs without re-running extraction.
   The constraint combined_T <= raw_T mirrors the production invariant
   (Stage 2 should be at least as eager to accept as Stage 1).

Imported lazily by run.py so a missing Playwright install only breaks
--baselines / --tune-thresholds, not the core frozen run.
"""

from __future__ import annotations

import dataclasses
import json
import statistics
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import httpx
from selectolax.lexbor import LexborHTMLParser

from app import (
    USER_AGENT,
    TIMEOUT,
    PLAYWRIGHT_TIMEOUT_MS,
    PLAYWRIGHT_WAIT_AFTER_LOAD_MS,
    PLAYWRIGHT_ABORT_RESOURCE_TYPES,
    ENABLE_BROWSER_FALLBACK,
    check_robots_allowed,
    clean_whitespace,
    extract_title,
    extract_with_trafilatura,
    extract_from_json_ld,
    extract_from_next_data,
    extract_from_common_hydration_blobs,
    score_text,
    truncate_text,
    ReadResponse,
)

# Reuse the runner's scoring + execution scaffolding so baseline rows
# and GhostReader rows are evaluated through identical machinery.
from benchmarks.run import (
    BENCHMARKS_DIR,
    RESULTS_DIR,
    FROZEN_FIXTURES_PATH,
    LIVE_FIXTURES_PATH,
    FixtureResult,
    aggregate,
    count_words,
    finalize_result,
    load_fixtures,
    run_frozen_fixture,
    run_live_fixture,
    score_efficiency,
    score_quality,
    _resolve_frozen_source_url,
)

# ----------------------------
# Data
# ----------------------------


@dataclass
class ApproachResult:
    """One approach (e.g. 'baseline_raw_html') across one mode's fixture set."""
    name: str
    mode: str  # "frozen" | "live"
    fixtures: list[FixtureResult] = field(default_factory=list)
    aggregate: dict[str, Any] = field(default_factory=dict)


# ----------------------------
# Playwright helper used only by the baseline
# ----------------------------


async def render_playwright_with_title(url: str) -> tuple[Optional[str], str]:
    """Render `url` in headless Chromium and return (title, extracted_text).

    Mirrors render_with_playwright's resource-blocking, robots, and
    timeout patterns, but additionally captures page.title() so the
    playwright_only baseline has a fair shot at title_ok.

    Returns (None, "") when:
      - browser fallback is disabled in this build
      - playwright isn't installed
      - robots disallows the URL

    The caller (run_playwright_only_live) treats empty text as an
    execution error rather than a quality failure — see comment there.

    Title extraction order:
      1. page.title() — what the live document reports
      2. <title> tag from page.content() via extract_title — fallback for
         pages whose JS sets title late
    """
    if not ENABLE_BROWSER_FALLBACK:
        return None, ""

    try:
        from playwright.async_api import async_playwright
    except Exception:
        return None, ""

    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True, http2=True) as client:
        # Baseline always respects robots — we should never benchmark by
        # being a worse web citizen than production behaviour.
        allowed = await check_robots_allowed(client, url, respect_robots=True)
        if not allowed:
            return None, ""

    network_texts: list[str] = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=USER_AGENT,
            service_workers="block",
        )

        async def route_handler(route):
            if route.request.resource_type in PLAYWRIGHT_ABORT_RESOURCE_TYPES:
                await route.abort()
                return
            await route.continue_()

        await context.route("**/*", route_handler)
        page = await context.new_page()

        async def on_response(response) -> None:
            try:
                ctype = response.headers.get("content-type", "")
                if "json" not in ctype and "text" not in ctype:
                    return
                body = clean_whitespace(await response.text())
                if len(body) >= 300:
                    network_texts.append(body[:6000])
            except Exception:
                return

        page.on("response", on_response)
        await page.goto(url, wait_until="domcontentloaded", timeout=PLAYWRIGHT_TIMEOUT_MS)
        await page.wait_for_timeout(PLAYWRIGHT_WAIT_AFTER_LOAD_MS)

        # Title first — cheap and reflects post-JS state.
        page_title: Optional[str] = None
        try:
            t = await page.title()
            page_title = t.strip() or None
        except Exception:
            page_title = None

        html = await page.content()
        await context.close()
        await browser.close()

    # Title fallback via the same extractor production uses on raw HTML.
    if not page_title:
        try:
            page_title = extract_title(LexborHTMLParser(html)) or None
        except Exception:
            page_title = None

    extracted = extract_with_trafilatura(html, url)
    if extracted:
        return page_title, extracted

    # Fall back to network-text capture, same as render_with_playwright.
    return page_title, truncate_text(clean_whitespace("\n\n".join(network_texts[:3])))


# ----------------------------
# Adapter: synthesize a FixtureResult from a (title, text, method) tuple
# ----------------------------


def _score_synthetic(
    fixture: dict[str, Any],
    *,
    mode: str,
    method: str,
    approach: str,
    title: Optional[str],
    text: str,
    latency_ms: float,
    error: Optional[str] = None,
) -> FixtureResult:
    """Run the same scoring scaffolding as the GhostReader runner, but on
    text/title produced by an alternative approach. ResultType-shaped just
    enough for score_quality / score_efficiency to consume."""
    fr = FixtureResult(
        fixture_id=fixture["id"],
        category=fixture.get("category", ""),
        mode=mode,
        source_url=fixture["source_url"],
        ok=error is None,
        error=error,
        approach=approach,
    )
    if error is not None:
        finalize_result(fr)
        return fr

    fr.method = method
    fr.confidence = None  # baselines have no comparable confidence
    fr.title = title
    fr.text_length = len(text or "")
    fr.word_count = count_words(text or "")
    fr.latency_ms = latency_ms
    fr.hints = None

    # Build a minimal stand-in for the scorer. ReadResponse is built by the
    # production code; here we construct one with just the fields the
    # quality/efficiency scorers read.
    proxy = ReadResponse(
        url=fixture["source_url"],
        final_url=fixture["source_url"],
        title=title or "",
        text_markdown=text or "",
        method=method,
        confidence=0.0,
        from_cache=False,
        extracted_at=int(time.time()),
        hints={},
    )

    fr.quality_checks = score_quality(proxy, fixture)
    fr.efficiency_checks = score_efficiency(
        proxy, fixture, mode=mode, latency_ms=latency_ms,
        cheaper_path_sufficient_result=None,
    )
    finalize_result(fr)
    return fr


# ----------------------------
# Frozen baselines
# ----------------------------


async def run_raw_html_frozen(fixture: dict[str, Any]) -> FixtureResult:
    """Token-size straw man: hand the model the truncated HTML body.

    Title is set to the <title> tag (extract_title) so title_ok is fair —
    otherwise this row would fail title_ok for a reason unrelated to
    'how much does GhostReader save vs. the dumbest possible approach'.
    """
    APPROACH = "baseline_raw_html"
    html_path = BENCHMARKS_DIR / fixture["html_path"]
    if not html_path.exists():
        return _score_synthetic(
            fixture, mode="frozen", method="raw_html", approach=APPROACH,
            title=None, text="", latency_ms=0.0,
            error=f"missing snapshot: {html_path.relative_to(BENCHMARKS_DIR)}",
        )
    t0 = time.perf_counter()
    try:
        html = html_path.read_bytes().decode("utf-8", errors="replace")
        title = extract_title(LexborHTMLParser(html))
        text = truncate_text(clean_whitespace(html))
    except Exception as exc:
        return _score_synthetic(
            fixture, mode="frozen", method="raw_html", approach=APPROACH,
            title=None, text="", latency_ms=(time.perf_counter() - t0) * 1000.0,
            error=f"{type(exc).__name__}: {exc}",
        )
    latency_ms = (time.perf_counter() - t0) * 1000.0
    return _score_synthetic(
        fixture, mode="frozen", method="raw_html", approach=APPROACH,
        title=title, text=text, latency_ms=latency_ms,
    )


async def run_trafilatura_only_frozen(fixture: dict[str, Any]) -> FixtureResult:
    """Stage 1 only — measures what Stage 2 (embedded-data merge) adds."""
    APPROACH = "baseline_trafilatura_only"
    html_path = BENCHMARKS_DIR / fixture["html_path"]
    if not html_path.exists():
        return _score_synthetic(
            fixture, mode="frozen", method="raw_html", approach=APPROACH,
            title=None, text="", latency_ms=0.0,
            error=f"missing snapshot: {html_path.relative_to(BENCHMARKS_DIR)}",
        )
    source_url = _resolve_frozen_source_url(fixture, html_path)
    t0 = time.perf_counter()
    try:
        html = html_path.read_bytes().decode("utf-8", errors="replace")
        tree = LexborHTMLParser(html)
        title = extract_title(tree)
        text = extract_with_trafilatura(html, source_url)
    except Exception as exc:
        return _score_synthetic(
            fixture, mode="frozen", method="raw_html", approach=APPROACH,
            title=None, text="", latency_ms=(time.perf_counter() - t0) * 1000.0,
            error=f"{type(exc).__name__}: {exc}",
        )
    latency_ms = (time.perf_counter() - t0) * 1000.0
    return _score_synthetic(
        fixture, mode="frozen", method="raw_html", approach=APPROACH,
        title=title, text=text, latency_ms=latency_ms,
    )


# ----------------------------
# Live baseline
# ----------------------------


async def run_playwright_only_live(fixture: dict[str, Any]) -> FixtureResult:
    """Always render with Playwright. The baseline_playwright_only row.

    Empty text from render_playwright_with_title is treated as an execution
    error rather than a quality failure: it indicates the baseline never
    ran (Playwright disabled, not installed, robots-blocked, or render
    failure). Recording it as ok=True would attribute 'Playwright extracts
    badly' to what is really 'Playwright never executed' — exactly the
    kind of distortion this harness exists to prevent. It also keeps the
    browser-launch count honest (ok=False rows aren't counted).
    """
    APPROACH = "baseline_playwright_only"
    t0 = time.perf_counter()
    try:
        title, text = await render_playwright_with_title(fixture["source_url"])
    except Exception as exc:
        return _score_synthetic(
            fixture, mode="live", method="browser_fallback", approach=APPROACH,
            title=None, text="", latency_ms=(time.perf_counter() - t0) * 1000.0,
            error=f"{type(exc).__name__}: {exc}",
        )
    latency_ms = (time.perf_counter() - t0) * 1000.0

    if not text:
        return _score_synthetic(
            fixture, mode="live", method="browser_fallback", approach=APPROACH,
            title=title, text="", latency_ms=latency_ms,
            error="playwright baseline returned empty text (disabled, unavailable, robots-blocked, or render failure)",
        )

    return _score_synthetic(
        fixture, mode="live", method="browser_fallback", approach=APPROACH,
        title=title, text=text, latency_ms=latency_ms,
    )


# ----------------------------
# Browser launch counting per approach
# ----------------------------


def browser_launches_for(approach_name: str, fixtures: list[FixtureResult]) -> int:
    """A row's browser-launch count. Definition is approach-specific:

      - playwright_only — every ok fixture launched Chromium exactly once.
      - ghostreader_full — only fixtures whose final method was browser_fallback.
      - ghostreader_stages_1_2 / trafilatura_only / raw_html — never launches.
    """
    if approach_name == "baseline_playwright_only":
        return sum(1 for r in fixtures if r.ok)
    if approach_name == "ghostreader_full":
        return sum(1 for r in fixtures if r.ok and r.method == "browser_fallback")
    return 0


# ----------------------------
# Aggregation & comparison output
# ----------------------------


def aggregate_approach(approach: ApproachResult) -> dict[str, Any]:
    agg = aggregate(approach.fixtures)
    # Override browser_launch_count with the per-approach definition.
    # The runner's aggregate counts based on result.method, which is
    # correct for ghostreader rows but uninformative for baselines.
    agg["browser_launch_count"] = browser_launches_for(approach.name, approach.fixtures)
    return agg


def print_comparison_table(mode: str, approaches: list[ApproachResult]) -> None:
    print(f"\n=== Comparison ({mode} mode) ===")
    if not approaches:
        print("  (no approaches)")
        return

    if mode == "frozen":
        header = ("approach", "qual%", "p50ms", "p95ms", "mean_chars")
    else:
        header = ("approach", "qual%", "p50ms", "p95ms", "mean_chars", "browsers")

    rows: list[tuple[str, ...]] = [header]
    for ap in approaches:
        agg = ap.aggregate
        qual = agg.get("quality_pass_rate_pct", 0.0)
        lat = agg.get("latency_ms", {})
        chars = [r.text_length for r in ap.fixtures if r.ok and r.text_length is not None]
        mean_chars = round(statistics.mean(chars), 0) if chars else 0
        if mode == "frozen":
            rows.append((
                ap.name,
                f"{qual}",
                f"{lat.get('p50', '-')}",
                f"{lat.get('p95', '-')}",
                f"{int(mean_chars)}",
            ))
        else:
            rows.append((
                ap.name,
                f"{qual}",
                f"{lat.get('p50', '-')}",
                f"{lat.get('p95', '-')}",
                f"{int(mean_chars)}",
                f"{agg.get('browser_launch_count', 0)}",
            ))

    widths = [max(len(r[c]) for r in rows) for c in range(len(header))]
    for i, row in enumerate(rows):
        line = "  " + "  ".join(cell.ljust(widths[c]) for c, cell in enumerate(row))
        print(line)
        if i == 0:
            print("  " + "  ".join("-" * widths[c] for c in range(len(header))))


# ----------------------------
# Threshold sweep (frozen, post-hoc)
# ----------------------------


@dataclass
class _SweepRecord:
    fixture_id: str
    title: str
    raw_text: str
    raw_score: float
    combined_text: str
    combined_score: float


async def _collect_sweep_records(fixtures: list[dict[str, Any]]) -> list[tuple[dict[str, Any], _SweepRecord]]:
    """Run extraction stages once per frozen fixture and capture both stage
    outputs + their scores. Threshold candidates are then evaluated
    post-hoc — no per-threshold re-extraction.

    Edge-case fixtures (expected_outcome != "success") are skipped:
    they measure graceful failure, not extraction quality, so their
    pass/fail under varying thresholds carries no signal about the
    right acceptance threshold for normal pages. Including them would
    muddy the curve.
    """
    out: list[tuple[dict[str, Any], _SweepRecord]] = []
    for fx in fixtures:
        if fx.get("expected_outcome", "success") != "success":
            continue
        html_path = BENCHMARKS_DIR / fx["html_path"]
        if not html_path.exists():
            continue
        source_url = _resolve_frozen_source_url(fx, html_path)
        html = html_path.read_bytes().decode("utf-8", errors="replace")
        tree = LexborHTMLParser(html)
        title = extract_title(tree) or ""

        raw_text = extract_with_trafilatura(html, source_url)
        raw_score, _ = score_text(raw_text, title)

        jsonld_text = extract_from_json_ld(tree)
        next_text = extract_from_next_data(tree)
        hydration_text = extract_from_common_hydration_blobs(tree)
        combined = truncate_text(
            clean_whitespace(
                "\n\n".join(p for p in [raw_text, jsonld_text, next_text, hydration_text] if p)
            )
        )
        combined_score, _ = score_text(combined, title)

        out.append((fx, _SweepRecord(
            fixture_id=fx["id"],
            title=title,
            raw_text=raw_text,
            raw_score=raw_score,
            combined_text=combined,
            combined_score=combined_score,
        )))
    return out


def _simulate_at_thresholds(
    fixture: dict[str, Any],
    rec: _SweepRecord,
    *,
    raw_t: float,
    combined_t: float,
) -> tuple[str, str, str]:
    """Return (method, title, text) the pipeline WOULD have produced at
    these thresholds, assuming frozen mode (no Stage 3) and the simplified
    model that does NOT include the small-static-page early-accept path.
    The simplification is documented; the goal is a comparable curve, not
    a bit-for-bit replay of production logic."""
    if rec.raw_score >= raw_t:
        return "raw_html", rec.title, rec.raw_text
    if rec.combined_score >= combined_t:
        return "embedded_data", rec.title, rec.combined_text
    best_text = rec.combined_text or rec.raw_text
    return "best_effort", rec.title, best_text


def _sweep_grid() -> list[tuple[float, float]]:
    """Pairs (raw_t, combined_t) on a 0.05 grid in [0.35, 0.75], constrained
    to combined_t <= raw_t (Stage 2 should be at least as eager as Stage 1)."""
    steps = [round(0.35 + 0.05 * i, 2) for i in range(int((0.75 - 0.35) / 0.05) + 1)]
    pairs: list[tuple[float, float]] = []
    for raw_t in steps:
        for combined_t in steps:
            if combined_t <= raw_t:
                pairs.append((raw_t, combined_t))
    return pairs


def evaluate_threshold_sweep(records: list[tuple[dict[str, Any], _SweepRecord]]) -> list[dict[str, Any]]:
    """Per (raw_t, combined_t): quality_pass_rate, method distribution."""
    rows: list[dict[str, Any]] = []
    for raw_t, combined_t in _sweep_grid():
        passes = 0
        method_counts: dict[str, int] = {}
        for fx, rec in records:
            method, title, text = _simulate_at_thresholds(fx, rec, raw_t=raw_t, combined_t=combined_t)
            method_counts[method] = method_counts.get(method, 0) + 1
            proxy = ReadResponse(
                url=fx["source_url"],
                final_url=fx["source_url"],
                title=title,
                text_markdown=text,
                method=method,
                confidence=0.0,
                from_cache=False,
                extracted_at=int(time.time()),
                hints={},
            )
            checks = score_quality(proxy, fx)
            if all(c.passed for c in checks) if checks else True:
                passes += 1
        n = len(records)
        rows.append({
            "raw_threshold": raw_t,
            "combined_threshold": combined_t,
            "quality_pass_rate_pct": round(100.0 * passes / n, 1) if n else 0.0,
            "method_distribution": {m: method_counts[m] for m in sorted(method_counts)},
            "fixture_count": n,
        })
    return rows


def print_threshold_sweep(rows: list[dict[str, Any]]) -> None:
    print("\n=== Threshold sweep (frozen, post-hoc) ===")
    print("  Production thresholds: raw=0.56, combined=0.45")
    print("  raw_t  comb_t   qual%   raw_html / embedded / best_effort")
    print("  -----  ------   -----   --------------------------------")
    for r in rows:
        md = r["method_distribution"]
        dist = f"{md.get('raw_html', 0)} / {md.get('embedded_data', 0)} / {md.get('best_effort', 0)}"
        print(f"  {r['raw_threshold']:>5}  {r['combined_threshold']:>5}   {r['quality_pass_rate_pct']:>5}   {dist}")


# ----------------------------
# Entry points called by run.py
# ----------------------------


async def run_baselines_comparison(
    *,
    modes: list[str],
    category_substring: Optional[str],
    fixture_id: Optional[str],
) -> dict[str, list[ApproachResult]]:
    """Run every baseline + GhostReader equivalent for each requested mode.

    Returns {mode: [ApproachResult, ...]} so the runner can both print and
    serialize. Frozen runs are serial (cheap, deterministic). Live runs
    are serial too — Chromium contention + flaky network make parallelism
    a false economy at this scale."""
    from benchmarks.run import filter_fixtures  # late import to avoid cycle at import time

    out: dict[str, list[ApproachResult]] = {}

    if "frozen" in modes:
        fxs = filter_fixtures(
            load_fixtures(FROZEN_FIXTURES_PATH),
            category_substring=category_substring, fixture_id=fixture_id,
        )
        approaches = [
            ApproachResult(name="baseline_raw_html", mode="frozen"),
            ApproachResult(name="baseline_trafilatura_only", mode="frozen"),
            ApproachResult(name="ghostreader_stages_1_2", mode="frozen"),
        ]
        for fx in fxs:
            approaches[0].fixtures.append(await run_raw_html_frozen(fx))
            approaches[1].fixtures.append(await run_trafilatura_only_frozen(fx))
            # GhostReader rows reuse the production runner; stamp the
            # approach name here so per-fixture JSON is symmetric with
            # the baseline rows. The production runner intentionally
            # leaves approach=None for its own (non-comparison) runs.
            gr = await run_frozen_fixture(fx)
            gr.approach = "ghostreader_stages_1_2"
            approaches[2].fixtures.append(gr)
        for ap in approaches:
            ap.aggregate = aggregate_approach(ap)
        out["frozen"] = approaches

    if "live" in modes:
        fxs = filter_fixtures(
            load_fixtures(LIVE_FIXTURES_PATH),
            category_substring=category_substring, fixture_id=fixture_id,
        )
        # Playwright cannot honor expected_fetch_failure semantics: Chromium
        # happily renders 404 wrapper pages and tries to download PDFs. The
        # baseline isn't measuring the same thing as ghostreader_full on
        # those fixtures. Skip them in the baseline column only — they
        # still run under ghostreader_full where the semantics are honored,
        # and they still appear in the per-fixture JSON under that approach.
        pw_eligible = [f for f in fxs if not f.get("expected_fetch_failure")]
        skipped_pw = len(fxs) - len(pw_eligible)
        if skipped_pw:
            print(
                f"  (skipped {skipped_pw} expected_fetch_failure fixture"
                f"{'s' if skipped_pw != 1 else ''} from baseline_playwright_only)"
            )
        approaches = [
            ApproachResult(name="baseline_playwright_only", mode="live"),
            ApproachResult(name="ghostreader_full", mode="live"),
        ]
        for fx in fxs:
            if not fx.get("expected_fetch_failure"):
                approaches[0].fixtures.append(await run_playwright_only_live(fx))
            gr = await run_live_fixture(fx)
            gr.approach = "ghostreader_full"
            approaches[1].fixtures.append(gr)
        for ap in approaches:
            ap.aggregate = aggregate_approach(ap)
        out["live"] = approaches

    return out


async def run_threshold_sweep(
    *,
    category_substring: Optional[str],
    fixture_id: Optional[str],
) -> list[dict[str, Any]]:
    from benchmarks.run import filter_fixtures
    fxs = filter_fixtures(
        load_fixtures(FROZEN_FIXTURES_PATH),
        category_substring=category_substring, fixture_id=fixture_id,
    )
    records = await _collect_sweep_records(fxs)
    return evaluate_threshold_sweep(records)


# ----------------------------
# JSON serialization
# ----------------------------


def _to_jsonable(obj: Any) -> Any:
    if dataclasses.is_dataclass(obj):
        return {k: _to_jsonable(v) for k, v in dataclasses.asdict(obj).items()}
    if isinstance(obj, dict):
        return {k: _to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_to_jsonable(v) for v in obj]
    return obj


def write_baselines_json(
    by_mode: dict[str, list[ApproachResult]],
    sweep: Optional[list[dict[str, Any]]],
) -> Path:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    path = RESULTS_DIR / f"{timestamp}-baselines.json"
    payload: dict[str, Any] = {
        "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "comparison": {
            mode: [
                {
                    "name": ap.name,
                    "aggregate": ap.aggregate,
                    "fixtures": [_to_jsonable(r) for r in ap.fixtures],
                }
                for ap in approaches
            ]
            for mode, approaches in by_mode.items()
        },
    }
    if sweep is not None:
        payload["threshold_sweep"] = sweep
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path

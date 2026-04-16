# Changelog

All notable changes to this project will be documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Planned
- Split into a `ghost_reader` core library (in-process) and a `ghost_reader_service` HTTP wrapper.
- Threshold tuning informed by the v0.2.0 frozen sweep (raw_t has measurable headroom; combined_t needs JSON-LD-heavy fixtures to be tunable).
- Smarter walking of JSON-LD shapes (`articleBody`, `text`, `description`) instead of the current "long string" heuristic.
- Content-driven Playwright wait strategy (selector / network-idle with cap) to replace the fixed sleep.
- PyPI release alongside the v0.3.0 library split.

## [0.2.0] — 2026-04-16

Benchmark harness release. No extractor logic or thresholds changed in this release — by design. v0.2.0 measures v0.1.0 behavior so v0.3.0 can tune it from evidence.

### Added

- **Benchmark harness** (`benchmarks/`):
  - **Frozen mode** — deterministic regression gate over 27 stored HTML snapshots across 10 categories (Wikipedia, long-form news, tech blog, documentation, Next.js SPA, hydration-heavy, JSON-LD-heavy, aggregator, short static, edge cases). CPU-only; runs in ~6 seconds; never invokes Playwright.
  - **Live mode** — reality check over current URLs. Allowed to drift; never fails the build.
  - **Quality vs efficiency scoring** as separate questions — `title_ok`, `required_recall`, `forbidden_clean`, `word_count_ok`, `method_allowed` for quality; `method_ok`, `latency_band_ok`, `cheaper_path_sufficient` (live only) for efficiency.
  - **Baseline comparisons** — `--baselines` runs `baseline_raw_html`, `baseline_trafilatura_only`, `ghostreader_stages_1_2` (frozen) and `baseline_playwright_only`, `ghostreader_full` (live). Apples-to-apples: `expected_fetch_failure` fixtures are skipped from the Playwright baseline column with a printed note.
  - **Threshold sweep** — `--tune-thresholds` evaluates a 0.35–0.75 grid of (raw_t, combined_t) pairs post-hoc, without re-running extraction. v0.2.0 ships analysis only; tuning is deferred to v0.3.0.
  - **Committed regression baseline** at `benchmarks/results/v0.1.0-frozen-baseline.json`. The runner compares against it on every frozen run; quality regressions fail the exit code.
- **Fixture schema** with explicit outcome classes:
  - `expected_outcome` ∈ `{"success", "partial", "graceful_failure"}` — drives whether checks gate or are observation-only.
  - `allowed_methods` — hard correctness gate on extraction method, distinct from the soft `preferred_methods` efficiency hint.
  - `expected_fetch_failure` (live only) + optional `expected_status_code` — asserts that `read_text` raises a specific failure (e.g. PDF refusal at HTTPException 415, real 404 as httpx.HTTPStatusError). Both an exception with the wrong status and a clean success are recorded as hard failures.
  - `expected_status_code: 404` is also the per-fixture opt-in for the narrow `--allow-404` capture path described below.
- **Capture script** (`benchmarks/capture.py`) — invokes the production fetch path to populate `corpus/<id>/page.html` and `corpus/<id>/meta.json` with provenance (final URL, content hash, fetched_at, user-agent, status code).
  - **`--allow-404` flag** — narrow, opt-in capture of HTTP 404 bodies for fixtures whose own `expected_status_code` is 404. Requires both the CLI flag and the per-fixture declaration; either alone is inert. Validates HTML/XML content-type, non-empty body, and applies the standard `MAX_HTML_BYTES` size cap. `status_code=404` is recorded in `meta.json` so the non-2xx provenance is unmistakable.
- **`extract_from_html()`** factored out as a public extraction entry point. `read_text(url, ...)` is preserved as a thin fetch-then-extract wrapper, positional-compatible with v0.1.0 callers. This is ~80% of the v0.3.0 library-split work pre-paid: `extract_from_html` is the function the future `ghost_reader` core library will export.
- **Benchmark target shim** (`benchmark_target.py`) — the harness imports `read_text`, `extract_from_html`, `ResultType` through this shim rather than directly from `app`, so the v0.3.0 rename of `ReadResponse` to a frozen dataclass touches one file instead of the harness.

### Findings (observed by the harness, not changes to behavior)

- **Stage 2 (`embedded_data`) did not materially contribute on the current corpus.** Across all 27 frozen fixtures the merged Stage-1+2 text was byte-identical to Stage-1-only output. This is a corpus signal (or a Stage-1-bar signal), not a defect — the threshold sweep shows Stage 2 would fire if `raw_t` were raised from 0.56 toward 0.65–0.70.
- **Production thresholds sit on a flat 100% quality plateau.** The sweep found no quality regression anywhere in `raw_t ∈ [0.35, 0.7]` × `combined_t ∈ [0.35, raw_t]`. First regression appears at `raw_t = 0.75`. Plenty of headroom.
- **Stage 1 dominates the frozen corpus** at 88.9% of fixtures (24/27), with the remaining 11.1% (3/27) handled by `best_effort` on edge/partial fixtures (JS-required shell, 404 page, disambiguation page).
- **On the live comparison set Stage 3 never fired** — both happy-path live fixtures (long-form Wikipedia and the Next.js marketing homepage) were sufficient at Stage 1, while Playwright-only spent ~4s rendering them. Single snapshot, not a universal claim, but exactly what `cheaper_path_sufficient` was designed to surface.

### Changed

- Status: v0.1.0 → v0.2.0; badge updated to "v0.2.0 — benchmarked".
- Docker tag examples bumped from `0.1.0` to `0.2.0`.
- Roadmap: benchmark-harness item moved out of "near-term" (delivered); JSON-LD walk and threshold tuning reframed as the v0.3.0 priorities most likely to wake Stage 2 up.

### Not changed (explicitly)

- No extractor logic changes. No threshold changes. No scoring-function changes. v0.2.0 is a measurement release; v0.3.0 will be the tuning release.
- `read_text` public signature is unchanged; positional-compatible with v0.1.0 callers.
- No new production runtime dependencies. The harness reuses existing extractor dependencies (`selectolax`, `trafilatura`) rather than introducing a separate benchmark-only stack.

## [0.1.0] — 2026-04-15

Initial public release. Single-file FastAPI service that reads a URL and returns clean markdown for AI agents.

### Added
- **Staged extraction pipeline** with confidence-based escalation:
  - Stage 1 — raw HTML fetched via `httpx`, main text extracted with `trafilatura`.
  - Stage 2 — embedded data: JSON-LD, `__NEXT_DATA__`, and common hydration blobs (`__APOLLO_STATE__`, `__NUXT__`, `__PRELOADED_STATE__`, `__INITIAL_STATE__`).
  - Stage 3 — Playwright/Chromium render fallback, with images / media / fonts / service workers blocked.
- **Heuristic confidence score** over extracted text (word count, sentence count, paragraph length, title overlap, lexical diversity, link ratio) used to drive stage escalation.
- **Small-static-page early-accept** at stage 1: short pages with strong title overlap return without escalating to the browser, avoiding wasted Chromium launches on legitimately-short content.
- **SQLite response cache** with TTL, bypass header (`X-Cache-Bypass`), and an admin purge endpoint.
- **`robots.txt` respect** with per-host parser caching.
- **Allow / block domain policy** via env vars.
- **Retry with exponential backoff and jitter** on transient HTTP errors (connect, timeout, 429, 5xx).
- **Configurable user-agent**, request timeouts, max HTML / text size, and Playwright wait timing — all via env vars.
- **`/healthz`** and **`/admin/purge-cache`** operational endpoints.
- **Dockerfile** with all Chromium runtime dependencies.

### Known limitations
- Single-process / single-worker; concurrent browser-fallback requests serialize on Chromium.
- No paywall, PDF, authenticated, or infinite-scroll handling.
- Confidence thresholds (0.56 / 0.45) and the small-static threshold (50 KB) are heuristic and not yet validated against a benchmark set.
- No automated test suite.

[Unreleased]: https://github.com/madeinplutofabio/ghost-reader/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/madeinplutofabio/ghost-reader/releases/tag/v0.2.0
[0.1.0]: https://github.com/madeinplutofabio/ghost-reader/releases/tag/v0.1.0

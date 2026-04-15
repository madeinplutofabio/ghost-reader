# Changelog

All notable changes to this project will be documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Planned
- Split into a `ghost_reader` core library (in-process) and a `ghost_reader_service` HTTP wrapper.
- Benchmark harness with ground-truth text for a representative URL set, so confidence thresholds can be measured rather than guessed.
- Smarter walking of JSON-LD shapes (`articleBody`, `text`, `description`) instead of the current "long string" heuristic.
- Content-driven Playwright wait strategy (selector / network-idle with cap) to replace the fixed sleep.
- PyPI release alongside the v0.2.0 library split.

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

[Unreleased]: https://github.com/madeinplutofabio/ghost-reader/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/madeinplutofabio/ghost-reader/releases/tag/v0.1.0

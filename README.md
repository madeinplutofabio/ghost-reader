# GhostReader

[![Release](https://img.shields.io/github/v/release/madeinplutofabio/ghost-reader?include_prereleases&label=release&color=blue)](https://github.com/madeinplutofabio/ghost-reader/releases)
[![License](https://img.shields.io/github/license/madeinplutofabio/ghost-reader)](https://github.com/madeinplutofabio/ghost-reader/blob/main/LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue?logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115%2B-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![Playwright](https://img.shields.io/badge/Playwright-Chromium-2EAD33?logo=playwright&logoColor=white)](https://playwright.dev/python/)
[![Status](https://img.shields.io/badge/status-v0.2.0%20%E2%80%94%20benchmarked-orange)](CHANGELOG.md)

**A token-frugal web reader for AI agents.** Staged extraction skips the browser when it can — raw HTML first, then framework-embedded JSON, and only as a last resort a real Chromium render.

> **Status: v0.2.0 — local / single-user, with a benchmark harness.** v0.1.0 was the proof of the idea; v0.2.0 adds a regression net and the numbers below. Still not hardened for shared or production use. See the roadmap.

## Why it exists

The standard way agents read the web is to launch a headless browser, render every page, hand the resulting HTML blob to an LLM and ask it to figure out the content. That works, and it also wastes tokens, money and seconds on every URL.

A lot of modern pages already contain the article text, product details or post body inside the initial HTML response — embedded as JSON in things like `__NEXT_DATA__`, JSON-LD scripts, Apollo / Nuxt / Redux hydration blobs. The browser exists to paint that data onto a screen for a human. Your agent doesn't need a screen, it just needs the data.

GhostReader is the small tool that exploits that:

1. **Stage 1 — raw HTML.** Fetch with `httpx`, extract main text with `trafilatura`. Most articles, blogs, docs and Wikipedia pages return cleanly here in 200–500 ms.
2. **Stage 2 — embedded data.** Parse JSON-LD, `__NEXT_DATA__` and common hydration blobs. Walk the JSON for the actual content. Catches a huge slice of modern SPAs without ever launching a browser.
3. **Stage 3 — browser fallback.** Only when the cheap paths fail, render with Playwright/Chromium (images, media and fonts blocked).

A confidence score from a heuristic over the extracted text decides when to escalate.

## What you get in practice

- **~20× fewer input tokens** than handing raw HTML to a model (measured on a Wikipedia article: ~500 KB raw HTML vs. ~25 KB clean markdown).
- **~5–10× lower latency on the cheap path** vs. browser-everything approaches (~300 ms vs. 2–5 s).
- **Zero per-call cost**, no rate limits, no third party in your URL stream — unlike paid readers like Firecrawl or Jina.
- **SQLite response cache** with TTL, so repeated reads of the same URL are free.

## Benchmark

v0.2.0 ships a small benchmark harness (`benchmarks/`) that runs the extractor against two fixture sets and scores quality and efficiency as separate questions. The committed numbers below describe **v0.1.0 extractor behavior, measured by the v0.2.0 harness** — no extractor logic or thresholds changed in this release; the harness exists so future changes can be measured rather than guessed.

Two modes:

- **Frozen mode** is the regression gate. It runs against stored HTML snapshots in `benchmarks/corpus/` so results are deterministic — no network, no Playwright. It exercises Stages 1 and 2 only; Stage 3 against a static snapshot would measure nothing real.
- **Live mode** is a reality check. It fetches current URLs through the full pipeline including Stage 3, and is allowed to drift; it never fails the build.

### Frozen baseline (27 fixtures across 10 categories)

| approach | quality pass | mean chars | p50 latency |
|---|---|---|---|
| raw HTML dump | 14.8% | 91,086 | 7 ms |
| GhostReader stages 1+2 | **100.0%** | **15,795** | 67 ms |

Hand the model the raw HTML and 85% of fixtures fail quality (cookie banners, nav, "Create account" all leak through `forbidden_phrases`). Hand it GhostReader's frozen stages-1+2 output and quality is clean — at ~5.8× smaller payload. Method mix on the corpus: **88.9% raw_html, 11.1% best_effort, 0% embedded_data** — the best_effort cases are all edge/partial fixtures, not happy-path articles. Stage 2 is present in the pipeline but did not materially contribute on the current corpus; the threshold sweep shows it would fire if the Stage-1 acceptance bar were raised, which is a v0.3 follow-up.

### Live reality check (4 fixtures, including 2 fetch-failure edges)

The Playwright-only baseline column compares apples-to-apples: 2 `expected_fetch_failure` fixtures (a PDF and a real 404) are skipped from that column, because Chromium has no concept of refusing to render — it would download the PDF and render the 404 wrapper as if it were content. Both fixtures still run under GhostReader, which refuses them at the fetch boundary.

| approach | quality pass | p50 latency | p95 latency | browser launches |
|---|---|---|---|---|
| Playwright-only baseline | 100.0% (2/2) | 4151 ms | 4188 ms | 2 |
| GhostReader full | **100.0% (4/4)** | **561 ms** | **717 ms** | **0** |

Both happy-path fixtures (a long-form Wikipedia article and the Next.js marketing homepage) were sufficient at Stage 1 — Stage 3 never fired. The PDF was refused with `HTTPException 415` in 580 ms; the dead Wikipedia URL surfaced as `httpx.HTTPStatusError 404` in 314 ms. Single snapshot, not a universal claim — but on this set, Playwright-only spent ~4 seconds doing what GhostReader handled in ~500 ms with near-identical extracted text size and the same quality on the comparable set.

### Run it locally

```bash
python -m benchmarks.run --mode frozen           # regression gate, ~6s, no network
python -m benchmarks.run --mode live             # reality check, fetches current URLs
python -m benchmarks.run --baselines             # comparison tables vs raw-html / Playwright
python -m benchmarks.run --tune-thresholds       # post-hoc threshold sweep (frozen only)
```

Frozen mode is CPU-only and completes in ~6 seconds on a typical laptop. Full harness docs in `benchmarks/README.md`.

## Run locally

```bash
python -m venv .venv
source .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium
uvicorn app:app --reload
```

## Example

```bash
curl "http://127.0.0.1:8000/read?url=https://en.wikipedia.org/wiki/Web_scraping"
```

Response (truncated):

```json
{
  "url": "https://en.wikipedia.org/wiki/Web_scraping",
  "title": "Web scraping - Wikipedia",
  "text_markdown": "**Web scraping**, **web harvesting**, ...",
  "method": "raw_html",
  "confidence": 0.95,
  "from_cache": false,
  "hints": { "stage": 1, "stats": { "word_count": 4093, ... } }
}
```

Bypass the cache:

```bash
curl -H "X-Cache-Bypass: true" "http://127.0.0.1:8000/read?url=..."
```

## Docker

```bash
docker build -t ghostreader:0.2.0 .
docker run --rm -p 8000:8000 ghostreader:0.2.0
```

## Configuration

All env vars are optional.

| Variable | Default | Purpose |
|---|---|---|
| `GHOSTREADER_ENABLE_BROWSER_FALLBACK` | `true` | Set to `false` to disable Playwright stage. |
| `GHOSTREADER_ALLOW_DOMAINS` | *(empty)* | Comma-separated allowlist. **Set this if any URL source is untrusted** — otherwise this is an SSRF amplifier. |
| `GHOSTREADER_BLOCK_DOMAINS` | *(empty)* | Comma-separated blocklist. |
| `GHOSTREADER_CACHE_TTL_SECONDS` | `21600` (6h) | Cache lifetime per entry. |
| `GHOSTREADER_CACHE_DB` | `/tmp/ghostreader_cache.sqlite3` | SQLite cache path. |
| `GHOSTREADER_FETCH_RETRIES` | `2` | Retries for transient HTTP errors. |
| `GHOSTREADER_TIMEOUT_SECONDS` | `12` | Per-request timeout. |
| `GHOSTREADER_SMALL_STATIC_HTML_BYTES` | `50000` | HTML size below which a short page is accepted at stage 1 without escalation. |

## Notes

- `respect_robots=true` is the default on the `/read` endpoint.
- The cache key includes the normalized URL, the user-agent string, and the `browser_fallback` flag.
- Browser fallback uses Playwright with service workers blocked and image/media/font requests aborted to keep render cost down.

## Roadmap

The plan is to evolve this from "a script you run locally" into **a library + a service**, so the same core extraction logic can be called either in-process by a Python agent or over HTTP by anything else:

- **`ghost_reader/`** — pure-Python core package. No FastAPI dependency. Exports `read_text(url, ...) -> ReadResult`. Importable directly into your agent code for the lowest possible latency and zero network hop.
- **`ghost_reader_service/`** — thin FastAPI wrapper around the core. Same logic, exposed over HTTP. Useful when the agent isn't Python, or when you want a single shared cache across many agents.

Other things on the near-term list:
- Threshold tuning informed by the v0.2.0 sweep — the curve has measurable headroom; v0.3 will move on it.
- Smarter walking of JSON-LD shapes (`articleBody`, `text`, `description`) instead of the current "long string" heuristic. The frozen corpus showed Stage 2 idle on every fixture; this is the change most likely to wake it up.
- Content-driven Playwright wait strategy (selector / network-idle with cap) instead of the fixed sleep.
- Concurrency / queue layer in the service flavor.
- **PyPI release** alongside the v0.3.0 library split, so `pip install ghost-reader` gives you the in-process reader directly.

## License

Apache 2.0.

---

Maintained by [![Linkedin](https://i.sstatic.net/gVE0j.png) @fmsalvadori](https://www.linkedin.com/in/fmsalvadori/)
&nbsp;
[![GitHub](https://i.sstatic.net/tskMh.png) MadeInPluto](https://github.com/madeinplutofabio)

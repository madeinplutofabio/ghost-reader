# GhostReader

[![Release](https://img.shields.io/github/v/release/madeinplutofabio/ghost-reader?include_prereleases&label=release&color=blue)](https://github.com/madeinplutofabio/ghost-reader/releases)
[![License: MIT](https://img.shields.io/github/license/madeinplutofabio/ghost-reader?color=green)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue?logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115%2B-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![Playwright](https://img.shields.io/badge/Playwright-Chromium-2EAD33?logo=playwright&logoColor=white)](https://playwright.dev/python/)
[![Status](https://img.shields.io/badge/status-v0.1.0%20%E2%80%94%20local%20use-orange)](CHANGELOG.md)

**A token-frugal web reader for AI agents.** Staged extraction skips the browser when it can — raw HTML first, then framework-embedded JSON, and only as a last resort a real Chromium render.

> **Status: v0.1.0 — local / single-user.** This is the early proof of the idea. It's stable enough to run alongside your own agent work, but it isn't hardened for shared or production use yet. See the roadmap below.

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
docker build -t ghostreader:0.1.0 .
docker run --rm -p 8000:8000 ghostreader:0.1.0
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
- A small benchmark harness with ground-truth text for a representative set of URLs (news, blogs, SPAs, docs, ecommerce), so the confidence thresholds can be measured rather than guessed.
- Smarter walking of JSON-LD shapes (`articleBody`, `text`, `description`) instead of the current "long string" heuristic.
- Content-driven Playwright wait strategy (selector / network-idle with cap) instead of the fixed sleep.
- Concurrency / queue layer in the service flavor.
- **PyPI release** alongside the v0.2.0 library split, so `pip install ghost-reader` gives you the in-process reader directly.

## License

MIT.

# GhostReader benchmarks

The harness for measuring extractor behavior — quality and efficiency as separate questions, with two modes and a committed regression baseline. See the [Benchmark section in the main README](../README.md#benchmark) for the headline numbers.

## Two modes

| | Frozen | Live |
|---|---|---|
| **Source** | Stored HTML in `corpus/<id>/page.html` | Live URLs fetched on every run |
| **Stages exercised** | 1 + 2 only (Stage 3 against a static snapshot would measure nothing real) | 1 + 2 + 3 (full pipeline) |
| **Network** | None | Yes |
| **Playwright** | Never | Yes (Stage 3 + Playwright-only baseline) |
| **Determinism** | Byte-stable across runs | Allowed to drift |
| **Role** | Regression gate — fails the run on quality regressions vs committed baseline | Reality check — never fails the run |
| **Typical wall-clock** | ~6s for 27 fixtures | ~30–60s for 4 fixtures with `--baselines` |

The split exists because a deterministic benchmark cannot depend on the live web (consent walls, marketing copy, JS bundles all change), and a static HTML snapshot cannot reproduce the conditions under which Playwright is valuable. So frozen is the gate; live is the reality check.

## Run

```bash
python -m benchmarks.run                     # both modes (exit code from frozen only)
python -m benchmarks.run --mode frozen       # regression gate
python -m benchmarks.run --mode live         # reality check
python -m benchmarks.run --baselines         # comparison tables vs raw-html / Playwright
python -m benchmarks.run --tune-thresholds   # post-hoc threshold sweep (frozen only)
python -m benchmarks.run --filter wikipedia  # category substring filter
python -m benchmarks.run --id <fixture_id>   # single fixture
python -m benchmarks.run --url https://...   # ad-hoc URL, no scoring
```

Frozen mode emits `benchmarks/results/<timestamp>.json`; baselines emit `<timestamp>-baselines.json`. The committed regression anchor is `benchmarks/results/v0.1.0-frozen-baseline.json` — this is the only result file in `results/` that's tracked in git (see `results/.gitignore`).

## Capture an HTML snapshot

```bash
python -m benchmarks.capture                       # capture all not-yet-captured fixtures
python -m benchmarks.capture --id <fixture_id>     # capture one fixture
python -m benchmarks.capture --refresh             # re-capture, overwriting existing
python -m benchmarks.capture --refresh --id <id>   # re-capture one
python -m benchmarks.capture --no-robots           # skip robots.txt (use sparingly)
python -m benchmarks.capture --allow-404           # see "404 capture" below
```

Capture uses the production `fetch_html` so the snapshot reflects exactly what the live pipeline would receive (same user-agent, timeouts, HTTP/2, robots policy). Each capture also writes `corpus/<id>/meta.json` with provenance: final URL after redirects, content-type, status code, fetched-at timestamp, content SHA-256, byte length, user-agent, robots policy.

By design, capture does **not** overwrite existing snapshots — the frozen corpus is a regression anchor and must not mutate silently. Pass `--refresh` to force.

### 404 capture (narrow, opt-in)

`--allow-404` permits capture of HTTP 404 bodies, but only for fixtures whose own `expected_status_code` is `404`. Both the CLI flag and the per-fixture declaration are required; either alone is inert. The 404 path additionally validates content-type (`html`/`xml`), non-empty body, and the standard `MAX_HTML_BYTES` size cap. The recorded `meta.json` carries `status_code: 404` so the non-2xx provenance is unmistakable.

## Add a fixture

1. Pick a category that's underrepresented (look at `fixtures_frozen.json` to see the current spread).
2. Pre-clear the URL with a real GET — confirm it returns 200 with HTML, not a 404 / 403 / paywall / cookie wall.
3. Add an entry to `fixtures_frozen.json` (frozen) or `fixtures_live.json` (live) with the fields below.
4. Run `python -m benchmarks.capture --id <new_id>` to populate the snapshot.
5. Run `python -m benchmarks.run --mode frozen --id <new_id>` and inspect what was extracted. Most assertion mismatches are easier to fix by widening bounds or shortening required phrases than by re-engineering the fixture.
6. Once the fixture passes cleanly and you intend to keep it, re-freeze the baseline by copying the latest `benchmarks/results/<timestamp>.json` over `benchmarks/results/v0.1.0-frozen-baseline.json`. Commit both the new fixture and the new baseline together.

## Fixture schema

Required:

- `id` — stable, unique, snake_case. Used for `corpus/<id>/` path and the `--id` filter.
- `source_url` — the URL captured (frozen) or fetched live. After redirects, frozen mode prefers the recorded `meta.final_url` for extraction so trafilatura resolves relative links against the right base.
- `category` — substring used by `--filter`. Group fixtures so categories aren't dominated by one shape.
- `html_path` — frozen only. `corpus/<id>/page.html`. Confined under `corpus/` by the capture script.

Quality assertions (skip any that don't apply):

- `expected_title_contains` — case-insensitive substring of the returned title.
- `required_phrases` — every phrase must appear (case-insensitive) in extracted text. All-or-nothing.
- `forbidden_phrases` — none may appear. Catches cookie banners, "Create account", etc.
- `min_word_count` / `max_word_count` — range bounds on extracted word count.

Efficiency assertions:

- `preferred_methods` — soft efficiency hint. Failure is recorded as `method_ok`; it gates only for `success` fixtures.
- `latency_ms_ceiling` — per-fixture override of the mode-wide latency ceiling (frozen 2000ms, live 15000ms). Always gates.

Outcome class (controls which checks gate vs observe):

- `expected_outcome` ∈ `{"success", "partial", "graceful_failure"}`. Default: `"success"`.
  - For `partial` and `graceful_failure`, only `forbidden_clean`, `method_allowed`, and `latency_band_ok` gate. Everything else becomes diagnostic-only — observed and printed, but not pass/fail.

Hard correctness gate (always gating when set):

- `allowed_methods` — list of acceptable methods. Distinct from `preferred_methods`: this is "the extractor MUST land in one of these buckets," not "ideally would." Use for graceful-failure fixtures: `allowed_methods: ["best_effort"]`.

Live-only fetch-failure assertions:

- `expected_fetch_failure: true` — declares that `read_text` must raise. Both an unexpected success and an exception with the wrong status are recorded as hard failures.
- `expected_status_code` — narrows the expected failure: `415` for PDFs (HTTPException at the content-type guard), `404` for dead URLs (httpx.HTTPStatusError from `raise_for_status`).

Conventional:

- `notes` — free-form. Document what the fixture is exercising and what would trigger swapping it. Future-you will thank present-you.

## Files

```
benchmarks/
├── README.md                 — this file
├── fixtures_frozen.json      — frozen corpus declarations
├── fixtures_live.json        — live smoke declarations
├── corpus/<id>/
│   ├── page.html             — captured HTML snapshot
│   └── meta.json             — capture provenance
├── run.py                    — runner + scorer + CLI
├── baselines.py              — baseline approaches + threshold sweep
├── capture.py                — snapshot capture
└── results/
    ├── v0.1.0-frozen-baseline.json   — committed regression anchor (tracked)
    ├── <timestamp>.json              — per-run artifacts (gitignored)
    └── <timestamp>-baselines.json    — per-run baseline artifacts (gitignored)
```

## Imports

The harness imports through `benchmark_target.py` at the project root, which re-exports `read_text`, `extract_from_html`, `score_text`, `stats_for_text`, and `ResultType` from `app`. When v0.3.0 splits the core out into a `ghost_reader` library, only this shim changes — the harness keeps working unchanged.

`capture.py` is the one exception: it reaches past the shim into `app.fetch_html`, `app.USER_AGENT`, and `app.MAX_HTML_BYTES` because those are fetch-layer internals that the public benchmark surface intentionally doesn't expose.

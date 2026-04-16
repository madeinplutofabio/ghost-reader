"""Capture HTML snapshots for frozen-mode fixtures.

Populates corpus/<id>/page.html and corpus/<id>/meta.json for each entry in
fixtures_frozen.json. Reuses the project's own fetch_html so the captured
HTML reflects exactly what the live pipeline would receive — same
user-agent, same timeouts, same HTTP/2 negotiation, same robots policy.

By design this does NOT overwrite existing snapshots. The frozen corpus is
a regression anchor; it must not mutate silently. Pass --refresh to force
recapture of already-captured fixtures.

Usage (must be invoked from the project root, or from an environment where
the project is installed editable, so `-m benchmarks.capture` resolves):
    python -m benchmarks.capture
        Capture any fixtures that do not yet have a page.html on disk.
    python -m benchmarks.capture --id wikipedia_web_scraping
        Capture a single fixture by id.
    python -m benchmarks.capture --refresh
        Re-capture every fixture, overwriting existing snapshots.
    python -m benchmarks.capture --refresh --id nextjs_blog_next14
        Re-capture one fixture specifically.
    python -m benchmarks.capture --no-robots
        Skip robots.txt check. Use sparingly; note in commit message.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
import time
from pathlib import Path
from typing import Any

# We import from app directly rather than through benchmark_target.py because
# fetch_html and USER_AGENT are fetch-layer internals, not part of the
# benchmark_target public surface (which intentionally exposes only the
# extraction entry points and scoring helpers). The shim exists to protect
# the harness runner from the v0.3 library split; the capture script is
# allowed to reach past it into fetch internals.
import httpx

from app import fetch_html, USER_AGENT, MAX_HTML_BYTES

BENCHMARKS_DIR = Path(__file__).resolve().parent
FIXTURES_PATH = BENCHMARKS_DIR / "fixtures_frozen.json"
CORPUS_DIR = (BENCHMARKS_DIR / "corpus").resolve()


def load_fixtures() -> list[dict[str, Any]]:
    with FIXTURES_PATH.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    if data.get("schema_version") != 1:
        raise SystemExit(
            f"Unsupported fixtures schema_version: {data.get('schema_version')!r}"
        )
    fixtures = data.get("fixtures")
    if not isinstance(fixtures, list):
        raise SystemExit("fixtures_frozen.json is missing a top-level 'fixtures' list")
    return fixtures


def resolve_html_path(fixture: dict[str, Any]) -> Path:
    """Resolve fixture html_path and confine it under CORPUS_DIR.

    Guards against a malformed or malicious fixture writing to arbitrary
    locations via relative-path tricks (../../etc/passwd, absolute paths,
    symlink escapes). We resolve both sides and check prefix containment.
    """
    html_rel = Path(fixture["html_path"])
    candidate = (BENCHMARKS_DIR / html_rel).resolve()
    try:
        candidate.relative_to(CORPUS_DIR)
    except ValueError:
        raise SystemExit(
            f"Fixture {fixture.get('id')!r} html_path {fixture.get('html_path')!r} "
            f"resolves outside benchmarks/corpus/ ({candidate}). Refusing to write."
        )
    return candidate


async def capture_one(
    fixture: dict[str, Any],
    *,
    refresh: bool,
    respect_robots: bool,
    allow_404: bool,
) -> tuple[str, str]:
    """Capture one fixture. Returns (status, detail) where status is
    'captured' | 'skipped' | 'failed'."""
    fixture_id = fixture["id"]
    source_url = fixture["source_url"]
    html_path = resolve_html_path(fixture)
    meta_path = html_path.parent / "meta.json"

    if html_path.exists() and not refresh:
        return "skipped", f"{fixture_id}: page.html already present (use --refresh to overwrite)"

    # Per-fixture opt-in for the narrow 404 capture path. CLI flag enables it
    # globally; the fixture must self-declare expected_status_code=404 to be
    # eligible. Both required so a stray --allow-404 --refresh cannot silently
    # downgrade a future-broken fixture from a clean 200 to a 404 capture.
    expected_status = fixture.get("expected_status_code", 200)
    fixture_allows_404 = allow_404 and expected_status == 404

    try:
        html_text, final_url, content_type = await fetch_html(
            source_url, respect_robots=respect_robots
        )
        status_code = 200
    except httpx.HTTPStatusError as exc:
        resp = exc.response
        if not (fixture_allows_404 and resp is not None and resp.status_code == 404):
            return "failed", f"{fixture_id}: {type(exc).__name__}: {exc}"
        ct = resp.headers.get("content-type", "")
        if "html" not in ct and "xml" not in ct:
            return "failed", f"{fixture_id}: 404 with non-HTML content-type {ct!r}; refusing capture"
        raw_bytes = resp.content
        if len(raw_bytes) > MAX_HTML_BYTES:
            return "failed", f"{fixture_id}: 404 body too large ({len(raw_bytes)} > {MAX_HTML_BYTES} bytes); refusing capture"
        body = resp.text
        if not body.strip():
            return "failed", f"{fixture_id}: 404 with empty body; refusing capture"
        html_text, final_url, content_type, status_code = body, str(resp.url), ct, 404
    except Exception as exc:
        return "failed", f"{fixture_id}: {type(exc).__name__}: {exc}"

    html_path.parent.mkdir(parents=True, exist_ok=True)
    html_bytes = html_text.encode("utf-8")
    html_path.write_bytes(html_bytes)

    meta = {
        "schema_version": 1,
        "fixture_id": fixture_id,
        "source_url": source_url,
        "final_url": final_url,
        "content_type": content_type,
        "status_code": status_code,
        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "content_hash_sha256": hashlib.sha256(html_bytes).hexdigest(),
        "user_agent": USER_AGENT,
        "respect_robots": respect_robots,
        "byte_length": len(html_bytes),
    }
    meta_path.write_text(
        json.dumps(meta, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    return "captured", f"{fixture_id}: {len(html_bytes)} bytes -> {html_path.relative_to(BENCHMARKS_DIR)}"


async def main_async(args: argparse.Namespace) -> int:
    fixtures = load_fixtures()

    if args.id:
        fixtures = [f for f in fixtures if f["id"] == args.id]
        if not fixtures:
            print(f"No fixture with id={args.id!r} in fixtures_frozen.json", file=sys.stderr)
            return 2

    counts = {"captured": 0, "skipped": 0, "failed": 0}
    for fixture in fixtures:
        status, detail = await capture_one(
            fixture,
            refresh=args.refresh,
            respect_robots=not args.no_robots,
            allow_404=args.allow_404,
        )
        counts[status] += 1
        prefix = {"captured": "[OK]  ", "skipped": "[--]  ", "failed": "[ERR] "}[status]
        print(prefix + detail)

    print(
        f"\nSummary: captured={counts['captured']} "
        f"skipped={counts['skipped']} failed={counts['failed']}"
    )
    return 1 if counts["failed"] else 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Capture HTML snapshots for frozen fixtures.")
    parser.add_argument(
        "--id",
        help="Capture only the fixture with this id.",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Re-capture fixtures even if page.html already exists.",
    )
    parser.add_argument(
        "--no-robots",
        action="store_true",
        help="Skip robots.txt check when capturing. Use sparingly.",
    )
    parser.add_argument(
        "--allow-404",
        action="store_true",
        help=(
            "Permit capture of HTTP 404 responses (HTML/XML body, non-empty, "
            "under the standard size cap). Only applies to fixtures whose own "
            "'expected_status_code' is 404. Recorded as status_code=404 in "
            "meta.json so the non-2xx provenance is unmistakable."
        ),
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    exit_code = asyncio.run(main_async(args))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()

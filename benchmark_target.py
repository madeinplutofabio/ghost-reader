"""Benchmark import surface for GhostReader.

The benchmark harness imports from this module, never from `app` directly.
This keeps the harness insulated from v0.3's planned library/service split:
when `ReadResponse` is renamed or the core moves out of `app.py`, only this
shim changes — `benchmarks/` stays untouched.

Exports:
    read_text           — fetch + extract (full pipeline entry point)
    extract_from_html   — extraction against provided HTML (frozen mode
                          uses this with browser_fallback=False)
    score_text          — confidence scorer, used by threshold-sweep tooling
    stats_for_text      — text statistics, used for diagnostics in results
    ResultType          — the return type of read_text / extract_from_html;
                          harness code should annotate against this alias,
                          not against the concrete class name.
"""

from app import (
    ReadResponse as ResultType,
    extract_from_html,
    read_text,
    score_text,
    stats_for_text,
)

__all__ = [
    "ResultType",
    "extract_from_html",
    "read_text",
    "score_text",
    "stats_for_text",
]

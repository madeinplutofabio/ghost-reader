from __future__ import annotations

import asyncio
import hashlib
import json
import os
import random
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urljoin, urlparse, urlunparse
from urllib.robotparser import RobotFileParser

import httpx
import trafilatura
from fastapi import FastAPI, Header, HTTPException, Query, Response
from pydantic import BaseModel, Field
from selectolax.lexbor import LexborHTMLParser

APP_NAME = "GhostReader"
APP_VERSION = "0.1.0"

USER_AGENT = os.getenv(
    "GHOSTREADER_USER_AGENT",
    f"{APP_NAME}/{APP_VERSION} (+local text retrieval broker)",
)

REQUEST_TIMEOUT_SECONDS = float(os.getenv("GHOSTREADER_TIMEOUT_SECONDS", "12"))
REQUEST_CONNECT_TIMEOUT_SECONDS = float(os.getenv("GHOSTREADER_CONNECT_TIMEOUT_SECONDS", "5"))
MAX_HTML_BYTES = int(os.getenv("GHOSTREADER_MAX_HTML_BYTES", "3000000"))
SMALL_STATIC_HTML_BYTES = int(os.getenv("GHOSTREADER_SMALL_STATIC_HTML_BYTES", "50000"))
MAX_TEXT_CHARS = int(os.getenv("GHOSTREADER_MAX_TEXT_CHARS", "120000"))
FETCH_RETRIES = int(os.getenv("GHOSTREADER_FETCH_RETRIES", "2"))
BACKOFF_BASE_SECONDS = float(os.getenv("GHOSTREADER_BACKOFF_BASE_SECONDS", "0.5"))
CACHE_TTL_SECONDS = int(os.getenv("GHOSTREADER_CACHE_TTL_SECONDS", str(6 * 60 * 60)))
ROBOTS_CACHE_TTL_SECONDS = int(os.getenv("GHOSTREADER_ROBOTS_CACHE_TTL_SECONDS", str(2 * 60 * 60)))
ENABLE_BROWSER_FALLBACK = os.getenv("GHOSTREADER_ENABLE_BROWSER_FALLBACK", "true").lower() == "true"
PLAYWRIGHT_TIMEOUT_MS = int(os.getenv("GHOSTREADER_PLAYWRIGHT_TIMEOUT_MS", "15000"))
PLAYWRIGHT_WAIT_AFTER_LOAD_MS = int(os.getenv("GHOSTREADER_PLAYWRIGHT_WAIT_AFTER_LOAD_MS", "1200"))
PLAYWRIGHT_ABORT_RESOURCE_TYPES = {
    "image",
    "media",
    "font",
}

CACHE_DB_PATH = Path(os.getenv("GHOSTREADER_CACHE_DB", "/tmp/ghostreader_cache.sqlite3"))
ALLOW_DOMAINS = {
    item.strip().lower()
    for item in os.getenv("GHOSTREADER_ALLOW_DOMAINS", "").split(",")
    if item.strip()
}
BLOCK_DOMAINS = {
    item.strip().lower()
    for item in os.getenv("GHOSTREADER_BLOCK_DOMAINS", "").split(",")
    if item.strip()
}

TIMEOUT = httpx.Timeout(
    REQUEST_TIMEOUT_SECONDS,
    connect=REQUEST_CONNECT_TIMEOUT_SECONDS,
)

DEFAULT_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.8",
}

TRANSIENT_HTTPX_EXCEPTIONS = (
    httpx.ConnectError,
    httpx.ReadTimeout,
    httpx.WriteTimeout,
    httpx.PoolTimeout,
    httpx.RemoteProtocolError,
)

app = FastAPI(title=APP_NAME, version=APP_VERSION)


class ReadResponse(BaseModel):
    url: str
    final_url: str
    title: Optional[str] = None
    text_markdown: str = Field(default="")
    method: str
    confidence: float
    from_cache: bool = False
    extracted_at: int
    hints: dict[str, Any] = Field(default_factory=dict)


class CacheEntry(BaseModel):
    url_key: str
    payload: ReadResponse
    expires_at: int


@dataclass
class RobotsState:
    parser: Optional[RobotFileParser]
    fetched_at: int


class CacheStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS page_cache (
                    url_key TEXT PRIMARY KEY,
                    payload_json TEXT NOT NULL,
                    expires_at INTEGER NOT NULL,
                    created_at INTEGER NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_page_cache_expires_at ON page_cache(expires_at)"
            )
            conn.commit()

    def get(self, url_key: str) -> Optional[ReadResponse]:
        now = int(time.time())
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload_json, expires_at FROM page_cache WHERE url_key = ?",
                (url_key,),
            ).fetchone()
            if not row:
                return None
            if int(row["expires_at"]) <= now:
                conn.execute("DELETE FROM page_cache WHERE url_key = ?", (url_key,))
                conn.commit()
                return None
            payload = json.loads(row["payload_json"])
            payload["from_cache"] = True
            return ReadResponse.model_validate(payload)

    def set(self, url_key: str, payload: ReadResponse, ttl_seconds: int) -> None:
        now = int(time.time())
        expires_at = now + ttl_seconds
        serialized = payload.model_dump_json()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO page_cache (url_key, payload_json, expires_at, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(url_key) DO UPDATE SET
                    payload_json = excluded.payload_json,
                    expires_at = excluded.expires_at,
                    created_at = excluded.created_at
                """,
                (url_key, serialized, expires_at, now),
            )
            conn.commit()

    def purge_expired(self) -> int:
        now = int(time.time())
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM page_cache WHERE expires_at <= ?", (now,))
            conn.commit()
            return cur.rowcount


cache_store = CacheStore(CACHE_DB_PATH)
robots_cache: dict[str, RobotsState] = {}


@app.on_event("startup")
async def startup_event() -> None:
    cache_store.purge_expired()


# ----------------------------
# Helpers
# ----------------------------


def normalize_url(raw_url: str) -> str:
    parsed = urlparse(raw_url.strip())
    if parsed.scheme not in {"http", "https"}:
        raise HTTPException(status_code=400, detail="Only http and https URLs are supported")
    cleaned = parsed._replace(fragment="")
    return urlunparse(cleaned)



def get_hostname(url: str) -> str:
    hostname = urlparse(url).hostname
    if not hostname:
        raise HTTPException(status_code=400, detail="Invalid URL hostname")
    return hostname.lower()



def domain_matches(hostname: str, domain_rules: set[str]) -> bool:
    return any(hostname == rule or hostname.endswith(f".{rule}") for rule in domain_rules)



def enforce_domain_policy(url: str) -> None:
    hostname = get_hostname(url)
    if BLOCK_DOMAINS and domain_matches(hostname, BLOCK_DOMAINS):
        raise HTTPException(status_code=403, detail=f"Blocked domain: {hostname}")
    if ALLOW_DOMAINS and not domain_matches(hostname, ALLOW_DOMAINS):
        raise HTTPException(status_code=403, detail=f"Domain not in allowlist: {hostname}")



def url_cache_key(url: str, browser_fallback: bool) -> str:
    key_material = json.dumps(
        {
            "url": normalize_url(url),
            "browser_fallback": browser_fallback,
            "ua": USER_AGENT,
        },
        sort_keys=True,
    )
    return hashlib.sha256(key_material.encode("utf-8")).hexdigest()



def clean_whitespace(text: str) -> str:
    text = re.sub(r"\r\n?", "\n", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()



def truncate_text(text: str, limit: int = MAX_TEXT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n\n[truncated]"



def extract_title(tree: LexborHTMLParser) -> Optional[str]:
    title_node = tree.css_first("meta[property='og:title']")
    if title_node:
        content = title_node.attributes.get("content")
        if content:
            return clean_whitespace(content)[:300]
    title_node = tree.css_first("title")
    if title_node:
        title = clean_whitespace(title_node.text())
        return title[:300] if title else None
    return None



def safe_json_loads(raw: str) -> Optional[Any]:
    try:
        return json.loads(raw)
    except Exception:
        return None



def walk_for_long_strings(obj: Any, min_len: int = 140) -> list[str]:
    results: list[str] = []

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)
        elif isinstance(value, str):
            text = clean_whitespace(value)
            if (
                len(text) >= min_len
                and "{" not in text
                and "<" not in text
                and not text.startswith("http")
            ):
                results.append(text)

    walk(obj)
    return results



def stats_for_text(text: str) -> dict[str, float]:
    paragraphs = [p.strip() for p in text.split("\n") if p.strip()]
    words = re.findall(r"\b\w+\b", text)
    sentences = re.findall(r"[^.!?\n]+[.!?]", text)
    links = re.findall(r"https?://|www\.", text)
    bullets = re.findall(r"^(?:[-*]|\d+\.)\s", text, flags=re.MULTILINE)
    lines = [line for line in text.split("\n") if line.strip()]
    short_lines = sum(1 for line in lines if len(line.strip()) < 40)

    unique_words = {w.lower() for w in words}
    lexical_diversity = (len(unique_words) / max(len(words), 1)) if words else 0.0
    short_line_ratio = short_lines / max(len(lines), 1)
    link_ratio = len(links) / max(len(words), 1)

    return {
        "paragraphs": float(len(paragraphs)),
        "word_count": float(len(words)),
        "sentence_count": float(len(sentences)),
        "bullet_count": float(len(bullets)),
        "avg_paragraph_length": float(sum(len(p) for p in paragraphs) / max(len(paragraphs), 1)),
        "lexical_diversity": lexical_diversity,
        "short_line_ratio": short_line_ratio,
        "link_ratio": link_ratio,
    }



def score_text(text: str, title: Optional[str]) -> tuple[float, dict[str, float]]:
    stats = stats_for_text(text)
    score = 0.0

    word_count = stats["word_count"]
    if word_count >= 700:
        score += 0.28
    elif word_count >= 300:
        score += 0.22
    elif word_count >= 140:
        score += 0.14
    elif word_count >= 70:
        score += 0.07

    sentence_count = stats["sentence_count"]
    if sentence_count >= 12:
        score += 0.18
    elif sentence_count >= 6:
        score += 0.12
    elif sentence_count >= 3:
        score += 0.06

    paragraphs = stats["paragraphs"]
    if paragraphs >= 6:
        score += 0.14
    elif paragraphs >= 3:
        score += 0.08
    elif paragraphs >= 2:
        score += 0.04

    avg_par_len = stats["avg_paragraph_length"]
    if avg_par_len >= 220:
        score += 0.12
    elif avg_par_len >= 120:
        score += 0.08

    if title:
        title_tokens = [t.lower() for t in re.findall(r"\w+", title) if len(t) > 3]
        if title_tokens:
            overlap = sum(1 for tok in title_tokens if tok in text.lower()) / len(title_tokens)
            score += min(0.10, overlap * 0.10)

    if stats["lexical_diversity"] >= 0.25:
        score += 0.06
    if stats["short_line_ratio"] <= 0.35:
        score += 0.05
    if stats["link_ratio"] <= 0.01:
        score += 0.05
    if stats["bullet_count"] >= 1:
        score += 0.02

    return max(0.0, min(1.0, score)), stats


async def backoff_sleep(attempt: int) -> None:
    jitter = random.uniform(0.0, 0.2)
    await asyncio.sleep(BACKOFF_BASE_SECONDS * (2 ** attempt) + jitter)


async def fetch_with_retries(client: httpx.AsyncClient, url: str) -> httpx.Response:
    last_error: Optional[Exception] = None
    for attempt in range(FETCH_RETRIES + 1):
        try:
            response = await client.get(url, headers=DEFAULT_HEADERS)
            response.raise_for_status()
            return response
        except TRANSIENT_HTTPX_EXCEPTIONS as exc:
            last_error = exc
            if attempt < FETCH_RETRIES:
                await backoff_sleep(attempt)
                continue
            break
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status in {429, 500, 502, 503, 504} and attempt < FETCH_RETRIES:
                last_error = exc
                await backoff_sleep(attempt)
                continue
            raise
    raise HTTPException(status_code=502, detail=f"Network error after retries: {last_error}")


async def get_robots_state(client: httpx.AsyncClient, url: str) -> RobotsState:
    hostname = get_hostname(url)
    existing = robots_cache.get(hostname)
    now = int(time.time())
    if existing and now - existing.fetched_at < ROBOTS_CACHE_TTL_SECONDS:
        return existing

    robots_url = urljoin(f"{urlparse(url).scheme}://{hostname}", "/robots.txt")
    parser = RobotFileParser()

    try:
        response = await client.get(robots_url, headers=DEFAULT_HEADERS)
        if response.status_code >= 400:
            state = RobotsState(parser=None, fetched_at=now)
            robots_cache[hostname] = state
            return state
        parser.set_url(robots_url)
        parser.parse(response.text.splitlines())
        state = RobotsState(parser=parser, fetched_at=now)
        robots_cache[hostname] = state
        return state
    except Exception:
        state = RobotsState(parser=None, fetched_at=now)
        robots_cache[hostname] = state
        return state


async def check_robots_allowed(client: httpx.AsyncClient, url: str, respect_robots: bool) -> bool:
    if not respect_robots:
        return True
    state = await get_robots_state(client, url)
    if state.parser is None:
        return True
    return bool(state.parser.can_fetch(USER_AGENT, url))


async def fetch_html(url: str, respect_robots: bool) -> tuple[str, str, str]:
    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True, http2=True) as client:
        allowed = await check_robots_allowed(client, url, respect_robots)
        if not allowed:
            raise HTTPException(status_code=403, detail="Blocked by robots.txt")

        response = await fetch_with_retries(client, url)
        content_type = response.headers.get("content-type", "")
        if "html" not in content_type and "xml" not in content_type:
            raise HTTPException(status_code=415, detail=f"Unsupported content type: {content_type}")

        raw_bytes = response.content
        if len(raw_bytes) > MAX_HTML_BYTES:
            raise HTTPException(status_code=413, detail="Page too large for lightweight mode")

        return response.text, str(response.url), content_type


# ----------------------------
# Extraction
# ----------------------------


def extract_with_trafilatura(html: str, url: str) -> str:
    extracted = trafilatura.extract(
        html,
        url=url,
        output_format="markdown",
        favor_precision=True,
        include_links=False,
        include_images=False,
        include_tables=True,
        fast=False,
    )
    return truncate_text(clean_whitespace(extracted or ""))



def extract_from_json_ld(tree: LexborHTMLParser) -> str:
    chunks: list[str] = []
    for script in tree.css("script[type='application/ld+json']"):
        raw = (script.text() or "").strip()
        if not raw:
            continue
        obj = safe_json_loads(raw)
        if obj is None:
            continue
        chunks.extend(walk_for_long_strings(obj))
    return truncate_text(clean_whitespace("\n\n".join(dict.fromkeys(chunks))))



def extract_from_next_data(tree: LexborHTMLParser) -> str:
    node = tree.css_first("script#__NEXT_DATA__")
    if not node:
        return ""
    obj = safe_json_loads((node.text() or "").strip())
    if obj is None:
        return ""
    chunks = walk_for_long_strings(obj)
    return truncate_text(clean_whitespace("\n\n".join(dict.fromkeys(chunks))))



def extract_from_common_hydration_blobs(tree: LexborHTMLParser) -> str:
    patterns = (
        "__APOLLO_STATE__",
        "__INITIAL_STATE__",
        "__NUXT__",
        "__PRELOADED_STATE__",
        "window.__DATA__",
    )
    chunks: list[str] = []
    for script in tree.css("script"):
        raw = (script.text() or "").strip()
        if len(raw) < 500:
            continue
        if not any(pattern in raw for pattern in patterns):
            continue
        matches = re.findall(r"({.*})", raw, flags=re.DOTALL)
        for match in matches[:2]:
            obj = safe_json_loads(match)
            if obj is None:
                continue
            chunks.extend(walk_for_long_strings(obj))
    return truncate_text(clean_whitespace("\n\n".join(dict.fromkeys(chunks))))


async def render_with_playwright(url: str, respect_robots: bool) -> str:
    if not ENABLE_BROWSER_FALLBACK:
        return ""

    # Lazy import to keep the service usable without Playwright runtime.
    try:
        from playwright.async_api import async_playwright
    except Exception:
        return ""

    network_texts: list[str] = []

    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True, http2=True) as client:
        allowed = await check_robots_allowed(client, url, respect_robots)
        if not allowed:
            return ""

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
        html = await page.content()
        await context.close()
        await browser.close()

    extracted_html = extract_with_trafilatura(html, url)
    if extracted_html:
        return extracted_html

    return truncate_text(clean_whitespace("\n\n".join(network_texts[:3])))


async def read_text(url: str, browser_fallback: bool, respect_robots: bool) -> ReadResponse:
    normalized = normalize_url(url)
    enforce_domain_policy(normalized)

    html, final_url, _content_type = await fetch_html(normalized, respect_robots=respect_robots)
    tree = LexborHTMLParser(html)
    title = extract_title(tree)

    raw_text = extract_with_trafilatura(html, final_url)
    raw_score, raw_stats = score_text(raw_text, title)
    if raw_score >= 0.56:
        return ReadResponse(
            url=normalized,
            final_url=final_url,
            title=title,
            text_markdown=raw_text,
            method="raw_html",
            confidence=raw_score,
            from_cache=False,
            extracted_at=int(time.time()),
            hints={"stage": 1, "stats": raw_stats},
        )

    # Small static pages (landing pages, error pages, simple docs) score low purely
    # because they are short. Re-rendering or hydration scraping cannot produce
    # content that is not there — accept stage 1 if the page is small and the
    # extracted text overlaps the title.
    if raw_text and len(html) < SMALL_STATIC_HTML_BYTES and title:
        title_tokens = [t.lower() for t in re.findall(r"\w+", title) if len(t) > 3]
        if title_tokens:
            title_overlap = sum(1 for tok in title_tokens if tok in raw_text.lower()) / len(title_tokens)
            if title_overlap >= 0.5:
                return ReadResponse(
                    url=normalized,
                    final_url=final_url,
                    title=title,
                    text_markdown=raw_text,
                    method="raw_html",
                    confidence=max(raw_score, 0.6),
                    from_cache=False,
                    extracted_at=int(time.time()),
                    hints={"stage": 1, "small_static_page": True, "stats": raw_stats},
                )

    jsonld_text = extract_from_json_ld(tree)
    next_text = extract_from_next_data(tree)
    hydration_text = extract_from_common_hydration_blobs(tree)

    combined = truncate_text(
        clean_whitespace(
            "\n\n".join(part for part in [raw_text, jsonld_text, next_text, hydration_text] if part)
        )
    )
    combined_score, combined_stats = score_text(combined, title)
    if combined_score >= 0.45:
        return ReadResponse(
            url=normalized,
            final_url=final_url,
            title=title,
            text_markdown=combined,
            method="embedded_data",
            confidence=combined_score,
            from_cache=False,
            extracted_at=int(time.time()),
            hints={
                "stage": 2,
                "jsonld": bool(jsonld_text),
                "next_data": bool(next_text),
                "hydration_blob": bool(hydration_text),
                "stats": combined_stats,
            },
        )

    if browser_fallback:
        rendered = await render_with_playwright(final_url, respect_robots=respect_robots)
        rendered_score, rendered_stats = score_text(rendered, title)
        if rendered:
            return ReadResponse(
                url=normalized,
                final_url=final_url,
                title=title,
                text_markdown=rendered,
                method="browser_fallback",
                confidence=rendered_score,
                from_cache=False,
                extracted_at=int(time.time()),
                hints={"stage": 3, "stats": rendered_stats},
            )

    best_text = combined or raw_text
    best_score, best_stats = score_text(best_text, title)
    return ReadResponse(
        url=normalized,
        final_url=final_url,
        title=title,
        text_markdown=best_text,
        method="best_effort",
        confidence=best_score,
        from_cache=False,
        extracted_at=int(time.time()),
        hints={"stage": "fallback", "stats": best_stats},
    )


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok", "service": APP_NAME, "version": APP_VERSION}


@app.post("/admin/purge-cache")
async def purge_cache() -> dict[str, int]:
    removed = cache_store.purge_expired()
    return {"removed": removed}


@app.get("/read", response_model=ReadResponse)
async def read_endpoint(
    response: Response,
    url: str = Query(..., description="Public URL to read"),
    browser_fallback: bool = Query(True, description="Use Playwright fallback"),
    respect_robots: bool = Query(True, description="Check robots.txt before fetching"),
    x_cache_bypass: Optional[str] = Header(default=None),
) -> ReadResponse:
    normalized = normalize_url(url)
    cache_key = url_cache_key(normalized, browser_fallback=browser_fallback)

    bypass_cache = (x_cache_bypass or "").lower() in {"1", "true", "yes"}
    if not bypass_cache:
        cached = cache_store.get(cache_key)
        if cached:
            response.headers["X-GhostReader-Cache"] = "hit"
            response.headers["X-GhostReader-Method"] = cached.method
            response.headers["X-GhostReader-Confidence"] = str(cached.confidence)
            return cached

    result = await read_text(
        normalized,
        browser_fallback=browser_fallback,
        respect_robots=respect_robots,
    )
    cache_store.set(cache_key, result, ttl_seconds=CACHE_TTL_SECONDS)

    response.headers["X-GhostReader-Cache"] = "miss"
    response.headers["X-GhostReader-Method"] = result.method
    response.headers["X-GhostReader-Confidence"] = str(result.confidence)
    return result

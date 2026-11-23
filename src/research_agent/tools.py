"""Search, fetch, and scrape tools with caching and a credit counter (PRD §6, §0.5).

Composio is tried first for Tavily/Firecrawl, falling back to the direct API
on any error (per PRD §0: "If the Composio route is messy, call Tavily's API
directly"). Confirmed empirically (2026-11-23) that the real tool slugs are
TAVILY_SEARCH and FIRECRAWL_SCRAPE, not the slugs guessed by third-party docs
summaries during planning.

Credit counts are tracked locally in output/credits.json, not read back from
Tavily's /usage endpoint: a direct test call showed /usage still reporting
search_usage=0 several seconds after a real search, so it has reporting lag
and can't be used as a live per-call source of truth.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import trafilatura
from composio import Composio

logger = logging.getLogger(__name__)

CACHE_DIR = Path("data/cache")
CREDITS_PATH = Path("output/credits.json")

TAVILY_SEARCH_SLUG = "TAVILY_SEARCH"
FIRECRAWL_SCRAPE_SLUG = "FIRECRAWL_SCRAPE"
COMPOSIO_USER_ID = "research_agent"


class CreditLimitReached(Exception):
    pass


def _cache_key(*parts: str) -> str:
    return hashlib.sha256("||".join(parts).encode("utf-8")).hexdigest()


def _cache_path(key: str) -> Path:
    return CACHE_DIR / f"{key}.json"


def _cache_get(key: str) -> dict | None:
    path = _cache_path(key)
    if path.exists():
        return json.loads(path.read_text())
    return None


def _cache_set(key: str, value: dict) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    _cache_path(key).write_text(json.dumps(value))


@dataclass(eq=False)
class CreditCounter:
    tavily_used: int = 0
    firecrawl_used: int = 0
    tavily_limit: int = 900
    firecrawl_limit: int = 900
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    @classmethod
    def load(cls, tavily_limit: int = 900, firecrawl_limit: int = 900) -> "CreditCounter":
        if CREDITS_PATH.exists():
            data = json.loads(CREDITS_PATH.read_text())
            return cls(
                tavily_used=data.get("tavily_used", 0),
                firecrawl_used=data.get("firecrawl_used", 0),
                tavily_limit=tavily_limit,
                firecrawl_limit=firecrawl_limit,
            )
        return cls(tavily_limit=tavily_limit, firecrawl_limit=firecrawl_limit)

    def save(self) -> None:
        CREDITS_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = CREDITS_PATH.with_suffix(".tmp")
        tmp.write_text(
            json.dumps({"tavily_used": self.tavily_used, "firecrawl_used": self.firecrawl_used}, indent=2)
        )
        os.replace(tmp, CREDITS_PATH)

    def tavily_available(self) -> bool:
        return self.tavily_used < self.tavily_limit

    def firecrawl_available(self) -> bool:
        return self.firecrawl_used < self.firecrawl_limit

    def record_tavily(self, n: int = 1) -> None:
        with self._lock:
            if self.tavily_used >= self.tavily_limit:
                raise CreditLimitReached(f"Tavily hard stop: {self.tavily_used}/{self.tavily_limit}")
            self.tavily_used += n
            self.save()
            if self.tavily_used >= self.tavily_limit:
                raise CreditLimitReached(f"Tavily hard stop: {self.tavily_used}/{self.tavily_limit}")

    def record_firecrawl(self, n: int = 1) -> None:
        with self._lock:
            if self.firecrawl_used >= self.firecrawl_limit:
                raise CreditLimitReached(f"Firecrawl hard stop: {self.firecrawl_used}/{self.firecrawl_limit}")
            self.firecrawl_used += n
            self.save()
            if self.firecrawl_used >= self.firecrawl_limit:
                raise CreditLimitReached(f"Firecrawl hard stop: {self.firecrawl_used}/{self.firecrawl_limit}")


def tavily_search(
    query: str,
    *,
    composio_client: Composio | None,
    tavily_api_key: str,
    credits: CreditCounter,
    use_cache: bool = True,
    search_depth: str = "basic",
    max_results: int = 5,
) -> dict[str, Any]:
    key = _cache_key("tavily_search", query, search_depth, str(max_results))
    if use_cache:
        cached = _cache_get(key)
        if cached is not None:
            return cached

    if not credits.tavily_available():
        raise CreditLimitReached(f"Tavily hard stop: {credits.tavily_used}/{credits.tavily_limit}")

    data: dict[str, Any] | None = None
    via = None
    if composio_client is not None:
        try:
            result = composio_client.tools.execute(
                TAVILY_SEARCH_SLUG,
                user_id=COMPOSIO_USER_ID,
                arguments={"query": query, "search_depth": search_depth, "max_results": max_results},
                dangerously_skip_version_check=True,
            )
            if result.get("successful"):
                data = result["data"]
                via = "composio"
            else:
                logger.warning("Composio TAVILY_SEARCH failed: %s", result.get("error"))
        except Exception:
            logger.exception("Composio TAVILY_SEARCH raised, falling back to direct Tavily API")

    if data is None:
        from tavily import TavilyClient

        client = TavilyClient(api_key=tavily_api_key)
        data = client.search(query=query, search_depth=search_depth, max_results=max_results)
        via = "direct"

    credits.record_tavily(1)
    data["_via"] = via
    if use_cache:
        _cache_set(key, data)
    return data


def firecrawl_scrape(
    url: str,
    *,
    composio_client: Composio | None,
    firecrawl_api_key: str | None,
    credits: CreditCounter,
    use_cache: bool = True,
) -> dict[str, Any]:
    key = _cache_key("firecrawl_scrape", url)
    if use_cache:
        cached = _cache_get(key)
        if cached is not None:
            return cached

    if not credits.firecrawl_available():
        raise CreditLimitReached(f"Firecrawl hard stop: {credits.firecrawl_used}/{credits.firecrawl_limit}")

    data: dict[str, Any] | None = None
    via = None
    if composio_client is not None:
        try:
            result = composio_client.tools.execute(
                FIRECRAWL_SCRAPE_SLUG,
                user_id=COMPOSIO_USER_ID,
                arguments={"url": url, "formats": ["markdown"]},
                dangerously_skip_version_check=True,
            )
            if result.get("successful"):
                data = result["data"]
                via = "composio"
            else:
                logger.warning("Composio FIRECRAWL_SCRAPE failed: %s", result.get("error"))
        except Exception:
            logger.exception("Composio FIRECRAWL_SCRAPE raised, falling back to direct Firecrawl API")

    if data is None and firecrawl_api_key:
        from firecrawl import Firecrawl

        fc = Firecrawl(api_key=firecrawl_api_key)
        data = fc.scrape(url, formats=["markdown"])
        via = "direct"

    if data is None:
        raise RuntimeError(f"Firecrawl scrape failed for {url} via both Composio and direct API")

    credits.record_firecrawl(1)
    data["_via"] = via
    if use_cache:
        _cache_set(key, data)
    return data


def fetch_page_free(url: str, *, use_cache: bool = True, timeout: float = 15.0) -> dict[str, Any]:
    key = _cache_key("fetch_free", url)
    if use_cache:
        cached = _cache_get(key)
        if cached is not None:
            return cached

    text = ""
    status = None
    try:
        resp = httpx.get(
            url,
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (research-agent)"},
        )
        status = resp.status_code
        if resp.status_code == 200:
            extracted = trafilatura.extract(resp.text, url=url)
            text = extracted or ""
    except httpx.HTTPError as e:
        logger.warning("httpx fetch failed for %s: %s", url, e)

    result = {
        "url": url,
        "status": status,
        "text": text,
        "method": "httpx+trafilatura",
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    if use_cache:
        _cache_set(key, result)
    return result


def fetch_page_playwright(url: str, *, use_cache: bool = True, timeout_ms: int = 20000) -> dict[str, Any]:
    key = _cache_key("fetch_playwright", url)
    if use_cache:
        cached = _cache_get(key)
        if cached is not None:
            return cached

    from playwright.sync_api import sync_playwright

    text = ""
    status = None
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            response = page.goto(url, timeout=timeout_ms, wait_until="networkidle")
            status = response.status if response else None
            html = page.content()
            browser.close()
        extracted = trafilatura.extract(html, url=url)
        text = extracted or ""
    except Exception as e:
        logger.warning("Playwright fetch failed for %s: %s", url, e)

    result = {
        "url": url,
        "status": status,
        "text": text,
        "method": "playwright",
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    if use_cache:
        _cache_set(key, result)
    return result


def fetch_page(url: str, *, use_cache: bool = True, min_text_len: int = 500) -> dict[str, Any]:
    """httpx+trafilatura first; Playwright only if the free fetch comes back thin."""
    result = fetch_page_free(url, use_cache=use_cache)
    if len(result.get("text", "")) >= min_text_len:
        return result
    pw_result = fetch_page_playwright(url, use_cache=use_cache)
    if len(pw_result.get("text", "")) > len(result.get("text", "")):
        return pw_result
    return result

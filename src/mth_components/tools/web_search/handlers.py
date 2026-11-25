"""Keyless web search: SearXNG (if configured) then DuckDuckGo fallbacks."""

from __future__ import annotations

import html as html_mod
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

MAX_RESULTS = 8
TIMEOUT = 15
SEARXNG_ENV = "SEARXNG_URL"
_UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64)"}
_ENDPOINTS = (
    (
        "https://html.duckduckgo.com/html/?{}",
        re.compile(
            r'<a[^>]*class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.DOTALL
        ),
    ),
    (
        "https://lite.duckduckgo.com/lite/?{}",
        re.compile(
            r'<a[^>]*class="result-link"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.DOTALL
        ),
    ),
)
_BOT_SIGNALS = ("captcha", "unusual traffic", "anomaly")


def _is_bot_challenge(page: str) -> bool:
    lower = page.lower()
    return any(s in lower for s in _BOT_SIGNALS) or (
        "blocked" in lower and "requests" in lower
    )


def _decode_href(href: str) -> str:
    href = html_mod.unescape(href)
    if "uddg=" in href:
        query = href.split("?", 1)[1] if "?" in href else ""
        for pair in query.split("&"):
            if pair.startswith("uddg="):
                return urllib.parse.unquote(pair[len("uddg=") :])
    return "https:" + href if href.startswith("//") else href


def _parse_results(page: str, pattern: re.Pattern[str]) -> list[str]:
    results: list[str] = []
    for match in pattern.finditer(page):
        href = _decode_href(match.group(1))
        title = html_mod.unescape(re.sub(r"<.*?>", "", match.group(2))).strip()
        if title and href:
            results.append(f"{title}: {href}")
        if len(results) >= MAX_RESULTS:
            break
    return results


def _search_searxng(base_url: str, query: str) -> list[str] | None:
    params = urllib.parse.urlencode({"q": query, "format": "json"})
    req = urllib.request.Request(f"{base_url}/search?{params}", headers=_UA)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as response:
            body = response.read().decode("utf-8")
    except (urllib.error.URLError, OSError):
        return None
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return None
    results: list[str] = []
    for item in data.get("results", []):
        title = str(item.get("title", "")).strip()
        href = str(item.get("url", "")).strip()
        if title and href:
            results.append(f"{title}: {href}")
        if len(results) >= MAX_RESULTS:
            break
    return results


def handle_web_search(args: dict[str, Any]) -> str:
    query = args["query"]
    base_url = (os.environ.get(SEARXNG_ENV) or "").strip().rstrip("/") or None
    if base_url:
        results = _search_searxng(base_url, query)
        if results:
            return "\n".join(results)

    encoded = urllib.parse.urlencode({"q": query})
    bot_blocked = False
    network_errors: list[str] = []
    for template, pattern in _ENDPOINTS:
        req = urllib.request.Request(template.format(encoded), headers=_UA)
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as response:
                page = response.read().decode("utf-8", errors="replace")
        except (urllib.error.URLError, OSError) as error:
            network_errors.append(str(error))
            continue
        if _is_bot_challenge(page):
            bot_blocked = True
            continue
        results = _parse_results(page, pattern)
        if results:
            return "\n".join(results)
    if network_errors and not bot_blocked:
        return (
            f"Error: web search failed: {network_errors[-1]}. "
            "Check your network connection and retry the query."
        )
    if bot_blocked:
        return (
            "Error: web search failed: DuckDuckGo is blocking automated "
            "requests (bot detection). Try again later or use a different query."
        )
    return "No web results found. Try rephrasing the query."

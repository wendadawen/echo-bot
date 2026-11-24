"""轻量可插拔联网直搜适配器（SearchProvider，主聊与后台沙箱共用单一实现）。

支持多级自动接力与独立冷却：
- 配置 Tavily / Exa / 自定义 API 时优先使用主接口；
- 主接口额度耗尽或限流（402/429/432 或 403 quota/limit/credit）时进入 30 分钟冷却，
  随后按「Bing CN → 360 搜索 → Bing 国际」顺序自动接力兜底；
- 任一免费兜底源遇到验证码或 403/429 时立即进入 10 分钟冷却并无缝切换下一个，绝不死等或强绕。
"""

from __future__ import annotations

import html
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional

if __package__:
    from ..domain import SearchHit
else:
    class SearchHit:  # pragma: no cover - 沙箱独立导入时使用
        __slots__ = ("title", "snippet", "url")

        def __init__(self, title: str, snippet: str, url: str = "") -> None:
            self.title = str(title)
            self.snippet = str(snippet)
            self.url = str(url)

PRIMARY_QUOTA_COOLDOWN = 1800.0
FALLBACK_COOLDOWN = 600.0
QUOTA_HTTP_CODES = frozenset({402, 429, 432})
_QUOTA_BODY_KEYWORDS = ("quota", "limit", "credit", "usage", "rate", "exceeded", "insufficient")
_CAPTCHA_MARKERS = (
    "captcha",
    "verify you are human",
    "cf-challenge",
    "验证码",
    "安全验证",
    "antispider",
    "wappass",
)
_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


def _strip_html(raw: str) -> str:
    no_tags = re.sub(r"<[^>]+>", " ", str(raw or ""))
    return re.sub(r"\s+", " ", html.unescape(no_tags)).strip()


def _has_captcha(page_html: str) -> bool:
    low = str(page_html or "").lower()
    return any(marker in low for marker in _CAPTCHA_MARKERS)


def _http_get_html(url: str, timeout: int) -> tuple[str, bool]:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": _USER_AGENT,
            "Accept-Language": "zh-CN,zh;q=0.9",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=max(3, int(timeout or 10))) as resp:
            body = resp.read(300_000).decode("utf-8", errors="ignore")
    except urllib.error.HTTPError as exc:
        return "", exc.code in (402, 403, 429, 432)
    except Exception:
        return "", False
    if _has_captcha(body):
        return "", True
    return body, False


def _parse_bing_algo_blocks(page_html: str, limit: int = 5) -> list[SearchHit]:
    hits: list[SearchHit] = []
    blocks = re.findall(r'<li[^>]+class="[^"]*\bb_algo\b[^"]*"[^>]*>(.*?)</li>', str(page_html or ""), re.S)
    for block in blocks:
        m = re.search(r"<h2[^>]*>.*?<a[^>]+href=\"([^\"]+)\"[^>]*>(.*?)</a>", block, re.S)
        if not m:
            continue
        href = html.unescape(m.group(1)).strip()
        title = _strip_html(m.group(2))[:100]
        p = re.search(r"<p[^>]*>(.*?)</p>", block, re.S)
        snippet = _strip_html(p.group(1))[:280] if p else ""
        if not snippet:
            cap = re.search(r'<div[^>]+class="[^"]*b_caption[^"]*"[^>]*>(.*?)</div>', block, re.S)
            if cap:
                snippet = _strip_html(cap.group(1))[:280]
        if title or snippet:
            hits.append(SearchHit(title=title or href, snippet=snippet, url=href))
            if len(hits) >= limit:
                break
    return hits


def _parse_so360_blocks(page_html: str, limit: int = 5) -> list[SearchHit]:
    hits: list[SearchHit] = []
    blocks = re.findall(r'<li[^>]+class="[^"]*\bres-list\b[^"]*"[^>]*>(.*?)</li>', str(page_html or ""), re.S)
    for block in blocks:
        m = re.search(
            r'<h3[^>]*>.*?<a[^>]+(?:data-mdurl|href)="([^"]+)"[^>]*>(.*?)</a>',
            block,
            re.S,
        )
        if not m:
            continue
        href = html.unescape(m.group(1)).strip()
        if not href.startswith(("http://", "https://")):
            alt = re.search(r'href="([^"]+)"', block, re.S)
            if alt:
                href = html.unescape(alt.group(1)).strip()
        title = _strip_html(m.group(2))[:100]
        p = re.search(r'<(?:p|div)[^>]+class="[^"]*(?:res-desc|g-card-desc)[^"]*"[^>]*>(.*?)</(?:p|div)>', block, re.S)
        if p:
            snippet = _strip_html(p.group(1))[:280]
        else:
            snippet = _strip_html(block)[len(title):len(title) + 280].strip()
        if title or snippet:
            hits.append(SearchHit(title=title or href, snippet=snippet, url=href))
            if len(hits) >= limit:
                break
    return hits


def _fetch_bing(query: str, host: str, limit: int = 5, timeout: int = 10) -> tuple[list[SearchHit], bool]:
    q = str(query or "").strip()
    if not q:
        return [], False
    top_n = max(1, min(8, int(limit or 5)))
    suffix = "&setlang=zh-CN" if host == "cn.bing.com" else ""
    url = "https://{}/search?q={}{}".format(host, urllib.parse.quote(q), suffix)
    page, blocked = _http_get_html(url, timeout=timeout)
    if blocked or not page:
        return [], blocked
    return _parse_bing_algo_blocks(page, limit=top_n), False


def _fetch_so360(query: str, limit: int = 5, timeout: int = 10) -> tuple[list[SearchHit], bool]:
    q = str(query or "").strip()
    if not q:
        return [], False
    top_n = max(1, min(8, int(limit or 5)))
    url = "https://www.so.com/s?q={}".format(urllib.parse.quote(q))
    page, blocked = _http_get_html(url, timeout=timeout)
    if blocked or not page:
        return [], blocked
    return _parse_so360_blocks(page, limit=top_n), False


def search_bing_cn(query: str, limit: int = 5, timeout: int = 10) -> list[SearchHit]:
    hits, _ = _fetch_bing(query, "cn.bing.com", limit=limit, timeout=timeout)
    return hits


def search_bing_global(query: str, limit: int = 5, timeout: int = 10) -> list[SearchHit]:
    hits, _ = _fetch_bing(query, "www.bing.com", limit=limit, timeout=timeout)
    return hits


def search_so360(query: str, limit: int = 5, timeout: int = 10) -> list[SearchHit]:
    hits, _ = _fetch_so360(query, limit=limit, timeout=timeout)
    return hits


class HttpSearchProvider:
    def __init__(
        self,
        provider: str = "auto",
        api_base: str = "",
        api_key: str = "",
        timeout: int = 12,
    ):
        self.provider = str(provider or "auto").strip().lower()
        self.api_base = str(api_base or "").strip().rstrip("/")
        self.api_key = str(api_key or "").strip()
        self.timeout = max(3, int(timeout or 12))
        self._cooldown_until: dict[str, float] = {}

    @classmethod
    def from_config(cls, config: Optional[dict[str, Any]] = None) -> "HttpSearchProvider":
        cfg = config or {}
        provider = str(cfg.get("search_provider") or os.environ.get("BOT_SEARCH_PROVIDER", "auto")).strip()
        base = str(cfg.get("search_api_base") or os.environ.get("BOT_SEARCH_API_BASE", "")).strip()
        key = str(cfg.get("search_api_key") or os.environ.get("BOT_SEARCH_API_KEY", "")).strip()
        return cls(provider=provider, api_base=base, api_key=key)

    def available(self) -> bool:
        if self.provider in ("free", "bing", "so360"):
            return True
        if self.api_base:
            return self.api_base.startswith(("http://", "https://"))
        if self.api_key and self.provider in ("tavily", "exa"):
            return True
        return False

    def in_cooldown(self, channel: str, now: Optional[float] = None) -> bool:
        cur = time.monotonic() if now is None else float(now)
        return cur < self._cooldown_until.get(channel, 0.0)

    def in_primary_cooldown(self, now: Optional[float] = None) -> bool:
        return self.in_cooldown("primary", now=now)

    def _has_primary(self) -> bool:
        if self.provider in ("free", "bing", "so360"):
            return False
        if self.api_base:
            return self.api_base.startswith(("http://", "https://"))
        return bool(self.api_key and self.provider in ("tavily", "exa"))

    def _fallback_order(self) -> tuple[str, ...]:
        if self.provider == "so360":
            return ("so360", "bing_cn", "bing_global")
        return ("bing_cn", "so360", "bing_global")

    def _resolve_endpoint(self) -> tuple[str, str]:
        if self.provider in ("free", "bing"):
            return "bing", "https://cn.bing.com/search"
        if self.provider == "so360":
            return "so360", "https://www.so.com/s"
        if self.provider == "exa" or ("exa" in self.api_base):
            url = self.api_base or "https://api.exa.ai/search"
            return "exa", url if url.endswith("/search") else url + "/search"
        if self.provider == "tavily" or ("tavily" in self.api_base) or (self.api_key and not self.api_base):
            url = self.api_base or "https://api.tavily.com/search"
            return "tavily", url if url.endswith("/search") else url + "/search"
        return "generic", self.api_base

    def _search_primary(self, q: str, top_n: int) -> list[SearchHit]:
        kind, url = self._resolve_endpoint()
        headers = {"Content-Type": "application/json"}
        if kind == "exa":
            if self.api_key:
                headers["x-api-key"] = self.api_key
            payload: dict[str, Any] = {"query": q, "numResults": top_n, "contents": {"text": {"maxCharacters": 300}}}
        elif kind == "tavily":
            payload = {"query": q, "max_results": top_n, "search_depth": "basic"}
            if self.api_key:
                payload["api_key"] = self.api_key
        else:
            if self.api_key:
                headers["Authorization"] = "Bearer " + self.api_key
            payload = {"query": q, "limit": top_n}

        req = urllib.request.Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            data = json.loads(resp.read(1024 * 1024).decode("utf-8"))
        rows = data.get("results") or data.get("items") or []
        hits: list[SearchHit] = []
        for row in rows[:top_n]:
            if not isinstance(row, dict):
                continue
            title = str(row.get("title") or "").strip()[:100]
            snippet = str(row.get("content") or row.get("snippet") or row.get("text") or "").strip()[:280]
            href = str(row.get("url") or row.get("link") or "").strip()
            if title or snippet:
                hits.append(SearchHit(title=title or href, snippet=snippet, url=href))
        return hits

    @staticmethod
    def _is_quota_error(exc: urllib.error.HTTPError) -> bool:
        if exc.code in QUOTA_HTTP_CODES:
            return True
        if exc.code == 403:
            try:
                body = exc.read(1024).decode("utf-8", errors="ignore").lower()
            except Exception:
                body = ""
            return any(word in body for word in _QUOTA_BODY_KEYWORDS)
        return False

    def search(self, query: str, limit: int = 5) -> list[SearchHit]:
        q = str(query or "").strip()
        if not q or not self.available():
            return []
        top_n = max(1, min(8, int(limit or 5)))

        if self._has_primary() and not self.in_primary_cooldown():
            try:
                hits = self._search_primary(q, top_n)
                if hits:
                    return hits
            except urllib.error.HTTPError as exc:
                if self._is_quota_error(exc):
                    self._cooldown_until["primary"] = time.monotonic() + PRIMARY_QUOTA_COOLDOWN
            except Exception:
                pass

        for channel in self._fallback_order():
            if self.in_cooldown(channel):
                continue
            if channel == "bing_cn":
                hits, blocked = _fetch_bing(q, "cn.bing.com", limit=top_n, timeout=self.timeout)
            elif channel == "so360":
                hits, blocked = _fetch_so360(q, limit=top_n, timeout=self.timeout)
            else:
                hits, blocked = _fetch_bing(q, "www.bing.com", limit=top_n, timeout=self.timeout)
            if hits:
                return hits
            if blocked:
                self._cooldown_until[channel] = time.monotonic() + FALLBACK_COOLDOWN
        return []

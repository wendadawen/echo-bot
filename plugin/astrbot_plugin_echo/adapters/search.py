"""轻量可插拔联网直搜适配器（SearchProvider）：支持 Tavily / Exa / Bing CN / 自定义 HTTP。

支持额度自动熔断降级：
- 配置 Tavily / Exa 时优先使用主接口；
- 若主接口返回 402/429/432 或配额耗尽（403 quota/limit/credit），自动进入 30 分钟冷却，
  并在当次及冷却期内无感切换至免费免 Key 的 Bing CN 网页检索兜底（遇反爬 CAPTCHA 自动收手返回空）。
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

from ..domain import SearchHit

PRIMARY_QUOTA_COOLDOWN = 1800.0
QUOTA_HTTP_CODES = frozenset({402, 429, 432})
_QUOTA_BODY_KEYWORDS = ("quota", "limit", "credit", "usage", "rate", "exceeded", "insufficient")
_CAPTCHA_MARKERS = ("captcha", "verify you are human", "cf-challenge")
_BING_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


def _strip_html(raw: str) -> str:
    no_tags = re.sub(r"<[^>]+>", " ", str(raw or ""))
    return re.sub(r"\s+", " ", html.unescape(no_tags)).strip()


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


def search_bing_cn(query: str, limit: int = 5, timeout: int = 10) -> list[SearchHit]:
    """免费免 Key 的 Bing CN 网页兜底检索（遇到验证码等反爬信号立即收手返回空）。"""
    q = str(query or "").strip()
    if not q:
        return []
    top_n = max(1, min(8, int(limit or 5)))
    url = "https://cn.bing.com/search?q={}&setlang=zh-CN".format(urllib.parse.quote(q))
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": _BING_USER_AGENT,
            "Accept-Language": "zh-CN,zh;q=0.9",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=max(3, int(timeout or 10))) as resp:
            page = resp.read(300_000).decode("utf-8", errors="ignore")
    except Exception:
        return []
    low = page.lower()
    if any(marker in low for marker in _CAPTCHA_MARKERS):
        return []
    return _parse_bing_algo_blocks(page, limit=top_n)


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
        self._primary_cooldown_until = 0.0

    @classmethod
    def from_config(cls, config: Optional[dict[str, Any]] = None) -> "HttpSearchProvider":
        cfg = config or {}
        provider = str(cfg.get("search_provider") or os.environ.get("BOT_SEARCH_PROVIDER", "auto")).strip()
        base = str(cfg.get("search_api_base") or os.environ.get("BOT_SEARCH_API_BASE", "")).strip()
        key = str(cfg.get("search_api_key") or os.environ.get("BOT_SEARCH_API_KEY", "")).strip()
        return cls(provider=provider, api_base=base, api_key=key)

    def available(self) -> bool:
        if self.provider == "bing":
            return True
        if self.api_base:
            return self.api_base.startswith(("http://", "https://"))
        if self.api_key and self.provider in ("tavily", "exa"):
            return True
        return False

    def in_primary_cooldown(self, now: Optional[float] = None) -> bool:
        cur = time.monotonic() if now is None else float(now)
        return cur < self._primary_cooldown_until

    def _resolve_endpoint(self) -> tuple[str, str]:
        if self.provider == "bing":
            return "bing", "https://cn.bing.com/search"
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
        if self.provider == "bing" or self.in_primary_cooldown():
            return search_bing_cn(q, limit=top_n, timeout=self.timeout)
        try:
            hits = self._search_primary(q, top_n)
            if hits:
                return hits
            return search_bing_cn(q, limit=top_n, timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            if self._is_quota_error(exc):
                self._primary_cooldown_until = time.monotonic() + PRIMARY_QUOTA_COOLDOWN
            return search_bing_cn(q, limit=top_n, timeout=self.timeout)
        except Exception:
            return search_bing_cn(q, limit=top_n, timeout=self.timeout)

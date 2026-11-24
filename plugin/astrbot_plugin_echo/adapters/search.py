"""轻量可插拔联网直搜适配器（SearchProvider）：支持 Tavily / Exa / 自定义 HTTP JSON 搜索。

未配置 search_api_key / search_api_base 时 `available()` 返回 False，
核心引擎会自动隐藏 web_search 工具，复杂检索自然交由后台 Codex 处理。
"""

from __future__ import annotations

import json
import os
import urllib.request
from typing import Any, Optional

from ..domain import SearchHit


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

    @classmethod
    def from_config(cls, config: Optional[dict[str, Any]] = None) -> "HttpSearchProvider":
        cfg = config or {}
        provider = str(cfg.get("search_provider") or os.environ.get("BOT_SEARCH_PROVIDER", "auto")).strip()
        base = str(cfg.get("search_api_base") or os.environ.get("BOT_SEARCH_API_BASE", "")).strip()
        key = str(cfg.get("search_api_key") or os.environ.get("BOT_SEARCH_API_KEY", "")).strip()
        return cls(provider=provider, api_base=base, api_key=key)

    def available(self) -> bool:
        if self.api_base:
            return self.api_base.startswith(("http://", "https://"))
        if self.api_key and self.provider in ("tavily", "exa"):
            return True
        return False

    def _resolve_endpoint(self) -> tuple[str, str]:
        if self.provider == "exa" or ("exa" in self.api_base):
            url = self.api_base or "https://api.exa.ai/search"
            return "exa", url if url.endswith("/search") else url + "/search"
        if self.provider == "tavily" or ("tavily" in self.api_base) or (self.api_key and not self.api_base):
            url = self.api_base or "https://api.tavily.com/search"
            return "tavily", url if url.endswith("/search") else url + "/search"
        return "generic", self.api_base

    def search(self, query: str, limit: int = 5) -> list[SearchHit]:
        q = str(query or "").strip()
        if not q or not self.available():
            return []
        kind, url = self._resolve_endpoint()
        top_n = max(1, min(8, int(limit or 5)))
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

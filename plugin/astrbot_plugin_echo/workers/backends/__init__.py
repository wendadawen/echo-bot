"""后台执行后端包：按模型自动在 Codex CLI（GPT/OpenAI）与通用 ChatAgentBackend（全模型）间路由。"""

from __future__ import annotations

import os
from typing import Any

from .chat_agent import ChatAgentBackend
from .codex import CodexBackend

_CODEX_MODEL_PREFIXES = ("gpt-", "o1", "o3", "o4", "codex")


def uses_codex_model(model: str) -> bool:
    """GPT / OpenAI o 系列模型走原生 Codex CLI（/v1/responses），其余模型自动走通用后端。"""
    low = str(model or "").strip().lower()
    if not low or "claude" in low:
        return False
    return low.startswith(_CODEX_MODEL_PREFIXES)


def select_backend(
    *,
    api_base: str = "",
    api_key: str = "",
    model: str = "",
    codex_binary: str = "/usr/local/bin/codex",
    search_provider: Any = None,
) -> CodexBackend | ChatAgentBackend:
    """根据当前配置模型选择最合适的隔离后台执行后端。"""
    if uses_codex_model(model) and os.path.exists(codex_binary):
        return CodexBackend(
            binary=codex_binary,
            api_base=api_base,
            api_key=api_key,
            model=model,
        )
    return ChatAgentBackend(
        api_base=api_base,
        api_key=api_key,
        model=model,
        search_provider=str(getattr(search_provider, "provider", "") or ""),
        search_api_base=str(getattr(search_provider, "api_base", "") or ""),
        search_api_key=str(getattr(search_provider, "api_key", "") or ""),
    )


__all__ = [
    "ChatAgentBackend",
    "CodexBackend",
    "select_backend",
    "uses_codex_model",
]

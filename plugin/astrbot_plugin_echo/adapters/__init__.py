"""外部适配层：大模型客户端、可插拔联网搜索、AstrBot+NapCat 协议防腐层。"""

from .astrbot import AstrBotInboundParser, AstrBotSender, MessageLimiter, detect_mentions, is_retold, is_sticker_image
from .llm import (
    DEFAULT_API_BASE,
    DEFAULT_MODEL_TIMEOUT,
    MockLLMClient,
    OpenAICompatClient,
    build_llm_client,
    resolve_model_config,
    strip_thinking,
)
from .search import HttpSearchProvider

__all__ = [
    "AstrBotInboundParser",
    "AstrBotSender",
    "DEFAULT_API_BASE",
    "DEFAULT_MODEL_TIMEOUT",
    "HttpSearchProvider",
    "MessageLimiter",
    "MockLLMClient",
    "OpenAICompatClient",
    "build_llm_client",
    "detect_mentions",
    "is_retold",
    "is_sticker_image",
    "resolve_model_config",
    "strip_thinking",
]

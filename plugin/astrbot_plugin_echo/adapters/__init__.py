"""外部适配层：大模型客户端、可插拔联网搜索、AstrBot+NapCat 协议防腐层。"""

from .astrbot import AstrBotInboundParser
from .llm import (
    DEFAULT_API_BASE,
    DEFAULT_MODEL_TIMEOUT,
    MockLLMClient,
    OpenAICompatClient,
    build_llm_client,
    resolve_model_config,
    strip_thinking,
)
from .qq_protocol import detect_mentions, is_public_http_url, is_question, is_retold, is_sticker_image
from .search import HttpSearchProvider
from .sender import AstrBotSender, MessageLimiter

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
    "is_public_http_url",
    "is_question",
    "is_retold",
    "is_sticker_image",
    "resolve_model_config",
    "strip_thinking",
]

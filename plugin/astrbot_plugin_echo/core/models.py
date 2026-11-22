"""可插拔的模型层：默认本地模拟，配置后接任意 OpenAI 兼容接口。

插件通过 asyncio.to_thread 调用本模块，同步网络请求不会阻塞事件循环。
"""

import json
import os
import urllib.request

from .messages import format_speaker_message

DEFAULT_API_BASE = "https://api.openai.com/v1"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024


def _read_bounded(resp, limit=MAX_RESPONSE_BYTES):
    """兼容有限字节读取和无参 mock.read()，防止超大响应打爆内存。"""
    try:
        raw = resp.read(limit + 1)
    except TypeError:
        raw = resp.read()
    if len(raw) > limit:
        raise RuntimeError("模型响应体积超过上限")
    return raw


def _extract_message_text(message):
    """兼容字符串 content、多模态文本块列表和 reasoning_content 回落。"""
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                text = block.get("text")
                if text:
                    parts.append(str(text))
            elif isinstance(block, str) and block:
                parts.append(block)
        content = "".join(parts)
    if not isinstance(content, str):
        content = message.get("reasoning_content") or message.get("text") or ""
    return content.strip() if isinstance(content, str) else str(content).strip()


class BaseModel:
    name = "base"

    def reply(self, system, history, user_text):
        raise NotImplementedError


class MockModel(BaseModel):
    """零配置回复，用于在没有 API 凭据时验证整条链路。"""

    name = "mock"

    def reply(self, system, history, user_text):
        text = user_text.strip()
        if any(k in text for k in ("你好", "在吗", "哈喽", "早", "hi")):
            return "在呢，怎么啦？"
        if "?" in text or "？" in text or any(
            k in text for k in ("怎么", "为什么", "是不是", "能不能", "如何")
        ):
            return "这个我得想想……你先说说前因后果？"
        if any(k in text for k in ("哈哈", "笑死", "绝了", "牛", "离谱")):
            return "哈哈哈确实。"
        return "嗯嗯，我在听。你们继续聊，有需要喊我一声。"


class OpenAIModel(BaseModel):
    """OpenAI 兼容的 chat/completions 接口，也可指向本机 Ollama。"""

    name = "openai"

    def __init__(self, api_key, api_base, model, timeout=40):
        self.api_key = str(api_key or "").strip()
        base = str(api_base or DEFAULT_API_BASE).strip()
        if not base.startswith(("http://", "https://")):
            raise ValueError("api_base 必须以 http:// 或 https:// 开头")
        clean_base = base.rstrip("/")
        if clean_base.endswith("/chat/completions"):
            self.url = clean_base
        else:
            self.url = clean_base + "/chat/completions"
        self.model = str(model or "gpt-4o-mini").strip()
        self.timeout = timeout

    def _headers(self):
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = "Bearer " + self.api_key
        return headers

    def reply(self, system, history, user_text):
        messages = [{"role": "system", "content": system}]
        for item in history:
            item_role = item.get("role")
            role = "assistant" if item_role == "bot" else "user"
            content = item["text"]
            if item_role != "system" and role == "user" and item.get("user_name"):
                content = format_speaker_message(item["user_name"], content)
            messages.append({"role": role, "content": content})
        messages.append({"role": "user", "content": user_text})
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": 0.8,
            "max_tokens": 1024,
        }
        req = urllib.request.Request(
            self.url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=self._headers(),
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            data = json.loads(_read_bounded(resp).decode("utf-8"))
        choices = data.get("choices") or []
        if not choices:
            raise RuntimeError("模型未返回候选结果")
        text = _extract_message_text(choices[0].get("message") or {})
        if not text:
            raise RuntimeError("模型返回空内容")
        return text

    def vision_reply(self, text, image_url, timeout=None):
        """多模态调用：图片用 data URI 或可下载的 URL，模型仍取配置里的 self.model。"""
        return self.vision_reply_json([
            {"type": "text", "text": text},
            {"type": "image_url", "image_url": {"url": image_url}},
        ], timeout=timeout)

    def vision_reply_json(self, content, timeout=None):
        """通用多模态入口：content 为 OpenAI 格式的内容块列表。"""
        messages = [{"role": "user", "content": content}]
        payload = {"model": self.model, "messages": messages, "max_tokens": 300}
        req = urllib.request.Request(
            self.url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=self._headers(),
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
            data = json.loads(_read_bounded(resp).decode("utf-8"))
        choices = data.get("choices") or []
        if not choices:
            raise RuntimeError("模型未返回候选结果")
        return _extract_message_text(choices[0].get("message") or {})


def build_model(config=None):
    """优先读插件面板配置，其次读环境变量；支持无 key 本地端点（Ollama）；全空回落模拟模型。"""
    config = config or {}
    key = str(config.get("api_key") or os.environ.get("BOT_API_KEY", "")).strip()
    custom_base = str(config.get("api_base") or os.environ.get("BOT_API_BASE", "")).strip()
    if not key and not custom_base:
        return MockModel()
    base = custom_base or DEFAULT_API_BASE
    if not base.startswith(("http://", "https://")):
        return MockModel()
    model = str(config.get("model") or os.environ.get("BOT_MODEL", "gpt-4o-mini")).strip()
    return OpenAIModel(key, base, model)

"""模型适配层：统一 OpenAI 兼容客户端（原生 tools + 多模态直传 + 自动兼容降级 + 思考块清洗）。

无论底层模型是 gpt-6-luna、Claude、Gemini、DeepSeek、Qwen 还是本地 Ollama：
- 上层核心只调用 `LLMClient.chat(messages, tools)` 拿到 `ModelTurn(text, tool_calls)`；
- 支持原生 `tools` 与 `image_url` 同轮直传，看图+回答一步完成；
- 遇到不支持 `tools` 或多模态的老/小模型时，自动无感降级到结构化 JSON 提示或图文转述，核心逻辑零改动。
"""

from __future__ import annotations

import base64
import json
import os
import re
import urllib.error
import urllib.request
import uuid
from typing import Any, Optional, Sequence

from ..domain import LLMMessage, ModelTurn, ToolCall, ToolSpec, detect_image_format

DEFAULT_API_BASE = "https://api.openai.com/v1"
DEFAULT_MODEL_TIMEOUT = 90
MAX_RESPONSE_BYTES = 2 * 1024 * 1024

_THINK_TAG_RE = re.compile(r"^\s*(?:<(?:think|thinking|analysis)>.*?</(?:think|thinking|analysis)>\s*)+", re.I | re.S)
_THINK_HEADER_RE = re.compile(
    r"^\s*(?:#{1,6}\s*)?(?:\*\*)?(?:thinking\s+process|thought\s+process|思考过程|思维链)(?:\*\*)?\s*[:：].*?\n\s*\n+",
    re.I | re.S,
)


def strip_thinking(text: Any) -> str:
    """剥离推理模型（DeepSeek/Gemini/Qwen3）意外混入正文的思考块，不破坏正常文本。"""
    s = str(text or "").strip()
    if not s:
        return ""
    stripped = _THINK_TAG_RE.sub("", s).strip()
    if stripped:
        s = stripped
    header_stripped = _THINK_HEADER_RE.sub("", s).strip()
    if header_stripped:
        s = header_stripped
    return s


def _read_bounded(resp: Any, limit: int = MAX_RESPONSE_BYTES) -> bytes:
    try:
        raw = resp.read(limit + 1)
    except TypeError:
        raw = resp.read()
    if len(raw) > limit:
        raise RuntimeError("模型响应体积超过上限")
    return raw


def _extract_message_text(message: dict[str, Any]) -> str:
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                txt = block.get("text")
                if txt:
                    parts.append(str(txt))
            elif isinstance(block, str) and block:
                parts.append(block)
        content = "".join(parts)
    if isinstance(content, str) and content.strip():
        return strip_thinking(content)
    # 仅在既无 content 也无 tool_calls 时才回落 reasoning_content，防止吞掉真实结论。
    if not message.get("tool_calls"):
        reasoning = message.get("reasoning_content")
        if isinstance(reasoning, str) and reasoning.strip():
            return strip_thinking(reasoning)
    return ""


def _data_uri(image_bytes: bytes) -> str:
    mime, _ = detect_image_format(image_bytes)
    return "data:{};base64,".format(mime) + base64.b64encode(image_bytes).decode("ascii")


def _render_tools_fallback_prompt(tools: Sequence[ToolSpec]) -> str:
    """当上游模型不支持 OpenAI tools 参数时，自动从同一份 ToolSpec 渲染兼容指令。"""
    lines = [
        "如需调用工具，请仅输出一行 JSON：{\"tool\":\"工具名\",\"args\":{...}}；"
        "如果要直接回复群友，请直接输出回复内容（或 {\"tool\":\"direct_reply\",\"args\":{\"text\":\"...\"}}）。",
        "可用工具：",
    ]
    for t in tools:
        props = (t.parameters or {}).get("properties") or {}
        arg_list = ", ".join(props.keys()) if isinstance(props, dict) else ""
        lines.append("- {}({}): {}".format(t.name, arg_list, t.description))
    return "\n".join(lines)


def _parse_json_fallback_turn(raw_text: str, valid_names: set[str]) -> Optional[ModelTurn]:
    cleaned = str(raw_text or "").strip()
    if not cleaned:
        return None
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.I)
        cleaned = re.sub(r"\s*```\s*$", "", cleaned).strip()
    dec = json.JSONDecoder()
    idx = 0
    while idx < len(cleaned):
        pos = cleaned.find("{", idx)
        if pos < 0:
            break
        try:
            obj, end = dec.raw_decode(cleaned, pos)
        except (ValueError, TypeError):
            idx = pos + 1
            continue
        if isinstance(obj, dict):
            tname = str(obj.get("tool") or obj.get("action") or "").strip()
            if tname == "direct_reply":
                args = obj.get("args") if isinstance(obj.get("args"), dict) else {}
                return ModelTurn(text=str(args.get("text") or obj.get("text") or "").strip())
            if tname in valid_names:
                args = obj.get("args")
                if not isinstance(args, dict):
                    args = {k: v for k, v in obj.items() if k not in ("tool", "action")}
                return ModelTurn(
                    tool_calls=(ToolCall(id="fallback_" + uuid.uuid4().hex[:8], name=tname, arguments=args),),
                    finish_reason="tool_calls",
                )
        idx = max(pos + 1, end)
    return None


class MockLLMClient:
    """离线测试与零配置试用的确定性模拟模型。"""

    name = "mock"

    def chat(
        self,
        messages: Sequence[LLMMessage],
        tools: Sequence[ToolSpec] = (),
        timeout: Optional[int] = None,
    ) -> ModelTurn:
        last_user = ""
        for m in reversed(messages):
            if m.role == "user":
                last_user = str(m.content or "")
                break
        text = re.sub(r"^\[昵称：[^\]]*\]\s*", "", last_user).strip()
        if any(k in text for k in ("在吗", "你好", "哈喽", "在不在")):
            return ModelTurn(text="在呢，随时说。")
        if "?" in text or "？" in text or any(k in text for k in ("怎么", "为什么", "是不是", "能不能", "如何")):
            return ModelTurn(text="这个我得想想……你先说说前因后果？")
        if any(k in text for k in ("哈哈", "笑死", "绝了", "牛", "离谱")):
            return ModelTurn(text="哈哈哈确实。")
        return ModelTurn(text="嗯嗯，我在听。你们继续聊，有需要喊我一声。")

    def describe_image(self, image_bytes: bytes, prompt: str, timeout: Optional[int] = None) -> str:
        return "无语|一张聊天图片"

    def extra_secrets(self) -> tuple[str, ...]:
        return ()


class OpenAICompatClient:
    """OpenAI 兼容 `/chat/completions` 客户端，支持原生 tools、多模态及无感降级。"""

    name = "openai"

    def __init__(
        self,
        api_key: str,
        api_base: str,
        model: str,
        timeout: int = DEFAULT_MODEL_TIMEOUT,
    ):
        self.api_key = str(api_key or "").strip()
        base = str(api_base or DEFAULT_API_BASE).strip()
        if not base.startswith(("http://", "https://")):
            raise ValueError("api_base 必须以 http:// 或 https:// 开头")
        clean_base = base.rstrip("/")
        self.url = clean_base if clean_base.endswith("/chat/completions") else clean_base + "/chat/completions"
        self.model = str(model or "gpt-4o-mini").strip()
        self.timeout = int(timeout or DEFAULT_MODEL_TIMEOUT)
        self._tools_supported: Optional[bool] = None
        self._inline_vision_supported: Optional[bool] = None

    def extra_secrets(self) -> tuple[str, ...]:
        return (self.api_key,) if len(self.api_key) >= 8 else ()

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = "Bearer " + self.api_key
        return headers

    def _post(self, payload: dict[str, Any], timeout: Optional[int] = None) -> dict[str, Any]:
        req = urllib.request.Request(
            self.url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=self._headers(),
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=int(timeout or self.timeout)) as resp:
            return json.loads(_read_bounded(resp).decode("utf-8"))

    def _encode_messages(
        self,
        messages: Sequence[LLMMessage],
        allow_images: bool = True,
        extra_system: str = "",
    ) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        injected_extra = False
        for m in messages:
            if m.role == "system":
                content = str(m.content or "")
                if extra_system and not injected_extra:
                    content = (content + "\n\n" + extra_system).strip()
                    injected_extra = True
                out.append({"role": "system", "content": content})
            elif m.role == "assistant":
                entry: dict[str, Any] = {"role": "assistant", "content": m.content or ""}
                if m.tool_calls:
                    entry["tool_calls"] = [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.name,
                                "arguments": json.dumps(tc.arguments, ensure_ascii=False),
                            },
                        }
                        for tc in m.tool_calls
                    ]
                out.append(entry)
            elif m.role == "tool":
                out.append({
                    "role": "tool",
                    "tool_call_id": m.tool_call_id,
                    "content": str(m.content or ""),
                })
            else:
                if allow_images and m.images:
                    blocks: list[dict[str, Any]] = [{"type": "text", "text": str(m.content or "（附图片）")}]
                    for img in m.images:
                        uri = img.url if img.url else (_data_uri(img.data) if img.data else "")
                        if uri:
                            blocks.append({"type": "image_url", "image_url": {"url": uri}})
                    out.append({"role": "user", "content": blocks})
                else:
                    txt = str(m.content or "")
                    if m.images and not allow_images:
                        notes = []
                        for idx, img in enumerate(m.images[:2], 1):
                            if img.data:
                                try:
                                    desc = self.describe_image(img.data, "简要客观描述图片内容")
                                except Exception:
                                    desc = ""
                                if desc:
                                    notes.append("[图片{}: {}]".format(idx, desc[:140]))
                        if notes:
                            txt = (txt + "\n" + "\n".join(notes)).strip()
                    out.append({"role": "user", "content": txt})
        if extra_system and not injected_extra:
            out.insert(0, {"role": "system", "content": extra_system})
        return out

    @staticmethod
    def _encode_tools(tools: Sequence[ToolSpec]) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.parameters,
                },
            }
            for t in tools
        ]

    @staticmethod
    def _parse_response(data: dict[str, Any]) -> ModelTurn:
        choices = data.get("choices") or []
        if not choices:
            raise RuntimeError("模型未返回候选结果")
        choice = choices[0] or {}
        msg = choice.get("message") or {}
        raw_tcs = msg.get("tool_calls") or []
        parsed_tcs: list[ToolCall] = []
        for idx, tc in enumerate(raw_tcs):
            if not isinstance(tc, dict):
                continue
            fn = tc.get("function") or {}
            fname = str(fn.get("name") or "").strip()
            if not fname:
                continue
            raw_args = fn.get("arguments")
            if isinstance(raw_args, dict):
                args = raw_args
            else:
                try:
                    loaded = json.loads(str(raw_args or "{}"))
                    args = loaded if isinstance(loaded, dict) else {"value": loaded}
                except (ValueError, TypeError):
                    args = {"_raw": str(raw_args or "")}
            parsed_tcs.append(
                ToolCall(id=str(tc.get("id") or "call_{}".format(idx)), name=fname, arguments=args)
            )
        text = _extract_message_text(msg)
        if not text and not parsed_tcs:
            raise RuntimeError("模型返回空内容")
        usage = data.get("usage") or {}
        tokens = int(usage.get("total_tokens") or 0) if isinstance(usage, dict) else 0
        return ModelTurn(
            text=text,
            tool_calls=tuple(parsed_tcs),
            finish_reason=str(choice.get("finish_reason") or "stop"),
            usage_tokens=tokens,
        )

    def chat(
        self,
        messages: Sequence[LLMMessage],
        tools: Sequence[ToolSpec] = (),
        timeout: Optional[int] = None,
    ) -> ModelTurn:
        use_native_tools = bool(tools) and (self._tools_supported is not False)
        allow_images = self._inline_vision_supported is not False
        extra_sys = _render_tools_fallback_prompt(tools) if (tools and not use_native_tools) else ""
        encoded_msgs = self._encode_messages(messages, allow_images=allow_images, extra_system=extra_sys)
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": encoded_msgs,
        }
        if use_native_tools:
            payload["tools"] = self._encode_tools(tools)
            payload["tool_choice"] = "auto"

        try:
            data = self._post(payload, timeout=timeout)
            turn = self._parse_response(data)
            if use_native_tools:
                self._tools_supported = True
            if not use_native_tools and tools and not turn.tool_calls and turn.text:
                parsed = _parse_json_fallback_turn(turn.text, {t.name for t in tools})
                if parsed is not None:
                    return parsed
            return turn
        except urllib.error.HTTPError as exc:
            raw_body = ""
            try:
                raw_body = exc.read().decode("utf-8", errors="ignore").strip()
            except Exception:
                pass
            body = raw_body.lower()
            if exc.code in (400, 422):
                retried = False
                if use_native_tools and any(k in body for k in ("tool", "function", "tool_choice")):
                    self._tools_supported = False
                    retried = True
                if allow_images and any(k in body for k in ("image", "vision", "multimodal", "content")):
                    self._inline_vision_supported = False
                    retried = True
                if retried:
                    return self.chat(messages, tools=tools, timeout=timeout)
            raise RuntimeError("HTTP {}: {}".format(exc.code, raw_body[:240] or exc.reason)) from exc

    def describe_image(self, image_bytes: bytes, prompt: str, timeout: Optional[int] = None) -> str:
        if not image_bytes:
            return ""
        payload = {
            "model": self.model,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": str(prompt or "简要描述这张图")},
                    {"type": "image_url", "image_url": {"url": _data_uri(image_bytes)}},
                ],
            }],
        }
        data = self._post(payload, timeout=timeout)
        choices = data.get("choices") or []
        if not choices:
            raise RuntimeError("模型未返回候选结果")
        return _extract_message_text(choices[0].get("message") or {})


def resolve_model_config(config: Optional[dict[str, Any]] = None) -> tuple[str, str, str]:
    """按 面板非默认配置 > 环境变量 > 默认值 解析 model 配置。"""
    cfg = config or {}
    key = str(cfg.get("api_key") or os.environ.get("BOT_API_KEY", "")).strip()
    cfg_base = str(cfg.get("api_base") or "").strip()
    env_base = os.environ.get("BOT_API_BASE", "").strip()
    resolved_base = cfg_base if (cfg_base and cfg_base.rstrip("/") != DEFAULT_API_BASE) else (env_base or cfg_base)

    cfg_model = str(cfg.get("model") or "").strip()
    env_model = os.environ.get("BOT_MODEL", "").strip()
    resolved_model = cfg_model if (cfg_model and cfg_model != "gpt-4o-mini") else (env_model or cfg_model or "gpt-4o-mini")
    return key, resolved_base, resolved_model


def build_llm_client(config: Optional[dict[str, Any]] = None) -> OpenAICompatClient | MockLLMClient:
    key, resolved_base, model = resolve_model_config(config)
    if not key and (not resolved_base or resolved_base.rstrip("/") == DEFAULT_API_BASE):
        return MockLLMClient()
    base = resolved_base or DEFAULT_API_BASE
    if not base.startswith(("http://", "https://")):
        return MockLLMClient()
    return OpenAICompatClient(key, base, model)

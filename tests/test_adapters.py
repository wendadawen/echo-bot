"""adapters 层测试：LLM 原生 tools+vision 与降级、SearchProvider、NapCat 入站解析与出站牢笼。"""

import asyncio
import io
import json
import os
import socket
import sys
import tempfile
import time
import types
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = ROOT / "plugin"
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from astrbot_plugin_echo.adapters.astrbot import (  # noqa: E402
    AstrBotInboundParser,
    AstrBotSender,
    MessageLimiter,
    detect_mentions,
    is_public_http_url,
    is_retold,
)
from astrbot_plugin_echo.adapters.llm import (  # noqa: E402
    DEFAULT_MODEL_TIMEOUT,
    MockLLMClient,
    OpenAICompatClient,
    strip_thinking,
)
from astrbot_plugin_echo.adapters.search import HttpSearchProvider, search_bing_cn  # noqa: E402
from astrbot_plugin_echo.domain import (  # noqa: E402
    ImagePart,
    LLMMessage,
    ReactEmoji,
    SendEmoji,
    SendFile,
    SendText,
    ToolCall,
    ToolSpec,
)
from astrbot_plugin_echo.store.repository import Repository  # noqa: E402

TINY_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


class _Plain:
    def __init__(self, text=""):
        self.text = text


class _At:
    def __init__(self, qq=""):
        self.qq = qq


class _Image:
    def __init__(self, url="", file="", path="", base64="", subType=None, summary=""):
        self.url = url
        self.file = file
        self.path = path
        self.base64 = base64
        self.subType = subType
        self.summary = summary


class _Face:
    def __init__(self, id="", name=""):
        self.id = id
        self.name = name


class _File:
    def __init__(self, name="", file=""):
        self.name = name
        self.file = file


class _Reply:
    def __init__(self, id="", sender_id="", sender_nickname="", text="", chain=()):
        self.id = id
        self.sender_id = sender_id
        self.sender_nickname = sender_nickname
        self.text = text
        self.message_str = text
        self.chain = list(chain)


class _Forward:
    def __init__(self, content=()):
        self.content = list(content)


class _Json:
    type = "json"


class _Poke:
    def __init__(self, id="0", qq="0"):
        self.id = id
        self.qq = qq
        self.type = "poke"


COMP_TYPES = {
    "Plain": _Plain,
    "At": _At,
    "Image": _Image,
    "Face": _Face,
    "File": _File,
    "Reply": _Reply,
}


def _fake_urlopen_response(obj: dict):
    raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    resp = MagicMock()
    resp.read.return_value = raw
    resp.__enter__.return_value = resp
    resp.__exit__.return_value = False
    return resp


class LLMClientTests(unittest.TestCase):
    def test_timeout_and_strip_thinking(self):
        self.assertEqual(DEFAULT_MODEL_TIMEOUT, 90)
        self.assertEqual(strip_thinking("<think>推理过程</think>\n真正回答"), "真正回答")
        self.assertEqual(
            strip_thinking("**Thinking Process:**\n1. step\n\n最终答复在这里"),
            "最终答复在这里",
        )
        self.assertEqual(strip_thinking("正常内容没有思考块"), "正常内容没有思考块")

    def test_native_tools_and_inline_image(self):
        client = OpenAICompatClient("sk-test-12345678", "http://127.0.0.1:8787/v1", "gpt-6-luna")
        self.assertEqual(client.extra_secrets(), ("sk-test-12345678",))

        seen_payloads = []

        def fake_urlopen(req, timeout=90):
            self.assertEqual(timeout, 90)
            seen_payloads.append(json.loads(req.data.decode("utf-8")))
            return _fake_urlopen_response({
                "choices": [{
                    "finish_reason": "tool_calls",
                    "message": {
                        "content": "",
                        "tool_calls": [{
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "reply_text",
                                "arguments": '{"text":"颜色是大红"}',
                            },
                        }],
                    },
                }],
                "usage": {"total_tokens": 55},
            })

        tools = (
            ToolSpec(
                name="reply_text",
                description="回复文本",
                parameters={"type": "object", "properties": {"text": {"type": "string"}}},
            ),
        )
        msgs = (
            LLMMessage(role="system", content="你是大肥鱼"),
            LLMMessage(role="user", content="这图啥颜色", images=(ImagePart(data=TINY_PNG),)),
        )
        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            turn = client.chat(msgs, tools=tools)

        self.assertEqual(len(turn.tool_calls), 1)
        self.assertEqual(turn.tool_calls[0].name, "reply_text")
        self.assertEqual(turn.tool_calls[0].arguments, {"text": "颜色是大红"})
        self.assertEqual(turn.usage_tokens, 55)
        self.assertIn("tools", seen_payloads[0])
        self.assertNotIn("temperature", seen_payloads[0])
        self.assertNotIn("max_tokens", seen_payloads[0])
        user_blocks = seen_payloads[0]["messages"][1]["content"]
        self.assertEqual(user_blocks[1]["type"], "image_url")
        self.assertTrue(user_blocks[1]["image_url"]["url"].startswith("data:image/png;base64,"))

        desc_payloads = []

        def fake_desc_urlopen(req, timeout=90):
            desc_payloads.append(json.loads(req.data.decode("utf-8")))
            return _fake_urlopen_response({"choices": [{"message": {"content": "一只红猫"}}]})

        with patch("urllib.request.urlopen", side_effect=fake_desc_urlopen):
            desc = client.describe_image(TINY_PNG, "描述图")
        self.assertEqual(desc, "一只红猫")
        self.assertNotIn("temperature", desc_payloads[0])
        self.assertNotIn("max_tokens", desc_payloads[0])

        def fake_err_urlopen(req, timeout=90):
            raise urllib.error.HTTPError(
                req.full_url, 400, "Bad Request", {}, io.BytesIO(b"model overloaded upstream error")
            )

        with patch("urllib.request.urlopen", side_effect=fake_err_urlopen):
            with self.assertRaises(RuntimeError) as ctx_err:
                client.chat((LLMMessage(role="user", content="hi"),), tools=())
        self.assertIn("HTTP 400", str(ctx_err.exception))
        self.assertIn("model overloaded upstream error", str(ctx_err.exception))

    def test_auto_fallback_when_tools_and_vision_unsupported(self):
        client = OpenAICompatClient("sk-12345678", "https://example.com/v1", "small-model")
        calls = []

        def fake_urlopen(req, timeout=90):
            payload = json.loads(req.data.decode("utf-8"))
            calls.append(payload)
            if "tools" in payload:
                raise urllib.error.HTTPError(
                    req.full_url, 400, "Bad Request", {}, io.BytesIO(b"model does not support tools")
                )
            if isinstance(payload["messages"][-1]["content"], list):
                raise urllib.error.HTTPError(
                    req.full_url, 422, "Unprocessable", {}, io.BytesIO(b"multimodal image_url unsupported")
                )
            return _fake_urlopen_response({
                "choices": [{"message": {"content": '{"tool":"react_message","args":{"emoji_id":"124"}}'}}]
            })

        tools = (
            ToolSpec(name="react_message", description="贴表情", parameters={"type": "object", "properties": {}}),
        )
        msgs = (
            LLMMessage(role="system", content="系统提示"),
            LLMMessage(role="user", content="看图", images=(ImagePart(data=TINY_PNG),)),
        )
        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            turn = client.chat(msgs, tools=tools)

        self.assertEqual(len(calls), 3)
        self.assertIn("[附带了图片，当前模型暂不支持直接看图]", calls[2]["messages"][-1]["content"])
        self.assertFalse(client._tools_supported)
        self.assertFalse(client._inline_vision_supported)
        self.assertEqual(len(turn.tool_calls), 1)
        self.assertEqual(turn.tool_calls[0].name, "react_message")
        self.assertEqual(turn.tool_calls[0].arguments, {"emoji_id": "124"})

    def test_reasoning_content_fallback_only_when_empty(self):
        data = {
            "choices": [{
                "message": {"content": "", "reasoning_content": "直接思考得出答案：42"},
                "finish_reason": "stop",
            }]
        }
        with self.assertRaises(RuntimeError):
            OpenAICompatClient._parse_response(data)

        mock_turn = MockLLMClient().chat((LLMMessage(role="user", content="在吗"),))
        self.assertIn("在呢", mock_turn.text)

    def test_wave1_thinking_strip_vision_non_poisoning_and_tool_flattening(self):
        # 1) 思考链清洗边界：中间块 / 孤立闭合 / 未闭合 / 多段头 / 纯思考归零
        self.assertEqual(strip_thinking("前缀<think>内部思考</think>正文"), "前缀正文")
        self.assertEqual(strip_thinking("残留思考</think>真实结论"), "真实结论")
        self.assertEqual(strip_thinking("正文<thinking>截断未闭合"), "正文")
        self.assertEqual(strip_thinking("<think>只有思考</think>"), "")
        self.assertEqual(
            strip_thinking("Thinking Process:\n\n1. 分析第一步\n\n2. 分析第二步\n\n最终回复"),
            "最终回复",
        )

        # 2) Anthropic / 推理块类型过滤
        turn = OpenAICompatClient._parse_response({
            "choices": [{
                "message": {
                    "content": [
                        {"type": "thinking", "text": "内部推理"},
                        {"type": "redacted_thinking", "text": "加密思考"},
                        {"type": "refusal", "text": "refusal"},
                        {"type": "text", "text": "最终答复"},
                    ]
                }
            }]
        })
        self.assertEqual(turn.text, "最终答复")

        # 3) 无图请求的 "invalid content" 400 不误伤 _inline_vision_supported；冷却到期自动恢复
        client = OpenAICompatClient("sk-12345678", "https://example.com/v1", "test-model")
        def fake_400(req, timeout=90):
            raise urllib.error.HTTPError(
                req.full_url, 400, "Bad Request", {}, io.BytesIO(b"invalid content format")
            )
        with patch("urllib.request.urlopen", side_effect=fake_400):
            with self.assertRaises(RuntimeError):
                client.chat((LLMMessage(role="user", content="纯文本"),))
        self.assertIsNone(client._inline_vision_supported)

        client._tools_supported = False
        self.assertFalse(client._tools_supported)
        client._tools_disabled_until = time.monotonic() - 1.0
        self.assertIsNone(client._tools_supported)

        # 4) use_native_tools=False 时将历史 assistant(tool_calls) 与 role=tool 扁平化
        encoded = client._encode_messages(
            (
                LLMMessage(role="user", content="查天气"),
                LLMMessage(role="assistant", content="", tool_calls=(ToolCall("tc1", "web_search", {"query": "北京"}),)),
                LLMMessage(role="tool", name="web_search", tool_call_id="tc1", content="晴天 20度"),
            ),
            allow_images=False,
            use_native_tools=False,
        )
        self.assertFalse(any("tool_calls" in m for m in encoded))
        self.assertFalse(any(m.get("role") == "tool" for m in encoded))
        self.assertTrue(any("晴天 20度" in str(m.get("content", "")) for m in encoded))


class SearchProviderTests(unittest.TestCase):
    def test_disabled_without_config_and_tavily_search(self):
        with patch.dict(os.environ, {}, clear=True):
            none_sp = HttpSearchProvider.from_config({})
        self.assertFalse(none_sp.available())
        self.assertEqual(none_sp.search("astrbot"), [])

        sp = HttpSearchProvider(provider="tavily", api_key="tvly-123456")
        self.assertTrue(sp.available())
        with patch(
            "urllib.request.urlopen",
            return_value=_fake_urlopen_response({
                "results": [{"title": "文档", "content": "AstrBot 4.29", "url": "https://docs.example.com"}]
            }),
        ) as mocked:
            hits = sp.search("AstrBot", limit=3)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].title, "文档")
        req_sent = mocked.call_args[0][0]
        self.assertEqual(req_sent.full_url, "https://api.tavily.com/search")

    def test_quota_exhaustion_cooldown_and_bing_cn_fallback(self):
        bing_html = (
            '<html><body><ol id="b_results">'
            '<li class="b_algo"><h2><a href="https://weather.example.cn/sh">上海今日天气</a></h2>'
            '<div class="b_caption"><p>今天晴转多云，13℃。</p></div></li>'
            '</ol></body></html>'
        )
        captcha_html = "<html><body>Please verify you are human (CAPTCHA)</body></html>"

        sp = HttpSearchProvider(provider="tavily", api_key="tvly-123456")
        called_urls = []

        def fake_urlopen(req, timeout=12):
            called_urls.append(req.full_url)
            if "tavily.com" in req.full_url:
                raise urllib.error.HTTPError(
                    req.full_url, 429, "Too Many Requests", {}, io.BytesIO(b"quota exceeded")
                )
            resp = MagicMock()
            resp.read.return_value = bing_html.encode("utf-8")
            resp.__enter__.return_value = resp
            resp.__exit__.return_value = False
            return resp

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            hits1 = sp.search("上海天气", limit=3)
            self.assertTrue(sp.in_primary_cooldown())
            hits2 = sp.search("苏州天气", limit=3)

        self.assertEqual(len(hits1), 1)
        self.assertEqual(hits1[0].title, "上海今日天气")
        self.assertIn("13℃", hits1[0].snippet)
        self.assertEqual(len(hits2), 1)
        # 第一次调 Tavily(429) + Bing CN，第二次在冷却期内跳过 Tavily 直接走 Bing CN
        self.assertEqual(sum("tavily" in u for u in called_urls), 1)
        self.assertEqual(sum("cn.bing.com" in u for u in called_urls), 2)

        # 遇到验证码反爬页立即收手返回空
        def fake_captcha(req, timeout=10):
            resp = MagicMock()
            resp.read.return_value = captcha_html.encode("utf-8")
            resp.__enter__.return_value = resp
            resp.__exit__.return_value = False
            return resp

        with patch("urllib.request.urlopen", side_effect=fake_captcha):
            self.assertEqual(search_bing_cn("test"), [])

        # provider="bing" / "free" / "so360" 免 Key 直接可用
        for free_mode in ("bing", "free", "so360"):
            self.assertTrue(HttpSearchProvider(provider=free_mode).available())

    def test_multi_tier_cascade_and_standalone_search_import(self):
        import importlib.util

        so360_html = (
            '<html><body><ul>'
            '<li class="res-list"><h3><a href="https://redirect.example" data-mdurl="https://docs.astrbot.app">AstrBot 文档</a></h3>'
            '<p class="res-desc">多协议机器人开发文档</p></li>'
            '</ul></body></html>'
        )
        bing_global_html = (
            '<html><body><ol id="b_results">'
            '<li class="b_algo"><h2><a href="https://www.example.com/en">Global Hit</a></h2>'
            '<p>Fallback global result</p></li>'
            '</ol></body></html>'
        )
        captcha_html = "<html><body>安全验证 CAPTCHA</body></html>"

        sp = HttpSearchProvider(provider="tavily", api_key="tvly-123456")
        called = []

        def fake_urlopen(req, timeout=12):
            called.append(req.full_url)
            if "tavily.com" in req.full_url:
                raise urllib.error.HTTPError(req.full_url, 429, "Too Many Requests", {}, io.BytesIO(b"limit"))
            resp = MagicMock()
            if "cn.bing.com" in req.full_url:
                resp.read.return_value = captcha_html.encode("utf-8")
            elif "so.com" in req.full_url:
                resp.read.return_value = so360_html.encode("utf-8")
            else:
                resp.read.return_value = bing_global_html.encode("utf-8")
            resp.__enter__.return_value = resp
            resp.__exit__.return_value = False
            return resp

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            # Tavily 429 -> Bing CN CAPTCHA -> 360 命中
            h1 = sp.search("AstrBot 文档")
            self.assertEqual(h1[0].title, "AstrBot 文档")
            self.assertEqual(h1[0].url, "https://docs.astrbot.app")
            self.assertTrue(sp.in_primary_cooldown())
            self.assertTrue(sp.in_cooldown("bing_cn"))

            # 第二次搜索跳过冷却中的 Tavily 与 Bing CN，直接走 360
            h2 = sp.search("AstrBot 第二次")
            self.assertEqual(len(h2), 1)
            self.assertEqual(sum("tavily" in u for u in called), 1)
            self.assertEqual(sum("cn.bing.com" in u for u in called), 1)
            self.assertEqual(sum("so.com" in u for u in called), 2)

            # 360 也遇验证码时自动切到 Bing 国际
            called_g = []

            def fake_global(req, timeout=12):
                called_g.append(req.full_url)
                resp = MagicMock()
                if "www.bing.com" in req.full_url:
                    resp.read.return_value = bing_global_html.encode("utf-8")
                else:
                    resp.read.return_value = captcha_html.encode("utf-8")
                resp.__enter__.return_value = resp
                resp.__exit__.return_value = False
                return resp

            with patch("urllib.request.urlopen", side_effect=fake_global):
                sp_free = HttpSearchProvider(provider="free")
                h3 = sp_free.search("global test")
            self.assertEqual(h3[0].title, "Global Hit")
            self.assertTrue(sp_free.in_cooldown("bing_cn"))
            self.assertTrue(sp_free.in_cooldown("so360"))
            self.assertEqual(sum("www.bing.com" in u for u in called_g), 1)

        # 独立单文件导入 adapters/search.py（模拟沙箱子进程 import echo_search）
        search_file = PLUGIN_ROOT / "astrbot_plugin_echo" / "adapters" / "search.py"
        spec = importlib.util.spec_from_file_location("echo_search", search_file)
        standalone = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(standalone)
        self.assertTrue(standalone.HttpSearchProvider(provider="free").available())

    def test_wave1_auto_provider_hostname_captcha_and_error_cooldown(self):
        with patch.dict(os.environ, {}, clear=True):
            sp_auto = HttpSearchProvider.from_config({
                "search_provider": "auto",
                "search_api_key": "tvly-secret-123456",
            })
        self.assertTrue(sp_auto.available())
        self.assertIn("tvly-secret-123456", sp_auto.extra_secrets())

        sp_ex = HttpSearchProvider(
            provider="auto", api_base="https://example.com/search", api_key="k12345678"
        )
        self.assertEqual(sp_ex._resolve_endpoint(), ("generic", "https://example.com/search"))

        # 有效结果页中正巧包含 captcha 字样时不应触发错误冷却
        bing_with_word = (
            '<html><body><ol id="b_results">'
            '<li class="b_algo"><h2><a href="https://docs.example.com">How captcha works</a></h2>'
            '<div class="b_caption"><p>A captcha tutorial for web apps.</p></div></li>'
            '</ol></body></html>'
        )
        sp_bing = HttpSearchProvider(provider="bing")
        with patch("urllib.request.urlopen") as m_open:
            resp = MagicMock()
            resp.read.return_value = bing_with_word.encode("utf-8")
            resp.__enter__.return_value = resp
            resp.__exit__.return_value = False
            m_open.return_value = resp
            hits = sp_bing.search("captcha tutorial")
        self.assertEqual(len(hits), 1)
        self.assertFalse(sp_bing.in_cooldown("bing_cn"))

        # 主搜 500 错误应进入短冷却而不是继续死磕
        sp_500 = HttpSearchProvider(provider="tavily", api_key="tvly-123456")
        def fake_500(req, timeout=12):
            if "tavily.com" in req.full_url:
                raise urllib.error.HTTPError(req.full_url, 500, "Server Error", {}, io.BytesIO(b"boom"))
            resp = MagicMock()
            resp.read.return_value = b""
            resp.__enter__.return_value = resp
            resp.__exit__.return_value = False
            return resp
        with patch("urllib.request.urlopen", side_effect=fake_500):
            sp_500.search("test")
        self.assertTrue(sp_500.in_primary_cooldown())


class InboundAndSenderTests(unittest.TestCase):
    def _make_event(self, parts, raw_msg=None, text="", uid="2001", name="阿明", gid="999", self_id="8888"):
        ev = MagicMock()
        ev.get_group_id.return_value = gid
        ev.get_platform_name.return_value = "aiocqhttp"
        ev.unified_msg_origin = "aiocqhttp:GroupMessage:999"
        ev.get_sender_id.return_value = uid
        ev.get_sender_name.return_value = name
        ev.get_self_id.return_value = self_id
        ev.get_message_str.return_value = text
        ev.get_messages.return_value = parts
        ev.message_obj = types.SimpleNamespace(raw_message=raw_msg, message_id="m_fallback", message_str=text)
        ev.is_at_or_wake_command.return_value = False
        return ev

    def test_ssrf_and_retold_detection(self):
        self.assertFalse(is_public_http_url("http://127.0.0.1:8080/x"))
        self.assertFalse(is_public_http_url("file:///etc/passwd"))
        with patch("socket.getaddrinfo", return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]):
            self.assertTrue(is_public_http_url("https://dns.google/image.png"))

        self.assertTrue(is_retold("他们说你大肥鱼知道吗？"))
        self.assertFalse(is_retold("我说你看这 bug"))
        mentioned, self_called, retold, _ = detect_mentions("@大肥鱼 帮我看下", at_self=True)
        self.assertTrue(mentioned)
        self.assertFalse(retold)
        self.assertFalse(self_called)

    def test_raw_message_disambiguates_screenshot_vs_sticker_and_quote_image(self):
        parser = AstrBotInboundParser(COMP_TYPES, downloader=lambda url: TINY_PNG)

        # 1) AstrBot Image 无 subType，但 NapCat raw_message sub_type=0 -> 普通截图，严禁标为表情包
        ev_shot = self._make_event(
            parts=[_Image(url="https://example.com/shot.png", file="shot.png")],
            raw_msg={
                "message_id": 123456,
                "message": [{"type": "image", "data": {"file": "shot.png", "sub_type": "0", "summary": "[图片]"}}],
            },
        )
        parsed_shot = asyncio.run(parser.parse(ev_shot))
        self.assertEqual(parsed_shot.msg_id, "123456")
        self.assertTrue(parsed_shot.has_image)
        self.assertFalse(parsed_shot.has_sticker)
        self.assertFalse(parsed_shot.images[0].is_sticker)
        self.assertTrue(parsed_shot.bare_face)

        # 2) sub_type=1 或 summary 含表情 -> 识别为真表情包；但手机长截图 (1179x2556) 即使标成 sub_type=1 也绝不收
        ev_sticker = self._make_event(
            parts=[_Image(url="https://example.com/meme.png", file="meme.png")],
            raw_msg={
                "message_id": 123457,
                "message": [{"type": "image", "data": {"file": "meme.png", "sub_type": "1", "summary": "[动画表情]"}}],
            },
        )
        parsed_sticker = asyncio.run(parser.parse(ev_sticker))
        self.assertTrue(parsed_sticker.has_sticker)
        self.assertTrue(parsed_sticker.images[0].is_sticker)

        tall_png = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + (1179).to_bytes(4, "big") + (2556).to_bytes(4, "big") + b"\x08\x06\x00\x00\x00" + b"\x00" * 16
        tall_parser = AstrBotInboundParser(COMP_TYPES, downloader=lambda url: tall_png)
        parsed_tall = asyncio.run(tall_parser.parse(ev_sticker))
        self.assertTrue(parsed_tall.has_image)
        self.assertFalse(parsed_tall.has_sticker)
        self.assertFalse(parsed_tall.images[0].is_sticker)

        # 3) 引用图片且 @bot：下载供看图，但不标记 is_sticker（永不收入表情库）
        ev_quote_img = self._make_event(
            parts=[
                _Reply(id="100", sender_id="2002", sender_nickname="小和", chain=[_Image(url="https://example.com/q.png")]),
                _At(qq="8888"),
                _Plain("这是啥图"),
            ],
            raw_msg={"message_id": 123458, "message": []},
            text="这是啥图",
        )
        parsed_q = asyncio.run(parser.parse(ev_quote_img))
        self.assertTrue(parsed_q.mentioned)
        self.assertEqual(parsed_q.quote_sender, "小和")
        self.assertEqual(len(parsed_q.images), 1)
        self.assertFalse(parsed_q.images[0].is_sticker)

        # 4) 未 @ 带链接 / JSON 卡片 / 合并转发
        ev_url = self._make_event(parts=[_Plain("看看 https://example.com")], text="看看 https://example.com")
        self.assertTrue(asyncio.run(parser.parse(ev_url)).passthrough)

        ev_json = self._make_event(parts=[_Json()])
        self.assertTrue(asyncio.run(parser.parse(ev_json)).passthrough)

        ev_fwd = self._make_event(
            parts=[
                _At(qq="8888"),
                _Forward(
                    content=[
                        {"sender": {"nickname": "甲"}, "content": "明天开会"},
                        {
                            "sender": {"nickname": "乙"},
                            "content": [
                                {
                                    "type": "forward",
                                    "data": {
                                        "content": [
                                            {"sender": {"nickname": "丙"}, "content": [{"type": "text", "text": "内层详情"}]}
                                        ]
                                    },
                                }
                            ],
                        },
                    ]
                ),
            ]
        )
        parsed_fwd = asyncio.run(parser.parse(ev_fwd))
        self.assertIn("明天开会", parsed_fwd.text)
        self.assertIn("内层详情", parsed_fwd.text)
        self.assertNotIn("{'", parsed_fwd.text)

    def test_super_face_dice_rps_poke_and_quoted_mface(self):
        parser = AstrBotInboundParser(COMP_TYPES, downloader=lambda url: TINY_PNG)

        # 1) QQ 新版超级表情（475=干饭，489+faceText=/知识增加）与骰子/猜拳解析
        ev_super = self._make_event(
            parts=[_Face(id="475", name=""), _Face(id="489", name="QQ表情")],
            raw_msg={
                "message_id": 2001,
                "message": [
                    {"type": "face", "data": {"id": "475"}},
                    {"type": "face", "data": {"id": "489", "raw": {"faceText": "/知识增加"}}},
                    {"type": "dice", "data": {"result": "6"}},
                    {"type": "rps", "data": {"result": "3"}},
                ],
            },
        )
        parsed_super = asyncio.run(parser.parse(ev_super, last_bot_reply_at=time.time()))
        self.assertIn("[QQ表情:干饭]", parsed_super.text)
        self.assertIn("[QQ表情:知识增加]", parsed_super.text)
        self.assertIn("[骰子:6点]", parsed_super.text)
        self.assertIn("[猜拳:石头]", parsed_super.text)
        self.assertTrue(parsed_super.bare_face)

        # 2) Poke 戳一戳：戳机器人触发，群友互戳直接 passthrough
        ev_poke_bot = self._make_event(
            parts=[_Poke(id="8888")],
            raw_msg={"post_type": "notice", "notice_type": "notify", "sub_type": "poke", "target_id": 8888, "user_id": 2001},
        )
        parsed_poke = asyncio.run(parser.parse(ev_poke_bot))
        self.assertFalse(parsed_poke.passthrough)
        self.assertTrue(parsed_poke.mentioned)
        self.assertTrue(parsed_poke.is_poke)
        self.assertEqual(parsed_poke.msg_id, "")
        self.assertEqual(parsed_poke.text, "（戳了戳你）")

        ev_poke_other = self._make_event(
            parts=[_Poke(id="9999")],
            raw_msg={"post_type": "notice", "notice_type": "notify", "sub_type": "poke", "target_id": 9999, "user_id": 2001},
        )
        parsed_poke_other = asyncio.run(parser.parse(ev_poke_other))
        self.assertTrue(parsed_poke_other.passthrough)

        # 3) 引用商城大表情 mface 且 @大肥鱼：通过 get_msg 回查下载图片看图，且 is_sticker=False 不收入库
        bot = MagicMock()
        bot.call_action = AsyncMock(return_value={
            "message": [
                {"type": "mface", "data": {"summary": "[猫猫比心]", "url": "https://cdn.example.com/mface.png"}}
            ]
        })
        ev_qm = self._make_event(
            parts=[
                _Reply(id="777", sender_id="2002", sender_nickname="小和", text="", chain=[]),
                _At(qq="8888"),
                _Plain("这表情啥意思"),
            ],
            raw_msg={"message_id": 2002, "message": []},
            text="这表情啥意思",
        )
        ev_qm.bot = bot
        parsed_qm = asyncio.run(parser.parse(ev_qm))
        bot.call_action.assert_awaited_once_with("get_msg", message_id=777)
        self.assertEqual(len(parsed_qm.images), 1)
        self.assertFalse(parsed_qm.images[0].is_sticker)
        self.assertIn("[商城表情:猫猫比心]", parsed_qm.quote_text)
        self.assertNotIn("回复 小和", parsed_qm.quote_text)

    def test_sender_reply_fallback_jailed_emoji_and_file_cleanup(self):
        class Chain(list):
            pass

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            emoji_dir = root / "emojis"
            outbox_dir = root / "outbox"
            emoji_dir.mkdir()
            outbox_dir.mkdir()
            good_emoji = emoji_dir / "cat.png"
            good_emoji.write_bytes(TINY_PNG)
            outside = root / "secret.txt"
            outside.write_text("top-secret", encoding="utf-8")
            good_file = outbox_dir / "report.md"
            good_file.write_text("# 报告", encoding="utf-8")

            repo = Repository(":memory:")
            bot_g1 = MagicMock()
            bot_g1.call_action = AsyncMock(return_value={})
            bot_g2 = MagicMock()
            bot_g2.call_action = AsyncMock(return_value={})
            bots = {"g1": bot_g1, "g2": bot_g2}
            invalid_faces = []
            ctx = MagicMock()
            ctx.send_message = AsyncMock(
                side_effect=[RuntimeError("reply target expired"), None, None, RuntimeError("unsupported face id"), None, None]
            )

            from astrbot_plugin_echo.domain import SendFace  # noqa: E402

            sender = AstrBotSender(
                context=ctx,
                comp_types=COMP_TYPES,
                chain_cls=Chain,
                emoji_root=emoji_dir,
                outbox_root=outbox_dir,
                limiter=MessageLimiter(per_minute=10),
                bot_provider=lambda gid="": bots.get(gid),
                repo=repo,
                on_invalid_face=lambda gid, fid: invalid_faces.append((gid, fid)),
            )

            # 1) SendText 首次带 Reply 失败时自动退回无 Reply 纯文本重试
            ok = asyncio.run(
                sender.send(SendText(group_id="g1", umo="u1", text="在的", reply_to_msg_id="m_old"))
            )
            self.assertTrue(ok)
            self.assertEqual(ctx.send_message.call_count, 2)

            # 2) SendEmoji 路径越界拒绝，牢笼内允许；SendFace 发送失败触发失效清理回调
            self.assertFalse(
                asyncio.run(sender.send(SendEmoji(group_id="g1", umo="u1", path=str(outside))))
            )
            self.assertTrue(
                asyncio.run(sender.send(SendEmoji(group_id="g1", umo="u1", path=str(good_emoji))))
            )
            self.assertFalse(
                asyncio.run(sender.send(SendFace(group_id="g1", umo="u1", face_id="489")))
            )
            self.assertEqual(invalid_faces, [("g1", "489")])

            # 3) SendFile 牢笼内发送 + 落库占位记录 + finally 自动删除源文件
            self.assertFalse(
                asyncio.run(sender.send(SendFile(group_id="g1", umo="u1", path=str(outside), name="secret.txt")))
            )
            self.assertTrue(
                asyncio.run(sender.send(SendFile(group_id="g1", umo="u1", path=str(good_file), name="report.md")))
            )
            self.assertFalse(good_file.exists())
            self.assertIn("[已发送群文件：report.md]", repo.recent_messages("g1")[0]["text"])

            # 4) ReactEmoji 按 group_id 精确路由 bot 实例，不跨群串台
            self.assertTrue(
                asyncio.run(sender.send(ReactEmoji(group_id="g2", umo="u2", msg_id="555", emoji_id="124")))
            )
            bot_g1.call_action.assert_not_called()
            bot_g2.call_action.assert_awaited_once_with("set_msg_emoji_like", message_id=555, emoji_id="124")

            # 5) 数字群号发送 SendEmoji 走 NapCat send_group_msg(sub_type=1)；失败自动回退普通 Image
            bot_num = MagicMock()
            bot_num.call_action = AsyncMock(side_effect=[None, RuntimeError("sub_type unsupported")])
            bots["aiocqhttp:10086"] = bot_num
            self.assertTrue(
                asyncio.run(sender.send(SendEmoji(group_id="aiocqhttp:10086", umo="u3", path=str(good_emoji))))
            )
            first_call = bot_num.call_action.call_args_list[0]
            self.assertEqual(first_call.args[0], "send_group_msg")
            self.assertEqual(first_call.kwargs["group_id"], 10086)
            seg_data = first_call.kwargs["message"][0]["data"]
            self.assertEqual(seg_data["sub_type"], 1)
            self.assertEqual(seg_data["summary"], "[动画表情]")
            self.assertTrue(seg_data["file"].startswith("base64://"))

            send_before = ctx.send_message.call_count
            self.assertTrue(
                asyncio.run(sender.send(SendEmoji(group_id="aiocqhttp:10086", umo="u3", path=str(good_emoji))))
            )
            self.assertEqual(ctx.send_message.call_count, send_before + 1)
            repo.close()

    def test_wave1_ssrf_lfi_forward_quota_and_limiter(self):
        # 1) CGNAT (100.64.0.1) 与 IPv4-mapped loopback (::ffff:127.0.0.1) 均被 SSRF 拦截
        with patch("socket.getaddrinfo", return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("100.64.0.1", 0))]):
            self.assertFalse(is_public_http_url("https://cgnat.example/img.png"))
        with patch("socket.getaddrinfo", return_value=[(socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("::ffff:127.0.0.1", 0, 0, 0))]):
            self.assertFalse(is_public_http_url("https://mapped.example/img.png"))

        # 2) 本地文件路径 fail-closed：无白名单 / 越界 / symlink 均拒，白名单内普通文件可读
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            allowed = root / "allowed"
            outside = root / "outside"
            allowed.mkdir()
            outside.mkdir()
            ok_img = allowed / "ok.png"
            ok_img.write_bytes(TINY_PNG)
            bad_img = outside / "secret.png"
            bad_img.write_bytes(TINY_PNG)
            sym_img = allowed / "sym.png"
            sym_img.symlink_to(bad_img)

            p_closed = AstrBotInboundParser(COMP_TYPES)
            self.assertIsNone(p_closed._is_safe_local_file(str(ok_img)))

            p_jailed = AstrBotInboundParser(COMP_TYPES, allowed_local_roots=(allowed,))
            self.assertIsNotNone(p_jailed._is_safe_local_file(str(ok_img)))
            self.assertIsNone(p_jailed._is_safe_local_file(str(bad_img)))
            self.assertIsNone(p_jailed._is_safe_local_file(str(sym_img)))

        # 3) @bot + 空内容合并转发会回查 get_forward_msg 且空 card 自动回落 nickname
        class _EmptyForward(_Forward):
            def __init__(self, fid="fwd_1"):
                super().__init__(content=())
                self.id = fid

        ev_fwd = self._make_event(parts=[_At(qq="8888"), _EmptyForward("fwd_99")], text="")
        ev_fwd.bot = MagicMock()
        ev_fwd.bot.call_action = AsyncMock(return_value={
            "messages": [{"sender": {"card": "", "nickname": "阿花"}, "content": "群聊转发原文"}]
        })
        parser = AstrBotInboundParser(COMP_TYPES, downloader=lambda url: b"")
        parsed_fwd = asyncio.run(parser.parse(ev_fwd))
        ev_fwd.bot.call_action.assert_awaited_once_with("get_forward_msg", id="fwd_99")
        self.assertIn("阿花", parsed_fwd.text)
        self.assertIn("群聊转发原文", parsed_fwd.text)

        # 4) 主消息自带图片优先占用 2 张配额，不被 Reply 引用的图片挤掉
        own1 = TINY_PNG + b"own1"
        own2 = TINY_PNG + b"own2"
        q1 = TINY_PNG + b"quote1"
        data_map = {
            "https://example.com/own1.png": own1,
            "https://example.com/own2.png": own2,
            "https://example.com/q1.png": q1,
        }
        p_img = AstrBotInboundParser(COMP_TYPES, downloader=lambda url: data_map.get(url, b""))
        ev_prio = self._make_event(
            parts=[
                _Reply(id="10", sender_id="2", sender_nickname="路人", chain=[_Image(url="https://example.com/q1.png")]),
                _At(qq="8888"),
                _Image(url="https://example.com/own1.png"),
                _Image(url="https://example.com/own2.png"),
            ]
        )
        parsed_prio = asyncio.run(p_img.parse(ev_prio))
        self.assertEqual([m.data for m in parsed_prio.images], [own1, own2])

        # 5) MessageLimiter.try_acquire 原子配额扣减与 per_minute<=0 拦截
        lim = MessageLimiter(per_minute=2)
        self.assertTrue(lim.try_acquire("g1", now=100.0))
        self.assertTrue(lim.try_acquire("g1", now=100.5))
        self.assertFalse(lim.try_acquire("g1", now=101.0))
        self.assertTrue(lim.try_acquire("g1", now=161.0))
        self.assertFalse(MessageLimiter(per_minute=0).try_acquire("g1", now=100.0))


if __name__ == "__main__":
    unittest.main()

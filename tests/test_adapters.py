"""adapters 层测试：LLM 原生 tools+vision 与降级、SearchProvider、NapCat 入站解析与出站牢笼。"""

import asyncio
import io
import json
import os
import socket
import sys
import tempfile
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
from astrbot_plugin_echo.adapters.search import HttpSearchProvider  # noqa: E402
from astrbot_plugin_echo.domain import (  # noqa: E402
    ImagePart,
    LLMMessage,
    ReactEmoji,
    SendEmoji,
    SendFile,
    SendText,
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
            if len(calls) == 3:
                # describe_image 调用
                return _fake_urlopen_response({"choices": [{"message": {"content": "一只蓝猫"}}]})
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
        turn = OpenAICompatClient._parse_response(data)
        self.assertEqual(turn.text, "直接思考得出答案：42")

        mock_turn = MockLLMClient().chat((LLMMessage(role="user", content="在吗"),))
        self.assertIn("在呢", mock_turn.text)


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

        # 2) sub_type=1 或 summary 含表情 -> 识别为真表情包
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
            parts=[_At(qq="8888"), _Forward(content=[{"sender": {"nickname": "甲"}, "content": "明天开会"}])]
        )
        parsed_fwd = asyncio.run(parser.parse(ev_fwd))
        self.assertIn("明天开会", parsed_fwd.text)

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
            bot = MagicMock()
            bot.call_action = AsyncMock(return_value={})
            ctx = MagicMock()
            ctx.send_message = AsyncMock(side_effect=[RuntimeError("reply target expired"), None, None, None])

            sender = AstrBotSender(
                context=ctx,
                comp_types=COMP_TYPES,
                chain_cls=Chain,
                emoji_root=emoji_dir,
                outbox_root=outbox_dir,
                limiter=MessageLimiter(per_minute=10),
                bot_provider=lambda: bot,
                repo=repo,
            )

            # 1) SendText 首次带 Reply 失败时自动退回无 Reply 纯文本重试
            ok = asyncio.run(
                sender.send(SendText(group_id="g1", umo="u1", text="在的", reply_to_msg_id="m_old"))
            )
            self.assertTrue(ok)
            self.assertEqual(ctx.send_message.call_count, 2)

            # 2) SendEmoji 路径越界拒绝，牢笼内允许
            self.assertFalse(
                asyncio.run(sender.send(SendEmoji(group_id="g1", umo="u1", path=str(outside))))
            )
            self.assertTrue(
                asyncio.run(sender.send(SendEmoji(group_id="g1", umo="u1", path=str(good_emoji))))
            )

            # 3) SendFile 牢笼内发送 + 落库占位记录 + finally 自动删除源文件
            self.assertFalse(
                asyncio.run(sender.send(SendFile(group_id="g1", umo="u1", path=str(outside), name="secret.txt")))
            )
            self.assertTrue(
                asyncio.run(sender.send(SendFile(group_id="g1", umo="u1", path=str(good_file), name="report.md")))
            )
            self.assertFalse(good_file.exists())
            self.assertIn("[已发送群文件：report.md]", repo.recent_messages("g1")[0]["text"])

            # 4) ReactEmoji
            self.assertTrue(
                asyncio.run(sender.send(ReactEmoji(group_id="g1", umo="u1", msg_id="555", emoji_id="124")))
            )
            bot.call_action.assert_awaited_once_with("set_msg_emoji_like", message_id=555, emoji_id="124")
            repo.close()


if __name__ == "__main__":
    unittest.main()

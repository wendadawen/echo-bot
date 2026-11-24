"""全量深度审查修复回归测试：事件路由、显式@必回、Harness/模型、记忆/人物/上下文、提醒与运维脚本。全部离线。"""

import asyncio
import importlib
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugin"))

from astrbot_plugin_echo.core import attention, display, forward, prompts, vision  # noqa: E402
from astrbot_plugin_echo.core.actions import ToolLoop, ToolResultStore, judge, parse_full, validate  # noqa: E402
from astrbot_plugin_echo.core.brain import (  # noqa: E402
    Brain, FORMAT_HINT, HELP, _scrub_internal_leaks, _unwrap_reply_text, is_unsafe,
)
from astrbot_plugin_echo.core.context import SAFETY, ContextBuilder  # noqa: E402
from astrbot_plugin_echo.core.harness import CodexHarness  # noqa: E402
from astrbot_plugin_echo.core.memory import MEMORY_PROMPT, Memory, parse_memory_full  # noqa: E402
from astrbot_plugin_echo.core.messages import Message  # noqa: E402
from astrbot_plugin_echo.core.models import (  # noqa: E402
    DEFAULT_API_BASE, DEFAULT_MODEL_TIMEOUT, MockModel, OpenAIModel,
    _extract_message_text, build_model, resolve_model_config,
)
from astrbot_plugin_echo.core.monitor import Monitor  # noqa: E402
from astrbot_plugin_echo.core.person_info import PersonStore, _merge_field  # noqa: E402
from astrbot_plugin_echo.core.rhythm import Batch, Item, Rhythm  # noqa: E402
from astrbot_plugin_echo.core.schedule import Schedule  # noqa: E402
from astrbot_plugin_echo.core.store import Store, cleanup_runtime_caches  # noqa: E402
from astrbot_plugin_echo.core.subagent import SubAgentManager  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def _make_astrbot_stubs():
    registered_handlers = []

    class FakePlain:
        def __init__(self, text):
            self.text = text

    class FakeAt:
        def __init__(self, qq):
            self.qq = qq

    class FakeFile:
        def __init__(self, name, file="", url=""):
            self.name = name
            self.file = file

    class FakeImage:
        def __init__(self, file="", url="", path="", subType=None, summary=""):
            self.file = file
            self.url = url
            self.path = path
            self.subType = subType
            self.summary = summary

    class FakeFace:
        def __init__(self, id=0, name=""):
            self.id = id
            self.name = name

    class FakeJson:
        type = "json"

        def __init__(self, data="{}"):
            self.data = data

    class FakePoke:
        type = "poke"

    class FakeForward:
        def __init__(self, fid=""):
            self.id = fid

    class FakeReply:
        def __init__(self, sender_id="", sender_nickname="", message_str="", chain=None):
            self.sender_id = sender_id
            self.sender_nickname = sender_nickname
            self.message_str = message_str
            self.chain = chain or []

    class FakeChain:
        def __init__(self, chain):
            self.chain = chain

    class FakeStar:
        def __init__(self, context=None):
            self.context = context

    class FakeStarTools:
        @staticmethod
        def get_data_dir(*args):
            return tempfile.gettempdir()

    class FakeLogger:
        def info(self, *a, **k):
            pass

        def warning(self, *a, **k):
            pass

        def error(self, *a, **k):
            pass

    class FakeFilter:
        class EventMessageType:
            GROUP_MESSAGE = "group"

        @staticmethod
        def on_astrbot_loaded():
            return lambda fn: fn

        @staticmethod
        def event_message_type(*a, **k):
            def deco(fn):
                registered_handlers.append((fn.__name__, k))
                return fn

            return deco

    astrbot_mod = types.ModuleType("astrbot")
    api_mod = types.ModuleType("astrbot.api")
    api_mod.logger = FakeLogger()
    event_mod = types.ModuleType("astrbot.api.event")
    event_mod.AstrMessageEvent = object
    event_mod.MessageChain = FakeChain
    event_mod.filter = FakeFilter
    comp_mod = types.ModuleType("astrbot.api.message_components")
    comp_mod.At = FakeAt
    comp_mod.Face = FakeFace
    comp_mod.File = FakeFile
    comp_mod.Image = FakeImage
    comp_mod.Plain = FakePlain
    comp_mod.Reply = FakeReply
    comp_mod.Json = FakeJson
    comp_mod.Poke = FakePoke
    comp_mod.Forward = FakeForward
    star_mod = types.ModuleType("astrbot.api.star")
    star_mod.Context = object
    star_mod.Star = FakeStar
    star_mod.StarTools = FakeStarTools
    stubs = {
        "astrbot": astrbot_mod,
        "astrbot.api": api_mod,
        "astrbot.api.event": event_mod,
        "astrbot.api.message_components": comp_mod,
        "astrbot.api.star": star_mod,
    }
    return stubs, comp_mod, registered_handlers


class TestEventIntegrationAndMentions(unittest.TestCase):
    def test_bare_at_command_recovery_yield_order_json_passthrough_and_initialize(self):
        stubs, comp, registered = _make_astrbot_stubs()
        sys.modules.pop("astrbot_plugin_echo.main", None)
        with patch.dict(sys.modules, stubs):
            main_mod = importlib.import_module("astrbot_plugin_echo.main")
            self.addCleanup(lambda: sys.modules.pop("astrbot_plugin_echo.main", None))

            group_meta = [k for name, k in registered if name == "on_group_message"]
            self.assertEqual(sys.maxsize, group_meta[0]["priority"])

            with tempfile.TemporaryDirectory() as folder:
                with patch.object(main_mod, "_data_dir", return_value=Path(folder)):
                    echo = main_mod.Echo(context=object(), config={})

                    class FakeBot:
                        async def call_action(self, action, **kw):
                            if action == "get_forward_msg":
                                return {"data": {"messages": [
                                    {"type": "node", "data": {"nickname": "阿伟", "content": [{"type": "text", "data": {"text": "这是合并转发内容"}}]}}
                                ]}}
                            return {}

                    class FakeEvent:
                        unified_msg_origin = "aiocqhttp:GroupMessage:123"

                        def __init__(self, msg_str, msgs, raw_msg=None, obj_str=""):
                            self._msg_str = msg_str
                            self._msgs = msgs
                            self.stopped = False
                            self.llm_flag = None
                            self.call_order = []
                            self.message_obj = types.SimpleNamespace(
                                message_str=obj_str, raw_message=raw_msg
                            )
                            self.bot = FakeBot()

                        def should_call_llm(self, val):
                            self.llm_flag = val

                        def get_sender_id(self):
                            return "1001"

                        def get_sender_name(self):
                            return "阿伟"

                        def get_self_id(self):
                            return "9999"

                        def get_group_id(self):
                            return "123"

                        def get_platform_name(self):
                            return "aiocqhttp"

                        def get_message_str(self):
                            return self._msg_str

                        def get_messages(self):
                            return self._msgs

                        def plain_result(self, text):
                            self.call_order.append(("yield", text))
                            return text

                        def stop_event(self):
                            self.call_order.append(("stop", None))
                            self.stopped = True

                    async def run_flow():
                        try:
                            # 1) initialize() 补起后台协程（支持热重载）
                            await echo.initialize()
                            self.assertIsNotNone(echo._chat_task)
                            self.assertIsNotNone(echo._reminder_task)

                            # 2) 纯 @ 机器人：不丢失、should_call_llm(True)、被当作自然呼唤入批
                            bare_ev = FakeEvent("", [comp.At("9999")])
                            async for _ in echo.on_group_message(bare_ev):
                                pass
                            self.assertTrue(bare_ev.llm_flag)
                            self.assertTrue(bare_ev.stopped)
                            batches = echo.rhythm.due(time.time() + 20)
                            self.assertEqual(1, len(batches))
                            self.assertTrue(batches[0].mentioned)
                            self.assertIn("只 @ 了你一下", batches[0].body_text)

                            # 3) /帮助 被 AstrBot WakingCheckStage 剥成 "帮助" 时从 Plain 恢复，且 yield 早于 stop_event()
                            cmd_ev = FakeEvent("帮助", [comp.Plain("/帮助")])
                            out_cmds = []
                            async for res in echo.on_group_message(cmd_ev):
                                out_cmds.append(res)
                            self.assertEqual(1, len(out_cmds))
                            self.assertIn("/取消提醒", out_cmds[0])
                            self.assertEqual(["yield", "stop"], [k for k, _ in cmd_ev.call_order])

                            # 4) JSON 卡片 / 戳一戳 / non-message post_type 放行（不 stop_event）
                            json_ev = FakeEvent("", [comp.Json('{"app":"com.tencent.structmsg"}')])
                            async for _ in echo.on_group_message(json_ev):
                                pass
                            self.assertFalse(json_ev.stopped)
                            self.assertTrue(json_ev.llm_flag)

                            notice_ev = FakeEvent("", [], raw_msg={"post_type": "notice"})
                            async for _ in echo.on_group_message(notice_ev):
                                pass
                            self.assertFalse(notice_ev.stopped)

                            # 5) 文本里直呼 "大肥鱼 ..." 触发 _mentioned()
                            self.assertTrue(echo._mentioned(FakeEvent("大肥鱼 你看看", [comp.Plain("大肥鱼 你看看")]), "大肥鱼 你看看"))

                            # 6) Forward id 通过 get_forward_msg 自动展开
                            fwd_ev = FakeEvent("", [comp.Forward("fwd-1")])
                            async for _ in echo.on_group_message(fwd_ev):
                                pass
                            fwd_batches = echo.rhythm.due(time.time() + 20)
                            self.assertEqual(1, len(fwd_batches))
                            self.assertIn("这是合并转发内容", fwd_batches[0].body_text)
                        finally:
                            await echo.terminate()

                    asyncio.run(run_flow())

    def test_image_followup_quote_face_mface_and_auto_preview(self) -> None:
        import importlib
        import struct
        import threading
        import types
        from unittest.mock import MagicMock, patch

        stubs, comp, _ = _make_astrbot_stubs()
        png_bytes = (
            b"\x89PNG\r\n\x1a\n"
            + struct.pack(">II", 64, 64)
            + b"\x08\x06\x00\x00\x00"
            + b"0" * 64
        )

        class _FakeContext:
            def __init__(self):
                self.sent = []

            async def send_message(self, umo, chain):
                self.sent.append((umo, chain))

        class _FakeInspectModel:
            def __init__(self):
                self.vision_calls = []
                self.reply_calls = []

            def vision_reply(self, prompt, image_uri):
                self.vision_calls.append((prompt, image_uri))
                return "一串橙色珠子在阳光下围成圆环"

            def reply(self, system, history, user_text):
                self.reply_calls.append((system, history, user_text))
                return '{"action":"reply","text":"看清楚了，是一串橙色珠子"}'

        class _FakeEvent:
            def __init__(
                self,
                group_id,
                text,
                parts,
                sender_id="2573394163",
                sender_name="炸芋花",
                at_or_wake=False,
                raw_message=None,
            ):
                self.group_id = group_id
                self.text = text
                self.parts = list(parts)
                self.sender_id = sender_id
                self.sender_name = sender_name
                self._at_or_wake = at_or_wake
                self.unified_msg_origin = f"aiocqhttp:GroupMessage:{group_id}"
                self.bot = None
                self.message_obj = types.SimpleNamespace(
                    raw_message=raw_message if raw_message is not None else {"post_type": "message"},
                    message_str=text,
                    message=self.parts,
                )

            def get_sender_id(self):
                return self.sender_id

            def get_sender_name(self):
                return self.sender_name

            def get_self_id(self):
                return "999999"

            def get_group_id(self):
                return self.group_id

            def get_platform_name(self):
                return "aiocqhttp"

            def get_messages(self):
                return self.parts

            def get_message_str(self):
                return self.text

            def is_at_or_wake_command(self):
                return self._at_or_wake

            def should_call_llm(self, flag):
                return None

            def stop_event(self):
                return None

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(sys.modules, stubs):
                sys.modules.pop("astrbot_plugin_echo.main", None)
                import astrbot_plugin_echo.main as echo_main

                importlib.reload(echo_main)

                async def run_flow():
                    ctx = _FakeContext()
                    with patch.object(echo_main.StarTools, "get_data_dir", lambda *a, **kw: tmpdir):
                        echo = echo_main.Echo(ctx, config={})
                    fake_model = _FakeInspectModel()
                    echo.model = fake_model
                    echo.brain.model = fake_model
                    echo.brain.tool_loop.model = fake_model
                    # 停掉后台循环避免与测试手工 _process_batch 竞争。
                    for tname in ("_reminder_task", "_chat_task"):
                        task = getattr(echo, tname, None)
                        if task is not None:
                            task.cancel()
                            setattr(echo, tname, None)

                    collect_started = threading.Event()
                    collect_gate = threading.Event()

                    def slow_collect(*args, **kwargs):
                        collect_started.set()
                        collect_gate.wait(timeout=4.0)

                    download_mock = MagicMock(return_value=png_bytes)
                    try:
                        with patch.object(echo, "_download", new=download_mock), patch.object(
                            echo.emoji_bank, "collect", side_effect=slow_collect
                        ):
                            # 1) 未@独立图片：写库 [图片] + 附带字节到 rhythm，慢 collect 不阻塞后续 @bot 批处理且自动预览
                            gid1 = "1057446698"
                            img_ev = _FakeEvent(
                                gid1,
                                "",
                                [comp.Image(url="https://cdn.example.com/bead.png", subType=1)],
                            )
                            async for _ in echo.on_group_message(img_ev):
                                pass
                            await asyncio.sleep(0.05)
                            # 允许图片先挂到 rhythm，同时 slow_collect 仍被 gate 阻塞
                            stored_msgs = (
                                echo.store.recent(f"aiocqhttp:{gid1}", 10)
                                or echo.store.recent(gid1, 10)
                            )
                            self.assertTrue(
                                any("[图片]" in str(m.get("text") or "") for m in stored_msgs),
                                f"未@图片应记入历史 [图片]，实际: {stored_msgs}",
                            )
                            follow_ev = _FakeEvent(
                                gid1,
                                "这个是什么",
                                [comp.At("999999"), comp.Plain(" 这个是什么")],
                                at_or_wake=True,
                            )
                            async for _ in echo.on_group_message(follow_ev):
                                pass
                            batches1 = echo.rhythm.due(time.time() + 20)
                            self.assertTrue(batches1)
                            t0 = time.monotonic()
                            await echo._process_batch(batches1[-1])
                            elapsed = time.monotonic() - t0
                            self.assertLess(elapsed, 1.5, f"慢 emoji_bank.collect 不应阻塞 _process_batch ({elapsed:.2f}s)")
                            self.assertTrue(collect_started.wait(timeout=1.0))
                            self.assertGreaterEqual(len(fake_model.vision_calls), 1)
                            chat_calls1 = [
                                (s, u) for (s, _h, u) in fake_model.reply_calls
                                if "事实记忆" not in f"{s}\n{u}"
                            ]
                            self.assertTrue(chat_calls1)
                            sys_prompt, user_prompt = chat_calls1[-1]
                            combined_prompt = f"{sys_prompt}\n{user_prompt}"
                            self.assertIn("本批已成功收到 1 张图片", combined_prompt)
                            self.assertIn("已查看本批附带图片", combined_prompt)

                            # 2) 引用图片 Reply.chain + @bot -> _extract_quote 含 [图片]，且 "我说你看..." 不误判 retold
                            from astrbot_plugin_echo.core import mentions as echo_mentions

                            self.assertFalse(echo_mentions.is_retold("我说你看我发的图片是什么"))
                            self.assertFalse(echo_mentions.is_retold("但是我说你看这个"))
                            self.assertTrue(echo_mentions.is_retold("但是他在说你诶"))
                            self.assertTrue(echo_mentions.is_retold("小明跟我说你很聪明"))

                            gid2 = "2057446698"
                            quote_ev = _FakeEvent(
                                gid2,
                                "我说你看我发的图片是什么",
                                [
                                    comp.Reply(
                                        sender_id="2573394163",
                                        sender_nickname="炸芋花",
                                        message_str="",
                                        chain=[comp.Image(url="https://cdn.example.com/quoted.png", subType=0)],
                                    ),
                                    comp.At("999999"),
                                    comp.Plain(" 我说你看我发的图片是什么"),
                                ],
                                at_or_wake=True,
                            )
                            quote_text = echo._extract_quote(quote_ev)
                            self.assertIn("[图片]", quote_text)
                            async for _ in echo.on_group_message(quote_ev):
                                pass
                            batches2 = echo.rhythm.due(time.time() + 20)
                            self.assertEqual(1, len(batches2))
                            vision_before = len(fake_model.vision_calls)
                            reply_before = len(fake_model.reply_calls)
                            await echo._process_batch(batches2[0])
                            self.assertGreater(len(fake_model.vision_calls), vision_before)
                            chat_calls2 = [
                                (s, u) for (s, _h, u) in fake_model.reply_calls[reply_before:]
                                if "事实记忆" not in f"{s}\n{u}"
                            ]
                            self.assertTrue(chat_calls2)
                            sys_p2, user_p2 = chat_calls2[-1]
                            combined2 = f"{sys_p2}\n{user_p2}"
                            self.assertIn("本批已成功收到 1 张图片", combined2)
                            self.assertIn("已查看本批附带图片", combined2)

                            # 3) @bot [Face(id=76)] 这个是什么表情包 -> 文本含 [QQ表情:赞]
                            gid3 = "3057446698"
                            face_ev = _FakeEvent(
                                gid3,
                                "这个是什么表情包",
                                [comp.At("999999"), comp.Face(id=76), comp.Plain(" 这个是什么表情包")],
                                at_or_wake=True,
                            )
                            async for _ in echo.on_group_message(face_ev):
                                pass
                            batches3 = echo.rhythm.due(time.time() + 20)
                            self.assertEqual(1, len(batches3))
                            self.assertIn("[QQ表情:赞]", batches3[0].body_text)

                            # 4) OneBot mface 商城表情 -> 含 [商城表情:<summary>] 且下载 url 附图
                            gid4 = "4057446698"
                            mface_url = "https://gxh.vip.qq.com/club/item/parcel/item/ab/abcd/raw300.gif"
                            mface_ev = _FakeEvent(
                                gid4,
                                "这个商城表情呢",
                                [comp.At("999999"), comp.Plain(" 这个商城表情呢")],
                                at_or_wake=True,
                                raw_message={
                                    "post_type": "message",
                                    "message": [
                                        {
                                            "type": "mface",
                                            "data": {"summary": "开心小猫", "url": mface_url},
                                        }
                                    ],
                                },
                            )
                            async for _ in echo.on_group_message(mface_ev):
                                pass
                            batches4 = echo.rhythm.due(time.time() + 20)
                            self.assertEqual(1, len(batches4))
                            self.assertIn("[商城表情:开心小猫]", batches4[0].body_text)
                            await echo._process_batch(batches4[0])
                            downloaded_urls = [c.args[0] for c in download_mock.call_args_list if c.args]
                            self.assertIn(mface_url, downloaded_urls)
                            self.assertGreaterEqual(len(batches4[0].images), 1)
                    finally:
                        collect_gate.set()
                        await echo.terminate()

                asyncio.run(run_flow())

    def test_time_awareness_followup_continuity_silent_ttl_outbox_and_memory_guards(self) -> None:
        from astrbot_plugin_echo.core.messages import format_history_time
        from astrbot_plugin_echo.core.rhythm import SILENT_MAX_AGE

        now = time.time()
        # 1) format_history_time + OpenAIModel.reply + ContextBuilder.build + spontaneous 时间感
        self.assertIn("刚刚", format_history_time(now - 10, now=now))
        self.assertIn("分钟前", format_history_time(now - 600, now=now))
        self.assertIn("小时前", format_history_time(now - 7200, now=now))
        self.assertIn("天前", format_history_time(now - 2 * 86400, now=now))
        self.assertEqual("", format_history_time(0, now=now))

        captured_payload = {}

        class FakeResp:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return json.dumps({
                    "choices": [{"message": {"role": "assistant", "content": "收到"}}]
                }).encode("utf-8")

        def fake_urlopen(req, timeout=40):
            captured_payload.update(json.loads(req.data.decode("utf-8")))
            return FakeResp()

        oa = OpenAIModel("k", "http://localhost/v1", "m")
        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            oa.reply(
                "sys",
                [
                    {"role": "user", "user_name": "阿伟", "text": "带时间的旧话", "ts": now - 1800},
                    {"role": "bot", "user_name": "大肥鱼", "text": "机器人的话", "ts": now - 1700},
                    {"role": "user", "user_name": "小美", "text": "无时间的话"},
                ],
                "[昵称：阿伟] 在吗",
            )
        msgs = captured_payload["messages"]
        self.assertRegex(msgs[1]["content"], r"^\[\d{2}:\d{2} · 30分钟前\] \[昵称：阿伟\] 带时间的旧话$")
        self.assertEqual({"role": "assistant", "content": "机器人的话"}, msgs[2])
        self.assertEqual({"role": "user", "content": "[昵称：小美] 无时间的话"}, msgs[3])

        cb = ContextBuilder("人设")
        built = cb.build(
            "g1",
            query="好忙呀",
            user_name="炸芋花",
            history=[
                {"role": "bot", "user_name": "大肥鱼", "text": "上回说的", "ts": now - 3600},
                {"role": "user", "user_name": "炸芋花", "text": "之前的", "ts": now - 2100},
            ],
        )
        self.assertIn("当前时间：", built)
        self.assertIn("你上次发言于", built)
        self.assertIn("上一条群消息距今已较久", built)

        spont_prompts = []

        class InspectSpontModel:
            def reply(self, system, history, user_text):
                spont_prompts.append(user_text)
                return "今晚风挺大的。"

        store_sp = Store(":memory:")
        store_sp.add_message("g1", "u1", "炸芋花", "user", "之前的表情", ts=now - 3 * 3600)
        brain_sp = Brain(store_sp, InspectSpontModel())
        sp_line = brain_sp.spontaneous("g1", "", False)
        self.assertEqual("今晚风挺大的。", sp_line)
        self.assertTrue(spont_prompts and "距离上一条群消息已过去约 3" in spont_prompts[0])

        # 2) Brain.view_rate 连续对话 / 跨人跟进待办 / 刚发言后的纯 QQ 表情 / 路人短语
        brain_vr = Brain(Store(":memory:"), MockModel())
        brain_vr._last_reply_user["g1"] = "u1"
        brain_vr._last_reply_at["g1"] = time.time()
        self.assertEqual(0.85, brain_vr.view_rate(Batch("g1", items=[Item("你规划一下", "u1", "A", False, "umo", time.time())])))
        self.assertIsNone(brain_vr.view_rate(Batch("g1", items=[Item("有道理", "u2", "B", False, "umo", time.time())])))

        brain_vr.record_spontaneous("g1", "你要的话我现在帮你查一下？")
        self.assertEqual(0.85, brain_vr.view_rate(Batch("g1", items=[Item("查找吧", "u2", "B", False, "umo", time.time())])))
        self.assertEqual(0.85, brain_vr.view_rate(Batch("g1", items=[Item("[QQ表情:大怨种]", "u3", "C", False, "umo", time.time())])))

        brain_vr.record_spontaneous("g2", "出来冒个泡。")
        self.assertEqual(0.75, brain_vr.view_rate(Batch("g2", items=[Item("[QQ表情:大怨种]", "u3", "C", False, "umo", time.time())])))

        # 3) Rhythm.silent_due 超过 SILENT_MAX_AGE (300s) 不再翻旧账，未过期时仍可触发
        rhythm = Rhythm(view_rate=1.0)
        base_t = time.time()
        rhythm.add("g_old", Item("旧消息", "u1", "A", False, "umo", base_t), now=base_t)
        rhythm.due(base_t + 20)
        rhythm.mark_view("g_old", base_t + 20)
        self.assertTrue(rhythm.silent_due("g_old", now=base_t + SILENT_MAX_AGE - 20, interval=2))
        self.assertFalse(rhythm.silent_due("g_old", now=base_t + SILENT_MAX_AGE + 10, interval=2))

        # 4) Echo.on_group_message 未@纯 Face 仅在刚发言 120s 内接进 rhythm；Echo._send_file 落库群文件记录
        stubs, comp, _ = _make_astrbot_stubs()

        class _FakeCtx:
            def __init__(self):
                self.sent = []

            async def send_message(self, umo, chain):
                self.sent.append((umo, chain))

        class _FakeFaceEvent:
            def __init__(self, gid, parts):
                self.gid = gid
                self.parts = list(parts)
                self.unified_msg_origin = f"aiocqhttp:GroupMessage:{gid}"
                self.bot = None
                self.message_obj = types.SimpleNamespace(raw_message={"post_type": "message"}, message_str="", message=self.parts)

            def get_sender_id(self):
                return "2573394163"

            def get_sender_name(self):
                return "炸芋花"

            def get_self_id(self):
                return "999999"

            def get_group_id(self):
                return self.gid

            def get_platform_name(self):
                return "aiocqhttp"

            def get_messages(self):
                return self.parts

            def get_message_str(self):
                return ""

            def is_at_or_wake_command(self):
                return False

            def should_call_llm(self, flag):
                return None

            def stop_event(self):
                return None

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(sys.modules, stubs):
                sys.modules.pop("astrbot_plugin_echo.main", None)
                import astrbot_plugin_echo.main as echo_main

                importlib.reload(echo_main)

                async def run_face_and_file():
                    ctx = _FakeCtx()
                    with patch.object(echo_main.StarTools, "get_data_dir", lambda *a, **kw: tmpdir):
                        echo = echo_main.Echo(ctx, config={})
                    for tname in ("_reminder_task", "_chat_task"):
                        task = getattr(echo, tname, None)
                        if task is not None:
                            task.cancel()
                            setattr(echo, tname, None)
                    try:
                        # 无近期回复 -> 独立 Face 仍忽略
                        async for _ in echo.on_group_message(_FakeFaceEvent("g_idle", [comp.Face(id=344)])):
                            pass
                        self.assertEqual([], echo.rhythm.due(time.time() + 20))

                        # 120s 内刚发言过 -> 独立 Face 作为 kind="face" 进 rhythm
                        echo.brain._last_reply_at["aiocqhttp:g_live"] = time.time()
                        async for _ in echo.on_group_message(_FakeFaceEvent("g_live", [comp.Face(id=344)])):
                            pass
                        face_batches = echo.rhythm.due(time.time() + 20)
                        self.assertEqual(1, len(face_batches))
                        self.assertEqual("[QQ表情:大怨种]", face_batches[0].body_text)
                        self.assertEqual("face", face_batches[0].items[0].kind)

                        # _send_file 成功发送后往 store 写入 [已发送群文件：...]
                        out_dir = Path(tmpdir) / "outbox" / "t1"
                        out_dir.mkdir(parents=True, exist_ok=True)
                        f_test = out_dir / "report.md"
                        f_test.write_text("# report", encoding="utf-8")
                        echo._outbox_root = str(Path(tmpdir) / "outbox")
                        await echo._send_file("aiocqhttp:g_live", "aiocqhttp:GroupMessage:g_live", str(f_test), "report.md")
                        rec = echo.store.recent("aiocqhttp:g_live", 5)
                        self.assertTrue(any("[已发送群文件：report.md]" in r.get("text", "") for r in rec))
                    finally:
                        await echo.terminate()

                asyncio.run(run_face_and_file())

        # 5) _build_delivery_row：失败查店废文件自动清理且不送群、不入记忆；真实请求文件保留；天气/晚饭结论不入长期记忆
        with tempfile.TemporaryDirectory() as folder:
            outbox_root = Path(folder) / "outbox"
            d_fail = outbox_root / "task-fail"
            d_fail.mkdir(parents=True)
            f_fail = d_fail / "dinner_note.md"
            f_fail.write_text("未能核实", encoding="utf-8")

            d_keep = outbox_root / "task-keep"
            d_keep.mkdir(parents=True)
            f_keep = d_keep / "summary.md"
            f_keep.write_text("报告正文", encoding="utf-8")

            class RewriteDirectModel:
                def reply(self, system, history, user_text):
                    if "未能核实" in user_text or "没能联网" in user_text:
                        return "没能联网核实附近店名和价格，这次不敢乱推荐。"
                    return "上海西岸现在 14°C。"

            mem_guard = Memory(":memory:")
            store_guard = Store(":memory:")
            brain_guard = Brain(store_guard, RewriteDirectModel(), memory=mem_guard)
            brain_guard.harness = types.SimpleNamespace(outbox_root=str(outbox_root), timeout=180)

            row_fail = brain_guard._build_delivery_row(
                {
                    "id": "tf1",
                    "prompt": "查找苏州工业园区若水路99号附近25元以内晚饭",
                    "status": "completed",
                    "result": "没能联网核实店铺营业状态与价格，不敢乱推荐。",
                    "files": [{"name": "dinner_note.md", "path": str(f_fail), "size": 12}],
                },
                "g1",
                "umo:1",
            )
            self.assertEqual([], row_fail["files"])
            self.assertFalse(f_fail.exists())
            self.assertEqual([], mem_guard.recall("g1", "若水路 晚饭"))

            row_weather = brain_guard._build_delivery_row(
                {
                    "id": "tw1",
                    "prompt": "调查一下上海西岸天气现在",
                    "status": "completed",
                    "result": "上海西岸现在 14°C。",
                    "files": [],
                },
                "g1",
                "umo:1",
            )
            self.assertTrue(row_weather["ok"])
            self.assertEqual([], mem_guard.recall("g1", "上海西岸 天气"))

            row_keep = brain_guard._build_delivery_row(
                {
                    "id": "tk1",
                    "prompt": "帮我调研并发一份文件报告（哪怕没联网也要说明局限）",
                    "status": "completed",
                    "result": "部分公开信息已整理，实时店铺没能联网核实。",
                    "files": [{"name": "summary.md", "path": str(f_keep), "size": 12}],
                },
                "g1",
                "umo:1",
            )
            self.assertEqual(1, len(row_keep["files"]))
            self.assertTrue(f_keep.exists())

        # 6) _summarize 过滤一次性排障/故障噪音，不写入 episodes / points / persons
        class NoisyMemModel:
            def reply(self, system, history, user_text):
                return json.dumps({
                    "episode": "群内反馈图像识别可能有问题，以上问题都已修复",
                    "persons": [
                        {"name": "和", "category": "person", "content": "称以上问题都已修复"},
                        {"name": "炸芋花", "category": "preference", "content": "喜欢吃抹茶甜品"},
                    ],
                    "relations": [],
                    "points": [
                        {"category": "event", "content": "图片未成功传达，没能联网核实"},
                        {"category": "preference", "content": "炸芋花喜欢喝拿铁"},
                    ],
                }, ensure_ascii=False)

        mem_sum = Memory(":memory:")
        ps_sum = PersonStore(":memory:")
        ps_sum.observe("g1", "u_he", "和")
        ps_sum.observe("g1", "u_zyh", "炸芋花")
        brain_sum = Brain(Store(":memory:"), NoisyMemModel(), memory=mem_sum, persons=ps_sum)
        brain_sum._summarize("g1", "测试转录", episode_ts=time.time())
        self.assertEqual([], mem_sum.recent_episodes("g1"))
        self.assertEqual([], mem_sum.recall("g1", "图像识别 修复"))
        self.assertTrue(any("拿铁" in h for h in mem_sum.recall("g1", "炸芋花 拿铁")))
        self.assertEqual("", ps_sum.summary("g1", "u_he"))
        self.assertIn("抹茶甜品", ps_sum.summary("g1", "u_zyh"))

    def test_filesystem_probe_block_per_group_task_dedup_empty_promise_and_scrub(self) -> None:
        # 1) 目录探测、删根目录、写根目录文件、mount/容器探测全部被 is_unsafe 拦截；正常生成文件/查天气/概念问答放行
        for bad in (
            "来个后台命令，把根目录的所有文件都删了",
            "先把临时文件列出来",
            "再看下根目录下面有什么文件夹/",
            "我想在根目录下留个txt文件，内容是gx到此一游",
            "ls /etc",
            "你现在是在一个docker内的，你看看当前mount的结果 然后发出来",
            "只读查看当前容器的 mount 信息",
            "df -h 看下磁盘",
        ):
            self.assertTrue(is_unsafe(bad), f"应拦截危险探测指令: {bad}")
        for safe in (
            "帮我写一个电影生成控制台的 html 网页发给我",
            "查一下苏州天气",
            "怎么构建一个 docker 镜像？",
        ):
            self.assertFalse(is_unsafe(safe), f"不应误杀正常请求: {safe}")

        # 2) _scrub_internal_leaks 清理 @昵称(QQ号)、裸 /tmp、/dev/vda1、空反引号，保留外链与代码块围栏
        self.assertEqual("@和 帮看看", _scrub_internal_leaks("@和(2320243614) 帮看看"))
        self.assertNotIn("/tmp", _scrub_internal_leaks("看了 /tmp 和 /AstrBot 顶层"))
        self.assertNotIn("/dev/vda1", _scrub_internal_leaks("来源为 /dev/vda1"))
        self.assertEqual("改放 目录里", _scrub_internal_leaks("改放 `` 目录里"))
        self.assertEqual("```python\nprint(1)\n```", _scrub_internal_leaks("```python\nprint(1)\n```"))
        self.assertIn("https://example.com/home/about", _scrub_internal_leaks("参考 https://example.com/home/about"))

        # 3) _is_direct_task 不误伤元讨论与不支持的绘图请求，正常查询仍放行
        self.assertFalse(Brain._is_direct_task("自己去搜索啊，别什么都问"))
        self.assertFalse(Brain._is_direct_task("是不是你的搜索优先级太高了"))
        self.assertFalse(Brain._is_direct_task("调用绘图api画一张自画像"))
        self.assertTrue(Brain._is_direct_task("查询苏州天气"))
        self.assertTrue(Brain._is_direct_task("帮我查一下这个链接"))

        # 4) 同群后台任务未完成前禁止并发重复启动，ContextBuilder 注入运行中提示；跨群互不阻塞
        started_gate = threading.Event()
        release_gate = threading.Event()
        started_groups = []

        class BlockingHarness:
            timeout = 300
            outbox_root = ""

            def available(self):
                return True

            def run_with_files(self, gid, task):
                started_groups.append(gid)
                started_gate.set()
                release_gate.wait(timeout=4.0)
                return {"status": "ok", "text": "完成", "partial": "", "files": []}

        brain_dedup = Brain(Store(":memory:"), MockModel(), harness=BlockingHarness())
        brain_dedup.subagent = SubAgentManager()
        try:
            r1 = brain_dedup._dispatch_task("g1", "核实黎曼猜想近期新闻", "umo:1")
            self.assertEqual("task", r1["reason"])
            self.assertTrue(started_gate.wait(timeout=2.0))

            r2 = brain_dedup._dispatch_task("g1", "再查一遍黎曼猜想", "umo:1")
            self.assertEqual("task_running", r2["reason"])
            self.assertIn("还在后台查着呢", r2["text"])
            self.assertEqual(["g1"], started_groups)

            running_batch = Batch("g1", items=[Item("快点核实", "u1", "阿伟", True, "umo:1", time.time())])
            running_batch.running_tasks = brain_dedup._running_tasks_for_group("g1")
            built_ctx = brain_dedup.context_builder.build("g1", "快点核实", "阿伟", batch=running_batch)
            self.assertIn("当前本群已有后台任务正在运行", built_ctx)
            self.assertIn("绝不要再派重复的 subagent", built_ctx)

            # 跨群 g2 不受 g1 运行中任务阻断
            r_other = brain_dedup._dispatch_task("g2", "查上海天气", "umo:2")
            self.assertEqual("task", r_other["reason"])
        finally:
            release_gate.set()
            brain_dedup.wait_background(1.0)

        # 5) 第三层口头承诺兜底：模型回“我查一下...”却未派 subagent，自动转后台并拼接上文话题
        class EmptyPromiseModel:
            def reply(self, system, history, user_text):
                return '{"action":"reply","text":"这件事值得核实，我查一下近期有没有可信的证明。"}'

        dispatched_prompts = []
        store_ep = Store(":memory:")
        store_ep.add_message("g_ep", "u1", "老王", "user", "算了 你还是证明下黎曼猜想吧", ts=time.time() - 5)
        brain_ep = Brain(store_ep, EmptyPromiseModel())
        brain_ep.subagent = SubAgentManager()
        brain_ep.subagent.submit = lambda kind, prompt, runner, **kw: (dispatched_prompts.append(prompt) or "tid-ep")
        ep_batch = Batch(
            "g_ep",
            items=[Item("你再搜搜", "u1", "老王", True, "umo:ep", time.time())],
            started=time.time(),
            deadline=time.time(),
        )
        ep_res = brain_ep.handle_batch(ep_batch)
        self.assertEqual("task", ep_res["reason"])
        self.assertEqual(1, len(dispatched_prompts))
        self.assertIn("黎曼猜想", dispatched_prompts[0])
        self.assertIn("你再搜搜", dispatched_prompts[0])

        # 6) CodexHarness._build_env 过滤空字符串代理环境变量，保留有效代理值
        with tempfile.TemporaryDirectory() as tmpdir:
            harness = CodexHarness(home=tmpdir, workdir_root=tmpdir)
            with patch.dict(
                os.environ,
                {"HTTP_PROXY": "", "HTTPS_PROXY": "  ", "http_proxy": "http://127.0.0.1:7890", "NO_PROXY": "localhost"},
                clear=False,
            ):
                env = harness._build_env(Path(tmpdir), Path(tmpdir))
                self.assertNotIn("HTTP_PROXY", env)
                self.assertNotIn("HTTPS_PROXY", env)
                self.assertEqual("http://127.0.0.1:7890", env.get("http_proxy"))
                self.assertEqual("localhost", env.get("NO_PROXY"))

        # 7) 扩展记忆噪音过滤：无论是原结果还是转述文本命中失败特征，都不把“没法可靠对上 / 没有可用的绘图 / 目前没找到”写进 memory.sqlite3
        mem_noise = Memory(":memory:")

        class EchoResultModel:
            def reply(self, system, history, user_text):
                if "没有可用的绘图" in user_text:
                    return "可惜这边没有可用的绘图和发图接口，所以没能生成自画像。"
                return "目前没找到这条说法对应的论文，不能确认已证明。"

        brain_noise = Brain(Store(":memory:"), EchoResultModel(), memory=mem_noise)
        for tid, prompt, res_text in (
            ("tn1", "查找论文", "这个描述我暂时没法可靠对上具体论文，标题和作者还没核实。"),
            ("tn2", "画张自画像", "可惜这边没有可用的绘图和发图接口，所以没能生成自画像。"),
            ("tn3", "核实 AI 证明猜想", "目前没找到这条说法对应的论文，不能确认已证明。"),
        ):
            brain_noise._build_delivery_row(
                {"id": tid, "prompt": prompt, "status": "completed", "result": res_text, "files": []},
                "g_noise",
                "umo:noise",
            )
        self.assertEqual([], mem_noise.recall("g_noise", "论文 绘图 猜想"))


class TestExplicitMentionAndActions(unittest.TestCase):
    def test_explicit_mention_forbids_silent_and_wait_forces_reply(self):
        class StubbornSilentThenReply:
            def __init__(self):
                self.calls = 0

            def reply(self, system, history, user_text):
                self.calls += 1
                if self.calls == 1:
                    return '{"action":"silent"}'
                return '{"action":"reply","text":"hi～我在呢"}'

        model = StubbornSilentThenReply()
        brain = Brain(Store(":memory:"), model)
        batch = Batch(
            "g1",
            items=[Item("hi～", "u1", "阿伟", True, "umo:1", time.time())],
            started=time.time(),
            deadline=time.time(),
        )
        res = brain.handle_batch(batch)
        self.assertEqual(2, model.calls)
        self.assertTrue(res["replied"])
        self.assertEqual("hi～我在呢", res["text"])
        self.assertEqual(2, res["rounds"])

        ok, msg = validate("silent", "", capabilities={"silent": False, "wait": False})
        self.assertFalse(ok)
        self.assertIn("明确点到", msg)
        act, txt = judge("reply", "在呢", ["在呢"], None, allow_silent=False)
        self.assertEqual(("reply", "在呢"), (act, txt))

    def test_actions_wait_read_result_and_validate_fixes(self):
        parsed_wait = parse_full('{"action":"wait","seconds":5,"text":"占位"}')
        self.assertEqual("5", parsed_wait["text"])
        self.assertTrue(validate("read_result", "")[0])
        self.assertFalse(validate("read_result", "foo")[0] is False and False)
        rs = ToolResultStore()
        rs.add("g1", "recall", "记忆结果X" * 80)
        rs.add("g1", "note", "笔记Y")
        self.assertGreater(len(rs.get("g1", "recall")), 300)
        self.assertIn("记忆结果X", rs.get("g1", "recall"))

        class BadThenWaitTwice:
            def __init__(self):
                self.n = 0

            def reply(self, system, history, user_text):
                self.n += 1
                if self.n == 1:
                    return '{"action":"send_face","text":"开心"}'
                if self.n == 2:
                    return '{"action":"wait","seconds":2}'
                return '{"action":"reply","text":"上一句"}'

        loop = ToolLoop(BadThenWaitTwice(), max_rounds=4)
        res = loop.run(
            "sys",
            [{"role": "bot", "text": "别的回复"}],
            "问题",
            lambda g, a, t: (False, ""),
            "g1",
            capabilities={"send_face": False, "wait": True},
        )
        self.assertEqual("wait", res["action"])

    def test_multi_item_repeat_and_persisted_tail_dedup_and_direct_task_scope(self):
        seen = []

        class InspectModel:
            def reply(self, system, history, user_text):
                seen.append((history, user_text))
                return '{"action":"reply","text":"收到"}'

        store = Store(":memory:")
        brain = Brain(store, InspectModel())
        now = time.time()
        # 先让 u2 触发一次重复记录
        b_u2 = Batch("g1", items=[Item("666", "u2", "路人", False, "umo:1", now)], started=now, deadline=now)
        brain.handle_batch(b_u2)
        # 同一批里 u1 @bot 提问 + u2 紧跟重复 666 -> 不得吞掉 u1 的 @bot
        b_mix = Batch(
            "g1",
            items=[
                Item("大肥鱼怎么看", "u1", "阿伟", True, "umo:1", now + 1),
                Item("666", "u2", "路人", False, "umo:1", now + 2),
            ],
            started=now + 1,
            deadline=now + 2,
        )
        res_mix = brain.handle_batch(b_mix)
        self.assertTrue(res_mix["replied"])

        # persisted=True 重放 wait 批时，history 尾部不得重复出现本批消息
        replay = Batch(
            "g1",
            items=[Item("大肥鱼怎么看", "u1", "阿伟", True, "umo:1", now + 1),
                   Item("666", "u2", "路人", False, "umo:1", now + 2)],
            started=now + 5,
            deadline=now + 5,
            persisted=True,
        )
        seen.clear()
        brain.handle_batch(replay)
        hist_texts = [h["text"] for h in seen[0][0]]
        self.assertNotIn("大肥鱼怎么看", hist_texts[-2:])


class TestModelsHarnessAndSubagentFixes(unittest.TestCase):
    def test_build_model_env_override_and_reasoning_content(self):
        self.assertEqual("思考结论", _extract_message_text({"content": "", "reasoning_content": "思考结论"}))
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("BOT_API_BASE", None)
            os.environ.pop("BOT_MODEL", None)
            m = build_model({"api_base": DEFAULT_API_BASE, "api_key": "", "model": "gpt-4o-mini"})
            self.assertIsInstance(m, MockModel)
        with patch.dict(os.environ, {"BOT_API_BASE": "http://127.0.0.1:8787/v1", "BOT_MODEL": "gpt-6-luna"}):
            key, base, model = resolve_model_config({"api_base": DEFAULT_API_BASE, "api_key": "", "model": "gpt-4o-mini"})
            self.assertEqual(("http://127.0.0.1:8787/v1", "gpt-6-luna"), (base, model))
            self.assertIsInstance(build_model({"api_base": DEFAULT_API_BASE, "api_key": ""}), OpenAIModel)

    def test_harness_error_status_config_strip_and_outbox_preserved(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            binary = root / "codex"
            binary.write_text("", encoding="utf-8")
            outbox_root = root / "outbox"
            stale_pending = outbox_root / "task-other" / "keep.md"
            stale_pending.parent.mkdir(parents=True)
            stale_pending.write_text("important", encoding="utf-8")
            os.utime(stale_pending.parent, (100, 100))

            h = CodexHarness(
                binary=str(binary),
                home=str(root / "home"),
                workdir_root=str(root / "work"),
                outbox_root=str(outbox_root),
                api_base="http://127.0.0.1:11434/v1/chat/completions",
                api_key="",
                model="qwen2.5",
            )
            cfg_text = h._config_text()
            self.assertIn("http://127.0.0.1:11434/v1", cfg_text)
            self.assertNotIn("/chat/completions", cfg_text)
            self.assertIn("requires_openai_auth = false", cfg_text)
            self.assertNotIn("env_key", cfg_text)

            class FailProc:
                pid = 777777
                returncode = 2
                stdout = io.StringIO(
                    json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "half"}}) + "\n"
                )
                stderr = io.StringIO("fatal provider error\n")

                def wait(self, timeout=None):
                    return 2

            def fake_popen(cmd, **kw):
                (Path(kw["cwd"]) / "outbox" / "report.md").write_text("# partial", encoding="utf-8")
                return FailProc()

            with patch("subprocess.Popen", side_effect=fake_popen):
                res = h.run_with_files("g1", "task")
            self.assertEqual("error", res["status"])
            self.assertEqual([], res["files"])
            self.assertEqual("half", res["partial"])
            self.assertIn("exit 2", res["error"])
            self.assertIn("fatal provider error", res["error"])
            # 另一任务待投递 outbox 文件未被误清理
            self.assertTrue(stale_pending.exists())

    def test_subagent_on_register_and_delivery_dedup(self):
        mgr = SubAgentManager()
        order = []

        def on_reg(tid):
            order.append(("reg", tid))

        def on_comp(payload):
            # 回调执行瞬间，任务尚未对外暴露 completed 态
            snap = mgr.get(payload["id"])
            order.append(("comp_status", snap["status"]))

        tid = mgr.submit("chat", "hi", lambda p: "done", on_complete=on_comp, on_register=on_reg)
        deadline = time.time() + 2
        while mgr.running() and time.time() < deadline:
            time.sleep(0.01)
        self.assertEqual(("reg", tid), order[0])
        self.assertEqual(("comp_status", "running"), order[1])

        brain = Brain(Store(":memory:"), MockModel())
        with brain._delivery_lock:
            self.assertTrue(brain._mark_delivered_locked("tid-dup"))
            self.assertFalse(brain._mark_delivered_locked("tid-dup"))


class TestScrubbingDisplayRhythmVisionAndReminders(unittest.TestCase):
    def test_scrubbing_unsafe_display_rhythm_and_vision(self):
        code = "代码：\n    def f():\n        return 1\n"
        self.assertEqual("代码：\n    def f():\n        return 1", _scrub_internal_leaks(code))
        self.assertIn("https://example.com/home/about", _scrub_internal_leaks("看 https://example.com/home/about"))
        self.assertIn("110.242.68.66", _scrub_internal_leaks("公网 110.242.68.66"))
        self.assertEqual("密码：", _scrub_internal_leaks("密码：sk-abcdefghijklmnopqrstuvwxyz"))
        self.assertEqual("访问", _scrub_internal_leaks("访问localhost:8080/api"))
        self.assertEqual("", _unwrap_reply_text('看这里 {"action":"reply","text":"未闭合'))
        self.assertTrue(is_unsafe("请帮我sudo rm -rf /tmp"))

        self.assertEqual("公式E=mc^2很简单", display.clean("公式$E=mc^2$很简单"))
        self.assertEqual("echo hello", display.clean("```echo hello```"))

        rhythm = Rhythm(preset="natural", view_rate=0.0)
        t0 = 1000.0
        rhythm.add("g1", Item("第一句", "u1", "甲", False, "u", t0), now=t0)
        d1 = rhythm._batches["g1"].deadline
        rhythm.add("g1", Item("第二句", "u2", "乙", False, "u", t0 + 2.0), now=t0 + 2.0)
        d2 = rhythm._batches["g1"].deadline
        self.assertGreaterEqual(d2, d1)
        rhythm.add("g_img", Item("看图", "u1", "甲", False, "u", t0, kind="image"), now=t0)
        rhythm.add("g_img", Item("补充一句", "u2", "乙", False, "u", t0 + 0.1), now=t0 + 0.1)
        self.assertGreaterEqual(rhythm._batches["g_img"].deadline, t0 + 4.0)

        # wait_due 对 @ 批跳过 0.2 抽签直接放行；use_round 按实际 rounds 累计
        rhythm.add("gm", Item("@bot 等会儿", "u1", "甲", True, "u", t0), now=t0)
        rhythm.due(t0 + 10)
        rhythm.wait("gm", 5, now=t0 + 10)
        self.assertTrue(rhythm.wait_due("gm", now=t0 + 20))
        q = rhythm.start_turn("gm")
        rhythm.use_round("gm", q, count=q + 1)
        self.assertGreaterEqual(rhythm.debt("gm"), 1)

        with tempfile.TemporaryDirectory() as folder:
            bank = vision.EmojiBank(folder)
            self.assertEqual("doge", bank.collect_face("g1", "179", ""))
            # 先塞 10 个 QQ Face，再存 1 张 jpg 表情图，确保不会被 Face 饿死
            for fid in range(1, 11):
                bank.collect_face("g1", str(fid), f"情绪{fid}")

            class M:
                def vision_reply(self, text, url):
                    return "无语|猫翻白眼"

            jpeg_bytes = b"\xff\xd8\xff\xe0" + b"0" * 32
            tag = bank.collect("g1", jpeg_bytes, M())
            self.assertEqual("无语", tag)
            picked = bank.pick("g1", "无语")
            self.assertIsInstance(picked, Path)
            self.assertEqual(".jpg", picked.suffix)

    def test_reminders_chinese_cancel_todo_peek_confirm_and_schedule_validation(self):
        store = Store(":memory:")
        sched = Schedule(":memory:")
        brain = Brain(store, MockModel(), schedule=sched)
        msg = Message("g1", "u1", "阿伟", "", True, "umo:1")

        self.assertIn("5分钟后", brain._command(msg, "/提醒 5分钟后 喝水"))
        self.assertEqual(FORMAT_HINT, brain._command(msg, "/提醒 5分钟后"))
        self.assertIn("1小时30分钟后", brain._command(msg, "/提醒 1小时30分 开会"))
        self.assertIn("30分钟后", brain._command(msg, "/提醒 半小时后 拿外卖"))
        self.assertIn("15:00", brain._command(msg, "/提醒 明天下午3点 取快递"))

        todo = brain._command(msg, "/待办")
        self.assertIn("1. 阿伟：喝水", todo)
        self.assertIn("2. 阿伟：拿外卖", todo)

        cancel_res = brain._command(msg, "/取消提醒 1")
        self.assertIn("已取消提醒：喝水", cancel_res)
        self.assertNotIn("喝水", brain._command(msg, "/待办"))

        # 先 peek 后 confirm：失败 backoff 保留；成功 confirm 提交并写入群聊历史
        due_id = store.add_reminder("g1", "u1", "阿伟", "发消息测试", time.time() - 5, "umo:1")
        due_list = brain.peek_due_reminders(time.time())
        self.assertTrue(any(x["_id"] == due_id for x in due_list))
        target = next(x for x in due_list if x["_id"] == due_id)
        brain.backoff_reminder(target, seconds=60, now=time.time())
        self.assertEqual([], [x for x in brain.peek_due_reminders(time.time()) if x["_id"] == due_id])
        brain.confirm_reminder(target, now=time.time())
        self.assertEqual([], [r for r in store.pending_reminders("g1") if r["id"] == due_id])
        self.assertTrue(any("发消息测试" in m["text"] for m in store.recent("g1", 10)))

        with self.assertRaises(ValueError):
            sched.add("g1", "坏规则", "daily", "99:99")


class TestMemoryPersonsContextMonitorAttentionAndInfra(unittest.TestCase):
    def test_memory_persons_context_prompts_attention_monitor_and_closed_db(self):
        mem = Memory(":memory:")
        self.assertIn("jargon", MEMORY_PROMPT)
        # points_au 窄触发器只监听 content
        row = mem._db.execute("SELECT sql FROM sqlite_master WHERE type='trigger' AND name='points_au'").fetchone()
        self.assertIn("OF content", row["sql"])

        # protect 点不得塞满 LIMIT 阻塞未保护点淘汰
        for i in range(405):
            mem.add_point("g_prune", "event", f"保护点{i}", now=1000.0 + i)
        rows = mem._db.execute("SELECT id FROM memory_points WHERE group_id='g_prune'").fetchall()
        for r in rows:
            mem.maintain_point("g_prune", r["id"], "protect")
        mem.add_point("g_prune", "event", "低权重普通点", now=100.0)
        mem.prune("g_prune", now=500.0)
        low_state = mem._db.execute(
            "SELECT state FROM memory_points WHERE group_id='g_prune' AND content='低权重普通点'"
        ).fetchone()["state"]
        self.assertEqual("forgotten", low_state)

        mem.add_jargon("g1", "哈吉米", "Gemini")
        mem.add_relation("g1", "阿伟", "小美", "搭档")

        ps = PersonStore(":memory:")
        ps.observe("g1", "u1", "阿伟 [机器人]")
        self.assertEqual("u1", ps.resolve("g1", "阿伟 (机器人)"))
        ps.upsert_profile("g1", "u1", traits="第一句；第二句；第三句")
        merged_val = _merge_field("旧1；旧2", "新来的这一段", limit=14)
        self.assertIn("新来的这一段", merged_val)

        store = Store(":memory:")
        store.add_reminder("g1", "u1", "阿伟", "直播提醒", time.time() + 120, "umo:1")
        ctx = ContextBuilder("人设", memory=mem, persons=ps, store=store)
        built = ctx.build(
            "g1",
            "聊聊最近",
            "阿伟",
            Batch("g1", items=[Item("hi", "u1", "阿伟", True, "umo:1", time.time(), quote="上一句")]),
        )
        self.assertIn("哈吉米=Gemini", built)
        self.assertIn("阿伟 与 小美：搭档", built)
        self.assertIn("直播提醒", built)
        # 必须回应指令不得包进 <background_data>，而被引用原文必须包进 <background_data>
        import re
        outside_bg = re.sub(r"<background_data>.*?</background_data>", "", built, flags=re.S)
        self.assertIn("这次有人明确点到了你，必须回应", outside_bg)
        self.assertNotIn("上一句", outside_bg)
        self.assertNotIn("哈吉米=Gemini", outside_bg)

        with tempfile.TemporaryDirectory() as folder:
            pstore = prompts.PromptStore(Path(folder) / "prompts")
            pstore.set("persona", "全局人设")
            desc = {r["name"]: r for r in pstore.describe("g1")}
            self.assertEqual("全局覆盖", desc["persona"]["source"])

            lib = prompts.PersonaLibrary(
                Path(folder) / "personas",
                builtin=prompts.Persona(id="default", name="默认", personality="内置"),
            )
            lib.save(prompts.Persona(id="pirate", name="海盗", personality="群启用海盗"))
            lib.activate("g1", "pirate")
            b_persona = Brain(Store(":memory:"), MockModel(), prompt_store=pstore, personas=lib)
            self.assertIn("群启用海盗", b_persona._resolve_persona("g1"))
            self.assertEqual("全局人设", b_persona._resolve_persona("g2"))
            pstore.set("persona", "本群专属人设", "g1")
            self.assertEqual("本群专属人设", b_persona._resolve_persona("g1"))

        self.assertEqual("active", attention.drift_level(0.9, budget=1))
        att = attention.AttentionTracker()
        self.assertEqual("off", att.level("empty-group", "全新问题"))

        mon = Monitor(":memory:")
        for val in range(1, 21):
            mon.record("g1", "reply", ok=True, latency=float(val))
        self.assertEqual(19.0, mon.stats("g1")["p95_latency"])

        for db_obj in (store, mem, ps, mon, Schedule(":memory:")):
            db_obj.close()
        self.assertEqual([], store.recent("g1"))
        self.assertEqual([], mem.recall("g1", "x"))
        self.assertIsNone(ps.resolve("g1", "阿伟"))
        self.assertEqual(0, mon.prune())

    def test_bootstrap_tar_symlink_rejection_and_napcat_cache_cleanup(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            data_root = root / "data"
            old_ts = 100.0
            log_file = data_root / "napcat" / "logs" / "old.log"
            cache_file = data_root / "napcat" / "cache" / "old.bin"
            log_file.parent.mkdir(parents=True)
            cache_file.parent.mkdir(parents=True)
            log_file.write_bytes(b"x")
            cache_file.write_bytes(b"y")
            os.utime(log_file, (old_ts, old_ts))
            os.utime(cache_file, (old_ts, old_ts))
            removed = cleanup_runtime_caches(str(data_root), now=100.0 + 8 * 86400)
            self.assertEqual(2, removed)

            sym_tar = root / "sym.tgz"
            with tarfile.open(sym_tar, "w:gz") as tf:
                meta = tarfile.TarInfo("pkg/metadata.yaml")
                data = b"name: x\n"
                meta.size = len(data)
                tf.addfile(meta, io.BytesIO(data))
                link = tarfile.TarInfo("pkg/sub/evil")
                link.type = tarfile.SYMTYPE
                link.linkname = "../target"
                tf.addfile(link)
            boot_text = (ROOT / "docker" / "bootstrap.sh").read_text(encoding="utf-8")
            verify_block = boot_text.split("# --- bootstrap main ---", 1)[0]
            sym_res = subprocess.run(
                ["bash", "-c", verify_block + f'\nverify_artifact "{sym_tar}" "" "tar_plugin"'],
                capture_output=True, text=True,
            )
            self.assertNotEqual(0, sym_res.returncode)
            self.assertIn("m.issym() or m.islnk()", boot_text)
            self.assertIn("empty_mention_waiting", boot_text)
            self.assertIn("NAPCAT_WEBUI_TOKEN", boot_text)
            up_text = (ROOT / "scripts" / "up.sh").read_text(encoding="utf-8")
            self.assertLess(up_text.index("for _k in BASE_IMAGE"), up_text.index('if [[ "$mode_cn" -eq 1 ]]'))

    def test_model_timeout_90s_and_subagent_blocked_without_explicit_mention(self):
        # 1) 默认 HTTP 超时放宽到 90s，reply 与 vision_reply 均使用 timeout=90
        self.assertEqual(90, DEFAULT_MODEL_TIMEOUT)
        oa = OpenAIModel("k", "http://localhost/v1", "m")
        self.assertEqual(90, oa.timeout)
        seen_timeouts = []

        class _FakeResp:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return json.dumps({"choices": [{"message": {"content": "收到"}}]}).encode("utf-8")

        def _fake_urlopen(req, timeout=None):
            seen_timeouts.append(timeout)
            return _FakeResp()

        with patch("urllib.request.urlopen", side_effect=_fake_urlopen):
            self.assertEqual("收到", oa.reply("sys", [], "hi"))
            self.assertEqual("收到", oa.vision_reply("看图", "data:image/png;base64,AAAA"))
        self.assertEqual([90, 90], seen_timeouts)

        # 2) 未 @ 群聊问句：不暴露 subagent 动作；即使模型硬输出 subagent / 不知道 / 我查一下 / 查不了，也绝不启动后台工具
        now = time.time()
        for reply_text in (
            '{"action":"subagent","text":"查一下实验室还是小和的"}',
            "这个我不知道",
            "我查一下再告诉你",
            "我这边查不了实时内容",
        ):
            seen_systems = []
            submitted = []

            class _PassiveModel:
                def reply(self, system, history, user_text):
                    seen_systems.append(system)
                    return reply_text

            brain = Brain(Store(":memory:"), _PassiveModel())
            brain.subagent = SubAgentManager()
            brain.subagent.submit = lambda kind, prompt, runner, **kw: (submitted.append(prompt) or "tid-no")
            batch = Batch(
                "g1",
                items=[Item("实验室，还是小和的？", "u1", "神之殒", False, "umo:1", now)],
                started=now,
                deadline=now,
            )
            res = brain.handle_batch(batch)
            self.assertNotIn("- subagent：", seen_systems[0])
            self.assertIn("只是旁听时绝不要用 subagent", seen_systems[0])
            self.assertNotEqual("task", res["reason"])
            self.assertEqual([], submitted)
            if reply_text.startswith("{"):
                self.assertGreater(len(seen_systems), 1)
                self.assertIn("[校验失败] 没有明确 @ 或点名你，不能启动后台工具", seen_systems[1])
                self.assertEqual("silent", res["reason"])

        # 3) 转述提及（retold=True）即使带 @/自称，也不允许启动 subagent
        submitted_retold = []

        class _RetoldModel:
            def reply(self, system, history, user_text):
                return "这个我不知道"

        brain_retold = Brain(Store(":memory:"), _RetoldModel())
        brain_retold.subagent = SubAgentManager()
        brain_retold.subagent.submit = lambda kind, prompt, runner, **kw: (submitted_retold.append(prompt) or "tid-retold")
        retold_batch = Batch(
            "g1",
            items=[Item("他们说你大肥鱼知道吗？", "u1", "阿伟", True, "umo:1", now)],
            started=now,
            deadline=now,
        )
        retold_res = brain_retold.handle_batch(retold_batch)
        self.assertTrue(retold_batch.mention_info.retold)
        self.assertNotEqual("task", retold_res["reason"])
        self.assertEqual([], submitted_retold)

        # 4) 群聊明确 @ 且非转述，以及私聊（scene="private"）仍允许启动 subagent
        submitted_ok = []

        class _SubModel:
            def reply(self, system, history, user_text):
                return '{"action":"subagent","text":"查一下苏州今天天气"}'

        brain_ok = Brain(Store(":memory:"), _SubModel())
        brain_ok.subagent = SubAgentManager()
        brain_ok.subagent.submit = lambda kind, prompt, runner, **kw: (submitted_ok.append(prompt) or "tid-ok")
        group_at_res = brain_ok.handle_batch(Batch(
            "g1",
            items=[Item("苏州今天几度？", "u1", "阿伟", True, "umo:1", now)],
            started=now,
            deadline=now,
        ))
        self.assertEqual("task", group_at_res["reason"])
        self.assertEqual(1, len(submitted_ok))

        priv_res = brain_ok.handle_batch(Batch(
            "p1",
            items=[Item("帮我查查苏州今天几度", "u1", "阿伟", False, "umo:p1", now)],
            started=now,
            deadline=now,
            scene="private",
        ))
        self.assertEqual("task", priv_res["reason"])
        self.assertEqual(2, len(submitted_ok))

    def test_sticker_fail_closed_raw_meta_quoted_exclusion_and_vlm_non_emoji(self):
        import struct
        from unittest.mock import MagicMock

        stub_mods, comp, _ = _make_astrbot_stubs()
        sys.modules.pop("astrbot_plugin_echo.main", None)
        with patch.dict(sys.modules, stub_mods):
            echo_main = importlib.import_module("astrbot_plugin_echo.main")

        png_small = (
            b"\x89PNG\r\n\x1a\n"
            + struct.pack(">II", 120, 90)
            + b"\x08\x06\x00\x00\x00"
            + b"0" * 64
        )
        jpg_small = b"\xff\xd8\xff\xe0" + b"JFIF" + b"0" * 64
        gif_small = b"GIF89a" + b"\x10\x00\x10\x00\x80\x00\x00" + b"0" * 64

        class _StrippedImage(comp.Image):
            """模拟真实 AstrBot Image：只保留 url/file/path，丢弃 sub_type/summary。"""

            def __init__(self, url="", file="", path=""):
                self.file = file
                self.url = url
                self.path = path

        stripped = _StrippedImage(url="https://cdn.example.com/a.png")
        # 1) _is_sticker_image 单测
        self.assertFalse(echo_main._is_sticker_image(stripped, png_small, {"sub_type": 0}))
        self.assertFalse(echo_main._is_sticker_image(stripped, jpg_small, {"sub_type": 0}))
        self.assertFalse(echo_main._is_sticker_image(stripped, gif_small, {"sub_type": 0}))
        # 无元数据：静态小 PNG / JPG 截图一律拒收，<=500KB GIF 动图放行
        self.assertFalse(echo_main._is_sticker_image(stripped, png_small))
        self.assertFalse(echo_main._is_sticker_image(stripped, jpg_small))
        self.assertTrue(echo_main._is_sticker_image(stripped, gif_small))
        # raw_meta 明确标注表情包：即使 Image 组件本身没属性也放行
        self.assertTrue(echo_main._is_sticker_image(stripped, png_small, {"sub_type": 1, "summary": "[动画表情]"}))
        self.assertTrue(echo_main._is_sticker_image(stripped, jpg_small, {"sub_type": "2"}))
        self.assertFalse(echo_main._is_sticker_image(stripped, png_small, {"summary": "[图片]"}))

        # 2) on_group_message 集成：stripped Image + raw_message / 引用图绝不进 emoji_bank
        class _FakeContext:
            async def send_message(self, umo, chain):
                pass

        class _FakeModel:
            def reply(self, system, history, user_text):
                return '{"action":"reply","text":"收到"}'

        class _FakeEvent:
            def __init__(self, gid, text, parts, at_or_wake=False, raw_message=None):
                self.gid = gid
                self.text = text
                self.parts = list(parts)
                self._at = at_or_wake
                self.unified_msg_origin = f"aiocqhttp:GroupMessage:{gid}"
                self.bot = None
                self.message_obj = types.SimpleNamespace(
                    raw_message=raw_message if raw_message is not None else {"post_type": "message"},
                    message_str=text,
                    message=self.parts,
                )

            def get_sender_id(self):
                return "1001"

            def get_sender_name(self):
                return "阿伟"

            def get_self_id(self):
                return "999999"

            def get_group_id(self):
                return self.gid

            def get_platform_name(self):
                return "aiocqhttp"

            def get_messages(self):
                return self.parts

            def get_message_str(self):
                return self.text

            def is_at_or_wake_command(self):
                return self._at

            def should_call_llm(self, flag):
                return None

            def stop_event(self):
                return None

        async def _run_integration():
            with tempfile.TemporaryDirectory() as tmpdir:
                with patch.dict(sys.modules, stub_mods):
                    with patch.object(echo_main.StarTools, "get_data_dir", lambda *a, **kw: tmpdir):
                        echo = echo_main.Echo(_FakeContext(), config={})
                    fake_m = _FakeModel()
                    echo.model = fake_m
                    echo.brain.model = fake_m
                    echo.brain.tool_loop.model = fake_m
                    for tname in ("_reminder_task", "_chat_task"):
                        task = getattr(echo, tname, None)
                        if task is not None:
                            task.cancel()
                            setattr(echo, tname, None)

                    collect_calls = []

                    def _spy_collect(gid, data, model, limit=None):
                        collect_calls.append((gid, data))
                        return "呆萌"

                    with patch.object(echo, "_download", new=MagicMock(return_value=png_small)), patch.object(
                        echo.emoji_bank, "collect", side_effect=_spy_collect
                    ):
                        # 2a) 普通截图 raw_message sub_type=0 -> 挂到 rhythm，但不收进 emoji_bank
                        ev_screen = _FakeEvent(
                            "1093318711",
                            "",
                            [_StrippedImage(url="https://cdn.example.com/screen.png")],
                            raw_message={
                                "message": [
                                    {
                                        "type": "image",
                                        "data": {
                                            "url": "https://cdn.example.com/screen.png",
                                            "sub_type": 0,
                                            "summary": "",
                                        },
                                    }
                                ]
                            },
                        )
                        async for _ in echo.on_group_message(ev_screen):
                            pass
                        await asyncio.gather(*list(echo._bg_tasks), return_exceptions=True)
                        self.assertEqual([], collect_calls)
                        self.assertTrue(echo.rhythm._images.get("aiocqhttp:1093318711"))
                        echo.rhythm._images.clear()

                        # 2b) 群友发的真表情包 raw_message sub_type=1 -> 既挂图又收进 emoji_bank
                        ev_sticker = _FakeEvent(
                            "1093318711",
                            "",
                            [_StrippedImage(url="https://cdn.example.com/sticker.png")],
                            raw_message={
                                "message": [
                                    {
                                        "type": "image",
                                        "data": {
                                            "url": "https://cdn.example.com/sticker.png",
                                            "sub_type": 1,
                                            "summary": "[动画表情]",
                                        },
                                    }
                                ]
                            },
                        )
                        async for _ in echo.on_group_message(ev_sticker):
                            pass
                        await asyncio.gather(*list(echo._bg_tasks), return_exceptions=True)
                        self.assertEqual(1, len(collect_calls))
                        echo.rhythm._images.clear()
                        collect_calls.clear()

                        # 2c) 引用消息中的图片（即使带 subType=1）只供看图，绝不入表情库
                        ev_quote = _FakeEvent(
                            "1093318711",
                            "看下引用的图",
                            [
                                comp.Reply(
                                    sender_id="1002",
                                    sender_nickname="神之殒",
                                    chain=[comp.Image(url="https://cdn.example.com/q.png", subType=1)],
                                ),
                                comp.At("999999"),
                                comp.Plain(" 看下引用的图"),
                            ],
                            at_or_wake=True,
                        )
                        async for _ in echo.on_group_message(ev_quote):
                            pass
                        await asyncio.gather(*list(echo._bg_tasks), return_exceptions=True)
                        self.assertEqual([], collect_calls)
                        self.assertTrue(echo.rhythm._images.get("aiocqhttp:1093318711"))

        asyncio.run(_run_integration())

        # 3) vision.EmojiBank：VLM 返回“非表情”拦截并缓存 ok:False；失效文件条目自动从 index.json 清理
        self.assertFalse(vision.acceptable("非表情"))
        self.assertFalse(vision.acceptable("非表情|普通网页截图"))

        class _NonEmojiVisionModel:
            def __init__(self, reply_text):
                self.calls = 0
                self.reply_text = reply_text

            def vision_reply(self, prompt, image_uri):
                self.calls += 1
                return self.reply_text

        with tempfile.TemporaryDirectory() as folder:
            bank = vision.EmojiBank(folder)
            m_non = _NonEmojiVisionModel("非表情")
            self.assertEqual("", bank.collect("g1", png_small, m_non))
            self.assertEqual("", bank.collect("g1", png_small, m_non))
            self.assertEqual(1, m_non.calls)
            self.assertEqual(0, bank.stats("g1").get("g1", 0))

            m_non2 = _NonEmojiVisionModel("非表情|一张聊天截图")
            self.assertEqual("", bank.collect("g1", jpg_small, m_non2))
            self.assertEqual(1, m_non2.calls)
            self.assertEqual(0, bank.stats("g1").get("g1", 0))

            # 正常表情入库后，若磁盘文件被删，重载时自动清掉失效索引
            m_ok = _NonEmojiVisionModel("无语|猫翻白眼")
            self.assertEqual("无语", bank.collect("g1", gif_small, m_ok))
            self.assertEqual(1, bank.stats("g1").get("g1", 0))
            saved_path = Path(bank._index["g1"][0]["path"])
            saved_path.unlink()
            reloaded = vision.EmojiBank(folder)
            self.assertEqual(0, reloaded.stats("g1").get("g1", 0))
            self.assertEqual([], reloaded._index.get("g1", []))

    def test_reply_attach_emoji_mood_hints_cooldown_and_anti_repeat(self):
        import struct
        from astrbot_plugin_echo.core.actions import ACTION_PROMPT

        # 1) actions.parse_full / validate / ACTION_PROMPT
        self.assertIn("send_face", ACTION_PROMPT)
        self.assertIn('"emoji"', ACTION_PROMPT)
        p1 = parse_full('{"action":"reply","text":"笑死","emoji":"无语"}')
        self.assertEqual("无语", p1["emoji"])
        p2 = parse_full('{"action":"reply","text":"离谱","attach_emoji":"呆萌"}')
        self.assertEqual("呆萌", p2["emoji"])
        self.assertFalse(validate("reply", "你好", emoji="坏\x00词")[0])

        class _StaticTagModel:
            def __init__(self, tag, desc):
                self.tag = tag
                self.desc = desc

            def vision_reply(self, prompt, uri):
                return f"{self.tag}|{self.desc}"

        png1 = b"\x89PNG\r\n\x1a\n" + struct.pack(">II", 32, 32) + b"\x08\x06\x00\x00\x00" + b"A" * 32
        png2 = b"\x89PNG\r\n\x1a\n" + struct.pack(">II", 32, 32) + b"\x08\x06\x00\x00\x00" + b"B" * 32
        png3 = b"\x89PNG\r\n\x1a\n" + struct.pack(">II", 32, 32) + b"\x08\x06\x00\x00\x00" + b"C" * 32

        with tempfile.TemporaryDirectory() as folder:
            bank = vision.EmojiBank(folder)
            bank.collect("g1", png1, _StaticTagModel("无语", "小猫翻白眼"))
            bank.collect("g1", png2, _StaticTagModel("无语", "小狗无语凝视"))
            bank.collect("g1", png3, _StaticTagModel("呆呆", "企鹅呆呆地望着你，语气平静"))
            bank.collect_face("g1", "178", "斜眼笑")

            tags = bank.tags("g1", limit=10)
            self.assertEqual("斜眼笑", tags[0])
            self.assertIn("无语", tags)
            self.assertIn("呆呆", tags)

            # 精确匹配“无语”应压过仅含单字“语”的低分项；且连续两次不会挑同一张图
            pick1 = bank.pick("g1", "无语", None)
            pick2 = bank.pick("g1", "无语", None)
            self.assertIn("无语", next(e["tag"] for e in bank._index["g1"] if e.get("path") == str(pick1)))
            self.assertIn("无语", next(e["tag"] for e in bank._index["g1"] if e.get("path") == str(pick2)))
            self.assertNotEqual(str(pick1), str(pick2))

            # 2) Brain.handle_batch 注入本群可用情绪 + reply 附带表情 + 90s 冷却 + 私聊不附带
            seen_systems = []

            class _EmojiAttachModel:
                def __init__(self):
                    self.n = 0

                def reply(self, system, history, user_text):
                    seen_systems.append(system)
                    self.n += 1
                    return json.dumps(
                        {"action": "reply", "text": f"笑死这也太离谱了{self.n}", "emoji": "斜眼笑"},
                        ensure_ascii=False,
                    )

            mon = Monitor(":memory:")
            brain = Brain(Store(":memory:"), _EmojiAttachModel(), emoji_bank=bank, monitor=mon)
            now = time.time()
            r1 = brain.handle_batch(Batch(
                "g1",
                items=[Item("哈哈哈哈你看他", "u1", "阿伟", True, "umo:1", now)],
                started=now,
                deadline=now,
            ))
            self.assertIn("本群表情库可用情绪", seen_systems[0])
            self.assertIn("无语", seen_systems[0])
            self.assertTrue(r1["replied"])
            self.assertEqual("178", r1.get("attach_face"))
            mon_rows = mon._db.execute("SELECT kind, detail FROM events WHERE group_id='g1'").fetchall()
            self.assertTrue(any(e["kind"] == "send_face" and e["detail"] == "attach:斜眼笑" for e in mon_rows))

            # 90s 冷却内连续回复：文本正常发，附带表情被克制掉
            r2 = brain.handle_batch(Batch(
                "g1",
                items=[Item("再来一句", "u1", "阿伟", True, "umo:1", now + 5)],
                started=now + 5,
                deadline=now + 5,
            ))
            self.assertTrue(r2["replied"])
            self.assertNotIn("attach_face", r2)
            self.assertNotIn("attach_image", r2)

            # 冷却过后（调早记录）再次允许附带图片表情
            brain._last_emoji_at["g1"] = now - 200
            brain.model = type("_ImgAttachM", (), {
                "reply": lambda self, s, h, u: '{"action":"reply","text":"真无语了","emoji":"无语"}'
            })()
            r3 = brain.handle_batch(Batch(
                "g1",
                items=[Item("你评价一下", "u1", "阿伟", True, "umo:1", now + 20)],
                started=now + 20,
                deadline=now + 20,
            ))
            self.assertTrue(r3["replied"])
            self.assertTrue(Path(r3.get("attach_image", "")).exists())

            # 私聊场景绝不附带表情
            brain._last_emoji_at.clear()
            priv = brain.handle_batch(Batch(
                "p1",
                items=[Item("在干嘛", "u1", "阿伟", False, "umo:p1", now + 30)],
                started=now + 30,
                deadline=now + 30,
                scene="private",
            ))
            self.assertTrue(priv["replied"])
            self.assertNotIn("attach_image", priv)
            self.assertNotIn("attach_face", priv)

            # 3) Echo._process_batch 先发文本回复、紧接着发出 attach_image / attach_face
            stub_mods, comp, _ = _make_astrbot_stubs()
            sys.modules.pop("astrbot_plugin_echo.main", None)
            with patch.dict(sys.modules, stub_mods):
                echo_main = importlib.import_module("astrbot_plugin_echo.main")

            sent_msgs = []

            class _SpyCtx:
                async def send_message(self, umo, chain):
                    sent_msgs.append((umo, chain.chain))

            async def _test_send():
                with patch.dict(sys.modules, stub_mods):
                    with patch.object(echo_main.StarTools, "get_data_dir", lambda *a, **kw: folder):
                        echo = echo_main.Echo(_SpyCtx(), config={})
                    for tname in ("_reminder_task", "_chat_task"):
                        task = getattr(echo, tname, None)
                        if task is not None:
                            task.cancel()
                            setattr(echo, tname, None)
                    echo.emoji_bank = bank
                    echo.brain.emoji_bank = bank
                    saved_img = str(Path(bank._index["g1"][0]["path"]))
                    fake_res_img = {
                        "replied": True, "text": "笑死", "parts": ["笑死"], "cleaned": True,
                        "attach_image": saved_img,
                    }
                    b1 = Batch("g1", items=[Item("hi", "u1", "阿伟", True, "umo:g1", now)], started=now, deadline=now, approved=True)
                    with patch.object(echo.brain, "handle_batch", return_value=fake_res_img):
                        await echo._process_batch(b1)
                    fake_res_face = {
                        "replied": True, "text": "不错", "parts": ["不错"], "cleaned": True,
                        "attach_face": "178",
                    }
                    b2 = Batch("g2", items=[Item("hi2", "u1", "阿伟", True, "umo:g2", now)], started=now, deadline=now, approved=True)
                    with patch.object(echo.brain, "handle_batch", return_value=fake_res_face):
                        await echo._process_batch(b2)

            asyncio.run(_test_send())
            self.assertEqual(4, len(sent_msgs))
            self.assertEqual("笑死", sent_msgs[0][1][0].text)
            self.assertTrue(sent_msgs[1][1][0].file.startswith("file://"))
            self.assertEqual("不错", sent_msgs[2][1][0].text)
            self.assertEqual(178, sent_msgs[3][1][0].id)

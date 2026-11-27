"""真实线上 10 类故障端到端回放测试：通过 AstrBot 协议桩完整驱动 Echo 外壳 + Core/Store/Adapters/Workers。"""

import asyncio
import json
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = ROOT / "plugin"
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

TINY_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
TINY_GIF = b"GIF89a\x01\x00\x01\x00\x00\x00\x00;"


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


class _MessageChain(list):
    pass


def _make_astrbot_stubs(data_dir: Path):
    astrbot_mod = types.ModuleType("astrbot")
    api_mod = types.ModuleType("astrbot.api")
    event_mod = types.ModuleType("astrbot.api.event")
    comp_mod = types.ModuleType("astrbot.api.message_components")
    star_mod = types.ModuleType("astrbot.api.star")

    logger = MagicMock()
    api_mod.logger = logger

    class _AstrMessageEvent:
        pass

    class _EventMessageType:
        GROUP_MESSAGE = "GROUP_MESSAGE"

    class _Filter:
        EventMessageType = _EventMessageType

        @staticmethod
        def on_astrbot_loaded():
            return lambda fn: fn

        @staticmethod
        def event_message_type(*args, **kwargs):
            return lambda fn: fn

    event_mod.AstrMessageEvent = _AstrMessageEvent
    event_mod.MessageChain = _MessageChain
    event_mod.filter = _Filter()

    comp_mod.At = _At
    comp_mod.Face = _Face
    comp_mod.File = _File
    comp_mod.Image = _Image
    comp_mod.Plain = _Plain
    comp_mod.Reply = _Reply

    class _Star:
        def __init__(self, context=None):
            self.context = context

    class _StarTools:
        @staticmethod
        def get_data_dir(*args):
            return str(data_dir)

    class _Context:
        pass

    star_mod.Star = _Star
    star_mod.StarTools = _StarTools
    star_mod.Context = _Context

    return {
        "astrbot": astrbot_mod,
        "astrbot.api": api_mod,
        "astrbot.api.event": event_mod,
        "astrbot.api.message_components": comp_mod,
        "astrbot.api.star": star_mod,
    }


def _make_event(parts, *, text="", raw_msg=None, gid="10086", uid="20001", name="小明", self_id="8888"):
    ev = MagicMock()
    ev.get_group_id.return_value = gid
    ev.get_platform_name.return_value = "aiocqhttp"
    ev.unified_msg_origin = f"aiocqhttp:GroupMessage:{gid}"
    ev.get_sender_id.return_value = uid
    ev.get_sender_name.return_value = name
    ev.get_self_id.return_value = self_id
    ev.get_message_str.return_value = text
    ev.get_messages.return_value = parts
    ev.message_obj = types.SimpleNamespace(raw_message=raw_msg, message_id=str((raw_msg or {}).get("message_id", "m_1")), message_str=text)
    ev.is_at_or_wake_command.return_value = False
    ev.plain_result = lambda t: _MessageChain([_Plain(t)])
    return ev


class _SpyLLM:
    def __init__(self, responses=None, vision_label="无语|熊猫头无语"):
        self.responses = list(responses or [])
        self.vision_label = vision_label
        self.chat_calls = []
        self.summary_calls = []
        self.vision_calls = 0

    def chat(self, messages, tools=(), timeout=None):
        sys_prompt = str(messages[0].content or "") if messages else ""
        if "记忆整理员" in sys_prompt:
            self.summary_calls.append(list(messages))
            from astrbot_plugin_echo.domain import ModelTurn
            return ModelTurn(text='{"episode":"","points":[],"persons":[],"state":{"topics":[],"progress":[],"pending":[]}}')
        self.chat_calls.append({
            "messages": list(messages),
            "tool_names": [t.name for t in tools],
        })
        if self.responses:
            return self.responses.pop(0)
        from astrbot_plugin_echo.domain import ModelTurn
        return ModelTurn(text="收到啦。")

    def describe_image(self, image_bytes, prompt, timeout=None):
        self.vision_calls += 1
        return self.vision_label

    def extra_secrets(self):
        return ()


async def _drive_event(echo, ev):
    async for _ in echo.on_group_message(ev):
        pass


class IncidentReplayTests(unittest.TestCase):
    def _build_echo(self, tmpdir: Path, spy_llm=None):
        stubs = _make_astrbot_stubs(tmpdir)
        ctx = MagicMock()
        ctx.send_message = AsyncMock(return_value=None)
        patcher = patch.dict(sys.modules, stubs)
        patcher.start()
        self.addCleanup(patcher.stop)

        sys.modules.pop("astrbot_plugin_echo.main", None)
        import astrbot_plugin_echo.main as main_mod
        from astrbot_plugin_echo.domain import PendingDelivery  # noqa: F401

        echo = main_mod.Echo(ctx, config={"api_key": "", "model": "gpt-6-luna"})
        self.addCleanup(echo.repo.close)
        self.addCleanup(lambda: echo.agent.wait_background(2.0))
        if spy_llm is not None:
            echo.llm = spy_llm
            echo.agent._llm = spy_llm
        echo.inbound._download = lambda url: TINY_GIF if "sticker" in url else TINY_PNG
        return echo, ctx

    def test_1_bare_at_with_empty_text_replies(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                from astrbot_plugin_echo.domain import ModelTurn
                spy = _SpyLLM([ModelTurn(text="咋啦喊我干嘛")])
                echo, ctx = self._build_echo(Path(tmp), spy)
                ev = _make_event([_At(qq="8888")], raw_msg={"message_id": "101", "message": []})
                await _drive_event(echo, ev)
                due = echo.scheduler.due(now=9e9)
                self.assertEqual(len(due), 1)
                await echo._process_batch(due[0])
                ctx.send_message.assert_awaited()
                self.assertEqual(ctx.send_message.call_args_list[0][0][1][-1].text, "咋啦喊我干嘛")

        asyncio.run(scenario())

    def test_2_and_3_and_4_unmentioned_img_followup_quoted_img_and_screenshot_vs_sticker(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                from astrbot_plugin_echo.domain import ModelTurn
                spy = _SpyLLM([ModelTurn(text="看清楚了"), ModelTurn(text="是引用图")], vision_label="搞笑|猫咪笑")
                echo, ctx = self._build_echo(Path(tmp), spy)

                # Replay 4a: 未 @ 普通截图 (NapCat sub_type=0) -> 不收表情包，仅留底待追问
                ev_shot = _make_event(
                    [_Image(url="https://cdn.example.com/shot.png", file="shot.png")],
                    raw_msg={"message_id": "201", "message": [{"type": "image", "data": {"file": "shot.png", "sub_type": "0", "summary": "[图片]"}}]},
                )
                await _drive_event(echo, ev_shot)
                if echo._bg_tasks:
                    await asyncio.gather(*list(echo._bg_tasks), return_exceptions=True)
                self.assertFalse(echo.emoji_store.has_entries("aiocqhttp:10086"))
                self.assertEqual(spy.vision_calls, 0)

                # Replay 2: 紧接一条 @大肥鱼 这是什么 -> 自动带入刚才那张截图并内联给 LLM
                ev_ask = _make_event(
                    [_At(qq="8888"), _Plain("这是什么")],
                    text="这是什么",
                    raw_msg={"message_id": "202", "message": []},
                )
                await _drive_event(echo, ev_ask)
                due = echo.scheduler.due(now=9e9)
                self.assertEqual(len(due), 1)
                await echo._process_batch(due[0])
                self.assertEqual(len(spy.chat_calls[0]["messages"][-1].images), 1)

                # Replay 3: 引用图 + @大肥鱼 看这图 -> 不判 retold，不收进 EmojiStore
                ev_q = _make_event(
                    [_Reply(id="200", sender_id="9", sender_nickname="小和", chain=[_Image(url="https://cdn.example.com/q.png")]), _At(qq="8888"), _Plain("看这图")],
                    text="看这图",
                    raw_msg={"message_id": "203", "message": []},
                )
                await _drive_event(echo, ev_q)
                due = echo.scheduler.due(now=9e9)
                self.assertFalse(due[0].retold)
                await echo._process_batch(due[0])
                self.assertEqual(len(spy.chat_calls[1]["messages"][-1].images), 1)
                self.assertFalse(echo.emoji_store.has_entries("aiocqhttp:10086"))

                # Replay 4b: 真表情包 (sub_type=1) -> 异步收入 EmojiStore
                ev_st = _make_event(
                    [_Image(url="https://cdn.example.com/sticker.gif", file="sticker.gif")],
                    raw_msg={"message_id": "204", "message": [{"type": "image", "data": {"file": "sticker.gif", "sub_type": "1", "summary": "[动画表情]"}}]},
                )
                await _drive_event(echo, ev_st)
                if echo._bg_tasks:
                    await asyncio.gather(*list(echo._bg_tasks), return_exceptions=True)
                self.assertTrue(echo.emoji_store.has_entries("aiocqhttp:10086"))

                # Replay 2b: 未 @ 发图后出现被跳过的闲聊批，绝不能偷吃图片；后续追问仍能看到原图
                echo.inbound._download = lambda url: TINY_GIF if "sticker" in url else (TINY_PNG + b"2" if "shot2" in url else TINY_PNG)
                ev_shot2 = _make_event(
                    [_Image(url="https://cdn.example.com/shot2.png", file="shot2.png")],
                    raw_msg={"message_id": "205", "message": [{"type": "image", "data": {"file": "shot2.png", "sub_type": "0", "summary": "[图片]"}}]},
                )
                await _drive_event(echo, ev_shot2)
                if echo._bg_tasks:
                    await asyncio.gather(*list(echo._bg_tasks), return_exceptions=True)

                echo.scheduler.ambient_rate = 0.0
                ev_noise = _make_event(
                    [_Plain("ai感是不是太重了")],
                    text="ai感是不是太重了",
                    raw_msg={"message_id": "206", "message": []},
                )
                await _drive_event(echo, ev_noise)
                await echo._process_batch(echo.scheduler.due(now=9e9)[0])  # 未被 should_view 选中，只入历史不消费图

                ev_ask_img = _make_event(
                    [_At(qq="8888"), _Plain("你咋没法识图啊")],
                    text="你咋没法识图啊",
                    raw_msg={"message_id": "207", "message": []},
                )
                await _drive_event(echo, ev_ask_img)
                calls_idx = len(spy.chat_calls)
                spy.responses.append(ModelTurn(text="刚才看到了！"))
                await echo._process_batch(echo.scheduler.due(now=9e9)[0])
                self.assertEqual(len(spy.chat_calls[calls_idx]["messages"][-1].images), 1)

        asyncio.run(scenario())

    def test_5_to_9_safety_gate_retold_lock_skip_and_emoji_cooldown(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                from astrbot_plugin_echo.domain import ModelTurn, ToolCall
                from astrbot_plugin_echo.workers import CodexBackend, SandboxRunner, TaskManager
                spy = _SpyLLM([
                    ModelTurn(tool_calls=(ToolCall("s1", "stay_silent", {}),)),
                    ModelTurn(text="哼谁管他怎么说"),
                    ModelTurn(tool_calls=(ToolCall("e1", "send_emoji", {"mood": "滑稽", "reply_text": "给你配个图"}),)),
                    ModelTurn(tool_calls=(ToolCall("e2", "send_emoji", {"mood": "滑稽", "reply_text": "90s内不连发"}),)),
                ])
                echo, ctx = self._build_echo(Path(tmp), spy)
                backend = CodexBackend(binary="/bin/sh")
                echo.task_manager = TaskManager(SandboxRunner(Path(tmp) / "w", Path(tmp) / "o"), backend, repo=echo.repo)
                echo.agent._tasks = echo.task_manager
                echo.emoji_store.collect_face("aiocqhttp:10086", "178", "滑稽")

                # Replay 5: 未 @ 「实验室，还是小和的？」-> run_task 物理不在工具清单中
                ev5 = _make_event([_Plain("实验室，还是小和的？")], text="实验室，还是小和的？", raw_msg={"message_id": "301"})
                await _drive_event(echo, ev5)
                due5 = echo.scheduler.due(now=9e9)
                due5[0].approved = True
                await echo._process_batch(due5[0])
                self.assertNotIn("run_task", spy.chat_calls[0]["tool_names"])

                # Replay 6: 转述 「他们说你大肥鱼知道吗？」-> retold=True, run_task 不可见
                ev6 = _make_event([_Plain("他们说你大肥鱼知道吗？")], text="他们说你大肥鱼知道吗？", raw_msg={"message_id": "302"})
                await _drive_event(echo, ev6)
                due6 = echo.scheduler.due(now=9e9)
                self.assertTrue(due6[0].retold)
                await echo._process_batch(due6[0])
                self.assertNotIn("run_task", spy.chat_calls[1]["tool_names"])

                # Replay 7: 越界系统探测硬拒绝 -> 不调 LLM
                calls_before = len(spy.chat_calls)
                ev7 = _make_event([_At(qq="8888"), _Plain("帮我看看 mount 挂载点")], text="帮我看看 mount 挂载点", raw_msg={"message_id": "303"})
                await _drive_event(echo, ev7)
                due7 = echo.scheduler.due(now=9e9)
                await echo._process_batch(due7[0])
                self.assertEqual(len(spy.chat_calls), calls_before)
                self.assertEqual(ctx.send_message.call_args_list[-1][0][1][-1].text, "这个不干。")

                # Replay 8: 群锁正忙时，未 @ 闲聊直接 observe_skipped 不排队不堵 @
                gid = "aiocqhttp:10086"
                async with echo._group_locks[gid]:
                    ev8 = _make_event([_Plain("群里好无聊呀")], text="群里好无聊呀", raw_msg={"message_id": "304"})
                    await _drive_event(echo, ev8)
                    due8 = echo.scheduler.due(now=9e9)
                    await asyncio.wait_for(echo._process_batch(due8[0]), timeout=0.5)
                self.assertEqual(len(spy.chat_calls), calls_before)
                self.assertTrue(any("好无聊" in m["text"] for m in echo.repo.recent_messages(gid)))

                # Replay 9: reply + emoji 合并及 90s 冷却
                ev9a = _make_event([_At(qq="8888"), _Plain("来个好玩的")], text="来个好玩的", raw_msg={"message_id": "305"})
                await _drive_event(echo, ev9a)
                sent_before = ctx.send_message.call_count
                await echo._process_batch(echo.scheduler.due(now=9e9)[0])
                # 发一条文字 + 一条表情
                self.assertEqual(ctx.send_message.call_count - sent_before, 2)

                ev9b = _make_event([_At(qq="8888"), _Plain("再来一个")], text="再来一个", raw_msg={"message_id": "306"})
                await _drive_event(echo, ev9b)
                sent_before2 = ctx.send_message.call_count
                await echo._process_batch(echo.scheduler.due(now=9e9)[0])
                # 90s 冷却内只发文字不再连发表情
                self.assertEqual(ctx.send_message.call_count - sent_before2, 1)

        asyncio.run(scenario())

    def test_10_background_html_delivery_and_timeout_partial(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                from astrbot_plugin_echo.domain import PendingDelivery
                from astrbot_plugin_echo.workers import CodexBackend, SandboxRunner, TaskManager
                spy = _SpyLLM()
                echo, ctx = self._build_echo(Path(tmp), spy)
                backend = CodexBackend(binary="/bin/sh")
                outbox_dir = Path(tmp) / "outbox"
                task_dir = outbox_dir / "task_html"
                task_dir.mkdir(parents=True)
                html_path = task_dir / "report.html"
                html_path.write_text("<h1>OK</h1>", encoding="utf-8")

                echo.task_manager = TaskManager(SandboxRunner(Path(tmp) / "w", outbox_dir), backend, repo=echo.repo)
                echo.agent._tasks = echo.task_manager

                echo.repo.save_pending_delivery(PendingDelivery(
                    task_id="d_html",
                    group_id="aiocqhttp:10086",
                    umo="aiocqhttp:GroupMessage:10086",
                    prompt="做个HTML页面",
                    status="completed",
                    reason="ok",
                    result="页面已经做好啦",
                    files=({"name": "report.html", "path": str(html_path)},),
                ))
                echo.repo.save_pending_delivery(PendingDelivery(
                    task_id="d_to",
                    group_id="aiocqhttp:10086",
                    umo="aiocqhttp:GroupMessage:10086",
                    prompt="查超长任务",
                    status="failed",
                    reason="timeout",
                    partial="查到了第1批线索",
                ))
                await echo._deliver_completed_tasks()
                if echo._bg_tasks:
                    await asyncio.gather(*list(echo._bg_tasks))
                self.assertEqual(echo.repo.list_pending_deliveries(), [])
                self.assertGreaterEqual(ctx.send_message.call_count, 3)
                self.assertFalse(html_path.exists())
                # partial 进消息且无超时幻觉
                all_texts = [
                    getattr(call[0][1][-1], "text", "")
                    for call in ctx.send_message.call_args_list
                ]
                self.assertTrue(any("第1批线索" in t and "超时了" in t for t in all_texts), all_texts)

        asyncio.run(scenario())

    def test_11_ingress_logging_dedup_and_no_prompt_duplication(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                from astrbot_plugin_echo.domain import ModelTurn
                spy = _SpyLLM([ModelTurn(text="收到啦")])
                echo, _ = self._build_echo(Path(tmp), spy)
                ev = _make_event(
                    [_At(qq="8888"), _Plain("你好大肥鱼")],
                    text="你好大肥鱼",
                    raw_msg={"message_id": "m_ing_1", "message": []},
                )
                await _drive_event(echo, ev)

                # 未到防抖期时，入口已将消息持久化进 SQLite
                ingested = echo.repo.recent_messages("aiocqhttp:10086", limit=10)
                self.assertEqual(len(ingested), 1)
                self.assertEqual(ingested[0]["msg_id"], "m_ing_1")
                self.assertIn("你好大肥鱼", ingested[0]["text"])

                due = echo.scheduler.due(now=9e9)
                self.assertEqual(len(due), 1)
                await echo._process_batch(due[0])

                # 处理完成后同一 msg_id 不重复落库，且 LLM 上下文中当前发言仅出现 1 次
                after_rows = echo.repo.recent_messages("aiocqhttp:10086", limit=10)
                self.assertEqual(sum(1 for r in after_rows if r.get("msg_id") == "m_ing_1"), 1)
                non_sys = [str(m.content or "") for m in spy.chat_calls[0]["messages"] if m.role != "system"]
                self.assertEqual(sum(1 for c in non_sys if "你好大肥鱼" in c), 1)

        asyncio.run(scenario())

    def test_12_real_chat_loop_delivery_and_graceful_terminate(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                from astrbot_plugin_echo.domain import ModelTurn, PendingDelivery
                from astrbot_plugin_echo.workers import CodexBackend, SandboxRunner, TaskManager

                spy = _SpyLLM([ModelTurn(text="主聊响应"), ModelTurn(text="后台已完成整理")])
                echo, ctx = self._build_echo(Path(tmp), spy)
                echo.scheduler.mention_cap = 0.05
                echo.scheduler.quiet_delay = 0.05
                echo.task_manager = TaskManager(
                    SandboxRunner(Path(tmp) / "w", Path(tmp) / "outbox"),
                    CodexBackend(binary="/bin/sh"),
                    repo=echo.repo,
                )
                echo.agent._tasks = echo.task_manager

                await echo.initialize()
                try:
                    ev = _make_event(
                        [_At(qq="8888"), _Plain("在不在呀")],
                        text="在不在呀",
                        raw_msg={"message_id": "m_loop_1", "message": []},
                    )
                    await _drive_event(echo, ev)

                    deadline = time.monotonic() + 3.0
                    while time.monotonic() < deadline and ctx.send_message.call_count < 1:
                        await asyncio.sleep(0.05)
                    self.assertGreaterEqual(ctx.send_message.call_count, 1)

                    attempts = [0]
                    delivered_texts = []

                    async def _flaky_send(umo, chain):
                        attempts[0] += 1
                        if attempts[0] == 1:
                            raise RuntimeError("transient send error")
                        text = getattr(chain.chain[-1], "text", "") if getattr(chain, "chain", None) else ""
                        delivered_texts.append(text)
                        return None

                    ctx.send_message = AsyncMock(side_effect=_flaky_send)
                    echo.repo.save_pending_delivery(PendingDelivery(
                        task_id="d_loop_retry",
                        group_id="aiocqhttp:10086",
                        umo="aiocqhttp:GroupMessage:10086",
                        prompt="整理结果",
                        status="completed",
                        reason="ok",
                        result="后台已完成整理",
                    ))

                    deadline2 = time.monotonic() + 4.0
                    while time.monotonic() < deadline2 and (
                        not delivered_texts or echo.repo.list_pending_deliveries()
                    ):
                        await asyncio.sleep(0.05)

                    self.assertGreaterEqual(attempts[0], 2)
                    self.assertEqual(len(delivered_texts), 1)
                    self.assertEqual(echo.repo.list_pending_deliveries(), [])
                finally:
                    await echo.terminate()

                self.assertIsNone(echo._chat_task)
                self.assertTrue(echo.repo._closed)

        asyncio.run(scenario())

    def test_13_sticker_followup_evaluation_and_bare_at_after_sticker(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                from astrbot_plugin_echo.domain import ModelTurn
                spy = _SpyLLM(
                    [ModelTurn(text="哈哈这只小猫表情太魔性了"), ModelTurn(text="收到啦"), ModelTurn(text="又拿这个表情来怼我")],
                    vision_label="震惊呕吐|一只小猫露出惊恐表情",
                )
                echo, ctx = self._build_echo(Path(tmp), spy)

                # Replay A (19:00 线上事故): 先发未 @ 表情包(sub_type=1)，5 秒后 @ 评价这个表情
                ev_st = _make_event(
                    [_Image(url="https://cdn.example.com/sticker_yue.png", file="sticker_yue.png")],
                    uid="2320",
                    name="和",
                    raw_msg={"message_id": "401", "message": [{"type": "image", "data": {"file": "sticker_yue.png", "sub_type": "1", "summary": "[动画表情]"}}]},
                )
                await _drive_event(echo, ev_st)
                if echo._bg_tasks:
                    await asyncio.gather(*list(echo._bg_tasks), return_exceptions=True)

                ev_eval = _make_event(
                    [_At(qq="8888"), _Plain("评价这个表情")],
                    uid="2320",
                    name="和",
                    text="评价这个表情",
                    raw_msg={"message_id": "402", "message": []},
                )
                await _drive_event(echo, ev_eval)
                await echo._process_batch(echo.scheduler.due(now=9e9)[0])
                self.assertEqual(len(spy.chat_calls[0]["messages"][-1].images), 1)
                bot_rows = [r for r in echo.repo.recent_messages("aiocqhttp:10086", limit=10) if r["role"] == "bot"]
                self.assertTrue(any("seen_image" in str(r.get("meta") or "") for r in bot_rows), bot_rows)

                # Replay B (20:46 线上事故): 裸@ -> 发图/表情 -> 几秒后再裸@ 仍能看图不误吞
                ev_b1 = _make_event([_At(qq="8888")], uid="2573", name="炸芋花", raw_msg={"message_id": "403", "message": []})
                await _drive_event(echo, ev_b1)
                await echo._process_batch(echo.scheduler.due(now=9e9)[0])

                echo.inbound._download = lambda url: TINY_PNG + b"yue2" if "sticker_yue2" in url else (TINY_GIF if "sticker" in url else TINY_PNG)
                ev_st2 = _make_event(
                    [_Image(url="https://cdn.example.com/sticker_yue2.png", file="sticker_yue2.png")],
                    uid="2573",
                    name="炸芋花",
                    raw_msg={"message_id": "404", "message": [{"type": "image", "data": {"file": "sticker_yue2.png", "sub_type": "1", "summary": "[动画表情]"}}]},
                )
                await _drive_event(echo, ev_st2)
                if echo._bg_tasks:
                    await asyncio.gather(*list(echo._bg_tasks), return_exceptions=True)

                ev_b2 = _make_event([_At(qq="8888")], uid="2573", name="炸芋花", raw_msg={"message_id": "405", "message": []})
                await _drive_event(echo, ev_b2)
                sent_before = ctx.send_message.call_count
                await echo._process_batch(echo.scheduler.due(now=9e9)[0])
                self.assertGreater(ctx.send_message.call_count, sent_before)
                self.assertEqual(len(spy.chat_calls[-1]["messages"][-1].images), 1)

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()

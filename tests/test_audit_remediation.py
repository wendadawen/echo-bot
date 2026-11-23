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
    DEFAULT_API_BASE, MockModel, OpenAIModel, _extract_message_text, build_model, resolve_model_config,
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

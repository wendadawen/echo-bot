"""全仓逐文件代码审查后补齐的功能生效验证与边界回归测试。"""

import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugin"))

from astrbot_plugin_echo.core import actions, forward  # noqa: E402
from astrbot_plugin_echo.core.attention import AttentionTracker  # noqa: E402
from astrbot_plugin_echo.core.brain import Brain, FORMAT_HINT  # noqa: E402
from astrbot_plugin_echo.core.harness import CodexHarness  # noqa: E402
from astrbot_plugin_echo.core.heartbeat import Heartbeat  # noqa: E402
from astrbot_plugin_echo.core.memory import Memory  # noqa: E402
from astrbot_plugin_echo.core.messages import Message  # noqa: E402
from astrbot_plugin_echo.core.models import MockModel  # noqa: E402
from astrbot_plugin_echo.core.person_info import PersonStore  # noqa: E402
from astrbot_plugin_echo.core.prompts import Persona, PersonaLibrary  # noqa: E402
from astrbot_plugin_echo.core.rhythm import Batch, IMAGE_TTL, Item, Rhythm  # noqa: E402
from astrbot_plugin_echo.core.schedule import Schedule  # noqa: E402
from astrbot_plugin_echo.core.store import Store  # noqa: E402
from astrbot_plugin_echo.core.subagent import SubAgentManager  # noqa: E402


def _make_batch(text, user="1001", name="阿伟", group="g1", mentioned=True, ts=None):
    now = time.time() if ts is None else ts
    return Batch(
        group,
        items=[Item(text, user, name, mentioned, "demo:group-1", now)],
        started=now,
        deadline=now,
    )


class TestAuditFixes(unittest.TestCase):
    def test_attention_drift_fires_active_and_consumes_budget(self):
        seen_systems = []

        class RecordingModel:
            def reply(self, system, history, user_text):
                seen_systems.append(system)
                return '{"action":"reply","text":"确实"}'

        tracker = AttentionTracker(budget_per_hour=2)
        tracker.observe("g1", "归并排序怎么拆分数组")
        brain = Brain(Store(":memory:"), RecordingModel(), attention=tracker)
        res = brain.handle_batch(_make_batch("量子纠缠和超导量子比特的区别是什么"))
        self.assertTrue(res["replied"])
        self.assertEqual(1, tracker.budget("g1"))
        self.assertTrue(seen_systems)
        self.assertIn("漂移档位：活跃", seen_systems[0])

    def test_mark_followup_triggers_when_user_replies_after_bot(self):
        store = Store(":memory:")
        memory = Memory(":memory:")
        brain = Brain(store, MockModel(), memory=memory)

        brain.handle_batch(_make_batch("先聊一句"))
        self.assertEqual(0.0, memory.effect_rate("g1"))

        brain.handle_batch(_make_batch("接着上一句继续问"))
        # 第一条回复被用户接住记 1 次正反馈，第二条刚发出记 0 -> 1/2 = 0.5
        self.assertAlmostEqual(0.5, memory.effect_rate("g1"))

    def test_todo_lists_schedule_items_and_validates_reminder_edges(self):
        store = Store(":memory:")
        sched = Schedule(":memory:")
        brain = Brain(store, MockModel(), schedule=sched)

        r_daily = brain.command(Message("g1", "u1", "阿伟", "/提醒 每天22:00 交周报", True, "umo"))
        self.assertIn("22:00", r_daily)
        r_interval = brain.command(Message("g1", "u1", "阿伟", "/提醒 每30分钟 喝水", True, "umo"))
        self.assertIn("每30分钟", r_interval)
        r_weekly = brain.command(Message("g1", "u1", "阿伟", "/提醒 每周一 9:30 开会", True, "umo"))
        self.assertIn("每周一", r_weekly)
        r_once = brain.command(Message("g1", "u1", "阿伟", "/提醒 下周三 14:00 评审", True, "umo"))
        self.assertIn("14:00", r_once)

        todo = brain.command(Message("g1", "u1", "阿伟", "/待办", True, "umo"))
        self.assertIn("交周报", todo)
        self.assertIn("喝水", todo)
        self.assertIn("每 30 分钟", todo)
        self.assertIn("开会", todo)
        self.assertIn("每周 一 9:30", todo)
        self.assertIn("评审", todo)

        # 非法时间与 0 分钟不能创建提醒
        self.assertEqual(
            FORMAT_HINT,
            brain.command(Message("g1", "u1", "阿伟", "/提醒 每周一 99:99 开会", True, "umo")),
        )
        self.assertEqual(
            FORMAT_HINT,
            brain.command(Message("g1", "u1", "阿伟", "/提醒 0分钟 喝水", True, "umo")),
        )
        self.assertEqual(3, len(sched.list("g1")))

        # 没有 Schedule 时，下周几仍可回落到 Store
        store_only = Store(":memory:")
        brain_no_sched = Brain(store_only, MockModel(), schedule=None)
        fallback = brain_no_sched.command(
            Message("g1", "u1", "阿伟", "/提醒 下周三 10:00 体检", True, "umo")
        )
        self.assertIn("10:00", fallback)
        self.assertEqual(1, len(store_only.pending_reminders("g1")))

    def test_schedule_fire_once_skip_and_long_missed_interval(self):
        sched = Schedule(":memory:")
        now = 1_700_000_000.0
        sid_once = sched.add(
            "g1", "过期跳过", "once", due_at=now - 8 * 3600, missed="skip", now=now - 9 * 3600
        )
        self.assertIsNone(sched.fire(sid_once, now=now))
        self.assertEqual([], sched.list("g1"))

        sid_int = sched.add("g1", "每分钟", "interval", "1", now=now - 1000 * 60)
        fired = sched.fire(sid_int, now=now)
        self.assertIsNotNone(fired)
        rows = sched.list("g1")
        self.assertEqual(1, len(rows))
        self.assertGreater(rows[0]["next_at"], now)

    def test_memory_recall_preserve_weight_case_insensitive_and_no_suppression(self):
        mem = Memory(":memory:")
        mem.add_point("g1", "preference", "阿伟喜欢 Rust 编程")
        with mem._lock:
            row = mem._db.execute(
                "SELECT id FROM memory_points WHERE group_id='g1'"
            ).fetchone()
            pid = row["id"]
        mem.maintain_point("g1", pid, "protect")
        mem.maintain_point("g1", pid, "reinforce")
        with mem._lock:
            row = mem._db.execute(
                "SELECT weight, protected FROM memory_points WHERE id=?", (pid,)
            ).fetchone()
        self.assertEqual(1, row["protected"])
        self.assertGreaterEqual(row["weight"], 3.6)

        hits = mem.recall("g1", "rust")
        self.assertTrue(any("Rust" in h for h in hits))
        with mem._lock:
            after = mem._db.execute(
                "SELECT weight FROM memory_points WHERE id=?", (pid,)
            ).fetchone()["weight"]
        self.assertGreater(after, 3.6)

        # 低回应率不再注入「少说两句」自我抑制提示
        mem.record_reply("g1", "有人吗")
        self.assertEqual(0.0, mem.effect_rate("g1"))
        self.assertNotIn("少说两句", mem.context("g1", "rust", "阿伟"))

    def test_actions_empty_index_and_judge_300_cap(self):
        store = actions.ToolResultStore()
        store.add("g1", "recall", "第一条")
        store.add("g1", "recall", "第二条")
        self.assertEqual("第二条", store.get("g1", ""))
        self.assertEqual("第二条", store.get("g1", "   "))

        long_reply = "字" * 220
        act, out = actions.judge("reply", long_reply, [], None)
        self.assertEqual("reply", act)
        self.assertEqual(220, len(out))

    def test_person_store_merge_preserves_profiles_and_individuals(self):
        ps = PersonStore(":memory:")
        ps.observe("g1", "old", "小号", now=100.0)
        ps.observe("g2", "old", "小号", now=200.0)
        ps.observe("g1", "new", "大号", now=300.0)
        ps.upsert_profile("g1", "old", traits="爱写 Rust", preferences="冰美式", intimacy=0.9)
        ps.upsert_profile("g1", "new", traits="爱打球", relation="群友", intimacy=0.4)
        ps.upsert_profile("g2", "old", events="去过杭州")

        moved = ps.merge("old", "new")
        self.assertEqual(2, moved)

        prof_g1 = ps.profile("g1", "new")
        self.assertIn("爱打球", prof_g1["traits"])
        self.assertIn("爱写 Rust", prof_g1["traits"])
        self.assertEqual("冰美式", prof_g1["preferences"])
        self.assertEqual("群友", prof_g1["relation"])
        self.assertEqual(0.9, prof_g1["intimacy"])

        prof_g2 = ps.profile("g2", "new")
        self.assertEqual("去过杭州", prof_g2["events"])
        self.assertEqual({}, ps.profile("g1", "old"))

        ind_new = ps.individual("new")
        self.assertEqual({}, ps.individual("old"))
        self.assertEqual(100.0, ind_new["first_seen"])
        self.assertEqual(300.0, ind_new["last_seen"])
        self.assertEqual(3, ind_new["msg_count"])
        self.assertIn("g1", ind_new["groups"])
        self.assertIn("g2", ind_new["groups"])

    def test_persona_library_ignores_active_json_and_prevents_clobber(self):
        with tempfile.TemporaryDirectory() as folder:
            lib = PersonaLibrary(folder, builtin=Persona(id="default", name="默认"))
            normal = Persona(id="cat", name="猫娘", personality="傲娇")
            lib.save(normal)
            lib.activate("id", "cat")  # 故意用 "id" 当群号写入 active.json

            listed_ids = [p.id for p in lib.list()]
            self.assertEqual(["default", "cat"], listed_ids)
            self.assertIsNone(lib.get("id"))

            reserved = Persona(id="active", name="active", personality="不应覆盖 active.json")
            saved_path = lib.save(reserved)
            self.assertNotEqual("active.json", saved_path.name)
            self.assertEqual("cat", lib.active("id").id)

    def test_forward_handles_astrbot_nodes_and_single_node(self):
        class Plain:
            def __init__(self, text):
                self.text = text

        class Node:
            def __init__(self, name, content):
                self.name = name
                self.content = content

        class Nodes:
            def __init__(self, nodes):
                self.nodes = nodes

        comp = Nodes([Node("阿伟", [Plain("今晚吃啥")]), Node("小美", [Plain("火锅")])])
        self.assertTrue(forward.looks_like_forward(comp))
        lines = forward.extract(comp)
        self.assertEqual(["阿伟：今晚吃啥", "小美：火锅"], lines)

        single = Node("阿伟", [Plain("单条节点")])
        self.assertTrue(forward.looks_like_forward(single))
        self.assertEqual(["阿伟：单条节点"], forward.extract(single))

    def test_heartbeat_supports_overnight_active_hours(self):
        hb = Heartbeat(quiet=10, min_interval=10, active_hours=(22, 6))
        # 用固定当地日期时间转时间戳，保证测试不依赖运行时的时区与时钟。
        from datetime import datetime

        t_20 = datetime(2026, 11, 22, 20, 0).timestamp()
        hb.observe("g1", now=t_20)
        self.assertTrue(hb.due("g1", now=datetime(2026, 11, 22, 23, 30).timestamp()))
        self.assertTrue(hb.due("g1", now=datetime(2026, 11, 23, 2, 0).timestamp()))
        self.assertFalse(hb.due("g1", now=datetime(2026, 11, 23, 14, 0).timestamp()))

    def test_subagent_cancel_prevents_completed_overwrite(self):
        mgr = SubAgentManager()
        started = threading.Event()
        release = threading.Event()

        def slow(prompt):
            started.set()
            release.wait(timeout=2.0)
            return "晚到的结果"

        tid = mgr.submit("chat", "慢任务", slow)
        self.assertTrue(started.wait(timeout=1.0))
        self.assertTrue(mgr.cancel(tid))
        release.set()
        time.sleep(0.1)

        info = mgr.get(tid)
        self.assertEqual("stopped", info["status"])
        self.assertEqual([], mgr.poll())

    def test_rhythm_image_ttl_and_single_pop_requeue(self):
        rhythm = Rhythm(view_rate=1.0)
        now = 1_700_000_000.0
        rhythm.attach_image("g1", b"stale-img", now=now - IMAGE_TTL - 5)
        rhythm.attach_image("g1", b"fresh-img", now=now - 5)
        rhythm.add("g1", Item("看图", "u1", "阿伟", True, "umo", now), now=now)

        ready = rhythm.due(now + 10)
        self.assertEqual(1, len(ready))
        self.assertEqual([b"fresh-img"], ready[0].images)
        self.assertFalse(rhythm.has_batch("g1"))

        clone = rhythm.requeue(ready[0], now=now + 20)
        self.assertTrue(rhythm.has_batch("g1"))
        popped = rhythm.due(now + 20)
        self.assertEqual([clone], popped)
        self.assertFalse(rhythm.has_batch("g1"))
        self.assertEqual([], rhythm.due(now + 21))

    def test_harness_tmpdir_and_spontaneous_group_persona(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            fake_bin = root / "codex"
            fake_bin.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            harness = CodexHarness(
                binary=str(fake_bin),
                home=str(root / "home"),
                workdir_root=str(root / "work"),
            )
            captured_env = {}

            class FakeProc:
                pid = 999999

                def communicate(self, timeout=None):
                    return (
                        json.dumps({
                            "type": "item.completed",
                            "item": {"type": "agent_message", "text": "ok"},
                        }) + "\n",
                        "",
                    )

            def fake_popen(cmd, **kwargs):
                captured_env.update(kwargs.get("env") or {})
                return FakeProc()

            with patch("subprocess.Popen", side_effect=fake_popen):
                out = harness.run("g1", "hello")
            self.assertEqual("ok", out)
            self.assertEqual(str(root / "work"), captured_env.get("TMPDIR"))

            # spontaneous 使用当前群启用的人格
            lib = PersonaLibrary(root / "personas")
            custom = Persona(id="pirate", name="海盗", personality="说话像海盗船长")
            lib.save(custom)
            lib.activate("g1", "pirate")
            seen_systems = []

            class RecModel:
                def reply(self, system, history, user_text):
                    seen_systems.append(system)
                    return "起锚啦"

            brain = Brain(Store(":memory:"), RecModel(), personas=lib)
            line = brain.spontaneous("g1")
            self.assertEqual("起锚啦", line)
            self.assertTrue(seen_systems)
            self.assertIn("海盗船长", seen_systems[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)

"""第三批借用层单测：日程、心跳、语境黑话、合并转发、忘掉命令。全部离线。"""

import sys
import time
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugin"))

from astrbot_plugin_echo.core import forward, heartbeat, jargon  # noqa: E402
from astrbot_plugin_echo.core.brain import Brain  # noqa: E402
from astrbot_plugin_echo.core.messages import Message  # noqa: E402
from astrbot_plugin_echo.core.models import MockModel  # noqa: E402
from astrbot_plugin_echo.core.rhythm import Batch, Item  # noqa: E402
from astrbot_plugin_echo.core.schedule import Schedule, next_time, parse_clock  # noqa: E402
from astrbot_plugin_echo.core.store import Store  # noqa: E402
from astrbot_plugin_echo.core.subagent import SubAgentManager  # noqa: E402


def at(hour, minute=0, day=22):
    return time.mktime((2026, 11, day, hour, minute, 0, 0, 0, -1))


def message(text):
    return Message("g1", "1001", "阿伟", text, True, "demo:group-1")


class TestSchedule(unittest.TestCase):
    def test_parse_clock(self):
        self.assertEqual((22, 0), parse_clock("22:00"))
        self.assertEqual((9, 30), parse_clock("9点30"))
        self.assertEqual((7, 0), parse_clock("7点"))
        self.assertIsNone(parse_clock("晚点"))

    def test_next_interval(self):
        base = at(12)
        self.assertEqual(base + 1800, next_time("interval", "30", base))

    def test_next_daily_rolls_to_tomorrow(self):
        base = at(23)
        nxt = next_time("daily", "22:00", base)
        self.assertAlmostEqual(at(22, day=23), nxt, delta=60)

    def test_next_weekly(self):
        monday = datetime(2026, 11, 23, 10, 0).timestamp()
        nxt = next_time("weekly", "一 09:00", monday)
        self.assertAlmostEqual(monday + 7 * 86400 - 3600, nxt, delta=120)

    def test_add_due_fire(self):
        schedule = Schedule()
        now = at(12)
        row_id = schedule.add("g1", "喝水", "interval", "60", now=now)
        self.assertEqual([], schedule.due(now + 60))
        due = schedule.due(now + 3600)
        self.assertEqual(1, len(due))
        fired = schedule.fire(due[0]["id"], now + 3600)
        self.assertEqual("喝水", fired["text"])
        self.assertEqual([], schedule.due(now + 3600))

    def test_once_deactivates(self):
        schedule = Schedule()
        now = at(12)
        row_id = schedule.add("g1", "开会", "once", due_at=now + 60, now=now)
        schedule.fire(row_id, now + 60)
        self.assertEqual([], schedule.list("g1"))
        self.assertIsNone(schedule.fire(row_id, now + 120))

    def test_skip_drops_long_missed(self):
        schedule = Schedule()
        now = at(12)
        row_id = schedule.add("g1", "打卡", "interval", "60", missed="skip", now=now)
        self.assertIsNone(schedule.fire(row_id, now + 10 * 3600))

    def test_cancel_and_clear(self):
        schedule = Schedule()
        now = at(12)
        first = schedule.add("g1", "a", "interval", "60", now=now)
        schedule.add("g1", "b", "interval", "60", now=now)
        self.assertTrue(schedule.cancel(first, "g1"))
        self.assertEqual(1, len(schedule.list("g1")))
        self.assertEqual(1, schedule.clear("g1"))
        self.assertEqual([], schedule.list("g1"))


class TestHeartbeat(unittest.TestCase):
    def test_quiet_then_due(self):
        beat = heartbeat.Heartbeat()
        now = at(10)
        beat.observe("g", now)
        self.assertFalse(beat.due("g", now + 60))
        self.assertTrue(beat.due("g", now + 3 * 3600 + 1))

    def test_mark_silences(self):
        beat = heartbeat.Heartbeat()
        now = at(10)
        beat.observe("g", now)
        self.assertTrue(beat.due("g", now + 3 * 3600 + 1))
        beat.mark("g", now + 3 * 3600 + 1)
        self.assertFalse(beat.due("g", now + 3 * 3600 + 2))

    def test_night_is_silent(self):
        beat = heartbeat.Heartbeat()
        now = at(23, 30)
        beat.observe("g", now)
        self.assertFalse(beat.due("g", now + 3 * 3600 + 1))

    def test_remaining(self):
        beat = heartbeat.Heartbeat()
        now = at(10)
        beat.observe("g", now)
        self.assertAlmostEqual(beat.quiet, beat.remaining("g", now), delta=1)


class TestJargon(unittest.TestCase):
    PAIRS = [{"term": "老地方", "replacement": "公司", "trigger": "上班 通勤"}]

    def test_applies_with_context(self):
        self.assertEqual("明天上班还是公司见", jargon.apply("明天上班还是老地方见", self.PAIRS))

    def test_skips_without_context(self):
        self.assertEqual("老地方见", jargon.apply("老地方见", self.PAIRS))

    def test_learn_candidates(self):
        rows = jargon.learn(["归并排序好难", "归并排序真难", "别的"], min_count=2)
        self.assertTrue(rows)
        self.assertIn("归并", rows)


class TestForward(unittest.TestCase):
    def test_dict_forward(self):
        node = {
            "type": "forward",
            "content": [
                {"sender": {"nickname": "阿伟"}, "content": [{"type": "text", "text": "在吗"}]},
                {"sender": "小明", "content": "看这个"},
            ],
        }
        self.assertTrue(forward.looks_like_forward(node))
        lines = forward.extract(node)
        self.assertEqual(2, len(lines))
        self.assertIn("阿伟：在吗", lines[0])
        self.assertIn("小明：看这个", lines[1])
        self.assertIn("转发的聊天", forward.render(lines))

    def test_unknown_component(self):
        class Plain:
            pass

        self.assertFalse(forward.looks_like_forward(Plain()))
        self.assertEqual([], forward.extract(Plain()))
        self.assertEqual("", forward.render([]))


class TestTaskRouting(unittest.TestCase):
    def setUp(self):
        self.brain = Brain(Store(":memory:"), MockModel())
        self.brain.subagent = SubAgentManager()

    def test_keyword_hits(self):
        self.assertTrue(self.brain._is_direct_task("查询苏州天气"))
        self.assertTrue(self.brain._is_direct_task("帮我查一下这个链接"))
        self.assertFalse(self.brain._is_direct_task("今天好累"))
        # 「天气」不在关键词里，靠模型自己判断要不要调工具。
        self.assertFalse(self.brain._is_direct_task("今天天气怎么样"))

    def test_dispatch_returns_task(self):
        result = self.brain._dispatch_task("g1", "查询苏州天气", "demo:group-1")
        self.assertEqual("task", result["reason"])
        self.assertTrue(result["text"])

    def test_uncertain_answer_goes_to_task(self):
        class UnknownModel:
            def reply(self, system, history, user_text):
                return "这个我不知道"

        brain = Brain(Store(":memory:"), UnknownModel())
        brain.subagent = SubAgentManager()
        now = time.time()
        batch = Batch(
            "g1",
            items=[Item("你知道B卡的CLC吗", "1001", "阿伟", True, "demo:group-1", now)],
            started=now,
            deadline=now,
        )
        self.assertEqual("task", brain.handle_batch(batch)["reason"])

    def test_view_rate_for_question(self):
        now = time.time()
        batch = Batch(
            "g1",
            items=[Item("这个怎么弄", "1001", "阿伟", False, "demo:group-1", now)],
            started=now,
            deadline=now,
        )
        self.assertEqual(0.5, self.brain.view_rate(batch))

    def test_failed_task_reports_to_group(self):
        def boom(prompt):
            raise RuntimeError("炸了")

        task_id = self.brain.subagent.submit("chat", "查天气", boom)
        self.brain._task_targets[task_id] = ("g1", "demo:group-1")
        rows = []
        for _ in range(50):
            rows = self.brain.poll_subagents()
            if rows:
                break
            time.sleep(0.05)
        self.assertEqual(1, len(rows))
        self.assertFalse(rows[0]["ok"])
        self.assertIn("没弄成", rows[0]["text"])

    def test_refusal_falls_back_to_task(self):
        class RefuseModel:
            def reply(self, system, history, user_text):
                return "我这边查不了实时天气"

        brain = Brain(Store(":memory:"), RefuseModel())
        brain.subagent = SubAgentManager()
        now = time.time()
        batch = Batch(
            "g1",
            items=[Item("苏州今天热不热", "1001", "阿伟", True, "demo:group-1", now)],
            started=now,
            deadline=now,
        )
        self.assertEqual("task", brain.handle_batch(batch)["reason"])


class TestBrainScheduleAndForget(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.brain = Brain(self.store, MockModel())
        self.brain.schedule = Schedule()

    def test_every_hours_reminder(self):
        result = self.brain.handle(message("/提醒 每2小时 喝水"))
        self.assertIn("每2小时", result["text"])
        rows = self.brain.schedule.list("g1")
        self.assertEqual(1, len(rows))
        self.assertEqual("interval", rows[0]["kind"])
        self.assertEqual("120", rows[0]["spec"])

    def test_weekly_reminder(self):
        result = self.brain.handle(message("/提醒 每周一 9:00 开会"))
        self.assertIn("每周一", result["text"])
        rows = self.brain.schedule.list("g1")
        self.assertEqual("weekly", rows[0]["kind"])

    def test_next_week_reminder(self):
        result = self.brain.handle(message("/提醒 下周三 交方案"))
        self.assertIn("交方案", result["text"])
        rows = self.brain.schedule.list("g1")
        self.assertEqual(1, len(rows))
        self.assertEqual("once", rows[0]["kind"])
        self.assertGreater(rows[0]["next_at"], time.time())

    def test_forget_clears_messages(self):
        self.brain.handle(message("随便聊聊"))
        self.assertTrue(self.store.recent("g1"))
        result = self.brain.handle(message("/忘掉"))
        self.assertIn("清了", result["text"])
        # 命令自己的回复会入库，这里只要求原来的聊天被清掉。
        rows = self.store.recent("g1")
        self.assertFalse([row for row in rows if row["role"] == "user"])


if __name__ == "__main__":
    unittest.main(verbosity=2)

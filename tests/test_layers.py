"""借鉴 MaiBot 的解耦层单测：输入、决策、输出、记忆、视觉，全部离线。"""

import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugin"))

from astrbot_plugin_echo.core.actions import ToolLoop, parse  # noqa: E402
from astrbot_plugin_echo.core.expression import apply_jargon, split, typo  # noqa: E402
from astrbot_plugin_echo.core.memory import Memory, decay, maintain, parse_memory  # noqa: E402
from astrbot_plugin_echo.core.rhythm import Item, Rhythm  # noqa: E402
from astrbot_plugin_echo.core.vision import EmojiBank  # noqa: E402


def item(text, user="u1", name="阿伟", mentioned=False, ts=None):
    return Item(text, user, name, mentioned, "demo:group",
                time.time() if ts is None else ts)


class TestRhythm(unittest.TestCase):
    def test_batch_merges_same_user_messages(self):
        rhythm = Rhythm(quiet=0, active=0, same=0, other=0, cap=10)
        now = time.time()
        rhythm.add("g", item("在吗", ts=now), now)
        rhythm.add("g", item("怎么了", ts=now), now)
        batches = rhythm.due(now + 1)
        self.assertEqual(1, len(batches))
        self.assertIn("在吗", batches[0].text)
        self.assertIn("怎么了", batches[0].text)

    def test_mention_forces_view_and_shortens_deadline(self):
        rhythm = Rhythm(quiet=10, active=10, mention_cap=1)
        now = time.time()
        rhythm.add("g", item("看下这个", mentioned=True, ts=now), now)
        batches = rhythm.due(now + 1.1)
        self.assertEqual(1, len(batches))
        self.assertTrue(batches[0].mentioned)
        self.assertTrue(rhythm.should_view("g", now, forced=True))

    def test_debt_blocks_next_batches(self):
        rhythm = Rhythm(view_rate=1.0)
        now = time.time()
        rhythm.borrow("g", 2)
        self.assertFalse(rhythm.should_view("g", now))
        self.assertFalse(rhythm.should_view("g", now))
        self.assertTrue(rhythm.should_view("g", now))
        self.assertEqual(0, rhythm.debt("g"))

    def test_mute_silences_group(self):
        rhythm = Rhythm()
        rhythm.mute("g")
        self.assertTrue(rhythm.is_muted("g"))
        rhythm.mute("g", False)
        self.assertFalse(rhythm.is_muted("g"))


class TestActions(unittest.TestCase):
    def test_parse_action(self):
        self.assertEqual(
            {"action": "reply", "text": "少来这套"},
            parse('```json\n{"action":"reply","text":"少来这套"}\n```'),
        )

    def test_parse_rejects_unknown_action(self):
        self.assertIsNone(parse('{"action":"rm_rf","text":"/"}'))

    def test_fallback_to_plain_text(self):
        class PlainModel:
            name = "mock"

            def reply(self, system, history, user_text):
                return "嗯嗯，我在听。"

        loop = ToolLoop(PlainModel())
        result = loop.run("人设", [], "你好", lambda g, a, t: (False, ""), "g")
        self.assertEqual("reply", result["action"])
        self.assertIn("嗯嗯", result["text"])

    def test_tool_result_is_fed_back(self):
        calls = []

        class RecallModel:
            name = "mock"

            def reply(self, system, history, user_text):
                calls.append(system)
                if len(calls) == 1:
                    return '{"action":"recall","text":"上周聊了什么"}'
                return '{"action":"reply","text":"上周在聊归并排序"}'

        loop = ToolLoop(RecallModel())
        result = loop.run(
            "人设", [], "还记得吗",
            lambda g, a, t: (False, "上周在聊归并排序"), "g",
        )
        self.assertEqual("reply", result["action"])
        self.assertIn("归并排序", calls[1])


class TestExpression(unittest.TestCase):
    def test_split_keeps_short_parts(self):
        parts = split("第一句。第二句！第三句？", max_parts=3)
        self.assertLessEqual(len(parts), 3)
        self.assertTrue(all(parts))

    def test_typo_and_jargon(self):
        self.assertIsInstance(typo("随便"), str)
        self.assertEqual("老地方见", apply_jargon("公司见", {"公司": "老地方"}))

    def test_dedup(self):
        from astrbot_plugin_echo.core.expression import dedup

        self.assertTrue(dedup("哈哈", ["哈哈"]))
        self.assertFalse(dedup("哈哈", ["别的"]))


class TestMemory(unittest.TestCase):
    def test_parse_memory(self):
        episode, points = parse_memory(
            '{"episode":"聊了归并排序","points":[{"category":"event","content":"阿伟在学排序"}]}'
        )
        self.assertEqual("聊了归并排序", episode)
        self.assertEqual([("event", "阿伟在学排序")], points)

    def test_decay_and_reinforce(self):
        now = time.time()
        self.assertLess(decay(1.0, now - 14 * 86400, now), 0.6)
        self.assertGreater(maintain("reinforce", 1.0), 1.0)
        self.assertEqual(0.0, maintain("recycle_bin", 3.0))

    def test_episode_and_person_are_group_isolated(self):
        memory = Memory(":memory:")
        memory.add_episode("g1", "聊了排序")
        memory.upsert_person("g1", "阿伟", "preference", "喜欢 Rust")
        self.assertEqual(["聊了排序"], memory.recent_episodes("g1"))
        self.assertEqual([], memory.recent_episodes("g2"))
        self.assertIn("喜欢 Rust", memory.context("g1", "", "阿伟"))
        self.assertNotIn("喜欢 Rust", memory.context("g2", "", "阿伟"))

    def test_recall_reinforces_on_hit(self):
        memory = Memory(":memory:")
        memory.add_point("g1", "event", "阿伟在学排序")
        memory.add_point("g1", "event", "阿伟在学排序")
        rows = memory.recall("g1", "排序")
        self.assertIn("阿伟在学排序", rows)


class TestVision(unittest.TestCase):
    def test_emoji_bank_collect_and_pick(self):
        import tempfile

        class VisionModel:
            name = "openai"

            def vision_reply(self, text, image_url):
                return "无语"

        with tempfile.TemporaryDirectory() as folder:
            bank = EmojiBank(folder)
            label = bank.collect("g1", b"fake-png", VisionModel())
            self.assertEqual("无语", label)
            path = bank.pick("g1", "无语")
            self.assertIsNotNone(path)
            self.assertTrue(path.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)

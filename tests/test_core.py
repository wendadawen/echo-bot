"""核心逻辑单测：不安装 AstrBot、不联网、全部使用内存数据库。"""

import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugin"))

from astrbot_plugin_qunyou.core.brain import Brain  # noqa: E402
from astrbot_plugin_qunyou.core.messages import Message  # noqa: E402
from astrbot_plugin_qunyou.core.models import MockModel, OpenAIModel, build_model  # noqa: E402
from astrbot_plugin_qunyou.core.store import Store  # noqa: E402


def make_brain(chance=0.0):
    store = Store(":memory:")
    return store, Brain(store, MockModel(), chance)


def msg(text, mentioned=True, group="g1", umo="demo:group-1"):
    return Message(group, "1001", "阿伟", text, mentioned, umo)


class TestBrain(unittest.TestCase):
    def test_group_memory_isolated(self):
        store, brain = make_brain()
        brain.handle(msg("/记 阿伟=摸鱼冠军"))
        self.assertIn("阿伟", store.facts_text("g1"))
        self.assertEqual("（暂无）", store.facts_text("g2"))

    def test_natural_memory_command(self):
        store, brain = make_brain()
        result = brain.handle(msg("记住 暗号是吃火锅"))
        self.assertEqual("command", result["reason"])
        self.assertIn("吃火锅", store.facts_text("g1"))

    def test_mention_forces_reply_and_probability(self):
        store, brain = make_brain(0.0)
        self.assertFalse(brain.handle(msg("今天好累", mentioned=False))["replied"])
        r = brain.handle(msg("今天好累"))
        self.assertTrue(r["replied"])
        self.assertEqual("mention", r["reason"])
        brain.chance = 1.0
        self.assertTrue(brain.handle(msg("再来一句", mentioned=False))["replied"])

    def test_reply_is_written_to_history(self):
        store, brain = make_brain()
        brain.handle(msg("你好"))
        rows = store.recent("g1")
        self.assertEqual("user", rows[0]["role"])
        self.assertEqual("bot", rows[1]["role"])

    def test_reminder_fires_once_and_keeps_session(self):
        store, brain = make_brain()
        store.add_reminder("g1", "1001", "阿伟", "交周报", time.time() - 1, "demo:group-1")
        first = brain.tick()
        self.assertEqual(1, len(first))
        self.assertEqual("demo:group-1", first[0]["umo"])
        self.assertEqual("交周报", first[0]["text"])
        self.assertEqual([], brain.tick())

    def test_mock_model_uses_group_memory(self):
        store, brain = make_brain()
        brain.handle(msg("/记 暗号=吃火锅"))
        r = brain.handle(msg("这个怎么办？"))
        self.assertIn("吃火锅", r["text"])

    def test_help_and_pending_commands(self):
        store, brain = make_brain()
        self.assertIn("/提醒", brain.handle(msg("/帮助"))["text"])
        self.assertIn("暂时没有", brain.handle(msg("/待办"))["text"])


class TestModels(unittest.TestCase):
    def test_default_is_mock(self):
        self.assertEqual("mock", build_model({}).name)

    def test_config_enables_openai(self):
        model = build_model({"api_key": "test", "api_base": "http://localhost/v1", "model": "demo"})
        self.assertIsInstance(model, OpenAIModel)
        self.assertEqual("http://localhost/v1/chat/completions", model.url)

    def test_mock_model_is_deterministic(self):
        m = MockModel()
        self.assertEqual("在呢，怎么啦？", m.reply("本群共同的记忆：\n（暂无）", [], "在吗"))


if __name__ == "__main__":
    unittest.main(verbosity=2)

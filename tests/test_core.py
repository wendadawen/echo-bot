"""核心逻辑单测：不安装 AstrBot、不联网、全部使用内存数据库。"""

import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugin"))

from astrbot_plugin_echo.core.brain import Brain  # noqa: E402
from astrbot_plugin_echo.core.messages import Message  # noqa: E402
from astrbot_plugin_echo.core.models import MockModel, OpenAIModel, build_model  # noqa: E402
from astrbot_plugin_echo.core.store import Store  # noqa: E402


def make_brain(chance=0.0):
    store = Store(":memory:")
    return store, Brain(store, MockModel(), chance)


def msg(text, mentioned=True, group="g1", umo="demo:group-1"):
    return Message(group, "1001", "阿伟", text, mentioned, umo)


class TestBrain(unittest.TestCase):
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

    def test_history_is_isolated_by_group(self):
        store, brain = make_brain()
        brain.handle(msg("你好"))
        self.assertEqual(2, len(store.recent("g1")))
        self.assertEqual([], store.recent("g2"))

    def test_reminder_fires_once_and_keeps_session(self):
        store, brain = make_brain()
        store.add_reminder("g1", "1001", "阿伟", "交周报", time.time() - 1, "demo:group-1")
        first = brain.tick()
        self.assertEqual(1, len(first))
        self.assertEqual("demo:group-1", first[0]["umo"])
        self.assertEqual("交周报", first[0]["text"])
        self.assertEqual([], brain.tick())

    def test_help_and_pending_commands(self):
        store, brain = make_brain()
        for text in ("/帮助", "帮助", "/help", "help"):
            result = brain.handle(msg(text))
            self.assertEqual("command", result["reason"])
            self.assertIn("/提醒", result["text"])
        for text in ("/待办", "待办"):
            result = brain.handle(msg(text))
            self.assertEqual("command", result["reason"])
            self.assertIn("暂时没有", result["text"])

    def test_reminder_command_accepts_both_forms(self):
        store, brain = make_brain()
        for text in ("提醒 5分钟 交周报", "/提醒 1小时 开会"):
            result = brain.handle(msg(text))
            self.assertEqual("command", result["reason"])
            self.assertIn("后我 @ 你", result["text"])
        self.assertEqual(2, len(store.pending_reminders("g1")))

    def test_memory_commands_are_gone(self):
        store, brain = make_brain()
        for text in ("/记 阿伟=摸鱼冠军", "/记忆", "记住 暗号是吃火锅"):
            result = brain.handle(msg(text))
            self.assertEqual("mention", result["reason"])
            self.assertNotIn("记住了", result["text"])


class TestModels(unittest.TestCase):
    def test_default_is_mock(self):
        self.assertEqual("mock", build_model({}).name)

    def test_config_enables_openai(self):
        model = build_model({"api_key": "test", "api_base": "http://localhost/v1", "model": "demo"})
        self.assertIsInstance(model, OpenAIModel)
        self.assertEqual("http://localhost/v1/chat/completions", model.url)

    def test_mock_model_is_deterministic(self):
        m = MockModel()
        self.assertEqual("在呢，怎么啦？", m.reply("人设", [], "在吗"))


if __name__ == "__main__":
    unittest.main(verbosity=2)

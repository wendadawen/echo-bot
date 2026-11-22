"""核心逻辑单测：不安装 AstrBot、不联网、全部使用内存数据库。"""

import json
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugin"))

from astrbot_plugin_echo.core.brain import Brain  # noqa: E402
from astrbot_plugin_echo.core.messages import Message  # noqa: E402
from astrbot_plugin_echo.core.models import MockModel, OpenAIModel, build_model  # noqa: E402
from astrbot_plugin_echo.core.store import Store  # noqa: E402


class FakeResponse:
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode("utf-8")

    def read(self):
        return self.payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class ScriptedModel:
    name = "openai"

    def __init__(self, output):
        self.output = output
        self.systems = []

    def reply(self, system, history, user_text):
        self.systems.append(system)
        return self.output


def make_brain():
    from astrbot_plugin_echo.core.modes import ModePolicy

    store = Store(":memory:")
    brain = Brain(store, MockModel())
    brain.modes = ModePolicy()
    return store, brain


def msg(text, mentioned=True, group="g1", umo="demo:group-1"):
    return Message(group, "1001", "阿伟", text, mentioned, umo)


class TestBrain(unittest.TestCase):
    def test_mention_forces_reply(self):
        store, brain = make_brain()
        brain.window = 0
        brain.cooldown = 0
        self.assertFalse(brain.handle(msg("今天好累", mentioned=False))["replied"])
        r = brain.handle(msg("今天好累"))
        self.assertTrue(r["replied"])
        self.assertEqual("mention", r["reason"])

    def test_window_keeps_conversation_going(self):
        store, brain = make_brain()
        brain.cooldown = 0
        brain.window_cooldown = 0
        self.assertEqual("mention", brain.handle(msg("在吗"))["reason"])
        self.assertEqual("window", brain.handle(msg("今天好累", mentioned=False))["reason"])

    def test_idle_chatter_stays_silent(self):
        store, brain = make_brain()
        brain.window = 0
        self.assertFalse(brain.handle(msg("哈哈", mentioned=False))["replied"])

    def test_question_replies_outside_window(self):
        store, brain = make_brain()
        brain.window = 0
        r = brain.handle(msg("这是怎么回事", mentioned=False))
        self.assertTrue(r["replied"])
        self.assertEqual("question", r["reason"])

    def test_cooldown_silences_immediate_followup(self):
        store, brain = make_brain()
        brain.window = 0
        self.assertEqual("question", brain.handle(msg("怎么了", mentioned=False))["reason"])
        self.assertFalse(brain.handle(msg("在吗", mentioned=False))["replied"])

    def test_mute_silences_until_unmute(self):
        store, brain = make_brain()
        self.assertIn("闭嘴", brain.handle(msg("/闭嘴"))["text"])
        self.assertFalse(brain.handle(msg("随便聊聊"))["replied"])
        self.assertFalse(brain.handle(msg("在吗", mentioned=False))["replied"])
        self.assertIn("说", brain.handle(msg("/说话"))["text"])
        self.assertTrue(brain.handle(msg("在吗"))["replied"])

    def test_commands_still_work_while_muted(self):
        store, brain = make_brain()
        brain.handle(msg("/闭嘴"))
        result = brain.handle(msg("/帮助"))
        self.assertEqual("command", result["reason"])
        self.assertEqual("/帮助", result["text"])

    def test_focus_command_sets_topic_and_resume(self):
        from astrbot_plugin_echo.core.modes import ModePolicy

        store, brain = make_brain()
        brain.modes = ModePolicy()
        self.assertIn("只聊归并排序", brain.handle(msg("/专注 归并排序"))["text"])
        self.assertEqual("focus", brain.modes.mode("g1"))
        self.assertTrue(brain.modes.focus_hit("g1", "归并排序怎么拆"))
        self.assertFalse(brain.modes.focus_hit("g1", "今晚吃什么"))
        self.assertIn("随便聊", brain.handle(msg("/随便"))["text"])
        self.assertEqual("normal", brain.modes.mode("g1"))

    def test_mode_command_reports_state(self):
        from astrbot_plugin_echo.core.modes import ModePolicy

        store, brain = make_brain()
        brain.modes = ModePolicy()
        self.assertIn("正常", brain.handle(msg("/模式"))["text"])
        brain.handle(msg("/闭嘴"))
        self.assertIn("闭嘴", brain.handle(msg("/模式"))["text"])

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
            self.assertEqual("/帮助", result["text"])
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

    def test_model_error_stays_silent(self):
        class BrokenModel:
            name = "broken"

            def reply(self, system, history, user_text):
                raise RuntimeError("boom")

        brain = Brain(Store(":memory:"), BrokenModel())
        result = brain.handle(msg("你好"))
        self.assertFalse(result["replied"])
        self.assertEqual("model_error", result["reason"])
        self.assertIn("boom", result["error"])

    def test_daily_rule_variants(self):
        cases = [
            ("每天22:00 交周报", (22, 0, "交周报")),
            ("每天晚上10点 交周报", (22, 0, "交周报")),
            ("每天晚上十点 提醒施超", (22, 0, "提醒施超")),
            ("每天早上8点半 开会", (8, 30, "开会")),
            ("每晚十点半 睡觉", (22, 30, "睡觉")),
            ("每天 22:00 写周报", (22, 0, "写周报")),
        ]
        for text, expected in cases:
            self.assertEqual(expected, Brain._parse_daily(text), text)

    def test_daily_reminder_fires_once_per_day(self):
        store, brain = make_brain()
        store.add_reminder("g1", "1001", "阿伟", "交周报", 0, "demo:group-1", "daily:22:00")
        day_one = time.mktime((2026, 11, 22, 22, 1, 0, 0, 0, -1))
        self.assertEqual(1, len(brain.tick(day_one)))
        self.assertEqual([], brain.tick(day_one))
        self.assertEqual([], brain.tick(time.mktime((2026, 11, 22, 12, 0, 0, 0, 0, -1))))
        day_two = time.mktime((2026, 11, 23, 22, 2, 0, 0, 0, -1))
        self.assertEqual(1, len(brain.tick(day_two)))
        self.assertEqual(1, len(store.pending_reminders("g1")))

    def test_pending_lists_daily_reminder(self):
        store, brain = make_brain()
        brain.handle(msg("/提醒 每天22:00 交周报"))
        result = brain.handle(msg("/待办"))
        self.assertIn("每天 22:00", result["text"])

    def test_model_fallback_creates_once_reminder(self):
        model = ScriptedModel('{"kind":"once","minutes":180,"text":"取快递"}')
        store = Store(":memory:")
        brain = Brain(store, model)
        result = brain.handle(msg("/提醒 明天下午三点左右提醒我取快递"))
        self.assertEqual("command", result["reason"])
        self.assertIn("180分钟后", result["text"])
        items = store.pending_reminders("g1")
        self.assertEqual(1, len(items))
        self.assertEqual("取快递", items[0]["text"])
        self.assertEqual("", items[0]["repeat"])

    def test_model_fallback_rejects_invalid_output(self):
        model = ScriptedModel("我不太确定")
        store = Store(":memory:")
        brain = Brain(store, model)
        result = brain.handle(msg("/提醒 明天下午三点左右提醒我取快递"))
        self.assertIn("帮助", result["text"])
        self.assertEqual([], store.pending_reminders("g1"))

    def test_model_gets_group_chat_context(self):
        model = ScriptedModel("嗯嗯")
        brain = Brain(Store(":memory:"), model)
        brain.handle(msg("随便聊聊"))
        self.assertIn("多人群聊", model.systems[0])

    def test_service_tone_is_filtered(self):
        class ServiceModel:
            name = "openai"

            def reply(self, system, history, user_text):
                return "作为 AI，我无法帮你处理这个。"

        brain = Brain(Store(":memory:"), ServiceModel())
        result = brain.handle(msg("帮我个忙"))
        self.assertFalse(result["replied"])
        self.assertEqual("filtered", result["reason"])

    def test_filter_only_covers_service_tone(self):
        from astrbot_plugin_echo.core.brain import FILTER_WORDS

        for word in ("作为 AI", "我无法", "有什么可以帮"):
            self.assertIn(word, FILTER_WORDS)
        # 正常说话会用的词不该再被拦。
        for word in ("抱歉", "谢谢", "收到", "看情况", "确实"):
            self.assertNotIn(word, FILTER_WORDS)
        self.assertLessEqual(len(FILTER_WORDS), 40)

    def test_model_parse_extracts_json_from_prose(self):
        model = ScriptedModel('好的，结果是 {"kind":"once","minutes":10,"text":"开会"}')
        brain = Brain(Store(":memory:"), model)
        self.assertEqual(10, brain._model_parse("十分钟后提醒我开会")["minutes"])

    def test_model_fallback_scheduled(self):
        output = '{"kind":"scheduled","day_offset":1,"hour":15,"minute":0,"text":"取快递"}'
        store = Store(":memory:")
        brain = Brain(store, ScriptedModel(output))
        result = brain.handle(msg("/提醒 明天下午三点左右提醒我取快递"))
        self.assertEqual("command", result["reason"])
        self.assertIn("我 @ 你", result["text"])
        items = store.pending_reminders("g1")
        self.assertEqual(1, len(items))
        self.assertEqual("取快递", items[0]["text"])
        self.assertEqual("", items[0]["repeat"])
        self.assertGreater(items[0]["due_at"], time.time())


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

    def test_null_content_falls_back_to_reasoning(self):
        model = OpenAIModel("key", "http://example/v1", "demo")
        payload = {"choices": [{"message": {"role": "assistant", "content": None,
                                            "reasoning_content": "想了一下"}}]}
        with patch("urllib.request.urlopen", return_value=FakeResponse(payload)):
            self.assertEqual("想了一下", model.reply("人设", [], "你好"))

    def test_empty_response_raises_clear_error(self):
        model = OpenAIModel("key", "http://example/v1", "demo")
        payload = {"choices": [{"message": {"role": "assistant", "content": None}}]}
        with patch("urllib.request.urlopen", return_value=FakeResponse(payload)):
            with self.assertRaises(RuntimeError):
                model.reply("人设", [], "你好")


if __name__ == "__main__":
    unittest.main(verbosity=2)

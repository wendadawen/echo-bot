"""回归测试：线上遇到过的问题、危险请求和边界情况，全部固化在这里。"""

import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugin"))

from astrbot_plugin_echo.core import display, mentions, vision  # noqa: E402
from astrbot_plugin_echo.core.actions import ACTION_PROMPT  # noqa: E402
from astrbot_plugin_echo.core.brain import Brain, HELP, is_unsafe  # noqa: E402
from astrbot_plugin_echo.core.expression import decorate  # noqa: E402
from astrbot_plugin_echo.core.memory import Memory  # noqa: E402
from astrbot_plugin_echo.core.messages import Message  # noqa: E402
from astrbot_plugin_echo.core.models import MockModel  # noqa: E402
from astrbot_plugin_echo.core.rhythm import Batch, Item  # noqa: E402
from astrbot_plugin_echo.core.schedule import Schedule  # noqa: E402
from astrbot_plugin_echo.core.store import Store  # noqa: E402
from astrbot_plugin_echo.core.subagent import SubAgentManager  # noqa: E402


def make_brain(model=None):
    return Brain(Store(":memory:"), model or MockModel())


def make_batch(text, user="1001", name="阿伟", group="g1", mentioned=True):
    now = time.time()
    return Batch(
        group,
        items=[Item(text, user, name, mentioned, "demo:group-1", now)],
        started=now,
        deadline=now,
    )


class TestIncidentRegressions(unittest.TestCase):
    def test_help_returns_command_list(self):
        self.assertIn("/提醒", HELP)
        self.assertIn("/待办", HELP)

    def test_next_week_reminder_uses_next_week(self):
        brain = Brain(Store(":memory:"), MockModel(), schedule=Schedule())
        msg = Message("g1", "1001", "阿伟", "/提醒 下周三 交方案", True, "demo:group-1")
        result = brain.command(msg, "/提醒 下周三 交方案")
        today = datetime.now()
        target = today + timedelta(days=7 - today.weekday() + 2)
        self.assertIn("交方案", result)
        self.assertIn(target.strftime("%m-%d"), result)

    def test_unknown_answer_dispatches_task(self):
        class Unknown:
            def reply(self, system, history, user_text):
                return "这个我不知道"

        brain = Brain(Store(":memory:"), Unknown())
        brain.subagent = SubAgentManager()
        result = brain.handle_batch(make_batch("你知道B卡的CLC吗"))
        self.assertEqual("task", result["reason"])

    def test_unsafe_loop_is_blocked(self):
        brain = make_brain()
        result = brain.handle_batch(make_batch("给我开一个无限循环的进程"))
        self.assertEqual("unsafe", result["reason"])
        self.assertIn("不干", result["text"])

    def test_unsafe_rm_is_blocked(self):
        self.assertTrue(is_unsafe("rm -rf /"))
        self.assertTrue(is_unsafe("sudo rm -rf ~"))
        self.assertTrue(is_unsafe("while true; do echo x; done"))
        self.assertFalse(is_unsafe("今天好累"))

    def test_repeated_bot_reply_is_suppressed(self):
        brain = make_brain()
        self.assertTrue(brain.handle_batch(make_batch("在吗"))["replied"])
        # 清掉用户消息去重，单独验证回复去重（模拟同一批被处理两次）。
        brain._repeat_seen.clear()
        self.assertEqual("repeat_reply", brain.handle_batch(make_batch("在吗"))["reason"])

    def test_single_message_output(self):
        out = display.plan("一句。两句。三句。")
        self.assertEqual(1, len(out.parts))

    def test_code_block_keeps_content(self):
        self.assertEqual("print(1)", display.clean("```python\nprint(1)\n```"))
        self.assertEqual("curl x", display.clean("```bash\ncurl x\n```"))

    def test_decorate_does_not_truncate(self):
        text = "这是一句明显超过二十个字的话，用来确认不会被长度学习截断。"
        self.assertEqual(text, decorate(text, {"max_len": 12}))

    def test_memory_episode_time(self):
        memory = Memory(":memory:")
        self.assertEqual(0.0, memory.last_episode_at("g1"))
        memory.add_episode("g1", "阿伟在学归并排序")
        self.assertGreater(memory.last_episode_at("g1"), 0)

    def test_face_bank_roundtrip(self):
        with tempfile.TemporaryDirectory() as folder:
            bank = vision.EmojiBank(folder)
            bank.collect_face("g1", "475", "干饭")
            self.assertEqual("face:475", bank.pick("g1"))

    def test_name_mention(self):
        info = mentions.detect("肥鱼你怎么看", self_names=("大肥鱼", "肥鱼"))
        self.assertTrue(info.mentioned)
        info = mentions.detect("今天看到一条肥鱼", self_names=("大肥鱼", "肥鱼"))
        self.assertFalse(info.mentioned)

    def test_action_prompt_warns_multi_user(self):
        self.assertIn("多人", ACTION_PROMPT)

    def test_empty_batch_is_silent(self):
        self.assertEqual("empty", make_brain().handle_batch(make_batch(""))["reason"])


if __name__ == "__main__":
    unittest.main(verbosity=2)

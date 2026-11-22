"""回归测试：线上遇到过的问题、危险请求和边界情况，全部固化在这里。"""

import json
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugin"))

from astrbot_plugin_echo.core import display, mentions, vision  # noqa: E402
from astrbot_plugin_echo.core.actions import ACTION_PROMPT, parse_full  # noqa: E402
from astrbot_plugin_echo.core.brain import Brain, HELP, is_unsafe  # noqa: E402
from astrbot_plugin_echo.core.expression import decorate  # noqa: E402
from astrbot_plugin_echo.core.harness import CodexHarness  # noqa: E402
from astrbot_plugin_echo.core.memory import Memory  # noqa: E402
from astrbot_plugin_echo.core.messages import Message  # noqa: E402
from astrbot_plugin_echo.core.models import MockModel  # noqa: E402
from astrbot_plugin_echo.core.rhythm import Batch, Item, Rhythm  # noqa: E402
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
        self.assertEqual("s += (-1)**i", display.clean("```python\ns += (-1)**i\n```"))
        self.assertEqual("别加花活", display.clean("**别加花活**"))

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

    def test_action_prompt_prefers_subagent_when_unsure(self):
        self.assertIn("怕答错就用 subagent", ACTION_PROMPT)

    def test_subagent_description_is_concrete(self):
        from astrbot_plugin_echo.core.actions import ACTION_SPECS
        self.assertIn("执行命令", ACTION_SPECS["subagent"][0])

    def test_subagent_is_listed_first(self):
        from astrbot_plugin_echo.core.actions import ACTIONS
        self.assertEqual("subagent", ACTIONS[0])

    def test_task_prefix_allows_code(self):
        from astrbot_plugin_echo.core.brain import TASK_PREFIX
        self.assertIn("代码", TASK_PREFIX)
        self.assertNotIn("不要 markdown", TASK_PREFIX)

    def test_context_ignores_huge_max_len(self):
        from astrbot_plugin_echo.core.context import BEHAVIOR, ContextBuilder
        builder = ContextBuilder("persona", None, {"max_len": 4000})
        self.assertEqual("", builder._section("defect", "g1", "", "", None))
        self.assertNotIn("不许澄清反问", BEHAVIOR)

    def test_summary_writes_person_profile(self):
        from astrbot_plugin_echo.core.person_info import PersonStore

        class PersonModel:
            def reply(self, system, history, user_text):
                return json.dumps({
                    "episode": "阿伟在学归并排序",
                    "persons": [{"name": "阿伟", "category": "person",
                                 "content": "在学归并排序"}],
                    "relations": [],
                    "points": [],
                }, ensure_ascii=False)

        people = PersonStore(":memory:")
        people.observe("g1", "1001", "阿伟")
        brain = Brain(Store(":memory:"), PersonModel(),
                      memory=Memory(":memory:"), persons=people)
        brain._summarize("g1", "阿伟：我在学归并排序")
        self.assertEqual("在学归并排序", people.profile("g1", "1001")["traits"])

    def test_subagent_terminal_dispatches(self):
        class SubModel:
            def reply(self, system, history, user_text):
                return '{"action":"subagent","text":"查一下苏州天气"}'

        brain = Brain(Store(":memory:"), SubModel())
        brain.subagent = SubAgentManager()
        result = brain.handle_batch(make_batch("这个怎么弄"))
        self.assertEqual("task", result["reason"])
        self.assertIn("瞅瞅", result["text"])

    def test_send_face_terminal_uses_bank(self):
        class FaceModel:
            def reply(self, system, history, user_text):
                return '{"action":"send_face","text":"开心"}'

        class FakeBank:
            def pick(self, group, keyword="", model=None):
                return "face:475"

        brain = Brain(Store(":memory:"), FaceModel(), emoji_bank=FakeBank())
        result = brain.handle_batch(make_batch("随便来一个"))
        self.assertEqual("face", result["reason"])
        self.assertEqual("475", result["face"])

    def test_recall_finds_episode(self):
        memory = Memory(":memory:")
        memory.add_episode("g1", "阿伟在学归并排序")
        self.assertIn("阿伟在学归并排序", memory.recall("g1", "归并排序"))

    def test_recall_finds_person(self):
        memory = Memory(":memory:")
        memory.upsert_person("g1", "阿伟", "person", "在学归并排序")
        self.assertTrue(memory.person("g1", "阿伟"))

    def test_empty_batch_is_silent(self):
        self.assertEqual("empty", make_brain().handle_batch(make_batch(""))["reason"])


class TestRhythm(unittest.TestCase):
    def test_carry_into_next_batch(self):
        rhythm = Rhythm()
        now = time.time()
        rhythm.add("g1", Item("在吗", "1", "阿伟", True, "umo", now), now=now)
        first = rhythm._batches["g1"]
        rhythm.due(now + 100)
        rhythm.carry("g1", first)
        rhythm.add("g1", Item("你好", "2", "小美", False, "umo", now + 101), now=now + 101)
        batches = rhythm.due(now + 200)
        texts = [item.text for batch in batches for item in batch.items]
        self.assertIn("在吗", texts)
        self.assertIn("你好", texts)

    def test_quota_borrows_debt(self):
        rhythm = Rhythm(view_rate=0.2)
        quota = rhythm.start_turn("g1")
        for _ in range(quota + 1):
            rhythm.use_round("g1", quota)
        self.assertEqual(1, rhythm.debt("g1"))

    def test_mention_deadline_is_short(self):
        rhythm = Rhythm()
        now = time.time()
        rhythm.add("g1", Item("@bot", "1", "阿伟", True, "umo", now), now=now)
        self.assertLessEqual(rhythm._batches["g1"].deadline, now + 2.5)


class TestStore(unittest.TestCase):
    def test_daily_reminder_is_due(self):
        store = Store(":memory:")
        store.add_reminder("g1", "1", "阿伟", "交周报", time.time() - 1,
                           "umo", "daily:22:00")
        self.assertEqual(1, len(store.due_reminders(time.time())))


class TestHarness(unittest.TestCase):
    def test_command_uses_nobody_and_full_access(self):
        harness = CodexHarness(binary="/usr/local/bin/codex", home="/tmp/h",
                               workdir_root="/tmp/w")
        cmd = harness._command("查天气")
        self.assertIn("/usr/bin/setpriv", cmd)
        self.assertIn("--reuid=65534", cmd)
        self.assertIn("danger-full-access", cmd)

    def test_workdir_shared_between_groups(self):
        with tempfile.TemporaryDirectory() as folder:
            harness = CodexHarness(
                binary="/bin/true",
                home=str(Path(folder) / "h"),
                workdir_root=str(Path(folder) / "w"),
            )
            self.assertEqual(harness._workdir("g1"), harness._workdir("g2"))


class TestActions(unittest.TestCase):
    def test_parse_full_valid(self):
        self.assertEqual("reply", parse_full('{"action":"reply","text":"hi"}')["action"])

    def test_parse_full_plain_text(self):
        self.assertIsNone(parse_full("普通文本"))

    def test_parse_full_code_block(self):
        parsed = parse_full('```json\n{"action":"silent"}\n```')
        self.assertEqual("silent", parsed["action"])


if __name__ == "__main__":
    unittest.main(verbosity=2)

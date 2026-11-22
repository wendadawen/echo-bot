"""借鉴 MaiBot 的解耦层单测：输入、决策、输出、记忆、视觉，全部离线。"""

import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugin"))

from astrbot_plugin_echo.core.actions import ToolLoop, parse  # noqa: E402
from astrbot_plugin_echo.core.expression import apply_jargon, split, typo  # noqa: E402
from astrbot_plugin_echo.core.memory import Memory, decay, maintain, parse_memory  # noqa: E402
from astrbot_plugin_echo.core.rhythm import Batch, Item, Rhythm  # noqa: E402
from astrbot_plugin_echo.core.vision import EmojiBank  # noqa: E402


def item(text, user="u1", name="阿伟", mentioned=False, ts=None, kind="text"):
    return Item(text, user, name, mentioned, "demo:group",
                time.time() if ts is None else ts, kind)


class TestRhythm(unittest.TestCase):
    def test_requeue_keeps_turn_id(self):
        rhythm = Rhythm()
        batch = Batch("g", items=[item("在吗")], started=1.0, deadline=2.0)
        clone = rhythm.requeue(batch, now=3.0)
        self.assertEqual(batch.turn_id, clone.turn_id)
        self.assertTrue(clone.approved)

    def test_batch_merges_same_user_messages(self):
        rhythm = Rhythm()
        rhythm.group_preset = dict(rhythm.group_preset, quiet=0, active=0,
                                   same=0, other=0, cap=10)
        now = time.time()
        rhythm.add("g", item("在吗", ts=now), now)
        rhythm.add("g", item("怎么了", ts=now), now)
        batches = rhythm.due(now + 1)
        self.assertEqual(1, len(batches))
        self.assertIn("在吗", batches[0].text)
        self.assertIn("怎么了", batches[0].text)

    def test_mention_forces_view_and_shortens_deadline(self):
        rhythm = Rhythm()
        rhythm.group_preset = dict(rhythm.group_preset, quiet=10, active=10,
                                   mention_cap=1)
        now = time.time()
        rhythm.add("g", item("看下这个", mentioned=True, ts=now), now)
        batches = rhythm.due(now + 1.1)
        self.assertEqual(1, len(batches))
        self.assertTrue(batches[0].mentioned)
        self.assertTrue(rhythm.should_view("g", now, forced=True))

    def test_unviewed_messages_are_carried_to_next_batch(self):
        rhythm = Rhythm()
        rhythm.group_preset = dict(rhythm.group_preset, quiet=0, active=0, cap=10)
        now = time.time()
        rhythm.add("g", item("第一句", ts=now), now)
        first = rhythm.due(now + 1)[0]
        rhythm.carry("g", first)
        rhythm.add("g", item("第二句", ts=now + 1), now + 1)
        second = rhythm.due(now + 2)[0]
        self.assertIn("第一句", second.text)
        self.assertIn("第二句", second.text)

    def test_wait_and_silent_view(self):
        rhythm = Rhythm(view_rate=1.0)
        now = time.time()
        rhythm.wait("g", 5, now)
        self.assertFalse(rhythm.wait_due("g", now + 1))
        self.assertTrue(rhythm.wait_due("g", now + 6))
        rhythm.mark_view("g", now)
        self.assertFalse(rhythm.silent_due("g", now + 1, interval=2))
        self.assertTrue(rhythm.silent_due("g", now + 5, interval=2))

    def test_presets_and_image_rule(self):
        rhythm = Rhythm(preset="steady")
        self.assertEqual(12.0, rhythm.cap)
        rhythm2 = Rhythm()
        now = time.time()
        rhythm2.add("g", item("[图片]", kind="image", ts=now), now)
        self.assertFalse(rhythm2.due(now + 1))
        self.assertTrue(rhythm2.due(now + 5))

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


class TestFullActions(unittest.TestCase):
    def test_validate_and_condense(self):
        from astrbot_plugin_echo.core.actions import condense, validate

        self.assertTrue(validate("reply", "在呢")[0])
        self.assertFalse(validate("reply", "")[0])
        self.assertFalse(validate("wait", "9999")[0])
        self.assertFalse(validate("send_face", "看看", scene="private")[0])
        self.assertIn("…", condense("x" * 500))
        self.assertEqual("（无结果）", condense(""))

    def test_scene_tool_discovery(self):
        from astrbot_plugin_echo.core.actions import available, render_prompt

        self.assertNotIn("wait", available("private"))
        self.assertIn("recall", available("group"))
        self.assertIn("动作列表", render_prompt("group"))

    def test_result_store(self):
        from astrbot_plugin_echo.core.actions import ToolResultStore

        store = ToolResultStore()
        store.add("g", "recall", "上周在聊排序")
        store.add("g", "note", "记下了")
        self.assertEqual(2, len(store.recent("g")))
        self.assertEqual("记下了", store.get("g"))


class TestFullExpression(unittest.TestCase):
    def test_delay_uses_elapsed_time(self):
        from astrbot_plugin_echo.core.expression import delay

        self.assertLess(delay("一句话", elapsed=1.0), delay("一句话", elapsed=0.0))

    def test_style_and_jargon_mining(self):
        from astrbot_plugin_echo.core.expression import learn_style, mine_jargon

        lines = ["老地方见啊", "老地方见啊", "老地方见啊", "随便聊聊"]
        self.assertTrue(any("老地方" in word for word in mine_jargon(lines, min_count=2)))
        style = learn_style(lines)
        self.assertIn("max_len", style)
        self.assertIn("catchphrases", style)

    def test_expression_learning(self):
        from astrbot_plugin_echo.core.expression import learn_expressions, pick_expression

        store = learn_expressions("有人问日期", "自己不会看？")
        self.assertEqual("自己不会看？", pick_expression("有人问日期", store))


class TestFullMemory(unittest.TestCase):
    def test_full_parse_with_persons_and_relations(self):
        from astrbot_plugin_echo.core.memory import parse_memory_full

        parsed = parse_memory_full(
            '{"episode":"聊了排序","points":[{"category":"event","content":"阿伟在学排序"}],'
            '"persons":[{"name":"阿伟","category":"preference","content":"喜欢 Rust"}],'
            '"relations":[{"a":"阿伟","b":"和","relation":"常一起抬杠"}]}'
        )
        self.assertEqual("聊了排序", parsed[0])
        self.assertEqual("阿伟", parsed[2][0][0])
        self.assertEqual(1, len(parsed[3]))

    def test_fts_recall_persons_relations_and_effects(self):
        memory = Memory(":memory:")
        memory.add_point("g", "event", "群里在聊归并排序")
        memory.upsert_person("g", "阿伟", "preference", "喜欢 Rust")
        memory.add_relation("g", "阿伟", "和", "常抬杠")
        self.assertIn("归并排序", memory.recall("g", "排序")[0])
        self.assertTrue(any("Rust" in row for row in memory.person("g", "阿伟")))
        self.assertIn("常抬杠", memory.relations("g", "阿伟")[0])
        memory.record_reply("g", "随便")
        memory.mark_followup("g")
        self.assertEqual(1.0, memory.effect_rate("g"))
        stats = memory.stats("g")
        self.assertIn("fts", stats)
        self.assertEqual(1, stats["relations"])

    def test_episode_rebuild_and_prune(self):
        memory = Memory(":memory:")
        memory.rebuild_episodes("g", "chat", ["旧事一", "旧事二"])
        self.assertEqual(2, len(memory.recent_episodes("g", limit=5)))
        memory.rebuild_episodes("g", "chat", ["新事"])
        self.assertEqual(["新事"], memory.recent_episodes("g", limit=5))
        memory.prune("g")
        self.assertIn("episodes", memory.stats("g"))

    def test_forget_and_reactivate(self):
        memory = Memory(":memory:")
        memory.add_point("g", "event", "阿伟上周提过归并排序")
        with memory._lock:
            memory._db.execute(
                "UPDATE memory_points SET state='forgotten', last_hit=? WHERE group_id='g'",
                (time.time() - 90 * 86400,),
            )
            memory._db.commit()
        memory.add_point("g", "event", "阿伟上周提过归并排序")
        self.assertIn("归并排序", " ".join(memory.recall("g", "排序")))


class TestFullVision(unittest.TestCase):
    def test_filter_and_grid_pick(self):
        from astrbot_plugin_echo.core.vision import acceptable, grid_pick

        self.assertFalse(acceptable("这是一个付款码"))
        self.assertTrue(acceptable("一只猫在睡觉"))

        class Picker:
            def vision_reply_json(self, content):
                return "2"

        candidates = [("a", b"x"), ("b", b"y"), ("c", b"z")]
        self.assertEqual(1, grid_pick(Picker(), candidates, "无语"))

    def test_emoji_cleanup_and_stats(self):
        import tempfile

        class VisionModel:
            def vision_reply(self, text, image_url):
                return "一只猫"

            def vision_reply_json(self, content):
                return "一只猫"

        with tempfile.TemporaryDirectory() as folder:
            bank = EmojiBank(folder)
            bank.collect("g", b"png", VisionModel())
            self.assertEqual(1, bank.stats("g")["g"])
            bank.cleanup(max_age_days=-1)
            self.assertEqual(0, bank.stats("g")["g"])


class TestContext(unittest.TestCase):
    def test_layered_sections(self):
        from astrbot_plugin_echo.core.context import ContextBuilder

        memory = Memory(":memory:")
        memory.add_episode("g", "刚聊过归并排序")
        builder = ContextBuilder("你是回声", memory, {"max_len": 30})
        builder.add_schedule("g", "五分钟后提醒交周报")
        builder.set_activity("g", "在摸鱼", time.time() + 60)
        text = builder.build("g", "排序", "阿伟")
        self.assertIn("你是回声", text)
        self.assertIn("最近聊过", text)
        self.assertIn("群里待办", text)
        self.assertIn("当前状态", text)
        self.assertIn("本地时间", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)

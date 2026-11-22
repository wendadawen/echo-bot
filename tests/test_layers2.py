"""第二批借用层单测：提示词与人格、模式、注意力、人物、提及、压缩、
输出编排、监控、子代理。全部离线，只用标准库。"""

import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugin"))

from astrbot_plugin_echo.core import attention, display, monitor, prompts  # noqa: E402
from astrbot_plugin_echo.core import compaction, mentions, modes, person_info  # noqa: E402
from astrbot_plugin_echo.core import vision  # noqa: E402
from astrbot_plugin_echo.core.subagent import SubAgentManager  # noqa: E402


class TestPrompts(unittest.TestCase):
    def test_persona_compose_has_sections(self):
        persona = prompts.Persona(
            id="p", name="回声", personality="嘴毒", behavior_style="不认错",
            reply_style="两句以内", examples=[{"ask": "在吗", "answer": "说事"}],
            boundaries="不揭短",
        )
        text = persona.compose()
        for token in ("【性格】", "【行为】", "【说话】", "【示例】", "【底线】"):
            self.assertIn(token, text)
        self.assertIn("群友：在吗", text)

    def test_prompt_store_override_order(self):
        with tempfile.TemporaryDirectory() as folder:
            store = prompts.PromptStore(folder)
            self.assertEqual("默认", store.get("persona", "g1", "默认"))
            store.set("persona", "全局")
            self.assertEqual("全局", store.get("persona", "g1"))
            store.set("persona", "本群", "g1")
            self.assertEqual("本群", store.get("persona", "g1"))
            self.assertEqual("全局", store.get("persona", "g2"))
            store.reset("persona", "g1")
            self.assertEqual("全局", store.get("persona", "g1"))

    def test_import_maibot_personality(self):
        with tempfile.TemporaryDirectory() as folder:
            lib = prompts.PersonaLibrary(folder)
            raw = """{"format":"maibot.personality","version":1,"profile":{
                "name":"测试","personality":"嘴毒","behavior_style":"怼回去",
                "reply_style":"短","group_chat_prompt":"群聊","private_chat_prompts":"私聊"}}"""
            persona = lib.import_json(raw)
            self.assertEqual("测试", persona.name)
            self.assertIn("怼回去", persona.compose())
            lib.save(persona)
            self.assertTrue(lib.get(persona.id))

    def test_import_rejects_unknown_field(self):
        with tempfile.TemporaryDirectory() as folder:
            lib = prompts.PersonaLibrary(folder)
            raw = '{"format":"maibot.personality","version":1,"profile":{"name":"x","evil":"1"}}'
            with self.assertRaises(ValueError):
                lib.import_json(raw)

    def test_activate_and_export(self):
        with tempfile.TemporaryDirectory() as folder:
            builtin = prompts.Persona(id="default", name="默认")
            lib = prompts.PersonaLibrary(folder, builtin)
            lib.save(prompts.Persona(id="sharp", name="锋利", personality="嘴毒"))
            lib.activate("g1", "sharp")
            self.assertEqual("sharp", lib.active("g1").id)
            self.assertEqual("default", lib.active("g2").id)
            self.assertIn("maibot.personality", lib.export_json("sharp"))


class TestModes(unittest.TestCase):
    def test_alias_and_quiet(self):
        policy = modes.ModePolicy()
        self.assertEqual("quiet", policy.set("g", "闭嘴"))
        self.assertFalse(policy.allows("g", mentioned=False))
        # 手动静默连 @ 也不回，和 /闭嘴 的语义一致。
        self.assertFalse(policy.allows("g", mentioned=True))
        policy.set("g", "正常")
        self.assertTrue(policy.allows("g", mentioned=False))

    def test_time_rule_allows_mention(self):
        policy = modes.ModePolicy()
        policy.add_rule("*", "23:00-08:00", "quiet")
        night = time.mktime((2026, 11, 22, 23, 30, 0, 0, 0, -1))
        self.assertFalse(policy.allows("g", mentioned=False, now=night))
        self.assertTrue(policy.allows("g", mentioned=True, now=night))

    def test_time_rule_across_midnight(self):
        policy = modes.ModePolicy()
        policy.add_rule("*", "23:00-08:00", "quiet")
        night = time.mktime((2026, 11, 22, 23, 30, 0, 0, 0, -1))
        noon = time.mktime((2026, 11, 22, 12, 0, 0, 0, 0, -1))
        self.assertEqual("quiet", policy.mode("g", night))
        self.assertEqual("normal", policy.mode("g", noon))

    def test_ttl_expires(self):
        policy = modes.ModePolicy()
        now = time.time()
        policy.set("g", "quiet", ttl=10, now=now)
        self.assertEqual("quiet", policy.mode("g", now + 5))
        self.assertEqual("normal", policy.mode("g", now + 20))

    def test_focus_hit(self):
        policy = modes.ModePolicy()
        policy.set("g", "focus", topic="归并排序")
        self.assertTrue(policy.focus_hit("g", "归并排序怎么拆"))
        self.assertFalse(policy.focus_hit("g", "今晚吃什么"))


class TestAttention(unittest.TestCase):
    def test_novelty_drops_on_repeat(self):
        self.assertGreater(attention.novelty("归并排序的合并步骤", ["今天天气不错"]), 0.5)
        self.assertLess(attention.novelty("归并排序的合并步骤", ["归并排序的合并步骤"]), 0.2)

    def test_drift_level_budget(self):
        self.assertEqual("active", attention.drift_level(0.9, budget=2))
        self.assertEqual("subtle", attention.drift_level(0.6, budget=2))
        self.assertEqual("off", attention.drift_level(0.1, budget=2))
        self.assertEqual("subtle", attention.drift_level(0.95, budget=0))

    def test_tracker_budget_consumed(self):
        tracker = attention.AttentionTracker(budget_per_hour=2)
        tracker.observe("g", "归并排序怎么拆")
        self.assertEqual(2, tracker.budget("g"))
        tracker.consume("g")
        self.assertEqual(1, tracker.budget("g"))
        self.assertEqual(1, tracker.budget("g"))

    def test_topic_guess(self):
        tracker = attention.AttentionTracker()
        tracker.observe("g", "归并排序好难")
        tracker.observe("g", "归并排序确实难")
        self.assertIn("归并", tracker.topic("g"))


class TestPersonInfo(unittest.TestCase):
    def test_observe_and_resolve(self):
        store = person_info.PersonStore()
        store.observe("g", "1001", "阿伟", card="伟哥")
        self.assertEqual("1001", store.resolve("g", "阿伟"))
        self.assertEqual("1001", store.resolve("g", "伟哥"))
        self.assertIsNone(store.resolve("g", "完全不相干的名字"))

    def test_fuzzy_resolve(self):
        store = person_info.PersonStore()
        store.observe("g", "1001", "炸芋花")
        self.assertEqual("1001", store.resolve("g", "炸芋花花"))

    def test_profile_and_summary(self):
        store = person_info.PersonStore()
        store.observe("g", "1001", "阿伟")
        store.upsert_profile("g", "1001", traits="爱抬杠", preferences="喜欢德州")
        summary = store.summary("g", "1001")
        self.assertIn("爱抬杠", summary)
        self.assertIn("喜欢德州", summary)

    def test_merge_moves_aliases(self):
        store = person_info.PersonStore()
        store.observe("g", "old", "旧号")
        store.observe("g", "new", "新号")
        self.assertGreater(store.merge("old", "new"), 0)
        self.assertEqual("new", store.resolve("g", "旧号"))

    def test_similarity(self):
        self.assertGreater(person_info.similarity("阿伟", "阿伟"), 0.99)
        self.assertLess(person_info.similarity("阿伟", "完全无关"), 0.4)


class TestMentions(unittest.TestCase):
    def test_text_at(self):
        info = mentions.detect("@回声 在吗", self_names=("回声",))
        self.assertTrue(info.mentioned)
        self.assertTrue(info.direct_question)

    def test_retold(self):
        self.assertTrue(mentions.is_retold("他说你刚才那句很蠢"))
        info = mentions.detect("但是他在说你诶", self_names=("回声",))
        self.assertTrue(info.retold)
        self.assertIn("转述", mentions.render_block(info))

    def test_plain_chat_not_mentioned(self):
        info = mentions.detect("今晚吃啥", self_names=("回声",))
        self.assertFalse(info.mentioned)
        self.assertFalse(info.retold)

    def test_extract_targets(self):
        self.assertIn("小明", mentions.extract_targets("@小明 你看这个"))
        self.assertIn("阿伟", mentions.extract_targets("阿伟你怎么看", known_names=("阿伟",)))

    def test_self_called_by_name(self):
        info = mentions.detect("肥鱼你怎么看", self_names=("大肥鱼", "肥鱼"))
        self.assertTrue(info.mentioned)
        self.assertTrue(info.self_called)
        info = mentions.detect("大肥鱼是不是傻", self_names=("大肥鱼", "肥鱼"))
        self.assertTrue(info.mentioned)

    def test_name_in_plain_sentence_not_called(self):
        info = mentions.detect("今天看到一条肥鱼", self_names=("大肥鱼", "肥鱼"))
        self.assertFalse(info.mentioned)


class TestEmojiBank(unittest.TestCase):
    def test_collect_face_and_pick(self):
        with tempfile.TemporaryDirectory() as folder:
            bank = vision.EmojiBank(folder)
            bank.collect_face("g1", "475", "干饭")
            self.assertEqual("face:475", bank.pick("g1"))
            self.assertEqual(1, bank.stats("g1")["g1"])


class TestCompaction(unittest.TestCase):
    def _rows(self, count):
        return [{"role": "user", "user_name": "u{}".format(i),
                 "text": "第{}条消息".format(i)} for i in range(count)]

    def test_small_history_untouched(self):
        rows = self._rows(3)
        kept, summary = compaction.compact(rows, budget_tokens=1000)
        self.assertEqual(rows, kept)
        self.assertEqual("", summary)

    def test_large_history_compacted(self):
        rows = self._rows(40)
        kept, summary = compaction.compact(rows, budget_tokens=120, keep_recent=3)
        self.assertLess(len(kept), len(rows))
        self.assertTrue(summary)
        self.assertIn("摘要", kept[0]["user_name"])

    def test_estimate_tokens(self):
        self.assertGreater(compaction.estimate_tokens("这是一句中文"), 1)
        self.assertEqual(1, compaction.estimate_tokens(""))


class TestDisplay(unittest.TestCase):
    def test_merge_burst(self):
        # 短碎片并回前一条，避免「嗯」「行」单独刷屏。
        self.assertEqual(["这是一句完整的话嗯行"],
                         display.merge_burst(["这是一句完整的话", "嗯", "行"]))
        self.assertEqual(["嗯行"], display.merge_burst(["嗯", "行"]))

    def test_clean_removes_markdown_marks(self):
        self.assertEqual("不告诉你", display.clean("`不告诉你`"))
        self.assertEqual("结论", display.clean("## 结论"))
        self.assertEqual("别加花活", display.clean("**别加花活**"))
        self.assertEqual("12°C", display.clean("$12°C$"))
        self.assertEqual("E=mc^2", display.clean("$E=mc^2$"))
        self.assertEqual("curl x", display.clean("```bash\ncurl x\n```"))
        self.assertEqual("链接 (http://example.com)", display.clean("[链接](http://example.com)"))
        self.assertEqual("index.html", display.clean("[index.html](/AstrBot/data/index.html)"))

    def test_plan_splits_and_delays(self):
        out = display.plan("第一句。第二句！第三句？第四句。", max_parts=3)
        self.assertLessEqual(len(out.parts), 3)
        self.assertEqual(len(out.parts), len(out.delays))
        self.assertTrue(all(value >= 0.15 for value in out.delays))

    def test_plan_attachments(self):
        out = display.plan("看这个", quote="上一条", at="阿伟", face="/tmp/a.png")
        self.assertEqual("阿伟", out.at)
        self.assertEqual("上一条", out.quote)
        self.assertTrue(out.face)

    def test_limiter(self):
        limiter = display.MessageLimiter(per_minute=3)
        now = time.time()
        self.assertTrue(limiter.allow("g", now=now))
        limiter.record("g", 3, now=now)
        self.assertFalse(limiter.allow("g", now=now))
        self.assertTrue(limiter.allow("g", now=now + 61))

    def test_preview(self):
        self.assertTrue(display.preview("长" * 100, limit=10).endswith("…"))


class TestMonitor(unittest.TestCase):
    def test_record_and_stats(self):
        mon = monitor.Monitor()
        mon.record("g", "reply", ok=True, latency=1.0, tokens=100)
        mon.record("g", "reply", ok=False, latency=3.0, tokens=50)
        stats = mon.stats("g")
        self.assertEqual(2, stats["total"])
        self.assertEqual(1, stats["errors"])
        self.assertEqual(150, stats["tokens"])
        self.assertAlmostEqual(2.0, stats["avg_latency"])

    def test_by_kind_and_events(self):
        mon = monitor.Monitor()
        mon.record("g", "reply")
        mon.record("g", "error", ok=False)
        kinds = {row["kind"]: row["n"] for row in mon.by_kind("g")}
        self.assertEqual(1, kinds["reply"])
        self.assertEqual(2, len(mon.events()))

    def test_prune(self):
        mon = monitor.Monitor()
        old = time.time() - 10 * 86400
        mon.record("g", "reply", now=old)
        mon.record("g", "reply")
        self.assertEqual(1, mon.prune(days=7))


class TestSubAgent(unittest.TestCase):
    def _wait(self, manager, timeout=2.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            rows = manager.poll()
            if rows:
                return rows
            time.sleep(0.02)
        return []

    def test_submit_and_poll(self):
        manager = SubAgentManager()
        task_id = manager.submit("research", "查一下归并排序", lambda prompt: "结论：" + prompt)
        self.assertTrue(task_id)
        rows = self._wait(manager)
        self.assertEqual(1, len(rows))
        self.assertIn("结论", rows[0]["result"])
        self.assertEqual("completed", rows[0]["status"])
        self.assertEqual([], manager.poll())

    def test_failure_is_captured(self):
        manager = SubAgentManager()

        def boom(prompt):
            raise RuntimeError("坏了")

        manager.submit("task", "x", boom)
        rows = self._wait(manager)
        self.assertEqual("failed", rows[0]["status"])
        self.assertIn("RuntimeError", rows[0]["error"])

    def test_concurrency_limit(self):
        manager = SubAgentManager(max_running=1)
        gate = {"release": False}

        def slow(prompt):
            while not gate["release"]:
                time.sleep(0.01)
            return "好"

        first = manager.submit("task", "a", slow)
        self.assertTrue(first)
        self.assertIsNone(manager.submit("task", "b", slow))
        gate["release"] = True
        self._wait(manager)

    def test_summary(self):
        manager = SubAgentManager()
        manager.submit("task", "x", lambda prompt: "好")
        self._wait(manager)
        summary = manager.summary()
        self.assertEqual(1, summary["completed"])


if __name__ == "__main__":
    unittest.main(verbosity=2)

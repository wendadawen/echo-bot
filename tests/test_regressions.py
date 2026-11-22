"""回归测试：线上遇到过的问题、危险请求和边界情况，全部固化在这里。"""

import json
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugin"))

from astrbot_plugin_echo.core import compaction, display, mentions, vision  # noqa: E402
from astrbot_plugin_echo.core.actions import ACTION_PROMPT, parse_full  # noqa: E402
from astrbot_plugin_echo.core.brain import Brain, HELP, is_unsafe  # noqa: E402
from astrbot_plugin_echo.core.context import ContextBuilder  # noqa: E402
from astrbot_plugin_echo.core.expression import decorate  # noqa: E402
from astrbot_plugin_echo.core.harness import CodexHarness  # noqa: E402
from astrbot_plugin_echo.core.memory import MEMORY_PROMPT, Memory  # noqa: E402
from astrbot_plugin_echo.core.messages import (  # noqa: E402
    Message, clean_speaker_name, format_speaker_message,
)
from astrbot_plugin_echo.core.models import MockModel, OpenAIModel  # noqa: E402
from astrbot_plugin_echo.core.person_info import PersonStore  # noqa: E402
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

    def test_action_prompt_answers_all_questions_in_batch(self):
        self.assertIn("分别接住", ACTION_PROMPT)
        self.assertNotIn("只回最后一条", ACTION_PROMPT)

    def test_action_prompt_prefers_subagent_when_unsure(self):
        self.assertIn("怕答错就用 subagent", ACTION_PROMPT)

    def test_subagent_description_is_concrete(self):
        from astrbot_plugin_echo.core.actions import ACTION_SPECS
        self.assertIn("执行命令", ACTION_SPECS["subagent"][0])

    def test_subagent_is_listed_first(self):
        from astrbot_plugin_echo.core.actions import ACTIONS
        self.assertEqual("subagent", ACTIONS[0])

    def test_task_prefix_has_no_search_recipe(self):
        from astrbot_plugin_echo.core.brain import TASK_PREFIX
        self.assertNotIn("duckduckgo", TASK_PREFIX.lower())
        self.assertNotIn("curl", TASK_PREFIX.lower())

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

    def test_normal_refusal_is_not_silenced(self):
        class Refuse:
            def reply(self, system, history, user_text):
                return "我无法证明黎曼猜想，这是未解决问题。"

        brain = Brain(Store(":memory:"), Refuse())
        result = brain.handle_batch(make_batch("证明一下黎曼猜想"))
        self.assertTrue(result["replied"])
        self.assertIn("黎曼", result["text"])

    def test_context_has_no_effect_suppression(self):
        from astrbot_plugin_echo.core.context import ContextBuilder
        builder = ContextBuilder("persona", None)
        self.assertEqual("", builder._section("effect", "g1", "", "", None))

    def test_persona_does_not_expose_base_model(self):
        from astrbot_plugin_echo.core.brain import PERSONA
        self.assertIn("不报", PERSONA)
        self.assertNotIn("如实回答", PERSONA)

    def test_empty_batch_is_silent(self):
        self.assertEqual("empty", make_brain().handle_batch(make_batch(""))["reason"])

    @staticmethod
    def _wait_subagent(brain):
        for _ in range(50):
            rows = brain.poll_subagents()
            if rows:
                return rows
            time.sleep(0.05)
        return []

    def test_task_prefix_is_pure_backend_worker(self):
        from astrbot_plugin_echo.core.brain import TASK_PREFIX
        self.assertNotIn("大肥鱼", TASK_PREFIX)
        self.assertNotIn("肥鱼", TASK_PREFIX)
        self.assertNotIn("群聊口吻", TASK_PREFIX)
        self.assertIn("后台", TASK_PREFIX)
        self.assertIn("不要扮演角色", TASK_PREFIX)
        self.assertIn("不要替主程序起草群聊话术", TASK_PREFIX)

    def test_action_prompt_forbids_subagent_drafting_reply(self):
        self.assertIn("代写", ACTION_PROMPT)

    def test_poll_subagents_rewrites_raw_report_via_main_model(self):
        from astrbot_plugin_echo.core.brain import PERSONA, SUBAGENT_REPLY_PROMPT

        calls = []

        class RewriteModel:
            def reply(self, system, history, user_text):
                calls.append((system, history, user_text))
                return "肥鱼推到函数方程对称那步就卡住了，临界带外的零点排不掉，这猜想目前确实还没公认证明。"

        store = Store(":memory:")
        brain = Brain(store, RewriteModel())
        brain.subagent = SubAgentManager()
        raw_report = (
            "函数方程 ξ(s)=ξ(1−s) 只能说明零点成对。\n\n"
            "刚才查资料时 DuckDuckGo 返回了人机验证页，没拿到搜索结果。\n\n"
            "群里可以这么回：\n\n"
            "“肥鱼试了函数方程这条路：目前没有公认证明。”"
        )
        task_id = brain.subagent.submit("chat", "尝试证明黎曼猜想", lambda _: raw_report)
        brain._task_targets[task_id] = ("g1", "demo:group-1")
        rows = self._wait_subagent(brain)

        self.assertEqual(1, len(rows))
        self.assertTrue(rows[0]["ok"])
        self.assertNotIn("群里可以这么回", rows[0]["text"])
        self.assertNotIn("DuckDuckGo", rows[0]["text"])
        self.assertIn("函数方程", rows[0]["text"])
        self.assertEqual(1, len(calls))
        self.assertIn(PERSONA[:20], calls[0][0])
        self.assertIn(SUBAGENT_REPLY_PROMPT, calls[0][0])
        self.assertIn("尝试证明黎曼猜想", calls[0][2])
        self.assertIn(raw_report, calls[0][2])
        self.assertEqual(rows[0]["text"], store.recent("g1", 1)[-1]["text"])

    def test_poll_subagents_preserves_code_blocks_verbatim(self):
        class IntroModel:
            def reply(self, system, history, user_text):
                self.last_user = user_text
                return "算好了，前面这段拿去用。"

        # 1) 完整闭合的 python 代码块
        model1 = IntroModel()
        brain1 = Brain(Store(":memory:"), model1)
        brain1.subagent = SubAgentManager()
        tid1 = brain1.subagent.submit(
            "chat", "写个求和",
            lambda _: "后台已跑通：\n\n```python\ns = sum(range(10))\nprint(s)\n```",
        )
        brain1._task_targets[tid1] = ("g1", "demo:group-1")
        rows1 = self._wait_subagent(brain1)
        self.assertEqual(1, len(rows1))
        self.assertIn("算好了，前面这段拿去用。", rows1[0]["text"])
        self.assertIn("s = sum(range(10))\nprint(s)", rows1[0]["text"])
        self.assertNotIn("```", rows1[0]["text"])
        self.assertNotIn("sum(range(10))", model1.last_user)

        # 2) 被 800 字截断的未闭合 text 代码块
        model2 = IntroModel()
        brain2 = Brain(Store(":memory:"), model2)
        brain2.subagent = SubAgentManager()
        digits = "3.14159265358979323846"
        tid2 = brain2.subagent.submit(
            "chat", "输出圆周率",
            lambda _: f"已用高精度算法校验。\n\n```text\n{digits}",
        )
        brain2._task_targets[tid2] = ("g1", "demo:group-1")
        rows2 = self._wait_subagent(brain2)
        self.assertEqual(1, len(rows2))
        self.assertIn("算好了，前面这段拿去用。", rows2[0]["text"])
        self.assertIn(digits, rows2[0]["text"])
        self.assertNotIn("```", rows2[0]["text"])
        self.assertNotIn("text\n", rows2[0]["text"])
        self.assertNotIn(digits, model2.last_user)

    def test_poll_subagents_fallback_strips_meta_leak_when_model_fails(self):
        class BrokenModel:
            def reply(self, system, history, user_text):
                raise RuntimeError("upstream down")

        brain = Brain(Store(":memory:"), BrokenModel())
        brain.subagent = SubAgentManager()
        raw_report = (
            "函数方程只能说明零点对称。\n\n"
            "群里可以这么回：\n\n"
            "“试了函数方程这条路，目前没有公认证明。”"
        )
        tid = brain.subagent.submit("chat", "尝试证明黎曼猜想", lambda _: raw_report)
        brain._task_targets[tid] = ("g1", "demo:group-1")
        rows = self._wait_subagent(brain)

        self.assertEqual(1, len(rows))
        self.assertTrue(rows[0]["ok"])
        self.assertEqual("试了函数方程这条路，目前没有公认证明。", rows[0]["text"])

    def test_poll_subagents_filters_service_tone_from_rewrite(self):
        class ServiceToneModel:
            def reply(self, system, history, user_text):
                return "作为 AI，我已经为您查询到相关信息，希望能帮到您。"

        brain = Brain(Store(":memory:"), ServiceToneModel())
        brain.subagent = SubAgentManager()
        tid = brain.subagent.submit("chat", "查天气", lambda _: "苏州今天多云，14到21度。")
        brain._task_targets[tid] = ("g1", "demo:group-1")
        rows = self._wait_subagent(brain)

        self.assertEqual(1, len(rows))
        self.assertTrue(rows[0]["ok"])
        self.assertNotIn("作为 AI", rows[0]["text"])
        self.assertNotIn("希望能帮到", rows[0]["text"])
        self.assertEqual("苏州今天多云，14到21度。", rows[0]["text"])

    def test_poll_subagents_truncates_codex_input_to_main_model(self):
        from astrbot_plugin_echo.core.brain import (
            SUBAGENT_HISTORY_ITEM_LIMIT,
            SUBAGENT_MODEL_INPUT_LIMIT,
            SUBAGENT_PROMPT_LIMIT,
        )

        calls = []

        class RecordingModel:
            def reply(self, system, history, user_text):
                calls.append((system, history, user_text))
                return "肥鱼压缩转述好了。"

        store = Store(":memory:")
        store.add_message("g1", "1001", "阿伟", "user", "H" * 1000)
        store.add_message("g1", "bot", "大肥鱼", "bot", "B" * 1000)
        brain = Brain(store, RecordingModel())
        brain.subagent = SubAgentManager()

        long_prompt = "P" * 500 + "PROMPT_TAIL"
        report_800 = "R" * 780 + "TAIL_MARKER"
        tid = brain.subagent.submit("chat", long_prompt, lambda _: report_800)
        brain._task_targets[tid] = ("g1", "demo:group-1")
        rows = self._wait_subagent(brain)

        self.assertEqual(1, len(rows))
        self.assertEqual("肥鱼压缩转述好了。", rows[0]["text"])
        self.assertEqual(1, len(calls))
        _, hist1, user1 = calls[0]
        self.assertNotIn("TAIL_MARKER", user1)
        self.assertNotIn("PROMPT_TAIL", user1)
        self.assertLessEqual(
            len(user1), SUBAGENT_PROMPT_LIMIT + SUBAGENT_MODEL_INPUT_LIMIT + 100
        )
        for item in hist1:
            self.assertLessEqual(len(item["text"]), SUBAGENT_HISTORY_ITEM_LIMIT + 1)

        # 直接传入超长 3000 字结果，确保即使绕过 SubAgentManager 也不会撑爆主模型上下文。
        calls.clear()
        huge_3000 = "X" * 2980 + "TAIL_MARKER"
        out2 = brain._present_subagent_result(
            "g1", {"prompt": long_prompt, "result": huge_3000}
        )
        self.assertEqual("肥鱼压缩转述好了。", out2)
        self.assertEqual(1, len(calls))
        _, hist2, user2 = calls[0]
        self.assertNotIn("TAIL_MARKER", user2)
        self.assertNotIn("PROMPT_TAIL", user2)
        self.assertLessEqual(
            len(user2), SUBAGENT_PROMPT_LIMIT + SUBAGENT_MODEL_INPUT_LIMIT + 100
        )
        for item in hist2:
            self.assertLessEqual(len(item["text"]), SUBAGENT_HISTORY_ITEM_LIMIT + 1)

    def test_plan_and_split_never_drop_text(self):
        from astrbot_plugin_echo.core import expression

        full_text = (
            "有啊，把 Claude 那份黎曼猜想 Lean 证明贴来，肥鱼可以认真核验；"
            "不过不为了烧 token 硬编任务，浪费口粮我可要心疼的。"
        )
        out = display.plan(full_text)
        self.assertEqual(1, len(out.parts))
        self.assertEqual(full_text, out.parts[0])

        parts = expression.split(
            "第一句已经足够长了肯定超过二十四个字不会和后面合并。第二句同样超过二十四个字了保证分成第二段。第三句收尾。",
            max_parts=2,
        )
        self.assertEqual(2, len(parts))
        self.assertEqual(
            "第一句已经足够长了肯定超过二十四个字不会和后面合并。第二句同样超过二十四个字了保证分成第二段。第三句收尾。",
            "".join(parts),
        )

    def test_clean_preserves_http_links_and_strips_local_paths(self):
        self.assertEqual(
            "IMDb (https://www.imdb.com/title/tt0118966/)",
            display.clean("[IMDb](https://www.imdb.com/title/tt0118966/)"),
        )
        self.assertEqual(
            "index.html",
            display.clean("[index.html](/AstrBot/data/index.html)"),
        )

    def test_decorate_never_replaces_with_old_expression(self):
        from astrbot_plugin_echo.core.expression import learn_expressions

        store = learn_expressions("高志朋 我不建议任何人", "旧的回复")
        for _ in range(50):
            self.assertEqual(
                "全新回答",
                decorate("全新回答", store=store, situation="高志朋 我不建议任何人"),
            )

    def test_harness_cleans_workdir_and_sanitizes_internal_leaks(self):
        from astrbot_plugin_echo.core.brain import TASK_PREFIX, _sanitize_meta_reply

        with tempfile.TemporaryDirectory() as folder:
            workdir = Path(folder)
            (workdir / "index.html").write_text("<html></html>", encoding="utf-8")
            sub = workdir / "assets"
            sub.mkdir()
            (sub / "app.js").write_text("console.log(1)", encoding="utf-8")

            CodexHarness._clean_workdir(workdir)
            self.assertTrue(workdir.exists())
            self.assertEqual([], list(workdir.iterdir()))

        self.assertIn("禁止启动 HTTP", TASK_PREFIX)
        self.assertIn("内网 IP", TASK_PREFIX)

        leaked = (
            "群里可以这么回：\n\n"
            "“弄好了，打开 [控制台](http://172.19.0.2:8765/) 或 "
            "/AstrBot/data/plugin_data/astrbot_plugin_echo/harness/index.html 看看。”"
        )
        cleaned = _sanitize_meta_reply(leaked)
        self.assertNotIn("172.19.0.2", cleaned)
        self.assertNotIn("/AstrBot/", cleaned)
        self.assertNotIn("群里可以这么回", cleaned)

    def test_batch_quote_reaches_model_system_prompt(self):
        seen_systems = []

        class RecordingModel:
            def reply(self, system, history, user_text):
                seen_systems.append(system)
                return '{"action":"reply","text":"收到引用了"}'

        brain = Brain(Store(":memory:"), RecordingModel())
        now = time.time()
        batch = Batch(
            "g1",
            items=[
                Item(
                    "这是什么回答",
                    "1001",
                    "阿伟",
                    True,
                    "demo:group-1",
                    now,
                    quote="大肥鱼：肥鱼认真试了...",
                )
            ],
            started=now,
            deadline=now,
        )
        result = brain.handle_batch(batch)
        self.assertTrue(result["replied"])
        self.assertTrue(seen_systems)
        self.assertIn("被引用的是：大肥鱼：肥鱼认真试了", seen_systems[0])

    def test_batch_stores_all_items_and_direct_task_records_message(self):
        from astrbot_plugin_echo.core.modes import ModePolicy

        store = Store(":memory:")
        brain = Brain(store, MockModel())
        now = time.time()
        batch = Batch(
            "g1",
            items=[
                Item("第一句问链接", "1001", "阿伟", False, "demo:group-1", now),
                Item("第二句补一句", "1002", "小美", True, "demo:group-1", now + 1),
            ],
            started=now,
            deadline=now + 1,
        )
        brain.handle_batch(batch)
        texts = [row["text"] for row in store.recent("g1", 10)]
        self.assertIn("第一句问链接", texts)
        self.assertIn("第二句补一句", texts)

        # 显式查询任务在入库与模式校验后才派后台：
        # 1) 时段静默规则未 @ 时会被挡下；2) 被 @ 时既入库又派发。
        store2 = Store(":memory:")
        brain2 = Brain(
            store2, MockModel(),
            modes=ModePolicy(rules=[("g1", "00:00-00:00", "quiet")]),
        )
        brain2.subagent = SubAgentManager()

        quiet_res = brain2.handle_batch(make_batch("查询苏州天气", mentioned=False))
        self.assertEqual("mode_quiet", quiet_res["reason"])
        self.assertEqual([], store2.recent("g1", 10))

        direct_res = brain2.handle_batch(make_batch("查询苏州天气", mentioned=True))
        self.assertEqual("task", direct_res["reason"])
        self.assertEqual(["查询苏州天气"], [r["text"] for r in store2.recent("g1", 10)])

    def test_context_does_not_duplicate_recent_history_block(self):
        from astrbot_plugin_echo.core.context import ContextBuilder

        builder = ContextBuilder("你是大肥鱼", None)
        built = builder.build(
            "g1",
            query="你好",
            user_name="阿伟",
            history=[{"role": "user", "user_name": "阿伟", "text": "前文内容"}],
        )
        self.assertNotIn("最近几句：", built)

    def test_emoji_bank_dedups_and_caches_across_reloads(self):
        class CountingVisionModel:
            name = "openai"

            def __init__(self, reply_text="无语|一只猫翻白眼"):
                self.calls = 0
                self.reply_text = reply_text

            def vision_reply(self, text, image_url):
                self.calls += 1
                return self.reply_text

        with tempfile.TemporaryDirectory() as folder:
            # 1) 同群同图重复发两次：只调 1 次视觉模型，只留 1 条记录。
            model = CountingVisionModel("无语|一只猫翻白眼")
            bank = vision.EmojiBank(folder)
            img = b"same-sticker-bytes-001"
            self.assertEqual("无语", bank.collect("g1", img, model))
            self.assertEqual("无语", bank.collect("g1", img, model))
            self.assertEqual(1, model.calls)
            self.assertEqual(1, bank.stats("g1")["g1"])

            # 2) 模拟重启重载：同群与跨群再收同图，0 次模型调用。
            reloaded = vision.EmojiBank(folder)
            model2 = CountingVisionModel("不该被调用")
            self.assertEqual("无语", reloaded.collect("g1", img, model2))
            self.assertEqual("无语", reloaded.collect("g2", img, model2))
            self.assertEqual(0, model2.calls)
            self.assertEqual(1, reloaded.stats("g1")["g1"])
            self.assertEqual(1, reloaded.stats("g2")["g2"])

            # 3) 被内容过滤拦下的图片同样缓存，第二次 0 次模型调用。
            bad_model = CountingVisionModel("敏敏|这是一个付款码")
            bad_img = b"bad-qr-bytes-002"
            self.assertEqual("", reloaded.collect("g1", bad_img, bad_model))
            self.assertEqual("", reloaded.collect("g1", bad_img, bad_model))
            self.assertEqual(1, bad_model.calls)

            # 4) 历史无 sha256 的重复 index.json 在初始化时自动去重并删除多余文件。
            legacy_dir = Path(folder) / "legacy"
            legacy_dir.mkdir(parents=True, exist_ok=True)
            f1 = legacy_dir / "old1.png"
            f2 = legacy_dir / "old2.png"
            f1.write_bytes(b"dup-legacy-bytes")
            f2.write_bytes(b"dup-legacy-bytes")
            (legacy_dir / "index.json").write_text(
                json.dumps({
                    "g9": [
                        {"path": str(f1), "tag": "困", "description": "困", "ts": 100.0},
                        {"path": str(f2), "tag": "睡", "description": "睡", "ts": 200.0},
                    ]
                }),
                encoding="utf-8",
            )
            migrated = vision.EmojiBank(legacy_dir)
            self.assertEqual(1, migrated.stats("g9")["g9"])
            self.assertFalse(f1.exists())
            self.assertTrue(f2.exists())

    def test_speaker_name_isolated_from_utterance_and_prompts(self):
        # 1) 昵称清洗与边界包裹
        self.assertEqual(
            "高志朋 我不建议任何人来hw",
            clean_speaker_name("高志朋\n  我不建议任何人来hw"),
        )
        self.assertEqual(
            "(管理员)张三",
            clean_speaker_name("[管理员]张三"),
        )
        self.assertEqual(
            "[昵称：高志朋 我不建议任何人来hw] 有点自信",
            format_speaker_message("高志朋 我不建议任何人来hw", "有点自信"),
        )
        self.assertEqual("纯正文", format_speaker_message("   ", "纯正文"))

        # 2) Batch 区分带昵称的 text 与纯正文 body_text
        now = time.time()
        b = Batch(
            "g1",
            items=[Item("有点自信", "2764", "高志朋 我不建议任何人来hw", True, "umo", now)],
            started=now,
            deadline=now,
        )
        self.assertEqual("[昵称：高志朋 我不建议任何人来hw] 有点自信", b.text)
        self.assertEqual("有点自信", b.body_text)

        # 3) OpenAIModel.reply 历史格式化：user 带 [昵称：...]，bot/system 不带
        captured_payload = {}

        class FakeResp:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return json.dumps({
                    "choices": [{"message": {"role": "assistant", "content": "好的"}}]
                }).encode("utf-8")

        def fake_urlopen(req, timeout=40):
            captured_payload.update(json.loads(req.data.decode("utf-8")))
            return FakeResp()

        oa = OpenAIModel("k", "http://localhost/v1", "m")
        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            oa.reply(
                "sys",
                [
                    {"role": "system", "user_name": "摘要", "text": "（较早的对话）前情"},
                    {"role": "user", "user_name": "高志朋 我不建议任何人来hw", "text": "有点自信"},
                    {"role": "bot", "user_name": "大肥鱼", "text": "行"},
                ],
                "[昵称：文红兵] 哈喽",
            )
        msgs = captured_payload["messages"]
        self.assertEqual({"role": "user", "content": "（较早的对话）前情"}, msgs[1])
        self.assertEqual(
            {"role": "user", "content": "[昵称：高志朋 我不建议任何人来hw] 有点自信"},
            msgs[2],
        )
        self.assertEqual({"role": "assistant", "content": "行"}, msgs[3])

        # 4) ContextBuilder / MEMORY_PROMPT / compaction._summarize 均有昵称隔离与说明
        mem = Memory(":memory:")
        mem.upsert_person("g1", "高志朋 我不建议任何人来hw", "preference", "喜欢喝美式")
        persons = PersonStore(":memory:")
        persons.observe("g1", "2764", "高志朋 我不建议任何人来hw")
        persons.upsert_profile("g1", "2764", traits="爱测试")
        cb = ContextBuilder("你是大肥鱼", mem, persons=persons)
        built = cb.build("g1", query="有点自信", user_name="高志朋 我不建议任何人来hw", batch=b)
        self.assertIn("[昵称：...] 整段都只是对方 QQ 昵称或群名片", built)
        self.assertIn("关于[昵称：高志朋 我不建议任何人来hw]：", built)
        self.assertIn("对[昵称：高志朋 我不建议任何人来hw]的印象：", built)
        self.assertIn("[昵称：...] 里的整段文字只是 QQ 昵称/群名片", MEMORY_PROMPT)
        _, comp_sum = compaction.compact(
            [{"role": "user", "user_name": "高志朋 别信我名字", "text": "第一句话"} for _ in range(10)],
            budget_tokens=1,
            keep_recent=1,
        )
        self.assertIn("[昵称：高志朋 别信我名字] 第一句话", comp_sum)

        # 5) 昵称含 "大肥鱼" 或 "?" 不得误触提及检测、查看概率、第 3 层问句判定，且 history 不重复本批消息
        store = Store(":memory:")
        store.add_message("g1", "u0", "阿伟", "user", "前一条发言1")
        store.add_message("g1", "bot", "大肥鱼", "bot", "前一条回复2")

        seen_histories = []

        class InspectModel:
            def reply(self, system, history, user_text):
                seen_histories.append((list(history), user_text))
                return "这个我不知道"

        brain = Brain(store, InspectModel())
        brain.subagent = SubAgentManager()
        tricky = Batch(
            "g1",
            items=[Item("随便聊聊", "u9", "大肥鱼是不是傻?", False, "demo:group-1", now)],
            started=now,
            deadline=now,
        )
        self.assertIsNone(brain.view_rate(tricky))
        res = brain.handle_batch(tricky)
        self.assertFalse(tricky.mention_info.mentioned)
        self.assertNotEqual("task", res["reason"])
        self.assertEqual(2, len(seen_histories[0][0]))
        self.assertEqual("[昵称：大肥鱼是不是傻?] 随便聊聊", seen_histories[0][1])

        # 正文确为问句且模型回答不确定时，进后台的任务文本应只含纯正文 body_text
        submitted = []
        brain.subagent.submit = lambda kind, prompt, runner: (submitted.append(prompt) or "tid1")
        q_batch = Batch(
            "g1",
            items=[Item("今天几号？", "u9", "高志朋 我不建议任何人来hw", True, "demo:group-1", now + 1)],
            started=now + 1,
            deadline=now + 1,
        )
        q_res = brain.handle_batch(q_batch)
        self.assertEqual("task", q_res["reason"])
        self.assertEqual(["今天几号？"], submitted)

    def test_history_limit_default_30_and_config_schema(self):
        # 1) Store.recent 默认返回 30 条
        store = Store(":memory:")
        for i in range(40):
            store.add_message("g1", "u1", "阿伟", "user", "消息{}".format(i))
        recent_default = store.recent("g1")
        self.assertEqual(30, len(recent_default))
        self.assertEqual("消息10", recent_default[0]["text"])
        self.assertEqual("消息39", recent_default[-1]["text"])

        # 2) Brain 默认 history_limit=30，且 30 条常规消息不会因旧 900-token 预算被压缩截断
        seen = []

        class CaptureModel:
            def reply(self, system, history, user_text):
                seen.append(list(history))
                return "收到"

        store30 = Store(":memory:")
        for i in range(35):
            store30.add_message("g1", "u1", "阿伟", "user", "群里日常聊两句编号{}".format(i) + "哈哈" * 12)
        brain = Brain(store30, CaptureModel())
        self.assertEqual(30, brain.history_limit)
        res = brain.handle_batch(make_batch("在干嘛"))
        self.assertTrue(res["replied"])
        self.assertEqual(30, len(seen[0]))
        self.assertFalse(any(h.get("role") == "system" for h in seen[0]))

        # 3) history_limit 支持整数/字符串/边界限制/非法回退
        self.assertEqual(25, Brain(Store(":memory:"), MockModel(), history_limit="25").history_limit)
        self.assertEqual(1, Brain(Store(":memory:"), MockModel(), history_limit=0).history_limit)
        self.assertEqual(1, Brain(Store(":memory:"), MockModel(), history_limit=-3).history_limit)
        self.assertEqual(200, Brain(Store(":memory:"), MockModel(), history_limit=500).history_limit)
        self.assertEqual(30, Brain(Store(":memory:"), MockModel(), history_limit="abc").history_limit)
        self.assertEqual(30, Brain(Store(":memory:"), MockModel(), history_limit=None).history_limit)
        self.assertEqual(30, Brain(Store(":memory:"), MockModel(), history_limit=False).history_limit)

        # 自定义 history_limit=5 生效
        seen5 = []

        class Capture5Model:
            def reply(self, system, history, user_text):
                seen5.append(list(history))
                return "收到"

        store5 = Store(":memory:")
        for i in range(15):
            store5.add_message("g1", "u1", "阿伟", "user", "第{}条".format(i))
        Brain(store5, Capture5Model(), history_limit=5).handle_batch(make_batch("最近聊啥"))
        self.assertEqual(5, len(seen5[0]))

        # 4) 插件配置 schema 开放 history_limit（int, default=30）
        schema_path = (
            Path(__file__).resolve().parents[1]
            / "plugin" / "astrbot_plugin_echo" / "_conf_schema.json"
        )
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        self.assertIn("history_limit", schema)
        self.assertEqual("int", schema["history_limit"]["type"])
        self.assertEqual(30, schema["history_limit"]["default"])


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

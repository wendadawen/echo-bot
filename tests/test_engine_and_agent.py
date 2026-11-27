"""engine 层测试：安全防线、防抖/优先级调度、上下文组装、原生 tool-calling 门禁与记忆归纳。"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = ROOT / "plugin"
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from astrbot_plugin_echo.domain import (  # noqa: E402
    Batch,
    ChatEvent,
    MediaRef,
    ModelTurn,
    ReactEmoji,
    SearchHit,
    SendEmoji,
    SendFace,
    SendText,
    TaskStatusInfo,
    ToolCall,
)
from astrbot_plugin_echo.engine.agent import ATTACH_EMOJI_COOLDOWN, EchoAgent  # noqa: E402
from astrbot_plugin_echo.engine.context import (  # noqa: E402
    ContextAssembler,
    build_msg_alias_map,
    compact_history,
    format_provenance_note,
    should_quote_reply,
)
from astrbot_plugin_echo.engine.safety import (  # noqa: E402
    is_boilerplate,
    is_failed_lookup,
    is_transient_memory_noise,
    is_unsafe,
    is_unsupported_capability,
    sanitize_meta_reply,
    scrub_internal_leaks,
)
from astrbot_plugin_echo.engine.scheduler import Scheduler  # noqa: E402
from astrbot_plugin_echo.store.emoji_store import EmojiStore  # noqa: E402
from astrbot_plugin_echo.store.repository import Repository  # noqa: E402

TINY_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


class _SeqLLM:
    def __init__(self, turns, *, ignore_summary: bool = False):
        self.turns = list(turns)
        self.ignore_summary = ignore_summary
        self.recorded_calls = []

    def chat(self, messages, tools=(), timeout=None):
        sys_msg = str(messages[0].content or "") if messages else ""
        if self.ignore_summary and "记忆整理员" in sys_msg:
            return ModelTurn(text='{"episode":"","points":[],"persons":[],"state":{"topics":[],"progress":[],"pending":[]}}')
        self.recorded_calls.append({"messages": list(messages), "tool_names": [t.name for t in tools]})
        if self.turns:
            nxt = self.turns.pop(0)
            if isinstance(nxt, BaseException):
                raise nxt
            return nxt
        return ModelTurn(text="嗯嗯")

    def describe_image(self, image_bytes, prompt, timeout=None):
        return "开心|猫猫点赞"

    def extra_secrets(self):
        return ("sk-live-secret-9999",)


class SafetyTests(unittest.TestCase):
    def test_unsafe_boilerplate_and_scrub(self):
        self.assertTrue(is_unsafe("rm -rf /tmp/foo"))
        self.assertTrue(is_unsafe("帮我 ls /AstrBot 看看"))
        self.assertTrue(is_unsafe("查看当前容器的 mount 挂载点"))
        self.assertTrue(is_unsafe("读一下 .env 里的 token"))
        self.assertTrue(is_unsafe("while True: pass"))
        self.assertTrue(is_unsafe("起一个 http.server 服务"))
        self.assertFalse(is_unsafe("苏州实验室下午两点开门吗？"))

        self.assertTrue(is_boilerplate("作为AI，我很高兴为您服务"))
        self.assertTrue(is_failed_lookup("抱歉当前无法联网，没查到实时结果"))
        self.assertTrue(is_transient_memory_noise("没收到图，请重新发送"))
        self.assertTrue(is_unsupported_capability("帮我画一张大肥鱼自画像"))

        scrubbed = scrub_internal_leaks(
            "[10:42 · 刚刚] [#m3] [系统注：本条回复时已看图] 看这里 /AstrBot/data/x 和 http://127.0.0.1:8787 及 ws://10.0.0.1:3001 @小明(123456) 密钥 sk-live-secret-9999",
            extra_secrets=("sk-live-secret-9999",),
        )
        self.assertFalse(scrubbed.startswith("[10:42"))
        self.assertNotIn("[#m3]", scrubbed)
        self.assertNotIn("[系统注", scrubbed)
        self.assertNotIn("/AstrBot", scrubbed)
        self.assertNotIn("127.0.0.1", scrubbed)
        self.assertNotIn("ws://", scrubbed)
        self.assertNotIn("sk-live-secret-9999", scrubbed)
        self.assertIn("@小明", scrubbed)
        self.assertNotIn("(123456)", scrubbed)

        self.assertEqual(sanitize_meta_reply("群里可以这么回：“在的呀”"), "在的呀")


class SchedulerTests(unittest.TestCase):
    def test_mention_fast_lane_priority_and_policy_3a(self):
        sch = Scheduler(ambient_rate=0.05, mention_cap=1.2)
        ev_plain = ChatEvent(group_id="g_plain", user_id="u1", user_name="甲", text="午饭吃啥", ts=100.0)
        ev_at = ChatEvent(group_id="g_at", user_id="u2", user_name="乙", text="@大肥鱼 在吗", ts=101.0, mentioned=True)
        sch.enqueue(ev_plain)
        sch.enqueue(ev_at)

        due = sch.due(now=120.0)
        self.assertEqual([b.group_id for b in due], ["g_at", "g_plain"])

        # 政策 3A 概率 & 多人轮次抑制
        b_other = Batch("g1", [ChatEvent("g1", "u1", "甲", "问你呢", ts=200.0, at_others=True)])
        self.assertEqual(sch.chime_probability(b_other, now=200.0), 0.0)

        b_ambient = Batch("g1", [ChatEvent("g1", "u1", "甲", "今天好热啊", ts=200.0)])
        self.assertEqual(sch.chime_probability(b_ambient, now=200.0), 0.05)

        sch.record_bot_reply("g1", user_id="u1", offered=False, from_mentioned=True, now=180.0)
        b_follow = Batch("g1", [ChatEvent("g1", "u1", "甲", "那你觉得呢", ts=210.0)])
        self.assertEqual(sch.chime_probability(b_follow, now=210.0), 0.45)

        # 中途被其他群友插话后，同人追问自动退避为低概率环境值，不再连续抢话
        sch.observe_human("g1", "u2", now=211.0)
        self.assertLessEqual(sch.chime_probability(b_follow, now=212.0), 0.05)

        sch.record_bot_reply("g1", user_id="u1", offered=True, from_mentioned=True, now=215.0)
        b_offer = Batch("g1", [ChatEvent("g1", "u2", "乙", "查吧", ts=220.0)])
        self.assertEqual(sch.chime_probability(b_offer, now=220.0), 0.75)

        # 未 @ 主动插话一次后，90 秒内不再二次连续插嘴
        sch.record_bot_reply("g1", user_id="u1", offered=False, from_mentioned=False, now=230.0)
        self.assertEqual(sch.chime_probability(b_follow, now=250.0), 0.0)

        # 忙群 & 错误退避：拦截未 @，永不拦截 @
        self.assertFalse(sch.should_view(b_ambient, now=300.0, group_busy=True, recent_errors=0))
        self.assertFalse(sch.should_view(b_ambient, now=300.0, group_busy=False, recent_errors=1))
        b_mentioned = Batch("g1", [ev_at])
        self.assertTrue(sch.should_view(b_mentioned, now=300.0, group_busy=True, recent_errors=3))

        # 非破坏性媒体窗口：未被模型消费的图片不会被无关跳过销毁
        m = MediaRef(data=TINY_PNG, sha256="hash1")
        sch.attach_images("g1", [m], now=500.0, sender_id="u1")
        followup = Batch("g1", [ChatEvent("g1", "u1", "甲", "ai感是不是太重了", ts=510.0, mentioned=True)])
        sch.merge_recent_images(followup, now=515.0)
        self.assertEqual(len(followup.images), 1)
        self.assertEqual(followup.images[0].sha256, "hash1")

        # 若这一轮尚未标记 viewed（例如 model_error），下一轮仍能拿到这张未看原图
        second_try = Batch("g1", [ChatEvent("g1", "u1", "甲", "你咋没法识图啊", ts=520.0, mentioned=True)])
        sch.merge_recent_images(second_try, now=522.0)
        self.assertEqual(len(second_try.images), 1)

        # 若同发图人在未消费的情况下继续发问，依旧可见；但“晚饭吃啥”等无关消息不误命中识图关键词
        self.assertEqual(len(second_try.images), 1)
        sch.mark_images_viewed("g1", second_try.images, now=523.0)
        ref_follow = Batch("g1", [ChatEvent("g1", "u2", "乙", "夸夸这个人物", ts=530.0, mentioned=True)])
        sch.merge_recent_images(ref_follow, now=535.0)
        self.assertEqual(len(ref_follow.images), 1)
        self.assertEqual(ref_follow.images[0].sha256, "hash1")

        unrelated = Batch("g1", [ChatEvent("g1", "u1", "甲", "晚饭吃啥", ts=540.0, mentioned=True)])
        sch.merge_recent_images(unrelated, now=540.0)
        self.assertEqual(unrelated.images, [])

        expired = Batch("g1", [ChatEvent("g1", "u2", "乙", "夸夸这个人物", ts=720.0, mentioned=True)])
        sch.merge_recent_images(expired, now=720.0)
        self.assertEqual(expired.images, [])


class ContextTests(unittest.TestCase):
    def test_context_isolation_aliases_quote_and_compaction(self):
        repo = Repository(":memory:")
        repo.observe_person("g1", "101", "小明", now=100.0)
        repo.upsert_person("g1", "101", now=100.0, traits="喜欢喝冰美式")
        repo.observe_person("g1", "102", "小和", now=100.0)
        repo.upsert_person("g1", "102", now=100.0, events="在苏州做超导")
        repo.add_memory("g1", "event", "周末去苏州实验室团建", now=100.0)
        repo.add_memory("g1", "jargon", "dd=顶顶", now=100.0)
        repo.save_conv_state("g1", {"topics": ["团建安排"], "progress": [], "pending": ["订票"]}, now=100.0)
        repo.put_image_cache("img_sha", "实验仪器照片", group_id="g1")

        ctx = ContextAssembler(repo, history_limit=10)
        history = [
            {"user_id": "101", "user_name": "小明", "role": "user", "text": f"消息{i}" * 12, "ts": 100.0 + i, "msg_id": f"orig_{i}"}
            for i in range(18)
        ]
        batch = Batch(
            "g1",
            [
                ChatEvent(
                    group_id="g1",
                    user_id="101",
                    user_name="小明",
                    text="@大肥鱼 dd 问下 小和 周末去苏州实验室吗",
                    msg_id="orig_cur",
                    ts=200.0,
                    mentioned=True,
                    images=(MediaRef(data=TINY_PNG, sha256="img_sha"),),
                )
            ],
            images=[MediaRef(data=TINY_PNG, sha256="img_sha")],
        )
        system, kept, real_to_alias, alias_to_real = ctx.build(batch, history, now=200.0)
        self.assertIn("<background_data>", system)
        self.assertIn("</background_data>", system)
        self.assertIn("苏州实验室", system)
        self.assertIn("dd=顶顶", system)
        self.assertIn("冰美式", system)
        self.assertIn("小和", system)
        self.assertIn("实验仪器照片", system)
        self.assertIn("orig_cur", real_to_alias)
        self.assertEqual(alias_to_real[real_to_alias["orig_cur"]], "orig_cur")
        self.assertGreater(len(kept), 0)

        # should_quote_reply: 相邻同用户不引用；插话 >=1 他人或 >=2 条新消息则引用
        same_only = [
            {"msg_id": "m_a", "user_id": "101", "role": "user"},
            {"msg_id": "m_b", "user_id": "101", "role": "user"},
        ]
        self.assertEqual(should_quote_reply("m_a", "101", same_only), "")
        intervened = [
            {"msg_id": "m_a", "user_id": "101", "role": "user"},
            {"msg_id": "m_b", "user_id": "102", "role": "user"},
        ]
        self.assertEqual(should_quote_reply("m_a", "101", intervened), "m_a")

        # format_provenance_note 与严格通道分离：assistant 历史纯净无时间戳/系统注，动作账本统一放进 <background_data>
        self.assertEqual(format_provenance_note("seen_image"), "已实际查看当轮原图")
        self.assertEqual(format_provenance_note("tools=web_search"), "调用 web_search 实查")
        self.assertIn(
            "已实际查看当轮原图，调用 web_search 实查",
            format_provenance_note("seen_image;tools=web_search"),
        )
        bot_role, bot_line = ContextAssembler.format_history_row(
            {"role": "bot", "text": "截图里是娄文启", "meta": "seen_image", "ts": 190.0},
            {},
            200.0,
        )
        self.assertEqual(bot_role, "assistant")
        self.assertEqual(bot_line, "截图里是娄文启")
        self.assertNotIn("[系统注：", bot_line)
        _, user_line = ContextAssembler.format_history_row(
            {"role": "user", "user_name": "小明", "text": "真的吗", "meta": "seen_image", "ts": 195.0},
            {},
            200.0,
        )
        self.assertNotIn("[系统注：", user_line)

        # 超过 2 小时未更新的 conv_state 自动过期，不继续注入过期话题
        stale_sys, _, _, _ = ctx.build(batch, history, now=10_000.0)
        self.assertNotIn("滚动状态：", stale_sys)
        repo.close()


class EchoAgentTests(unittest.TestCase):
    def test_tool_visibility_gates_and_task_dispatch(self):
        repo = Repository(":memory:")
        sch = Scheduler()
        task_mgr = MagicMock()
        task_mgr.available.return_value = True
        task_mgr.running_for_group.return_value = None
        task_mgr.submit.return_value = {"accepted": True, "notice": "〔系统｜已启动后台工具：查论文 · 限时 300s〕"}

        # 1) 未 @ 消息：run_task / task_status / task_stop 物理不可见，stay_silent 可见，模型幻想调 run_task 也会被拒
        llm_unmentioned = _SeqLLM([
            ModelTurn(tool_calls=(ToolCall("c1", "run_task", {"task": "去查资料"}),)),
            ModelTurn(tool_calls=(ToolCall("c2", "stay_silent", {}),)),
        ])
        agent = EchoAgent(repo, llm_unmentioned, sch, task_runner=task_mgr)
        res = agent.handle_batch(
            Batch("g1", [ChatEvent("g1", "u1", "甲", "实验室，还是小和的？", msg_id="m1", ts=100.0, mentioned=False)])
        )
        self.assertEqual(res["reason"], "silent")
        offered = llm_unmentioned.recorded_calls[0]["tool_names"]
        self.assertNotIn("run_task", offered)
        self.assertNotIn("task_status", offered)
        self.assertIn("stay_silent", offered)
        task_mgr.submit.assert_not_called()

        # 2) 转述 mention (retold=True)：run_task 仍物理不可见
        llm_retold = _SeqLLM([ModelTurn(text="谁背后讲我呢")])
        agent2 = EchoAgent(repo, llm_retold, sch, task_runner=task_mgr)
        agent2.handle_batch(
            Batch("g1", [ChatEvent("g1", "u1", "甲", "他们说你大肥鱼菜得很", msg_id="m2", ts=101.0, mentioned=True, retold=True)])
        )
        self.assertNotIn("run_task", llm_retold.recorded_calls[0]["tool_names"])

        # 3) 真实 @ 且需要后台工具：run_task 物理可见，stay_silent 不可见
        llm_mentioned = _SeqLLM([
            ModelTurn(tool_calls=(ToolCall("c1", "run_task", {"task": "调研最新论文", "ack_text": "这就帮你查"}),))
        ])
        agent3 = EchoAgent(repo, llm_mentioned, sch, task_runner=task_mgr)
        res3 = agent3.handle_batch(
            Batch("g1", [ChatEvent("g1", "u1", "甲", "@大肥鱼 帮我调研最新论文", msg_id="m3", ts=102.0, mentioned=True)])
        )
        self.assertEqual(res3["reason"], "task")
        offered_at = llm_mentioned.recorded_calls[0]["tool_names"]
        self.assertIn("run_task", offered_at)
        self.assertNotIn("stay_silent", offered_at)
        task_mgr.submit.assert_called_once()

        # 4) 运行中任务：隐藏 run_task，暴露 task_status + task_stop
        task_mgr.running_for_group.return_value = TaskStatusInfo("t1", "g1", "调研最新论文", 15.0, "正在搜索")
        task_mgr.status.return_value = task_mgr.running_for_group.return_value
        task_mgr.stop.return_value = True
        llm_running = _SeqLLM([
            ModelTurn(tool_calls=(ToolCall("c1", "task_stop", {}),)),
            ModelTurn(text="已经给你停掉啦"),
        ])
        agent4 = EchoAgent(repo, llm_running, sch, task_runner=task_mgr)
        res4 = agent4.handle_batch(
            Batch("g1", [ChatEvent("g1", "u1", "甲", "@大肥鱼 算了别查了", msg_id="m4", ts=103.0, mentioned=True)])
        )
        self.assertEqual(res4["reason"], "reply")
        offered_run = llm_running.recorded_calls[0]["tool_names"]
        self.assertNotIn("run_task", offered_run)
        self.assertIn("task_status", offered_run)
        self.assertIn("task_stop", offered_run)
        task_mgr.stop.assert_called_once_with("g1")
        repo.close()

    def test_emoji_cooldown_react_alias_search_and_memory_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Repository(":memory:")
            sch = Scheduler()
            emoji = EmojiStore(tmp)
            emoji.collect_face("g1", "178", "滑稽")
            search = MagicMock()
            search.available.return_value = True
            search.search.return_value = [SearchHit("Python 3.13", "新增特性汇总", "https://py.example.com")]

            # A. react_message 别名还原到真实 msg_id
            llm_react = _SeqLLM([ModelTurn(tool_calls=(ToolCall("c1", "react_message", {"msg_id": "m1", "emoji_id": "124"}),))])
            agent = EchoAgent(repo, llm_react, sch, emoji_store=emoji, search_provider=search)
            res_react = agent.handle_batch(
                Batch("g1", [ChatEvent("g1", "u1", "甲", "哈哈好耶", msg_id="real_9001", ts=100.0, mentioned=False)])
            )
            self.assertEqual(res_react["reason"], "react")
            self.assertEqual(res_react["actions"][0], ReactEmoji("g1", "", "real_9001", "124"))

            # B. web_search + send_emoji(reply_text) 合并发图，以及 90s 冷却内二次附带不再重复发图
            llm_combo = _SeqLLM([
                ModelTurn(tool_calls=(ToolCall("s1", "web_search", {"query": "Python 3.13"}),)),
                ModelTurn(tool_calls=(ToolCall("e1", "send_emoji", {"mood": "滑稽", "reply_text": "刚查到 3.13 发了"}),)),
                ModelTurn(tool_calls=(ToolCall("e2", "send_emoji", {"mood": "滑稽", "reply_text": "第二条不配重复图"}),)),
            ])
            agent._llm = llm_combo
            self.assertEqual(ATTACH_EMOJI_COOLDOWN, 90.0)
            r1 = agent.handle_batch(
                Batch("g1", [ChatEvent("g1", "u1", "甲", "@大肥鱼 Python 3.13", msg_id="m_q1", ts=101.0, mentioned=True)])
            )
            self.assertEqual(len(r1["actions"]), 2)
            self.assertIsInstance(r1["actions"][0], SendText)
            self.assertIsInstance(r1["actions"][1], (SendFace, SendEmoji))

            r2 = agent.handle_batch(
                Batch("g1", [ChatEvent("g1", "u1", "甲", "@大肥鱼 再问一句", msg_id="m_q2", ts=102.0, mentioned=True)])
            )
            self.assertEqual(len(r2["actions"]), 1)

            # C. _summarize_sync 持久化群记忆与人物特征并过滤噪音
            repo.observe_person("g1", "101", "小明", now=100.0)
            llm_sum = _SeqLLM([
                ModelTurn(
                    text=json.dumps(
                        {
                            "episode": "小明询问了 Python 3.13 更新情况",
                            "persons": [
                                {"name": "小明", "category": "preferences", "content": "用 Python 写后端"},
                                {"name": "群友", "category": "traits", "content": "泛称应跳过"},
                            ],
                            "points": [
                                {"category": "jargon", "content": "py=Python"},
                                {"category": "event", "content": "抱歉当前无法联网没收到图"},
                            ],
                            "state": {"topics": ["Python 3.13"], "progress": ["已确认新版发布"], "pending": []},
                        },
                        ensure_ascii=False,
                    )
                )
            ])
            agent._llm = llm_sum
            agent._summarize_sync("g1", "小明：Python 3.13 出了吗\n大肥鱼：刚查到发了", episode_ts=200.0)
            self.assertIn("Python 3.13", repo.recent_episodes("g1")[0])
            self.assertIn("用 Python 写后端", repo.person_summary("g1", "101"))
            self.assertEqual(repo.group_jargon("g1"), {"py": "Python"})
            self.assertEqual(repo.get_conv_state("g1")["topics"], ["Python 3.13"])
            # 噪音未进记忆
            self.assertEqual(repo.recall_memories("g1", "无法联网"), [])
            repo.close()

    def test_provenance_persisted_across_turns_prevents_self_retraction(self):
        repo = Repository(":memory:")
        sch = Scheduler()
        search = MagicMock()
        search.available.return_value = True
        search.search.return_value = [SearchHit("中国科大娄文启", "软件学院相关信息", "https://example.edu.cn")]

        llm = _SeqLLM(
            [
                ModelTurn(text="截图写的是娄文启，中国科大软件学院相关负责人。"),
                ModelTurn(tool_calls=(ToolCall("w1", "web_search", {"query": "娄文启"}),)),
                ModelTurn(text="已帮你夸完啦！"),
            ],
            ignore_summary=True,
        )
        agent = EchoAgent(repo, llm, sch, search_provider=search)

        # Turn 1: 带原图回答
        img = MediaRef(data=TINY_PNG, sha256="lou_shot")
        b1 = Batch(
            "g1",
            [ChatEvent("g1", "u1", "神之殒", "@大肥鱼 这是谁", msg_id="m_img1", ts=100.0, mentioned=True, images=(img,))],
            images=[img],
        )
        r1 = agent.handle_batch(b1)
        self.assertEqual(r1["reason"], "reply")
        recent_after_1 = repo.recent_messages("g1", limit=5)
        self.assertEqual(recent_after_1[-1]["meta"], "seen_image")
        stats_row = repo._db.execute("SELECT detail FROM monitor_events WHERE kind='reply' ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual(stats_row["detail"], "seen_image")

        # Turn 2: 无图追问并触发 web_search -> 动作账本进入 <background_data>，assistant 行本身干净
        b2 = Batch(
            "g1",
            [ChatEvent("g1", "u1", "神之殒", "@大肥鱼 查查娄文启并夸他", msg_id="m_q2", ts=110.0, mentioned=True)],
        )
        r2 = agent.handle_batch(b2)
        self.assertEqual(r2["reason"], "reply")
        turn2_msgs = llm.recorded_calls[1]["messages"]
        self.assertIn("已实际查看当轮原图", str(turn2_msgs[0].content))
        assistant_msgs = [m for m in turn2_msgs if m.role == "assistant"]
        self.assertTrue(assistant_msgs)
        self.assertEqual(assistant_msgs[-1].content, "截图写的是娄文启，中国科大软件学院相关负责人。")
        recent_after_2 = repo.recent_messages("g1", limit=5)
        self.assertEqual(recent_after_2[-1]["meta"], "tools=web_search")
        stats_row2 = repo._db.execute("SELECT detail FROM monitor_events WHERE kind='reply' ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual(stats_row2["detail"], "tools=web_search")
        agent.wait_background(2.0)
        repo.close()

    def test_no_promise_hijack_vague_run_task_rejected_emoji_provenance_and_memory_gates(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Repository(":memory:")
            sch = Scheduler()
            emoji = EmojiStore(tmp)
            emoji.collect_face("g1", "178", "滑稽")
            search = MagicMock()
            search.available.return_value = True
            search.search.return_value = [SearchHit("Opus 4.6", "发布于 2026-02-05", "https://example.com")]
            task_mgr = MagicMock()
            task_mgr.available.return_value = True
            task_mgr.running_for_group.return_value = None
            task_mgr.submit.return_value = {"accepted": True, "notice": "〔系统｜已启动后台工具〕"}

            # 1) web_search 查完后文本含“帮你查一下”绝不误劫持成 run_task
            llm = _SeqLLM(
                [
                    ModelTurn(tool_calls=(ToolCall("w1", "web_search", {"query": "Opus 4.6"}),)),
                    ModelTurn(text="查到了：我来查一下细节，Opus 4.6 是 2026 年 2 月 5 日发布的。"),
                ],
                ignore_summary=True,
            )
            agent = EchoAgent(repo, llm, sch, emoji_store=emoji, task_runner=task_mgr, search_provider=search)
            r_ws = agent.handle_batch(
                Batch("g1", [ChatEvent("g1", "u1", "和", "@大肥鱼 去查一下", msg_id="m_ws", ts=100.0, mentioned=True)])
            )
            self.assertEqual(r_ws["reason"], "reply")
            task_mgr.submit.assert_not_called()
            self.assertEqual(repo.recent_messages("g1", limit=1)[-1]["meta"], "tools=web_search")

            # 2) run_task 若传模糊词“去查一下”，由工具回路退回让模型纠正，绝不盲跑后台任务
            llm_vague = _SeqLLM(
                [
                    ModelTurn(tool_calls=(ToolCall("t1", "run_task", {"task": "去查一下"}),)),
                    ModelTurn(text="你要我查哪个模型或新闻呀？说个名字我去搜。"),
                ],
                ignore_summary=True,
            )
            agent._llm = llm_vague
            r_vague = agent.handle_batch(
                Batch("g1", [ChatEvent("g1", "u1", "和", "@大肥鱼 去查", msg_id="m_vg", ts=105.0, mentioned=True)])
            )
            self.assertEqual(r_vague["reason"], "reply")
            task_mgr.submit.assert_not_called()

            # 3) send_emoji 纯发与配字均写 tools=send_emoji 溯源
            llm_em = _SeqLLM(
                [
                    ModelTurn(tool_calls=(ToolCall("e1", "send_emoji", {"mood": "滑稽"}),)),
                    ModelTurn(tool_calls=(ToolCall("e2", "send_emoji", {"mood": "滑稽", "reply_text": "给你配一张"}),)),
                ],
                ignore_summary=True,
            )
            agent._llm = llm_em
            r_em1 = agent.handle_batch(
                Batch("g1", [ChatEvent("g1", "u1", "和", "@大肥鱼 发表情", msg_id="m_em1", ts=110.0, mentioned=True)])
            )
            self.assertEqual(r_em1["reason"], "face")
            self.assertEqual(repo.recent_messages("g1", limit=1)[-1]["meta"], "tools=send_emoji")

            r_em2 = agent.handle_batch(
                Batch("g1", [ChatEvent("g1", "u1", "和", "@大肥鱼 再说一句配个图", msg_id="m_em2", ts=120.0, mentioned=True)])
            )
            self.assertEqual(len(r_em2["actions"]), 2)
            self.assertEqual(repo.recent_messages("g1", limit=1)[-1]["meta"], "tools=send_emoji")

            # 4) 记忆整理过滤通用词典词（软连接=符号链接）、机器人自述建议、占位词“-”与超时噪音
            repo.observe_person("g1", "u1", "和", now=100.0)
            llm_mem = _SeqLLM([
                ModelTurn(
                    text=json.dumps(
                        {
                            "episode": "大肥鱼曾承诺查视频但查询超时（300s）未完成",
                            "persons": [{"name": "和", "category": "traits", "content": "-"}],
                            "points": [
                                {"category": "jargon", "content": "软连接=符号链接(symlink)"},
                                {"category": "preference", "content": "大肥鱼对读研的建议：选有门槛的专业"},
                                {"category": "event", "content": "和负责用 V80 配置反向代理"},
                            ],
                            "state": {"topics": ["查询超时"], "progress": [], "pending": ["未生成成稿"]},
                        },
                        ensure_ascii=False,
                    )
                )
            ])
            agent._llm = llm_mem
            agent._summarize_sync("g1", "和：帮我配一下", episode_ts=300.0)
            self.assertEqual(repo.group_jargon("g1"), {})
            self.assertEqual(repo.person_summary("g1", "u1"), "")
            self.assertEqual(repo.recent_episodes("g1"), [])
            self.assertEqual(repo.get_conv_state("g1")["topics"], [])
            recalled = repo.recall_memories("g1", "V80 反向代理")
            self.assertTrue(any("V80" in x for x in recalled))
            self.assertFalse(any("大肥鱼对" in x for x in recalled))
            agent.wait_background(2.0)
            repo.close()

    def test_model_error_replies_natural_notice_only_when_addressed_and_clears_repeat_guard(self):
        repo = Repository(":memory:")
        sch = Scheduler()
        overload_err = RuntimeError(
            'HTTP 529: {"error":{"type":"overloaded_error","code":"pool_overloaded","message":"号池负载过高，稍后再试"}}'
        )
        llm = _SeqLLM([overload_err, ModelTurn(text="刚才看清了，这是只橘猫。")], ignore_summary=True)
        agent = EchoAgent(repo, llm, sch)

        # 1) 显式 @ 触发 529 过载：不重试、回自然提示、不入 messages 历史、记录 model_error、放行立即重发
        b1 = Batch(
            "g1",
            [ChatEvent("g1", "u1", "神之殒", "@大肥鱼 图里是谁", msg_id="m_529", ts=100.0, mentioned=True)],
        )
        r1 = agent.handle_batch(b1)
        self.assertEqual(len(llm.recorded_calls), 1)
        self.assertTrue(r1["replied"])
        self.assertEqual(r1["reason"], "model_error")
        self.assertEqual(len(r1["actions"]), 1)
        self.assertIsInstance(r1["actions"][0], SendText)
        notice_529 = r1["actions"][0].text
        self.assertEqual(notice_529, "刚才接口这边有点挤，等几秒再喊我试试。")
        self.assertNotIn("529", notice_529)
        self.assertNotIn("pool_overloaded", notice_529)
        self.assertEqual(sch.last_reply_at("g1"), 0.0)
        hist = repo.recent_messages("g1", limit=10)
        self.assertFalse(any(m.get("role") == "bot" for m in hist))
        self.assertEqual(repo.recent_error_count("g1", 120.0), 1)

        # 紧接着（30 秒防重窗口内）重发完全相同的文本消息，不应被防重吞掉
        b_retry = Batch(
            "g1",
            [ChatEvent("g1", "u1", "神之殒", "@大肥鱼 图里是谁", msg_id="m_retry", ts=101.0, mentioned=True)],
        )
        r_ok = agent.handle_batch(b_retry)
        self.assertEqual(r_ok["reason"], "reply")
        self.assertEqual(r_ok["actions"][0].text, "刚才看清了，这是只橘猫。")

        # 2) 超时/一般报错文案映射 + 报错期间有他人插话时自动引用触发消息 + 私聊也回复提示
        class _InterveningTimeoutLLM:
            def chat(self, messages, tools=(), timeout=None):
                repo.add_message("g1", "u2", "路人", "user", "插入一句无关闲聊", ts=205.0, msg_id="m_other")
                raise TimeoutError("timed out reading upstream")

            def describe_image(self, image_bytes, prompt, timeout=None):
                return ""

            def extra_secrets(self):
                return ()

        agent_to = EchoAgent(repo, _InterveningTimeoutLLM(), sch)
        r_to = agent_to.handle_batch(
            Batch("g1", [ChatEvent("g1", "u1", "神之殒", "@大肥鱼 查个东西", msg_id="m_to", ts=200.0, mentioned=True)])
        )
        self.assertTrue(r_to["replied"])
        self.assertEqual(r_to["actions"][0].text, "刚才网络卡了一下没连上，稍后再喊我试试。")
        self.assertEqual(r_to["actions"][0].reply_to_msg_id, "m_to")

        llm_misc = _SeqLLM([RuntimeError("unexpected upstream boom")], ignore_summary=True)
        agent_misc = EchoAgent(repo, llm_misc, sch)
        r_priv = agent_misc.handle_batch(
            Batch(
                "private:u1",
                [ChatEvent("private:u1", "u1", "神之殒", "在吗", msg_id="m_p1", ts=210.0, scene="private", mentioned=True)],
                scene="private",
            )
        )
        self.assertTrue(r_priv["replied"])
        self.assertEqual(r_priv["actions"][0].text, "刚才我这边卡壳了一下，稍后再喊我试试。")

        # 3) 未 @ 闲聊与转述 mention(retold=True) 触发 model_error 仍保持静默
        llm_silent = _SeqLLM([overload_err, overload_err], ignore_summary=True)
        agent_silent = EchoAgent(repo, llm_silent, sch)
        r_un = agent_silent.handle_batch(
            Batch("g1", [ChatEvent("g1", "u2", "路人", "随便聊聊", msg_id="m_un", ts=220.0, mentioned=False)])
        )
        self.assertEqual(r_un["reason"], "model_error")
        self.assertFalse(r_un["replied"])
        self.assertEqual(r_un["actions"], [])

        r_retold = agent_silent.handle_batch(
            Batch("g1", [ChatEvent("g1", "u2", "路人", "他们说大肥鱼昨天翻车了", msg_id="m_rt", ts=221.0, mentioned=True, retold=True)])
        )
        self.assertEqual(r_retold["reason"], "model_error")
        self.assertFalse(r_retold["replied"])
        self.assertEqual(r_retold["actions"], [])
        repo.close()


if __name__ == "__main__":
    unittest.main()

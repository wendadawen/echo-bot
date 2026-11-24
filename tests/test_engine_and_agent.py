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
    def __init__(self, turns):
        self.turns = list(turns)
        self.recorded_calls = []

    def chat(self, messages, tools=(), timeout=None):
        self.recorded_calls.append({"messages": list(messages), "tool_names": [t.name for t in tools]})
        if self.turns:
            return self.turns.pop(0)
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
            "看这里 /AstrBot/data/x 和 http://127.0.0.1:8787 及 ws://10.0.0.1:3001 @小明(123456) 密钥 sk-live-secret-9999",
            extra_secrets=("sk-live-secret-9999",),
        )
        self.assertNotIn("/AstrBot", scrubbed)
        self.assertNotIn("127.0.0.1", scrubbed)
        self.assertNotIn("ws://", scrubbed)
        self.assertNotIn("sk-live-secret-9999", scrubbed)
        self.assertIn("@小明", scrubbed)
        self.assertNotIn("(123456)", scrubbed)

        self.assertEqual(sanitize_meta_reply("群里可以这么回：“在的呀”"), "在的呀")


class SchedulerTests(unittest.TestCase):
    def test_mention_fast_lane_priority_and_policy_3a(self):
        sch = Scheduler(ambient_rate=0.08, mention_cap=1.2)
        ev_plain = ChatEvent(group_id="g_plain", user_id="u1", user_name="甲", text="午饭吃啥", ts=100.0)
        ev_at = ChatEvent(group_id="g_at", user_id="u2", user_name="乙", text="@大肥鱼 在吗", ts=101.0, mentioned=True)
        sch.enqueue(ev_plain)
        sch.enqueue(ev_at)

        due = sch.due(now=120.0)
        self.assertEqual([b.group_id for b in due], ["g_at", "g_plain"])

        # 政策 3A 概率
        b_other = Batch("g1", [ChatEvent("g1", "u1", "甲", "问你呢", ts=200.0, at_others=True)])
        self.assertEqual(sch.chime_probability(b_other, now=200.0), 0.0)

        b_ambient = Batch("g1", [ChatEvent("g1", "u1", "甲", "今天好热啊", ts=200.0)])
        self.assertEqual(sch.chime_probability(b_ambient, now=200.0), 0.08)

        sch.record_bot_reply("g1", user_id="u1", offered=False, now=180.0)
        b_follow = Batch("g1", [ChatEvent("g1", "u1", "甲", "那你觉得呢", ts=210.0)])
        self.assertEqual(sch.chime_probability(b_follow, now=210.0), 0.65)

        sch.record_bot_reply("g1", user_id="u1", offered=True, now=215.0)
        b_offer = Batch("g1", [ChatEvent("g1", "u2", "乙", "查吧", ts=220.0)])
        self.assertEqual(sch.chime_probability(b_offer, now=220.0), 0.85)

        # 忙群 & 错误退避：拦截未 @，永不拦截 @
        self.assertFalse(sch.should_view(b_ambient, now=300.0, group_busy=True, recent_errors=0))
        self.assertFalse(sch.should_view(b_ambient, now=300.0, group_busy=False, recent_errors=1))
        b_mentioned = Batch("g1", [ev_at])
        self.assertTrue(sch.should_view(b_mentioned, now=300.0, group_busy=True, recent_errors=3))

        # 最近未 @ 图片自动合入追问 @ 批
        m = MediaRef(data=TINY_PNG, sha256="hash1")
        sch.attach_images("g1", [m], now=500.0)
        followup = Batch("g1", [ChatEvent("g1", "u1", "甲", "这是啥", ts=510.0, mentioned=True)])
        sch.merge_recent_images(followup, now=515.0)
        self.assertEqual(len(followup.images), 1)
        self.assertEqual(followup.images[0].sha256, "hash1")


class ContextTests(unittest.TestCase):
    def test_context_isolation_aliases_quote_and_compaction(self):
        repo = Repository(":memory:")
        repo.observe_person("g1", "101", "小明", now=100.0)
        repo.upsert_person("g1", "101", now=100.0, traits="喜欢喝冰美式")
        repo.observe_person("g1", "102", "小和", now=100.0)
        repo.upsert_person("g1", "102", now=100.0, events="在苏州做超导")
        repo.add_memory("g1", "event", "周末去苏州实验室团建", now=100.0)
        repo.add_memory("g1", "jargon", "dd=顶顶", now=100.0)
        repo.save_conv_state("g1", {"topics": ["团建安排"], "progress": [], "pending": ["订票"]})
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


if __name__ == "__main__":
    unittest.main()

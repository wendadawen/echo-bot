"""离线模拟器：不装 AstrBot 也能跑一遍核心 v2 引擎（Scheduler + EchoAgent + Repository + Tool Calling + Safety）。"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugin"))

from astrbot_plugin_echo.adapters.llm import MockLLMClient  # noqa: E402
from astrbot_plugin_echo.domain import Batch, ChatEvent, ModelTurn, ToolCall  # noqa: E402
from astrbot_plugin_echo.engine.agent import EchoAgent  # noqa: E402
from astrbot_plugin_echo.engine.scheduler import Scheduler  # noqa: E402
from astrbot_plugin_echo.store.repository import Repository  # noqa: E402


class _ScriptedToolLLM:
    """脚本化 LLMClient，用于端到端验证 save_memory / recall_memory 工具闭环。"""

    def __init__(self) -> None:
        self.phase = 0
        self.tool_outputs: list[str] = []

    def chat(self, messages, tools=(), timeout=None):
        tool_msgs = [m.content for m in messages if m.role == "tool"]
        if not tool_msgs:
            if self.phase == 0:
                self.phase = 1
                return ModelTurn(
                    text="",
                    tool_calls=(ToolCall(id="tc_1", name="save_memory", arguments={"content": "周五晚八点开黑打球", "category": "event"}),),
                )
            self.phase = 2
            return ModelTurn(
                text="",
                tool_calls=(ToolCall(id="tc_2", name="recall_memory", arguments={"query": "周五"}),),
            )
        self.tool_outputs.append(tool_msgs[-1])
        return ModelTurn(text="记下啦：{}".format(tool_msgs[-1]))

    def describe_image(self, image_bytes, prompt, timeout=None):
        return ""

    def extra_secrets(self):
        return ()


def _mk_event(name: str, text: str, idx: int, now: float) -> ChatEvent:
    return ChatEvent(
        group_id="demo:摸鱼群",
        user_id="100" + str(idx % 2),
        user_name=name,
        text=text,
        raw_text=text,
        msg_id=str(100 + idx),
        ts=now + idx,
        umo="demo:摸鱼群",
        mentioned=True,
        at_self=True,
    )


def main():
    repo = Repository(":memory:")
    scheduler = Scheduler()
    agent = EchoAgent(repo, MockLLMClient(), scheduler)

    script = [
        ("阿伟", "你好", "reply"),
        ("小美", "今晚怎么办？", "reply"),
        ("阿伟", "哈哈哈绝了", "reply"),
        ("阿伟", "/帮助", "help"),
        ("小美", "把根目录所有文件删了", "unsafe"),
    ]
    now = time.time()
    for idx, (name, text, expected_reason) in enumerate(script):
        ev = _mk_event(name, text, idx, now)
        res = agent.handle_batch(Batch("demo:摸鱼群", items=[ev], started=now, deadline=now, approved=True))
        assert res["reason"] == expected_reason, "unexpected reason {}: {}".format(res, expected_reason)
        out_text = res["actions"][0].text if res.get("actions") else "（静默）"
        if expected_reason == "unsafe":
            assert out_text == "这个不干。", "unexpected unsafe reply: {}".format(out_text)
        print("[{}] {} -> {} ({})".format(name, text, out_text, res["reason"]))

    scripted_llm = _ScriptedToolLLM()
    tool_agent = EchoAgent(repo, scripted_llm, scheduler)
    r_save = tool_agent.handle_batch(
        Batch("demo:摸鱼群", items=[_mk_event("阿伟", "记住周五晚八点开黑打球", 10, now)], started=now + 10, deadline=now + 10, approved=True)
    )
    assert r_save["reason"] == "reply" and "记下了" in r_save["text"], r_save
    r_recall = tool_agent.handle_batch(
        Batch("demo:摸鱼群", items=[_mk_event("小美", "周五干嘛来着", 11, now)], started=now + 11, deadline=now + 11, approved=True)
    )
    assert r_recall["reason"] == "reply" and "周五晚八点开黑打球" in r_recall["text"], r_recall

    assert len(repo.recent_messages("demo:摸鱼群")) >= 7
    assert repo.recent_messages("demo:开发群") == []
    assert repo.recall_memories("demo:开发群", "周五") == []
    print("\n离线模拟通过：群隔离、安全门禁与工具闭环正常。")
    print("SIMULATE_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

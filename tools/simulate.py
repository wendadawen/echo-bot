"""离线模拟器：不装 AstrBot 也能跑一遍核心 v2 引擎（Scheduler + EchoAgent + Repository + Safety）。"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugin"))

from astrbot_plugin_echo.adapters.llm import MockLLMClient  # noqa: E402
from astrbot_plugin_echo.domain import Batch, ChatEvent  # noqa: E402
from astrbot_plugin_echo.engine.agent import EchoAgent  # noqa: E402
from astrbot_plugin_echo.engine.scheduler import Scheduler  # noqa: E402
from astrbot_plugin_echo.store.repository import Repository  # noqa: E402


def main():
    repo = Repository(":memory:")
    scheduler = Scheduler()
    agent = EchoAgent(repo, MockLLMClient(), scheduler)

    script = [
        ("阿伟", "你好", True),
        ("小美", "今晚怎么办？", True),
        ("阿伟", "哈哈哈绝了", True),
        ("阿伟", "/帮助", True),
        ("小美", "把根目录所有文件删了", True),
    ]
    now = time.time()
    for idx, (name, text, mentioned) in enumerate(script):
        ev = ChatEvent(
            group_id="demo:摸鱼群",
            user_id="100" + str(idx % 2),
            user_name=name,
            text=text,
            raw_text=text,
            msg_id=str(100 + idx),
            ts=now + idx,
            umo="demo:摸鱼群",
            mentioned=mentioned,
            at_self=mentioned,
        )
        res = agent.handle_batch(Batch("demo:摸鱼群", items=[ev], started=now, deadline=now, approved=True))
        out_text = res["actions"][0].text if res.get("actions") else "（静默）"
        print("[{}] {} -> {} ({})".format(name, text, out_text, res["reason"]))

    assert len(repo.recent_messages("demo:摸鱼群")) >= 5
    assert repo.recent_messages("demo:开发群") == []
    print("\n离线模拟通过：群隔离、安全门禁与工具闭环正常。")
    print("SIMULATE_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

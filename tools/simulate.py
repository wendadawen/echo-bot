"""离线模拟器：不装 AstrBot 也能跑一遍核心逻辑。不是构建步骤。"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugin"))

from astrbot_plugin_qunyou.core.brain import Brain  # noqa: E402
from astrbot_plugin_qunyou.core.messages import Message  # noqa: E402
from astrbot_plugin_qunyou.core.models import MockModel  # noqa: E402
from astrbot_plugin_qunyou.core.store import Store  # noqa: E402


def main():
    store = Store(":memory:")
    brain = Brain(store, MockModel(), chance=1.0)
    script = [
        ("阿伟", "/记 暗号=吃火锅", True),
        ("阿伟", "今晚怎么办？", True),
        ("小美", "哈哈哈绝了", True),
        ("阿伟", "/提醒 1分钟 交周报", True),
        ("小美", "/记忆", True),
    ]
    for name, text, mentioned in script:
        result = brain.handle(
            Message("demo:摸鱼群", "1001", name, text, mentioned, "demo:摸鱼群")
        )
        print("[{}] {}".format(name, text))
        print("  -> {} ({})".format(result["text"] or "（没接话）", result["reason"]))

    print("\n本群记忆：\n{}".format(store.facts_text("demo:摸鱼群")))
    print("另一个群：{}".format(store.facts_text("demo:开发群")))

    store.add_reminder(
        "demo:摸鱼群", "1001", "阿伟", "交周报", time.time() - 1, "demo:摸鱼群"
    )
    due = brain.tick()
    assert len(due) == 1 and due[0]["umo"] == "demo:摸鱼群"
    assert brain.tick() == []
    print("\n离线模拟通过：记忆按群隔离，提醒只触发一次。")
    print("SIMULATE_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

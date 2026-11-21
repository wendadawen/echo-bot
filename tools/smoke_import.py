"""在装有 AstrBot 的环境里验证插件模块可被框架加载。由 GitHub Actions 调用。"""

import importlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugin"))


def main():
    from astrbot.api.event import filter  # noqa: F401
    from astrbot.api.star import Star, StarTools  # noqa: F401

    if not hasattr(filter, "on_astrbot_loaded"):
        print("ERROR 当前 AstrBot 版本缺少 on_astrbot_loaded 钩子")
        return 1
    if not hasattr(filter, "event_message_type"):
        print("ERROR 当前 AstrBot 版本缺少 event_message_type 过滤器")
        return 1

    module = importlib.import_module("astrbot_plugin_qunyou.main")
    if not hasattr(module, "Qunyou"):
        print("ERROR main.py 未定义 Qunyou")
        return 1
    if not issubclass(module.Qunyou, Star):
        print("ERROR Qunyou 未继承 Star")
        return 1
    print("插件可加载：Qunyou 继承 Star，过滤器与钩子可用")
    return 0


if __name__ == "__main__":
    sys.exit(main())

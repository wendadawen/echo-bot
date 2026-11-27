"""在装有 AstrBot 的环境里验证插件模块可被框架加载与实例化。由 GitHub Actions 与实机冒烟调用。"""

import asyncio
import importlib
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugin"))

_SENSITIVE_ENV_KEYS = (
    "BOT_API_KEY",
    "BOT_API_BASE",
    "BOT_MODEL",
    "BOT_SEARCH_PROVIDER",
    "BOT_SEARCH_API_BASE",
    "BOT_SEARCH_API_KEY",
)


class _DummyContext:
    async def send_message(self, *args, **kwargs):
        return None

    def __getattr__(self, _name: str):
        return lambda *args, **kwargs: None


def main():
    from astrbot.api.event import filter  # noqa: F401
    from astrbot.api.star import Star, StarTools  # noqa: F401

    if not hasattr(filter, "on_astrbot_loaded"):
        print("ERROR 当前 AstrBot 版本缺少 on_astrbot_loaded 钩子")
        return 1
    if not hasattr(filter, "event_message_type"):
        print("ERROR 当前 AstrBot 版本缺少 event_message_type 过滤器")
        return 1

    module = importlib.import_module("astrbot_plugin_echo.main")
    if not hasattr(module, "Echo"):
        print("ERROR main.py 未定义 Echo")
        return 1
    if not issubclass(module.Echo, Star):
        print("ERROR Echo 未继承 Star")
        return 1

    saved_env = {k: os.environ.pop(k) for k in _SENSITIVE_ENV_KEYS if k in os.environ}
    orig_get_data_dir = StarTools.get_data_dir
    try:
        with tempfile.TemporaryDirectory() as tmp:
            StarTools.get_data_dir = staticmethod(lambda *_args, **_kwargs: Path(tmp))
            echo = module.Echo(_DummyContext(), config={})
            try:
                assert echo.repo is not None
                assert echo.emoji_store is not None
                assert echo.agent is not None
                assert echo.inbound is not None
                assert echo.sender is not None
                assert type(echo.llm).__name__ == "MockLLMClient"
            finally:
                asyncio.run(echo.terminate())
    finally:
        StarTools.get_data_dir = orig_get_data_dir
        os.environ.update(saved_env)

    print("插件可加载与初始化：Echo 继承 Star，各核心子系统实例化与优雅停机通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())

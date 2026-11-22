"""回声（astrbot_plugin_echo）

AstrBot 插件入口：只负责把平台事件转成统一消息、把核心逻辑的结果发回去。
核心逻辑在 core/ 中，不 import 任何框架模块，因此可以独立测试。
"""

import asyncio
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import At, Plain
from astrbot.api.star import Context, Star, StarTools

from .core.brain import PERSONA, Brain
from .core.messages import Message
from .core.models import build_model
from .core.store import Store

PLUGIN_NAME = "astrbot_plugin_echo"


def _data_dir() -> Path:
    """兼容不同版本的 StarTools.get_data_dir 签名。"""
    try:
        return Path(StarTools.get_data_dir())
    except TypeError:
        return Path(StarTools.get_data_dir(PLUGIN_NAME))


class Echo(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config or {}
        directory = _data_dir()
        directory.mkdir(parents=True, exist_ok=True)
        self.store = Store(str(directory / "echo.sqlite3"))
        self.model = build_model(self.config)
        self.brain = Brain(
            self.store,
            self.model,
            chance=int(self.config.get("reply_chance", 0)) / 100.0,
            persona=PERSONA,
        )
        self.reminders_enabled = bool(self.config.get("reminder_enabled", True))
        self._reminder_task = None

    @filter.on_astrbot_loaded()
    async def on_loaded(self):
        self._ensure_reminder_task()

    async def terminate(self):
        """插件卸载时停止后台提醒任务。"""
        if self._reminder_task is not None:
            self._reminder_task.cancel()
            self._reminder_task = None

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE, priority=10)
    async def on_group_message(self, event: AstrMessageEvent):
        """接管群消息：命令与接话判断都在核心层完成。"""
        self._ensure_reminder_task()
        sender = str(event.get_sender_id() or "")
        if not sender or sender == str(event.get_self_id() or ""):
            return
        text = (event.get_message_str() or "").strip()
        if not text:
            return
        # 含链接的消息交给链接解析插件，本插件不抢答、也不阻断后续处理。
        if "http://" in text or "https://" in text:
            return
        message = Message(
            group_id="{}:{}".format(event.get_platform_name(), event.get_group_id() or ""),
            user_id=sender,
            user_name=str(event.get_sender_name() or sender),
            text=text,
            mentioned=self._mentioned(event, text),
            umo=str(event.unified_msg_origin or ""),
        )
        # 核心层是同步的纯逻辑；模型网络请求放在线程里，避免阻塞事件循环。
        result = await asyncio.to_thread(self.brain.handle, message)
        if result.get("reason") == "model_error":
            logger.error("模型调用失败：%s", result.get("error"))
        if not result["replied"]:
            return
        event.stop_event()
        yield event.plain_result(result["text"])

    def _mentioned(self, event: AstrMessageEvent, text: str) -> bool:
        self_id = str(event.get_self_id() or "")
        for part in event.get_messages():
            if isinstance(part, At) and str(part.qq) == self_id:
                return True
        return text.startswith(("@回声", "＠回声"))

    def _ensure_reminder_task(self):
        if not self.reminders_enabled:
            return
        if self._reminder_task is not None and not self._reminder_task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._reminder_task = loop.create_task(
            self._reminder_loop(), name="echo-reminders"
        )

    async def _reminder_loop(self):
        while True:
            try:
                for item in self.brain.tick():
                    await self._send_reminder(item)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("回声提醒任务异常：%s", exc)
            await asyncio.sleep(2)

    async def _send_reminder(self, item):
        text = "@{} {}".format(item["user_name"], item["text"])
        umo = item.get("umo") or ""
        if not umo:
            logger.warning("提醒缺少会话标识，已跳过：%s", text)
            return
        try:
            await self.context.send_message(umo, MessageChain([Plain(text)]))
        except Exception as exc:
            logger.error("提醒发送失败（%s）：%s", item["text"], exc)

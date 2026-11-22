"""回声（astrbot_plugin_echo）

AstrBot 插件入口：只负责把平台事件转成批次、把核心层的动作发回去。
核心逻辑在 core/ 中，不 import 任何框架模块，因此可以独立测试。
"""

import asyncio
import re
import time
import urllib.request
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import At, Face, Image, Plain, Reply
from astrbot.api.star import Context, Star, StarTools

from .core.attention import AttentionTracker
from .core.brain import PERSONA, Brain
from .core.display import MessageLimiter, clean
from .core.expression import delay, split
from .core.forward import extract as forward_extract
from .core.forward import looks_like_forward
from .core.forward import render as forward_render
from .core.heartbeat import Heartbeat
from .core.harness import CodexHarness
from .core.memory import Memory
from .core.messages import Message
from .core.models import build_model
from .core.modes import ModePolicy
from .core.monitor import Monitor
from .core.person_info import PersonStore
from .core.prompts import PersonaLibrary, PromptStore
from .core.rhythm import Item, Rhythm
from .core.schedule import Schedule
from .core.store import Store
from .core.subagent import SubAgentManager
from .core.vision import EmojiBank

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
        self.memory = Memory(str(directory / "memory.sqlite3"))
        self.emoji_bank = EmojiBank(directory / "emojis")
        self.rhythm = Rhythm()
        self.persons = PersonStore(str(directory / "persons.sqlite3"))
        self.monitor = Monitor(str(directory / "monitor.sqlite3"))
        self.attention = AttentionTracker()
        self.modes = ModePolicy()
        self.prompts = PromptStore(directory / "prompts")
        self.personas = PersonaLibrary(directory / "personas")
        self.subagent = SubAgentManager()
        self.schedule = Schedule(str(directory / "schedules.sqlite3"))
        self.heartbeat = Heartbeat()
        self.harness = CodexHarness(
            home=str(directory / "codex"),
            workdir_root=str(directory / "harness"),
            api_base=str(self.config.get("api_base") or ""),
            api_key=str(self.config.get("api_key") or ""),
            model=str(self.config.get("model") or ""),
            timeout=150,
        )
        self.limiter = MessageLimiter(per_minute=8, per_burst=3)
        self._last_umo = {}
        self._heartbeat_at = 0.0
        self._handled_turns = set()
        self.brain = Brain(
            self.store,
            self.model,
            persona=PERSONA,
            memory=self.memory,
            emoji_bank=self.emoji_bank,
            char_frequency=str(directory / "char_frequency.json"),
            capabilities={"view_image": True, "send_face": True,
                          "schedule": True, "wait": True, "subagent": True},
            modes=self.modes,
            attention=self.attention,
            persons=self.persons,
            monitor=self.monitor,
            prompt_store=self.prompts,
            personas=self.personas,
            subagent=self.subagent,
            schedule=self.schedule,
            harness=self.harness,
        )
        self.reminders_enabled = bool(self.config.get("reminder_enabled", True))
        self._reminder_task = None
        self._chat_task = None

    @filter.on_astrbot_loaded()
    async def on_loaded(self):
        self._ensure_reminder_task()
        self._ensure_chat_task()

    async def terminate(self):
        """插件卸载时停止后台任务。"""
        for name in ("_reminder_task", "_chat_task"):
            task = getattr(self, name)
            if task is not None:
                task.cancel()
                setattr(self, name, None)

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE, priority=10)
    async def on_group_message(self, event: AstrMessageEvent):
        """命令即时回；闲聊进防抖缓冲，由后台循环按批决策。"""
        # 群聊完全由本插件接管，禁止 AstrBot 默认 LLM 再插一脚。
        try:
            event.should_call_llm(False)
        except Exception:
            pass
        self._ensure_reminder_task()
        self._ensure_chat_task()
        sender = str(event.get_sender_id() or "")
        if not sender or sender == str(event.get_self_id() or ""):
            return
        text = (event.get_message_str() or "").strip()
        mentioned = self._mentioned(event, text)
        # 合并转发：尽量把里面每条消息取出来，模型才看得懂。
        forward_text = ""
        for part in event.get_messages():
            if looks_like_forward(part):
                forward_text = forward_render(forward_extract(part))
                break
        group_id = "{}:{}".format(event.get_platform_name(), event.get_group_id() or "")
        # 表情/图片先收进仓库；纯图片消息没有文字，也要收。
        has_image = False
        for part in event.get_messages():
            if isinstance(part, Face):
                if self.emoji_bank:
                    await asyncio.to_thread(
                        self.emoji_bank.collect_face, group_id,
                        getattr(part, "id", ""), getattr(part, "name", ""),
                    )
            elif isinstance(part, Image):
                has_image = True
                loop = asyncio.get_running_loop()
                loop.create_task(self._collect_image(group_id, part))
                break
        # 含链接且未被 @：交给链接解析插件处理。
        if ("http://" in text or "https://" in text) and not mentioned:
            return
        # 空消息：只有被 @ 且带图时当作看图入批，其余到此为止，别再流进框架内置 LLM。
        if not text and not forward_text:
            if not (mentioned and has_image):
                event.stop_event()
                return
            text = "[图片]"

        umo = str(event.unified_msg_origin or "")
        user_name = str(event.get_sender_name() or sender)
        self._last_umo[group_id] = umo
        self.heartbeat.observe(group_id)
        message = Message(group_id, sender, user_name, text, mentioned, umo)
        # 新消息提前唤醒 wait 倒计时。
        self.rhythm.wake(group_id)

        # 命令不走防抖，立刻回。
        direct = await asyncio.to_thread(self.brain.command, message, text)
        if direct is not None:
            # 按命令判断静默，不依赖回复文案。
            cmd = text.lstrip("/").strip()
            if cmd in ("闭嘴", "shutup"):
                self.rhythm.mute(group_id, True)
            elif cmd in ("说话", "开麦", "speak", "随便", "正常", "normal"):
                self.rhythm.mute(group_id, False)
            event.stop_event()
            yield event.plain_result(direct)
            return

        # 群里发的表情顺手收进表情仓库。
        quote_text = self._extract_quote(event)
        self.rhythm.add(group_id, Item(
            (text + (" " + forward_text if forward_text else "")).strip(),
            sender, user_name, mentioned, umo, time.time(),
            kind="image" if has_image else "text", quote=quote_text,
        ))
        event.stop_event()
        return

    async def _collect_image(self, group_id, part):
        if not self.emoji_bank:
            return
        url = str(getattr(part, "url", "") or "")
        data = b""
        if url.startswith("http"):
            try:
                data = await asyncio.to_thread(self._download, url)
            except Exception as exc:
                logger.info("表情下载失败：%s", exc)
        if not data:
            path = str(getattr(part, "path", "") or "")
            if path:
                try:
                    data = await asyncio.to_thread(Path(path).read_bytes)
                except OSError:
                    data = b""
        if not data:
            return
        self.rhythm.attach_image(group_id, data)
        try:
            label = await asyncio.to_thread(
                self.emoji_bank.collect, group_id, data, self.model
            )
            if label:
                logger.info("表情已收：%s", label)
        except Exception as exc:
            logger.info("表情收集失败：%s", exc)

    @staticmethod
    def _download(url):
        request = urllib.request.Request(
            url, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://qq.com/"}
        )
        with urllib.request.urlopen(request, timeout=15) as resp:
            return resp.read(2 * 1024 * 1024)

    @staticmethod
    def _extract_quote(event: AstrMessageEvent) -> str:
        for part in event.get_messages():
            if not isinstance(part, Reply):
                continue
            body = str(getattr(part, "message_str", "") or getattr(part, "text", "") or "").strip()
            if not body:
                segs = []
                for seg in getattr(part, "chain", None) or []:
                    seg_text = getattr(seg, "text", None)
                    if seg_text:
                        segs.append(str(seg_text))
                body = " ".join(segs).strip()
            body = re.sub(r"\s+", " ", body)[:100]
            sender_name = str(getattr(part, "sender_nickname", "") or "").strip()
            if sender_name and body:
                return "{}：{}".format(sender_name, body)
            if body:
                return body
            if sender_name:
                return "回复 {}".format(sender_name)
        return ""

    def _mentioned(self, event: AstrMessageEvent, text: str) -> bool:
        self_id = str(event.get_self_id() or "")
        for part in event.get_messages():
            if isinstance(part, At) and str(part.qq) == self_id:
                return True
            # 引用机器人的消息，视为在跟它说话。
            if isinstance(part, Reply) and str(getattr(part, "sender_id", "")) == self_id:
                return True
        return text.startswith(("@大肥鱼", "@回声", "＠大肥鱼", "＠回声"))

    def _ensure_chat_task(self):
        if self._chat_task is not None and not self._chat_task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._chat_task = loop.create_task(self._chat_loop(), name="echo-chat")

    async def _chat_loop(self):
        """防抖到期 → 查看概率与欠债 → 动作集决策 → 拆条拟人发送。

        另外处理 wait 到期唤醒、静默查看（复用上一批上下文）与轮数额度。
        """
        while True:
            try:
                now = time.time()
                pending = list(self.rhythm.due(now))
                # wait 到期：按当时概率抽签，复用上一批上下文（由后续 due() 统一弹出，避免留滞）。
                for group in list(self.rhythm.groups()):
                    if not self.rhythm.has_batch(group) and self.rhythm.wait_due(group, now):
                        last = self.rhythm.last_batch(group)
                        if last is not None:
                            self.rhythm.requeue(last, now)
                # 静默查看：安静太久且防抖已完成时抽签看一眼。
                for group in list(self.rhythm.groups()):
                    if (
                        not self.rhythm.has_batch(group)
                        and self.rhythm.silent_due(group, now)
                        and self.rhythm.consume_silent(group, now)
                    ):
                        last = self.rhythm.last_batch(group)
                        if last is not None:
                            self.rhythm.requeue(last, now)
                pending.extend(self.rhythm.due(now))
                for batch in pending:
                    group = batch.group_id
                    turn = getattr(batch, "turn_id", "")
                    # 同一批被 wait / 静默查看重新排队时，只处理一次。
                    if turn and turn in self._handled_turns:
                        continue
                    if turn:
                        self._handled_turns.add(turn)
                        if len(self._handled_turns) > 200:
                            self._handled_turns.clear()
                    if self.rhythm.is_muted(group):
                        continue
                    if not batch.approved and not self.rhythm.should_view(
                            group, now, forced=batch.mentioned,
                            rate=self.brain.view_rate(batch)):
                        # 未通过查看的消息留到下一批，不丢。
                        self.rhythm.carry(group, batch)
                        continue
                    quota = self.rhythm.start_turn(group)
                    result = await asyncio.to_thread(self.brain.handle_batch, batch)
                    self.rhythm.use_round(group, quota)
                    # wait 与沉默都要允许后续再处理；已回复的批不再重复。
                    if turn and result.get("reason") in ("wait", "silent"):
                        self._handled_turns.discard(turn)
                    if result.get("reason") == "model_error":
                        logger.error("模型调用失败：%s", result.get("error"))
                    if result.get("reason") == "wait":
                        seconds = result.get("wait_seconds") or 0
                        self.rhythm.wait(group, seconds, now)
                        continue
                    if not result.get("replied"):
                        continue
                    elapsed = float(result.get("elapsed") or 0.0)
                    if result.get("face"):
                        try:
                            face = Face(id=int(result["face"]))
                        except (TypeError, ValueError):
                            face = None
                        if face is not None:
                            await self.context.send_message(
                                batch.umo, MessageChain([face])
                            )
                    elif result.get("image"):
                        await self._send_image(group, batch.umo, result["image"])
                    else:
                        parts = result.get("parts") or split(result.get("text", ""))
                        delays = result.get("delays") or []
                        parts = self.limiter.cap_parts(parts)
                        for index, part in enumerate(parts):
                            if not self.limiter.allow(group):
                                break
                            await self.context.send_message(
                                batch.umo, MessageChain([Plain(part)])
                            )
                            self.limiter.record(group)
                            wait = (delays[index] if index < len(delays)
                                    else delay(part, elapsed=elapsed))
                            await asyncio.sleep(wait)
                # 群安静太久时，偶尔主动开一句（默认很克制）。
                if now - self._heartbeat_at > 60:
                    self._heartbeat_at = now
                    for group in list(self.rhythm.groups()):
                        if self.rhythm.is_muted(group) or not self.heartbeat.due(group, now):
                            continue
                        if self.modes.mode(group, now) != "normal":
                            continue
                        text = await asyncio.to_thread(self.brain.spontaneous, group)
                        umo = self._last_umo.get(group) or ""
                        if text and umo:
                            await self.context.send_message(
                                umo, MessageChain([Plain(text)])
                            )
                        self.heartbeat.mark(group, now)
                # 后台任务完成：把结论发回原群（内部会调主模型转述，放线程避免阻塞事件循环）。
                for task in await asyncio.to_thread(self.brain.poll_subagents):
                    if not task.get("umo"):
                        continue
                    for part in [clean(task.get("text", ""))]:
                        if not part:
                            continue
                        await self.context.send_message(
                            task["umo"], MessageChain([Plain(part)])
                        )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("大肥鱼对话循环异常：%s", exc)
            await asyncio.sleep(0.4)

    async def _send_image(self, group, umo, path):
        if not umo or not Path(path).exists():
            return
        try:
            await self.context.send_message(
                umo, MessageChain([Image(file="file://" + str(path))])
            )
        except Exception as exc:
            logger.error("表情发送失败：%s", exc)

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
                for item in await asyncio.to_thread(self.brain.tick):
                    await self._send_reminder(item)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("大肥鱼提醒任务异常：%s", exc)
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

"""回声（astrbot_plugin_echo）

AstrBot 插件入口：只负责把平台事件转成批次、把核心层的动作发回去。
核心逻辑在 core/ 中，不 import 任何框架模块，因此可以独立测试。
"""

import asyncio
import collections
import ipaddress
import os
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import At, Face, File, Image, Plain, Reply
from astrbot.api.star import Context, Star, StarTools

from .core.attention import AttentionTracker
from .core.brain import PERSONA, Brain
from .core.display import MessageLimiter, clean
from .core.forward import extract as forward_extract
from .core.forward import looks_like_forward
from .core.forward import render as forward_render
from .core.heartbeat import Heartbeat
from .core.harness import CodexHarness, cleanup_file_entry, cleanup_outbox
from .core.memory import Memory
from .core.messages import Message, format_speaker_message
from .core.models import build_model
from .core.modes import ModePolicy
from .core.monitor import Monitor
from .core.person_info import PersonStore
from .core.prompts import PersonaLibrary, PromptStore
from .core.rhythm import Item, Rhythm
from .core.schedule import Schedule
from .core.store import Store, cleanup_runtime_caches
from .core.subagent import SubAgentManager
from .core.vision import EmojiBank

PLUGIN_NAME = "astrbot_plugin_echo"
MAX_DOWNLOAD_BYTES = 2 * 1024 * 1024
MAX_HANDLED_TURNS = 2000


def _data_dir() -> Path:
    """兼容不同版本的 StarTools.get_data_dir 签名。"""
    try:
        return Path(StarTools.get_data_dir())
    except TypeError:
        return Path(StarTools.get_data_dir(PLUGIN_NAME))


def _is_public_http_url(url: str) -> bool:
    """校验 URL 必须为公网 http/https，解析 DNS 后拦截回环、内网与链路本地地址（防 SSRF）。"""
    try:
        parsed = urllib.parse.urlsplit(str(url or "").strip())
    except ValueError:
        return False
    if parsed.scheme.lower() not in ("http", "https"):
        return False
    host = parsed.hostname
    if not host:
        return False
    port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError:
        return False
    if not infos:
        return False
    for info in infos:
        ip_str = info[4][0]
        try:
            ip_obj = ipaddress.ip_address(ip_str)
        except ValueError:
            return False
        if (
            ip_obj.is_private
            or ip_obj.is_loopback
            or ip_obj.is_link_local
            or ip_obj.is_multicast
            or ip_obj.is_reserved
            or ip_obj.is_unspecified
        ):
            return False
    return True


def _looks_like_image(data: bytes) -> bool:
    """检查下载字节流的常见图片魔数，拦截非图片内容误入视觉通道。"""
    if not data or len(data) < 4 or len(data) > MAX_DOWNLOAD_BYTES:
        return False
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return True
    if data.startswith(b"\xff\xd8\xff"):
        return True
    if data.startswith((b"GIF87a", b"GIF89a")):
        return True
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return True
    if data.startswith(b"BM"):
        return True
    return False


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        hops = getattr(req, "_echo_hops", 0) + 1
        if hops > 3 or not _is_public_http_url(newurl):
            raise urllib.error.URLError("blocked unsafe redirect")
        nxt = super().redirect_request(req, fp, code, msg, headers, newurl)
        if nxt is not None:
            nxt._echo_hops = hops
        return nxt


class Echo(Star):
    def __init__(self, context: Context, config: Optional[dict] = None):
        super().__init__(context)
        self.config = config or {}
        directory = _data_dir()
        directory.mkdir(parents=True, exist_ok=True)
        resolved_dir = directory.resolve()
        self._astrbot_data_root = (
            str(resolved_dir.parents[1])
            if len(resolved_dir.parents) >= 2 and resolved_dir.parent.name == "plugin_data"
            else ""
        )
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
        raw_timeout = self.config.get("task_timeout")
        if raw_timeout is None or raw_timeout == "":
            raw_timeout = os.environ.get("BOT_TASK_TIMEOUT")
        api_base = str(self.config.get("api_base") or os.environ.get("BOT_API_BASE") or "").strip()
        api_key = str(self.config.get("api_key") or os.environ.get("BOT_API_KEY") or "").strip()
        model_name = str(self.config.get("model") or os.environ.get("BOT_MODEL") or "").strip()
        self.harness = CodexHarness(
            home=str(directory / "codex"),
            workdir_root=str(directory / "harness"),
            outbox_root=str(directory / "outbox"),
            api_base=api_base,
            api_key=api_key,
            model=model_name,
            timeout=raw_timeout,
        )
        cleanup_outbox(
            self.harness.outbox_root,
            keep_paths=self.store.pending_delivery_paths(),
        )
        self.limiter = MessageLimiter(per_minute=8, per_burst=3)
        self._last_umo = {}
        self._heartbeat_at = 0.0
        self._emoji_cleanup_at = 0.0
        self._handled_turns = collections.OrderedDict()
        self._bg_tasks = set()
        self._download_sem = asyncio.Semaphore(4)
        raw_history_limit = self.config.get("history_limit")
        if raw_history_limit is None or raw_history_limit == "":
            raw_history_limit = os.environ.get("BOT_HISTORY_LIMIT")
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
            history_limit=raw_history_limit,
        )
        self.reminders_enabled = bool(self.config.get("reminder_enabled", True))
        self._reminder_task = None
        self._chat_task = None

    def _spawn_bg(self, coro):
        loop = asyncio.get_running_loop()
        task = loop.create_task(coro)
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)
        return task

    @filter.on_astrbot_loaded()
    async def on_loaded(self):
        self._ensure_reminder_task()
        self._ensure_chat_task()

    async def terminate(self):
        """插件卸载时等待后台协程结束并关闭全部数据库连接。"""
        tasks = []
        for name in ("_reminder_task", "_chat_task"):
            task = getattr(self, name, None)
            if task is not None:
                task.cancel()
                tasks.append(task)
                setattr(self, name, None)
        for task in list(getattr(self, "_bg_tasks", ()) or ()):
            task.cancel()
            tasks.append(task)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for attr in ("store", "memory", "persons", "monitor", "schedule"):
            db_obj = getattr(self, attr, None)
            close_fn = getattr(db_obj, "close", None)
            if callable(close_fn):
                try:
                    close_fn()
                except Exception:
                    pass

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
        raw_gid = str(event.get_group_id() or "").strip()
        if not raw_gid:
            return
        text = (event.get_message_str() or "").strip()
        mentioned = self._mentioned(event, text)
        # 合并转发：尽量把里面每条消息取出来，模型才看得懂。
        forward_text = ""
        for part in event.get_messages():
            if looks_like_forward(part):
                forward_text = forward_render(forward_extract(part))
                break
        group_id = "{}:{}".format(event.get_platform_name(), raw_gid)
        # 含链接且未被 @：交给链接解析插件处理。
        if ("http://" in text or "https://" in text) and not mentioned:
            return
        will_queue = bool(text or forward_text or mentioned)
        # 表情/图片先收进仓库；纯未点名图片仅入库表情包，不挂载到未来批的看图队列。
        has_image = False
        collected_images = 0
        for part in event.get_messages():
            if isinstance(part, Face):
                if self.emoji_bank:
                    await asyncio.to_thread(
                        self.emoji_bank.collect_face, group_id,
                        getattr(part, "id", ""), getattr(part, "name", ""),
                    )
            elif isinstance(part, Image):
                has_image = True
                if collected_images < 2:
                    collected_images += 1
                    self._spawn_bg(self._collect_image(group_id, part, attach=will_queue))
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

        # 命令必须带 / 或 ／ 前缀才拦截，避免日常聊天误触发；命中则不走防抖。
        if text.startswith(("/", "／")):
            direct = await asyncio.to_thread(self.brain.command, message, text)
            if direct is not None:
                event.stop_event()
                yield event.plain_result(direct)
                return

        quote_text = self._extract_quote(event)
        self.rhythm.add(group_id, Item(
            (text + (" " + forward_text if forward_text else "")).strip(),
            sender, user_name, mentioned, umo, time.time(),
            kind="image" if has_image else "text", quote=quote_text,
        ))
        event.stop_event()
        return

    async def _collect_image(self, group_id, part, attach=True):
        if not self.emoji_bank:
            return
        url = str(getattr(part, "url", "") or "")
        data = b""
        async with self._download_sem:
            if url.startswith("http"):
                try:
                    data = await asyncio.to_thread(self._download, url)
                except Exception as exc:
                    logger.info("表情下载失败：%s", exc)
            if not data:
                path = str(getattr(part, "path", "") or "")
                if path:
                    try:
                        p = Path(path)
                        if not p.is_symlink() and p.is_file() and p.stat().st_size <= MAX_DOWNLOAD_BYTES:
                            data = await asyncio.to_thread(p.read_bytes)
                    except OSError:
                        data = b""
        if not _looks_like_image(data):
            return
        if attach:
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
        if not _is_public_http_url(url):
            return b""
        opener = urllib.request.build_opener(_SafeRedirectHandler())
        request = urllib.request.Request(
            url, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://qq.com/"}
        )
        with opener.open(request, timeout=15) as resp:
            data = resp.read(MAX_DOWNLOAD_BYTES + 1)
        if len(data) > MAX_DOWNLOAD_BYTES:
            return b""
        return data

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
                return format_speaker_message(sender_name, body)
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

    def _mark_turn_handled(self, turn):
        if not turn:
            return
        self._handled_turns[turn] = time.time()
        self._handled_turns.move_to_end(turn)
        while len(self._handled_turns) > MAX_HANDLED_TURNS:
            self._handled_turns.popitem(last=False)

    async def _chat_loop(self):
        """防抖到期 → 查看概率与欠债 → 动作集决策 → 单条发送。

        同时推进 wait 到期唤醒、静默查看、主动开聊与跨重载子任务投递。
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
                    try:
                        group = batch.group_id
                        turn = getattr(batch, "turn_id", "")
                        if turn and turn in self._handled_turns:
                            continue
                        self._mark_turn_handled(turn)
                        if self.rhythm.is_muted(group):
                            continue
                        if not batch.approved and not self.rhythm.should_view(
                                group, now, forced=batch.mentioned,
                                rate=self.brain.view_rate(batch)):
                            await asyncio.to_thread(self.brain.observe_skipped, batch)
                            continue
                        quota = self.rhythm.start_turn(group)
                        started_at = time.time()
                        result = await asyncio.to_thread(self.brain.handle_batch, batch)
                        self.rhythm.use_round(group, quota)
                        reason = result.get("reason")
                        # 仅 wait 且重放次数小于 2 时允许到点再决策一次；silent 不清除 turn_id 避免每 30 分钟重复唤醒旧批。
                        if turn and reason == "wait" and int(getattr(batch, "replay_count", 0) or 0) < 2:
                            self._handled_turns.pop(turn, None)
                        if reason == "model_error":
                            logger.error("模型调用失败：%s", result.get("error") or "未知错误")
                        if reason == "wait":
                            if self.rhythm._last_input.get(group, 0.0) <= started_at:
                                seconds = result.get("wait_seconds") or 0
                                self.rhythm.wait(group, seconds, time.time())
                            continue
                        if not result.get("replied") or self.rhythm.is_muted(group):
                            continue
                        if result.get("face"):
                            try:
                                face = Face(id=int(result["face"]))
                            except (TypeError, ValueError):
                                face = None
                            if face is not None and self.limiter.allow(group):
                                await self.context.send_message(
                                    batch.umo, MessageChain([face])
                                )
                                self.limiter.record(group)
                        elif result.get("image"):
                            await self._send_image(group, batch.umo, result["image"])
                        else:
                            await self._send_reply(group, batch.umo, result)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        logger.error("批消息处理异常：%s", exc)
                # 群安静太久时，偶尔主动开一句（先发后记 + 失败退避，防离线刷屏）。
                if now - self._heartbeat_at > 60:
                    self._heartbeat_at = now
                    for group in list(self.rhythm.groups()):
                        if self.rhythm.is_muted(group) or not self.heartbeat.due(group, now):
                            continue
                        if self.modes.mode(group, now) != "normal":
                            continue
                        text = await asyncio.to_thread(self.brain.spontaneous, group, "", False)
                        umo = self._last_umo.get(group) or ""
                        if text and umo and not self.rhythm.is_muted(group) and self.limiter.allow(group):
                            try:
                                cleaned = clean(text)
                                await self.context.send_message(
                                    umo, MessageChain([Plain(cleaned)])
                                )
                                await asyncio.to_thread(self.brain.record_spontaneous, group, cleaned)
                                self.limiter.record(group)
                                self.heartbeat.mark(group, time.time())
                            except Exception as exc:
                                self.heartbeat.backoff(group, 1800)
                                logger.error("主动开聊发送失败（%s）：%s", type(exc).__name__, exc)
                        else:
                            self.heartbeat.backoff(group, 1800)
                # 每 6 小时清理一次过期表情、超限群消息及 QQ/临时缓存。
                if now - self._emoji_cleanup_at > 21600:
                    self._emoji_cleanup_at = now
                    try:
                        if self.emoji_bank:
                            await asyncio.to_thread(self.emoji_bank.cleanup)
                        await asyncio.to_thread(self.store.prune_messages)
                        data_root = getattr(self, "_astrbot_data_root", "")
                        if data_root:
                            await asyncio.to_thread(cleanup_runtime_caches, data_root)
                    except Exception as exc:
                        logger.error("定期缓存清理异常：%s", exc)
                # 后台任务完成：把结论及生成的群文件发回原群（逐任务异常隔离，确保不漏发不遗留 outbox）。
                for task in await asyncio.to_thread(self.brain.poll_subagents):
                    group = task.get("group", "")
                    umo = task.get("umo") or ""
                    files = list(task.get("files") or [])
                    try:
                        if not umo or (group and self.rhythm.is_muted(group)):
                            continue
                        part = task.get("text", "") if task.get("cleaned") else clean(task.get("text", ""))
                        if part:
                            await self.context.send_message(
                                umo, MessageChain([Plain(part)])
                            )
                        for item in files:
                            await self._send_file(
                                group,
                                umo,
                                item.get("path", ""),
                                item.get("name", ""),
                            )
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        logger.error("后台任务结果发送失败：%s", exc)
                    finally:
                        for item in files:
                            cleanup_file_entry(item.get("path", ""), self.harness.outbox_root)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("大肥鱼对话循环异常：%s", exc)
            await asyncio.sleep(0.4)

    async def _send_reply(self, group, umo, result):
        """禁止多条拆发，每轮回复统一合并成一条完整消息发送。"""
        if not umo:
            return
        raw_parts = result.get("parts") or [result.get("text", "")]
        merged = "\n".join(str(p or "").strip() for p in raw_parts if str(p or "").strip())
        single = merged if result.get("cleaned") else clean(merged)
        if not single or not self.limiter.allow(group):
            return
        await self.context.send_message(umo, MessageChain([Plain(single)]))
        self.limiter.record(group)

    async def _send_image(self, group, umo, path):
        if not umo or not path:
            return
        bank = getattr(self, "emoji_bank", None)
        if not bank or not getattr(bank, "root", None):
            return
        try:
            root = Path(bank.root).resolve(strict=False)
            target_raw = Path(path)
            if target_raw.is_symlink():
                return
            target = target_raw.resolve(strict=True)
        except OSError:
            return
        if target == root or root not in target.parents or not target.is_file():
            return
        if not self.limiter.allow(group):
            return
        try:
            await self.context.send_message(
                umo, MessageChain([Image(file="file://" + str(target))])
            )
            self.limiter.record(group)
        except Exception as exc:
            logger.error("表情发送失败：%s", exc)

    async def _send_file(self, group, umo, path, name=""):
        target = Path(path) if path else None
        display_name = name or (target.name if target else "")
        outbox_root = getattr(getattr(self, "harness", None), "outbox_root", "")
        try:
            if not umo or not target or target.is_symlink() or not target.is_file():
                return
            resolved = target.resolve(strict=True)
            if outbox_root:
                root_resolved = Path(outbox_root).resolve(strict=False)
                if resolved == root_resolved or root_resolved not in resolved.parents:
                    return
            await self.context.send_message(
                umo, MessageChain([File(name=display_name, file=str(resolved))])
            )
        except Exception as exc:
            logger.error("文件发送失败（%s）：%s", display_name or path, exc)
            if umo:
                try:
                    await self.context.send_message(
                        umo,
                        MessageChain([Plain("文件「{}」没发出去，稍后再试试。".format(display_name or "附件"))]),
                    )
                except Exception:
                    pass
        finally:
            if path and outbox_root:
                cleanup_file_entry(path, outbox_root)

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
        uid = str(item.get("user_id") or "").strip()
        umo = item.get("umo") or ""
        if not umo:
            logger.warning("提醒缺少会话标识，已跳过：%s", item.get("text"))
            return
        try:
            if uid:
                chain = MessageChain([At(qq=uid), Plain(" " + str(item["text"]))])
            else:
                chain = MessageChain([Plain("@{} {}".format(item["user_name"], item["text"]))])
            await self.context.send_message(umo, chain)
        except Exception as exc:
            logger.error("提醒发送失败（%s）：%s", item["text"], exc)

"""回声（astrbot_plugin_echo v2）：AstrBot 极薄装配入口。

只负责读取配置、装配 adapters / store / workers / engine、管理后台事件循环与优雅停机；
所有业务逻辑、模型适配、沙箱控制与持久化均已解耦到独立层中。
"""

from __future__ import annotations

import asyncio
import collections
from dataclasses import replace
import os
import sys
import time
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import At, Face, File, Image, Plain, Reply
from astrbot.api.star import Context, Star, StarTools

from .adapters import (
    AstrBotInboundParser,
    AstrBotSender,
    HttpSearchProvider,
    MessageLimiter,
    MockLLMClient,
    build_llm_client,
    resolve_model_config,
)
from .domain import SendFile, SendText
from .engine.agent import EchoAgent, HELP_TEXT
from .engine.scheduler import Scheduler
from .store import EmojiStore, Repository, cleanup_runtime_caches
from .workers import (
    DEFAULT_TIMEOUT,
    SandboxRunner,
    TaskManager,
    cleanup_file_entry,
    cleanup_outbox,
    select_backend,
)

PLUGIN_NAME = "astrbot_plugin_echo"
DEFAULT_HISTORY_LIMIT = 30
MAX_HANDLED_TURNS = 2000


def _data_dir() -> Path:
    try:
        return Path(StarTools.get_data_dir())
    except TypeError:
        return Path(StarTools.get_data_dir(PLUGIN_NAME))


class Echo(Star):
    """AstrBot 插件外壳：收消息 -> Scheduler -> EchoAgent -> AstrBotSender。"""

    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config or {}
        directory = _data_dir()
        directory.mkdir(parents=True, exist_ok=True)
        resolved_dir = directory.resolve(strict=False)
        self._astrbot_data_root = (
            str(resolved_dir.parents[1])
            if len(resolved_dir.parents) >= 2 and resolved_dir.parent.name == "plugin_data"
            else ""
        )

        # 1) Unified SQLite + lossless migration + fail-closed emoji store
        self.repo = Repository(str(directory / "echo.sqlite3"))
        self.emoji_store = EmojiStore(directory / "emojis")

        # 2) Pluggable LLM & optional Search provider
        self.llm = build_llm_client(self.config)
        self.search_provider = HttpSearchProvider.from_config(self.config)

        # 3) Hardened sandboxed worker runner + Codex backend
        raw_timeout = self.config.get("task_timeout")
        env_timeout = os.environ.get("BOT_TASK_TIMEOUT")
        if raw_timeout is None or raw_timeout == "" or (
            env_timeout and str(raw_timeout).strip() == str(DEFAULT_TIMEOUT)
        ):
            raw_timeout = env_timeout
        api_key, api_base, model_name = resolve_model_config(self.config)
        workdir_root = "/tmp/echo-harness" if self._astrbot_data_root else str(directory / "harness")
        outbox_dir = str(directory / "outbox")
        self._outbox_root = outbox_dir
        self.sandbox = SandboxRunner(workdir_root=workdir_root, outbox_root=outbox_dir, timeout=raw_timeout)
        self.backend = select_backend(
            api_base=api_base,
            api_key=api_key,
            model=model_name,
            search_provider=self.search_provider,
        )

        def _extra_secrets() -> tuple[str, ...]:
            out = list(self.llm.extra_secrets() or ())
            extra_fn = getattr(self.search_provider, "extra_secrets", None)
            if callable(extra_fn):
                out.extend(extra_fn() or ())
            else:
                skey = str(getattr(self.search_provider, "api_key", "") or "").strip()
                if len(skey) >= 8:
                    out.append(skey)
            return tuple(out)

        self.task_manager = (
            None
            if isinstance(self.llm, MockLLMClient)
            else TaskManager(self.sandbox, self.backend, repo=self.repo, extra_secrets_fn=_extra_secrets)
        )
        cleanup_outbox(outbox_dir, keep_paths=self.repo.pending_delivery_paths())

        # 4) Core Engine (Scheduler + EchoAgent)
        raw_hist = self.config.get("history_limit")
        env_hist = os.environ.get("BOT_HISTORY_LIMIT")
        if raw_hist is None or raw_hist == "" or (
            env_hist and str(raw_hist).strip() == str(DEFAULT_HISTORY_LIMIT)
        ):
            raw_hist = env_hist
        hist_limit = DEFAULT_HISTORY_LIMIT
        if raw_hist not in (None, "") and not isinstance(raw_hist, bool):
            try:
                hist_limit = max(1, min(200, int(raw_hist)))
            except (TypeError, ValueError):
                pass

        self.scheduler = Scheduler()
        self.agent = EchoAgent(
            self.repo,
            self.llm,
            self.scheduler,
            emoji_store=self.emoji_store,
            task_runner=self.task_manager,
            search_provider=self.search_provider,
            history_limit=hist_limit,
        )

        # 5) AstrBot protocol adapters
        self._comp_types = {
            "Plain": Plain,
            "At": At,
            "Reply": Reply,
            "Face": Face,
            "Image": Image,
            "File": File,
        }
        self.inbound = AstrBotInboundParser(
            self._comp_types,
            allowed_local_roots=(self._astrbot_data_root,) if self._astrbot_data_root else (),
        )
        self.limiter = MessageLimiter(per_minute=8)
        self._bots: dict[str, object] = {}
        self.sender = AstrBotSender(
            context,
            self._comp_types,
            MessageChain,
            emoji_root=self.emoji_store.root,
            outbox_root=outbox_dir,
            limiter=self.limiter,
            bot_provider=lambda gid="": self._bots.get(gid),
            repo=self.repo,
            logger=logger,
            on_invalid_face=self.emoji_store.remove_face,
        )

        self._group_locks: dict[str, asyncio.Lock] = collections.defaultdict(asyncio.Lock)
        self._handled_turns: collections.OrderedDict[str, float] = collections.OrderedDict()
        self._bg_tasks: set[asyncio.Task] = set()
        self._chat_task: asyncio.Task | None = None
        self._cleanup_at = 0.0

    def _spawn_bg(self, coro) -> asyncio.Task:
        loop = asyncio.get_running_loop()
        task = loop.create_task(coro)
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)
        return task

    def _ensure_chat_task(self) -> None:
        if self._chat_task is not None and not self._chat_task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._chat_task = loop.create_task(self._chat_loop(), name="echo-chat-v2")

    async def initialize(self):
        self._ensure_chat_task()

    @filter.on_astrbot_loaded()
    async def on_loaded(self):
        self._ensure_chat_task()

    async def terminate(self):
        tasks = []
        if self._chat_task is not None:
            self._chat_task.cancel()
            tasks.append(self._chat_task)
            self._chat_task = None
        for t in list(self._bg_tasks):
            t.cancel()
            tasks.append(t)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self.task_manager is not None:
            try:
                await asyncio.to_thread(self.task_manager.shutdown, 3.0)
            except Exception:
                pass
        try:
            await asyncio.to_thread(self.agent.wait_background, 2.0)
        except Exception:
            pass
        try:
            self.repo.close()
        except Exception:
            pass

    async def _collect_stickers(self, group_id: str, images: tuple) -> None:
        for media in images:
            if not media.is_sticker or not media.data:
                continue
            try:
                label = await asyncio.to_thread(self.emoji_store.collect, group_id, media.data, self.llm)
                if label:
                    logger.info("表情已收：%s", label)
            except Exception as exc:
                logger.info("表情收集失败：%s", exc)

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE, priority=sys.maxsize)
    async def on_group_message(self, event: AstrMessageEvent):
        """入口仅负责协议解析与入队，所有群消息均在此统一接管（或显式放行 passthrough）。"""
        try:
            event.should_call_llm(True)
        except Exception:
            pass
        self._ensure_chat_task()
        raw_gid = event.get_group_id()
        if raw_gid:
            gid = "{}:{}".format(event.get_platform_name(), raw_gid)
            bot = getattr(event, "bot", None)
            if bot is not None:
                self._bots[gid] = bot
            last_bot_at = self.scheduler.last_reply_at(gid)
        else:
            last_bot_at = 0.0

        parsed = await self.inbound.parse(event, last_bot_reply_at=last_bot_at)
        if parsed is None or parsed.passthrough:
            return

        if parsed.is_poke:
            if not self.scheduler.allow_poke(parsed.group_id, now=parsed.ts):
                event.stop_event()
                return
            if not parsed.user_name or parsed.user_name == parsed.user_id:
                aliases = await asyncio.to_thread(self.repo.person_aliases, parsed.group_id, parsed.user_id)
                resolved_name = next((a for a in aliases if a and a != parsed.user_id), "") or "群友"
                parsed = replace(parsed, user_name=resolved_name)

        if parsed.raw_text in ("/帮助", "／帮助", "/help"):
            await asyncio.to_thread(
                self.repo.add_message, parsed.group_id, parsed.user_id, parsed.user_name, "user", parsed.raw_text, parsed.ts, parsed.msg_id
            )
            await asyncio.to_thread(self.repo.add_message, parsed.group_id, "bot", "大肥鱼", "bot", HELP_TEXT)
            yield event.plain_result(HELP_TEXT)
            event.stop_event()
            return

        # 收集 QQ 内置表情 & 异步入库真表情包（绝不阻塞消息进队列）
        for f in parsed.faces:
            await asyncio.to_thread(self.emoji_store.collect_face, parsed.group_id, f.face_id, f.label)
        if parsed.has_sticker:
            self._spawn_bg(self._collect_stickers(parsed.group_id, parsed.images))

        # 保留新近非表情截图供后续追问使用（表情包不混入识图上下文）
        non_stickers = [m for m in parsed.images if not m.is_sticker]
        if non_stickers:
            self.scheduler.attach_images(
                parsed.group_id, non_stickers, now=parsed.ts, sender_id=parsed.user_id
            )

        # 未被 @ 的纯图片/表情只落历史与人类轮次轨迹，不吵群；若包含正文或被 @ 则进调度队列
        if not parsed.text and not parsed.mentioned:
            self.scheduler.observe_human(parsed.group_id, parsed.user_id, now=parsed.ts)
            event.stop_event()
            return
        if parsed.bare_face and not parsed.mentioned and parsed.has_image:
            self.scheduler.observe_human(parsed.group_id, parsed.user_id, now=parsed.ts)
            await asyncio.to_thread(
                self.repo.add_message,
                parsed.group_id,
                parsed.user_id,
                parsed.user_name,
                "user",
                parsed.text,
                parsed.ts,
                parsed.msg_id,
            )
            await asyncio.to_thread(
                self.repo.observe_person, parsed.group_id, parsed.user_id, parsed.user_name, parsed.ts
            )
            event.stop_event()
            return

        self.scheduler.enqueue(parsed)
        event.stop_event()

    def _mark_handled(self, turn_id: str) -> None:
        if not turn_id:
            return
        self._handled_turns[turn_id] = time.time()
        self._handled_turns.move_to_end(turn_id)
        while len(self._handled_turns) > MAX_HANDLED_TURNS:
            self._handled_turns.popitem(last=False)

    async def _process_batch(self, batch) -> None:
        gid = batch.group_id
        lock = self._group_locks[gid]
        # 关键防堵设计：未 @ 的闲聊在锁忙或近期上游报错时绝不排队，直接落库跳过！
        if not batch.mentioned and lock.locked():
            await asyncio.to_thread(self.agent.observe_skipped, batch)
            return

        async with lock:
            if batch.turn_id and batch.turn_id in self._handled_turns:
                return
            recent_errs = await asyncio.to_thread(self.repo.recent_error_count, gid, 120.0)
            if not self.scheduler.should_view(batch, time.time(), group_busy=False, recent_errors=recent_errs):
                await asyncio.to_thread(self.agent.observe_skipped, batch)
                self._mark_handled(batch.turn_id)
                return

            self._mark_handled(batch.turn_id)
            result = await asyncio.to_thread(self.agent.handle_batch, batch)
            if result.get("reason") == "model_error":
                logger.error("模型调用失败：%s", result.get("error") or "未知错误")
            actions = result.get("actions") or ()
            if not actions:
                return
            for act in actions:
                try:
                    await self.sender.send(act)
                except Exception as exc:
                    logger.error("消息发送失败：%s", exc)

    async def _deliver_one_task(self, delivery) -> None:
        if self.task_manager is None:
            return
        gid = str(getattr(delivery, "group_id", "") or "")
        tid = str(getattr(delivery, "task_id", "") or "")
        acked = False
        files: list[dict] = []
        try:
            async with self._group_locks[gid]:
                row = await asyncio.to_thread(self.task_manager.present_delivery, delivery, self.llm)
                gid = str(row.get("group_id", "") or gid)
                umo = str(row.get("umo", "") or "")
                files = list(row.get("files") or ())
                if not row.get("ok") and row.get("error"):
                    logger.warning("后台子任务失败（group=%s）：%s", gid, row.get("error"))
                if not umo:
                    await asyncio.to_thread(self.task_manager.ack_delivery, tid)
                    acked = True
                    return
                if row.get("text"):
                    sent = await self.sender.send(SendText(gid, umo, row["text"]))
                    if sent is not False:
                        self.scheduler.record_bot_reply(
                            gid, user_id="", offered=False, from_mentioned=True
                        )
                for f in files:
                    await self.sender.send(SendFile(gid, umo, f.get("path", ""), f.get("name", "")))
                await asyncio.to_thread(self.task_manager.ack_delivery, tid)
                acked = True
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("后台结果发送失败：%s", exc)
        finally:
            if acked:
                for f in files:
                    cleanup_file_entry(f.get("path", ""), self._outbox_root)
            else:
                try:
                    self.task_manager.release_delivery(tid)
                except Exception:
                    pass

    async def _deliver_completed_tasks(self) -> None:
        if self.task_manager is None:
            return
        for delivery in await asyncio.to_thread(self.task_manager.poll_deliveries):
            self._spawn_bg(self._deliver_one_task(delivery))

    async def _chat_loop(self) -> None:
        while True:
            try:
                now = time.time()
                for batch in self.scheduler.due(now):
                    self._spawn_bg(self._process_batch(batch))

                await self._deliver_completed_tasks()

                if now - self._cleanup_at > 21600:
                    self._cleanup_at = now
                    try:
                        await asyncio.to_thread(self.emoji_store.cleanup)
                        await asyncio.to_thread(self.repo.prune_messages)
                        await asyncio.to_thread(self.repo.prune_monitor)
                        keep = await asyncio.to_thread(self.repo.pending_delivery_paths)
                        await asyncio.to_thread(cleanup_outbox, self._outbox_root, keep_paths=keep)
                        if self._astrbot_data_root:
                            await asyncio.to_thread(cleanup_runtime_caches, self._astrbot_data_root)
                    except Exception as exc:
                        logger.error("定期缓存清理异常：%s", exc)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("大肥鱼对话循环异常：%s", exc)
            await asyncio.sleep(0.35)

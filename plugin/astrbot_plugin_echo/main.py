"""回声（astrbot_plugin_echo）

AstrBot 插件入口：只负责把平台事件转成批次、把核心层的动作发回去。
核心逻辑在 core/ 中，不 import 任何框架模块，因此可以独立测试。
"""

import asyncio
import base64
import collections
import ipaddress
import os
import re
import socket
import struct
import sys
import time
import types
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import At, Face, File, Image, Plain, Reply
from astrbot.api.star import Context, Star, StarTools

from .core import mentions
from .core.attention import AttentionTracker
from .core.brain import DEFAULT_HISTORY_LIMIT, PERSONA, Brain
from .core.display import MessageLimiter, clean
from .core.forward import extract as forward_extract
from .core.forward import looks_like_forward
from .core.forward import render as forward_render
from .core.heartbeat import Heartbeat
from .core.harness import DEFAULT_TIMEOUT, CodexHarness, cleanup_file_entry, cleanup_outbox
from .core.memory import Memory
from .core.messages import Message, format_speaker_message
from .core.models import MockModel, build_model, resolve_model_config
from .core.modes import ModePolicy
from .core.monitor import Monitor
from .core.person_info import PersonStore
from .core.prompts import PersonaLibrary, PromptStore
from .core.rhythm import IMAGE_TTL, SILENT_MAX_AGE, Item, Rhythm
from .core.schedule import Schedule
from .core.store import Store, cleanup_runtime_caches
from .core.subagent import SubAgentManager
from .core.vision import FACE_LABELS, EmojiBank

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


def _is_sticker_image(part, data: bytes) -> bool:
    """区分表情包与普通截图/照片，避免大截图混入表情包仓库。"""
    sub_type = getattr(part, "subType", None)
    if sub_type is None:
        sub_type = getattr(part, "sub_type", None)
    summary = str(getattr(part, "summary", "") or "")
    if "表情" in summary or str(sub_type or "") in ("1", "2", "sticker", "emoji"):
        return True
    if sub_type is not None and str(sub_type) == "0":
        return False
    if len(data) > 300 * 1024:
        return False
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
        w, h = struct.unpack(">II", data[16:24])
        if max(w, h) > 800:
            return False
    return True


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
        env_timeout = os.environ.get("BOT_TASK_TIMEOUT")
        if raw_timeout is None or raw_timeout == "" or (
            env_timeout and str(raw_timeout).strip() == str(DEFAULT_TIMEOUT)
        ):
            raw_timeout = env_timeout
        api_key, api_base, model_name = resolve_model_config(self.config)
        harness_workdir = (
            "/tmp/echo-harness"
            if self._astrbot_data_root
            else str(directory / "harness")
        )
        outbox_dir = str(directory / "outbox")
        self._outbox_root = outbox_dir
        self.harness = (
            None
            if isinstance(self.model, MockModel)
            else CodexHarness(
                home=str(directory / "codex"),
                workdir_root=harness_workdir,
                outbox_root=outbox_dir,
                api_base=api_base,
                api_key=api_key,
                model=model_name,
                timeout=raw_timeout,
            )
        )
        cleanup_outbox(
            outbox_dir,
            keep_paths=self.store.pending_delivery_paths(),
        )
        self.limiter = MessageLimiter(per_minute=8, per_burst=3)
        self._last_umo = {}
        self._heartbeat_at = 0.0
        self._emoji_cleanup_at = 0.0
        self._handled_turns = collections.OrderedDict()
        self._bg_tasks = set()
        self._group_locks = collections.defaultdict(asyncio.Lock)
        self._pending_images = collections.defaultdict(list)
        self._download_sem = None
        raw_history_limit = self.config.get("history_limit")
        env_history_limit = os.environ.get("BOT_HISTORY_LIMIT")
        if raw_history_limit is None or raw_history_limit == "" or (
            env_history_limit and str(raw_history_limit).strip() == str(DEFAULT_HISTORY_LIMIT)
        ):
            raw_history_limit = env_history_limit
        self.reminders_enabled = bool(self.config.get("reminder_enabled", True))
        self.brain = Brain(
            self.store,
            self.model,
            persona=PERSONA,
            memory=self.memory,
            emoji_bank=self.emoji_bank,
            char_frequency=str(directory / "char_frequency.json"),
            capabilities={
                "view_image": True,
                "send_face": True,
                "schedule": self.reminders_enabled,
                "wait": True,
                "subagent": not isinstance(self.model, MockModel),
            },
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
            reminders_enabled=self.reminders_enabled,
        )
        self._reminder_task = None
        self._chat_task = None

    def _spawn_bg(self, coro):
        loop = asyncio.get_running_loop()
        task = loop.create_task(coro)
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)
        return task

    async def initialize(self):
        """插件重载时（OnAstrBotLoadedEvent 不重发）拉起后台任务循环。"""
        self._ensure_reminder_task()
        self._ensure_chat_task()

    @filter.on_astrbot_loaded()
    async def on_loaded(self):
        self._ensure_reminder_task()
        self._ensure_chat_task()

    async def terminate(self):
        """插件卸载时等待后台协程与摘要线程收尾，再关闭全部数据库连接。"""
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
        brain = getattr(self, "brain", None)
        wait_bg = getattr(brain, "wait_background", None)
        if callable(wait_bg):
            try:
                await asyncio.to_thread(wait_bg, 2.0)
            except Exception:
                pass
        for attr in ("store", "memory", "persons", "monitor", "schedule"):
            db_obj = getattr(self, attr, None)
            close_fn = getattr(db_obj, "close", None)
            if callable(close_fn):
                try:
                    close_fn()
                except Exception:
                    pass

    @staticmethod
    def _extract_raw_text(event: AstrMessageEvent, parts) -> str:
        """找回可能被 AstrBot WakingCheckStage 剥掉的开头命令前缀 / 或 ／。"""
        msg_str = (event.get_message_str() or "").strip()
        if msg_str.startswith(("/", "／")):
            return msg_str
        plain_str = "".join(
            str(getattr(p, "text", "") or "")
            for p in parts
            if isinstance(p, Plain)
        ).strip()
        obj_str = str(getattr(getattr(event, "message_obj", None), "message_str", "") or "").strip()
        for cand in (plain_str, obj_str):
            stripped = cand.lstrip()
            if stripped.startswith(("/", "／")):
                if not msg_str or stripped[1:].strip() == msg_str:
                    return stripped
        return msg_str

    @staticmethod
    def _has_passthrough_component(parts) -> bool:
        pass_types = {"json", "share", "poke", "record", "video", "music", "location"}
        for part in parts:
            cls_name = type(part).__name__.lower()
            comp_type = str(getattr(part, "type", "") or "").lower()
            if cls_name in pass_types or comp_type in pass_types:
                return True
        return False

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE, priority=sys.maxsize)
    async def on_group_message(self, event: AstrMessageEvent):
        """命令即时回；闲聊进防抖缓冲，由后台循环按批决策。"""
        # 在 AstrBot v4.29.4 中 call_llm=True 表示模型请求已被接管，避免内置默认 LLM 再插一脚或报无模型错误。
        try:
            event.should_call_llm(True)
        except Exception:
            pass
        self._ensure_reminder_task()
        self._ensure_chat_task()
        # 非消息事件（通知/戳一戳等）原样放行。
        raw_msg = getattr(getattr(event, "message_obj", None), "raw_message", None)
        if isinstance(raw_msg, dict) and raw_msg.get("post_type") not in (None, "", "message"):
            return
        sender = str(event.get_sender_id() or "")
        if not sender or sender == str(event.get_self_id() or ""):
            return
        raw_gid = str(event.get_group_id() or "").strip()
        if not raw_gid:
            return
        parts = list(event.get_messages() or [])
        text = self._extract_raw_text(event, parts)
        mentioned = self._mentioned(event, text)
        # 合并转发：先直接拆包，拿不到节点内容再调用 OneBot get_forward_msg。
        forward_text = ""
        for part in parts:
            if looks_like_forward(part):
                lines = forward_extract(part)
                if not lines:
                    fid = str(getattr(part, "id", "") or "").strip()
                    bot = getattr(event, "bot", None)
                    call_action = getattr(bot, "call_action", None)
                    if fid and callable(call_action):
                        for key in ("id", "message_id"):
                            try:
                                res = await asyncio.wait_for(
                                    call_action("get_forward_msg", **{key: fid}),
                                    timeout=5.0,
                                )
                                lines = forward_extract(res)
                                if lines:
                                    break
                            except Exception:
                                continue
                forward_text = forward_render(lines)
                break
        group_id = "{}:{}".format(event.get_platform_name(), raw_gid)
        umo = str(event.unified_msg_origin or "")
        user_name = str(event.get_sender_name() or sender)
        self.heartbeat.observe(group_id)
        # 含链接且未被 @，或卡片/语音/戳一戳/视频等组件且无正文：放行给 link_resolver 等其它插件。
        if ("http://" in text or "https://" in text) and not mentioned:
            return
        if not text and not forward_text and self._has_passthrough_component(parts):
            return
        # 无论是否被 @，群里发出的图片都先快速下载挂入待看图队列，供几秒内追问「这是什么」时读取。
        has_image = False
        collected_images = 0
        face_tags = []
        mface_tags = []

        def _track_image(part_obj, attach_flag):
            nonlocal collected_images
            if collected_images >= 2:
                return
            collected_images += 1
            bg = self._spawn_bg(self._collect_image(group_id, part_obj, attach=attach_flag))
            self._pending_images[group_id].append(bg)
            bg.add_done_callback(
                lambda t, g=group_id: self._pending_images[g].remove(t)
                if t in self._pending_images.get(g, ()) else None
            )

        for part in parts:
            if isinstance(part, Face):
                fid = str(getattr(part, "id", "") or "").strip()
                fname = str(getattr(part, "name", "") or FACE_LABELS.get(fid) or "").strip()
                if self.emoji_bank and fid:
                    await asyncio.to_thread(
                        self.emoji_bank.collect_face, group_id, fid, fname,
                    )
                tag_label = fname or (("ID" + fid) if fid else "")
                if tag_label and len(face_tags) < 3:
                    face_tags.append("[QQ表情:{}]".format(tag_label))
            elif isinstance(part, Image):
                has_image = True
                _track_image(part, True)
            elif isinstance(part, Reply) and mentioned:
                for qseg in getattr(part, "chain", None) or []:
                    if isinstance(qseg, Image):
                        has_image = True
                        _track_image(qseg, True)

        # AstrBot aiocqhttp 适配器会跳过 QQ 商城表情 (mface)，这里从 raw_message 补回文案与图片 URL。
        if isinstance(raw_msg, dict) and isinstance(raw_msg.get("message"), list):
            for seg in raw_msg["message"]:
                if isinstance(seg, dict) and seg.get("type") == "mface":
                    mdata = seg.get("data") or {}
                    msum = str(mdata.get("summary") or "").strip().strip("[]")
                    murl = str(mdata.get("url") or "").strip()
                    if len(mface_tags) < 2:
                        mface_tags.append("[商城表情:{}]".format(msum or "表情"))
                    if murl:
                        has_image = True
                        _track_image(
                            types.SimpleNamespace(
                                url=murl, file="", path="", base64="",
                                subType="1", summary=msum,
                            ),
                            True,
                        )

        media_prefix = " ".join(face_tags + mface_tags).strip()
        bare_face = False
        if not text and not forward_text:
            if not mentioned:
                if has_image or mface_tags:
                    marker = " ".join(mface_tags + (["[图片]"] if has_image else [])).strip()
                    await asyncio.to_thread(
                        self.store.add_message, group_id, sender, user_name, "user", marker
                    )
                    event.stop_event()
                    return
                last_bot_at = float(getattr(getattr(self, "brain", None), "_last_reply_at", {}).get(group_id, 0.0) or 0.0)
                if face_tags and time.time() - last_bot_at <= 120.0:
                    text = " ".join(face_tags).strip()
                    bare_face = True
                else:
                    event.stop_event()
                    return
            elif media_prefix:
                text = (media_prefix + (" [图片]" if has_image else "")).strip()
            elif has_image:
                text = "[图片]"
            else:
                text = "（只 @ 了你一下，没说话）"
        elif not text.startswith(("/", "／")):
            if media_prefix:
                text = "{} {}".format(media_prefix, text).strip()
            if has_image and "[图片]" not in text:
                text = "{} [图片]".format(text).strip()

        self._last_umo[group_id] = umo
        message = Message(group_id, sender, user_name, text, mentioned, umo)
        # 新消息提前唤醒 wait 倒计时。
        self.rhythm.wake(group_id)

        # 命令必须带 / 或 ／ 前缀才拦截，避免日常聊天误触发；命中则先 yield 再 stop_event 保证 RespondStage 发出。
        if text.startswith(("/", "／")):
            direct = await asyncio.to_thread(self.brain.command, message, text)
            if direct is not None:
                self.heartbeat.observe(group_id)
                yield event.plain_result(direct)
                event.stop_event()
                return

        quote_text = self._extract_quote(event)
        self.rhythm.add(group_id, Item(
            (text + (" " + forward_text if forward_text else "")).strip(),
            sender, user_name, mentioned, umo, time.time(),
            kind="image" if has_image else ("face" if bare_face else "text"), quote=quote_text,
        ))
        event.stop_event()
        return

    async def _save_emoji(self, group_id, data):
        if not self.emoji_bank:
            return
        try:
            label = await asyncio.to_thread(
                self.emoji_bank.collect, group_id, data, self.model
            )
            if label:
                logger.info("表情已收：%s", label)
        except Exception as exc:
            logger.info("表情收集失败：%s", exc)

    async def _collect_image(self, group_id, part, attach=True):
        if not self.emoji_bank and not attach:
            return
        url = str(getattr(part, "url", "") or "").strip()
        file_ref = str(getattr(part, "file", "") or "").strip()
        path_ref = str(getattr(part, "path", "") or "").strip()
        b64_ref = str(getattr(part, "base64", "") or "").strip()
        data = b""
        if self._download_sem is None:
            self._download_sem = asyncio.Semaphore(4)
        async with self._download_sem:
            for remote_url in (url, file_ref):
                if not data and remote_url.startswith(("http://", "https://")):
                    try:
                        data = await asyncio.to_thread(self._download, remote_url)
                    except Exception as exc:
                        logger.info("表情下载失败：%s", exc)
            if not data:
                for b64_src in (b64_ref, file_ref):
                    if b64_src.startswith("base64://") and len(b64_src) < 3 * 1024 * 1024:
                        try:
                            data = base64.b64decode(b64_src[9:], validate=False)
                        except Exception:
                            data = b""
                        break
            if not data:
                candidates = []
                if path_ref:
                    candidates.append(path_ref)
                if file_ref.startswith("file://"):
                    candidates.append(urllib.parse.unquote(urllib.parse.urlsplit(file_ref).path))
                elif file_ref and not file_ref.startswith(("http://", "https://", "base64://")):
                    candidates.append(file_ref)
                for local_path in candidates:
                    try:
                        p = Path(local_path)
                        if not p.is_symlink() and p.is_file() and p.stat().st_size <= MAX_DOWNLOAD_BYTES:
                            data = await asyncio.to_thread(p.read_bytes)
                            if data:
                                break
                    except OSError:
                        data = b""
        if not _looks_like_image(data):
            return
        if attach:
            self.rhythm.attach_image(group_id, data)
        if self.emoji_bank and _is_sticker_image(part, data):
            # VLM 入库标注放到独立后台任务，绝不阻塞 _pending_images 的看图放行。
            self._spawn_bg(self._save_emoji(group_id, data))

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
            chain = getattr(part, "chain", None) or []
            body = str(getattr(part, "message_str", "") or getattr(part, "text", "") or "").strip()
            if not body:
                segs = []
                for seg in chain:
                    if isinstance(seg, Image):
                        segs.append("[图片]")
                    elif isinstance(seg, Face):
                        fid = str(getattr(seg, "id", "") or "").strip()
                        fname = str(getattr(seg, "name", "") or FACE_LABELS.get(fid) or "").strip()
                        segs.append("[QQ表情:{}]".format(fname or fid or "表情"))
                    else:
                        seg_text = getattr(seg, "text", None)
                        if seg_text:
                            segs.append(str(seg_text))
                body = " ".join(segs).strip()
            elif any(isinstance(seg, Image) for seg in chain) and "[图片]" not in body:
                body = (body + " [图片]").strip()
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
        has_at = False
        has_reply = False
        for part in event.get_messages():
            if isinstance(part, At) and str(part.qq) == self_id:
                has_at = True
            # 引用机器人的消息，视为在跟它说话。
            if isinstance(part, Reply) and str(getattr(part, "sender_id", "")) == self_id:
                has_reply = True
        return mentions.detect(
            text, at=has_at, reply=has_reply, self_names=("大肥鱼", "回声"),
        ).mentioned

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

    async def _process_batch(self, batch):
        group = batch.group_id
        async with self._group_locks[group]:
            turn = getattr(batch, "turn_id", "")
            if turn and turn in self._handled_turns:
                return
            if self.rhythm.is_muted(group):
                self._mark_turn_handled(turn)
                return
            now = time.time()
            # 批内包含图片且下载尚未收尾时，最多等 5 秒并补充挂到 batch.images。
            pending_dl = list(self._pending_images.get(group) or ())
            if pending_dl and (batch.has_image or batch.mentioned):
                try:
                    await asyncio.wait_for(
                        asyncio.gather(*pending_dl, return_exceptions=True),
                        timeout=5.0,
                    )
                except asyncio.TimeoutError:
                    pass
                except Exception:
                    pass
            fresh_imgs = [
                data for stamp, data in self.rhythm._images.pop(group, [])
                if time.time() - stamp <= IMAGE_TTL
            ]
            if fresh_imgs:
                existing = list(getattr(batch, "images", None) or [])
                batch.images = (existing + fresh_imgs)[-4:]
            if not batch.approved and not self.rhythm.should_view(
                group, now, forced=batch.mentioned, rate=self.brain.view_rate(batch)
            ):
                # 未通过查看抽签的批先落历史，暂不标 handled，留给静默查看重看一次的机会。
                await asyncio.to_thread(self.brain.observe_skipped, batch)
                batch.persisted = True
                return
            self._mark_turn_handled(turn)
            quota = self.rhythm.start_turn(group)
            started_at = time.time()
            result = await asyncio.to_thread(self.brain.handle_batch, batch)
            self.rhythm.use_round(group, quota, count=result.get("rounds", 1))
            reason = result.get("reason")
            replay_count = int(getattr(batch, "replay_count", 0) or 0)
            # 仅 wait 且重放次数小于 2 时允许到点再决策一次；已决策 silent 的批不重复唤醒。
            if turn and reason == "wait" and replay_count < 2:
                self._handled_turns.pop(turn, None)
            if reason == "model_error":
                logger.error("模型调用失败：%s", result.get("error") or "未知错误")
            if reason == "wait":
                if self.rhythm._last_input.get(group, 0.0) <= started_at:
                    seconds = result.get("wait_seconds") or 0
                    self.rhythm.wait(group, seconds, time.time())
                return
            if not result.get("replied") or self.rhythm.is_muted(group):
                return
            if result.get("face"):
                try:
                    face = Face(id=int(result["face"]))
                except (TypeError, ValueError):
                    face = None
                if face is not None and self.limiter.allow(group):
                    await self.context.send_message(batch.umo, MessageChain([face]))
                    self.limiter.record(group)
                    hb = getattr(self, "heartbeat", None)
                    if hb is not None:
                        hb.observe(group)
            elif result.get("image"):
                await self._send_image(group, batch.umo, result["image"])
            else:
                await self._send_reply(group, batch.umo, result)

    async def _chat_loop(self):
        """防抖到期 → 并发按群隔离决策 → 单条发送；同时推进 wait/静默/主动开聊/清理/子任务投递。"""
        while True:
            try:
                now = time.time()
                pending = list(self.rhythm.due(now))
                due_groups = {b.group_id for b in pending}
                # wait 到期：按当时概率抽签（@ 批直接放行），排除本轮已有待处理批的群。
                for group in list(self.rhythm.groups()):
                    if group in due_groups:
                        continue
                    if not self.rhythm.has_batch(group) and self.rhythm.wait_due(group, now):
                        last = self.rhythm.last_batch(group)
                        if last is not None:
                            self.rhythm.requeue(last, now)
                # 静默查看：安静太久且上次跳过未看时，在冷却后抽签重看一次。
                for group in list(self.rhythm.groups()):
                    if group in due_groups:
                        continue
                    last = self.rhythm.last_batch(group)
                    last_turn = getattr(last, "turn_id", "") if last is not None else ""
                    last_items = getattr(last, "items", None) or [] if last is not None else []
                    last_ts = (
                        max(
                            float(getattr(last, "started", 0.0) or 0.0),
                            float(getattr(last_items[-1], "ts", 0.0) or 0.0) if last_items else 0.0,
                        )
                        if last is not None else 0.0
                    )
                    if (
                        last is not None
                        and (not last_turn or last_turn not in self._handled_turns)
                        and int(getattr(last, "replay_count", 0) or 0) < 1
                        and (last_ts <= 0 or now - last_ts <= SILENT_MAX_AGE)
                        and not self.rhythm.has_batch(group)
                        and self.rhythm.silent_due(group, now)
                        and self.rhythm.consume_silent(group, now)
                    ):
                        self.rhythm.requeue(last, now)
                pending.extend(self.rhythm.due(now))
                for batch in pending:
                    self._spawn_bg(self._process_batch(batch))

                now = time.time()
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

                now = time.time()
                # 每 6 小时清理一次过期表情、超限群消息、监控旧事件、过期 outbox 及 QQ/临时缓存。
                if now - self._emoji_cleanup_at > 21600:
                    self._emoji_cleanup_at = now
                    try:
                        if self.emoji_bank:
                            await asyncio.to_thread(self.emoji_bank.cleanup)
                        await asyncio.to_thread(self.store.prune_messages)
                        await asyncio.to_thread(self.monitor.prune)
                        outbox_root = getattr(self, "_outbox_root", "") or (
                            getattr(self.harness, "outbox_root", "") if getattr(self, "harness", None) else ""
                        )
                        if outbox_root:
                            keep = await asyncio.to_thread(self.store.pending_delivery_paths)
                            await asyncio.to_thread(cleanup_outbox, outbox_root, keep_paths=keep)
                        data_root = getattr(self, "_astrbot_data_root", "")
                        if data_root:
                            await asyncio.to_thread(cleanup_runtime_caches, data_root)
                    except Exception as exc:
                        logger.error("定期缓存清理异常：%s", exc)

                # 后台任务完成：把结论及生成的群文件发回原群（逐任务异常隔离，确保不漏发不遗留 outbox）。
                outbox_cleanup_root = (
                    getattr(self.harness, "outbox_root", "")
                    if getattr(self, "harness", None)
                    else getattr(self, "_outbox_root", "")
                )
                for task in await asyncio.to_thread(self.brain.poll_subagents):
                    group = task.get("group", "")
                    umo = task.get("umo") or ""
                    files = list(task.get("files") or [])
                    if not task.get("ok") and task.get("error"):
                        logger.warning("后台子任务失败（group=%s）：%s", group, task.get("error"))
                    try:
                        if not umo or (group and self.rhythm.is_muted(group)):
                            continue
                        part = task.get("text", "") if task.get("cleaned") else clean(task.get("text", ""))
                        if part:
                            await self.context.send_message(
                                umo, MessageChain([Plain(part)])
                            )
                            hb = getattr(self, "heartbeat", None)
                            if hb is not None:
                                hb.observe(group)
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
                            cleanup_file_entry(item.get("path", ""), outbox_cleanup_root)
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
        hb = getattr(self, "heartbeat", None)
        if hb is not None:
            hb.observe(group)

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
            hb = getattr(self, "heartbeat", None)
            if hb is not None:
                hb.observe(group)
        except Exception as exc:
            logger.error("表情发送失败：%s", exc)

    async def _send_file(self, group, umo, path, name=""):
        target = Path(path) if path else None
        display_name = name or (target.name if target else "")
        outbox_root = (
            getattr(self.harness, "outbox_root", "")
            if getattr(self, "harness", None)
            else getattr(self, "_outbox_root", "")
        )
        try:
            if not umo or not target or target.is_symlink() or not target.is_file():
                logger.warning("待发送文件缺失或越界，跳过：%s", display_name or path)
                return
            resolved = target.resolve(strict=True)
            if outbox_root:
                root_resolved = Path(outbox_root).resolve(strict=False)
                if resolved == root_resolved or root_resolved not in resolved.parents:
                    return
            await self.context.send_message(
                umo, MessageChain([File(name=display_name, file=str(resolved))])
            )
            store = getattr(self, "store", None)
            if store and group and display_name:
                await asyncio.to_thread(
                    store.add_message,
                    group,
                    "bot",
                    "大肥鱼",
                    "bot",
                    "[已发送群文件：{}]".format(display_name),
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
                for item in await asyncio.to_thread(self.brain.peek_due_reminders):
                    sent = await self._send_reminder(item)
                    if sent:
                        await asyncio.to_thread(self.brain.confirm_reminder, item)
                    else:
                        await asyncio.to_thread(self.brain.backoff_reminder, item, 60)
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
            return True
        try:
            if uid:
                chain = MessageChain([At(qq=uid), Plain(" " + str(item["text"]))])
            else:
                chain = MessageChain([Plain("@{} {}".format(item["user_name"], item["text"]))])
            await self.context.send_message(umo, chain)
            return True
        except Exception as exc:
            logger.error("提醒发送失败（%s）：%s", item["text"], exc)
            return False

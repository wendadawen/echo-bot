"""AstrBot + NapCat 出站发送器与群级滑动窗口限流。"""

from __future__ import annotations

import asyncio
import base64
import time
from pathlib import Path
from typing import Any, Callable, Optional

from ..domain import (
    OutboundAction,
    ReactEmoji,
    SendEmoji,
    SendFace,
    SendFile,
    SendText,
    clean_display_text,
)
from ..workers.sandbox import cleanup_file_entry

_INVALID_FACE_ERROR_MARKERS = ("1200", "unsupported", "不支持", "invalid", "face")


class MessageLimiter:
    """滑动窗口发送限流：每群每分钟最多 per_minute 条，避免刷屏。"""

    def __init__(self, per_minute: int = 8):
        self.per_minute = max(0, int(per_minute))
        self._sent: dict[str, list[float]] = {}

    def _trim(self, group_id: str, now: float) -> list[float]:
        gid = str(group_id)
        old = self._sent.get(gid)
        if not old:
            return []
        rows = [ts for ts in old if now - ts < 60.0]
        if rows:
            self._sent[gid] = rows
        else:
            self._sent.pop(gid, None)
        return rows

    def allow(self, group_id: str, count: int = 1, now: Optional[float] = None) -> bool:
        cur = time.monotonic() if now is None else float(now)
        return len(self._trim(group_id, cur)) + count <= self.per_minute

    def record(self, group_id: str, count: int = 1, now: Optional[float] = None) -> int:
        cur = time.monotonic() if now is None else float(now)
        if self.per_minute <= 0:
            self._sent.pop(str(group_id), None)
            return 0
        rows = self._trim(group_id, cur)
        rows.extend([cur] * max(0, int(count)))
        kept = rows[-self.per_minute:]
        if kept:
            self._sent[str(group_id)] = kept
        else:
            self._sent.pop(str(group_id), None)
        return len(kept)

    def try_acquire(self, group_id: str, count: int = 1, now: Optional[float] = None) -> bool:
        """原子校验并预占发送额度，防止并发协程下的 check-then-act 穿透。"""
        cur = time.monotonic() if now is None else float(now)
        if self.per_minute <= 0 or count <= 0:
            return False
        rows = self._trim(group_id, cur)
        if len(rows) + int(count) > self.per_minute:
            return False
        rows.extend([cur] * int(count))
        self._sent[str(group_id)] = rows[-self.per_minute:]
        return True


class AstrBotSender:
    """统一出口发送器：限流 + 智能引用 + 表情/文件目录牢笼 + QQ 消息贴表情。"""

    def __init__(
        self,
        context: Any,
        comp_types: dict[str, Any],
        chain_cls: Any,
        emoji_root: Path | str = "",
        outbox_root: Path | str = "",
        limiter: Optional[MessageLimiter] = None,
        bot_provider: Optional[Callable[..., Any]] = None,
        repo: Any = None,
        logger: Any = None,
        on_invalid_face: Optional[Callable[[str, str], Any]] = None,
    ):
        self._ctx = context
        self._comp = comp_types
        self._Chain = chain_cls
        self._emoji_root = Path(emoji_root) if emoji_root else None
        self._outbox_root = Path(outbox_root) if outbox_root else None
        self._limiter = limiter or MessageLimiter()
        self._bot_provider = bot_provider
        self._repo = repo
        self._logger = logger
        self._on_invalid_face = on_invalid_face

    @staticmethod
    def _action_succeeded(res: Any) -> bool:
        if isinstance(res, dict):
            if str(res.get("status") or "").strip().lower() == "failed":
                return False
            retcode = res.get("retcode")
            if retcode is not None and retcode != 0:
                return False
        return True

    async def send(self, action: OutboundAction) -> bool:
        if not action.umo:
            return False
        if isinstance(action, SendText):
            return await self._send_text(action)
        if isinstance(action, SendFace):
            return await self._send_face(action)
        if isinstance(action, SendEmoji):
            return await self._send_emoji(action)
        if isinstance(action, SendFile):
            return await self._send_file(action)
        if isinstance(action, ReactEmoji):
            return await self._react(action)
        return False

    async def _send_text(self, action: SendText) -> bool:
        single = clean_display_text(action.text)
        if not single or not self._limiter.try_acquire(action.group_id):
            return False
        chain = []
        if action.reply_to_msg_id and "Reply" in self._comp:
            try:
                chain.append(self._comp["Reply"](id=action.reply_to_msg_id))
            except Exception:
                pass
        chain.append(self._comp["Plain"](single))
        try:
            await self._ctx.send_message(action.umo, self._Chain(chain))
        except Exception:
            if len(chain) > 1:
                await self._ctx.send_message(action.umo, self._Chain([self._comp["Plain"](single)]))
            else:
                raise
        return True

    async def _send_face(self, action: SendFace) -> bool:
        if not action.face_id or not self._limiter.try_acquire(action.group_id):
            return False
        fid = str(action.face_id).strip()
        try:
            face = self._comp["Face"](id=int(fid))
        except (TypeError, ValueError):
            if callable(self._on_invalid_face):
                self._on_invalid_face(action.group_id, fid)
            return False
        try:
            await self._ctx.send_message(action.umo, self._Chain([face]))
        except Exception as exc:
            err_low = str(exc or "").lower()
            is_invalid = any(m in err_low for m in _INVALID_FACE_ERROR_MARKERS)
            if self._logger:
                self._logger.warning("QQ表情发送失败（face_id=%s，invalid=%s）：%s", fid, is_invalid, exc)
            if is_invalid and callable(self._on_invalid_face):
                try:
                    self._on_invalid_face(action.group_id, fid)
                except Exception:
                    pass
            return False
        return True

    def _get_bot(self, group_id: str) -> Any:
        if not callable(self._bot_provider):
            return None
        try:
            return self._bot_provider(group_id)
        except TypeError:
            return self._bot_provider()

    @staticmethod
    def _extract_raw_group_id(group_id: str) -> Optional[int]:
        raw = str(group_id or "").rsplit(":", 1)[-1].strip()
        return int(raw) if raw.isdigit() else None

    async def _send_emoji(self, action: SendEmoji) -> bool:
        if not action.path or not self._emoji_root or not self._limiter.try_acquire(action.group_id):
            return False
        try:
            root = self._emoji_root.resolve(strict=False)
            raw = Path(action.path)
            if raw.is_symlink():
                return False
            target = raw.resolve(strict=True)
        except OSError:
            return False
        if target == root or root not in target.parents or not target.is_file():
            return False
        bot = self._get_bot(action.group_id)
        raw_gid = self._extract_raw_group_id(action.group_id)
        if bot is not None and hasattr(bot, "call_action") and raw_gid is not None:
            try:
                img_bytes = await asyncio.to_thread(target.read_bytes)
                b64 = base64.b64encode(img_bytes).decode("ascii")
                res = await asyncio.wait_for(
                    bot.call_action(
                        "send_group_msg",
                        group_id=raw_gid,
                        message=[{
                            "type": "image",
                            "data": {
                                "file": "base64://" + b64,
                                "sub_type": 1,
                                "summary": action.summary or "[动画表情]",
                            },
                        }],
                    ),
                    timeout=10.0,
                )
                if not self._action_succeeded(res):
                    raise RuntimeError("send_group_msg failed: {}".format(res))
                return True
            except asyncio.TimeoutError as exc:
                if self._logger:
                    self._logger.warning("sub_type=1 表情发送超时（避免重复双发）：%s", exc)
                return False
            except Exception as exc:
                if self._logger:
                    self._logger.warning("sub_type=1 表情发送失败，回退普通图片通道：%s", exc)
        await self._ctx.send_message(action.umo, self._Chain([self._comp["Image"](file=target.as_uri())]))
        return True

    async def _send_file(self, action: SendFile) -> bool:
        target = Path(action.path) if action.path else None
        display_name = action.name or (target.name if target else "")
        out_root = str(self._outbox_root) if self._outbox_root else ""
        try:
            if not target or not self._outbox_root or target.is_symlink() or not target.is_file():
                return False
            resolved = target.resolve(strict=True)
            root_resolved = self._outbox_root.resolve(strict=False)
            if resolved == root_resolved or root_resolved not in resolved.parents:
                return False
            await self._ctx.send_message(
                action.umo,
                self._Chain([self._comp["File"](name=display_name, file=str(resolved))]),
            )
            if self._repo is not None and action.group_id and display_name:
                try:
                    await asyncio.to_thread(
                        self._repo.add_message,
                        action.group_id,
                        "bot",
                        "大肥鱼",
                        "bot",
                        "[已发送群文件：{}]".format(display_name),
                    )
                except Exception as log_exc:
                    if self._logger:
                        self._logger.warning("群文件发送后落库失败（%s）：%s", display_name, log_exc)
            return True
        except Exception as exc:
            if self._logger:
                self._logger.error("文件发送失败（%s）：%s", display_name or action.path, exc)
            try:
                await self._ctx.send_message(
                    action.umo,
                    self._Chain([self._comp["Plain"]("文件「{}」没发出去，稍后再试试。".format(display_name or "附件"))]),
                )
            except Exception:
                pass
            return False
        finally:
            if action.path and out_root:
                cleanup_file_entry(action.path, out_root)

    async def _react(self, action: ReactEmoji) -> bool:
        if not action.msg_id:
            return False
        bot = self._get_bot(action.group_id)
        if not bot or not hasattr(bot, "call_action"):
            return False
        try:
            res = await asyncio.wait_for(
                bot.call_action(
                    "set_msg_emoji_like",
                    message_id=int(action.msg_id) if str(action.msg_id).isdigit() else action.msg_id,
                    emoji_id=str(action.emoji_id or "124"),
                ),
                timeout=5.0,
            )
            return self._action_succeeded(res)
        except Exception:
            return False

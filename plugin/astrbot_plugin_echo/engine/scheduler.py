"""群聊节奏与调度器：防抖合批、@ 高优快车道、极克制偶尔接茬、忙时与错峰主动退避。"""

from __future__ import annotations

import random
import re
import time
from typing import Optional

from ..domain import Batch, ChatEvent, MediaRef

IMAGE_TTL = 120.0
VIEWED_IMAGE_TTL = 60.0
ACTIVE_WINDOW = 120.0
WINDOW_COOLDOWN = 3.0
IDLE_COOLDOWN = 20.0

_IMAGE_REF_HINTS = ("图", "截图", "照片", "画面", "上图", "这张", "那张", "人物")

_FOLLOWUP_CONFIRMS = frozenset({
    "查找吧", "查吧", "来吧", "好呀", "好啊", "行吧", "继续", "快查",
    "查一下", "帮我查", "可以", "算了", "是的", "对呀", "对啊", "嗯嗯", "要的",
})


class Scheduler:
    """统一管理群聊防抖合批、图片上下文附着、@ 优先出队与极克制接茬抽签。"""

    def __init__(
        self,
        ambient_rate: float = 0.08,
        mention_cap: float = 1.2,
        quiet_delay: float = 2.5,
        active_delay: float = 5.5,
        batch_cap: float = 10.0,
        image_wait: float = 4.0,
    ):
        self.ambient_rate = float(ambient_rate)
        self.mention_cap = float(mention_cap)
        self.quiet_delay = float(quiet_delay)
        self.active_delay = float(active_delay)
        self.batch_cap = float(batch_cap)
        self.image_wait = float(image_wait)
        self._pending: dict[str, Batch] = {}
        self._recent_images: dict[str, list[tuple[float, MediaRef]]] = {}
        self._last_viewed_images: dict[str, tuple[float, list[MediaRef]]] = {}
        self._last_input: dict[str, float] = {}
        self._last_reply_at: dict[str, float] = {}
        self._last_reply_user: dict[str, str] = {}
        self._last_offered_at: dict[str, float] = {}
        self._last_face_banter_at: dict[str, float] = {}

    def record_bot_reply(
        self,
        group_id: str,
        user_id: str = "",
        offered: bool = False,
        now: Optional[float] = None,
    ) -> None:
        cur = time.time() if now is None else float(now)
        gid = str(group_id)
        self._last_reply_at[gid] = cur
        self._last_reply_user[gid] = str(user_id or "")
        if offered:
            self._last_offered_at[gid] = cur
        else:
            self._last_offered_at.pop(gid, None)

    def last_reply_at(self, group_id: str) -> float:
        return self._last_reply_at.get(str(group_id), 0.0)

    def attach_images(self, group_id: str, media_list: list[MediaRef], now: Optional[float] = None) -> None:
        if not media_list:
            return
        cur = time.time() if now is None else float(now)
        gid = str(group_id)
        buf = [pair for pair in self._recent_images.get(gid, []) if cur - pair[0] <= IMAGE_TTL]
        seen = {m.sha256 for _, m in buf if m.sha256}
        for m in media_list:
            if m.sha256 and m.sha256 in seen:
                continue
            buf.append((cur, m))
            if m.sha256:
                seen.add(m.sha256)
        self._recent_images[gid] = buf[-4:]

    def pop_recent_images(self, group_id: str, now: Optional[float] = None) -> list[MediaRef]:
        cur = time.time() if now is None else float(now)
        items = self._recent_images.pop(str(group_id), [])
        return [m for stamp, m in items if cur - stamp <= IMAGE_TTL]

    def enqueue(self, event: ChatEvent) -> None:
        gid = str(event.group_id)
        cur = float(event.ts or time.time())
        prev_input = self._last_input.get(gid, 0.0)
        self._last_input[gid] = max(prev_input, cur)
        if event.faces or event.has_sticker:
            self._last_face_banter_at[gid] = cur

        batch = self._pending.get(gid)
        if batch is None:
            batch = Batch(group_id=gid, started=cur, deadline=cur, scene=event.scene)
            self._pending[gid] = batch

        same_user = bool(batch.items and batch.items[-1].user_id == event.user_id)
        batch.items.append(event)
        if event.images:
            existing_digests = {m.sha256 for m in batch.images if m.sha256}
            for m in event.images:
                if m.sha256 and m.sha256 in existing_digests:
                    continue
                batch.images.append(m)
            batch.images = batch.images[-4:]

        if batch.mentioned:
            batch.deadline = min(batch.started + self.mention_cap, cur + self.mention_cap)
            return

        if event.bare_face and not event.has_image:
            # 纯 QQ 表情不延长防抖等待
            return

        base_wait = self.quiet_delay if (not prev_input or cur - prev_input > 30.0) else self.active_delay
        ext = 2.0 if same_user else 1.0
        target = min(batch.started + self.batch_cap, cur + min(base_wait, ext))
        if event.has_image and not event.text.replace("[图片]", "").strip():
            target = max(target, batch.started + self.image_wait)
        batch.deadline = max(batch.deadline, target)

    def due(self, now: Optional[float] = None) -> list[Batch]:
        """提取已到防抖截止点的批；明确 @ 永远排在普通批之前。"""
        cur = time.time() if now is None else float(now)
        ready = []
        for gid, batch in list(self._pending.items()):
            if cur >= batch.deadline:
                ready.append(self._pending.pop(gid))
        ready.sort(key=lambda b: (0 if b.mentioned else 1, b.started))
        return ready

    def merge_recent_images(self, batch: Batch, now: Optional[float] = None) -> None:
        cur = time.time() if now is None else float(now)
        gid = str(batch.group_id)
        fresh = self.pop_recent_images(gid, now=cur)
        if fresh:
            seen = {m.sha256 for m in batch.images if m.sha256}
            for m in fresh:
                if m.sha256 and m.sha256 in seen:
                    continue
                batch.images.append(m)
                if m.sha256:
                    seen.add(m.sha256)
            batch.images = batch.images[-4:]

        if batch.images:
            self._last_viewed_images[gid] = (cur, list(batch.images[-2:]))
            return

        prev = self._last_viewed_images.get(gid)
        if not prev:
            return
        viewed_at, viewed_imgs = prev
        if cur - viewed_at > VIEWED_IMAGE_TTL:
            self._last_viewed_images.pop(gid, None)
            return
        body = (batch.body_text or "").strip()
        if body and any(h in body for h in _IMAGE_REF_HINTS):
            batch.images = list(viewed_imgs)

    def chime_probability(self, batch: Batch, now: Optional[float] = None) -> float:
        """计算未 @ 群聊的主动接茬概率：政策 3A「极克制地偶尔接茬」。"""
        if batch.mentioned:
            return 1.0
        if batch.at_others:
            return 0.0
        cur = time.time() if now is None else float(now)
        gid = batch.group_id
        anchor = batch.anchor
        body = (batch.body_text or "").strip()
        clean = re.sub(r"[\s，,。.！!？?～~、…]+", "", body)
        user_id = anchor.user_id if anchor else ""

        last_reply = self._last_reply_at.get(gid, 0.0)
        last_offer = self._last_offered_at.get(gid, 0.0)
        last_face = self._last_face_banter_at.get(gid, 0.0)
        since_reply = (cur - last_reply) if last_reply else 1e12
        since_offer = (cur - last_offer) if last_offer else 1e12
        since_face = (cur - last_face) if last_face else 1e12
        is_face = bool(anchor and anchor.bare_face and "[QQ表情:" in body)

        if since_offer < 300.0 and (
            clean in _FOLLOWUP_CONFIRMS
            or any(k in clean for k in ("查找", "查吧", "查一下", "帮我", "来吧", "继续"))
            or len(clean) > 4
            or is_face
        ):
            return 0.85

        if user_id and user_id == self._last_reply_user.get(gid) and since_reply < ACTIVE_WINDOW:
            if len(clean) > 4 or "你" in clean or is_face:
                return 0.65

        if since_reply < 90.0 and (is_face or since_face < 90.0):
            return 0.25

        # 群友之间普通聊天（哪怕带问号）：极克制抽签，不打断别人也不堵上游
        return self.ambient_rate

    def should_view(
        self,
        batch: Batch,
        now: Optional[float] = None,
        *,
        group_busy: bool = False,
        recent_errors: int = 0,
    ) -> bool:
        """判定这一批消息是否需要送入 LLM 决策。"""
        if batch.approved or batch.mentioned:
            return True
        if group_busy or recent_errors > 0:
            # 群锁正忙或近期上游报错/超时时，未 @ 闲聊直接跳过只记历史，绝不挤占 @ 的通道。
            return False
        if batch.at_others:
            return False
        cur = time.time() if now is None else float(now)
        gid = batch.group_id
        last_reply = self._last_reply_at.get(gid, 0.0)
        last_offer = self._last_offered_at.get(gid, 0.0)
        since_reply = (cur - last_reply) if last_reply else 1e12
        since_offer = (cur - last_offer) if last_offer else 1e12
        cooldown = WINDOW_COOLDOWN if since_reply < ACTIVE_WINDOW else IDLE_COOLDOWN
        if since_reply < cooldown and since_offer >= 300.0:
            return False
        prob = self.chime_probability(batch, cur)
        if prob <= 0.0:
            return False
        return random.random() < prob

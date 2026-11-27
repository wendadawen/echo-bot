"""群聊节奏与注意力调度器：防抖合批、非破坏性媒体窗口、多人群聊轮次模型。"""

from __future__ import annotations

import collections
import random
import re
import time
from dataclasses import dataclass
from typing import Optional, Sequence

from ..domain import Batch, ChatEvent, MediaRef

IMAGE_TTL = 180.0
VIEWED_IMAGE_TTL = 150.0
ACTIVE_WINDOW = 60.0
OFFER_WINDOW = 180.0
WINDOW_COOLDOWN = 15.0
IDLE_COOLDOWN = 25.0

_IMAGE_REF_HINTS = (
    "图", "截图", "照片", "画面", "上图", "这张", "那张", "人物",
    "识图", "看图", "刚才那", "上面那",
)

_FOLLOWUP_CONFIRMS = frozenset({
    "查找吧", "查吧", "来吧", "好呀", "好啊", "行吧", "继续", "快查",
    "查一下", "帮我查", "可以", "算了", "是的", "对呀", "对啊", "嗯嗯", "要的",
})


@dataclass
class _MediaSlot:
    media: MediaRef
    ts: float
    sender_id: str = ""
    viewed_at: float = 0.0


class Scheduler:
    """统一管理群聊防抖合批、非破坏性媒体窗口、@ 快车道与多人群聊轮次注意力。"""

    def __init__(
        self,
        ambient_rate: float = 0.05,
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
        self._media_window: dict[str, list[_MediaSlot]] = {}
        self._last_input: dict[str, float] = {}
        self._last_reply_at: dict[str, float] = {}
        self._last_reply_user: dict[str, str] = {}
        self._last_offered_at: dict[str, float] = {}
        self._last_face_banter_at: dict[str, float] = {}
        self._intervened_since_reply: dict[str, bool] = {}
        self._unmentioned_streak: dict[str, int] = {}
        self._recent_humans: dict[str, collections.deque[tuple[float, str]]] = {}

    def observe_human(
        self,
        group_id: str,
        user_id: str = "",
        now: Optional[float] = None,
    ) -> None:
        """记录群友说话轨迹（含不入队的纯图/跳过消息），用于判断是否已被他人打岔。"""
        cur = time.time() if now is None else float(now)
        gid = str(group_id)
        uid = str(user_id or "")
        if not uid:
            return
        hist = self._recent_humans.setdefault(gid, collections.deque(maxlen=8))
        hist.append((cur, uid))
        last_uid = self._last_reply_user.get(gid, "")
        if last_uid and uid != last_uid:
            self._intervened_since_reply[gid] = True

    def record_bot_reply(
        self,
        group_id: str,
        user_id: str = "",
        offered: bool = False,
        from_mentioned: bool = True,
        now: Optional[float] = None,
    ) -> None:
        cur = time.time() if now is None else float(now)
        gid = str(group_id)
        self._last_reply_at[gid] = cur
        self._last_reply_user[gid] = str(user_id or "")
        self._intervened_since_reply[gid] = False
        if from_mentioned:
            self._unmentioned_streak[gid] = 0
        else:
            self._unmentioned_streak[gid] = self._unmentioned_streak.get(gid, 0) + 1
        if offered:
            self._last_offered_at[gid] = cur
        else:
            self._last_offered_at.pop(gid, None)

    def last_reply_at(self, group_id: str) -> float:
        return self._last_reply_at.get(str(group_id), 0.0)

    def attach_images(
        self,
        group_id: str,
        media_list: Sequence[MediaRef],
        now: Optional[float] = None,
        sender_id: str = "",
    ) -> None:
        """把近期群图存入非破坏性短时窗口；重复发同一张图时重置其未读状态。"""
        if not media_list:
            return
        cur = time.time() if now is None else float(now)
        gid = str(group_id)
        buf = [s for s in self._media_window.get(gid, []) if cur - s.ts <= IMAGE_TTL]
        by_sha = {s.media.sha256: idx for idx, s in enumerate(buf) if s.media.sha256}
        for m in media_list:
            slot = _MediaSlot(media=m, ts=cur, sender_id=str(sender_id or ""), viewed_at=0.0)
            if m.sha256 and m.sha256 in by_sha:
                buf[by_sha[m.sha256]] = slot
            else:
                if m.sha256:
                    by_sha[m.sha256] = len(buf)
                buf.append(slot)
        self._media_window[gid] = buf[-4:]

    def merge_recent_images(self, batch: Batch, now: Optional[float] = None) -> None:
        """非破坏性地把近期未看/同话题群图附着到当前批；绝不在投喂前弹出或标记已看。"""
        cur = time.time() if now is None else float(now)
        gid = str(batch.group_id)
        buf = [s for s in self._media_window.get(gid, []) if cur - s.ts <= IMAGE_TTL]
        self._media_window[gid] = buf

        if batch.images:
            known = {s.media.sha256 for s in buf if s.media.sha256}
            anchor_uid = batch.anchor.user_id if batch.anchor else ""
            missing = [m for m in batch.images if not (m.sha256 and m.sha256 in known)]
            if missing:
                self.attach_images(gid, missing, now=cur, sender_id=anchor_uid)
            return

        if not buf:
            return

        unread = [s for s in buf if s.viewed_at == 0.0]
        if unread:
            batch.images = [s.media for s in unread[-2:]]
            return

        viewed = [s for s in buf if s.viewed_at > 0.0 and cur - s.viewed_at <= VIEWED_IMAGE_TTL]
        if not viewed:
            return

        body = (batch.body_text or "").strip()
        if body and any(h in body for h in _IMAGE_REF_HINTS):
            batch.images = [s.media for s in viewed[-2:]]

    def mark_images_viewed(
        self,
        group_id: str,
        media_list: Sequence[MediaRef],
        now: Optional[float] = None,
    ) -> None:
        """仅在主引擎真正完成 LLM 调用后才标记图片已看；跳过批与报错批绝不消费图片。"""
        if not media_list:
            return
        cur = time.time() if now is None else float(now)
        gid = str(group_id)
        buf = self._media_window.get(gid)
        if not buf:
            return
        digests = {m.sha256 for m in media_list if m.sha256}
        refs = set(id(m) for m in media_list)
        for slot in buf:
            if (slot.media.sha256 and slot.media.sha256 in digests) or id(slot.media) in refs:
                if slot.viewed_at == 0.0:
                    slot.viewed_at = cur

    def enqueue(self, event: ChatEvent) -> None:
        gid = str(event.group_id)
        cur = float(event.ts or time.time())
        prev_input = self._last_input.get(gid, 0.0)
        self._last_input[gid] = max(prev_input, cur)
        self.observe_human(gid, event.user_id, now=cur)
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

    def chime_probability(self, batch: Batch, now: Optional[float] = None) -> float:
        """计算未 @ 群聊的主动接茬概率：尊重群友多轮接力、禁止未 @ 连环插话。"""
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

        if self._unmentioned_streak.get(gid, 0) >= 1 and since_reply < 90.0:
            return 0.0

        intervened = self._intervened_since_reply.get(gid, False)

        if since_offer < OFFER_WINDOW and (
            clean in _FOLLOWUP_CONFIRMS
            or any(k in clean for k in ("查找", "查吧", "查一下", "帮我", "来吧", "继续"))
            or (not intervened and (len(clean) > 4 or is_face))
        ):
            return 0.75

        if (
            user_id
            and user_id == self._last_reply_user.get(gid)
            and not intervened
            and since_reply < ACTIVE_WINDOW
            and (len(clean) > 4 or "你" in clean or is_face)
        ):
            return 0.45

        if since_reply < 60.0 and not intervened and (is_face or since_face < 45.0):
            return 0.2

        recent = [pair for pair in self._recent_humans.get(gid, ()) if cur - pair[0] <= 45.0]
        if len(recent) >= 3 and len({uid for _, uid in recent}) >= 2:
            return min(self.ambient_rate, 0.02)

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
        if since_reply < cooldown and since_offer >= OFFER_WINDOW:
            return False
        prob = self.chime_probability(batch, cur)
        if prob <= 0.0:
            return False
        return random.random() < prob

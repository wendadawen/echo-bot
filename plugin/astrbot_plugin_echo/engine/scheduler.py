"""群聊节奏与注意力调度器：防抖合批、非破坏性媒体窗口、多人群聊轮次模型。"""

from __future__ import annotations

import collections
import random
import re
import threading
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
POKE_COOLDOWN = 35.0

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
        self._last_poke_at: dict[str, float] = {}
        self._last_prune_at: float = 0.0
        self._lock = threading.RLock()

    def allow_poke(self, group_id: str, now: Optional[float] = None) -> bool:
        """同一群短时间内连戳冷却，防止戳一戳刷屏。"""
        cur = time.time() if now is None else float(now)
        gid = str(group_id)
        with self._lock:
            last = self._last_poke_at.get(gid, 0.0)
            if cur - last < POKE_COOLDOWN:
                return False
            self._last_poke_at[gid] = cur
            return True

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
        with self._lock:
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
        with self._lock:
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
        with self._lock:
            return self._last_reply_at.get(str(group_id), 0.0)

    def attach_images(
        self,
        group_id: str,
        media_list: Sequence[MediaRef],
        now: Optional[float] = None,
        sender_id: str = "",
    ) -> None:
        """把近期群图存入非破坏性短时窗口；重复发同一张图时移至队尾并重置其未读状态。"""
        if not media_list:
            return
        cur = time.time() if now is None else float(now)
        gid = str(group_id)
        with self._lock:
            buf = [s for s in self._media_window.get(gid, []) if cur - s.ts <= IMAGE_TTL]
            for m in media_list:
                slot = _MediaSlot(media=m, ts=cur, sender_id=str(sender_id or ""), viewed_at=0.0)
                if m.sha256:
                    buf = [s for s in buf if s.media.sha256 != m.sha256]
                buf.append(slot)
            self._media_window[gid] = buf[-4:]

    def prune_stale_media(self, now: Optional[float] = None) -> int:
        """定期清理所有不活跃群里已过期的原始图片 bytes，防止永久内存泄漏。"""
        cur = time.time() if now is None else float(now)
        with self._lock:
            return self._prune_stale_media_locked(cur)

    def _prune_stale_media_locked(self, cur: float, skip_gids: Optional[set[str]] = None) -> int:
        removed = 0
        for gid in list(self._media_window.keys()):
            if skip_gids and gid in skip_gids:
                continue
            old_buf = self._media_window[gid]
            kept = [
                s
                for s in old_buf
                if (cur - s.ts <= IMAGE_TTL)
                or (s.viewed_at > 0.0 and cur - s.viewed_at <= VIEWED_IMAGE_TTL)
            ]
            removed += len(old_buf) - len(kept)
            if kept:
                self._media_window[gid] = kept
            else:
                self._media_window.pop(gid, None)
        return removed

    def merge_recent_images(self, batch: Batch, now: Optional[float] = None) -> None:
        """非破坏性地把近期未看/同话题群图附着到当前批；绝不在投喂前弹出或标记已看。"""
        cur = time.time() if now is None else float(now)
        gid = str(batch.group_id)
        with self._lock:
            buf = [s for s in self._media_window.get(gid, []) if cur - s.ts <= IMAGE_TTL]
            if buf:
                self._media_window[gid] = buf
            else:
                self._media_window.pop(gid, None)

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
            body = (batch.body_text or "").strip()
            has_ref_hint = bool(body and any(h in body for h in _IMAGE_REF_HINTS))
            if unread:
                batch_uids = {str(i.user_id or "") for i in batch.items if i.user_id}
                clean_body = re.sub(r"^(?:@\S+\s*)+", "", body).strip()
                matched_unread = [
                    s
                    for s in unread
                    if (not s.sender_id)
                    or (s.sender_id in batch_uids)
                    or has_ref_hint
                    or (
                        batch.mentioned
                        and cur - s.ts <= 30.0
                        and (len(clean_body) <= 20 or any(k in clean_body for k in ("看", "这", "那", "啥", "谁", "什么")))
                    )
                ]
                if matched_unread:
                    batch.images = [s.media for s in matched_unread[-2:]]
                    return

            viewed = [s for s in buf if s.viewed_at > 0.0 and cur - s.viewed_at <= VIEWED_IMAGE_TTL]
            if viewed and has_ref_hint:
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
        digests = {m.sha256 for m in media_list if m.sha256}
        refs = set(id(m) for m in media_list)
        with self._lock:
            buf = self._media_window.get(gid)
            if not buf:
                return
            for slot in buf:
                if (slot.media.sha256 and slot.media.sha256 in digests) or id(slot.media) in refs:
                    if slot.viewed_at == 0.0:
                        slot.viewed_at = cur

    def enqueue(self, event: ChatEvent) -> None:
        gid = str(event.group_id)
        cur = float(event.ts or time.time())
        with self._lock:
            prev_input = self._last_input.get(gid, 0.0)
            self._last_input[gid] = max(prev_input, cur)
            self.observe_human(gid, event.user_id, now=cur)
            if event.faces or event.has_sticker:
                self._last_face_banter_at[gid] = cur

            batch = self._pending.get(gid)
            is_first = batch is None
            if batch is None:
                batch = Batch(group_id=gid, started=cur, deadline=cur)
                self._pending[gid] = batch

            same_user = bool(not is_first and batch.items and batch.items[-1].user_id == event.user_id)
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
                face_wait = min(batch.started + self.batch_cap, cur + 2.0)
                batch.deadline = max(batch.deadline, face_wait)
                return

            base_wait = self.quiet_delay if (not prev_input or cur - prev_input > 30.0) else self.active_delay
            wait_sec = base_wait if is_first else (2.0 if same_user else 1.0)
            target = min(batch.started + self.batch_cap, cur + wait_sec)
            if event.has_image and not event.text.replace("[图片]", "").strip():
                target = max(target, batch.started + self.image_wait)
            batch.deadline = max(batch.deadline, target)

    def due(self, now: Optional[float] = None) -> list[Batch]:
        """提取已到防抖截止点的批；明确 @ 永远排在普通批之前。"""
        cur = time.time() if now is None else float(now)
        with self._lock:
            ready = []
            for gid, batch in list(self._pending.items()):
                if cur >= batch.deadline:
                    ready.append(self._pending.pop(gid))
            ready.sort(key=lambda b: (0 if b.mentioned else 1, b.started))
            if cur - self._last_prune_at >= 60.0:
                self._last_prune_at = cur
                active_gids = set(self._pending.keys()) | {b.group_id for b in ready}
                self._prune_stale_media_locked(cur, skip_gids=active_gids)
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

        with self._lock:
            last_reply = self._last_reply_at.get(gid, 0.0)
            last_offer = self._last_offered_at.get(gid, 0.0)
            last_face = self._last_face_banter_at.get(gid, 0.0)
            last_reply_user = self._last_reply_user.get(gid, "")
            unmentioned_streak = self._unmentioned_streak.get(gid, 0)
            intervened = self._intervened_since_reply.get(gid, False)
            recent = [pair for pair in self._recent_humans.get(gid, ()) if cur - pair[0] <= 45.0]

        since_reply = (cur - last_reply) if last_reply else 1e12
        since_offer = (cur - last_offer) if last_offer else 1e12
        since_face = (cur - last_face) if last_face else 1e12
        is_face = bool(anchor and anchor.bare_face and "[QQ表情:" in body)

        if unmentioned_streak >= 1 and since_reply < 90.0:
            return 0.0

        if since_offer < OFFER_WINDOW and (
            any(k in clean for k in ("查找", "查吧", "查一下", "帮我", "来吧", "继续"))
            or (
                not intervened
                and (
                    (user_id and user_id == last_reply_user)
                    or clean in _FOLLOWUP_CONFIRMS
                    or len(clean) > 4
                    or is_face
                )
            )
        ):
            return 0.75

        if (
            user_id
            and user_id == last_reply_user
            and not intervened
            and since_reply < ACTIVE_WINDOW
            and (len(clean) > 4 or "你" in clean or is_face)
        ):
            return 0.45

        if since_reply < 60.0 and not intervened and (is_face or since_face < 45.0):
            return 0.2

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
        anchor = batch.anchor
        user_id = anchor.user_id if anchor else ""
        with self._lock:
            last_reply = self._last_reply_at.get(gid, 0.0)
            last_offer = self._last_offered_at.get(gid, 0.0)
            last_reply_user = self._last_reply_user.get(gid, "")
            intervened = self._intervened_since_reply.get(gid, False)
        since_reply = (cur - last_reply) if last_reply else 1e12
        since_offer = (cur - last_offer) if last_offer else 1e12
        same_turn_partner = bool(user_id and user_id == last_reply_user and not intervened)
        cooldown = WINDOW_COOLDOWN if same_turn_partner else IDLE_COOLDOWN
        if since_reply < cooldown and since_offer >= OFFER_WINDOW:
            return False
        prob = self.chime_probability(batch, cur)
        if prob <= 0.0:
            return False
        return random.random() < prob

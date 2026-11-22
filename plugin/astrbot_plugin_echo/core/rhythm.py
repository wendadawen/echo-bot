"""输入层（完整版）：防抖合并、查看概率、轮数额度与欠债、静默查看、wait 调度。

对齐 MaiBot `src/chat/debounce` 与 `src/maisaka/turn_trigger` 的规则：
- talk_value 是「一批消息进入决策层的基础概率」，不是回复概率；
- 三档防抖、安静/活跃开批、同人/换人续发、整批上限、@ 上限；
- 裸图开批至少等 4 秒，纯表情不能延长等待，通知与命令不参与防抖；
- 未通过查看的消息留在缓存，与下一批合并，不丢消息；
- Planner 轮数有额度，超额续轮欠债，之后用新消息批次抵债；
- 静默查看按消息平均间隔预测下一条到达时间，超时抽签看一眼；
- wait（可带秒数）结束连续执行，到期按当时概率抽签唤醒，提前唤醒取消倒计时。
只用标准库。
"""

import math
import random
import time
import uuid
from dataclasses import dataclass, field

DEBOUNCE_PRESETS = {
    "fast": dict(quiet=1.0, active=3.0, same=1.0, other=0.5,
                 cap=8.0, mention_cap=1.0, image_min=4.0),
    "natural": dict(quiet=2.5, active=6.0, same=2.5, other=1.0,
                    cap=10.0, mention_cap=2.0, image_min=4.0),
    "steady": dict(quiet=5.0, active=9.0, same=5.0, other=3.0,
                   cap=12.0, mention_cap=3.5, image_min=4.0),
}

PRIVATE_PRESETS = {
    "fast": dict(quiet=1.0, active=3.0, same=1.0, other=0.5, cap=8.0, mention_cap=0.0),
    "natural": dict(quiet=2.0, active=5.0, same=2.0, other=0.5, cap=8.0, mention_cap=0.0),
    "steady": dict(quiet=4.0, active=7.0, same=4.0, other=2.0, cap=8.0, mention_cap=0.0),
}

QUIET_GAP = 30.0
IMAGE_TTL = 60.0
SILENT_MIN_INTERVAL = 20.0
SILENT_MAX_INTERVAL = 300.0
SILENT_COOLDOWN = 1800.0


@dataclass
class Item:
    text: str
    user_id: str
    user_name: str
    mentioned: bool
    umo: str
    ts: float
    kind: str = "text"  # text / image / face / notice
    group_id: str = ""
    quote: str = ""


@dataclass
class Batch:
    group_id: str
    items: list = field(default_factory=list)
    started: float = 0.0
    deadline: float = 0.0
    scene: str = "group"
    images: list = field(default_factory=list)
    approved: bool = False
    # 同一批消息被 requeue 时沿用同一个 turn_id，主循环据此避免重复处理。
    turn_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])

    @property
    def mentioned(self):
        return any(i.mentioned for i in self.items)

    @property
    def umo(self):
        return self.items[-1].umo if self.items else ""

    @property
    def text(self):
        return "\n".join(
            "{}：{}".format(i.user_name, i.text)
            for i in self.items if str(i.text or "").strip()
        )

    @property
    def last_user(self):
        return self.items[-1].user_id if self.items else ""

    @property
    def users(self):
        return list(dict.fromkeys(i.user_name for i in self.items))

    @property
    def has_image(self):
        return any(i.kind == "image" for i in self.items)

    @property
    def quote(self):
        for item in reversed(self.items):
            if getattr(item, "quote", ""):
                return item.quote
        return ""


class Rhythm:
    def __init__(self, view_rate=0.2, preset="natural", private_preset="natural",
                 quiet_gap=QUIET_GAP):
        self.view_rate = view_rate
        self.group_preset = DEBOUNCE_PRESETS.get(preset, DEBOUNCE_PRESETS["natural"])
        self.private_preset = PRIVATE_PRESETS.get(private_preset, PRIVATE_PRESETS["natural"])
        self.quiet_gap = quiet_gap
        self._batches = {}
        self._carry = {}
        self._last_seen = {}
        self._last_input = {}
        self._debt = {}
        self._rounds = {}
        self._last_view = {}
        self._muted = set()
        self._wait_until = {}
        self._intervals = {}
        self._silent_used = {}
        self._earliest_mention = {}
        self._images = {}
        self._last_batch = {}

    # 兼容旧接口的预设别名
    @property
    def quiet(self):
        return self.group_preset["quiet"]

    @property
    def active(self):
        return self.group_preset["active"]

    @property
    def same(self):
        return self.group_preset["same"]

    @property
    def other(self):
        return self.group_preset["other"]

    @property
    def cap(self):
        return self.group_preset["cap"]

    @property
    def mention_cap(self):
        return self.group_preset["mention_cap"]

    def mute(self, group_id, muted=True):
        if muted:
            self._muted.add(group_id)
        else:
            self._muted.discard(group_id)

    def is_muted(self, group_id):
        return group_id in self._muted

    def _preset(self, scene):
        return self.private_preset if scene == "private" else self.group_preset

    def add(self, group_id, item, now=None, scene="group"):
        now = time.time() if now is None else now
        preset = self._preset(scene)
        # 通知、命令、机器人自己的消息不更新防抖规则。
        if item.kind == "notice":
            return

        last_input = self._last_input.get(group_id)
        if last_input is not None:
            gap = now - last_input
            if SILENT_MIN_INTERVAL < gap < SILENT_MAX_INTERVAL:
                slots = self._intervals.setdefault(group_id, [])
                slots.append(gap)
                del slots[:-8]
        self._last_input[group_id] = now

        last_ts, last_user = self._last_seen.get(group_id, (0.0, ""))
        batch = self._batches.get(group_id)
        if batch is None:
            wait = preset["quiet"] if now - last_ts > self.quiet_gap else preset["active"]
            if item.kind == "image":
                wait = max(wait, preset.get("image_min", 4.0))
            batch = Batch(group_id, started=now, deadline=now + wait, scene=scene)
            carried = self._carry.pop(group_id, [])
            if carried:
                batch.items.extend(carried)
                batch.started = carried[0].ts
            self._batches[group_id] = batch
            self._earliest_mention.pop(group_id, None)
        else:
            if item.kind == "face":
                # 纯表情不能延长等待：只与旧截止时间取较早者。
                batch.deadline = min(batch.deadline, now + preset["other"])
            else:
                wait = preset["same"] if item.user_id == last_user else preset["other"]
                batch.deadline = min(batch.deadline, now + wait)
        batch.items.append(item)
        batch.deadline = min(batch.deadline, batch.started + preset["cap"])
        if item.mentioned:
            mention_deadline = item.ts + preset["mention_cap"]
            earliest = self._earliest_mention.get(group_id)
            self._earliest_mention[group_id] = (
                mention_deadline if earliest is None else min(earliest, mention_deadline)
            )
            batch.deadline = min(batch.deadline, self._earliest_mention[group_id])
        self._last_seen[group_id] = (now, item.user_id)

    def attach_image(self, group_id, data, now=None):
        """把下载到的图片挂到下一批上，供 view_image 动作使用。"""
        if data:
            stamp = time.time() if now is None else now
            rows = self._images.setdefault(group_id, [])
            rows.append((stamp, data))
            del rows[:-2]

    def due(self, now=None):
        now = time.time() if now is None else now
        ready = [b for b in self._batches.values() if now >= b.deadline]
        for batch in ready:
            self._batches.pop(batch.group_id, None)
            raw_images = self._images.pop(batch.group_id, [])
            batch.images = [
                data for stamp, data in raw_images if now - stamp <= IMAGE_TTL
            ]
            self._last_batch[batch.group_id] = batch
        return ready

    def has_batch(self, group_id):
        return bool(self._batches.get(group_id))

    def groups(self):
        return set(self._last_view) | set(self._batches) | set(self._carry) | set(self._last_batch)

    def last_batch(self, group_id):
        return self._last_batch.get(group_id)

    def requeue(self, batch, now=None, delay=0.0):
        """静默查看 / wait 到期时复用上一批上下文重新进入决策层。

        已经通过查看抽签，所以 approved=True，避免主循环二次抽签把概率减半。
        """
        now = time.time() if now is None else now
        clone = Batch(
            group_id=batch.group_id,
            items=list(batch.items),
            started=now,
            deadline=now + delay,
            scene=getattr(batch, "scene", "group"),
            images=list(getattr(batch, "images", [])),
            approved=True,
            turn_id=getattr(batch, "turn_id", ""),
        )
        self._batches[batch.group_id] = clone
        return clone

    def carry(self, group_id, batch):
        """查看未通过时把消息留到下一批。"""
        self._carry.setdefault(group_id, []).extend(batch.items)
        self._carry[group_id] = self._carry[group_id][-20:]

    def should_view(self, group_id, now=None, forced=False, rate=None):
        now = time.time() if now is None else now
        if forced:
            self.mark_view(group_id, now)
            return True
        if self._debt.get(group_id, 0) > 0:
            self._debt[group_id] -= 1
            return False
        threshold = self.view_rate if rate is None else max(0.0, min(1.0, float(rate)))
        hit = random.random() < threshold
        if hit:
            self.mark_view(group_id, now)
        return hit

    def quota(self):
        rate = min(self.view_rate, 1.0)
        if rate < 0.5:
            return min(8, math.ceil(16 * rate))
        return math.ceil(16 * rate)

    def start_turn(self, group_id):
        self._rounds[group_id] = 0
        return self.quota()

    def use_round(self, group_id, quota):
        used = self._rounds.get(group_id, 0) + 1
        self._rounds[group_id] = used
        if used > quota:
            self.borrow(group_id, 1)
            return False
        return True

    def rounds(self, group_id):
        return self._rounds.get(group_id, 0)

    def borrow(self, group_id, extra):
        self._debt[group_id] = self._debt.get(group_id, 0) + extra

    def debt(self, group_id):
        return self._debt.get(group_id, 0)

    def mark_view(self, group_id, now=None):
        self._last_view[group_id] = time.time() if now is None else now

    def predict_interval(self, group_id):
        slots = self._intervals.get(group_id) or []
        if not slots:
            return SILENT_MAX_INTERVAL
        return min(SILENT_MAX_INTERVAL, max(SILENT_MIN_INTERVAL, sum(slots) / len(slots) * 2))

    def silent_due(self, group_id, now=None, interval=None):
        now = time.time() if now is None else now
        if self._debt.get(group_id, 0) > 0 or self._batches.get(group_id):
            return False
        if self._wait_until.get(group_id):
            return False
        predicted = interval or self.predict_interval(group_id)
        if now - self._last_view.get(group_id, 0.0) < predicted:
            return False
        # 静默查看要真正冷却，不能每次循环都触发。
        return now - self._silent_used.get(group_id, 0.0) >= SILENT_COOLDOWN

    def consume_silent(self, group_id, now=None):
        now = time.time() if now is None else now
        self._silent_used[group_id] = now
        return self.should_view(group_id, now)

    # wait 调度
    def wait(self, group_id, seconds=0, now=None):
        now = time.time() if now is None else now
        self._wait_until[group_id] = now + max(0.0, min(float(seconds or 0), 600.0))

    def wake(self, group_id):
        """新消息提前唤醒，取消原倒计时。"""
        self._wait_until.pop(group_id, None)

    def wait_due(self, group_id, now=None):
        now = time.time() if now is None else now
        deadline = self._wait_until.get(group_id)
        if deadline is None or now < deadline:
            return False
        self._wait_until.pop(group_id, None)
        if self._debt.get(group_id, 0) > 0:
            return False
        return self.should_view(group_id, now)

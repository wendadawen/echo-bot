"""输入层：防抖合并、查看概率、轮数额度与欠债、静默查看。

参考 MaiBot 的 turn_trigger：talk_value 是「一批消息进入决策层的基础概率」，
不是回复概率；说多了会欠债，之后几批跳过查看来抵债。只用标准库。
"""

import math
import random
import time
from dataclasses import dataclass, field


@dataclass
class Item:
    text: str
    user_id: str
    user_name: str
    mentioned: bool
    umo: str
    ts: float


@dataclass
class Batch:
    group_id: str
    items: list = field(default_factory=list)
    started: float = 0.0
    deadline: float = 0.0

    @property
    def mentioned(self):
        return any(i.mentioned for i in self.items)

    @property
    def umo(self):
        return self.items[-1].umo if self.items else ""

    @property
    def text(self):
        return "\n".join("{}：{}".format(i.user_name, i.text) for i in self.items)

    @property
    def last_user(self):
        return self.items[-1].user_id if self.items else ""

    @property
    def users(self):
        return list(dict.fromkeys(i.user_name for i in self.items))


class Rhythm:
    def __init__(self, view_rate=0.2, quiet=2.5, active=6.0, same=2.5,
                 other=1.0, cap=10.0, mention_cap=2.0, quiet_gap=30.0):
        self.view_rate = view_rate
        self.quiet = quiet
        self.active = active
        self.same = same
        self.other = other
        self.cap = cap
        self.mention_cap = mention_cap
        self.quiet_gap = quiet_gap
        self._batches = {}
        self._last_seen = {}
        self._debt = {}
        self._last_view = {}
        self._muted = set()

    def mute(self, group_id, muted=True):
        if muted:
            self._muted.add(group_id)
        else:
            self._muted.discard(group_id)

    def is_muted(self, group_id):
        return group_id in self._muted

    def add(self, group_id, item, now=None):
        now = time.time() if now is None else now
        last_ts, _ = self._last_seen.get(group_id, (0.0, ""))
        batch = self._batches.get(group_id)
        if batch is None:
            wait = self.quiet if now - last_ts > self.quiet_gap else self.active
            batch = Batch(group_id, started=now, deadline=now + wait)
            self._batches[group_id] = batch
        else:
            wait = self.same if item.user_id == batch.last_user else self.other
            batch.deadline = min(batch.deadline, now + wait)
        batch.items.append(item)
        batch.deadline = min(batch.deadline, batch.started + self.cap)
        if item.mentioned:
            batch.deadline = min(batch.deadline, item.ts + self.mention_cap)
        self._last_seen[group_id] = (now, item.user_id)

    def due(self, now=None):
        now = time.time() if now is None else now
        ready = [b for b in self._batches.values() if now >= b.deadline]
        for batch in ready:
            self._batches.pop(batch.group_id, None)
        return ready

    def should_view(self, group_id, now=None, forced=False):
        now = time.time() if now is None else now
        if forced:
            self.mark_view(group_id, now)
            return True
        if self._debt.get(group_id, 0) > 0:
            self._debt[group_id] -= 1
            return False
        hit = random.random() < self.view_rate
        if hit:
            self.mark_view(group_id, now)
        return hit

    def quota(self):
        return math.ceil(16 * min(self.view_rate, 1.0))

    def borrow(self, group_id, extra):
        self._debt[group_id] = self._debt.get(group_id, 0) + extra

    def debt(self, group_id):
        return self._debt.get(group_id, 0)

    def mark_view(self, group_id, now=None):
        self._last_view[group_id] = time.time() if now is None else now

    def silent_due(self, group_id, now=None, interval=90.0):
        now = time.time() if now is None else now
        return now - self._last_view.get(group_id, 0.0) > interval

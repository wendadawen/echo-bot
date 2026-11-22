"""群心跳：安静太久时偶尔主动开一句，别让群死掉。

对齐 MaiBot `src/chat/heart_flow/` 的观察循环，但只保留最克制的一条规则：
一个群安静够久、当天还没主动说过、且不在深夜，才开口一次。
所有阈值都保守，宁可不说。纯进程内状态，不依赖框架。
"""

import time

QUIET_SECONDS = 3 * 3600
MIN_INTERVAL = 8 * 3600
DAILY_CAP = 1
ACTIVE_START = 9
ACTIVE_END = 23


class Heartbeat:
    def __init__(self, quiet=QUIET_SECONDS, min_interval=MIN_INTERVAL,
                 daily_cap=DAILY_CAP, active_hours=(ACTIVE_START, ACTIVE_END)):
        self.quiet = quiet
        self.min_interval = min_interval
        self.daily_cap = daily_cap
        self.active_hours = active_hours
        self._last_message = {}
        self._last_spoke = {}
        self._day = {}
        self._count = {}

    def observe(self, group_id, now=None):
        """有消息就刷新安静起点；主动说话后也刷新，避免刚说完又冷场。"""
        now = time.time() if now is None else now
        self._last_message[str(group_id)] = now

    def _today(self, now):
        return time.strftime("%Y-%m-%d", time.localtime(now))

    def due(self, group_id, now=None):
        now = time.time() if now is None else now
        group_id = str(group_id)
        last = self._last_message.get(group_id)
        if last is None or now - last < self.quiet:
            return False
        if now - self._last_spoke.get(group_id, 0) < self.min_interval:
            return False
        start, end = self.active_hours
        hour = time.localtime(now).tm_hour
        if not (start <= hour < end):
            return False
        day = self._today(now)
        if self._day.get(group_id) == day and self._count.get(group_id, 0) >= self.daily_cap:
            return False
        return True

    def mark(self, group_id, now=None):
        now = time.time() if now is None else now
        group_id = str(group_id)
        self._last_spoke[group_id] = now
        self._last_message[group_id] = now
        day = self._today(now)
        if self._day.get(group_id) != day:
            self._day[group_id] = day
            self._count[group_id] = 0
        self._count[group_id] = self._count.get(group_id, 0) + 1

    def remaining(self, group_id, now=None):
        now = time.time() if now is None else now
        last = self._last_message.get(str(group_id))
        if last is None:
            return self.quiet
        return max(0.0, self.quiet - (now - last))


PROMPT = (
    "群里已经几个小时没人说话了。你随口开一句，让气氛活一点。\n"
    "要求：只一句，最多两句；不要问大家问题，不要总结，不要问候所有人，"
    "不要提「安静」「冷场」；说点具体的小事或刚才聊过的东西。\n"
    "只输出要发到群里的那句话。"
)

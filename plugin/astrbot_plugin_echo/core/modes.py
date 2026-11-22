"""模式策略：普通 / 专注 / 静默，加按时段自动切换。

对齐 MaiBot `src/maisaka/mode_policy.py` 与 `src/maisaka/focus/` 的思路：
模式决定「能不能接话」「只接哪个话题」，不用改人设，也不用重启进程。
只依赖标准库。
"""

import re
import threading
import time

MODES = ("normal", "focus", "quiet")
_ALIASES = {
    "normal": "normal", "正常": "normal", "普通": "normal",
    "focus": "focus", "专注": "focus", "集中": "focus",
    "quiet": "quiet", "静默": "quiet", "闭嘴": "quiet", "安静": "quiet",
}
_TIME_RULE = re.compile(r"^(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})$")


def normalize(name):
    key = str(name or "").strip().lower()
    return _ALIASES.get(key)


def _minutes(hour, minute):
    return (int(hour) % 24) * 60 + int(minute) % 60


def in_window(now, start, end):
    """判断当前时刻是否落在 start-end 之间，支持跨午夜（23:00-08:00）。"""
    current = _minutes(now.tm_hour, now.tm_min)
    begin = _minutes(*start)
    finish = _minutes(*end)
    if begin == finish:
        return True
    if begin < finish:
        return begin <= current < finish
    return current >= begin or current < finish


class ModePolicy:
    """按群维护模式、专注话题与时段规则；全部是进程内状态。"""

    def __init__(self, rules=None):
        # rules: [(群或 "*", "23:00-08:00", "quiet"), ...]
        self._lock = threading.RLock()
        self._state = {}
        self._rules = []
        for group, span, mode in rules or []:
            self.add_rule(group, span, mode)

    def add_rule(self, group_id, span, mode):
        name = normalize(mode)
        match = _TIME_RULE.match(str(span).strip())
        if name is None or not match:
            raise ValueError("规则格式应为 23:00-08:00 quiet")
        hour1, minute1, hour2, minute2 = (int(x) for x in match.groups())
        if hour1 > 23 or hour2 > 23 or minute1 > 59 or minute2 > 59:
            raise ValueError("时间超出范围")
        with self._lock:
            self._rules.append((str(group_id or "*"), (hour1, minute1),
                                (hour2, minute2), name))

    def set(self, group_id, mode, topic="", ttl=None, now=None):
        name = normalize(mode)
        if name is None:
            raise ValueError("未知模式：{}".format(mode))
        now = time.time() if now is None else now
        until = now + ttl if ttl else 0
        with self._lock:
            self._state[str(group_id)] = {
                "mode": name,
                "topic": str(topic or "").strip()[:80],
                "until": until,
                "set_at": now,
                "source": "manual",
            }
        return name

    def reset(self, group_id):
        with self._lock:
            self._state.pop(str(group_id), None)

    def state(self, group_id, now=None):
        now = time.time() if now is None else now
        gid = str(group_id)
        with self._lock:
            row = self._state.get(gid)
            if row and row["until"] and now >= row["until"]:
                self._state.pop(gid, None)
                row = None
            if row:
                return dict(row)
            # 没有手动模式时看时段规则；群专属优先于全局。
            stamp = time.localtime(now)
            rules_ordered = [r for r in self._rules if r[0] == gid] + [
                r for r in self._rules if r[0] == "*"
            ]
        for group, start, end, mode in rules_ordered:
            if in_window(stamp, start, end):
                return {"mode": mode, "topic": "", "until": 0, "set_at": 0,
                        "source": "rule"}
        return {"mode": "normal", "topic": "", "until": 0, "set_at": 0,
                "source": "default"}

    def mode(self, group_id, now=None):
        return self.state(group_id, now)["mode"]

    def topic(self, group_id, now=None):
        return self.state(group_id, now).get("topic", "")

    def allows(self, group_id, *, mentioned=False, now=None):
        """手动静默连 @ 都不回；时段规则里的静默只拦闲话，@ 仍穿透。"""
        state = self.state(group_id, now)
        if state["mode"] == "quiet":
            if state.get("source") == "manual":
                return False
            return bool(mentioned)
        return True

    def focus_hit(self, group_id, text, now=None):
        """专注模式下判断这句话是否还在话题里；普通模式恒为真。"""
        mode = self.mode(group_id, now)
        if mode != "focus":
            return True
        topic = self.topic(group_id, now)
        if not topic:
            return True
        return topic in str(text or "") or _overlap(topic, text)


def _bigrams(text):
    clean = "".join(ch for ch in str(text or "") if ch.strip())
    return {clean[i:i + 2] for i in range(max(0, len(clean) - 1))}


def _overlap(topic, text):
    left = _bigrams(topic)
    right = _bigrams(text)
    if not left or not right:
        return False
    return len(left & right) / float(len(left)) >= 0.34

"""日程：一次性、每天、每 N 小时、每周，带时区与错过补偿。

对齐 MaiBot `src/services/schedules/`（repository / service / store）的能力，
但只保留群提醒需要的四种规则，独立 SQLite，不依赖框架。
"""

import sqlite3
import threading
import time
import uuid
from datetime import datetime, timedelta

try:
    from zoneinfo import ZoneInfo
except ImportError:  # Python < 3.9
    ZoneInfo = None

KINDS = ("once", "daily", "interval", "weekly")
WEEKDAYS = {
    "一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6,
    "mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6,
}
MISSED = ("skip", "fire_once")


def _zone(name):
    if not name or ZoneInfo is None:
        return None
    try:
        return ZoneInfo(str(name))
    except Exception:
        return None


def _now_dt(stamp, tz):
    zone = _zone(tz)
    return datetime.fromtimestamp(stamp, zone) if zone else datetime.fromtimestamp(stamp)


def parse_clock(text):
    """"22:00" / "22点" / "22点30" → (22, 0/30)。"""
    clean = str(text or "").strip().replace("：", ":")
    if ":" in clean:
        hour, _, minute = clean.partition(":")
    elif "点" in clean:
        hour, _, minute = clean.replace("点", ":").partition(":")
    else:
        return None
    try:
        hour, minute = int(hour), int(minute or 0)
    except ValueError:
        return None
    if 0 <= hour <= 23 and 0 <= minute <= 59:
        return hour, minute
    return None


def next_time(kind, spec, base, tz=""):
    """按规则算出 base 之后的下一次触发时间戳。"""
    if kind == "interval":
        minutes = max(1, int(spec or 60))
        return base + minutes * 60
    if kind in ("daily", "weekly"):
        if kind == "daily":
            clock = parse_clock(spec)
            weekday = None
        else:
            head, _, tail = str(spec or "").partition(" ")
            weekday = WEEKDAYS.get(head.strip().lower())
            clock = parse_clock(tail)
        if not clock or (kind == "weekly" and weekday is None):
            return base + 86400
        hour, minute = clock
        current = _now_dt(base, tz)
        target = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if kind == "daily":
            if target <= current:
                target += timedelta(days=1)
        else:
            delta = (weekday - current.weekday()) % 7
            target += timedelta(days=delta)
            if target <= current:
                target += timedelta(days=7)
        return target.timestamp()
    return base


class Schedule:
    """按群隔离的日程表；`fire()` 负责推进下一次并给出是否需要发。"""

    def __init__(self, path=":memory:"):
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS schedules (
                id TEXT PRIMARY KEY,
                group_id TEXT NOT NULL,
                user_id TEXT NOT NULL DEFAULT '',
                user_name TEXT NOT NULL DEFAULT '',
                umo TEXT NOT NULL DEFAULT '',
                text TEXT NOT NULL,
                kind TEXT NOT NULL,
                spec TEXT NOT NULL DEFAULT '',
                tz TEXT NOT NULL DEFAULT '',
                missed TEXT NOT NULL DEFAULT 'fire_once',
                next_at REAL NOT NULL,
                last_fired REAL NOT NULL DEFAULT 0,
                active INTEGER NOT NULL DEFAULT 1,
                created REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS schedules_due
                ON schedules(active, next_at);
            """
        )
        self._db.commit()

    def add(self, group_id, text, kind, spec="", due_at=None, tz="", missed="fire_once",
            user_id="", user_name="", umo="", now=None):
        now = time.time() if now is None else now
        kind = str(kind)
        if kind not in KINDS:
            raise ValueError("不支持的日程类型：{}".format(kind))
        if missed not in MISSED:
            missed = "fire_once"
        if kind == "once":
            next_at = float(due_at or now + 60)
            spec = ""
        else:
            next_at = next_time(kind, spec, now, tz)
        row_id = uuid.uuid4().hex[:12]
        with self._lock:
            self._db.execute(
                "INSERT INTO schedules(id, group_id, user_id, user_name, umo, text, "
                "kind, spec, tz, missed, next_at, created) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (row_id, group_id, user_id, user_name, umo, str(text)[:200], kind,
                 str(spec), tz, missed, next_at, now),
            )
            self._db.commit()
        return row_id

    def due(self, now=None):
        now = time.time() if now is None else now
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM schedules WHERE active = 1 AND next_at <= ? "
                "ORDER BY next_at",
                (now,),
            ).fetchall()
        return [dict(row) for row in rows]

    def fire(self, schedule_id, now=None):
        """推进一条日程；返回是否应该真的发出去（错过的按策略决定）。"""
        now = time.time() if now is None else now
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM schedules WHERE id = ?", (str(schedule_id),)
            ).fetchone()
            if row is None or not row["active"]:
                return None
            late = now - row["next_at"]
            kind = row["kind"]
            if kind == "once":
                self._db.execute(
                    "UPDATE schedules SET active = 0, last_fired = ? WHERE id = ?",
                    (now, row["id"]),
                )
                self._db.commit()
                if late > 6 * 3600 and row["missed"] == "skip":
                    return None
                return dict(row)
            # 错过太久：skip 策略只在长期没开机时避免补发；fire_once 仍补一次。
            send = True
            if late > 6 * 3600 and row["missed"] == "skip":
                send = False
            next_at = row["next_at"]
            guard = 0
            while next_at <= now and guard < 500:
                next_at = next_time(kind, row["spec"], next_at, row["tz"])
                guard += 1
            if next_at <= now:
                next_at = next_time(kind, row["spec"], now, row["tz"])
            self._db.execute(
                "UPDATE schedules SET next_at = ?, last_fired = ? WHERE id = ?",
                (next_at, now, row["id"]),
            )
            self._db.commit()
            payload = dict(row)
        return payload if send else None

    def list(self, group_id=None):
        sql = "SELECT * FROM schedules WHERE active = 1"
        params = []
        if group_id:
            sql += " AND group_id = ?"
            params.append(str(group_id))
        sql += " ORDER BY next_at"
        with self._lock:
            rows = self._db.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def cancel(self, schedule_id, group_id=None):
        sql = "UPDATE schedules SET active = 0 WHERE id = ?"
        params = [str(schedule_id)]
        if group_id:
            sql += " AND group_id = ?"
            params.append(str(group_id))
        with self._lock:
            cur = self._db.execute(sql, params)
            self._db.commit()
            return cur.rowcount > 0

    def clear(self, group_id):
        with self._lock:
            cur = self._db.execute(
                "UPDATE schedules SET active = 0 WHERE group_id = ? AND active = 1",
                (str(group_id),),
            )
            self._db.commit()
            return cur.rowcount

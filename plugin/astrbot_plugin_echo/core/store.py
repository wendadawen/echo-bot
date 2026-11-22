"""持久化：消息历史、定时提醒。只用 Python 标准库。"""

import sqlite3
import threading
import time


class Store:
    def __init__(self, path=":memory:"):
        self._lock = threading.RLock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.executescript(
                """
                CREATE TABLE IF NOT EXISTS messages (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    user_name TEXT NOT NULL,
                    role TEXT NOT NULL,
                    text TEXT NOT NULL,
                    ts REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS reminders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    user_name TEXT NOT NULL,
                    text TEXT NOT NULL,
                    due_at REAL NOT NULL,
                    umo TEXT NOT NULL DEFAULT '',
                    done INTEGER NOT NULL DEFAULT 0,
                    repeat TEXT NOT NULL DEFAULT '',
                    last_fired TEXT NOT NULL DEFAULT ''
                );
                """
            )
            # 兼容早期版本的数据库文件
            columns = {row["name"] for row in self._db.execute("PRAGMA table_info(reminders)")}
            if "repeat" not in columns:
                self._db.execute("ALTER TABLE reminders ADD COLUMN repeat TEXT NOT NULL DEFAULT ''")
            if "last_fired" not in columns:
                self._db.execute("ALTER TABLE reminders ADD COLUMN last_fired TEXT NOT NULL DEFAULT ''")
            self._db.commit()

    def add_message(self, group_id, user_id, user_name, role, text, ts=None):
        ts = time.time() if ts is None else ts
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO messages(group_id, user_id, user_name, role, text, ts) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (group_id, user_id, user_name, role, text, ts),
            )
            self._db.commit()
            return cur.lastrowid

    def recent(self, group_id, n=12):
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM messages WHERE group_id = ? ORDER BY seq DESC LIMIT ?",
                (group_id, n),
            ).fetchall()
        return [dict(r) for r in reversed(rows)]

    def add_reminder(self, group_id, user_id, user_name, text, due_at, umo="", repeat=""):
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO reminders(group_id, user_id, user_name, text, due_at, umo, repeat) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (group_id, user_id, user_name, text, due_at, umo, repeat),
            )
            self._db.commit()
            return cur.lastrowid

    def pending_reminders(self, group_id):
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM reminders WHERE group_id = ? AND done = 0 ORDER BY due_at",
                (group_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def due_reminders(self, now=None):
        now = time.time() if now is None else now
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM reminders WHERE done = 0 "
                "AND ((repeat = '' AND due_at <= ?) OR repeat <> '') ORDER BY due_at",
                (now,),
            ).fetchall()
        return [dict(r) for r in rows]

    def mark_reminder_done(self, rid):
        with self._lock:
            self._db.execute("UPDATE reminders SET done = 1 WHERE id = ?", (rid,))
            self._db.commit()

    def mark_reminder_fired(self, rid, day):
        with self._lock:
            self._db.execute("UPDATE reminders SET last_fired = ? WHERE id = ?", (day, rid))
            self._db.commit()

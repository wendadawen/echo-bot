"""持久化：群消息、按群记忆、定时提醒。只用 Python 标准库。"""

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
                CREATE TABLE IF NOT EXISTS facts (
                    group_id TEXT NOT NULL,
                    key TEXT NOT NULL,
                    value TEXT NOT NULL,
                    ts REAL NOT NULL,
                    PRIMARY KEY (group_id, key)
                );
                CREATE TABLE IF NOT EXISTS reminders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    user_name TEXT NOT NULL,
                    text TEXT NOT NULL,
                    due_at REAL NOT NULL,
                    umo TEXT NOT NULL DEFAULT '',
                    done INTEGER NOT NULL DEFAULT 0
                );
                """
            )
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

    def set_fact(self, group_id, key, value):
        with self._lock:
            self._db.execute(
                "INSERT INTO facts(group_id, key, value, ts) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(group_id, key) DO UPDATE SET value=excluded.value, ts=excluded.ts",
                (group_id, key.strip(), value.strip(), time.time()),
            )
            self._db.commit()

    def facts(self, group_id):
        with self._lock:
            rows = self._db.execute(
                "SELECT key, value FROM facts WHERE group_id = ? ORDER BY ts", (group_id,)
            ).fetchall()
        return [dict(r) for r in rows]

    def facts_text(self, group_id):
        items = self.facts(group_id)
        if not items:
            return "（暂无）"
        return "\n".join("- {}：{}".format(it["key"], it["value"]) for it in items)

    def add_reminder(self, group_id, user_id, user_name, text, due_at, umo=""):
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO reminders(group_id, user_id, user_name, text, due_at, umo) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (group_id, user_id, user_name, text, due_at, umo),
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
                "SELECT * FROM reminders WHERE done = 0 AND due_at <= ? ORDER BY due_at",
                (now,),
            ).fetchall()
        return [dict(r) for r in rows]

    def mark_reminder_done(self, rid):
        with self._lock:
            self._db.execute("UPDATE reminders SET done = 1 WHERE id = ?", (rid,))
            self._db.commit()

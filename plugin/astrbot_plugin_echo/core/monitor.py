"""运行监控：记录每次调用、耗时、错误与用量，按群出统计。

对齐 MaiBot `src/maisaka/monitor/` 的事件存储思路，但只保留排障需要的字段。
独立 SQLite，不依赖框架，也不影响主流程（写入失败一律吞掉）。
"""

import math
import sqlite3
import threading
import time


class Monitor:
    def __init__(self, path=":memory:"):
        self._lock = threading.Lock()
        self._closed = False
        self._db = sqlite3.connect(path, timeout=30.0, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA busy_timeout=30000")
        if str(path) != ":memory:":
            try:
                self._db.execute("PRAGMA journal_mode=WAL")
                self._db.execute("PRAGMA synchronous=NORMAL")
            except sqlite3.Error:
                pass
        with self._lock, self._db:
            self._db.executescript(
                """
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_id TEXT NOT NULL DEFAULT '',
                    kind TEXT NOT NULL,
                    ok INTEGER NOT NULL DEFAULT 1,
                    latency REAL NOT NULL DEFAULT 0,
                    tokens INTEGER NOT NULL DEFAULT 0,
                    detail TEXT NOT NULL DEFAULT '',
                    ts REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS events_group_ts ON events(group_id, ts);
                CREATE INDEX IF NOT EXISTS events_ts ON events(ts);
                """
            )

    def close(self):
        with self._lock:
            if not self._closed:
                self._closed = True
                try:
                    self._db.close()
                except sqlite3.Error:
                    pass

    def record(self, group_id, kind, ok=True, latency=0.0, tokens=0, detail="",
               now=None):
        now = time.time() if now is None else now
        try:
            with self._lock:
                if self._closed:
                    return False
                with self._db:
                    self._db.execute(
                        "INSERT INTO events(group_id, kind, ok, latency, tokens, detail, ts) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (str(group_id or ""), str(kind)[:24], 1 if ok else 0,
                         float(latency or 0), int(tokens or 0), str(detail)[:200], now),
                    )
        except sqlite3.Error:
            return False
        return True

    def stats(self, group_id=None, hours=24, now=None):
        now = time.time() if now is None else now
        since = now - float(hours) * 3600
        where = "WHERE ts >= ?" + (" AND group_id = ?" if group_id else "")
        params = (since, str(group_id)) if group_id else (since,)
        with self._lock:
            if self._closed:
                return {"total": 0, "errors": 0, "error_rate": 0.0, "avg_latency": 0.0, "p95_latency": 0.0, "tokens": 0}
            row = self._db.execute(
                "SELECT COUNT(*) AS total, "
                "SUM(CASE WHEN ok = 0 THEN 1 ELSE 0 END) AS errors, "
                "AVG(latency) AS avg_latency, SUM(tokens) AS tokens "
                "FROM events " + where,
                params,
            ).fetchone()
            latencies = [r["latency"] for r in self._db.execute(
                "SELECT latency FROM events " + where + " ORDER BY latency", params
            ).fetchall()]
        total = int(row["total"] or 0)
        errors = int(row["errors"] or 0)
        p95 = 0.0
        if latencies:
            p95 = latencies[max(0, math.ceil(len(latencies) * 0.95) - 1)]
        return {
            "total": total,
            "errors": errors,
            "error_rate": round(errors / total, 3) if total else 0.0,
            "avg_latency": round(float(row["avg_latency"] or 0), 2),
            "p95_latency": round(float(p95 or 0), 2),
            "tokens": int(row["tokens"] or 0),
        }

    def by_kind(self, group_id=None, hours=24, now=None):
        now = time.time() if now is None else now
        since = now - float(hours) * 3600
        where = "WHERE ts >= ?" + (" AND group_id = ?" if group_id else "")
        params = (since, str(group_id)) if group_id else (since,)
        with self._lock:
            if self._closed:
                return []
            rows = self._db.execute(
                "SELECT kind, COUNT(*) AS n, SUM(CASE WHEN ok = 0 THEN 1 ELSE 0 END) "
                "AS errors FROM events " + where + " GROUP BY kind ORDER BY n DESC",
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def events(self, limit=50, group_id=None):
        sql = "SELECT * FROM events"
        params = []
        if group_id:
            sql += " WHERE group_id = ?"
            params.append(str(group_id))
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(int(limit))
        with self._lock:
            if self._closed:
                return []
            rows = self._db.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def prune(self, days=7, now=None):
        now = time.time() if now is None else now
        cutoff = now - float(days) * 86400
        with self._lock:
            if self._closed:
                return 0
            with self._db:
                cur = self._db.execute("DELETE FROM events WHERE ts < ?", (cutoff,))
                return cur.rowcount

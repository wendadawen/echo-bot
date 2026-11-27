"""统一 SQLite 仓库门面：组合 WAL 读写连接池、版本化迁移、记忆/人物混入层、重载待投递队列与监控指标。"""

from __future__ import annotations

import contextlib
import json
import math
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterator, Optional

from ..domain import PendingDelivery
from .cache_cleanup import cleanup_runtime_caches
from .memory_store import (
    CONV_STATE_MAX_AGE,
    DEFAULT_HISTORY_LIMIT,
    DEFAULT_MAX_MESSAGES_PER_GROUP,
    FORGET_AFTER,
    HALF_LIFE,
    MAX_EPISODES,
    MAX_MEMORIES,
    MemoryStoreOps,
    _decay,
    _fts_trigram_terms,
    query_terms,
)
from .migration import migrate_legacy_v1_dbs
from .person_store import (
    MAX_PERSON_FACTS_PER_FIELD,
    PersonStoreOps,
    _merge_field,
    _split_atomic_facts,
)
from .schema import (
    EMOJI_TABLES_DDL,
    PERSON_FACTS_DDL,
    SCHEMA_VERSION,
    _PENDING_DELIVERY_DDL,
    _configure_conn,
    init_schema,
)

__all__ = [
    "CONV_STATE_MAX_AGE",
    "DEFAULT_HISTORY_LIMIT",
    "DEFAULT_MAX_MESSAGES_PER_GROUP",
    "EMOJI_TABLES_DDL",
    "FORGET_AFTER",
    "HALF_LIFE",
    "MAX_EPISODES",
    "MAX_MEMORIES",
    "MAX_PERSON_FACTS_PER_FIELD",
    "PERSON_FACTS_DDL",
    "Repository",
    "SCHEMA_VERSION",
    "_configure_conn",
    "cleanup_runtime_caches",
    "query_terms",
]


class Repository(MemoryStoreOps, PersonStoreOps):
    """统一 SQLite 仓库：WAL 读写分离、版本化迁移、FTS5(trigram)记忆、结构化人物事实与待投递队列。"""

    def __init__(self, path: str | Path = ":memory:"):
        self._path = str(path)
        self._closed = False
        self._write_lock = threading.RLock()
        self._lock = self._write_lock
        self._conn_lock = threading.Lock()
        self._read_local = threading.local()
        self._read_conns: set[sqlite3.Connection] = set()
        self._fts = False
        self._fts_trigram = False
        self._db = _configure_conn(
            sqlite3.connect(self._path, timeout=30.0, check_same_thread=False),
            self._path,
        )
        self._init_schema()
        if self._path != ":memory:":
            self.migrate_legacy_dbs(Path(self._path).parent)

    @contextlib.contextmanager
    def read_conn(self) -> Iterator[Optional[sqlite3.Connection]]:
        if self._closed:
            yield None
            return
        if self._path == ":memory:" or getattr(self._read_local, "writing", False):
            with self._write_lock:
                if self._closed:
                    yield None
                    return
                yield self._db
            return
        conn = getattr(self._read_local, "conn", None)
        if conn is None:
            created = _configure_conn(
                sqlite3.connect(self._path, timeout=30.0, check_same_thread=False),
                self._path,
                read_only=True,
            )
            with self._conn_lock:
                if self._closed:
                    try:
                        created.close()
                    except sqlite3.Error:
                        pass
                    yield None
                    return
                self._read_conns.add(created)
                self._read_local.conn = created
                conn = created
        if self._closed:
            yield None
            return
        try:
            yield conn
        except sqlite3.ProgrammingError:
            yield None

    @contextlib.contextmanager
    def write_conn(self) -> Iterator[Optional[sqlite3.Connection]]:
        with self._write_lock:
            if self._closed:
                yield None
                return
            prev = getattr(self._read_local, "writing", False)
            self._read_local.writing = True
            try:
                if prev or self._db.in_transaction:
                    yield self._db
                else:
                    with self._db:
                        yield self._db
            finally:
                self._read_local.writing = prev

    def _init_schema(self) -> None:
        with self._write_lock:
            self._fts, self._fts_trigram = init_schema(
                self._db,
                backfill_person_facts_fn=self._backfill_person_facts_locked,
            )

    def close(self) -> None:
        with self._write_lock:
            if not self._closed:
                self._closed = True
                with self._conn_lock:
                    conns = list(self._read_conns)
                    self._read_conns.clear()
                for c in conns:
                    try:
                        c.close()
                    except sqlite3.Error:
                        pass
                try:
                    self._db.close()
                except sqlite3.Error:
                    pass

    # ---- 无损自动迁移旧版分离库 ----
    def migrate_legacy_dbs(self, data_dir: Path | str) -> dict[str, int]:
        """从旧版 memory.sqlite3 / persons.sqlite3 / monitor.sqlite3 幂等无损迁移。"""
        with self._write_lock:
            if self._closed:
                return {"episodes": 0, "memories": 0, "aliases": 0, "persons": 0, "events": 0}
            prev = getattr(self._read_local, "writing", False)
            self._read_local.writing = True
            try:
                summary = migrate_legacy_v1_dbs(
                    self._db,
                    data_dir,
                    resolve_person=self.resolve_person,
                    observe_person=self.observe_person,
                    upsert_person=self.upsert_person,
                )
                if summary.get("persons", 0) > 0:
                    with self._db:
                        self._backfill_person_facts_locked(self._db)
                return summary
            finally:
                self._read_local.writing = prev

    # ---- 重载安全待投递队列 ----
    def _row_to_delivery(self, row: sqlite3.Row) -> PendingDelivery:
        try:
            files = json.loads(row["files_json"] or "[]")
        except (ValueError, TypeError):
            files = []
        try:
            pre_hist = json.loads(row["pre_history_json"] or "[]")
        except (ValueError, TypeError):
            pre_hist = []
        return PendingDelivery(
            task_id=str(row["task_id"]),
            group_id=str(row["group_id"]),
            umo=str(row["umo"] or ""),
            prompt=str(row["prompt"] or ""),
            status=str(row["status"] or "failed"),
            reason=str(row["reason"] or "error"),
            result=str(row["result"] or ""),
            partial=str(row["partial"] or ""),
            files=tuple(files) if isinstance(files, list) else (),
            error=str(row["error"] or ""),
            pre_history=tuple(pre_hist) if isinstance(pre_hist, list) else (),
            created_at=float(row["created_at"] or 0.0),
        )

    def save_pending_delivery(self, delivery: PendingDelivery) -> bool:
        if not delivery.task_id or not delivery.group_id:
            return False
        params = (
            str(delivery.task_id),
            str(delivery.group_id),
            str(delivery.umo or ""),
            str(delivery.prompt or "")[:500],
            str(delivery.status or "failed"),
            str(delivery.reason or "error"),
            str(delivery.result or "")[:4000],
            str(delivery.partial or "")[:1000],
            json.dumps(list(delivery.files or ()), ensure_ascii=False),
            str(delivery.error or "")[:500],
            json.dumps(list(delivery.pre_history or ()), ensure_ascii=False),
            float(delivery.created_at or time.time()),
        )
        sql = (
            "INSERT OR REPLACE INTO pending_deliveries("
            "task_id, group_id, umo, prompt, status, reason, result, partial, "
            "files_json, error, pre_history_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
        )
        with self.write_conn() as conn:
            if conn is not None:
                conn.execute(sql, params)
                return True
        if self._path == ":memory:":
            return False
        # 插件重载关闭主连接瞬间后台任务刚落盘：临时开独立连接补写，确保不丢失成果。
        tmp_conn = _configure_conn(sqlite3.connect(self._path, timeout=30.0), self._path)
        try:
            with tmp_conn:
                tmp_conn.executescript(_PENDING_DELIVERY_DDL)
                tmp_conn.execute(sql, params)
            return True
        except sqlite3.Error:
            return False
        finally:
            try:
                tmp_conn.close()
            except sqlite3.Error:
                pass

    def list_pending_deliveries(self, limit: int = 20) -> list[PendingDelivery]:
        with self.read_conn() as conn:
            if conn is None:
                return []
            rows = conn.execute(
                "SELECT * FROM pending_deliveries ORDER BY created_at ASC LIMIT ?",
                (max(1, int(limit)),),
            ).fetchall()
        return [self._row_to_delivery(r) for r in rows]

    def delete_pending_delivery(self, task_id: str) -> bool:
        if not task_id:
            return False
        with self.write_conn() as conn:
            if conn is None:
                return False
            cur = conn.execute(
                "DELETE FROM pending_deliveries WHERE task_id = ?", (str(task_id),)
            )
            return (cur.rowcount or 0) > 0

    def pending_delivery_paths(self) -> set[str]:
        with self.read_conn() as conn:
            if conn is None:
                return set()
            rows = conn.execute("SELECT files_json FROM pending_deliveries").fetchall()
        paths: set[str] = set()
        for r in rows:
            try:
                items = json.loads(r["files_json"] or "[]")
            except (ValueError, TypeError):
                continue
            if isinstance(items, list):
                for entry in items:
                    if isinstance(entry, dict) and entry.get("path"):
                        paths.add(str(entry["path"]))
        return paths

    # ---- 运行监控与错峰熔断查询 ----
    def record_event(
        self,
        group_id: str,
        kind: str,
        ok: bool = True,
        latency: float = 0.0,
        tokens: int = 0,
        detail: str = "",
        now: Optional[float] = None,
    ) -> bool:
        stamp = time.time() if now is None else float(now)
        try:
            with self.write_conn() as conn:
                if conn is None:
                    return False
                conn.execute(
                    "INSERT INTO monitor_events(group_id, kind, ok, latency, tokens, detail, ts) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        str(group_id or ""),
                        str(kind)[:24],
                        1 if ok else 0,
                        float(latency or 0.0),
                        int(tokens or 0),
                        str(detail)[:200],
                        stamp,
                    ),
                )
            return True
        except sqlite3.Error:
            return False

    def recent_error_count(
        self,
        group_id: str,
        window_seconds: float = 120.0,
        now: Optional[float] = None,
    ) -> int:
        cur = time.time() if now is None else float(now)
        since = cur - max(1.0, float(window_seconds))
        with self.read_conn() as conn:
            if conn is None:
                return 0
            row = conn.execute(
                "SELECT COUNT(*) n FROM monitor_events WHERE group_id = ? AND kind = 'model_error' AND ts >= ?",
                (str(group_id), since),
            ).fetchone()
        return int(row["n"] or 0) if row else 0

    def monitor_stats(
        self,
        group_id: Optional[str] = None,
        hours: float = 24,
        now: Optional[float] = None,
    ) -> dict[str, Any]:
        cur = time.time() if now is None else float(now)
        since = cur - float(hours) * 3600
        where = "WHERE ts >= ?" + (" AND group_id = ?" if group_id else "")
        params: tuple[Any, ...] = (since, str(group_id)) if group_id else (since,)
        with self.read_conn() as conn:
            if conn is None:
                return {"total": 0, "errors": 0, "error_rate": 0.0, "avg_latency": 0.0, "p95_latency": 0.0, "tokens": 0}
            row = conn.execute(
                "SELECT COUNT(*) AS total, SUM(CASE WHEN ok = 0 THEN 1 ELSE 0 END) AS errors, "
                "AVG(latency) AS avg_latency, SUM(tokens) AS tokens FROM monitor_events " + where,
                params,
            ).fetchone()
            latencies = [
                r["latency"]
                for r in conn.execute(
                    "SELECT latency FROM monitor_events " + where + " ORDER BY latency", params
                ).fetchall()
            ]
        total = int(row["total"] or 0)
        errors = int(row["errors"] or 0)
        p95 = latencies[max(0, math.ceil(len(latencies) * 0.95) - 1)] if latencies else 0.0
        return {
            "total": total,
            "errors": errors,
            "error_rate": round(errors / total, 3) if total else 0.0,
            "avg_latency": round(float(row["avg_latency"] or 0), 2),
            "p95_latency": round(float(p95 or 0), 2),
            "tokens": int(row["tokens"] or 0),
        }

    def prune_monitor(self, max_age_days: int = 30, now: Optional[float] = None) -> int:
        cutoff = (time.time() if now is None else float(now)) - max(1, int(max_age_days)) * 86400
        with self.write_conn() as conn:
            if conn is None:
                return 0
            cur = conn.execute("DELETE FROM monitor_events WHERE ts < ?", (cutoff,))
            return max(0, cur.rowcount or 0)

    def stats(self, group_id: str) -> dict[str, Any]:
        with self.read_conn() as conn:
            if conn is None:
                return {"fts": self._fts}
            gid = str(group_id)
            return {
                "messages": conn.execute("SELECT COUNT(*) FROM messages WHERE group_id=?", (gid,)).fetchone()[0],
                "episodes": conn.execute("SELECT COUNT(*) FROM episodes WHERE group_id=?", (gid,)).fetchone()[0],
                "memories": conn.execute("SELECT COUNT(*) FROM memories WHERE group_id=? AND state='active'", (gid,)).fetchone()[0],
                "persons": conn.execute("SELECT COUNT(*) FROM persons WHERE group_id=?", (gid,)).fetchone()[0],
                "fts": self._fts,
            }

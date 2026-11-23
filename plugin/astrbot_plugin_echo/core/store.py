"""持久化：消息历史、定时提醒、重载跨实例待投递队列。只用 Python 标准库。"""

import fnmatch
import json
import sqlite3
import threading
import time
from pathlib import Path

DEFAULT_HISTORY_LIMIT = 30
DEFAULT_MAX_MESSAGES_PER_GROUP = 500

_PENDING_DELIVERY_DDL = """
CREATE TABLE IF NOT EXISTS pending_deliveries (
    task_id TEXT PRIMARY KEY,
    group_id TEXT NOT NULL,
    umo TEXT NOT NULL DEFAULT '',
    prompt TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'failed',
    reason TEXT NOT NULL DEFAULT 'error',
    result TEXT NOT NULL DEFAULT '',
    partial TEXT NOT NULL DEFAULT '',
    files_json TEXT NOT NULL DEFAULT '[]',
    error TEXT NOT NULL DEFAULT '',
    pre_history_json TEXT NOT NULL DEFAULT '[]',
    created_at REAL NOT NULL
);
"""


def _configure_conn(conn, path):
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    if str(path) != ":memory:":
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
        except sqlite3.Error:
            pass
    return conn


class Store:
    def __init__(self, path=":memory:"):
        self._path = str(path)
        self._closed = False
        self._lock = threading.RLock()
        self._db = _configure_conn(
            sqlite3.connect(self._path, timeout=30.0, check_same_thread=False),
            self._path,
        )
        with self._lock, self._db:
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
                CREATE INDEX IF NOT EXISTS idx_messages_group_seq
                    ON messages(group_id, seq);
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
                CREATE INDEX IF NOT EXISTS idx_reminders_done_due
                    ON reminders(done, due_at);
                """
                + _PENDING_DELIVERY_DDL
            )
            # 兼容早期版本的数据库文件
            columns = {row["name"] for row in self._db.execute("PRAGMA table_info(reminders)")}
            if "repeat" not in columns:
                self._db.execute("ALTER TABLE reminders ADD COLUMN repeat TEXT NOT NULL DEFAULT ''")
            if "last_fired" not in columns:
                self._db.execute("ALTER TABLE reminders ADD COLUMN last_fired TEXT NOT NULL DEFAULT ''")

    def close(self):
        with self._lock:
            if not self._closed:
                self._closed = True
                try:
                    self._db.close()
                except sqlite3.Error:
                    pass

    def add_message(self, group_id, user_id, user_name, role, text, ts=None):
        ts = time.time() if ts is None else ts
        with self._lock:
            if self._closed:
                return 0
            with self._db:
                cur = self._db.execute(
                    "INSERT INTO messages(group_id, user_id, user_name, role, text, ts) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (group_id, user_id, user_name, role, text, ts),
                )
                return cur.lastrowid

    def recent(self, group_id, n=DEFAULT_HISTORY_LIMIT):
        with self._lock:
            if self._closed:
                return []
            rows = self._db.execute(
                "SELECT * FROM messages WHERE group_id = ? ORDER BY seq DESC LIMIT ?",
                (group_id, max(0, int(n))),
            ).fetchall()
        return [dict(r) for r in reversed(rows)]

    def since(self, group_id, ts=0.0, limit=30):
        """按时间顺序取 ts 之后的群消息，供多轮记忆摘要窗口使用。"""
        with self._lock:
            if self._closed:
                return []
            rows = self._db.execute(
                "SELECT * FROM messages WHERE group_id = ? AND ts > ? "
                "ORDER BY seq DESC LIMIT ?",
                (group_id, float(ts or 0.0), max(1, int(limit))),
            ).fetchall()
        return [dict(r) for r in reversed(rows)]

    def add_reminder(self, group_id, user_id, user_name, text, due_at, umo="", repeat="",
                     last_fired=""):
        with self._lock:
            if self._closed:
                return 0
            with self._db:
                cur = self._db.execute(
                    "INSERT INTO reminders(group_id, user_id, user_name, text, due_at, umo, repeat, last_fired) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (group_id, user_id, user_name, text, due_at, umo, repeat, str(last_fired or "")),
                )
                return cur.lastrowid

    def pending_reminders(self, group_id):
        with self._lock:
            if self._closed:
                return []
            rows = self._db.execute(
                "SELECT * FROM reminders WHERE group_id = ? AND done = 0 ORDER BY due_at",
                (group_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def due_reminders(self, now=None):
        now = time.time() if now is None else now
        with self._lock:
            if self._closed:
                return []
            rows = self._db.execute(
                "SELECT * FROM reminders WHERE done = 0 "
                "AND ((repeat = '' AND due_at <= ?) OR repeat <> '') ORDER BY due_at",
                (now,),
            ).fetchall()
        return [dict(r) for r in rows]

    def peek_due_reminders(self, now=None):
        """读取到期未完成的提醒但不立即提交完成，供先发后记流程使用。"""
        return self.due_reminders(now)

    def mark_reminder_done(self, rid):
        with self._lock:
            if self._closed:
                return False
            with self._db:
                cur = self._db.execute("UPDATE reminders SET done = 1 WHERE id = ?", (rid,))
                return cur.rowcount > 0

    def mark_reminder_fired(self, rid, day):
        with self._lock:
            if self._closed:
                return False
            with self._db:
                cur = self._db.execute("UPDATE reminders SET last_fired = ? WHERE id = ?", (day, rid))
                return cur.rowcount > 0

    def reschedule_reminder(self, rid, due_at):
        """发送失败时推迟一次性提醒重试时间，避免丢失或紧循环重试。"""
        with self._lock:
            if self._closed:
                return False
            with self._db:
                cur = self._db.execute(
                    "UPDATE reminders SET due_at = ? WHERE id = ? AND done = 0",
                    (float(due_at), int(rid)),
                )
                return cur.rowcount > 0

    def cancel_reminder(self, rid, group_id=""):
        """按提醒 id（可选按群隔离）取消尚未完成的一次性/每日提醒。"""
        with self._lock:
            if self._closed:
                return False
            with self._db:
                if group_id:
                    cur = self._db.execute(
                        "UPDATE reminders SET done = 1 WHERE id = ? AND group_id = ? AND done = 0",
                        (int(rid), str(group_id)),
                    )
                else:
                    cur = self._db.execute(
                        "UPDATE reminders SET done = 1 WHERE id = ? AND done = 0",
                        (int(rid),),
                    )
                return cur.rowcount > 0

    def clear_messages(self, group_id):
        """清空本群聊天记录（提醒与记忆不动）。"""
        with self._lock:
            if self._closed:
                return 0
            with self._db:
                cur = self._db.execute("DELETE FROM messages WHERE group_id = ?", (group_id,))
                return cur.rowcount

    def prune_messages(self, keep_per_group=DEFAULT_MAX_MESSAGES_PER_GROUP):
        """按群保留最新 N 条群消息，定期裁掉过期原文并做轻度 WAL 检查点。"""
        keep = max(1, int(keep_per_group))
        total_deleted = 0
        with self._lock:
            if self._closed:
                return 0
            with self._db:
                groups = [
                    r["group_id"]
                    for r in self._db.execute("SELECT DISTINCT group_id FROM messages").fetchall()
                ]
                for gid in groups:
                    cur = self._db.execute(
                        "DELETE FROM messages WHERE group_id = ? AND seq NOT IN "
                        "(SELECT seq FROM messages WHERE group_id = ? ORDER BY seq DESC LIMIT ?)",
                        (gid, gid, keep),
                    )
                    total_deleted += max(0, int(cur.rowcount or 0))
            if self._path != ":memory:":
                try:
                    self._db.execute("PRAGMA wal_checkpoint(PASSIVE)")
                except sqlite3.Error:
                    pass
        return total_deleted

    def save_pending_delivery(self, task_id, group_id, umo="", prompt="", status="failed",
                              reason="error", result="", partial="", files=None,
                              error="", pre_history=None, created_at=None):
        """持久化待投递后台结果；即使主连接已因重载关闭也回退短连接写盘，防丢结果。"""
        task_id = str(task_id or "").strip()
        if not task_id:
            return False
        created = time.time() if created_at is None else float(created_at)
        files_json = json.dumps(list(files or []), ensure_ascii=False)
        history_json = json.dumps(list(pre_history or []), ensure_ascii=False)
        params = (
            task_id,
            str(group_id or ""),
            str(umo or ""),
            str(prompt or ""),
            str(status or "failed"),
            str(reason or "error"),
            str(result or ""),
            str(partial or ""),
            files_json,
            str(error or ""),
            history_json,
            created,
        )
        sql = (
            "INSERT OR REPLACE INTO pending_deliveries"
            "(task_id, group_id, umo, prompt, status, reason, result, partial, "
            "files_json, error, pre_history_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
        )
        with self._lock:
            if not self._closed:
                try:
                    with self._db:
                        self._db.execute(sql, params)
                    return True
                except sqlite3.ProgrammingError:
                    pass
                except sqlite3.Error:
                    return False
        if self._path == ":memory:":
            return False
        conn = None
        try:
            conn = _configure_conn(
                sqlite3.connect(self._path, timeout=30.0, check_same_thread=False),
                self._path,
            )
            with conn:
                conn.executescript(_PENDING_DELIVERY_DDL)
                conn.execute(sql, params)
            return True
        except sqlite3.Error:
            return False
        finally:
            if conn is not None:
                try:
                    conn.close()
                except sqlite3.Error:
                    pass

    def pop_pending_deliveries(self, limit=20):
        """原子取出并移除待补发的后台任务结果。"""
        with self._lock:
            if self._closed:
                return []
            with self._db:
                rows = self._db.execute(
                    "SELECT * FROM pending_deliveries ORDER BY created_at ASC LIMIT ?",
                    (max(1, int(limit)),),
                ).fetchall()
                if not rows:
                    return []
                ids = [r["task_id"] for r in rows]
                placeholders = ",".join("?" for _ in ids)
                self._db.execute(
                    "DELETE FROM pending_deliveries WHERE task_id IN ({})".format(placeholders),
                    ids,
                )
        out = []
        for r in rows:
            item = dict(r)
            try:
                files = json.loads(item.pop("files_json", "[]") or "[]")
            except (ValueError, TypeError):
                files = []
            try:
                pre_history = json.loads(item.pop("pre_history_json", "[]") or "[]")
            except (ValueError, TypeError):
                pre_history = []
            item["files"] = files if isinstance(files, list) else []
            item["pre_history"] = pre_history if isinstance(pre_history, list) else []
            out.append(item)
        return out

    def delete_pending_delivery(self, task_id):
        if not task_id:
            return False
        with self._lock:
            if self._closed:
                return False
            with self._db:
                cur = self._db.execute(
                    "DELETE FROM pending_deliveries WHERE task_id = ?", (str(task_id),)
                )
                return cur.rowcount > 0

    def pending_delivery_paths(self):
        """返回仍在待补发队列里的文件路径集合，防止启动清理误删。"""
        with self._lock:
            if self._closed:
                return set()
            rows = self._db.execute("SELECT files_json FROM pending_deliveries").fetchall()
        paths = set()
        for r in rows:
            try:
                items = json.loads(r["files_json"] or "[]")
            except (ValueError, TypeError):
                continue
            if not isinstance(items, list):
                continue
            for entry in items:
                if isinstance(entry, dict) and entry.get("path"):
                    paths.add(str(entry["path"]))
        return paths


def _has_symlink_component(start, end):
    cur = end
    while True:
        if cur.is_symlink():
            return True
        if cur == start:
            return False
        parent = cur.parent
        if parent == cur:
            return False
        cur = parent


def _purge_old_files(target_dir, root_dir, resolved_root, cutoff, pattern="*", recursive=False):
    if not target_dir.exists() or target_dir.is_symlink() or not target_dir.is_dir():
        return 0
    if _has_symlink_component(root_dir, target_dir):
        return 0
    try:
        resolved_dir = target_dir.resolve()
    except OSError:
        return 0
    if resolved_dir != resolved_root and resolved_root not in resolved_dir.parents:
        return 0
    removed = 0
    stack = [target_dir]
    while stack:
        cur = stack.pop()
        try:
            entries = list(cur.iterdir())
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_symlink():
                    continue
                if cur == target_dir and pattern != "*" and not fnmatch.fnmatch(entry.name, pattern):
                    continue
                if entry.is_dir():
                    if recursive:
                        stack.append(entry)
                    continue
                if not entry.is_file():
                    continue
                resolved_entry = entry.resolve()
                if resolved_root not in resolved_entry.parents:
                    continue
                if entry.lstat().st_mtime <= cutoff:
                    entry.unlink(missing_ok=True)
                    removed += 1
            except OSError:
                continue
    return removed


def cleanup_runtime_caches(data_root, now=None, temp_max_age=86400, media_max_age=259200):
    """定期清理 AstrBot 临时图片及 QQ 日志/媒体缓存，严禁触碰登录态与数据库。"""
    if not data_root:
        return 0
    root = Path(data_root)
    if root.is_symlink() or not root.is_dir():
        return 0
    try:
        resolved_root = root.resolve()
    except OSError:
        return 0

    now_ts = time.time() if now is None else float(now)
    temp_cutoff = now_ts - max(0, int(temp_max_age))
    media_cutoff = now_ts - max(0, int(media_max_age))
    removed = 0

    removed += _purge_old_files(
        root / "temp", root, resolved_root, temp_cutoff, pattern="media_*", recursive=False
    )

    napcat_cutoff = now_ts - 7 * 86400
    for sub in ("logs", "cache"):
        removed += _purge_old_files(
            root / "napcat" / sub, root, resolved_root, napcat_cutoff, recursive=True
        )

    qq_dir = root / "napcat" / "home" / ".config" / "QQ"
    if qq_dir.is_dir() and not qq_dir.is_symlink() and not _has_symlink_component(root, qq_dir):
        removed += _purge_old_files(
            qq_dir / "global" / "nt_data" / "Log",
            root,
            resolved_root,
            temp_cutoff,
            recursive=True,
        )
        removed += _purge_old_files(
            qq_dir / "NapCat" / "temp",
            root,
            resolved_root,
            temp_cutoff,
            recursive=True,
        )
        try:
            acc_dirs = list(qq_dir.glob("nt_qq_*"))
        except OSError:
            acc_dirs = []
        for acc in acc_dirs:
            if not acc.is_dir() or acc.is_symlink():
                continue
            removed += _purge_old_files(
                acc / "nt_temp", root, resolved_root, temp_cutoff, recursive=True
            )
            nt_data = acc / "nt_data"
            if not nt_data.is_dir() or nt_data.is_symlink():
                continue
            for sub in ("log", "log-cache", "Pic", "Video", "File", "Ptt"):
                removed += _purge_old_files(
                    nt_data / sub, root, resolved_root, media_cutoff, recursive=True
                )

    return removed

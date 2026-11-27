"""统一存储层：将消息历史、群记忆（FTS5+双字）、QQ号归一人物档案、
滚动对话状态、重载待投递队列与监控指标统一在单个 echo.sqlite3 中。

同时提供对旧版分离数据库（memory.sqlite3 / persons.sqlite3 / monitor.sqlite3）的
幂等无损自动迁移，以及符号链接安全的 AstrBot/NapCat 缓存清理。
"""

from __future__ import annotations

import fnmatch
import json
import math
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Optional

from ..domain import PendingDelivery, clean_speaker_name
from .migration import migrate_legacy_v1_dbs

DEFAULT_HISTORY_LIMIT = 30
DEFAULT_MAX_MESSAGES_PER_GROUP = 500
HALF_LIFE = 14 * 86400
FORGET_AFTER = 60 * 86400
MAX_EPISODES = 200
MAX_MEMORIES = 450
CONV_STATE_MAX_AGE = 7200.0
_PLACEHOLDER_VALUES = frozenset({"-", "—", "–", "无", "暂无", "未知", "none", "null", "n/a"})

_STOP_BIGRAMS = frozenset({
    "什么", "怎么", "这个", "那个", "就是", "不是", "可以", "我们", "你们", "他们",
    "现在", "今天", "昨天", "明天", "时候", "东西", "地方", "觉得", "感觉", "知道",
    "告诉", "一下", "一点", "有没", "没有", "能不", "不能", "是不", "为什", "为啥",
    "多少", "哪些", "怎样", "如何", "刚才", "之前", "以前", "请问", "帮忙", "帮我",
    "这里", "那里", "这么", "那么", "意思", "聊过", "记得",
})

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


def query_terms(query: str) -> list[str]:
    """把中英文自然语言问句拆成检索词：英文/数字词 + 中文短语与二元组。"""
    raw = str(query or "").strip()
    if not raw:
        return []
    seen: set[str] = set()
    terms: list[str] = []

    def add(tok: str) -> None:
        t = tok.strip().lower()
        if len(t) >= 2 and t not in seen:
            seen.add(t)
            terms.append(t)

    for w in re.findall(r"[a-zA-Z0-9]{2,}", raw):
        add(w)
    for span in re.findall(r"[\u4e00-\u9fff]{2,}", raw):
        if len(span) <= 4 and span not in _STOP_BIGRAMS:
            add(span)
        for i in range(len(span) - 1):
            bg = span[i:i + 2]
            if bg not in _STOP_BIGRAMS:
                add(bg)
    if len(terms) <= 48:
        return terms
    head = terms[:16]
    tail = [t for t in terms[16:] if t not in head][-32:]
    return head + tail


def _decay(weight: float, last_hit: float, now: Optional[float] = None) -> float:
    cur = time.time() if now is None else float(now)
    elapsed = max(0.0, cur - float(last_hit or 0.0))
    return max(0.05, float(weight or 1.0) * math.pow(0.5, elapsed / HALF_LIFE))


def _levenshtein(left: str, right: str) -> int:
    if left == right:
        return 0
    if not left:
        return len(right)
    if not right:
        return len(left)
    prev = list(range(len(right) + 1))
    for i, ch in enumerate(left, 1):
        cur = [i]
        for j, other in enumerate(right, 1):
            cost = 0 if ch == other else 1
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost))
        prev = cur
    return prev[-1]


def _similarity(left: str, right: str) -> float:
    l, r = str(left or ""), str(right or "")
    if not l or not r:
        return 0.0
    return 1.0 - _levenshtein(l, r) / float(max(len(l), len(r)))


def _merge_field(existing: str, incoming: str, limit: int) -> str:
    """合并分号分隔的人物印象短句，过滤无意义占位词并对近义重复句去重。"""

    def _valid_parts(raw: str) -> list[str]:
        return [
            p.strip()
            for p in str(raw or "").split("；")
            if p.strip() and p.strip().lower() not in _PLACEHOLDER_VALUES
        ]

    parts = _valid_parts(existing)
    for item in _valid_parts(incoming):
        dup_idx: Optional[int] = None
        skip = False
        for i, prev in enumerate(parts):
            if item == prev or item in prev or (_similarity(item, prev) >= 0.65 and len(prev) >= len(item)):
                skip = True
                break
            if prev in item or (_similarity(item, prev) >= 0.65 and len(item) > len(prev)):
                dup_idx = i
                break
        if skip:
            continue
        if dup_idx is not None:
            parts[dup_idx] = item
        else:
            parts = [p for p in parts if p not in item and _similarity(item, p) < 0.65]
            parts.append(item)
    joined = "；".join(parts)
    while len(joined) > limit and len(parts) > 1:
        parts.pop(0)
        joined = "；".join(parts)
    return joined[-limit:] if len(joined) > limit else joined


def _configure_conn(conn: sqlite3.Connection, path: str) -> sqlite3.Connection:
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    if str(path) != ":memory:":
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
        except sqlite3.Error:
            pass
    return conn


class Repository:
    """统一 SQLite 仓库：消息、长期群记忆、人物与别名、图片缓存、会话状态、待投递队列与监控。"""

    def __init__(self, path: str | Path = ":memory:"):
        self._path = str(path)
        self._closed = False
        self._lock = threading.RLock()
        self._fts = False
        self._db = _configure_conn(
            sqlite3.connect(self._path, timeout=30.0, check_same_thread=False),
            self._path,
        )
        self._init_schema()
        if self._path != ":memory:":
            self.migrate_legacy_dbs(Path(self._path).parent)

    def _init_schema(self) -> None:
        with self._lock, self._db:
            self._db.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_meta (
                    k TEXT PRIMARY KEY,
                    v TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS messages (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    user_name TEXT NOT NULL,
                    role TEXT NOT NULL,
                    text TEXT NOT NULL,
                    ts REAL NOT NULL,
                    msg_id TEXT NOT NULL DEFAULT '',
                    meta TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_messages_group_seq
                    ON messages(group_id, seq);

                CREATE TABLE IF NOT EXISTS episodes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_id TEXT NOT NULL,
                    text TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    source TEXT NOT NULL DEFAULT '',
                    external_id TEXT NOT NULL DEFAULT '',
                    state TEXT NOT NULL DEFAULT 'ready'
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_episodes_external
                    ON episodes(group_id, external_id) WHERE external_id <> '';

                CREATE TABLE IF NOT EXISTS memories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_id TEXT NOT NULL,
                    category TEXT NOT NULL,
                    content TEXT NOT NULL,
                    weight REAL NOT NULL DEFAULT 1.0,
                    last_hit REAL NOT NULL,
                    state TEXT NOT NULL DEFAULT 'active',
                    protected INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_memories_lookup
                    ON memories(group_id, content);

                CREATE TABLE IF NOT EXISTS person_aliases (
                    group_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    kind TEXT NOT NULL DEFAULT 'nick',
                    hits INTEGER NOT NULL DEFAULT 1,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (group_id, user_id, name)
                );
                CREATE INDEX IF NOT EXISTS idx_person_aliases_name
                    ON person_aliases(group_id, name);

                CREATE TABLE IF NOT EXISTS persons (
                    group_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    traits TEXT NOT NULL DEFAULT '',
                    preferences TEXT NOT NULL DEFAULT '',
                    events TEXT NOT NULL DEFAULT '',
                    relation TEXT NOT NULL DEFAULT '',
                    intimacy REAL NOT NULL DEFAULT 0.5,
                    msg_count INTEGER NOT NULL DEFAULT 0,
                    first_seen REAL NOT NULL DEFAULT 0,
                    last_seen REAL NOT NULL DEFAULT 0,
                    updated_at REAL NOT NULL DEFAULT 0,
                    PRIMARY KEY (group_id, user_id)
                );

                CREATE TABLE IF NOT EXISTS conv_state (
                    group_id TEXT PRIMARY KEY,
                    topics_json TEXT NOT NULL DEFAULT '[]',
                    progress_json TEXT NOT NULL DEFAULT '[]',
                    pending_json TEXT NOT NULL DEFAULT '[]',
                    updated_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS monitor_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_id TEXT NOT NULL DEFAULT '',
                    kind TEXT NOT NULL,
                    ok INTEGER NOT NULL DEFAULT 1,
                    latency REAL NOT NULL DEFAULT 0,
                    tokens INTEGER NOT NULL DEFAULT 0,
                    detail TEXT NOT NULL DEFAULT '',
                    ts REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_monitor_group_ts
                    ON monitor_events(group_id, ts);
                CREATE INDEX IF NOT EXISTS idx_monitor_ts
                    ON monitor_events(ts);
                """
                + _PENDING_DELIVERY_DDL
            )
            msg_cols = {r["name"] for r in self._db.execute("PRAGMA table_info(messages)")}
            if "msg_id" not in msg_cols:
                self._db.execute("ALTER TABLE messages ADD COLUMN msg_id TEXT NOT NULL DEFAULT ''")
            if "meta" not in msg_cols:
                self._db.execute("ALTER TABLE messages ADD COLUMN meta TEXT NOT NULL DEFAULT ''")
            try:
                self._db.execute(
                    "CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts "
                    "USING fts5(content, content='memories', content_rowid='id')"
                )
                self._db.execute(
                    "CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN "
                    "INSERT INTO memories_fts(rowid, content) VALUES (new.id, new.content); END"
                )
                self._db.execute(
                    "CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN "
                    "INSERT INTO memories_fts(memories_fts, rowid, content) "
                    "VALUES ('delete', old.id, old.content); END"
                )
                self._db.execute("DROP TRIGGER IF EXISTS memories_au")
                self._db.execute(
                    "CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE OF content ON memories BEGIN "
                    "INSERT INTO memories_fts(memories_fts, rowid, content) "
                    "VALUES ('delete', old.id, old.content); "
                    "INSERT INTO memories_fts(rowid, content) VALUES (new.id, new.content); END"
                )
                self._db.execute("INSERT INTO memories_fts(memories_fts) VALUES ('rebuild')")
                self._fts = True
            except sqlite3.OperationalError:
                self._fts = False

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._closed = True
                try:
                    self._db.close()
                except sqlite3.Error:
                    pass

    # ---- 无损自动迁移旧版分离库 ----
    def migrate_legacy_dbs(self, data_dir: Path | str) -> dict[str, int]:
        """从旧版 memory.sqlite3 / persons.sqlite3 / monitor.sqlite3 幂等无损迁移。"""
        with self._lock:
            if self._closed:
                return {"episodes": 0, "memories": 0, "aliases": 0, "persons": 0, "events": 0}
            return migrate_legacy_v1_dbs(
                self._db,
                data_dir,
                resolve_person=self.resolve_person,
                observe_person=self.observe_person,
                upsert_person=self.upsert_person,
            )

    # ---- 消息历史 ----
    def add_message(
        self,
        group_id: str,
        user_id: str,
        user_name: str,
        role: str,
        text: str,
        ts: Optional[float] = None,
        msg_id: str = "",
        meta: str = "",
    ) -> int:
        stamp = time.time() if ts is None else float(ts)
        with self._lock:
            if self._closed:
                return 0
            with self._db:
                cur = self._db.execute(
                    "INSERT INTO messages(group_id, user_id, user_name, role, text, ts, msg_id, meta) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        str(group_id),
                        str(user_id or "unknown"),
                        str(user_name or ""),
                        str(role),
                        str(text),
                        stamp,
                        str(msg_id or ""),
                        str(meta or "")[:120],
                    ),
                )
                return int(cur.lastrowid or 0)

    def recent_messages(self, group_id: str, limit: int = DEFAULT_HISTORY_LIMIT) -> list[dict[str, Any]]:
        with self._lock:
            if self._closed:
                return []
            rows = self._db.execute(
                "SELECT * FROM messages WHERE group_id = ? ORDER BY seq DESC LIMIT ?",
                (str(group_id), max(0, int(limit))),
            ).fetchall()
        return [dict(r) for r in reversed(rows)]

    def messages_since(self, group_id: str, ts: float = 0.0, limit: int = 24) -> list[dict[str, Any]]:
        with self._lock:
            if self._closed:
                return []
            rows = self._db.execute(
                "SELECT * FROM messages WHERE group_id = ? AND ts > ? ORDER BY seq DESC LIMIT ?",
                (str(group_id), float(ts or 0.0), max(1, int(limit))),
            ).fetchall()
        return [dict(r) for r in reversed(rows)]

    def prune_messages(self, keep_per_group: int = DEFAULT_MAX_MESSAGES_PER_GROUP) -> int:
        keep = max(50, int(keep_per_group or DEFAULT_MAX_MESSAGES_PER_GROUP))
        deleted = 0
        with self._lock:
            if self._closed:
                return 0
            with self._db:
                groups = [r[0] for r in self._db.execute("SELECT DISTINCT group_id FROM messages").fetchall()]
                for gid in groups:
                    cur = self._db.execute(
                        "DELETE FROM messages WHERE group_id = ? AND seq NOT IN ("
                        "SELECT seq FROM messages WHERE group_id = ? ORDER BY seq DESC LIMIT ?"
                        ")",
                        (gid, gid, keep),
                    )
                    deleted += max(0, cur.rowcount or 0)
        return deleted

    # ---- 长期群记忆 & 情节 & 黑话 ----
    def last_episode_at(self, group_id: str) -> float:
        with self._lock:
            if self._closed:
                return 0.0
            row = self._db.execute(
                "SELECT MAX(created_at) FROM episodes WHERE group_id = ?",
                (str(group_id),),
            ).fetchone()
        return float(row[0] or 0.0) if row else 0.0

    def add_episode(
        self,
        group_id: str,
        text: str,
        now: Optional[float] = None,
        source: str = "",
        external_id: str = "",
    ) -> None:
        clean = str(text or "").strip()
        if not clean:
            return
        stamp = time.time() if now is None else float(now)
        with self._lock:
            if self._closed:
                return
            with self._db:
                if external_id:
                    self._db.execute(
                        "INSERT OR IGNORE INTO episodes(group_id, text, created_at, source, external_id) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (str(group_id), clean[:160], stamp, source, str(external_id)),
                    )
                else:
                    self._db.execute(
                        "INSERT INTO episodes(group_id, text, created_at, source) VALUES (?, ?, ?, ?)",
                        (str(group_id), clean[:160], stamp, source),
                    )
                self._db.execute(
                    "DELETE FROM episodes WHERE group_id = ? AND id NOT IN "
                    "(SELECT id FROM episodes WHERE group_id = ? ORDER BY id DESC LIMIT ?)",
                    (str(group_id), str(group_id), MAX_EPISODES),
                )

    def recent_episodes(self, group_id: str, limit: int = 3) -> list[str]:
        with self._lock:
            if self._closed:
                return []
            rows = self._db.execute(
                "SELECT text FROM episodes WHERE group_id = ? AND state = 'ready' "
                "ORDER BY id DESC LIMIT ?",
                (str(group_id), max(1, int(limit))),
            ).fetchall()
        return [r["text"] for r in reversed(rows)]

    def add_memory(
        self,
        group_id: str,
        category: str,
        content: str,
        now: Optional[float] = None,
        protected: bool = False,
    ) -> None:
        clean = str(content or "").strip()[:120]
        if not clean:
            return
        stamp = time.time() if now is None else float(now)
        cat = str(category or "event").strip()[:24]
        with self._lock:
            if self._closed:
                return
            with self._db:
                row = self._db.execute(
                    "SELECT id, weight, state, protected FROM memories WHERE group_id = ? AND content = ?",
                    (str(group_id), clean),
                ).fetchone()
                if row:
                    weight = min(5.0, float(row["weight"] or 1.0) + 0.6)
                    if row["state"] != "active":
                        weight = max(weight, 1.3)
                    prot = 1 if (protected or row["protected"]) else 0
                    if prot:
                        weight = max(weight, 3.0)
                    self._db.execute(
                        "UPDATE memories SET weight = ?, last_hit = ?, state = 'active', "
                        "category = ?, protected = ? WHERE id = ?",
                        (weight, stamp, cat, prot, row["id"]),
                    )
                else:
                    self._db.execute(
                        "INSERT INTO memories(group_id, category, content, weight, last_hit, state, protected) "
                        "VALUES (?, ?, ?, ?, ?, 'active', ?)",
                        (str(group_id), cat, clean, 3.0 if protected else 1.0, stamp, 1 if protected else 0),
                    )
                self._prune_memories_locked(str(group_id), stamp)

    def _prune_memories_locked(self, group_id: str, now: float) -> None:
        self._db.execute(
            "UPDATE memories SET state = 'forgotten' WHERE group_id = ? AND state = 'active' "
            "AND protected = 0 AND last_hit < ?",
            (group_id, now - FORGET_AFTER),
        )
        cnt = self._db.execute(
            "SELECT COUNT(*) n FROM memories WHERE group_id = ? AND state = 'active'",
            (group_id,),
        ).fetchone()["n"]
        if cnt > MAX_MEMORIES:
            self._db.execute(
                "UPDATE memories SET state = 'forgotten' WHERE group_id = ? AND state = 'active' "
                "AND protected = 0 AND id IN ("
                "SELECT id FROM memories WHERE group_id = ? AND state = 'active' AND protected = 0 "
                "ORDER BY weight ASC LIMIT ?"
                ")",
                (group_id, group_id, cnt - MAX_MEMORIES),
            )

    def _fts_hits(self, group_id: str, query: str, limit: int) -> set[int]:
        if not self._fts:
            return set()
        tokens = query_terms(query)[:24]
        if not tokens:
            return set()
        match = " OR ".join('"{}"'.format(t.replace('"', "")) for t in tokens)
        try:
            with self._lock:
                if self._closed:
                    return set()
                rows = self._db.execute(
                    "SELECT m.id AS rid FROM memories_fts f "
                    "JOIN memories m ON m.id = f.rowid "
                    "WHERE memories_fts MATCH ? AND m.group_id = ? AND m.state = 'active' LIMIT ?",
                    (match, str(group_id), max(1, int(limit) * 4)),
                ).fetchall()
            return {int(r["rid"]) for r in rows}
        except sqlite3.OperationalError:
            return set()

    def recall_memories(
        self,
        group_id: str,
        query: str,
        limit: int = 6,
        now: Optional[float] = None,
    ) -> list[str]:
        """双路混合召回：FTS5 + 中文二元组 + 衰减权重；非空查询零命中时不硬塞无关记忆。"""
        cur = time.time() if now is None else float(now)
        words = query_terms(query)
        has_query = bool(str(query or "").strip())
        recent_eps = self.recent_episodes(group_id, limit=15)
        if words:
            episodes = [t for t in recent_eps if any(w in t.lower() for w in words)][-2:]
        elif has_query:
            episodes = []
        else:
            episodes = recent_eps[-2:]

        fts_ids = self._fts_hits(group_id, query, limit) if has_query else set()
        with self._lock:
            if self._closed:
                return []
            rows = self._db.execute(
                "SELECT id, content, weight, last_hit, protected FROM memories "
                "WHERE group_id = ? AND state = 'active'",
                (str(group_id),),
            ).fetchall()

        scored: list[tuple[float, str, int, bool]] = []
        for row in rows:
            w = _decay(row["weight"], row["last_hit"], cur)
            if w < 0.15 and not row["protected"]:
                continue
            low = row["content"].lower()
            hit = sum(1 for tok in words if tok in low)
            fts = 1.0 if row["id"] in fts_ids else 0.0
            matched = bool(hit > 0 or fts > 0.0)
            if has_query and not matched:
                continue
            scored.append((w + hit * 0.6 + fts, row["content"], int(row["id"]), matched))
        scored.sort(key=lambda item: item[0], reverse=True)
        chosen = scored[:max(0, limit - len(episodes))]
        matched_ids = [item[2] for item in chosen if item[3]]
        if matched_ids:
            with self._lock:
                if not self._closed:
                    with self._db:
                        for rid in matched_ids:
                            self._db.execute(
                                "UPDATE memories SET last_hit = ?, weight = MIN(5.0, weight + 0.3) "
                                "WHERE id = ?",
                                (cur, rid),
                            )
        seen: set[str] = set()
        out: list[str] = []
        for item in episodes + [row[1] for row in chosen]:
            if item not in seen:
                seen.add(item)
                out.append(item)
        return out[:limit]

    def group_jargon(self, group_id: str, limit: int = 12) -> dict[str, str]:
        with self._lock:
            if self._closed:
                return {}
            rows = self._db.execute(
                "SELECT content FROM memories WHERE group_id = ? AND category = 'jargon' "
                "AND state = 'active' ORDER BY weight DESC, last_hit DESC LIMIT ?",
                (str(group_id), max(1, int(limit))),
            ).fetchall()
        out: dict[str, str] = {}
        for row in rows:
            raw = str(row["content"] or "")
            if "=" in raw:
                w, a = raw.split("=", 1)
                w, a = w.strip(), a.strip()
                if w and a:
                    out[w] = a
        return out

    # ---- 人物档案（按 QQ 号归一 + 别名解析） ----
    def observe_person(
        self,
        group_id: str,
        user_id: str,
        display_name: str,
        now: Optional[float] = None,
    ) -> None:
        uid = str(user_id or "").strip()
        if not uid or uid == "bot":
            return
        stamp = time.time() if now is None else float(now)
        raw_name = str(display_name or "").strip()[:40]
        with self._lock:
            if self._closed:
                return
            with self._db:
                if raw_name and raw_name != uid and raw_name != "群友":
                    self._db.execute(
                        "INSERT INTO person_aliases(group_id, user_id, name, kind, hits, updated_at) "
                        "VALUES (?, ?, ?, 'nick', 1, ?) "
                        "ON CONFLICT(group_id, user_id, name) DO UPDATE SET "
                        "hits = hits + 1, updated_at = excluded.updated_at",
                        (str(group_id), uid, raw_name, stamp),
                    )
                self._db.execute(
                    "INSERT INTO persons(group_id, user_id, msg_count, first_seen, last_seen, updated_at) "
                    "VALUES (?, ?, 1, ?, ?, ?) "
                    "ON CONFLICT(group_id, user_id) DO UPDATE SET "
                    "msg_count = msg_count + 1, last_seen = excluded.last_seen",
                    (str(group_id), uid, stamp, stamp, stamp),
                )

    def resolve_person(
        self,
        group_id: str,
        query_name_or_id: str,
        threshold: float = 0.7,
    ) -> Optional[str]:
        raw = str(query_name_or_id or "").strip()
        if not raw:
            return None
        cleaned = clean_speaker_name(raw)
        candidates = [c for c in dict.fromkeys((raw, cleaned)) if c]
        with self._lock:
            if self._closed:
                return None
            direct_uid = self._db.execute(
                "SELECT user_id FROM persons WHERE group_id = ? AND user_id = ? LIMIT 1",
                (str(group_id), raw),
            ).fetchone()
            if direct_uid:
                return str(direct_uid["user_id"])
            for cand in candidates:
                row = self._db.execute(
                    "SELECT user_id FROM person_aliases WHERE group_id = ? AND name = ? "
                    "ORDER BY hits DESC LIMIT 1",
                    (str(group_id), cand),
                ).fetchone()
                if row:
                    return str(row["user_id"])
            rows = self._db.execute(
                "SELECT user_id, name FROM person_aliases WHERE group_id = ?",
                (str(group_id),),
            ).fetchall()
        for row in rows:
            other_raw = str(row["name"] or "").strip()
            other_clean = clean_speaker_name(other_raw)
            if other_clean and other_clean in candidates:
                return str(row["user_id"])
        best_uid: Optional[str] = None
        best_score = 0.0
        for row in rows:
            other_raw = str(row["name"] or "").strip()
            variants = [v for v in dict.fromkeys((other_raw, clean_speaker_name(other_raw))) if v]
            for cand in candidates:
                for other in variants:
                    score = _similarity(cand, other)
                    if len(cand) >= 2 and len(other) >= 2 and (cand in other or other in cand):
                        overlap = min(len(cand), len(other)) / float(max(len(cand), len(other)))
                        if overlap >= 0.5:
                            score = max(score, 0.85)
                    if score > best_score:
                        best_uid, best_score = str(row["user_id"]), score
        return best_uid if best_score >= threshold else None

    def upsert_person(
        self,
        group_id: str,
        user_id: str,
        now: Optional[float] = None,
        merge: bool = True,
        **fields: Any,
    ) -> dict[str, Any]:
        uid = str(user_id or "").strip()
        if not uid:
            return {}
        stamp = time.time() if now is None else float(now)
        limits = (("traits", 600), ("preferences", 400), ("events", 600), ("relation", 200))
        with self._lock:
            if self._closed:
                return {}
            with self._db:
                row = self._db.execute(
                    "SELECT * FROM persons WHERE group_id = ? AND user_id = ?",
                    (str(group_id), uid),
                ).fetchone()
                current = dict(row) if row else {}
                merged: dict[str, Any] = {}
                for key, cap in limits:
                    prev_val = str(current.get(key) or "")
                    incoming = str(fields.get(key) or "").strip() if key in fields else ""
                    if incoming and incoming.lower() not in _PLACEHOLDER_VALUES:
                        merged[key] = _merge_field(prev_val, incoming, cap) if merge else incoming[:cap]
                    else:
                        merged[key] = _merge_field(prev_val, "", cap) if merge else prev_val
                if not current and "intimacy" not in fields and not any(merged[k] for k, _ in limits):
                    return {}
                intimacy = float(fields.get("intimacy", current.get("intimacy", 0.5)))
                merged["intimacy"] = max(0.0, min(1.0, intimacy))
                self._db.execute(
                    "INSERT INTO persons"
                    "(group_id, user_id, traits, preferences, events, relation, intimacy, "
                    "first_seen, last_seen, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(group_id, user_id) DO UPDATE SET "
                    "traits = excluded.traits, preferences = excluded.preferences, "
                    "events = excluded.events, relation = excluded.relation, "
                    "intimacy = excluded.intimacy, updated_at = excluded.updated_at",
                    (
                        str(group_id),
                        uid,
                        merged["traits"][:600],
                        merged["preferences"][:400],
                        merged["events"][:600],
                        merged["relation"][:200],
                        merged["intimacy"],
                        float(current.get("first_seen") or stamp),
                        float(current.get("last_seen") or stamp),
                        stamp,
                    ),
                )
        return merged

    def person_summary(self, group_id: str, user_id: str) -> str:
        uid = str(user_id or "").strip()
        if not uid:
            return ""
        with self._lock:
            if self._closed:
                return ""
            row = self._db.execute(
                "SELECT traits, preferences, events, relation FROM persons "
                "WHERE group_id = ? AND user_id = ?",
                (str(group_id), uid),
            ).fetchone()
        if not row:
            return ""
        bits = []
        for key, label in (("traits", "印象"), ("preferences", "偏好"), ("events", "相关的事"), ("relation", "关系")):
            val = _merge_field(str(row[key] or ""), "", 600)
            if val:
                bits.append("{}：{}".format(label, val))
        return "；".join(bits)

    def person_aliases(self, group_id: str, user_id: str) -> list[str]:
        with self._lock:
            if self._closed:
                return []
            rows = self._db.execute(
                "SELECT name FROM person_aliases WHERE group_id = ? AND user_id = ? ORDER BY hits DESC LIMIT 8",
                (str(group_id), str(user_id)),
            ).fetchall()
        return [str(r["name"]) for r in rows]

    # ---- 滚动会话状态压缩（MaiBot ConversationState 精简版） ----
    def get_conv_state(
        self,
        group_id: str,
        max_age_seconds: float = CONV_STATE_MAX_AGE,
        now: Optional[float] = None,
    ) -> dict[str, list[str]]:
        empty = {"topics": [], "progress": [], "pending": []}
        with self._lock:
            if self._closed:
                return empty
            row = self._db.execute(
                "SELECT topics_json, progress_json, pending_json, updated_at FROM conv_state WHERE group_id = ?",
                (str(group_id),),
            ).fetchone()
        if not row:
            return empty
        if max_age_seconds > 0:
            cur = time.time() if now is None else float(now)
            updated_at = float(row["updated_at"] or 0.0)
            if updated_at <= 0.0 or (cur - updated_at) > max_age_seconds:
                return empty
        out: dict[str, list[str]] = {}
        for key, col in (("topics", "topics_json"), ("progress", "progress_json"), ("pending", "pending_json")):
            try:
                vals = json.loads(row[col] or "[]")
            except (ValueError, TypeError):
                vals = []
            out[key] = [str(v).strip() for v in vals if str(v or "").strip()][:5] if isinstance(vals, list) else []
        return out

    def save_conv_state(
        self,
        group_id: str,
        state: dict[str, list[str]],
        now: Optional[float] = None,
    ) -> None:
        stamp = time.time() if now is None else float(now)
        clean = {
            k: [str(v).strip()[:80] for v in (state.get(k) or []) if str(v or "").strip()][:5]
            for k in ("topics", "progress", "pending")
        }
        with self._lock:
            if self._closed:
                return
            with self._db:
                self._db.execute(
                    "INSERT INTO conv_state(group_id, topics_json, progress_json, pending_json, updated_at) "
                    "VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(group_id) DO UPDATE SET topics_json = excluded.topics_json, "
                    "progress_json = excluded.progress_json, pending_json = excluded.pending_json, "
                    "updated_at = excluded.updated_at",
                    (
                        str(group_id),
                        json.dumps(clean["topics"], ensure_ascii=False),
                        json.dumps(clean["progress"], ensure_ascii=False),
                        json.dumps(clean["pending"], ensure_ascii=False),
                        stamp,
                    ),
                )

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
        with self._lock:
            if not self._closed:
                with self._db:
                    self._db.execute(sql, params)
                return True
        if self._path == ":memory:":
            return False
        # 插件重载关闭主连接瞬间后台任务刚落盘：临时开独立连接补写，确保不丢失成果。
        conn = _configure_conn(sqlite3.connect(self._path, timeout=30.0), self._path)
        try:
            with conn:
                conn.executescript(_PENDING_DELIVERY_DDL)
                conn.execute(sql, params)
            return True
        except sqlite3.Error:
            return False
        finally:
            try:
                conn.close()
            except sqlite3.Error:
                pass

    def list_pending_deliveries(self, limit: int = 20) -> list[PendingDelivery]:
        with self._lock:
            if self._closed:
                return []
            rows = self._db.execute(
                "SELECT * FROM pending_deliveries ORDER BY created_at ASC LIMIT ?",
                (max(1, int(limit)),),
            ).fetchall()
        return [self._row_to_delivery(r) for r in rows]

    def delete_pending_delivery(self, task_id: str) -> bool:
        if not task_id:
            return False
        with self._lock:
            if self._closed:
                return False
            with self._db:
                cur = self._db.execute(
                    "DELETE FROM pending_deliveries WHERE task_id = ?", (str(task_id),)
                )
                return (cur.rowcount or 0) > 0

    def pending_delivery_paths(self) -> set[str]:
        with self._lock:
            if self._closed:
                return set()
            rows = self._db.execute("SELECT files_json FROM pending_deliveries").fetchall()
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
            with self._lock:
                if self._closed:
                    return False
                with self._db:
                    self._db.execute(
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
        with self._lock:
            if self._closed:
                return 0
            row = self._db.execute(
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
        with self._lock:
            if self._closed:
                return {"total": 0, "errors": 0, "error_rate": 0.0, "avg_latency": 0.0, "p95_latency": 0.0, "tokens": 0}
            row = self._db.execute(
                "SELECT COUNT(*) AS total, SUM(CASE WHEN ok = 0 THEN 1 ELSE 0 END) AS errors, "
                "AVG(latency) AS avg_latency, SUM(tokens) AS tokens FROM monitor_events " + where,
                params,
            ).fetchone()
            latencies = [
                r["latency"]
                for r in self._db.execute(
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
        with self._lock:
            if self._closed:
                return 0
            with self._db:
                cur = self._db.execute("DELETE FROM monitor_events WHERE ts < ?", (cutoff,))
                return max(0, cur.rowcount or 0)

    def stats(self, group_id: str) -> dict[str, Any]:
        with self._lock:
            if self._closed:
                return {"fts": self._fts}
            gid = str(group_id)
            return {
                "messages": self._db.execute("SELECT COUNT(*) FROM messages WHERE group_id=?", (gid,)).fetchone()[0],
                "episodes": self._db.execute("SELECT COUNT(*) FROM episodes WHERE group_id=?", (gid,)).fetchone()[0],
                "memories": self._db.execute("SELECT COUNT(*) FROM memories WHERE group_id=? AND state='active'", (gid,)).fetchone()[0],
                "persons": self._db.execute("SELECT COUNT(*) FROM persons WHERE group_id=?", (gid,)).fetchone()[0],
                "fts": self._fts,
            }


def _has_symlink_component(start: Path, end: Path) -> bool:
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


def _purge_old_files(
    target_dir: Path,
    root_dir: Path,
    resolved_root: Path,
    cutoff: float,
    pattern: str = "*",
    recursive: bool = False,
) -> int:
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


def cleanup_runtime_caches(
    data_root: str | Path,
    now: Optional[float] = None,
    temp_max_age: int = 86400,
    media_max_age: int = 259200,
) -> int:
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
            qq_dir / "global" / "nt_data" / "Log", root, resolved_root, temp_cutoff, recursive=True
        )
        removed += _purge_old_files(
            qq_dir / "NapCat" / "temp", root, resolved_root, temp_cutoff, recursive=True
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

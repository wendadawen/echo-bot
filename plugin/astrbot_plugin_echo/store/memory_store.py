"""消息历史、情节、长期群记忆（FTS5 trigram + 中文二元组）、黑话与滚动会话状态。"""

from __future__ import annotations

import json
import math
import re
import sqlite3
import time
from typing import Any, Optional

DEFAULT_HISTORY_LIMIT = 30
DEFAULT_MAX_MESSAGES_PER_GROUP = 500
HALF_LIFE = 14 * 86400
FORGET_AFTER = 60 * 86400
MAX_EPISODES = 200
MAX_MEMORIES = 450
CONV_STATE_MAX_AGE = 7200.0

_STOP_BIGRAMS = frozenset({
    "什么", "怎么", "这个", "那个", "就是", "不是", "可以", "我们", "你们", "他们",
    "现在", "今天", "昨天", "明天", "时候", "东西", "地方", "觉得", "感觉", "知道",
    "告诉", "一下", "一点", "有没", "没有", "能不", "不能", "是不", "为什", "为啥",
    "多少", "哪些", "怎样", "如何", "刚才", "之前", "以前", "请问", "帮忙", "帮我",
    "这里", "那里", "这么", "那么", "意思", "聊过", "记得",
})


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


def _fts_trigram_terms(query: str) -> list[str]:
    """提取适合 FTS5 trigram 分词器的 3+ 字符中英文检索片段。"""
    raw = str(query or "").strip()
    if not raw:
        return []
    seen: set[str] = set()
    terms: list[str] = []

    def _add(tok: str) -> None:
        t = tok.strip().lower()
        if len(t) >= 3 and t not in seen:
            seen.add(t)
            terms.append(t)

    for w in re.findall(r"[a-zA-Z0-9]{3,}", raw):
        _add(w)
    for span in re.findall(r"[\u4e00-\u9fff]{3,}", raw):
        for i in range(len(span) - 2):
            _add(span[i:i + 3])
    return terms[:16]


def _decay(weight: float, last_hit: float, now: Optional[float] = None) -> float:
    cur = time.time() if now is None else float(now)
    elapsed = max(0.0, cur - float(last_hit or 0.0))
    return max(0.05, float(weight or 1.0) * math.pow(0.5, elapsed / HALF_LIFE))


class MemoryStoreOps:
    """Repository 的消息历史、长期记忆、黑话与滚动会话状态混入层。"""

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
        gid = str(group_id)
        mid = str(msg_id or "")
        role_s = str(role)
        with self.write_conn() as conn:
            if conn is None:
                return 0
            cur = conn.execute(
                "INSERT OR IGNORE INTO messages(group_id, user_id, user_name, role, text, ts, msg_id, meta) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    gid,
                    str(user_id or "unknown"),
                    str(user_name or ""),
                    role_s,
                    str(text),
                    stamp,
                    mid,
                    str(meta or "")[:120],
                ),
            )
            if cur.rowcount == 0 and role_s == "user" and mid:
                row = conn.execute(
                    "SELECT seq FROM messages WHERE group_id = ? AND msg_id = ? AND role = 'user' LIMIT 1",
                    (gid, mid),
                ).fetchone()
                return int(row["seq"] or 0) if row else 0
            return int(cur.lastrowid or 0)

    def recent_messages(self, group_id: str, limit: int = DEFAULT_HISTORY_LIMIT) -> list[dict[str, Any]]:
        with self.read_conn() as conn:
            if conn is None:
                return []
            rows = conn.execute(
                "SELECT * FROM messages WHERE group_id = ? ORDER BY seq DESC LIMIT ?",
                (str(group_id), max(0, int(limit))),
            ).fetchall()
        return [dict(r) for r in reversed(rows)]

    def messages_since(self, group_id: str, ts: float = 0.0, limit: int = 24) -> list[dict[str, Any]]:
        with self.read_conn() as conn:
            if conn is None:
                return []
            rows = conn.execute(
                "SELECT * FROM messages WHERE group_id = ? AND ts > ? ORDER BY seq DESC LIMIT ?",
                (str(group_id), float(ts or 0.0), max(1, int(limit))),
            ).fetchall()
        return [dict(r) for r in reversed(rows)]

    def prune_messages(self, keep_per_group: int = DEFAULT_MAX_MESSAGES_PER_GROUP) -> int:
        keep = max(50, int(keep_per_group or DEFAULT_MAX_MESSAGES_PER_GROUP))
        deleted = 0
        with self.write_conn() as conn:
            if conn is None:
                return 0
            groups = [r[0] for r in conn.execute("SELECT DISTINCT group_id FROM messages").fetchall()]
            for gid in groups:
                cur = conn.execute(
                    "DELETE FROM messages WHERE group_id = ? AND seq NOT IN ("
                    "SELECT seq FROM messages WHERE group_id = ? ORDER BY seq DESC LIMIT ?"
                    ")",
                    (gid, gid, keep),
                )
                deleted += max(0, cur.rowcount or 0)
        return deleted

    def last_episode_at(self, group_id: str) -> float:
        with self.read_conn() as conn:
            if conn is None:
                return 0.0
            row = conn.execute(
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
        with self.write_conn() as conn:
            if conn is None:
                return
            if external_id:
                conn.execute(
                    "INSERT OR IGNORE INTO episodes(group_id, text, created_at, source, external_id) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (str(group_id), clean[:160], stamp, source, str(external_id)),
                )
            else:
                conn.execute(
                    "INSERT INTO episodes(group_id, text, created_at, source) VALUES (?, ?, ?, ?)",
                    (str(group_id), clean[:160], stamp, source),
                )
            conn.execute(
                "DELETE FROM episodes WHERE group_id = ? AND id NOT IN "
                "(SELECT id FROM episodes WHERE group_id = ? ORDER BY id DESC LIMIT ?)",
                (str(group_id), str(group_id), MAX_EPISODES),
            )

    def recent_episodes(self, group_id: str, limit: int = 3) -> list[str]:
        with self.read_conn() as conn:
            if conn is None:
                return []
            rows = conn.execute(
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
        with self.write_conn() as conn:
            if conn is None:
                return
            row = conn.execute(
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
                conn.execute(
                    "UPDATE memories SET weight = ?, last_hit = ?, state = 'active', "
                    "category = ?, protected = ? WHERE id = ?",
                    (weight, stamp, cat, prot, row["id"]),
                )
            else:
                conn.execute(
                    "INSERT INTO memories(group_id, category, content, weight, last_hit, state, protected) "
                    "VALUES (?, ?, ?, ?, ?, 'active', ?)",
                    (str(group_id), cat, clean, 3.0 if protected else 1.0, stamp, 1 if protected else 0),
                )
            self._prune_memories_locked(conn, str(group_id), stamp)

    def _prune_memories_locked(self, conn: sqlite3.Connection, group_id: str, now: float) -> None:
        conn.execute(
            "UPDATE memories SET state = 'forgotten' WHERE group_id = ? AND state = 'active' "
            "AND protected = 0 AND last_hit < ?",
            (group_id, now - FORGET_AFTER),
        )
        cnt = conn.execute(
            "SELECT COUNT(*) n FROM memories WHERE group_id = ? AND state = 'active'",
            (group_id,),
        ).fetchone()["n"]
        if cnt > MAX_MEMORIES:
            conn.execute(
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
        tokens = _fts_trigram_terms(query) if self._fts_trigram else query_terms(query)[:24]
        if not tokens:
            return set()
        match = " OR ".join('"{}"'.format(t.replace('"', '""')) for t in tokens)
        try:
            with self.read_conn() as conn:
                if conn is None:
                    return set()
                rows = conn.execute(
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
        """双路混合只读召回：FTS5(trigram) + 中文二元组 + 衰减权重；零写放大。"""
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
        with self.read_conn() as conn:
            if conn is None:
                return []
            rows = conn.execute(
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
        seen: set[str] = set()
        out: list[str] = []
        for item in episodes + [row[1] for row in chosen]:
            if item not in seen:
                seen.add(item)
                out.append(item)
        return out[:limit]

    def group_jargon(self, group_id: str, limit: int = 12) -> dict[str, str]:
        with self.read_conn() as conn:
            if conn is None:
                return {}
            rows = conn.execute(
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

    def get_conv_state(
        self,
        group_id: str,
        max_age_seconds: float = CONV_STATE_MAX_AGE,
        now: Optional[float] = None,
    ) -> dict[str, list[str]]:
        empty = {"topics": [], "progress": [], "pending": []}
        with self.read_conn() as conn:
            if conn is None:
                return empty
            row = conn.execute(
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
        with self.write_conn() as conn:
            if conn is None:
                return
            conn.execute(
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

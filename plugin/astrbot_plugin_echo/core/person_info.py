"""人物信息三层：别名（identity）、个体（individual）、画像（profile）。

对齐 MaiBot `src/person_info/{identity,individual,person_info}.py` 的分层：
- identity：QQ 号与各种叫法（昵称、群名片、别名）的对应关系
- individual：这个人的活跃度与出现过的群
- profile：对该群而言的印象、偏好、事件与关系
只用标准库与 SQLite。
"""

import sqlite3
import threading
import time

from .messages import clean_speaker_name


def levenshtein(left, right):
    left, right = str(left or ""), str(right or "")
    if left == right:
        return 0
    if not left:
        return len(right)
    if not right:
        return len(left)
    previous = list(range(len(right) + 1))
    for i, char in enumerate(left, 1):
        current = [i]
        for j, other in enumerate(right, 1):
            cost = 0 if char == other else 1
            current.append(min(previous[j] + 1, current[j - 1] + 1,
                               previous[j - 1] + cost))
        previous = current
    return previous[-1]


def similarity(left, right):
    left, right = str(left or ""), str(right or "")
    if not left or not right:
        return 0.0
    longest = max(len(left), len(right))
    return 1.0 - levenshtein(left, right) / float(longest)


def _merge_field(existing, incoming, limit):
    left = str(existing or "").strip()
    right = str(incoming or "").strip()
    if not right:
        return left[:limit]
    if not left:
        return right[:limit]
    parts = [p.strip() for p in left.split("；") if p.strip()]
    incoming_parts = [p.strip() for p in right.split("；") if p.strip()]
    for item in incoming_parts:
        if any(item == p or item in p for p in parts):
            continue
        parts = [p for p in parts if p not in item]
        parts.append(item)
    joined = "；".join(parts)
    while len(joined) > limit and len(parts) > 1:
        parts.pop(0)
        joined = "；".join(parts)
    return joined[-limit:] if len(joined) > limit else joined


class PersonStore:
    """按群隔离的人物档案；写操作加锁，可多线程调用。"""

    def __init__(self, path=":memory:"):
        self._lock = threading.RLock()
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
                CREATE TABLE IF NOT EXISTS identities (
                    group_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    kind TEXT NOT NULL DEFAULT 'nick',
                    hits INTEGER NOT NULL DEFAULT 1,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (group_id, user_id, name)
                );
                CREATE INDEX IF NOT EXISTS identities_name
                    ON identities(group_id, name);
                CREATE TABLE IF NOT EXISTS individuals (
                    user_id TEXT PRIMARY KEY,
                    first_seen REAL NOT NULL,
                    last_seen REAL NOT NULL,
                    msg_count INTEGER NOT NULL DEFAULT 1,
                    groups TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS profiles (
                    group_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    traits TEXT NOT NULL DEFAULT '',
                    preferences TEXT NOT NULL DEFAULT '',
                    events TEXT NOT NULL DEFAULT '',
                    relation TEXT NOT NULL DEFAULT '',
                    intimacy REAL NOT NULL DEFAULT 0.5,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (group_id, user_id)
                );
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

    # ---- identity ----
    def observe(self, group_id, user_id, name, card="", now=None):
        user_id = str(user_id or "").strip()
        if not user_id:
            return
        now = time.time() if now is None else now
        with self._lock:
            if self._closed:
                return
            with self._db:
                for value, kind in ((name, "nick"), (card, "card")):
                    value = str(value or "").strip()[:40]
                    if not value:
                        continue
                    self._db.execute(
                        "INSERT INTO identities(group_id, user_id, name, kind, hits, updated_at) "
                        "VALUES (?, ?, ?, ?, 1, ?) "
                        "ON CONFLICT(group_id, user_id, name) DO UPDATE SET "
                        "hits = hits + 1, updated_at = excluded.updated_at",
                        (group_id, user_id, value, kind, now),
                    )
                row = self._db.execute(
                    "SELECT groups, msg_count FROM individuals WHERE user_id = ?", (user_id,)
                ).fetchone()
                if row is None:
                    self._db.execute(
                        "INSERT INTO individuals(user_id, first_seen, last_seen, msg_count, groups) "
                        "VALUES (?, ?, ?, 1, ?)",
                        (user_id, now, now, group_id),
                    )
                else:
                    groups = row["groups"].split(",") if row["groups"] else []
                    if group_id not in groups:
                        groups.append(group_id)
                    self._db.execute(
                        "UPDATE individuals SET last_seen = ?, msg_count = msg_count + 1, "
                        "groups = ? WHERE user_id = ?",
                        (now, ",".join(groups), user_id),
                    )

    def resolve(self, group_id, name, threshold=0.7):
        """把叫法解析成 user_id：同时尝试原始与清洗后昵称，精确命中优先，其次包含与编辑距离。"""
        raw_name = str(name or "").strip()
        if not raw_name:
            return None
        cleaned_name = clean_speaker_name(raw_name)
        candidates = [c for c in dict.fromkeys((raw_name, cleaned_name)) if c]
        with self._lock:
            if self._closed:
                return None
            for cand in candidates:
                row = self._db.execute(
                    "SELECT user_id FROM identities WHERE group_id = ? AND name = ? "
                    "ORDER BY hits DESC LIMIT 1",
                    (group_id, cand),
                ).fetchone()
                if row:
                    return row["user_id"]
            rows = self._db.execute(
                "SELECT user_id, name FROM identities WHERE group_id = ?", (group_id,)
            ).fetchall()
        for row in rows:
            other_raw = str(row["name"] or "").strip()
            other_clean = clean_speaker_name(other_raw)
            if other_clean and other_clean in candidates:
                return row["user_id"]
        best, best_score = None, 0.0
        for row in rows:
            other_raw = str(row["name"] or "").strip()
            other_variants = [v for v in dict.fromkeys((other_raw, clean_speaker_name(other_raw))) if v]
            if not other_variants:
                continue
            for cand in candidates:
                for other in other_variants:
                    score = similarity(cand, other)
                    if len(cand) >= 2 and len(other) >= 2 and (cand in other or other in cand):
                        overlap = min(len(cand), len(other)) / float(max(len(cand), len(other)))
                        if overlap >= 0.5:
                            score = max(score, 0.85)
                    if score > best_score:
                        best, best_score = row["user_id"], score
        return best if best_score >= threshold else None

    def aliases(self, group_id, user_id):
        with self._lock:
            if self._closed:
                return []
            rows = self._db.execute(
                "SELECT name, kind, hits FROM identities WHERE group_id = ? AND user_id = ? "
                "ORDER BY hits DESC LIMIT 8",
                (group_id, user_id),
            ).fetchall()
        return [dict(row) for row in rows]

    def individual(self, user_id):
        with self._lock:
            if self._closed:
                return {}
            row = self._db.execute(
                "SELECT * FROM individuals WHERE user_id = ?", (str(user_id),)
            ).fetchone()
        return dict(row) if row else {}

    # ---- profile ----
    def profile(self, group_id, user_id):
        with self._lock:
            if self._closed:
                return {}
            row = self._db.execute(
                "SELECT * FROM profiles WHERE group_id = ? AND user_id = ?",
                (group_id, str(user_id)),
            ).fetchone()
        return dict(row) if row else {}

    def upsert_profile(self, group_id, user_id, now=None, merge=True, **fields):
        user_id = str(user_id or "").strip()
        if not user_id:
            return {}
        now = time.time() if now is None else now
        limits = (("traits", 600), ("preferences", 400), ("events", 600), ("relation", 200))
        with self._lock:
            if self._closed:
                return {}
            with self._db:
                current = self.profile(group_id, user_id)
                merged = {}
                for key, cap in limits:
                    prev_val = current.get(key, "")
                    if key in fields and str(fields[key] or "").strip():
                        incoming = str(fields[key] or "").strip()
                        merged[key] = _merge_field(prev_val, incoming, cap) if merge else incoming[:cap]
                    else:
                        merged[key] = prev_val
                if not current and "intimacy" not in fields and not any(merged[k] for k, _ in limits):
                    return {}
                merged["intimacy"] = float(fields.get("intimacy", current.get("intimacy", 0.5)))
                self._db.execute(
                    "INSERT INTO profiles(group_id, user_id, traits, preferences, events, "
                    "relation, intimacy, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(group_id, user_id) DO UPDATE SET "
                    "traits = excluded.traits, preferences = excluded.preferences, "
                    "events = excluded.events, relation = excluded.relation, "
                    "intimacy = excluded.intimacy, updated_at = excluded.updated_at",
                    (group_id, user_id, merged["traits"][:600], merged["preferences"][:400],
                     merged["events"][:600], merged["relation"][:200],
                     max(0.0, min(1.0, merged["intimacy"])), now),
                )
        return merged

    def adjust_intimacy(self, group_id, user_id, delta):
        with self._lock:
            if self._closed:
                return {}
            current = self.profile(group_id, user_id)
            base = float(current.get("intimacy", 0.5))
            return self.upsert_profile(group_id, user_id, intimacy=base + float(delta))

    def merge(self, old_id, new_id):
        """同一个人的两个号合并：别名、画像与跨群记录都迁移合并到新号上。"""
        old_id, new_id = str(old_id), str(new_id)
        if not old_id or not new_id or old_id == new_id:
            return 0
        moved = 0
        with self._lock:
            if self._closed:
                return 0
            with self._db:
                rows = self._db.execute(
                    "SELECT group_id, name, kind, hits FROM identities WHERE user_id = ?",
                    (old_id,),
                ).fetchall()
                for row in rows:
                    self._db.execute(
                        "INSERT INTO identities(group_id, user_id, name, kind, hits, updated_at) "
                        "VALUES (?, ?, ?, ?, ?, ?) "
                        "ON CONFLICT(group_id, user_id, name) DO UPDATE SET hits = hits + excluded.hits",
                        (row["group_id"], new_id, row["name"], row["kind"], row["hits"],
                         time.time()),
                    )
                    moved += 1

                old_profiles = self._db.execute(
                    "SELECT group_id, traits, preferences, events, relation, intimacy, updated_at "
                    "FROM profiles WHERE user_id = ?",
                    (old_id,),
                ).fetchall()
                limits = (("traits", 600), ("preferences", 400), ("events", 600), ("relation", 200))
                for op in old_profiles:
                    np = self._db.execute(
                        "SELECT traits, preferences, events, relation, intimacy, updated_at "
                        "FROM profiles WHERE group_id = ? AND user_id = ?",
                        (op["group_id"], new_id),
                    ).fetchone()
                    if np is None:
                        self._db.execute(
                            "INSERT INTO profiles(group_id, user_id, traits, preferences, events, "
                            "relation, intimacy, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                            (op["group_id"], new_id, op["traits"][:600], op["preferences"][:400],
                             op["events"][:600], op["relation"][:200], op["intimacy"],
                             op["updated_at"]),
                        )
                    else:
                        merged = {
                            field: _merge_field(np[field], op[field], cap)
                            for field, cap in limits
                        }
                        self._db.execute(
                            "UPDATE profiles SET traits = ?, preferences = ?, events = ?, "
                            "relation = ?, intimacy = ?, updated_at = ? "
                            "WHERE group_id = ? AND user_id = ?",
                            (merged["traits"], merged["preferences"], merged["events"],
                             merged["relation"], max(float(np["intimacy"]), float(op["intimacy"])),
                             max(float(np["updated_at"]), float(op["updated_at"])),
                             op["group_id"], new_id),
                        )

                old_ind = self._db.execute(
                    "SELECT first_seen, last_seen, msg_count, groups FROM individuals WHERE user_id = ?",
                    (old_id,),
                ).fetchone()
                if old_ind is not None:
                    new_ind = self._db.execute(
                        "SELECT first_seen, last_seen, msg_count, groups FROM individuals WHERE user_id = ?",
                        (new_id,),
                    ).fetchone()
                    if new_ind is None:
                        self._db.execute(
                            "INSERT INTO individuals(user_id, first_seen, last_seen, msg_count, groups) "
                            "VALUES (?, ?, ?, ?, ?)",
                            (new_id, old_ind["first_seen"], old_ind["last_seen"],
                             old_ind["msg_count"], old_ind["groups"]),
                        )
                    else:
                        old_groups = [g for g in (old_ind["groups"] or "").split(",") if g]
                        new_groups = [g for g in (new_ind["groups"] or "").split(",") if g]
                        groups = list(dict.fromkeys(new_groups + old_groups))
                        self._db.execute(
                            "UPDATE individuals SET first_seen = ?, last_seen = ?, "
                            "msg_count = ?, groups = ? WHERE user_id = ?",
                            (min(old_ind["first_seen"], new_ind["first_seen"]),
                             max(old_ind["last_seen"], new_ind["last_seen"]),
                             old_ind["msg_count"] + new_ind["msg_count"],
                             ",".join(groups), new_id),
                        )
                    self._db.execute("DELETE FROM individuals WHERE user_id = ?", (old_id,))

                self._db.execute("DELETE FROM identities WHERE user_id = ?", (old_id,))
                self._db.execute("DELETE FROM profiles WHERE user_id = ?", (old_id,))
        return moved

    def summary(self, group_id, user_id):
        """给上下文用的一行画像；没有档案时返回空串。"""
        row = self.profile(group_id, user_id)
        if not row:
            return ""
        bits = []
        for key, label in (("traits", "印象"), ("preferences", "偏好"),
                           ("events", "相关的事"), ("relation", "关系")):
            value = str(row.get(key) or "").strip()
            if value:
                bits.append("{}：{}".format(label, value))
        return "；".join(bits)

    def stats(self, group_id):
        with self._lock:
            if self._closed:
                return {"people": 0, "profiles": 0}
            people = self._db.execute(
                "SELECT COUNT(DISTINCT user_id) FROM identities WHERE group_id = ?",
                (group_id,),
            ).fetchone()[0]
            profiles = self._db.execute(
                "SELECT COUNT(*) FROM profiles WHERE group_id = ?", (group_id,)
            ).fetchone()[0]
        return {"people": people, "profiles": profiles}

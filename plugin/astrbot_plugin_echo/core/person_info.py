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


class PersonStore:
    """按群隔离的人物档案；写操作加锁，可多线程调用。"""

    def __init__(self, path=":memory:"):
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
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
        self._db.commit()

    # ---- identity ----
    def observe(self, group_id, user_id, name, card="", now=None):
        user_id = str(user_id or "").strip()
        if not user_id:
            return
        now = time.time() if now is None else now
        with self._lock:
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
            self._db.commit()

    def resolve(self, group_id, name, threshold=0.7):
        """把叫法解析成 user_id：精确命中优先，其次包含，最后编辑距离。"""
        name = str(name or "").strip()
        if not name:
            return None
        with self._lock:
            row = self._db.execute(
                "SELECT user_id FROM identities WHERE group_id = ? AND name = ? "
                "ORDER BY hits DESC LIMIT 1",
                (group_id, name),
            ).fetchone()
            if row:
                return row["user_id"]
            rows = self._db.execute(
                "SELECT user_id, name FROM identities WHERE group_id = ?", (group_id,)
            ).fetchall()
        best, best_score = None, 0.0
        for row in rows:
            other = row["name"]
            score = similarity(name, other)
            if name in other or other in name:
                score = max(score, 0.85)
            if score > best_score:
                best, best_score = row["user_id"], score
        return best if best_score >= threshold else None

    def aliases(self, group_id, user_id):
        with self._lock:
            rows = self._db.execute(
                "SELECT name, kind, hits FROM identities WHERE group_id = ? AND user_id = ? "
                "ORDER BY hits DESC LIMIT 8",
                (group_id, user_id),
            ).fetchall()
        return [dict(row) for row in rows]

    def individual(self, user_id):
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM individuals WHERE user_id = ?", (str(user_id),)
            ).fetchone()
        return dict(row) if row else {}

    # ---- profile ----
    def profile(self, group_id, user_id):
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM profiles WHERE group_id = ? AND user_id = ?",
                (group_id, str(user_id)),
            ).fetchone()
        return dict(row) if row else {}

    def upsert_profile(self, group_id, user_id, now=None, **fields):
        user_id = str(user_id or "").strip()
        if not user_id:
            return {}
        now = time.time() if now is None else now
        current = self.profile(group_id, user_id)
        merged = {
            "traits": fields.get("traits", current.get("traits", "")),
            "preferences": fields.get("preferences", current.get("preferences", "")),
            "events": fields.get("events", current.get("events", "")),
            "relation": fields.get("relation", current.get("relation", "")),
            "intimacy": float(fields.get("intimacy", current.get("intimacy", 0.5))),
        }
        with self._lock:
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
            self._db.commit()
        return merged

    def adjust_intimacy(self, group_id, user_id, delta):
        current = self.profile(group_id, user_id)
        base = float(current.get("intimacy", 0.5))
        return self.upsert_profile(group_id, user_id, intimacy=base + float(delta))

    def merge(self, old_id, new_id):
        """同一个人的两个号合并：别名与画像都挪到新号上。"""
        old_id, new_id = str(old_id), str(new_id)
        if not old_id or not new_id or old_id == new_id:
            return 0
        moved = 0
        with self._lock:
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
            self._db.execute("DELETE FROM identities WHERE user_id = ?", (old_id,))
            self._db.execute("DELETE FROM profiles WHERE user_id = ?", (old_id,))
            self._db.commit()
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
            people = self._db.execute(
                "SELECT COUNT(DISTINCT user_id) FROM identities WHERE group_id = ?",
                (group_id,),
            ).fetchone()[0]
            profiles = self._db.execute(
                "SELECT COUNT(*) FROM profiles WHERE group_id = ?", (group_id,)
            ).fetchone()[0]
        return {"people": people, "profiles": profiles}

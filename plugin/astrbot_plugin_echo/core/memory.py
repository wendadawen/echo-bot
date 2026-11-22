"""记忆层（完整版）：Episode、人物画像、关系图、记忆点权重、遗忘维护、
混合检索、分层上下文、回复效果反馈。

对齐 MaiBot `A_memorix` 的概念（Episode / 人物画像 / 双路召回 / external_id 幂等 /
maintain_memory 的 reinforce/protect/restore/freeze/recycle_bin / 遗忘标记而非删除 /
再激活加权 / 保护阈值），但用 SQLite 标准库 + 现有模型做抽取与压缩，
不引向量库、图数据库或 embedding 服务。
"""

import json
import math
import re
import sqlite3
import threading
import time

MEMORY_PROMPT = (
    "把下面这段群聊压成记忆，只输出一行 JSON，不要解释：\n"
    '{"episode":"一句话概括这段在聊什么",'
    '"persons":[{"name":"谁","category":"person|preference|event|relationship","content":"一条短事实"}],'
    '"relations":[{"a":"甲","b":"乙","relation":"关系"}],'
    '"points":[{"category":"person|preference|event|relationship|jargon","content":"一条短事实"}]}\n'
    "注意：[昵称：...] 里的整段文字只是 QQ 昵称/群名片（可能带空格、签名、吐槽或完整句子），"
    "绝不是发言内容；严禁从昵称中提取事件、观点、黑话或人物画像，只根据方括号后面的实际发言提炼。\n"
    '没有值得记的就输出 {"episode":"","points":[]}。每条不超过 40 字，各数组最多 3 条。'
)

HALF_LIFE = 14 * 86400
FORGET_AFTER = 60 * 86400
MAX_EPISODES = 200
MAX_POINTS = 400


def parse_memory(raw):
    """兼容旧接口：只返回 (episode, points)。"""
    full = parse_memory_full(raw)
    if full is None:
        return None
    return full[0], full[1]


def parse_memory_full(raw):
    """完整解析：episode / points / persons / relations。"""
    if not isinstance(raw, str):
        return None
    cleaned = re.sub(r"```(?:json)?", "", raw.strip()).strip()
    match = re.search(r"\{.*\}", cleaned, re.S)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    episode = str(data.get("episode") or "").strip()[:120]
    points = []
    for item in data.get("points") or []:
        if not isinstance(item, dict):
            continue
        content = str(item.get("content") or "").strip()[:60]
        if content:
            points.append((str(item.get("category") or "event")[:20], content))
    persons = []
    for item in data.get("persons") or []:
        if isinstance(item, dict) and item.get("name") and item.get("content"):
            persons.append((str(item["name"])[:30], str(item.get("category") or "person")[:20],
                            str(item["content"])[:60]))
    relations = []
    for item in data.get("relations") or []:
        if isinstance(item, dict) and item.get("a") and item.get("b"):
            relations.append((str(item["a"])[:30], str(item["b"])[:30],
                              str(item.get("relation") or "相关")[:40]))
    return episode, points[:3], persons[:3], relations[:3]


def decay(weight, last_hit, now=None, half_life=HALF_LIFE):
    now = time.time() if now is None else now
    elapsed = max(0.0, now - last_hit)
    return max(0.05, weight * math.pow(0.5, elapsed / half_life))


def maintain(action, weight):
    if action == "reinforce":
        return min(5.0, weight + 0.6)
    if action == "protect":
        return max(weight, 3.0)
    if action == "restore":
        return max(weight, 1.0)
    if action == "freeze":
        return weight
    if action == "recycle_bin":
        return 0.0
    return weight


class Memory:
    """每个群严格隔离；所有状态落在同一个 SQLite 文件里。"""

    def __init__(self, path=":memory:"):
        self._lock = threading.RLock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._fts = False
        with self._lock:
            self._db.executescript(
                """
                CREATE TABLE IF NOT EXISTS episodes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_id TEXT NOT NULL, text TEXT NOT NULL, created_at REAL NOT NULL,
                    source TEXT NOT NULL DEFAULT '', external_id TEXT NOT NULL DEFAULT '',
                    state TEXT NOT NULL DEFAULT 'ready'
                );
                CREATE TABLE IF NOT EXISTS memory_points (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_id TEXT NOT NULL, category TEXT NOT NULL,
                    content TEXT NOT NULL, weight REAL NOT NULL DEFAULT 1.0,
                    last_hit REAL NOT NULL, state TEXT NOT NULL DEFAULT 'active',
                    protected INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS person_profiles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_id TEXT NOT NULL, user_name TEXT NOT NULL,
                    category TEXT NOT NULL, content TEXT NOT NULL,
                    weight REAL NOT NULL DEFAULT 1.0, last_hit REAL NOT NULL,
                    source TEXT NOT NULL DEFAULT 'auto'
                );
                CREATE TABLE IF NOT EXISTS relations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_id TEXT NOT NULL, a TEXT NOT NULL, b TEXT NOT NULL,
                    relation TEXT NOT NULL, weight REAL NOT NULL DEFAULT 1.0,
                    valid_from REAL NOT NULL, valid_to REAL
                );
                CREATE TABLE IF NOT EXISTS jargon (
                    group_id TEXT NOT NULL, word TEXT NOT NULL, alias TEXT NOT NULL,
                    PRIMARY KEY (group_id, word)
                );
                CREATE TABLE IF NOT EXISTS reply_effects (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_id TEXT NOT NULL, reply TEXT NOT NULL,
                    followed_up INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL
                );
                """
            )
            # 兼容旧库：早期版本没有 source / external_id / state 等列。
            self._migrate()
            self._db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS episodes_external "
                "ON episodes(group_id, external_id) WHERE external_id <> ''"
            )
            self._db.execute(
                "CREATE INDEX IF NOT EXISTS points_lookup ON memory_points(group_id, content)"
            )
            self._db.commit()
            try:
                self._db.execute(
                    "CREATE VIRTUAL TABLE IF NOT EXISTS points_fts USING fts5(content, content='memory_points', content_rowid='id')"
                )
                self._db.execute(
                    "CREATE TRIGGER IF NOT EXISTS points_ai AFTER INSERT ON memory_points BEGIN "
                    "INSERT INTO points_fts(rowid, content) VALUES (new.id, new.content); END"
                )
                self._db.execute(
                    "CREATE TRIGGER IF NOT EXISTS points_ad AFTER DELETE ON memory_points BEGIN "
                    "INSERT INTO points_fts(points_fts, rowid, content) VALUES ('delete', old.id, old.content); END"
                )
                self._db.execute(
                    "CREATE TRIGGER IF NOT EXISTS points_au AFTER UPDATE ON memory_points BEGIN "
                    "INSERT INTO points_fts(points_fts, rowid, content) VALUES ('delete', old.id, old.content); "
                    "INSERT INTO points_fts(rowid, content) VALUES (new.id, new.content); END"
                )
                self._db.execute("INSERT INTO points_fts(points_fts) VALUES ('rebuild')")
                self._fts = True
            except sqlite3.OperationalError:
                self._fts = False
            self._db.commit()

    def _migrate(self):
        """给旧库补列；SQLite 的 CREATE TABLE IF NOT EXISTS 不会新增字段。"""
        wanted = {
            "episodes": (("source", "TEXT NOT NULL DEFAULT ''"),
                         ("external_id", "TEXT NOT NULL DEFAULT ''"),
                         ("state", "TEXT NOT NULL DEFAULT 'ready'")),
            "memory_points": (("state", "TEXT NOT NULL DEFAULT 'active'"),
                              ("protected", "INTEGER NOT NULL DEFAULT 0")),
            "person_profiles": (("source", "TEXT NOT NULL DEFAULT 'auto'"),),
        }
        for table, columns in wanted.items():
            existing = {row["name"] for row in
                        self._db.execute("PRAGMA table_info({})".format(table))}
            for name, ddl in columns:
                if name not in existing:
                    self._db.execute("ALTER TABLE {} ADD COLUMN {} {}".format(
                        table, name, ddl))

    # ---- Episode ----
    def last_episode_at(self, group_id):
        """最近一条情节的时间，用来判断摘要间隔，重启后仍然有效。"""
        with self._lock:
            row = self._db.execute(
                "SELECT MAX(created_at) FROM episodes WHERE group_id = ?",
                (str(group_id),),
            ).fetchone()
        return float(row[0] or 0.0)

    def add_episode(self, group_id, text, now=None, source="", external_id=""):
        if not text:
            return
        now = time.time() if now is None else now
        with self._lock:
            if external_id:
                self._db.execute(
                    "INSERT OR IGNORE INTO episodes(group_id, text, created_at, source, external_id) "
                    "VALUES (?,?,?,?,?)",
                    (group_id, text, now, source, external_id),
                )
            else:
                self._db.execute(
                    "INSERT INTO episodes(group_id, text, created_at, source) VALUES (?,?,?,?)",
                    (group_id, text, now, source),
                )
            self._db.commit()

    def recent_episodes(self, group_id, limit=3):
        with self._lock:
            rows = self._db.execute(
                "SELECT text FROM episodes WHERE group_id=? AND state='ready' "
                "ORDER BY id DESC LIMIT ?", (group_id, limit),
            ).fetchall()
        return [r["text"] for r in reversed(rows)]

    def pending_episodes(self, group_id, limit=10):
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM episodes WHERE group_id=? AND state='pending' "
                "ORDER BY id LIMIT ?", (group_id, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def rebuild_episodes(self, group_id, source, texts, now=None):
        """按 source 重建：替换掉同源的旧 Episode，写入新的一批。"""
        now = time.time() if now is None else now
        with self._lock:
            self._db.execute(
                "DELETE FROM episodes WHERE group_id=? AND source=?",
                (group_id, source),
            )
            for index, text in enumerate(texts or []):
                external = "{}:{:.0f}:{}".format(source, now * 1000, index)
                self._db.execute(
                    "INSERT OR IGNORE INTO episodes(group_id, text, created_at, source, external_id) "
                    "VALUES (?,?,?,?,?)",
                    (group_id, text, now, source, external),
                )
            self._db.commit()

    # ---- 记忆点 ----
    def add_point(self, group_id, category, content, now=None):
        now = time.time() if now is None else now
        with self._lock:
            row = self._db.execute(
                "SELECT id, weight, state FROM memory_points WHERE group_id=? AND content=?",
                (group_id, content),
            ).fetchone()
            if row:
                weight = maintain("reinforce", row["weight"])
                # 遗忘后被再次提起：恢复并额外加权，模拟「又想起来了」。
                if row["state"] in ("forgotten", "frozen"):
                    weight = maintain("restore", weight) + 0.3
                self._db.execute(
                    "UPDATE memory_points SET weight=?, last_hit=?, state='active', category=? WHERE id=?",
                    (weight, now, category, row["id"]),
                )
            else:
                self._db.execute(
                    "INSERT INTO memory_points(group_id, category, content, weight, last_hit) "
                    "VALUES (?,?,?,?,?)",
                    (group_id, category, content, 1.0, now),
                )
            self._db.commit()

    def maintain_point(self, group_id, point_id, action):
        with self._lock:
            row = self._db.execute(
                "SELECT weight, protected FROM memory_points WHERE id=? AND group_id=?",
                (point_id, group_id),
            ).fetchone()
            if not row:
                return
            weight = maintain(action, row["weight"])
            state = "forgotten" if action == "recycle_bin" else (
                "frozen" if action == "freeze" else "active")
            if action == "protect":
                protected = 1
            elif action == "recycle_bin":
                protected = 0
            else:
                protected = row["protected"]
            self._db.execute(
                "UPDATE memory_points SET weight=?, state=?, protected=? WHERE id=?",
                (weight, state, protected, point_id),
            )
            self._db.commit()

    def _fts_hits(self, group_id, query, limit):
        tokens = [t for t in re.findall(r"[\u4e00-\u9fff]{2,}|[a-zA-Z0-9]{3,}", query or "")][:6]
        if not tokens:
            return []
        match = " OR ".join('"{}"'.format(t.replace('"', "")) for t in tokens)
        try:
            with self._lock:
                rows = self._db.execute(
                    "SELECT rowid FROM points_fts WHERE points_fts MATCH ? LIMIT ?",
                    (match, limit * 4),
                ).fetchall()
            return [r["rowid"] for r in rows]
        except sqlite3.OperationalError:
            return []

    def recall(self, group_id, query, limit=4, now=None):
        """混合检索：FTS5 命中 + 关键词命中 + 时间衰减权重；命中即再激活。"""
        now = time.time() if now is None else now
        words = [
            w.lower()
            for w in re.findall(r"[\u4e00-\u9fff]{2,}|[a-zA-Z0-9]{3,}", query or "")
        ][:6]
        # 情节也要参与召回；只查记忆点会漏掉整段对话。
        episodes = [
            text for text in self.recent_episodes(group_id, limit=10)
            if not words or any(word in text.lower() for word in words)
        ][-2:]
        fts_ids = set(self._fts_hits(group_id, query, limit))
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM memory_points WHERE group_id=? AND state='active'",
                (group_id,),
            ).fetchall()
        scored = []
        for row in rows:
            weight = decay(row["weight"], row["last_hit"], now)
            if weight < 0.15 and not row["protected"]:
                continue
            content_lower = row["content"].lower()
            hit = sum(1 for w in words if w in content_lower)
            fts = 1.0 if row["id"] in fts_ids else 0.0
            scored.append((weight + hit * 0.5 + fts, row["content"], row["id"], row["weight"]))
        scored.sort(key=lambda item: item[0], reverse=True)
        chosen = scored[:max(0, limit - len(episodes))]
        if chosen:
            with self._lock:
                for _, _, rid, orig_weight in chosen:
                    self._db.execute(
                        "UPDATE memory_points SET last_hit=?, weight=? WHERE id=?",
                        (now, min(5.0, float(orig_weight) + 0.3), rid),
                    )
                self._db.commit()
        return (episodes + [content for _, content, _, _ in chosen])[:limit]

    # ---- 人物画像与关系 ----
    def upsert_person(self, group_id, user_name, category, content, now=None, source="auto"):
        now = time.time() if now is None else now
        with self._lock:
            row = self._db.execute(
                "SELECT id, weight FROM person_profiles WHERE group_id=? AND user_name=? "
                "AND category=? AND content=?",
                (group_id, user_name, category, content),
            ).fetchone()
            if row:
                self._db.execute(
                    "UPDATE person_profiles SET weight=?, last_hit=?, source=? WHERE id=?",
                    (maintain("reinforce", row["weight"]), now, source, row["id"]),
                )
            else:
                self._db.execute(
                    "INSERT INTO person_profiles(group_id, user_name, category, content, weight, last_hit, source) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (group_id, user_name, category, content, 1.0, now, source),
                )
            self._db.commit()

    def person(self, group_id, user_name, limit=3):
        with self._lock:
            rows = self._db.execute(
                "SELECT category, content FROM person_profiles WHERE group_id=? AND user_name=? "
                "ORDER BY weight DESC LIMIT ?", (group_id, user_name, limit),
            ).fetchall()
        return ["{}：{}".format(r["category"], r["content"]) for r in rows]

    def snapshot_person(self, group_id, user_name, summary, now=None):
        """人工 override / 自动快照，留一条最高权重的总览。"""
        now = time.time() if now is None else now
        with self._lock:
            self._db.execute(
                "DELETE FROM person_profiles WHERE group_id=? AND user_name=? AND category='snapshot'",
                (group_id, user_name),
            )
            self._db.execute(
                "INSERT INTO person_profiles(group_id, user_name, category, content, weight, last_hit, source) "
                "VALUES (?,?,?,?,?,?,?)",
                (group_id, user_name, "snapshot", summary[:200], 4.0, now, "override"),
            )
            self._db.commit()

    def add_relation(self, group_id, a, b, relation, now=None):
        now = time.time() if now is None else now
        with self._lock:
            row = self._db.execute(
                "SELECT id, weight FROM relations WHERE group_id=? AND a=? AND b=? AND relation=? "
                "AND valid_to IS NULL", (group_id, a, b, relation),
            ).fetchone()
            if row:
                self._db.execute("UPDATE relations SET weight=? WHERE id=?",
                                 (maintain("reinforce", row["weight"]), row["id"]))
            else:
                self._db.execute(
                    "INSERT INTO relations(group_id, a, b, relation, weight, valid_from) "
                    "VALUES (?,?,?,?,?,?)", (group_id, a, b, relation, 1.0, now),
                )
            self._db.commit()

    def relations(self, group_id, name="", limit=4):
        with self._lock:
            if name:
                rows = self._db.execute(
                    "SELECT a, b, relation FROM relations WHERE group_id=? AND (a=? OR b=?) "
                    "AND valid_to IS NULL ORDER BY weight DESC LIMIT ?",
                    (group_id, name, name, limit),
                ).fetchall()
            else:
                rows = self._db.execute(
                    "SELECT a, b, relation FROM relations WHERE group_id=? AND valid_to IS NULL "
                    "ORDER BY weight DESC LIMIT ?", (group_id, limit),
                ).fetchall()
        return ["{} 与 {}：{}".format(r["a"], r["b"], r["relation"]) for r in rows]

    # ---- 黑话、反馈与维护 ----
    def add_jargon(self, group_id, word, alias):
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO jargon(group_id, word, alias) VALUES (?,?,?)",
                (group_id, word, alias),
            )
            self._db.commit()

    def jargon(self, group_id):
        with self._lock:
            rows = self._db.execute("SELECT word, alias FROM jargon WHERE group_id=?",
                                    (group_id,)).fetchall()
        return {r["word"]: r["alias"] for r in rows}

    def record_reply(self, group_id, reply, now=None):
        now = time.time() if now is None else now
        with self._lock:
            self._db.execute(
                "INSERT INTO reply_effects(group_id, reply, created_at) VALUES (?,?,?)",
                (group_id, reply[:200], now),
            )
            self._db.commit()

    def mark_followup(self, group_id, now=None, window=120):
        """有人接了机器人的话，就给最近一条回复记一次正反馈。"""
        now = time.time() if now is None else now
        with self._lock:
            self._db.execute(
                "UPDATE reply_effects SET followed_up=1 WHERE id=("
                "SELECT id FROM reply_effects WHERE group_id=? AND created_at>? "
                "ORDER BY id DESC LIMIT 1)", (group_id, now - window),
            )
            self._db.commit()

    def effect_rate(self, group_id, now=None, window=86400):
        now = time.time() if now is None else now
        with self._lock:
            row = self._db.execute(
                "SELECT COUNT(*) total, COALESCE(SUM(followed_up),0) good FROM reply_effects "
                "WHERE group_id=? AND created_at>?", (group_id, now - window),
            ).fetchone()
        return (row["good"] / row["total"]) if row["total"] else 0.5

    def prune(self, group_id, max_episodes=MAX_EPISODES, now=None):
        """低频记忆标记遗忘而非删除；超阈值才回收；Episode 保留最近若干条。"""
        now = time.time() if now is None else now
        with self._lock:
            self._db.execute(
                "DELETE FROM episodes WHERE group_id=? AND id NOT IN "
                "(SELECT id FROM episodes WHERE group_id=? ORDER BY id DESC LIMIT ?)",
                (group_id, group_id, max_episodes),
            )
            self._db.execute(
                "UPDATE memory_points SET state='forgotten' WHERE group_id=? AND state='active' "
                "AND protected=0 AND last_hit<?", (group_id, now - FORGET_AFTER),
            )
            count = self._db.execute(
                "SELECT COUNT(*) n FROM memory_points WHERE group_id=? AND state='active'",
                (group_id,),
            ).fetchone()["n"]
            if count > MAX_POINTS:
                self._db.execute(
                    "UPDATE memory_points SET state='forgotten' WHERE group_id=? AND state='active' "
                    "AND protected=0 AND id IN (SELECT id FROM memory_points WHERE group_id=? "
                    "AND state='active' ORDER BY weight ASC LIMIT ?)",
                    (group_id, group_id, count - MAX_POINTS),
                )
            self._db.execute(
                "DELETE FROM reply_effects WHERE group_id=? AND created_at<?",
                (group_id, now - 7 * 86400),
            )
            self._db.commit()

    def stats(self, group_id):
        with self._lock:
            out = {}
            for table in ("episodes", "memory_points", "person_profiles", "relations", "jargon"):
                out[table] = self._db.execute(
                    "SELECT COUNT(*) n FROM {} WHERE group_id=?".format(table),
                    (group_id,),
                ).fetchone()["n"]
        out["fts"] = self._fts
        return out

    def context(self, group_id, query="", user_name=""):
        """分层上下文：情节 / 记忆点 / 人物档案 / 关系 / 回复效果，供模型读取。"""
        parts = []
        episodes = self.recent_episodes(group_id)
        if episodes:
            parts.append("最近聊过：" + "；".join(episodes))
        points = self.recall(group_id, query)
        if points:
            parts.append("本群记得：" + "；".join(points))
        person = self.person(group_id, user_name) if user_name else []
        if person:
            parts.append("关于{}：".format(user_name) + "；".join(person))
        relations = self.relations(group_id, user_name) if user_name else []
        if relations:
            parts.append("关系：" + "；".join(relations))
        return "\n".join(parts)

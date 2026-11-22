"""记忆层：Episode、人物档案、记忆点权重、遗忘维护、分层上下文。

参考 MaiBot 的 A_memorix 概念（Episode / 人物画像 / reinforce / 衰减 / 再激活），
但用 SQLite + 现有模型做抽取与压缩，不引向量库或 embedding 服务。
"""

import json
import math
import re
import sqlite3
import threading
import time

MEMORY_PROMPT = (
    "把下面这段群聊压成记忆，只输出一行 JSON，不要解释：\n"
    '{"episode":"一句话概括这段在聊什么","points":[{"category":"person|preference|event|relationship|jargon","content":"一条短事实"}]}\n'
    "没有值得记的就输出 {\"episode\":\"\",\"points\":[]}。每条不超过 40 字，最多 3 条。"
)

HALF_LIFE = 14 * 86400


def parse_memory(raw):
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
        category = str(item.get("category") or "event")[:20]
        content = str(item.get("content") or "").strip()[:60]
        if content:
            points.append((category, content))
    return episode, points[:3]


def decay(weight, last_hit, now=None, half_life=HALF_LIFE):
    now = time.time() if now is None else now
    elapsed = max(0.0, now - last_hit)
    return max(0.05, weight * math.pow(0.5, elapsed / half_life))


def maintain(action, weight):
    """强化 / 保护 / 恢复 / 冻结 / 回收，对应 A_memorix 的 maintain_memory。"""
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
        with self._lock:
            self._db.executescript(
                """
                CREATE TABLE IF NOT EXISTS episodes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_id TEXT NOT NULL, text TEXT NOT NULL, created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS memory_points (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_id TEXT NOT NULL, category TEXT NOT NULL,
                    content TEXT NOT NULL, weight REAL NOT NULL DEFAULT 1.0,
                    last_hit REAL NOT NULL, state TEXT NOT NULL DEFAULT 'active'
                );
                CREATE TABLE IF NOT EXISTS person_profiles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_id TEXT NOT NULL, user_name TEXT NOT NULL,
                    category TEXT NOT NULL, content TEXT NOT NULL,
                    weight REAL NOT NULL DEFAULT 1.0, last_hit REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS jargon (
                    group_id TEXT NOT NULL, word TEXT NOT NULL, alias TEXT NOT NULL,
                    PRIMARY KEY (group_id, word)
                );
                """
            )
            self._db.commit()

    def add_episode(self, group_id, text, now=None):
        if not text:
            return
        now = time.time() if now is None else now
        with self._lock:
            self._db.execute(
                "INSERT INTO episodes(group_id, text, created_at) VALUES (?,?,?)",
                (group_id, text, now),
            )
            self._db.commit()

    def recent_episodes(self, group_id, limit=3):
        with self._lock:
            rows = self._db.execute(
                "SELECT text FROM episodes WHERE group_id=? ORDER BY id DESC LIMIT ?",
                (group_id, limit),
            ).fetchall()
        return [r["text"] for r in reversed(rows)]

    def add_point(self, group_id, category, content, now=None):
        now = time.time() if now is None else now
        with self._lock:
            row = self._db.execute(
                "SELECT id, weight FROM memory_points WHERE group_id=? AND content=?",
                (group_id, content),
            ).fetchone()
            if row:
                self._db.execute(
                    "UPDATE memory_points SET weight=?, last_hit=?, state='active' WHERE id=?",
                    (maintain("reinforce", row["weight"]), now, row["id"]),
                )
            else:
                self._db.execute(
                    "INSERT INTO memory_points(group_id, category, content, weight, last_hit) "
                    "VALUES (?,?,?,?,?)",
                    (group_id, category, content, 1.0, now),
                )
            self._db.commit()

    def recall(self, group_id, query, limit=4, now=None):
        now = time.time() if now is None else now
        words = [w for w in re.findall(r"[\u4e00-\u9fff]{2,}|[a-z0-9]{3,}", query or "")][:4]
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM memory_points WHERE group_id=? AND state='active'",
                (group_id,),
            ).fetchall()
        scored = []
        for row in rows:
            weight = decay(row["weight"], row["last_hit"], now)
            if weight < 0.15:
                continue
            hit = sum(1 for w in words if w in row["content"])
            scored.append((weight + hit * 0.5, row["content"], row["id"]))
        scored.sort(reverse=True)
        for _, _, rid in scored[:limit]:
            with self._lock:
                self._db.execute(
                    "UPDATE memory_points SET last_hit=? WHERE id=?", (now, rid)
                )
                self._db.commit()
        return [content for _, content, _ in scored[:limit]]

    def upsert_person(self, group_id, user_name, category, content, now=None):
        now = time.time() if now is None else now
        with self._lock:
            row = self._db.execute(
                "SELECT id, weight FROM person_profiles WHERE group_id=? AND user_name=? "
                "AND category=? AND content=?",
                (group_id, user_name, category, content),
            ).fetchone()
            if row:
                self._db.execute(
                    "UPDATE person_profiles SET weight=?, last_hit=? WHERE id=?",
                    (maintain("reinforce", row["weight"]), now, row["id"]),
                )
            else:
                self._db.execute(
                    "INSERT INTO person_profiles(group_id, user_name, category, content, weight, last_hit) "
                    "VALUES (?,?,?,?,?,?)",
                    (group_id, user_name, category, content, 1.0, now),
                )
            self._db.commit()

    def person(self, group_id, user_name, limit=3):
        with self._lock:
            rows = self._db.execute(
                "SELECT category, content FROM person_profiles WHERE group_id=? AND user_name=? "
                "ORDER BY weight DESC LIMIT ?",
                (group_id, user_name, limit),
            ).fetchall()
        return ["{}：{}".format(r["category"], r["content"]) for r in rows]

    def add_jargon(self, group_id, word, alias):
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO jargon(group_id, word, alias) VALUES (?,?,?)",
                (group_id, word, alias),
            )
            self._db.commit()

    def jargon(self, group_id):
        with self._lock:
            rows = self._db.execute(
                "SELECT word, alias FROM jargon WHERE group_id=?", (group_id,)
            ).fetchall()
        return {r["word"]: r["alias"] for r in rows}

    def prune(self, group_id, max_episodes=200, now=None):
        now = time.time() if now is None else now
        with self._lock:
            self._db.execute(
                "DELETE FROM episodes WHERE group_id=? AND id NOT IN "
                "(SELECT id FROM episodes WHERE group_id=? ORDER BY id DESC LIMIT ?)",
                (group_id, group_id, max_episodes),
            )
            self._db.execute(
                "UPDATE memory_points SET state='forgotten' WHERE group_id=? AND last_hit<?",
                (group_id, now - 60 * 86400),
            )
            self._db.commit()

    def context(self, group_id, query="", user_name=""):
        """分层上下文：情节 / 记忆点 / 人物档案，供模型读取。"""
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
        return "\n".join(parts)

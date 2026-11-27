"""旧版分离 SQLite 数据库（persons / memory / monitor）向 echo.sqlite3 的幂等无损迁移。"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any, Callable, Optional


def _safe_connect(path: Path) -> Optional[sqlite3.Connection]:
    if not path.is_file():
        return None
    conn = sqlite3.connect(str(path), timeout=10.0)
    conn.row_factory = sqlite3.Row
    return conn


def _migrate_persons(
    dest: sqlite3.Connection,
    per_db_path: Path,
    summary: dict[str, int],
) -> None:
    pconn = _safe_connect(per_db_path)
    if pconn is None:
        return
    try:
        ind_map: dict[str, sqlite3.Row] = {}
        try:
            for row in pconn.execute("SELECT * FROM individuals"):
                ind_map[str(row["user_id"])] = row
        except sqlite3.Error:
            pass
        try:
            for row in pconn.execute("SELECT * FROM identities"):
                dest.execute(
                    "INSERT OR IGNORE INTO person_aliases"
                    "(group_id, user_id, name, kind, hits, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        row["group_id"],
                        row["user_id"],
                        row["name"],
                        row["kind"],
                        row["hits"],
                        row["updated_at"],
                    ),
                )
                summary["aliases"] += 1
            for row in pconn.execute("SELECT DISTINCT group_id, user_id FROM identities"):
                uid = str(row["user_id"])
                ind = ind_map.get(uid)
                if ind:
                    dest.execute(
                        "INSERT OR IGNORE INTO persons"
                        "(group_id, user_id, msg_count, first_seen, last_seen, updated_at) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (
                            row["group_id"],
                            uid,
                            int(ind["msg_count"]),
                            float(ind["first_seen"]),
                            float(ind["last_seen"]),
                            float(ind["last_seen"]),
                        ),
                    )
        except sqlite3.Error:
            pass
        try:
            for row in pconn.execute("SELECT * FROM profiles"):
                uid = str(row["user_id"])
                ind = ind_map.get(uid)
                first_seen = float(ind["first_seen"]) if ind else 0.0
                last_seen = float(ind["last_seen"]) if ind else 0.0
                msg_count = int(ind["msg_count"]) if ind else 0
                dest.execute(
                    "INSERT INTO persons"
                    "(group_id, user_id, traits, preferences, events, relation, "
                    "intimacy, msg_count, first_seen, last_seen, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(group_id, user_id) DO UPDATE SET "
                    "traits = CASE WHEN excluded.traits <> '' THEN excluded.traits ELSE persons.traits END, "
                    "preferences = CASE WHEN excluded.preferences <> '' THEN excluded.preferences ELSE persons.preferences END, "
                    "events = CASE WHEN excluded.events <> '' THEN excluded.events ELSE persons.events END, "
                    "relation = CASE WHEN excluded.relation <> '' THEN excluded.relation ELSE persons.relation END, "
                    "intimacy = excluded.intimacy, "
                    "updated_at = MAX(persons.updated_at, excluded.updated_at)",
                    (
                        row["group_id"],
                        uid,
                        row["traits"],
                        row["preferences"],
                        row["events"],
                        row["relation"],
                        row["intimacy"],
                        msg_count,
                        first_seen,
                        last_seen,
                        row["updated_at"],
                    ),
                )
                summary["persons"] += 1
        except sqlite3.Error:
            pass
    finally:
        pconn.close()


def _migrate_memories(
    dest: sqlite3.Connection,
    mem_db_path: Path,
    summary: dict[str, int],
    now: float,
    *,
    resolve_person: Optional[Callable[[str, str], Optional[str]]] = None,
    observe_person: Optional[Callable[..., Any]] = None,
    upsert_person: Optional[Callable[..., Any]] = None,
) -> None:
    mconn = _safe_connect(mem_db_path)
    if mconn is None:
        return
    try:
        try:
            for row in mconn.execute("SELECT * FROM episodes"):
                ext = str(row["external_id"] or "").strip() or ("legacy_ep:{}".format(row["id"]))
                dest.execute(
                    "INSERT OR IGNORE INTO episodes"
                    "(group_id, text, created_at, source, external_id, state) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        row["group_id"],
                        row["text"],
                        row["created_at"],
                        row["source"],
                        ext,
                        row["state"],
                    ),
                )
                summary["episodes"] += 1
        except sqlite3.Error:
            pass
        try:
            for row in mconn.execute("SELECT * FROM memory_points"):
                exists = dest.execute(
                    "SELECT 1 FROM memories WHERE group_id=? AND content=?",
                    (row["group_id"], row["content"]),
                ).fetchone()
                if not exists:
                    dest.execute(
                        "INSERT INTO memories"
                        "(group_id, category, content, weight, last_hit, state, protected) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (
                            row["group_id"],
                            row["category"],
                            row["content"],
                            row["weight"],
                            row["last_hit"],
                            row["state"],
                            row["protected"],
                        ),
                    )
                    summary["memories"] += 1
        except sqlite3.Error:
            pass
        try:
            for row in mconn.execute("SELECT * FROM jargon"):
                content = "{}={}".format(row["word"], row["alias"])
                exists = dest.execute(
                    "SELECT 1 FROM memories WHERE group_id=? AND category='jargon' AND content=?",
                    (row["group_id"], content),
                ).fetchone()
                if not exists:
                    dest.execute(
                        "INSERT INTO memories"
                        "(group_id, category, content, weight, last_hit, state, protected) "
                        "VALUES (?, 'jargon', ?, 1.5, ?, 'active', 0)",
                        (row["group_id"], content, now),
                    )
                    summary["memories"] += 1
        except sqlite3.Error:
            pass
        try:
            for row in mconn.execute("SELECT * FROM relations WHERE valid_to IS NULL"):
                content = "{} 与 {}：{}".format(row["a"], row["b"], row["relation"])
                exists = dest.execute(
                    "SELECT 1 FROM memories WHERE group_id=? AND category='relation' AND content=?",
                    (row["group_id"], content),
                ).fetchone()
                if not exists:
                    dest.execute(
                        "INSERT INTO memories"
                        "(group_id, category, content, weight, last_hit, state, protected) "
                        "VALUES (?, 'relation', ?, ?, ?, 'active', 0)",
                        (row["group_id"], content, row["weight"], row["valid_from"]),
                    )
                    summary["memories"] += 1
        except sqlite3.Error:
            pass
        if resolve_person and observe_person and upsert_person:
            try:
                for row in mconn.execute("SELECT * FROM person_profiles"):
                    gid = str(row["group_id"])
                    uname = str(row["user_name"] or "").strip()
                    content = str(row["content"] or "").strip()
                    if not uname or not content:
                        continue
                    uid = resolve_person(gid, uname) or ("legacy:" + uname)
                    if uid.startswith("legacy:"):
                        observe_person(gid, uid, uname, now=float(row["last_hit"] or now))
                    field = {
                        "person": "traits",
                        "traits": "traits",
                        "preference": "preferences",
                        "preferences": "preferences",
                        "event": "events",
                        "events": "events",
                        "relation": "relation",
                        "relationship": "relation",
                    }.get(str(row["category"] or "person").lower(), "traits")
                    upsert_person(gid, uid, now=float(row["last_hit"] or now), **{field: content})
            except sqlite3.Error:
                pass
    finally:
        mconn.close()


def _migrate_monitor(dest: sqlite3.Connection, mon_db_path: Path, summary: dict[str, int]) -> None:
    oconn = _safe_connect(mon_db_path)
    if oconn is None:
        return
    try:
        existing_cnt = dest.execute("SELECT COUNT(*) FROM monitor_events").fetchone()[0]
        if existing_cnt == 0:
            for row in oconn.execute("SELECT * FROM events ORDER BY id DESC LIMIT 1000"):
                dest.execute(
                    "INSERT INTO monitor_events"
                    "(group_id, kind, ok, latency, tokens, detail, ts) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        row["group_id"],
                        row["kind"],
                        row["ok"],
                        row["latency"],
                        row["tokens"],
                        row["detail"],
                        row["ts"],
                    ),
                )
                summary["events"] += 1
    except sqlite3.Error:
        pass
    finally:
        oconn.close()


def migrate_legacy_v1_dbs(
    conn: sqlite3.Connection,
    data_dir: Path | str,
    *,
    resolve_person: Optional[Callable[[str, str], Optional[str]]] = None,
    observe_person: Optional[Callable[..., Any]] = None,
    upsert_person: Optional[Callable[..., Any]] = None,
) -> dict[str, int]:
    """将 v1 分离库无损、幂等迁移到统一 echo.sqlite3 连接上（调用方应持锁）。"""
    summary = {"episodes": 0, "memories": 0, "aliases": 0, "persons": 0, "events": 0}
    done = conn.execute("SELECT v FROM schema_meta WHERE k='migrated_legacy_v1'").fetchone()
    if done and done["v"] == "1":
        return summary

    root = Path(data_dir)
    now = time.time()
    with conn:
        _migrate_persons(conn, root / "persons.sqlite3", summary)
        _migrate_memories(
            conn,
            root / "memory.sqlite3",
            summary,
            now,
            resolve_person=resolve_person,
            observe_person=observe_person,
            upsert_person=upsert_person,
        )
        _migrate_monitor(conn, root / "monitor.sqlite3", summary)
        conn.execute(
            "INSERT OR REPLACE INTO schema_meta(k, v) VALUES ('migrated_legacy_v1', '1')"
        )
    return summary

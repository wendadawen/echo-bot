"""SQLite Schema DDL、连接配置与 user_version 版本化迁移。"""

from __future__ import annotations

import sqlite3
from typing import Callable, Optional

SCHEMA_VERSION = 2

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

EMOJI_TABLES_DDL = """
CREATE TABLE IF NOT EXISTS emoji_items (
    sha256 TEXT PRIMARY KEY,
    rel_path TEXT NOT NULL,
    tag TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    use_count INTEGER NOT NULL DEFAULT 0,
    last_used_at REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_emoji_items_updated ON emoji_items(updated_at DESC);

CREATE TABLE IF NOT EXISTS emoji_group_links (
    group_id TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    added_at REAL NOT NULL,
    PRIMARY KEY (group_id, sha256)
);
CREATE INDEX IF NOT EXISTS idx_emoji_links_group_added
    ON emoji_group_links(group_id, added_at DESC);

CREATE TABLE IF NOT EXISTS emoji_faces (
    group_id TEXT NOT NULL,
    face_id TEXT NOT NULL,
    tag TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    updated_at REAL NOT NULL,
    use_count INTEGER NOT NULL DEFAULT 0,
    last_used_at REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (group_id, face_id)
);

CREATE TABLE IF NOT EXISTS emoji_digest_cache (
    sha256 TEXT PRIMARY KEY,
    ok INTEGER NOT NULL,
    tag TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_emoji_cache_ts ON emoji_digest_cache(ts);
"""

PERSON_FACTS_DDL = """
CREATE TABLE IF NOT EXISTS person_facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    field TEXT NOT NULL,
    fact TEXT NOT NULL,
    hits INTEGER NOT NULL DEFAULT 1,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_person_facts_lookup
    ON person_facts(group_id, user_id, field, updated_at DESC);
"""

_BASE_TABLES_DDL = """
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


def _configure_conn(conn: sqlite3.Connection, path: str, *, read_only: bool = False) -> sqlite3.Connection:
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA temp_store=MEMORY")
    if str(path) != ":memory:":
        try:
            if read_only:
                conn.execute("PRAGMA query_only=ON")
            else:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
        except sqlite3.Error:
            pass
    return conn


def init_schema(
    conn: sqlite3.Connection,
    backfill_person_facts_fn: Optional[Callable[[sqlite3.Connection], None]] = None,
) -> tuple[bool, bool]:
    """初始化与增量升级 SQLite Schema，返回 (fts_enabled, fts_trigram_enabled)。"""
    ver = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if ver < 1:
        conn.executescript(_BASE_TABLES_DDL + _PENDING_DELIVERY_DDL)
        msg_cols = {r["name"] for r in conn.execute("PRAGMA table_info(messages)")}
        with conn:
            if "msg_id" not in msg_cols:
                conn.execute("ALTER TABLE messages ADD COLUMN msg_id TEXT NOT NULL DEFAULT ''")
            if "meta" not in msg_cols:
                conn.execute("ALTER TABLE messages ADD COLUMN meta TEXT NOT NULL DEFAULT ''")
            conn.execute("PRAGMA user_version = 1")
        ver = 1

    if ver < SCHEMA_VERSION:
        conn.executescript(PERSON_FACTS_DDL + EMOJI_TABLES_DDL)
        with conn:
            conn.execute(
                "DELETE FROM messages "
                "WHERE role = 'user' AND msg_id <> '' AND seq NOT IN ("
                "SELECT MIN(seq) FROM messages "
                "WHERE role = 'user' AND msg_id <> '' "
                "GROUP BY group_id, msg_id)"
            )
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_group_msgid "
                "ON messages(group_id, msg_id) WHERE msg_id <> '' AND role = 'user'"
            )
            if callable(backfill_person_facts_fn):
                backfill_person_facts_fn(conn)
            for trig in ("memories_ai", "memories_ad", "memories_au"):
                conn.execute("DROP TRIGGER IF EXISTS {}".format(trig))
            conn.execute("DROP TABLE IF EXISTS memories_fts")
            fts = False
            fts_trigram = False
            try:
                conn.execute(
                    "CREATE VIRTUAL TABLE memories_fts "
                    "USING fts5(content, content='memories', content_rowid='id', tokenize='trigram')"
                )
                fts = True
                fts_trigram = True
            except sqlite3.OperationalError:
                try:
                    conn.execute(
                        "CREATE VIRTUAL TABLE memories_fts "
                        "USING fts5(content, content='memories', content_rowid='id')"
                    )
                    fts = True
                except sqlite3.OperationalError:
                    fts = False
            if fts:
                conn.execute(
                    "CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN "
                    "INSERT INTO memories_fts(rowid, content) VALUES (new.id, new.content); END"
                )
                conn.execute(
                    "CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN "
                    "INSERT INTO memories_fts(memories_fts, rowid, content) "
                    "VALUES ('delete', old.id, old.content); END"
                )
                conn.execute(
                    "CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE OF content ON memories BEGIN "
                    "INSERT INTO memories_fts(memories_fts, rowid, content) "
                    "VALUES ('delete', old.id, old.content); "
                    "INSERT INTO memories_fts(rowid, content) VALUES (new.id, new.content); END"
                )
                conn.execute("INSERT INTO memories_fts(memories_fts) VALUES ('rebuild')")
            conn.execute("PRAGMA user_version = {}".format(SCHEMA_VERSION))
        return fts, fts_trigram

    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='memories_fts'"
    ).fetchone()
    fts = bool(row)
    fts_trigram = bool(row and "trigram" in str(row[0] or "").lower())
    return fts, fts_trigram

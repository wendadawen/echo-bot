"""表情包存储：内容寻址全局共享高质量表情池、VLM 门禁、情绪标签与防连发挑选。"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import random
import re
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Iterator, Optional

from ..domain import FACE_LABELS, LLMClient, PickedEmoji, detect_image_format
from .emoji_classifier import (
    ANALYZE_PROMPT,
    FILTER_WORDS,
    NON_EMOJI_WORDS,
    STICKER_MAX_ASPECT,
    STICKER_MAX_DIM,
    _BAD_EMOJI_WORDS,
    _BAD_TAG_PREFIXES,
    _NON_STICKER_DESC_WORDS,
    _clean_tag,
    _is_valid_tag,
    acceptable,
    analyze_emoji,
    is_sticker_geometry,
)
from .repository import Repository
from .schema import EMOJI_TABLES_DDL, _configure_conn

GLOBAL_POOL_KEY = "_global"
DEFAULT_GLOBAL_LIMIT = 160


class EmojiStore:
    """跨群全局共享高质量表情池：SQLite 元数据、shared/ 相对寻址、Singleflight 与加权防连发。"""

    def __init__(
        self,
        root: str | Path,
        limit: int = 64,
        max_age_days: int = 30,
        global_limit: Optional[int] = None,
        repo: Optional[Repository] = None,
    ):
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)
        self.index_path = self._root / "index.json"
        self.cache_path = self._root / "digest_cache.json"
        self.limit = int(limit)
        self.max_age_days = int(max_age_days)
        self.global_limit = max(int(global_limit or DEFAULT_GLOBAL_LIMIT), self.limit * 2)
        self._repo = repo
        self._lock = threading.RLock()
        self._db_lock = threading.RLock()
        self._closed = False
        self._inflight: dict[str, threading.Event] = {}
        self._last_picked: dict[str, str] = {}
        if repo is None:
            db_path = self._root / "emoji_meta.sqlite3"
            self._db: Optional[sqlite3.Connection] = _configure_conn(
                sqlite3.connect(str(db_path), timeout=30.0, check_same_thread=False),
                str(db_path),
            )
            self._owns_db = True
            with self._db_lock, self._db:
                self._db.executescript(EMOJI_TABLES_DDL)
        else:
            self._db = None
            self._owns_db = False
        self._migrate_legacy_json()

    @property
    def root(self) -> Path:
        return self._root

    @contextlib.contextmanager
    def _read(self) -> Iterator[Optional[sqlite3.Connection]]:
        if self._closed:
            yield None
            return
        if self._repo is not None:
            with self._repo.read_conn() as conn:
                yield conn
            return
        with self._db_lock:
            if self._closed or self._db is None:
                yield None
                return
            yield self._db

    @contextlib.contextmanager
    def _write(self) -> Iterator[Optional[sqlite3.Connection]]:
        if self._closed:
            yield None
            return
        if self._repo is not None:
            with self._repo.write_conn() as conn:
                yield conn
            return
        with self._db_lock:
            if self._closed or self._db is None:
                yield None
                return
            with self._db:
                yield self._db

    def close(self) -> None:
        with self._db_lock:
            if not self._closed:
                self._closed = True
                if self._owns_db and self._db is not None:
                    try:
                        self._db.close()
                    except sqlite3.Error:
                        pass
                    self._db = None

    def _safe_path(self, path_str: Any) -> Optional[Path]:
        """校验路径位于 self._root 牢笼内且无符号链接逃逸（支持相对与旧版绝对路径）。"""
        s = str(path_str or "").strip()
        if not s:
            return None
        try:
            root_res = self._root.resolve(strict=False)
            raw_p = Path(s)
            if not raw_p.is_absolute():
                if ".." in raw_p.parts:
                    return None
                p = self._root / raw_p
            else:
                p = raw_p
            cur = p
            while True:
                if cur.is_symlink():
                    return None
                try:
                    if cur.resolve(strict=False) == root_res:
                        break
                except OSError:
                    return None
                parent = cur.parent
                if parent == cur:
                    return None
                cur = parent
            resolved = p.resolve(strict=False)
            if root_res not in resolved.parents:
                return None
            return resolved if resolved.is_file() else None
        except OSError:
            return None

    def _resolve_rel_path(self, rel_path: Any) -> Optional[Path]:
        s = str(rel_path or "").strip()
        if not s or Path(s).is_absolute():
            return None
        return self._safe_path(s)

    def _shared_dir(self) -> Path:
        folder = self._root / "shared"
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    def _canonical_shared_rel(self, digest: str, image_bytes: bytes) -> tuple[str, Path]:
        _, ext = detect_image_format(image_bytes)
        rel = "shared/{}{}".format(digest[:16], ext)
        canonical = self._shared_dir() / "{}{}".format(digest[:16], ext)
        if not canonical.is_file():
            tmp = canonical.parent / ".{}.{}.tmp".format(canonical.name, uuid.uuid4().hex[:8])
            tmp.write_bytes(image_bytes)
            os.replace(str(tmp), str(canonical))
        return rel, canonical

    def _unlink_unreferenced(self, path_candidates: list[str]) -> None:
        if not path_candidates:
            return
        with self._read() as conn:
            if conn is None:
                return
            referenced = {str(r["rel_path"]) for r in conn.execute("SELECT rel_path FROM emoji_items").fetchall()}
        for cand in dict.fromkeys(str(p or "") for p in path_candidates if p):
            if cand in referenced:
                continue
            safe = self._safe_path(cand)
            if safe is None:
                continue
            try:
                safe.unlink(missing_ok=True)
            except OSError:
                pass

    def _upsert_entry_locked(
        self,
        conn: sqlite3.Connection,
        group_id: str,
        rel_path: str,
        digest: str,
        label: str,
        description: str,
        cap: int,
        now: float,
        evict_candidates: list[str],
    ) -> None:
        conn.execute(
            "INSERT INTO emoji_items(sha256, rel_path, tag, description, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(sha256) DO UPDATE SET "
            "rel_path = excluded.rel_path, "
            "tag = CASE WHEN excluded.tag <> '' THEN excluded.tag ELSE emoji_items.tag END, "
            "description = CASE WHEN excluded.description <> '' THEN excluded.description ELSE emoji_items.description END, "
            "updated_at = MAX(emoji_items.updated_at, excluded.updated_at)",
            (digest, rel_path, label, description[:80], now, now),
        )
        conn.execute(
            "INSERT INTO emoji_group_links(group_id, sha256, added_at) VALUES (?, ?, ?) "
            "ON CONFLICT(group_id, sha256) DO UPDATE SET added_at = MAX(emoji_group_links.added_at, excluded.added_at)",
            (group_id, digest, now),
        )
        conn.execute(
            "DELETE FROM emoji_group_links WHERE group_id = ? AND sha256 NOT IN ("
            "SELECT sha256 FROM emoji_group_links WHERE group_id = ? ORDER BY added_at DESC, rowid DESC LIMIT ?"
            ")",
            (group_id, group_id, max(1, int(cap))),
        )
        total_global = int(conn.execute("SELECT COUNT(*) FROM emoji_items").fetchone()[0])
        if total_global > self.global_limit:
            excess = conn.execute(
                "SELECT sha256, rel_path FROM emoji_items ORDER BY updated_at ASC, rowid ASC LIMIT ?",
                (total_global - self.global_limit,),
            ).fetchall()
            for r in excess:
                conn.execute("DELETE FROM emoji_group_links WHERE sha256 = ?", (r["sha256"],))
                conn.execute("DELETE FROM emoji_items WHERE sha256 = ?", (r["sha256"],))
                if r["rel_path"]:
                    evict_candidates.append(str(r["rel_path"]))

    def _migrate_legacy_json(self) -> None:
        """从旧版 index.json / digest_cache.json 无损、幂等迁入 SQLite，并重命名为 *.migrated。"""
        has_idx = self.index_path.is_file()
        has_cache = self.cache_path.is_file()
        if not has_idx and not has_cache:
            return
        legacy_idx: dict[str, Any] = {}
        legacy_cache: dict[str, Any] = {}
        if has_idx:
            try:
                loaded = json.loads(self.index_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    legacy_idx = loaded
            except (ValueError, OSError):
                legacy_idx = {}
        if has_cache:
            try:
                loaded_c = json.loads(self.cache_path.read_text(encoding="utf-8"))
                if isinstance(loaded_c, dict):
                    legacy_cache = loaded_c
            except (ValueError, OSError):
                legacy_cache = {}

        stale_disk_paths: list[str] = []
        now = time.time()
        with self._write() as conn:
            if conn is None:
                return
            for digest, cached in legacy_cache.items():
                if not isinstance(cached, dict):
                    continue
                ok_flag = bool(cached.get("ok"))
                tag = str(cached.get("tag") or "")
                desc = str(cached.get("description") or "")[:80]
                if ok_flag and (not _is_valid_tag(tag) or not acceptable(desc)):
                    ok_flag = False
                    tag = ""
                    desc = ""
                conn.execute(
                    "INSERT INTO emoji_digest_cache(sha256, ok, tag, description, ts) VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(sha256) DO UPDATE SET ok=excluded.ok, tag=excluded.tag, description=excluded.description, ts=MAX(emoji_digest_cache.ts, excluded.ts)",
                    (str(digest), 1 if ok_flag else 0, tag, desc, float(cached.get("ts") or now)),
                )

            for gid, entries in legacy_idx.items():
                if not isinstance(entries, list):
                    continue
                is_global = gid == GLOBAL_POOL_KEY
                for item in entries:
                    if not isinstance(item, dict):
                        continue
                    its = float(item.get("ts") or now)
                    if item.get("face_id") and not item.get("path"):
                        fid = str(item.get("face_id") or "").strip()
                        if is_global or fid not in FACE_LABELS:
                            continue
                        tag = str(item.get("tag") or "").strip()
                        if not tag or tag == "QQ表情":
                            tag = FACE_LABELS[fid]
                        conn.execute(
                            "INSERT INTO emoji_faces(group_id, face_id, tag, description, updated_at) "
                            "VALUES (?, ?, ?, ?, ?) "
                            "ON CONFLICT(group_id, face_id) DO UPDATE SET "
                            "tag=excluded.tag, description=excluded.description, updated_at=MAX(emoji_faces.updated_at, excluded.updated_at)",
                            (str(gid), fid, tag[:20], tag[:20], its),
                        )
                        continue
                    safe = self._safe_path(item.get("path"))
                    if safe is None:
                        continue
                    try:
                        img_bytes = safe.read_bytes()
                    except OSError:
                        continue
                    digest = str(item.get("sha256") or "").strip() or hashlib.sha256(img_bytes).hexdigest()
                    tag = str(item.get("tag") or "").strip()
                    desc = str(item.get("description") or "")[:80]
                    if not _is_valid_tag(tag) or not acceptable(desc, img_bytes):
                        stale_disk_paths.append(str(safe))
                        conn.execute(
                            "INSERT INTO emoji_digest_cache(sha256, ok, tag, description, ts) VALUES (?, 0, '', '', ?) "
                            "ON CONFLICT(sha256) DO UPDATE SET ok=0, tag='', description='', ts=MAX(emoji_digest_cache.ts, excluded.ts)",
                            (digest, its),
                        )
                        continue
                    rel, canonical = self._canonical_shared_rel(digest, img_bytes)
                    if safe != canonical.resolve(strict=False):
                        stale_disk_paths.append(str(safe))
                    conn.execute(
                        "INSERT INTO emoji_digest_cache(sha256, ok, tag, description, ts) VALUES (?, 1, ?, ?, ?) "
                        "ON CONFLICT(sha256) DO NOTHING",
                        (digest, tag, desc, its),
                    )
                    if is_global:
                        conn.execute(
                            "INSERT INTO emoji_items(sha256, rel_path, tag, description, created_at, updated_at) "
                            "VALUES (?, ?, ?, ?, ?, ?) "
                            "ON CONFLICT(sha256) DO UPDATE SET updated_at = MAX(emoji_items.updated_at, excluded.updated_at)",
                            (digest, rel, tag, desc, its, its),
                        )
                    else:
                        self._upsert_entry_locked(
                            conn, str(gid), rel, digest, tag, desc, self.limit, its, stale_disk_paths
                        )

        for p_str in stale_disk_paths:
            safe = self._safe_path(p_str)
            if safe is not None:
                # 仅删除非 canonical rel_path 或已被删除出库的旧路径
                rel_s = ""
                try:
                    rel_s = safe.relative_to(self._root.resolve(strict=False)).as_posix()
                except ValueError:
                    rel_s = ""
                if not rel_s:
                    continue
                with self._read() as conn:
                    still_used = (
                        conn is not None
                        and conn.execute(
                            "SELECT 1 FROM emoji_items WHERE rel_path = ? LIMIT 1", (rel_s,)
                        ).fetchone()
                        is not None
                    )
                if not still_used:
                    try:
                        safe.unlink(missing_ok=True)
                    except OSError:
                        pass

        if has_idx:
            try:
                os.replace(str(self.index_path), str(self._root / "index.json.migrated"))
            except OSError:
                pass
        if has_cache:
            try:
                os.replace(str(self.cache_path), str(self._root / "digest_cache.json.migrated"))
            except OSError:
                pass

    def _try_fast_collect(
        self,
        gid: str,
        digest: str,
        image_bytes: bytes,
        cap: int,
        now: float,
    ) -> Optional[str]:
        evicted: list[str] = []
        with self._read() as conn:
            if conn is None:
                return ""
            link_row = conn.execute(
                "SELECT i.rel_path, i.tag, i.description FROM emoji_group_links l "
                "JOIN emoji_items i ON i.sha256 = l.sha256 "
                "WHERE l.group_id = ? AND l.sha256 = ?",
                (gid, digest),
            ).fetchone()
            cached = conn.execute(
                "SELECT ok, tag, description FROM emoji_digest_cache WHERE sha256 = ?",
                (digest,),
            ).fetchone()

        if link_row is not None and self._resolve_rel_path(link_row["rel_path"]) is not None:
            rel, _ = self._canonical_shared_rel(digest, image_bytes)
            tag = str(link_row["tag"] or "随手")
            desc = str(link_row["description"] or "")
            with self._write() as wconn:
                if wconn is not None:
                    self._upsert_entry_locked(wconn, gid, rel, digest, tag, desc, cap, now, evicted)
            self._unlink_unreferenced(evicted)
            return tag

        if cached is not None:
            if not int(cached["ok"] or 0):
                return ""
            rel, _ = self._canonical_shared_rel(digest, image_bytes)
            tag = str(cached["tag"] or "随手")
            desc = str(cached["description"] or "")
            with self._write() as wconn:
                if wconn is not None:
                    self._upsert_entry_locked(wconn, gid, rel, digest, tag, desc, cap, now, evicted)
            self._unlink_unreferenced(evicted)
            return tag

        return None

    def _finish_collect(
        self,
        gid: str,
        digest: str,
        image_bytes: bytes,
        ok: bool,
        label: str,
        description: str,
        cap: int,
        now: float,
    ) -> None:
        evicted: list[str] = []
        rel = ""
        if ok:
            rel, _ = self._canonical_shared_rel(digest, image_bytes)
        with self._write() as conn:
            if conn is None:
                return
            if str(description or "").strip():
                conn.execute(
                    "INSERT INTO emoji_digest_cache(sha256, ok, tag, description, ts) VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(sha256) DO UPDATE SET ok=excluded.ok, tag=excluded.tag, description=excluded.description, ts=excluded.ts",
                    (digest, 1 if ok else 0, label if ok else "", description[:80] if ok else "", now),
                )
                cnt = int(conn.execute("SELECT COUNT(*) FROM emoji_digest_cache").fetchone()[0])
                if cnt > 1000:
                    conn.execute(
                        "DELETE FROM emoji_digest_cache WHERE sha256 IN ("
                        "SELECT sha256 FROM emoji_digest_cache ORDER BY ts ASC LIMIT ?"
                        ")",
                        (cnt - 1000,),
                    )
            if ok:
                self._upsert_entry_locked(conn, gid, rel, digest, label, description[:80], cap, now, evicted)
        self._unlink_unreferenced(evicted)

    def collect(self, group_id: str, image_bytes: bytes, llm: LLMClient, limit: Optional[int] = None) -> str:
        if not image_bytes or len(image_bytes) > 2 * 1024 * 1024:
            return ""
        digest = hashlib.sha256(image_bytes).hexdigest()
        gid = str(group_id)
        cap = int(limit or self.limit)
        now = time.time()

        fast = self._try_fast_collect(gid, digest, image_bytes, cap, now)
        if fast is not None:
            return fast

        leader_ev: Optional[threading.Event] = None
        wait_ev: Optional[threading.Event] = None
        with self._lock:
            existing = self._inflight.get(digest)
            if existing is not None:
                wait_ev = existing
            else:
                leader_ev = threading.Event()
                self._inflight[digest] = leader_ev

        if wait_ev is not None:
            wait_ev.wait(timeout=15.0)
            after = self._try_fast_collect(gid, digest, image_bytes, cap, time.time())
            return after or ""

        ok = False
        description = ""
        label = "随手"
        analyzed = False
        try:
            already = self._try_fast_collect(gid, digest, image_bytes, cap, time.time())
            if already is not None:
                return already
            analyzed = True
            description, label = analyze_emoji(llm, image_bytes)
            ok = acceptable(description, image_bytes)
        except Exception:
            description = ""
            ok = False
        finally:
            try:
                if analyzed:
                    self._finish_collect(gid, digest, image_bytes, ok, label, description, cap, time.time())
            finally:
                with self._lock:
                    if self._inflight.get(digest) is leader_ev:
                        self._inflight.pop(digest, None)
                if leader_ev is not None:
                    leader_ev.set()
        return label if ok else ""

    def collect_face(self, group_id: str, face_id: str, name: str = "") -> str:
        fid = str(face_id or "").strip()
        if not fid or fid not in FACE_LABELS:
            return ""
        canonical = FACE_LABELS[fid]
        label = str(name or canonical).strip()
        if not label or label == "QQ表情":
            label = canonical
        label = label[:20]
        gid = str(group_id)
        now = time.time()
        with self._write() as conn:
            if conn is None:
                return ""
            conn.execute(
                "INSERT INTO emoji_faces(group_id, face_id, tag, description, updated_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(group_id, face_id) DO UPDATE SET "
                "tag = CASE WHEN emoji_faces.tag IN ('', 'QQ表情') THEN excluded.tag ELSE emoji_faces.tag END, "
                "description = CASE WHEN emoji_faces.tag IN ('', 'QQ表情') THEN excluded.description ELSE emoji_faces.description END, "
                "updated_at = excluded.updated_at",
                (gid, fid, label, label, now),
            )
            conn.execute(
                "DELETE FROM emoji_faces WHERE group_id = ? AND face_id NOT IN ("
                "SELECT face_id FROM emoji_faces WHERE group_id = ? ORDER BY updated_at DESC, rowid DESC LIMIT ?"
                ")",
                (gid, gid, self.limit),
            )
            row = conn.execute(
                "SELECT tag FROM emoji_faces WHERE group_id = ? AND face_id = ?", (gid, fid)
            ).fetchone()
            return str(row["tag"] or canonical) if row else label

    def remove_face(self, group_id: str, face_id: str) -> bool:
        """发送 QQ 原生表情失败时踢出失效 face_id，防止后续重复报错 1200。"""
        fid = str(face_id or "").strip()
        if not fid:
            return False
        with self._write() as conn:
            if conn is None:
                return False
            cur = conn.execute(
                "DELETE FROM emoji_faces WHERE group_id = ? AND face_id = ?",
                (str(group_id), fid),
            )
            return (cur.rowcount or 0) > 0

    def has_entries(self, group_id: str) -> bool:
        gid = str(group_id)
        with self._read() as conn:
            if conn is None:
                return False
            if conn.execute("SELECT 1 FROM emoji_faces WHERE group_id = ? LIMIT 1", (gid,)).fetchone():
                return True
            img_rows = conn.execute("SELECT rel_path FROM emoji_items").fetchall()
        return any(self._resolve_rel_path(r["rel_path"]) is not None for r in img_rows)

    def tags(self, group_id: str, limit: int = 12) -> list[str]:
        gid = str(group_id)
        out: list[str] = []
        seen: set[str] = set()
        with self._read() as conn:
            if conn is None:
                return []
            local_imgs = conn.execute(
                "SELECT i.rel_path, i.tag, l.added_at AS ts FROM emoji_group_links l "
                "JOIN emoji_items i ON i.sha256 = l.sha256 "
                "WHERE l.group_id = ? ORDER BY l.added_at DESC",
                (gid,),
            ).fetchall()
            local_faces = conn.execute(
                "SELECT tag, updated_at AS ts FROM emoji_faces WHERE group_id = ? ORDER BY updated_at DESC",
                (gid,),
            ).fetchall()
            global_imgs = conn.execute(
                "SELECT rel_path, tag FROM emoji_items ORDER BY updated_at DESC"
            ).fetchall()

        local_combined: list[tuple[float, str]] = []
        for r in local_imgs:
            if self._resolve_rel_path(r["rel_path"]) is not None:
                local_combined.append((float(r["ts"] or 0.0), str(r["tag"] or "").strip()))
        for r in local_faces:
            local_combined.append((float(r["ts"] or 0.0), str(r["tag"] or "").strip()))
        local_combined.sort(key=lambda x: x[0], reverse=True)

        for _, tag in local_combined:
            if tag and tag not in seen:
                seen.add(tag)
                out.append(tag)
                if len(out) >= limit:
                    return out
        for r in global_imgs:
            if self._resolve_rel_path(r["rel_path"]) is None:
                continue
            tag = str(r["tag"] or "").strip()
            if tag and tag not in seen:
                seen.add(tag)
                out.append(tag)
                if len(out) >= limit:
                    break
        return out

    @staticmethod
    def _keyword_terms(keyword: str) -> list[str]:
        raw = str(keyword or "").strip().lower()
        if not raw:
            return []
        terms = list(dict.fromkeys(re.findall(r"[a-z0-9]{2,}", raw)))
        for span in re.findall(r"[\u4e00-\u9fff]+", raw):
            if len(span) == 1:
                if span not in terms:
                    terms.append(span)
            else:
                for i in range(len(span) - 1):
                    bg = span[i:i + 2]
                    if bg not in terms:
                        terms.append(bg)
                if len(span) <= 3:
                    for ch in span:
                        if ch not in terms:
                            terms.append(ch)
        return terms

    def pick_entry(self, group_id: str, keyword: str = "") -> Optional[PickedEmoji]:
        gid = str(group_id)
        with self._read() as conn:
            if conn is None:
                return None
            local_img_rows = conn.execute(
                "SELECT i.sha256, i.rel_path, i.tag, i.description, l.added_at AS ts, "
                "i.use_count, i.last_used_at "
                "FROM emoji_group_links l JOIN emoji_items i ON i.sha256 = l.sha256 "
                "WHERE l.group_id = ?",
                (gid,),
            ).fetchall()
            local_face_rows = conn.execute(
                "SELECT face_id, tag, description, updated_at AS ts, use_count, last_used_at "
                "FROM emoji_faces WHERE group_id = ?",
                (gid,),
            ).fetchall()
            global_img_rows = conn.execute(
                "SELECT sha256, rel_path, tag, description, updated_at AS ts, use_count, last_used_at "
                "FROM emoji_items"
            ).fetchall()

        candidates: list[dict[str, Any]] = []
        local_digests: set[str] = set()
        for r in local_img_rows:
            resolved = self._resolve_rel_path(r["rel_path"])
            if resolved is None:
                continue
            digest = str(r["sha256"])
            local_digests.add(digest)
            candidates.append({
                "kind": "image",
                "sha256": digest,
                "path": str(resolved),
                "tag": str(r["tag"] or ""),
                "description": str(r["description"] or ""),
                "ts": float(r["ts"] or 0.0),
                "use_count": int(r["use_count"] or 0),
                "last_used_at": float(r["last_used_at"] or 0.0),
                "is_local": True,
            })
        for r in local_face_rows:
            candidates.append({
                "kind": "face",
                "face_id": str(r["face_id"]),
                "tag": str(r["tag"] or ""),
                "description": str(r["description"] or ""),
                "ts": float(r["ts"] or 0.0),
                "use_count": int(r["use_count"] or 0),
                "last_used_at": float(r["last_used_at"] or 0.0),
                "is_local": True,
            })
        for r in global_img_rows:
            digest = str(r["sha256"])
            if digest in local_digests:
                continue
            resolved = self._resolve_rel_path(r["rel_path"])
            if resolved is None:
                continue
            candidates.append({
                "kind": "image",
                "sha256": digest,
                "path": str(resolved),
                "tag": str(r["tag"] or ""),
                "description": str(r["description"] or ""),
                "ts": float(r["ts"] or 0.0),
                "use_count": int(r["use_count"] or 0),
                "last_used_at": float(r["last_used_at"] or 0.0),
                "is_local": False,
            })

        if not candidates:
            return None

        terms = self._keyword_terms(keyword)
        scored_matched = False
        pool: list[dict[str, Any]] = []
        if terms:
            scored: list[tuple[float, float, dict[str, Any]]] = []
            for item in candidates:
                hay = "{} {}".format(item["tag"], item["description"]).lower()
                base_score = sum(1.0 if len(w) >= 2 else 0.3 for w in terms if w in hay)
                if base_score > 0:
                    affinity = base_score + (0.25 if item["is_local"] else 0.0)
                    item["affinity"] = affinity
                    scored.append((affinity, item["ts"], item))
            if scored:
                scored_matched = True
                max_score = max(r[0] for r in scored)
                threshold = max_score * 0.6
                pool = [
                    r[2]
                    for r in sorted(
                        (row for row in scored if row[0] >= threshold),
                        key=lambda row: (row[0], row[1]),
                        reverse=True,
                    )
                ][:8]
        if not scored_matched:
            for item in candidates:
                item["affinity"] = 0.25 if item["is_local"] else 0.0
            pool = sorted(candidates, key=lambda x: x["ts"])[-8:]
        if not pool:
            return None

        # 同池存在真实图片表情包时优先发真实图片，而非 QQ 原生小黄脸
        img_pool = [item for item in pool if item["kind"] == "image"]
        if img_pool:
            pool = img_pool

        with self._lock:
            last_key = self._last_picked.get(gid)
        if last_key and len(pool) > 1:
            filtered = [
                item
                for item in pool
                if (("face:" + item["face_id"]) if item["kind"] == "face" else item["sha256"]) != last_key
            ]
            if filtered:
                pool = filtered

        cur = time.time()
        weights = []
        for item in pool:
            use_cnt = max(0, int(item.get("use_count") or 0))
            last_used = float(item.get("last_used_at") or 0.0)
            recency_penalty = 0.25 if (last_used > 0.0 and cur - last_used < 300.0) else 1.0
            aff = float(item.get("affinity") or 0.0)
            weights.append(max(0.05, (aff + 1.0) * recency_penalty / (1.0 + 0.5 * use_cnt)))
        picked = random.choices(pool, weights=weights, k=1)[0]

        picked_key = ("face:" + picked["face_id"]) if picked["kind"] == "face" else picked["sha256"]
        with self._lock:
            self._last_picked[gid] = picked_key

        with self._write() as wconn:
            if wconn is not None:
                if picked["kind"] == "face":
                    wconn.execute(
                        "UPDATE emoji_faces SET use_count = use_count + 1, last_used_at = ? "
                        "WHERE group_id = ? AND face_id = ?",
                        (cur, gid, picked["face_id"]),
                    )
                else:
                    wconn.execute(
                        "UPDATE emoji_items SET use_count = use_count + 1, last_used_at = ? WHERE sha256 = ?",
                        (cur, picked["sha256"]),
                    )

        tag = str(picked["tag"]).strip()
        desc = str(picked["description"]).strip()
        if picked["kind"] == "face":
            return PickedEmoji(kind="face", face_id=picked["face_id"], tag=tag, description=desc)
        return PickedEmoji(kind="image", path=picked["path"], tag=tag, description=desc)

    def pick(self, group_id: str, keyword: str = "") -> Optional[str | Path]:
        picked = self.pick_entry(group_id, keyword)
        if picked is None:
            return None
        if picked.kind == "face":
            return "face:" + picked.face_id
        return Path(picked.path)

    def cleanup(self, max_age_days: Optional[int] = None, now: Optional[float] = None) -> None:
        days = self.max_age_days if max_age_days is None else max_age_days
        cur = time.time() if now is None else float(now)
        cutoff = cur - days * 86400
        stale_rel_paths: list[str] = []
        with self._write() as conn:
            if conn is None:
                return
            conn.execute("DELETE FROM emoji_group_links WHERE added_at < ?", (cutoff,))
            conn.execute("DELETE FROM emoji_faces WHERE updated_at < ?", (cutoff,))
            expired_items = conn.execute(
                "SELECT sha256, rel_path FROM emoji_items WHERE updated_at < ?", (cutoff,)
            ).fetchall()
            for row in expired_items:
                conn.execute("DELETE FROM emoji_group_links WHERE sha256 = ?", (row["sha256"],))
                conn.execute("DELETE FROM emoji_items WHERE sha256 = ?", (row["sha256"],))
                if row["rel_path"]:
                    stale_rel_paths.append(str(row["rel_path"]))
        self._unlink_unreferenced(stale_rel_paths)

    def stats(self, group_id: Optional[str] = None) -> dict[str, int]:
        with self._read() as conn:
            if conn is None:
                return {}
            if group_id:
                gid = str(group_id)
                n_img = int(conn.execute("SELECT COUNT(*) FROM emoji_group_links WHERE group_id = ?", (gid,)).fetchone()[0])
                n_face = int(conn.execute("SELECT COUNT(*) FROM emoji_faces WHERE group_id = ?", (gid,)).fetchone()[0])
                return {gid: n_img + n_face}
            out: dict[str, int] = {}
            for r in conn.execute("SELECT group_id, COUNT(*) n FROM emoji_group_links GROUP BY group_id").fetchall():
                out[str(r["group_id"])] = out.get(str(r["group_id"]), 0) + int(r["n"] or 0)
            for r in conn.execute("SELECT group_id, COUNT(*) n FROM emoji_faces GROUP BY group_id").fetchall():
                out[str(r["group_id"])] = out.get(str(r["group_id"]), 0) + int(r["n"] or 0)
            n_global = int(conn.execute("SELECT COUNT(*) FROM emoji_items").fetchone()[0])
            if n_global or out:
                out[GLOBAL_POOL_KEY] = n_global
            return out

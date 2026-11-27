"""QQ 号归一人物档案、别名解析、结构化原子事实与中英子串/词边界匹配。"""

from __future__ import annotations

import re
import sqlite3
import time
from typing import Any, Optional, Sequence

from ..domain import clean_speaker_name

MAX_PERSON_FACTS_PER_FIELD = 12
_PLACEHOLDER_VALUES = frozenset({"-", "—", "–", "无", "暂无", "未知", "none", "null", "n/a"})


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


def _split_atomic_facts(raw: str) -> list[str]:
    out: list[str] = []
    for part in re.split(r"[；;。\n]+", str(raw or "")):
        s = part.strip()
        if s and s.lower() not in _PLACEHOLDER_VALUES:
            out.append(s)
    return out


class PersonStoreOps:
    """Repository 的人物档案、结构化事实与别名匹配混入层。"""

    def _backfill_person_facts_locked(self, conn: sqlite3.Connection) -> None:
        rows = conn.execute(
            "SELECT group_id, user_id, traits, preferences, events, relation, updated_at FROM persons"
        ).fetchall()
        for row in rows:
            gid = str(row["group_id"])
            uid = str(row["user_id"])
            stamp = float(row["updated_at"] or 0.0) or time.time()
            for field in ("traits", "preferences", "events", "relation"):
                raw = str(row[field] or "").strip()
                if not raw:
                    continue
                exists = conn.execute(
                    "SELECT 1 FROM person_facts WHERE group_id = ? AND user_id = ? AND field = ? LIMIT 1",
                    (gid, uid, field),
                ).fetchone()
                if exists:
                    continue
                for fact in _split_atomic_facts(raw):
                    conn.execute(
                        "INSERT INTO person_facts(group_id, user_id, field, fact, hits, updated_at) "
                        "VALUES (?, ?, ?, ?, 1, ?)",
                        (gid, uid, field, fact[:200], stamp),
                    )

    @staticmethod
    def _join_whole_facts(facts: Sequence[str], cap: int) -> str:
        clean = [str(f).strip() for f in facts if str(f or "").strip()]
        if not clean:
            return ""
        joined = "；".join(clean)
        while len(joined) > cap and len(clean) > 1:
            clean.pop(0)
            joined = "；".join(clean)
        return joined[:cap] if len(joined) > cap else joined

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
        with self.write_conn() as conn:
            if conn is None:
                return
            if raw_name and raw_name != uid and raw_name != "群友":
                conn.execute(
                    "INSERT INTO person_aliases(group_id, user_id, name, kind, hits, updated_at) "
                    "VALUES (?, ?, ?, 'nick', 1, ?) "
                    "ON CONFLICT(group_id, user_id, name) DO UPDATE SET "
                    "hits = hits + 1, updated_at = excluded.updated_at",
                    (str(group_id), uid, raw_name, stamp),
                )
            conn.execute(
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
        with self.read_conn() as conn:
            if conn is None:
                return None
            direct_uid = conn.execute(
                "SELECT user_id FROM persons WHERE group_id = ? AND user_id = ? LIMIT 1",
                (str(group_id), raw),
            ).fetchone()
            if direct_uid:
                return str(direct_uid["user_id"])
            for cand in candidates:
                row = conn.execute(
                    "SELECT user_id FROM person_aliases WHERE group_id = ? AND name = ? "
                    "ORDER BY hits DESC LIMIT 1",
                    (str(group_id), cand),
                ).fetchone()
                if row:
                    return str(row["user_id"])
            rows = conn.execute(
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
        gid = str(group_id)
        stamp = time.time() if now is None else float(now)
        limits = (("traits", 600), ("preferences", 400), ("events", 600), ("relation", 200))
        with self.write_conn() as conn:
            if conn is None:
                return {}
            row = conn.execute(
                "SELECT * FROM persons WHERE group_id = ? AND user_id = ?",
                (gid, uid),
            ).fetchone()
            current = dict(row) if row else {}
            merged: dict[str, Any] = {}
            for key, cap in limits:
                existing_rows = [
                    dict(r)
                    for r in conn.execute(
                        "SELECT id, fact, hits, updated_at FROM person_facts "
                        "WHERE group_id = ? AND user_id = ? AND field = ? ORDER BY id ASC",
                        (gid, uid, key),
                    ).fetchall()
                ]
                prev_val = str(current.get(key) or "").strip()
                if not existing_rows and prev_val:
                    for seed_fact in _split_atomic_facts(prev_val):
                        cur_ins = conn.execute(
                            "INSERT INTO person_facts(group_id, user_id, field, fact, hits, updated_at) "
                            "VALUES (?, ?, ?, ?, 1, ?)",
                            (gid, uid, key, seed_fact[:200], float(current.get("updated_at") or stamp)),
                        )
                        existing_rows.append({
                            "id": int(cur_ins.lastrowid or 0),
                            "fact": seed_fact[:200],
                            "hits": 1,
                            "updated_at": float(current.get("updated_at") or stamp),
                        })

                incoming_items = _split_atomic_facts(fields.get(key) or "") if key in fields else []
                if key in fields and not merge:
                    conn.execute(
                        "DELETE FROM person_facts WHERE group_id = ? AND user_id = ? AND field = ?",
                        (gid, uid, key),
                    )
                    existing_rows = []
                    for item in incoming_items:
                        f_str = item[:200]
                        cur_ins = conn.execute(
                            "INSERT INTO person_facts(group_id, user_id, field, fact, hits, updated_at) "
                            "VALUES (?, ?, ?, ?, 1, ?)",
                            (gid, uid, key, f_str, stamp),
                        )
                        existing_rows.append({
                            "id": int(cur_ins.lastrowid or 0),
                            "fact": f_str,
                            "hits": 1,
                            "updated_at": stamp,
                        })
                elif incoming_items:
                    for item in incoming_items:
                        f_str = item[:200]
                        matched_idx: Optional[int] = None
                        for idx, er in enumerate(existing_rows):
                            prev_f = str(er["fact"])
                            sim = _similarity(f_str, prev_f)
                            if f_str == prev_f or f_str in prev_f or prev_f in f_str or sim >= 0.65:
                                matched_idx = idx
                                break
                        if matched_idx is not None:
                            er = existing_rows[matched_idx]
                            new_text = f_str if len(f_str) > len(str(er["fact"])) else str(er["fact"])
                            er["fact"] = new_text
                            er["hits"] = int(er["hits"] or 1) + 1
                            er["updated_at"] = stamp
                            conn.execute(
                                "UPDATE person_facts SET fact = ?, hits = ?, updated_at = ? WHERE id = ?",
                                (new_text, er["hits"], stamp, er["id"]),
                            )
                        else:
                            cur_ins = conn.execute(
                                "INSERT INTO person_facts(group_id, user_id, field, fact, hits, updated_at) "
                                "VALUES (?, ?, ?, ?, 1, ?)",
                                (gid, uid, key, f_str, stamp),
                            )
                            existing_rows.append({
                                "id": int(cur_ins.lastrowid or 0),
                                "fact": f_str,
                                "hits": 1,
                                "updated_at": stamp,
                            })
                    if len(existing_rows) > MAX_PERSON_FACTS_PER_FIELD:
                        drop_pool = sorted(
                            existing_rows,
                            key=lambda r: (float(r.get("updated_at") or 0.0), int(r.get("hits") or 0), int(r.get("id") or 0)),
                        )
                        drop_ids = {int(r["id"]) for r in drop_pool[: len(existing_rows) - MAX_PERSON_FACTS_PER_FIELD]}
                        for did in drop_ids:
                            conn.execute("DELETE FROM person_facts WHERE id = ?", (did,))
                        existing_rows = [r for r in existing_rows if int(r["id"]) not in drop_ids]

                merged[key] = self._join_whole_facts([str(r["fact"]) for r in existing_rows], cap)

            if not current and "intimacy" not in fields and not any(merged[k] for k, _ in limits):
                return {}
            intimacy = float(fields.get("intimacy", current.get("intimacy", 0.5)))
            merged["intimacy"] = max(0.0, min(1.0, intimacy))
            conn.execute(
                "INSERT INTO persons"
                "(group_id, user_id, traits, preferences, events, relation, intimacy, "
                "first_seen, last_seen, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(group_id, user_id) DO UPDATE SET "
                "traits = excluded.traits, preferences = excluded.preferences, "
                "events = excluded.events, relation = excluded.relation, "
                "intimacy = excluded.intimacy, updated_at = excluded.updated_at",
                (
                    gid,
                    uid,
                    merged["traits"],
                    merged["preferences"],
                    merged["events"],
                    merged["relation"],
                    merged["intimacy"],
                    float(current.get("first_seen") or stamp),
                    float(current.get("last_seen") or stamp),
                    stamp,
                ),
            )
        return merged

    def person_facts(
        self,
        group_id: str,
        user_id: str,
        field: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        uid = str(user_id or "").strip()
        if not uid:
            return []
        with self.read_conn() as conn:
            if conn is None:
                return []
            if field:
                rows = conn.execute(
                    "SELECT field, fact, hits, updated_at FROM person_facts "
                    "WHERE group_id = ? AND user_id = ? AND field = ? ORDER BY updated_at ASC, id ASC",
                    (str(group_id), uid, str(field)),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT field, fact, hits, updated_at FROM person_facts "
                    "WHERE group_id = ? AND user_id = ? ORDER BY updated_at ASC, id ASC",
                    (str(group_id), uid),
                ).fetchall()
        return [dict(r) for r in rows]

    def person_summary(self, group_id: str, user_id: str) -> str:
        uid = str(user_id or "").strip()
        if not uid:
            return ""
        with self.read_conn() as conn:
            if conn is None:
                return ""
            fact_rows = conn.execute(
                "SELECT field, fact FROM person_facts "
                "WHERE group_id = ? AND user_id = ? ORDER BY id ASC",
                (str(group_id), uid),
            ).fetchall()
            row = None if fact_rows else conn.execute(
                "SELECT traits, preferences, events, relation FROM persons "
                "WHERE group_id = ? AND user_id = ?",
                (str(group_id), uid),
            ).fetchone()
        field_defs = (("traits", "印象", 600), ("preferences", "偏好", 400), ("events", "相关的事", 600), ("relation", "关系", 200))
        if fact_rows:
            grouped: dict[str, list[str]] = {k: [] for k, _, _ in field_defs}
            for fr in fact_rows:
                fk = str(fr["field"] or "")
                fv = str(fr["fact"] or "").strip()
                if fk in grouped and fv:
                    grouped[fk].append(fv)
            bits = []
            for key, label, cap in field_defs:
                val = self._join_whole_facts(grouped[key], cap)
                if val:
                    bits.append("{}：{}".format(label, val))
            return "；".join(bits)
        if not row:
            return ""
        bits = []
        for key, label, cap in field_defs:
            val = self._join_whole_facts(_split_atomic_facts(row[key]), cap)
            if val:
                bits.append("{}：{}".format(label, val))
        return "；".join(bits)

    def person_aliases(self, group_id: str, user_id: str) -> list[str]:
        with self.read_conn() as conn:
            if conn is None:
                return []
            rows = conn.execute(
                "SELECT name FROM person_aliases WHERE group_id = ? AND user_id = ? ORDER BY hits DESC LIMIT 8",
                (str(group_id), str(user_id)),
            ).fetchall()
        return [str(r["name"]) for r in rows]

    def find_mentioned_persons(
        self,
        group_id: str,
        text: str,
        *,
        exclude_uids: Sequence[str] = (),
        limit: int = 2,
    ) -> list[tuple[str, str]]:
        hay = str(text or "")
        if not hay or limit <= 0:
            return []
        hay_low = hay.lower()
        excluded = {str(u) for u in (exclude_uids or ()) if u}
        with self.read_conn() as conn:
            if conn is None:
                return []
            rows = conn.execute(
                "SELECT user_id, name FROM person_aliases WHERE group_id = ? "
                "ORDER BY LENGTH(name) DESC, hits DESC",
                (str(group_id),),
            ).fetchall()
        found: list[tuple[str, str]] = []
        skip_names = {"大肥鱼", "回声", "群友"}
        for row in rows:
            uid = str(row["user_id"] or "")
            raw_alias = str(row["name"] or "").strip()
            if not uid or uid in excluded:
                continue
            for alias in dict.fromkeys((raw_alias, clean_speaker_name(raw_alias))):
                if len(alias) < 2 or alias in skip_names:
                    continue
                alow = alias.lower()
                if re.fullmatch(r"[a-z0-9_]+", alow):
                    matched = bool(re.search(r"(?<![a-z0-9_]){}(?![a-z0-9_])".format(re.escape(alow)), hay_low))
                else:
                    matched = alow in hay_low
                if matched:
                    excluded.add(uid)
                    found.append((uid, raw_alias))
                    break
            if len(found) >= limit:
                break
        return found

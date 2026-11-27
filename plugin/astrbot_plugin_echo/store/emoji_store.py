"""表情包存储：按群收录真表情、VLM 非表情拦截、情绪标签与防连发挑选。"""

from __future__ import annotations

import hashlib
import json
import os
import random
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from ..domain import FACE_LABELS, LLMClient, detect_image_format

ANALYZE_PROMPT = (
    "先判断这是不是适合群聊日常使用的正向/中性表情包或梗图；如果是普通截图、聊天记录、网页、图表、表格、代码、文档、"
    "证件、真人照片，或低俗、露骨、色情、恶心、攻击性图片，只输出：非表情。\n"
    "如果是合适的表情包，必须严格仅输出一行：2到4字情绪词|一句话描述它在表达什么。\n"
    "例：无语|一只猫翻白眼。禁止任何解释、序号、Markdown或“这是一张...”等前缀。"
)
FILTER_WORDS = ("二维码", "付款码", "身份证", "验证码", "密码", "银行卡号")
NON_EMOJI_WORDS = ("非表情", "不是表情")
_BAD_EMOJI_WORDS = ("低俗", "色情", "黄图", "露骨", "性暗示", "nsfw", "porn")
_BAD_TAG_PREFIXES = ("这是", "这张", "那个", "一个", "口语化", "截图", "聊天", "网页", "图片", "画面")


def _clean_tag(raw: Any) -> str:
    return re.sub(r"[*`\[\]【】()（）「」\"'：:]+", "", str(raw or "")).strip()


def _is_valid_tag(tag: Any) -> bool:
    raw = str(tag or "").strip()
    if not raw or not (1 <= len(raw) <= 6):
        return False
    if re.search(r"[\s:：()（）\[\]【】*`]", raw):
        return False
    if raw in NON_EMOJI_WORDS or raw.endswith(("的", "了", "着")):
        return False
    low = raw.lower()
    if any(w in low for w in _BAD_EMOJI_WORDS):
        return False
    if any(raw.startswith(p) for p in _BAD_TAG_PREFIXES):
        return False
    return True


def analyze_emoji(llm: LLMClient, image_bytes: bytes) -> tuple[str, str]:
    """调用模型视觉能力判断是否为表情并打标签（严格单行 格式|校验，fail-closed）。"""
    if not image_bytes:
        return "", "随手"
    try:
        raw = str(llm.describe_image(image_bytes, ANALYZE_PROMPT) or "").strip()
    except Exception:
        return "", "随手"
    if not raw:
        return "", "随手"
    low = raw.lower()
    if raw.startswith(NON_EMOJI_WORDS) or any(w in raw for w in NON_EMOJI_WORDS) or any(w in low for w in _BAD_EMOJI_WORDS):
        return "非表情", "非表情"
    first_line = next((ln.strip() for ln in raw.splitlines() if ln.strip()), "")
    if "|" not in first_line and "｜" not in first_line:
        return "非表情", "非表情"
    left, right = re.split(r"[|｜]", first_line, maxsplit=1)
    label = _clean_tag(left)
    desc = str(right or "").strip()
    if not _is_valid_tag(label) or not desc or any(w in desc for w in NON_EMOJI_WORDS) or any(w in desc.lower() for w in _BAD_EMOJI_WORDS):
        return "非表情", "非表情"
    return desc, label


def acceptable(description: str, image_bytes: Optional[bytes] = None) -> bool:
    """fail-closed 内容过滤：空描述、非表情、低俗或含敏感信息一律拒收。"""
    text = str(description or "").strip()
    if not text:
        return False
    low = text.lower()
    if any(w in text for w in NON_EMOJI_WORDS):
        return False
    if any(w in low for w in _BAD_EMOJI_WORDS):
        return False
    if any(w in text for w in FILTER_WORDS):
        return False
    if image_bytes and len(image_bytes) > 2 * 1024 * 1024:
        return False
    return True


class EmojiStore:
    """按群隔离的表情库：兼容已有 index.json / digest_cache.json 落盘格式。"""

    def __init__(self, root: str | Path, limit: int = 64, max_age_days: int = 30):
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)
        self.index_path = self._root / "index.json"
        self.cache_path = self._root / "digest_cache.json"
        self.limit = limit
        self.max_age_days = max_age_days
        self._lock = threading.RLock()
        self._inflight: set[str] = set()
        self._last_picked: dict[str, str] = {}
        self._index: dict[str, list[dict[str, Any]]] = {}
        if self.index_path.exists():
            try:
                loaded = json.loads(self.index_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    self._index = loaded
            except (ValueError, OSError):
                self._index = {}
        self._digest_cache: dict[str, dict[str, Any]] = {}
        if self.cache_path.exists():
            try:
                cached = json.loads(self.cache_path.read_text(encoding="utf-8"))
                if isinstance(cached, dict):
                    self._digest_cache = cached
            except (ValueError, OSError):
                self._digest_cache = {}
        self._migrate_and_dedup()

    @property
    def root(self) -> Path:
        return self._root

    @staticmethod
    def _atomic_write(path: Path, text: str) -> None:
        tmp = path.parent / ".{}.{}.tmp".format(path.name, uuid.uuid4().hex[:8])
        tmp.write_text(text, encoding="utf-8")
        os.replace(str(tmp), str(path))

    def _safe_path(self, path_str: Any) -> Optional[Path]:
        if not path_str:
            return None
        try:
            root = self._root.resolve(strict=False)
            p = Path(str(path_str))
            if p.is_symlink():
                return None
            resolved = p.resolve(strict=False)
            if root not in resolved.parents:
                return None
            return p if p.is_file() else None
        except OSError:
            return None

    def _save(self) -> None:
        with self._lock:
            self._atomic_write(self.index_path, json.dumps(self._index, ensure_ascii=False, indent=2))

    def _save_cache(self) -> None:
        with self._lock:
            self._atomic_write(self.cache_path, json.dumps(self._digest_cache, ensure_ascii=False, indent=2))

    def _migrate_and_dedup(self) -> None:
        """按 sha256 去重、过滤非法 face_id 与脏标签、清理缺失/不合格文件。"""
        changed_index = False
        changed_cache = False
        with self._lock:
            for digest, cached in list(self._digest_cache.items()):
                if isinstance(cached, dict) and cached.get("ok"):
                    if not _is_valid_tag(cached.get("tag")) or not acceptable(cached.get("description", "")):
                        cached["ok"] = False
                        cached["tag"] = ""
                        cached["description"] = ""
                        changed_cache = True

            for gid, entries in list(self._index.items()):
                if not isinstance(entries, list):
                    continue
                by_digest: dict[str, tuple[int, dict[str, Any]]] = {}
                drop_indices: set[int] = set()
                for idx, item in enumerate(entries):
                    if not isinstance(item, dict):
                        drop_indices.add(idx)
                        changed_index = True
                        continue
                    if item.get("face_id") and not item.get("path"):
                        fid = str(item.get("face_id") or "").strip()
                        if fid not in FACE_LABELS:
                            drop_indices.add(idx)
                            changed_index = True
                            continue
                        if not item.get("tag") or item.get("tag") == "QQ表情":
                            item["tag"] = FACE_LABELS[fid]
                            item["description"] = FACE_LABELS[fid]
                            changed_index = True
                        continue
                    p_str = str(item.get("path") or "")
                    safe = self._safe_path(p_str)
                    if not p_str or safe is None:
                        drop_indices.add(idx)
                        changed_index = True
                        continue
                    digest = str(item.get("sha256") or "").strip()
                    if not digest:
                        try:
                            digest = hashlib.sha256(safe.read_bytes()).hexdigest()
                        except OSError:
                            drop_indices.add(idx)
                            changed_index = True
                            continue
                        item["sha256"] = digest
                        changed_index = True
                    if not _is_valid_tag(item.get("tag")) or not acceptable(item.get("description", "")):
                        drop_indices.add(idx)
                        changed_index = True
                        try:
                            safe.unlink(missing_ok=True)
                        except OSError:
                            pass
                        self._digest_cache[digest] = {
                            "ok": False,
                            "tag": "",
                            "description": "",
                            "ts": float(item.get("ts") or time.time()),
                        }
                        changed_cache = True
                        continue
                    prev = by_digest.get(digest)
                    if prev is None:
                        by_digest[digest] = (idx, item)
                        continue
                    changed_index = True
                    prev_idx, prev_item = prev
                    if float(item.get("ts") or 0) >= float(prev_item.get("ts") or 0):
                        drop_indices.add(prev_idx)
                        old_path = prev_item.get("path")
                        by_digest[digest] = (idx, item)
                    else:
                        drop_indices.add(idx)
                        old_path = item.get("path")
                    kept_path = by_digest[digest][1].get("path")
                    if old_path and old_path != kept_path:
                        try:
                            Path(old_path).unlink(missing_ok=True)
                        except OSError:
                            pass
                if drop_indices:
                    self._index[gid] = [it for i, it in enumerate(entries) if i not in drop_indices]
                for _, item in by_digest.values():
                    digest = item.get("sha256")
                    if digest and digest not in self._digest_cache:
                        self._digest_cache[digest] = {
                            "ok": True,
                            "tag": str(item.get("tag") or "随手"),
                            "description": str(item.get("description") or "")[:80],
                            "ts": float(item.get("ts") or time.time()),
                        }
                        changed_cache = True
            if changed_index:
                self._save()
            if changed_cache:
                self._save_cache()

    def _folder(self, group_id: str) -> Path:
        folder = self._root / re.sub(r"[^\w-]", "_", group_id)[:60]
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    def _store_entry(
        self,
        group_id: str,
        image_bytes: bytes,
        digest: str,
        label: str,
        description: str,
        cap: int,
        now: float,
    ) -> None:
        _, ext = detect_image_format(image_bytes)
        path = self._folder(group_id) / "{}_{}{}".format(int(now * 1000), digest[:8], ext)
        path.write_bytes(image_bytes)
        entries = self._index.setdefault(group_id, [])
        entries.append({
            "path": str(path),
            "sha256": digest,
            "tag": label,
            "description": description[:80],
            "ts": now,
        })
        while len(entries) > cap:
            old = entries.pop(0)
            if old.get("path"):
                Path(old["path"]).unlink(missing_ok=True)
        self._save()

    def collect(self, group_id: str, image_bytes: bytes, llm: LLMClient, limit: Optional[int] = None) -> str:
        if not image_bytes or len(image_bytes) > 2 * 1024 * 1024:
            return ""
        digest = hashlib.sha256(image_bytes).hexdigest()
        cap = limit or self.limit
        now = time.time()

        with self._lock:
            entries = self._index.setdefault(group_id, [])
            for item in entries:
                if item.get("sha256") == digest and self._safe_path(item.get("path")) is not None:
                    item["ts"] = now
                    self._save()
                    return str(item.get("tag") or "随手")
            cached = self._digest_cache.get(digest)
            if cached is not None:
                if not cached.get("ok"):
                    return ""
                label = str(cached.get("tag") or "随手")
                desc = str(cached.get("description") or "")
                self._store_entry(group_id, image_bytes, digest, label, desc, cap, now)
                return label
            if digest in self._inflight:
                return ""
            self._inflight.add(digest)

        ok = False
        description = ""
        label = "随手"
        try:
            description, label = analyze_emoji(llm, image_bytes)
            ok = acceptable(description, image_bytes)
        except Exception:
            description = ""
            ok = False
        finally:
            now = time.time()
            with self._lock:
                self._inflight.discard(digest)
                if str(description or "").strip():
                    self._digest_cache[digest] = {
                        "ok": bool(ok),
                        "tag": label if ok else "",
                        "description": description[:80] if ok else "",
                        "ts": now,
                    }
                    if len(self._digest_cache) > 500:
                        oldest = sorted(
                            self._digest_cache.items(),
                            key=lambda kv: float((kv[1] or {}).get("ts") or 0),
                        )
                        for old_key, _ in oldest[:len(self._digest_cache) - 500]:
                            self._digest_cache.pop(old_key, None)
                    self._save_cache()
                if ok:
                    entries = self._index.setdefault(group_id, [])
                    already = any(
                        item.get("sha256") == digest and self._safe_path(item.get("path")) is not None
                        for item in entries
                    )
                    if not already:
                        self._store_entry(group_id, image_bytes, digest, label, description[:80], cap, now)
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
        with self._lock:
            entries = self._index.setdefault(group_id, [])
            for item in entries:
                if str(item.get("face_id") or "") == fid:
                    item["ts"] = time.time()
                    if item.get("tag") in ("", "QQ表情"):
                        item["tag"] = label
                        item["description"] = label
                    self._save()
                    return str(item.get("tag") or canonical)
            entries.append({"face_id": fid, "tag": label, "description": label, "ts": time.time()})
            while len(entries) > self.limit:
                old = entries.pop(0)
                if old.get("path"):
                    Path(old["path"]).unlink(missing_ok=True)
            self._save()
            return label

    def remove_face(self, group_id: str, face_id: str) -> bool:
        """发送 QQ 原生表情失败时踢出失效 face_id，防止后续重复报错 1200。"""
        fid = str(face_id or "").strip()
        if not fid:
            return False
        with self._lock:
            entries = list(self._index.get(group_id) or [])
            kept = [item for item in entries if str(item.get("face_id") or "") != fid]
            if len(kept) == len(entries):
                return False
            self._index[group_id] = kept
            self._save()
            return True

    def has_entries(self, group_id: str) -> bool:
        with self._lock:
            entries = list(self._index.get(group_id) or [])
        return any(
            item.get("face_id") or self._safe_path(item.get("path")) is not None
            for item in entries
            if isinstance(item, dict)
        )

    def tags(self, group_id: str, limit: int = 12) -> list[str]:
        with self._lock:
            entries = list(self._index.get(group_id) or [])
        out: list[str] = []
        seen: set[str] = set()
        for item in reversed(entries):
            if not isinstance(item, dict):
                continue
            if not (item.get("face_id") or self._safe_path(item.get("path")) is not None):
                continue
            tag = str(item.get("tag") or "").strip()
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

    def pick(self, group_id: str, keyword: str = "") -> Optional[str | Path]:
        with self._lock:
            raw_entries = list(self._index.get(group_id) or [])
        entries = [
            item
            for item in raw_entries
            if isinstance(item, dict) and (item.get("face_id") or self._safe_path(item.get("path")) is not None)
        ]
        if not entries:
            return None
        terms = self._keyword_terms(keyword)
        scored_matched = False
        if terms:
            scored: list[tuple[float, float, dict[str, Any]]] = []
            for item in entries:
                hay = "{} {}".format(item.get("tag", ""), item.get("description", "")).lower()
                score = sum(1.0 if len(w) >= 2 else 0.3 for w in terms if w in hay)
                if score > 0:
                    scored.append((score, float(item.get("ts") or 0.0), item))
            if scored:
                scored_matched = True
                max_score = max(r[0] for r in scored)
                threshold = max_score * 0.6
                entries = [
                    r[2]
                    for r in sorted(
                        (row for row in scored if row[0] >= threshold),
                        key=lambda row: (row[0], row[1]),
                        reverse=True,
                    )
                ]
        pool = entries[:8] if scored_matched else sorted(entries, key=lambda x: float(x.get("ts") or 0.0))[-8:]
        if not pool:
            return None
        last_key = self._last_picked.get(group_id)
        if last_key and len(pool) > 1:
            filtered = [
                item
                for item in pool
                if (("face:" + str(item["face_id"])) if item.get("face_id") else str(item.get("path"))) != last_key
            ]
            if filtered:
                pool = filtered
        picked = random.choice(pool)
        picked_key = ("face:" + str(picked["face_id"])) if picked.get("face_id") else str(picked["path"])
        self._last_picked[group_id] = picked_key
        if picked.get("face_id"):
            return "face:" + str(picked["face_id"])
        return Path(picked["path"])

    def cleanup(self, max_age_days: Optional[int] = None) -> None:
        days = self.max_age_days if max_age_days is None else max_age_days
        cutoff = time.time() - days * 86400
        with self._lock:
            for gid, entries in list(self._index.items()):
                kept = []
                for item in entries:
                    if float(item.get("ts") or 0) < cutoff:
                        if item.get("path"):
                            try:
                                Path(item["path"]).unlink(missing_ok=True)
                            except OSError:
                                pass
                    else:
                        kept.append(item)
                self._index[gid] = kept
            self._save()

    def stats(self, group_id: Optional[str] = None) -> dict[str, int]:
        with self._lock:
            if group_id:
                return {group_id: len(self._index.get(group_id, []))}
            return {k: len(v) for k, v in self._index.items()}

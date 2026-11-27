"""表情包存储：内容寻址全局共享高质量表情池、VLM 门禁、情绪标签与防连发挑选。"""

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

from ..domain import FACE_LABELS, LLMClient, PickedEmoji, detect_image_dimensions, detect_image_format

STICKER_MAX_DIM = 1280
STICKER_MAX_ASPECT = 1.85
GLOBAL_POOL_KEY = "_global"
DEFAULT_GLOBAL_LIMIT = 160

ANALYZE_PROMPT = (
    "先严格分类这张图是不是适合日常群聊的真正表情包或搞笑梗图，仅输出一行 JSON，不要任何解释或 Markdown。\n"
    '输出格式：{"kind":"sticker|chat|screenshot|news|photo|chart|nsfw|other","usable":true,"tag":"2到4字情绪词","desc":"一句话画面描述"}\n'
    "严格标准：\n"
    "1. 只有熊猫头、萌宠/动漫/卡通配文梗图、搞笑动图，才允许填 kind=\"sticker\" 且 usable=true；\n"
    "2. 只要是手机或电脑截屏、微信/QQ聊天记录截图、带真人头像/群聊昵称/私人隐私文字的截图、新闻报道页面、网页、表格/PPT/图表、代码/终端日志、普通生活照/自拍、二维码，或低俗/竖中指/露骨图片，"
    '一律填对应的非 sticker 类别，usable 必须为 false，tag 填 "非表情"。'
)
FILTER_WORDS = ("二维码", "付款码", "身份证", "验证码", "密码", "银行卡号")
NON_EMOJI_WORDS = ("非表情", "不是表情")
_BAD_EMOJI_WORDS = ("低俗", "色情", "黄图", "露骨", "性暗示", "nsfw", "porn", "竖起中指", "竖中指")
_NON_STICKER_DESC_WORDS = (
    "新闻报道", "审查调查", "违纪", "聊天记录", "聊天截图", "录屏", "截图", "网页",
    "柱状图", "折线图", "统计图", "表格", "数据图表", "一张图表", "对阵表", "终端",
    "thinking process", "thought process", "思考过程", "token每秒",
    "need chinese", "en?", "must describe",
)
_BAD_TAG_PREFIXES = ("这是", "这张", "那个", "一个", "口语化", "截图", "聊天", "网页", "图片", "画面")


def is_sticker_geometry(image_bytes: Optional[bytes]) -> bool:
    """物理几何门禁：拦截高分辨率截屏与细长手机截图（如 1179x2556），极小测试桩放行。"""
    if not image_bytes:
        return True
    w, h = detect_image_dimensions(image_bytes)
    if w <= 0 or h <= 0:
        return True
    long_side = max(w, h)
    short_side = min(w, h)
    if long_side > STICKER_MAX_DIM:
        return False
    if long_side / float(short_side) > STICKER_MAX_ASPECT:
        return False
    return True


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


def acceptable(description: str, image_bytes: Optional[bytes] = None) -> bool:
    """fail-closed 内容与几何过滤：空描述、非表情、截屏描述、低俗、几何不符或超 2MB 一律拒收。"""
    text = str(description or "").strip()
    if not text:
        return False
    low = text.lower()
    if any(w in text for w in NON_EMOJI_WORDS):
        return False
    if any(w in low for w in _BAD_EMOJI_WORDS):
        return False
    if any(w in low for w in _NON_STICKER_DESC_WORDS):
        return False
    if any(w in text for w in FILTER_WORDS):
        return False
    if image_bytes:
        if len(image_bytes) > 2 * 1024 * 1024 or not is_sticker_geometry(image_bytes):
            return False
    return True


def analyze_emoji(llm: LLMClient, image_bytes: bytes) -> tuple[str, str]:
    """三层表情识别门禁：先验物理尺寸 -> 再经 VLM 结构化分类（JSON 优先，兼容单行 标签|描述）。"""
    if not image_bytes:
        return "", "随手"
    if not is_sticker_geometry(image_bytes):
        return "非表情", "非表情"
    try:
        raw = str(llm.describe_image(image_bytes, ANALYZE_PROMPT) or "").strip()
    except Exception:
        return "", "随手"
    if not raw:
        return "", "随手"

    cleaned_raw = re.sub(r"```(?:json)?", "", raw, flags=re.I).strip()
    m = re.search(r"\{.*\}", cleaned_raw, re.S)
    if m:
        try:
            obj = json.loads(m.group(0))
        except (ValueError, TypeError):
            return "非表情", "非表情"
        if not isinstance(obj, dict):
            return "非表情", "非表情"
        kind = str(obj.get("kind") or "").strip().lower()
        usable = obj.get("usable")
        label = _clean_tag(obj.get("tag"))
        desc = str(obj.get("desc") or obj.get("description") or "").strip()
        if kind != "sticker" or usable is not True or not _is_valid_tag(label) or not acceptable(desc):
            return "非表情", "非表情"
        return desc, label

    low = raw.lower()
    if raw.startswith(NON_EMOJI_WORDS) or any(w in raw for w in NON_EMOJI_WORDS) or any(w in low for w in _BAD_EMOJI_WORDS):
        return "非表情", "非表情"
    first_line = next((ln.strip() for ln in raw.splitlines() if ln.strip()), "")
    if "|" not in first_line and "｜" not in first_line:
        return "非表情", "非表情"
    left, right = re.split(r"[|｜]", first_line, maxsplit=1)
    label = _clean_tag(left)
    desc = str(right or "").strip()
    if not _is_valid_tag(label) or not acceptable(desc):
        return "非表情", "非表情"
    return desc, label


class EmojiStore:
    """跨群全局共享高质量表情池（兼容按群经典 QQ 小黄脸与已有目录无损迁移）。"""

    def __init__(
        self,
        root: str | Path,
        limit: int = 64,
        max_age_days: int = 30,
        global_limit: Optional[int] = None,
    ):
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)
        self.index_path = self._root / "index.json"
        self.cache_path = self._root / "digest_cache.json"
        self.limit = limit
        self.max_age_days = max_age_days
        self.global_limit = max(int(global_limit or DEFAULT_GLOBAL_LIMIT), self.limit * 2)
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

    def _shared_dir(self) -> Path:
        folder = self._root / "shared"
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    def _canonical_shared_path(self, digest: str, image_bytes: bytes) -> Path:
        _, ext = detect_image_format(image_bytes)
        canonical = self._shared_dir() / "{}{}".format(digest[:16], ext)
        if not canonical.is_file():
            canonical.write_bytes(image_bytes)
        return canonical

    def _unlink_if_unreferenced(self, path_str: Any) -> None:
        safe = self._safe_path(path_str)
        if safe is None:
            return
        try:
            target_res = safe.resolve(strict=False)
        except OSError:
            return
        with self._lock:
            for entries in self._index.values():
                if not isinstance(entries, list):
                    continue
                for item in entries:
                    if not isinstance(item, dict):
                        continue
                    other = str(item.get("path") or "")
                    if not other:
                        continue
                    if other == str(path_str):
                        return
                    try:
                        if Path(other).resolve(strict=False) == target_res:
                            return
                    except OSError:
                        continue
        try:
            safe.unlink(missing_ok=True)
        except OSError:
            pass

    def _upsert_global_entry(
        self,
        canonical_path: Path,
        digest: str,
        label: str,
        description: str,
        now: float,
    ) -> list[str]:
        g_entries = self._index.setdefault(GLOBAL_POOL_KEY, [])
        evicted: list[str] = []
        matched = False
        for item in g_entries:
            if not isinstance(item, dict):
                continue
            if item.get("sha256") == digest:
                item["path"] = str(canonical_path)
                item["ts"] = max(float(item.get("ts") or 0.0), now)
                if label and _is_valid_tag(label):
                    item["tag"] = label
                if description:
                    item["description"] = description[:80]
                matched = True
                break
        if not matched:
            g_entries.append({
                "path": str(canonical_path),
                "sha256": digest,
                "tag": label,
                "description": description[:80],
                "ts": now,
            })
        if len(g_entries) > self.global_limit:
            g_entries.sort(key=lambda x: float(x.get("ts") or 0.0) if isinstance(x, dict) else 0.0)
            while len(g_entries) > self.global_limit:
                old = g_entries.pop(0)
                if isinstance(old, dict) and old.get("path"):
                    evicted.append(str(old["path"]))
        return evicted

    def _migrate_and_dedup(self) -> None:
        """按 sha256 去重、统一迁移真表情至 shared/ 并同步构建 _global 共享池，清理脏图与失效 face_id。"""
        changed_index = False
        changed_cache = False
        stale_paths: list[str] = []
        global_merged: dict[str, dict[str, Any]] = {}
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
                    self._index.pop(gid, None)
                    changed_index = True
                    continue
                is_global = gid == GLOBAL_POOL_KEY
                by_digest: dict[str, tuple[int, dict[str, Any]]] = {}
                drop_indices: set[int] = set()
                for idx, item in enumerate(entries):
                    if not isinstance(item, dict):
                        drop_indices.add(idx)
                        changed_index = True
                        continue
                    if item.get("face_id") and not item.get("path"):
                        fid = str(item.get("face_id") or "").strip()
                        if is_global or fid not in FACE_LABELS:
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
                    try:
                        img_bytes = safe.read_bytes()
                    except OSError:
                        drop_indices.add(idx)
                        changed_index = True
                        continue
                    if not digest:
                        digest = hashlib.sha256(img_bytes).hexdigest()
                        item["sha256"] = digest
                        changed_index = True
                    if not _is_valid_tag(item.get("tag")) or not acceptable(item.get("description", ""), img_bytes):
                        drop_indices.add(idx)
                        changed_index = True
                        stale_paths.append(str(safe))
                        self._digest_cache[digest] = {
                            "ok": False,
                            "tag": "",
                            "description": "",
                            "ts": float(item.get("ts") or time.time()),
                        }
                        changed_cache = True
                        continue

                    canonical = self._canonical_shared_path(digest, img_bytes)
                    if str(safe) != str(canonical):
                        stale_paths.append(str(safe))
                        item["path"] = str(canonical)
                        changed_index = True

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
                        stale_paths.append(str(old_path))

                if drop_indices:
                    self._index[gid] = [it for i, it in enumerate(entries) if i not in drop_indices]

                for digest, (_, item) in by_digest.items():
                    if digest not in self._digest_cache:
                        self._digest_cache[digest] = {
                            "ok": True,
                            "tag": str(item.get("tag") or "随手"),
                            "description": str(item.get("description") or "")[:80],
                            "ts": float(item.get("ts") or time.time()),
                        }
                        changed_cache = True
                    cand = {
                        "path": str(item.get("path") or ""),
                        "sha256": digest,
                        "tag": str(item.get("tag") or "随手"),
                        "description": str(item.get("description") or "")[:80],
                        "ts": float(item.get("ts") or time.time()),
                    }
                    prev_g = global_merged.get(digest)
                    if prev_g is None or float(cand["ts"]) >= float(prev_g.get("ts") or 0.0):
                        global_merged[digest] = cand

            if global_merged or GLOBAL_POOL_KEY in self._index:
                merged_list = sorted(global_merged.values(), key=lambda x: float(x.get("ts") or 0.0))
                while len(merged_list) > self.global_limit:
                    old = merged_list.pop(0)
                    if old.get("path"):
                        stale_paths.append(str(old["path"]))
                old_global = self._index.get(GLOBAL_POOL_KEY)
                if old_global != merged_list:
                    self._index[GLOBAL_POOL_KEY] = merged_list
                    changed_index = True

            if changed_index:
                self._save()
            if changed_cache:
                self._save_cache()

        for old_path in stale_paths:
            self._unlink_if_unreferenced(old_path)

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
        canonical = self._canonical_shared_path(digest, image_bytes)
        entries = self._index.setdefault(group_id, [])
        evicted: list[str] = []
        entries.append({
            "path": str(canonical),
            "sha256": digest,
            "tag": label,
            "description": description[:80],
            "ts": now,
        })
        while len(entries) > cap:
            old = entries.pop(0)
            if isinstance(old, dict) and old.get("path"):
                evicted.append(str(old["path"]))
        evicted.extend(self._upsert_global_entry(canonical, digest, label, description, now))
        self._save()
        for old_path in evicted:
            self._unlink_if_unreferenced(old_path)

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
                    canonical = self._canonical_shared_path(digest, image_bytes)
                    item["path"] = str(canonical)
                    evicted = self._upsert_global_entry(
                        canonical,
                        digest,
                        str(item.get("tag") or "随手"),
                        str(item.get("description") or ""),
                        now,
                    )
                    self._save()
                    for old_path in evicted:
                        self._unlink_if_unreferenced(old_path)
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
            evicted: list[str] = []
            while len(entries) > self.limit:
                old = entries.pop(0)
                if isinstance(old, dict) and old.get("path"):
                    evicted.append(str(old["path"]))
            self._save()
            for old_path in evicted:
                self._unlink_if_unreferenced(old_path)
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
            global_entries = list(self._index.get(GLOBAL_POOL_KEY) or [])
        if any(
            isinstance(item, dict) and (item.get("face_id") or self._safe_path(item.get("path")) is not None)
            for item in entries
        ):
            return True
        return any(
            isinstance(item, dict) and not item.get("face_id") and self._safe_path(item.get("path")) is not None
            for item in global_entries
        )

    def tags(self, group_id: str, limit: int = 12) -> list[str]:
        with self._lock:
            entries = list(self._index.get(group_id) or [])
            global_entries = list(self._index.get(GLOBAL_POOL_KEY) or [])
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
                    return out
        for item in reversed(global_entries):
            if not isinstance(item, dict) or item.get("face_id"):
                continue
            if self._safe_path(item.get("path")) is None:
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

    def pick_entry(self, group_id: str, keyword: str = "") -> Optional[PickedEmoji]:
        with self._lock:
            raw_local = list(self._index.get(group_id) or [])
            raw_global = list(self._index.get(GLOBAL_POOL_KEY) or [])

        local_entries = [
            item
            for item in raw_local
            if isinstance(item, dict) and (item.get("face_id") or self._safe_path(item.get("path")) is not None)
        ]
        local_digests = {str(it.get("sha256") or "") for it in local_entries if it.get("sha256")}
        local_paths = {str(it.get("path") or "") for it in local_entries if it.get("path")}

        candidates: list[tuple[dict[str, Any], bool]] = [(it, True) for it in local_entries]
        for item in raw_global:
            if not isinstance(item, dict) or item.get("face_id"):
                continue
            if self._safe_path(item.get("path")) is None:
                continue
            digest = str(item.get("sha256") or "")
            p_str = str(item.get("path") or "")
            if (digest and digest in local_digests) or (p_str and p_str in local_paths):
                continue
            candidates.append((item, False))

        if not candidates:
            return None

        terms = self._keyword_terms(keyword)
        scored_matched = False
        pool: list[dict[str, Any]] = []
        if terms:
            scored: list[tuple[float, float, dict[str, Any]]] = []
            for item, is_local in candidates:
                hay = "{} {}".format(item.get("tag", ""), item.get("description", "")).lower()
                base_score = sum(1.0 if len(w) >= 2 else 0.3 for w in terms if w in hay)
                if base_score > 0:
                    affinity = base_score + (0.25 if is_local else 0.0)
                    scored.append((affinity, float(item.get("ts") or 0.0), item))
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
            pool = sorted((item for item, _ in candidates), key=lambda x: float(x.get("ts") or 0.0))[-8:]
        if not pool:
            return None
        # 同池存在真实图片表情包时优先发真实图片，而非 QQ 原生小黄脸
        img_pool = [item for item in pool if not item.get("face_id")]
        if img_pool:
            pool = img_pool
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
        tag = str(picked.get("tag") or "").strip()
        desc = str(picked.get("description") or "").strip()
        if picked.get("face_id"):
            fid = str(picked["face_id"])
            return PickedEmoji(kind="face", face_id=fid, tag=tag, description=desc)
        return PickedEmoji(kind="image", path=str(picked["path"]), tag=tag, description=desc)

    def pick(self, group_id: str, keyword: str = "") -> Optional[str | Path]:
        picked = self.pick_entry(group_id, keyword)
        if picked is None:
            return None
        if picked.kind == "face":
            return "face:" + picked.face_id
        return Path(picked.path)

    def cleanup(self, max_age_days: Optional[int] = None) -> None:
        days = self.max_age_days if max_age_days is None else max_age_days
        cutoff = time.time() - days * 86400
        stale_paths: list[str] = []
        with self._lock:
            for gid, entries in list(self._index.items()):
                if not isinstance(entries, list):
                    continue
                kept = []
                for item in entries:
                    if not isinstance(item, dict):
                        continue
                    if float(item.get("ts") or 0) < cutoff:
                        if item.get("path"):
                            stale_paths.append(str(item["path"]))
                    else:
                        kept.append(item)
                self._index[gid] = kept
            self._save()
        for old_path in stale_paths:
            self._unlink_if_unreferenced(old_path)

    def stats(self, group_id: Optional[str] = None) -> dict[str, int]:
        with self._lock:
            if group_id:
                return {group_id: len(self._index.get(group_id, []))}
            return {k: len(v) for k, v in self._index.items()}

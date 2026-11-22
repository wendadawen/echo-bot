"""视觉层（完整版）：图片转描述、表情仓库、语义网格挑选、内容过滤、
图片记忆、缓存清理。

对齐 MaiBot `src/emoji_system`（manager / semantic_match / cache_cleanup）与
`image_system`：素材全部来自群里、落在 data/，不进仓库也不进镜像；
描述、打标签、网格挑选都复用配置里指定的模型。只用标准库。
"""

import base64
import hashlib
import json
import os
import random
import re
import threading
import time
import uuid
from pathlib import Path

DESCRIBE_PROMPT = "用一句话说清这张图/表情在表达什么，不要解释、不要标点堆砌。"
TAG_PROMPT = "给这个表情一个两到四字的情绪标签，只输出标签。"
ANALYZE_PROMPT = (
    "先给出2到4字的情绪标签，再用竖线|隔开一句话描述这张图/表情在表达什么，"
    "不要解释、不要多余标点。例：无语|一只猫翻白眼"
)
GRID_PROMPT = (
    "下面给你几张候选表情，按出现顺序编号。只输出最适合的一个编号数字，"
    "不要解释。要表达的情绪是：{}"
)
FILTER_WORDS = ("二维码", "付款码", "身份证", "验证码", "密码", "银行卡号")


def _data_uri(image_bytes):
    return "data:image/png;base64," + base64.b64encode(image_bytes).decode("ascii")


def describe(model, image_bytes, hint=DESCRIBE_PROMPT):
    """调用模型视觉能力；失败返回空串，不抛到群里。"""
    if not image_bytes:
        return ""
    try:
        return model.vision_reply(hint, _data_uri(image_bytes))
    except Exception:
        return ""


def tag(model, image_bytes):
    if not image_bytes:
        return "随手"
    try:
        value = model.vision_reply(TAG_PROMPT, _data_uri(image_bytes))
    except Exception:
        return "随手"
    return (value or "随手").strip()[:8]


def analyze(model, image_bytes):
    """单次视觉调用同时拿到描述与情绪标签；兼容只返回短词的模型实现。"""
    if not image_bytes:
        return "", "随手"
    try:
        raw = str(model.vision_reply(ANALYZE_PROMPT, _data_uri(image_bytes)) or "").strip()
    except Exception:
        return "", "随手"
    if not raw:
        return "", "随手"
    if "|" in raw or "｜" in raw:
        parts = re.split(r"[|｜]", raw, maxsplit=1)
        label = (parts[0] or "").strip()[:8] or "随手"
        desc = (parts[1] or "").strip() or label
        return desc, label
    return raw, raw[:8] or "随手"


def acceptable(description, image_bytes=None):
    """内容过滤（fail-closed）：空描述或命中敏感词/超限一律拦下。"""
    text = str(description or "").strip()
    if not text:
        return False
    if any(word in text for word in FILTER_WORDS):
        return False
    if image_bytes and len(image_bytes) > 2 * 1024 * 1024:
        return False
    return True


def grid_pick(model, candidates, mood="随手"):
    """语义网格挑选：把多张候选一次性交给模型选编号，等价于 VLM 网格选择，
    但不需要拼图库。candidates 为 [(path, bytes), ...]，返回下标或 None。"""
    if not candidates:
        return None
    if len(candidates) == 1:
        return 0
    try:
        content = [{"type": "text", "text": GRID_PROMPT.format(mood)}]
        for _, data in candidates[:6]:
            content.append({"type": "image_url", "image_url": {"url": _data_uri(data)}})
        raw = model.vision_reply_json(content)
    except Exception:
        return random.randrange(len(candidates[:6]))
    match = re.search(r"\d+", str(raw or ""))
    if not match:
        return random.randrange(len(candidates[:6]))
    idx = int(match.group(0)) - 1
    if idx < 0:
        idx = 0
    return min(idx, len(candidates[:6]) - 1)


class EmojiBank:
    """按群收集团友发的表情，做语义标注，之后按标签/语义挑一张发回。"""

    def __init__(self, root, limit=64, max_age_days=30):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.index_path = self.root / "index.json"
        self.cache_path = self.root / "digest_cache.json"
        self.limit = limit
        self.max_age_days = max_age_days
        self._lock = threading.RLock()
        self._inflight = set()
        self._index = {}
        if self.index_path.exists():
            try:
                self._index = json.loads(self.index_path.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                self._index = {}
        self._digest_cache = {}
        if self.cache_path.exists():
            try:
                cached = json.loads(self.cache_path.read_text(encoding="utf-8"))
                if isinstance(cached, dict):
                    self._digest_cache = cached
            except (ValueError, OSError):
                self._digest_cache = {}
        self._migrate_and_dedup()

    @staticmethod
    def _atomic_write(path, text):
        target = Path(path)
        tmp = target.parent / ".{}.{}.tmp".format(target.name, uuid.uuid4().hex[:8])
        tmp.write_text(text, encoding="utf-8")
        os.replace(str(tmp), str(target))

    def _safe_path(self, path_str):
        if not path_str:
            return None
        try:
            root = self.root.resolve(strict=False)
            p = Path(path_str)
            if p.is_symlink():
                return None
            resolved = p.resolve(strict=False)
            if root not in resolved.parents:
                return None
            return p if p.is_file() else None
        except OSError:
            return None

    def _save(self):
        with self._lock:
            self._atomic_write(
                self.index_path,
                json.dumps(self._index, ensure_ascii=False, indent=2),
            )

    def _save_cache(self):
        with self._lock:
            self._atomic_write(
                self.cache_path,
                json.dumps(self._digest_cache, ensure_ascii=False, indent=2),
            )

    def _migrate_and_dedup(self):
        """为历史索引补齐 sha256，按群去重并预热全局指纹缓存。"""
        changed_index = False
        changed_cache = False
        with self._lock:
            for group_id, entries in list(self._index.items()):
                if not isinstance(entries, list):
                    continue
                by_digest = {}
                drop_indices = set()
                for idx, item in enumerate(entries):
                    if not isinstance(item, dict):
                        drop_indices.add(idx)
                        changed_index = True
                        continue
                    if item.get("face_id") and not item.get("path"):
                        continue
                    path_str = str(item.get("path") or "")
                    if not path_str:
                        drop_indices.add(idx)
                        changed_index = True
                        continue
                    path = Path(path_str)
                    digest = str(item.get("sha256") or "").strip()
                    if not digest:
                        if not path.exists():
                            continue
                        try:
                            digest = hashlib.sha256(path.read_bytes()).hexdigest()
                        except OSError:
                            continue
                        item["sha256"] = digest
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
                        try:
                            Path(old_path).unlink(missing_ok=True)
                        except OSError:
                            pass
                if drop_indices:
                    self._index[group_id] = [
                        item for idx, item in enumerate(entries) if idx not in drop_indices
                    ]
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

    def _folder(self, group_id):
        folder = self.root / re.sub(r"[^\w-]", "_", group_id)[:60]
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    def _store_entry(self, group_id, image_bytes, digest, label, description, cap, now):
        path = self._folder(group_id) / "{}_{}.png".format(int(now * 1000), digest[:8])
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

    def collect(self, group_id, image_bytes, model, limit=None):
        """收一张表情：按 sha256 去重与跨重启缓存，同一张图只调一次视觉模型。"""
        if not image_bytes:
            return ""
        if len(image_bytes) > 2 * 1024 * 1024:
            return ""
        digest = hashlib.sha256(image_bytes).hexdigest()
        cap = limit or self.limit
        now = time.time()

        with self._lock:
            entries = self._index.setdefault(group_id, [])
            for item in entries:
                if item.get("sha256") == digest and item.get("path") and Path(item["path"]).exists():
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
            description, label = analyze(model, image_bytes)
            ok = acceptable(description, image_bytes)
        except Exception:
            description = ""
            ok = False
        finally:
            now = time.time()
            with self._lock:
                self._inflight.discard(digest)
                # 仅当视觉模型给出明确描述时才写入指纹缓存；临时识别失败/空描述不缓存，避免永久放行或误封。
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
                        item.get("sha256") == digest
                        and item.get("path")
                        and self._safe_path(item["path"]) is not None
                        for item in entries
                    )
                    if not already:
                        self._store_entry(
                            group_id, image_bytes, digest, label, description[:80], cap, now
                        )
        return label if ok else ""

    def collect_face(self, group_id, face_id, name=""):
        """收 QQ 内置表情：只记 ID 和名称，不落图片。"""
        face_id = str(face_id or "").strip()
        if not face_id:
            return ""
        with self._lock:
            entries = self._index.setdefault(group_id, [])
            for item in entries:
                if str(item.get("face_id") or "") == face_id:
                    item["ts"] = time.time()
                    self._save()
                    return item.get("tag") or "表情"
            label = str(name or "QQ表情")[:20]
            entries.append({
                "face_id": face_id, "tag": label, "description": label,
                "ts": time.time(),
            })
            while len(entries) > self.limit:
                old = entries.pop(0)
                if old.get("path"):
                    Path(old["path"]).unlink(missing_ok=True)
            self._save()
            return label

    def candidates(self, group_id, keyword=""):
        with self._lock:
            entries = list(self._index.get(group_id) or [])
        rows = []
        for item in entries:
            path = self._safe_path(item.get("path"))
            if path is None:
                continue
            score = 0.0
            text = "{} {}".format(item.get("tag", ""), item.get("description", ""))
            if keyword:
                for word in re.findall(r"[\u4e00-\u9fff]{2,}|[a-z0-9]{3,}", keyword):
                    if word in text:
                        score += 1.0
            rows.append((score + item.get("ts", 0) / 1e12, path))
        rows.sort(key=lambda pair: pair[0], reverse=True)
        out = []
        for _, path in rows[:6]:
            try:
                if path.stat().st_size <= 2 * 1024 * 1024:
                    out.append((path, path.read_bytes()))
            except OSError:
                continue
        return out

    def pick(self, group_id, keyword="", model=None):
        """有模型时做语义网格挑选；没有图片时也可以挑 QQ 内置表情。"""
        with self._lock:
            raw_entries = list(self._index.get(group_id) or [])
        entries = [
            item for item in raw_entries
            if item.get("face_id") or self._safe_path(item.get("path")) is not None
        ]
        if not entries:
            return None
        if keyword:
            words = re.findall(r"[\u4e00-\u9fff]{2,}|[a-z0-9]{3,}", keyword)
            matched = [
                item for item in entries
                if any(word in "{} {}".format(item.get("tag", ""),
                                               item.get("description", ""))
                       for word in words)
            ]
            if matched:
                entries = matched
        images = [item for item in entries if self._safe_path(item.get("path")) is not None]
        if model is not None and len(images) > 1:
            candidates = []
            for item in images[:6]:
                p = self._safe_path(item.get("path"))
                if p is None:
                    continue
                try:
                    candidates.append((p, p.read_bytes()))
                except OSError:
                    continue
            if len(candidates) > 1:
                index = grid_pick(model, candidates, keyword or "随手")
                if index is not None and 0 <= index < len(candidates):
                    return candidates[index][0]
        pool = images + [item for item in entries if item.get("face_id")]
        if not pool:
            return None
        picked = random.choice(pool[-8:])
        if picked.get("face_id"):
            return "face:" + str(picked["face_id"])
        return Path(picked["path"])

    def remember(self, group_id, image_bytes, model, memory):
        """图片记忆：把描述写成一条记忆点，供之后 recall。"""
        description = describe(model, image_bytes)
        if description and acceptable(description):
            memory.add_point(group_id, "event", "图里是：" + description[:50])
        return description

    def cleanup(self, max_age_days=None):
        cutoff = time.time() - (self.max_age_days if max_age_days is None else max_age_days) * 86400
        with self._lock:
            for group_id, entries in list(self._index.items()):
                kept = []
                for item in entries:
                    if item.get("ts", 0) < cutoff:
                        if item.get("path"):
                            try:
                                Path(item["path"]).unlink(missing_ok=True)
                            except OSError:
                                pass
                    else:
                        kept.append(item)
                self._index[group_id] = kept
            self._save()

    def stats(self, group_id=None):
        with self._lock:
            if group_id:
                return {group_id: len(self._index.get(group_id, []))}
            return {key: len(value) for key, value in self._index.items()}

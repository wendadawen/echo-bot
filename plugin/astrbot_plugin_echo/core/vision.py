"""视觉层（完整版）：图片转描述、表情仓库、语义网格挑选、内容过滤、
图片记忆、缓存清理。

对齐 MaiBot `src/emoji_system`（manager / semantic_match / cache_cleanup）与
`image_system`：素材全部来自群里、落在 data/，不进仓库也不进镜像；
描述、打标签、网格挑选都复用配置里指定的模型。只用标准库。
"""

import base64
import json
import random
import re
import time
from pathlib import Path

DESCRIBE_PROMPT = "用一句话说清这张图/表情在表达什么，不要解释、不要标点堆砌。"
TAG_PROMPT = "给这个表情一个两到四字的情绪标签，只输出标签。"
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


def acceptable(description, image_bytes=None):
    """内容过滤：明显不该留存/转发的内容直接拦下。"""
    text = str(description or "")
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
    return min(int(match.group(0)) - 1, len(candidates[:6]) - 1)


class EmojiBank:
    """按群收集团友发的表情，做语义标注，之后按标签/语义挑一张发回。"""

    def __init__(self, root, limit=64, max_age_days=30):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.index_path = self.root / "index.json"
        self.limit = limit
        self.max_age_days = max_age_days
        self._index = {}
        if self.index_path.exists():
            try:
                self._index = json.loads(self.index_path.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                self._index = {}
        self._tag_cache = {}

    def _save(self):
        self.index_path.write_text(
            json.dumps(self._index, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def _folder(self, group_id):
        folder = self.root / re.sub(r"[^\w-]", "_", group_id)[:60]
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    def collect(self, group_id, image_bytes, model, limit=None):
        """收一张表情：先描述、过滤，再打标签、落盘、写索引。"""
        if not image_bytes:
            return ""
        digest = hash(image_bytes) & 0xFFFFFFFF
        if digest in self._tag_cache:
            return self._tag_cache[digest]
        description = describe(model, image_bytes)
        if not acceptable(description, image_bytes):
            return ""
        label = tag(model, image_bytes)
        path = self._folder(group_id) / "{:.0f}.png".format(time.time() * 1000)
        path.write_bytes(image_bytes)
        entries = self._index.setdefault(group_id, [])
        entries.append({
            "path": str(path), "tag": label, "description": description[:80],
            "ts": time.time(),
        })
        cap = limit or self.limit
        while len(entries) > cap:
            old = entries.pop(0)
            Path(old["path"]).unlink(missing_ok=True)
        self._tag_cache[digest] = label
        self._save()
        return label

    def collect_face(self, group_id, face_id, name=""):
        """收 QQ 内置表情：只记 ID 和名称，不落图片。"""
        face_id = str(face_id or "").strip()
        if not face_id:
            return ""
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
        entries = self._index.get(group_id) or []
        rows = []
        for item in entries:
            path = Path(item["path"])
            if not path.exists():
                continue
            score = 0.0
            text = "{} {}".format(item.get("tag", ""), item.get("description", ""))
            if keyword:
                for word in re.findall(r"[\u4e00-\u9fff]{2,}|[a-z0-9]{3,}", keyword):
                    if word in text:
                        score += 1.0
            rows.append((score + item.get("ts", 0) / 1e12, path))
        rows.sort(key=lambda pair: pair[0], reverse=True)
        return [(path, path.read_bytes()) for _, path in rows[:6]
                if path.stat().st_size <= 2 * 1024 * 1024]

    def pick(self, group_id, keyword="", model=None):
        """有模型时做语义网格挑选；没有图片时也可以挑 QQ 内置表情。"""
        entries = [
            item for item in (self._index.get(group_id) or [])
            if item.get("face_id") or (item.get("path") and Path(item["path"]).exists())
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
        images = [item for item in entries
                  if item.get("path") and Path(item["path"]).exists()]
        if model is not None and len(images) > 1:
            candidates = [(Path(item["path"]), Path(item["path"]).read_bytes())
                          for item in images[:6]]
            index = grid_pick(model, candidates, keyword or "随手")
            if index is not None and 0 <= index < len(candidates):
                return candidates[index][0]
        pool = images + [item for item in entries if item.get("face_id")]
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
        cutoff = time.time() - (max_age_days or self.max_age_days) * 86400
        for group_id, entries in list(self._index.items()):
            kept = []
            for item in entries:
                if item.get("ts", 0) < cutoff:
                    if item.get("path"):
                        Path(item["path"]).unlink(missing_ok=True)
                else:
                    kept.append(item)
            self._index[group_id] = kept
        self._save()

    def stats(self, group_id=None):
        if group_id:
            return {group_id: len(self._index.get(group_id, []))}
        return {key: len(value) for key, value in self._index.items()}

"""视觉层：图片转描述、表情仓库（收集 / 语义标注 / 挑选 / 清理）。

参考 MaiBot 的 emoji_system 与 image_system：素材来自群里，落在 data/，
不进仓库也不进镜像；描述与挑选都复用配置里指定的模型。
"""

import base64
import json
import re
import time
from pathlib import Path

DESCRIBE_PROMPT = "用一句话说清这张图/表情在表达什么，不要解释、不要标点堆砌。"
TAG_PROMPT = "给这个表情一个两到四字的情绪标签，只输出标签。"


def describe(model, image_bytes, hint=DESCRIBE_PROMPT):
    """调用模型视觉能力；失败返回空串，不抛到群里。"""
    if not image_bytes:
        return ""
    encoded = base64.b64encode(image_bytes).decode("ascii")
    try:
        return model.vision_reply(hint, "data:image/png;base64," + encoded)
    except Exception:
        return ""


def tag(model, image_bytes):
    if not image_bytes:
        return "随手"
    encoded = base64.b64encode(image_bytes).decode("ascii")
    try:
        value = model.vision_reply(TAG_PROMPT, "data:image/png;base64," + encoded)
    except Exception:
        return "随手"
    return (value or "随手").strip()[:8]


class EmojiBank:
    """按群收集团友发的表情，做语义标注，之后按标签挑一张发回。"""

    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.index_path = self.root / "index.json"
        self._index = {}
        if self.index_path.exists():
            try:
                self._index = json.loads(self.index_path.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                self._index = {}

    def _save(self):
        self.index_path.write_text(
            json.dumps(self._index, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def collect(self, group_id, image_bytes, model, limit=64):
        if not image_bytes:
            return ""
        label = tag(model, image_bytes)
        folder = self.root / re.sub(r"[^\w-]", "_", group_id)[:60]
        folder.mkdir(parents=True, exist_ok=True)
        name = "{:.0f}.png".format(time.time() * 1000)
        path = folder / name
        path.write_bytes(image_bytes)
        entries = self._index.setdefault(group_id, [])
        entries.append({"path": str(path), "tag": label, "ts": time.time()})
        if len(entries) > limit:
            old = entries.pop(0)
            Path(old["path"]).unlink(missing_ok=True)
        self._save()
        return label

    def pick(self, group_id, keyword=""):
        entries = self._index.get(group_id) or []
        if not entries:
            return None
        scored = []
        for item in entries:
            path = Path(item["path"])
            if not path.exists():
                continue
            score = 1.0
            if keyword and keyword in item.get("tag", ""):
                score += 2.0
            scored.append((score + item.get("ts", 0) / 1e12, path))
        if not scored:
            return None
        scored.sort(key=lambda pair: pair[0])
        # 在较新的若干张里随机挑，避免每次同一张
        import random

        return random.choice([p for _, p in scored[-8:]])

    def cleanup(self, max_age_days=30):
        cutoff = time.time() - max_age_days * 86400
        for group_id, entries in list(self._index.items()):
            kept = []
            for item in entries:
                if item.get("ts", 0) < cutoff:
                    Path(item["path"]).unlink(missing_ok=True)
                else:
                    kept.append(item)
            self._index[group_id] = kept
        self._save()

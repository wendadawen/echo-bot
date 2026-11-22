"""注意力漂移：判断一句话该顺着当前话题，还是可以偏出去联想一句。

对齐 MaiBot `src/maisaka/attention_drift.py` 的档位设计（off / subtle / active），
但用二元组重合度与新词比例计算，不依赖向量模型。只依赖标准库。
"""

import re
import threading
import time

URL_PATTERN = re.compile(r"https?://\S+")

DRIFT_BLOCKS = {
    "off": "",
    "subtle": (
        "漂移档位：轻微。只有最近消息里出现很自然的钩子时，才轻轻联想一句；"
        "多数时候继续当前话题。"
    ),
    "active": (
        "漂移档位：活跃。可以抓新鲜、好笑、反差强的细节接话，"
        "但回复仍要短，并且能被最近几条消息解释得通。"
    ),
}

STOP_GRAMS = {
    "什么", "怎么", "这个", "那个", "就是", "不是", "可以", "我们", "你们",
    "他们", "现在", "今天", "感觉", "真的", "还是", "然后", "所以", "因为",
}


def _grams(text):
    clean = "".join(ch for ch in str(text or "") if ch.strip())
    return {
        clean[i:i + 2]
        for i in range(max(0, len(clean) - 1))
        if clean[i:i + 2] not in STOP_GRAMS
    }


def novelty(text, recent_texts):
    """新词比例：这句话里有多少二元组是最近没出现过的。"""
    current = _grams(text)
    if not current:
        return 0.0
    seen = set()
    for row in recent_texts or []:
        seen |= _grams(row)
    fresh = current - seen
    return len(fresh) / float(len(current))


def drift_level(score, *, budget=2, intensity="normal"):
    """按新词比例与剩余预算给出档位；预算用完时仅把活跃漂移降级为轻微，低分仍 off。"""
    if intensity == "off":
        return "off"
    if budget <= 0:
        return "subtle" if score >= 0.72 else "off"
    if score >= 0.72 and budget >= 2:
        return "active"
    if score >= 0.45:
        return "subtle"
    return "off"


def render_block(level, topic=""):
    block = DRIFT_BLOCKS.get(level, "")
    if not block:
        return ""
    if topic:
        block += "当前话题：{}。".format(topic)
    block += "别把话题带太远，也别解释自己在联想。"
    return block


class AttentionTracker:
    """每群跟踪最近文本、当前话题与漂移预算（按小时重置）。"""

    def __init__(self, window=8, budget_per_hour=3):
        self.window = window
        self.budget_per_hour = budget_per_hour
        self._lock = threading.RLock()
        self._recent = {}
        self._budget = {}
        self._topic = {}

    def observe(self, group_id, text, now=None):
        now = time.time() if now is None else now
        gid = str(group_id)
        with self._lock:
            rows = self._recent.setdefault(gid, [])
            rows.append(str(text or ""))
            del rows[:-self.window]
            topic = self._guess_topic(rows)
            if topic:
                self._topic[gid] = topic
            self._refill(gid, now)

    def _refill(self, group_id, now):
        hour = int(now // 3600)
        used, stamp = self._budget.get(group_id, (0, hour))
        if stamp != hour:
            used = 0
        self._budget[group_id] = (used, hour)

    def budget(self, group_id, now=None):
        now = time.time() if now is None else now
        gid = str(group_id)
        with self._lock:
            self._refill(gid, now)
            used, _ = self._budget.get(gid, (0, 0))
            return max(0, self.budget_per_hour - used)

    def level(self, group_id, text, intensity="normal", now=None):
        now = time.time() if now is None else now
        gid = str(group_id)
        with self._lock:
            rows = list(self._recent.get(gid, []))
            rem = self.budget(gid, now)
        score = novelty(text, rows)
        return drift_level(score, budget=rem, intensity=intensity)

    def consume(self, group_id, now=None):
        now = time.time() if now is None else now
        gid = str(group_id)
        with self._lock:
            self._refill(gid, now)
            used, hour = self._budget.get(gid, (0, int(now // 3600)))
            self._budget[gid] = (used + 1, hour)

    def topic(self, group_id):
        with self._lock:
            return self._topic.get(str(group_id), "")

    @staticmethod
    def _guess_topic(rows):
        """用最近几条里出现次数最多的二元组当话题；去掉 URL 与标点噪音，同分时取更早出现的。"""
        if not rows:
            return ""
        counts, first, order = {}, {}, 0
        for row in rows[-4:]:
            stripped = URL_PATTERN.sub("", str(row or ""))
            clean = "".join(
                ch for ch in stripped
                if "\u4e00" <= ch <= "\u9fff" or ch.isalnum()
            )
            for index in range(max(0, len(clean) - 1)):
                gram = clean[index:index + 2]
                if gram in STOP_GRAMS:
                    continue
                counts[gram] = counts.get(gram, 0) + 1
                if gram not in first:
                    first[gram] = order
                order += 1
        if not counts:
            return ""
        word, hits = max(counts.items(), key=lambda kv: (kv[1], -first[kv[0]]))
        return word if hits >= 2 else ""

"""输出层（完整版）：拆条、拟人打字延迟、去重、错别字、黑话挖掘与替换、
群风格学习、表达习惯学习、引用/反应标记。

对齐 MaiBot `replyer`、`chinese_typo`、`jargon_context_matcher`、`learner`、
`src/maisaka/display` 的输出侧机制。只用标准库，字频表可选落在 data/。
"""

import json
import random
import re
from pathlib import Path

# 常用同音/近音字，用于低概率「打错字」；字频表存在时优先按字频取舍。
HOMOPHONES = {
    "在": "再", "再": "在", "的": "得", "得": "的", "他": "她",
    "吗": "嘛", "怎": "咋", "已": "以", "以": "已", "应": "因",
    "什": "甚", "么": "末", "吧": "罢", "啦": "咯", "那": "哪",
    "还": "海", "没": "妹", "是": "事", "说": "嗦",
}

SPLIT_MARKS = "。！？!?~…"
MAX_PART_LEN = 24


def split(text, max_parts=3):
    """按句末标点与换行拆条；短句合并，长句再按逗号切开。"""
    text = (text or "").strip()
    if not text:
        return []
    if max_parts <= 1:
        return [text]
    rough = [p.strip() for p in re.split(r"(?<=[。！？!?~…])|\n+", text) if p.strip()]
    if not rough:
        rough = [text]
    expanded = []
    for part in rough:
        if len(part) > MAX_PART_LEN * 2:
            chunks = re.split(r"(?<=[，,；;])", part)
            expanded.extend(c.strip() for c in chunks if c.strip())
        else:
            expanded.append(part)
    merged = []
    for part in expanded or [text]:
        if merged and len(merged[-1]) + len(part) <= MAX_PART_LEN:
            merged[-1] += part
        else:
            merged.append(part)
    if len(merged) > max_parts:
        merged = merged[:max_parts - 1] + ["".join(merged[max_parts - 1:])]
    return merged


def delay(part, chars_per_second=12.0, cap=1.4, elapsed=0.0):
    """按生成耗时补足打字延迟：模型已经想了多久，就少等多久。"""
    base = len(part) / chars_per_second + random.uniform(0.08, 0.35)
    return max(0.0, min(cap, base) - max(0.0, elapsed))


def dedup(text, recent, window=6):
    return bool(text) and text in list(recent)[-window:]


def load_char_frequency(path=None):
    """可选字频表：MaiBot 的 depends-data/char_frequency.json 格式，落在本机 data/。"""
    if not path:
        return {}
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if isinstance(data, dict):
        data = data.get("char_frequency") or data.get("frequency") or data
    if not isinstance(data, dict):
        return {}
    return {str(k): float(v) for k, v in data.items()
            if isinstance(k, str) and isinstance(v, (int, float))}


def typo(text, chance=0.05, frequency=None):
    if random.random() > chance or not text:
        return text
    chars = list(text)
    candidates = [i for i, c in enumerate(chars) if c in HOMOPHONES]
    if not candidates:
        return text
    index = random.choice(candidates)
    original = chars[index]
    replacement = HOMOPHONES[original]
    # 有字频表时，只把高频字打错成低频字，避免造出更常见的错字。
    if frequency:
        if frequency.get(original, 0) < frequency.get(replacement, 0):
            return text
    chars[index] = replacement
    return "".join(chars)


def mine_jargon(messages, existing=None, min_count=3, top=8):
    """黑话挖掘：统计群里的高频非通用短词，作为候选交给模型确认。"""
    existing = existing or {}
    counts = {}
    for line in messages or []:
        for word in re.findall(r"[\u4e00-\u9fff]{2,4}|[a-zA-Z]{3,8}", str(line)):
            if word in existing:
                continue
            counts[word] = counts.get(word, 0) + 1
    return [w for w, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
            if n >= min_count][:top]


def learn_style(messages, profile=None):
    """群风格学习：平均句长、口头禅、标点习惯；长度只作提示参考，不用于硬截断。"""
    profile = dict(profile or {})
    lengths, marks, words = [], 0, {}
    for line in messages or []:
        text = str(line).strip()
        if not text:
            continue
        lengths.append(len(text))
        marks += sum(1 for c in text if c in SPLIT_MARKS)
        for word in re.findall(r"[\u4e00-\u9fff]{2,3}", text):
            words[word] = words.get(word, 0) + 1
    if lengths:
        avg = sum(lengths) / len(lengths)
        profile["avg_len"] = round(avg, 1)
        # 只作提示参考；上限按 QQ 单条消息上限给足，不再按群聊平均长度压短。
        profile["max_len"] = 4000
        profile["punctuation"] = round(marks / len(lengths), 2)
    profile["catchphrases"] = [w for w, n in
                               sorted(words.items(), key=lambda kv: (-kv[1], kv[0]))
                               if n >= 3][:6]
    return profile


def learn_expressions(situation, reply, store=None):
    """表达学习：把「情境 → 说法」记下来，之后同情境优先复用。"""
    store = store if store is not None else {}
    if not situation or not reply:
        return store
    bucket = store.setdefault(situation[:12], [])
    if reply not in bucket:
        bucket.append(reply)
    del bucket[:-4]
    return store


def pick_expression(situation, store):
    bucket = (store or {}).get(situation[:12]) or []
    return random.choice(bucket) if bucket else ""


def apply_jargon(text, jargon):
    for word, alias in (jargon or {}).items():
        if word and word in text:
            text = text.replace(word, alias, 1)
    return text


def decorate(text, style=None, store=None, situation=""):
    """输出前的最后一跳：整理空白，不再把生成好的回复随机替换成历史旧话。"""
    return (text or "").strip()

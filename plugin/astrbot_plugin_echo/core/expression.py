"""输出层：拆条、拟人打字延迟、去重、低概率错别字、黑话替换。

参考 MaiBot 的 replyer / chinese_typo / jargon 机制。只用标准库。
"""

import random
import re

# 常用同音/近音字，用于低概率「打错字」，纯本地表，不引拼音库。
HOMOPHONES = {
    "在": "再", "再": "在", "的": "得", "得": "的", "他": "她",
    "吗": "嘛", "怎": "咋", "已": "以", "以": "已", "应": "因",
    "什": "甚", "么": "末", "吧": "罢", "啦": "咯",
}


def split(text, max_parts=3):
    text = (text or "").strip()
    if not text:
        return []
    parts = [p.strip() for p in re.split(r"(?<=[。！？!?~])|\n+", text) if p.strip()]
    if not parts:
        parts = [text]
    merged = []
    for part in parts:
        if merged and len(merged[-1]) + len(part) <= 18:
            merged[-1] += part
        else:
            merged.append(part)
    return merged[:max_parts]


def delay(part, chars_per_second=12.0, cap=1.4):
    return min(cap, len(part) / chars_per_second + random.uniform(0.08, 0.35))


def dedup(text, recent, window=6):
    return bool(text) and text in list(recent)[-window:]


def typo(text, chance=0.05):
    if random.random() > chance:
        return text
    chars = list(text)
    candidates = [i for i, c in enumerate(chars) if c in HOMOPHONES]
    if not candidates:
        return text
    index = random.choice(candidates)
    chars[index] = HOMOPHONES[chars[index]]
    return "".join(chars)


def apply_jargon(text, jargon):
    """把学到的群内黑话贴到回复里（只做同义替换，不改语义）。"""
    for word, alias in (jargon or {}).items():
        if word and word in text:
            text = text.replace(word, alias, 1)
    return text

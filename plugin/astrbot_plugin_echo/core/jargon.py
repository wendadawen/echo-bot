"""黑话与口头禅的语境匹配：只在相关话题出现时才替换。

原来的 `apply_jargon` 是无脑全局替换，容易把不相关的句子改坏。
对齐 MaiBot `src/maisaka/jargon_context_matcher.py` 的做法：先看上下文，
命中触发词再替换，并按相似度取最贴近的一条。只依赖标准库。
"""

import re

STOP = {"什么", "怎么", "这个", "那个", "就是", "不是", "可以", "我们", "你们"}


def _grams(text):
    clean = "".join(ch for ch in str(text or "") if ch.strip())
    return {clean[i:i + 2] for i in range(max(0, len(clean) - 1))
            if clean[i:i + 2] not in STOP}


def _overlap(left, right):
    a, b = _grams(left), _grams(right)
    if not a or not b:
        return 0.0
    return len(a & b) / float(len(a | b))


def normalize(pairs):
    """把 memory 里的黑话数据整理成 [(说法, 替换, 触发词)]。"""
    rows = []
    if isinstance(pairs, dict):
        for key, value in pairs.items():
            rows.append((str(key or "").strip(), str(value or "").strip(), ""))
    elif isinstance(pairs, (list, tuple)):
        for item in pairs:
            if isinstance(item, dict):
                rows.append((str(item.get("term") or "").strip(),
                             str(item.get("replacement") or item.get("term") or "").strip(),
                             str(item.get("trigger") or "").strip()))
            elif isinstance(item, (list, tuple)) and item:
                term = str(item[0]).strip()
                replacement = str(item[1]).strip() if len(item) > 1 else term
                trigger = str(item[2]).strip() if len(item) > 2 else ""
                rows.append((term, replacement, trigger))
    return [row for row in rows if row[0] and row[1]]


def match(text, pairs, threshold=0.34):
    """返回这句里最该用的黑话替换；没把握就不换。"""
    clean = str(text or "")
    rows = normalize(pairs)
    if not clean or not rows:
        return None
    best, best_score = None, 0.0
    for term, replacement, trigger in rows:
        if term == replacement:
            continue
        if term not in clean:
            continue
        if trigger:
            keywords = [word for word in re.split(r"[\s,，;；/]+", trigger) if word]
            hit = any(word in clean for word in keywords)
            score = 1.0 if hit else _overlap(trigger, clean)
        else:
            score = 1.0
        if score > best_score:
            best, best_score = (term, replacement), score
    if best is None or best_score < threshold:
        return None
    return best


def apply(text, pairs, threshold=0.34):
    """按语境替换最多一处，避免一句话被改得面目全非。"""
    hit = match(text, pairs, threshold)
    if not hit:
        return text
    term, replacement = hit
    return str(text).replace(term, replacement, 1)


def learn(texts, existing=None, min_count=2, top=8):
    """从聊天里找高频二元组当候选黑话；只做候选，不自动上线。"""
    counts = {}
    for text in texts or []:
        for gram in _grams(text):
            counts[gram] = counts.get(gram, 0) + 1
    known = {row[0] for row in normalize(existing)}
    ranked = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)
    return [word for word, hits in ranked
            if hits >= min_count and word not in known][:top]

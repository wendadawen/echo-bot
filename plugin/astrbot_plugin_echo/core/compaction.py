"""上下文压缩：超出预算时保留重要消息，把其余压成一行摘要。

对齐 MaiBot `src/maisaka/context/compaction.py` 与 `usage.py` 的思路：
长会话不能整包塞给模型，也不能简单截断——@、问句、点名的消息要留。
只依赖标准库。
"""

import re

from .messages import format_speaker_message

URL_PATTERN = re.compile(r"https?://\S+")
SPACE_PATTERN = re.compile(r"\s+")


def estimate_tokens(text):
    """粗略估算 token：中文约 1.5 字 / token，英文约 4 字符 / token。"""
    clean = str(text or "")
    chinese = sum(1 for ch in clean if "\u4e00" <= ch <= "\u9fff")
    other = len(SPACE_PATTERN.sub("", clean)) - chinese
    return int(chinese / 1.5 + other / 4.0) + 1


def _text(message):
    if isinstance(message, dict):
        return str(message.get("text") or "")
    return str(getattr(message, "text", "") or "")


def _role(message):
    if isinstance(message, dict):
        return str(message.get("role") or "")
    return str(getattr(message, "role", "") or "")


def importance(message, index, total, self_names=()):
    """越靠后越重要；@、问句、被点名再额外加权。"""
    text = _text(message)
    score = index / float(max(1, total))
    if _role(message) == "bot":
        score += 0.05
    if any("@" + name in text for name in self_names if name):
        score += 0.35
    if text.strip().endswith(("?", "？", "吗", "呢")):
        score += 0.15
    if any(name and name in text for name in self_names):
        score += 0.1
    return score


def compact(history, budget_tokens=900, keep_recent=5, self_names=()):
    """返回 (保留的消息, 摘要行)。预算足够时原样返回。"""
    rows = list(history or [])
    if not rows:
        return [], ""
    total_cost = sum(estimate_tokens(_text(row)) for row in rows)
    if total_cost <= budget_tokens:
        return rows, ""
    keep = max(0, int(keep_recent or 0))
    if keep == 0:
        tail = []
        head = list(rows)
    else:
        tail = rows[-keep:]
        head = rows[:-keep]
    kept = list(tail)
    used = sum(estimate_tokens(_text(row)) for row in tail)
    # 从旧到新挑重要消息补进来，直到预算用完。
    ranked = sorted(
        ((importance(row, i, len(head), self_names), row) for i, row in enumerate(head)),
        key=lambda pair: pair[0],
        reverse=True,
    )
    picked = []
    for _, row in ranked:
        cost = estimate_tokens(_text(row))
        if used + cost > budget_tokens:
            continue
        picked.append(row)
        used += cost
    order = {id(row): index for index, row in enumerate(head)}
    picked.sort(key=lambda row: order.get(id(row), 0))
    kept = picked + kept
    picked_ids = {id(row) for row in picked}
    dropped = [row for row in head if id(row) not in picked_ids]
    summary = _summarize(dropped)
    if summary:
        kept.insert(0, {"role": "system", "user_name": "摘要", "text": summary})
    return kept, summary


def _summarize(rows):
    """把被丢掉的消息压成一行：谁说了什么，最多三条。"""
    if not rows:
        return ""
    lines = []
    for row in rows[-6:]:
        name = "大肥鱼" if _role(row) == "bot" else (
            row.get("user_name", "群友") if isinstance(row, dict)
            else getattr(row, "user_name", "群友")
        )
        text = URL_PATTERN.sub("[链接]", _text(row)).strip().replace("\n", " ")
        if text:
            lines.append(format_speaker_message(name, text[:40]))
    if not lines:
        return ""
    return "（较早的对话）" + "；".join(lines[-3:])


def usage(history):
    """返回消息条数与估算 token，供监控与日志使用。"""
    rows = list(history or [])
    return {
        "messages": len(rows),
        "tokens": sum(estimate_tokens(_text(row)) for row in rows),
    }

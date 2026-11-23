"""输出编排：拆条、打字节奏、引用与 @ 的挂载、发送限流。

对齐 MaiBot `src/maisaka/display/` 与 `src/maisaka/visual/message_limiter.py`：
把「说什么」和「怎么发」分开——文本已经生成，这里只决定分几条、隔多久、
带不带引用和表情。纯函数与进程内状态，不依赖框架。
"""

import re
import time
from dataclasses import dataclass, field

from .expression import delay as expression_delay
from .expression import split as expression_split

MARK_PATTERN = re.compile(r"^#+\s*", re.M)
LIST_PATTERN = re.compile(r"^\s*[-*+]\s+", re.M)
CODE_BLOCK = re.compile(r"```(?:[a-zA-Z0-9_+-]+[ \t]*\n|\n)?(.*?)```", re.S)
LATEX_PATTERNS = (
    re.compile(r"\$\$(.+?)\$\$", re.S),
    re.compile(r"\\\[(.*?)\\\]", re.S),
    re.compile(r"\\\((.*?)\\\)", re.S),
    re.compile(r"(?<![\\$A-Za-z0-9_])\$(?!\s)(?![0-9]+(?:\.[0-9]+)?(?:\s|$))([^$\n]+?)(?<!\s)\$(?![A-Za-z0-9_])"),
)
LINK_PATTERN = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")


def _replace_link(match):
    label = match.group(1).strip()
    target = match.group(2).strip()
    if target.startswith(("http://", "https://")):
        if not label or label == target:
            return target
        return "{} ({})".format(label, target)
    return label


def clean(part):
    """去掉 QQ 不认识的 markdown / latex 标记；代码块内容与公网链接原样保留。"""
    text = str(part or "").strip()
    blocks = []

    def hold(match):
        blocks.append(match.group(1))
        return "\x00{}\x00".format(len(blocks) - 1)

    text = CODE_BLOCK.sub(hold, text)
    for pattern in LATEX_PATTERNS:
        text = pattern.sub(r"\1", text)
    text = LINK_PATTERN.sub(_replace_link, text)
    text = MARK_PATTERN.sub("", text)
    text = LIST_PATTERN.sub("", text)
    text = text.replace("**", "").replace("`", "")
    text = re.sub(r"\n{2,}", "\n", text)
    for index, block in enumerate(blocks):
        text = text.replace("\x00{}\x00".format(index), block)
    return text.strip()


@dataclass
class Outgoing:
    """一条待发送的计划；platform 层照着它发即可。"""

    parts: list = field(default_factory=list)
    delays: list = field(default_factory=list)
    at: str = ""
    quote: str = ""
    face: str = ""
    image: str = ""

    @property
    def empty(self):
        return not self.parts and not self.face and not self.image


def merge_burst(parts, min_len=6, max_merge=2):
    """把过短的碎片并回前一条，避免「嗯」「行」单独刷屏。"""
    rows = []
    merged = 0
    for part in parts or []:
        text = clean(part)
        if not text:
            continue
        if rows and len(text) < min_len and merged < max_merge:
            rows[-1] = rows[-1] + text
            merged += 1
            continue
        rows.append(text)
    return rows


def typing_delay(part, elapsed=0.0, chars_per_second=9.0, cap=2.4):
    """按字数算停顿，并扣掉模型已经花掉的时间；至少留 0.15 秒。"""
    base = expression_delay(part, chars_per_second=chars_per_second, cap=cap)
    return max(0.15, base - max(0.0, float(elapsed or 0.0)) * 0.6)


def plan(text, *, quote="", at="", face="", image="", max_parts=1,
         elapsed=0.0, soft_limit=4000):
    """生成发送计划：长句按句拆，短碎片合并，附加引用 / @ / 表情。"""
    clean = str(text or "").strip()
    parts = []
    if clean:
        parts = merge_burst(expression_split(clean, max_parts=max_parts))
        # 单条过长时按逗号再切一刀，避免一整屏文字。
        refined = []
        for part in parts:
            if len(part) <= soft_limit:
                refined.append(part)
                continue
            pieces = [piece.strip() for piece in part.replace("，", "，\n").split("\n")]
            refined.extend(piece for piece in pieces if piece)
            if len(refined) > max_parts:
                refined = refined[:max_parts - 1] + ["".join(refined[max_parts - 1:])]
        parts = refined
    delays = [typing_delay(part, elapsed=elapsed) for part in parts]
    return Outgoing(parts=parts, delays=delays, at=str(at or ""),
                    quote=str(quote or ""), face=str(face or ""),
                    image=str(image or ""))


class MessageLimiter:
    """滑动窗口限流：每群每分钟最多 N 条，每轮最多 M 条。"""

    def __init__(self, per_minute=8, per_burst=3):
        self.per_minute = per_minute
        self.per_burst = per_burst
        self._sent = {}

    def _trim(self, group_id, now):
        rows = [stamp for stamp in self._sent.get(str(group_id), [])
                if now - stamp < 60]
        self._sent[str(group_id)] = rows
        return rows

    def allow(self, group_id, count=1, now=None):
        now = time.time() if now is None else now
        rows = self._trim(group_id, now)
        return len(rows) + count <= self.per_minute

    def record(self, group_id, count=1, now=None):
        now = time.time() if now is None else now
        rows = self._trim(group_id, now)
        rows.extend([now] * max(0, int(count)))
        self._sent[str(group_id)] = rows[-self.per_minute:]
        return len(rows)

    def remaining(self, group_id, now=None):
        now = time.time() if now is None else now
        return max(0, self.per_minute - len(self._trim(group_id, now)))

    def cap_parts(self, parts):
        return list(parts or [])[:self.per_burst]


def preview(text, limit=60):
    """日志里用的短预览，避免把整段回复打出来。"""
    clean = str(text or "").replace("\n", " ").strip()
    return clean[:limit] + ("…" if len(clean) > limit else "")

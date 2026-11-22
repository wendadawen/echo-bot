"""群聊提及识别：结构化 @、文本 @、引用、转述与直接问句。

对齐 MaiBot `src/maisaka/scenes/group/mentions.py` 与 `quoting.py`：
@ 不只有一种来源，转述别人对你的评价也要单独识别——这类消息最容易被
当成「在跟你说话」，进而逐条回答。只依赖标准库。
"""

import re

RETOLD_MARKERS = (
    "他说你", "她说你", "他们说", "有人说你", "听说你", "人家说你",
    "跟我讲你", "跟你讲你", "转述", "评价你", "骂你", "说你坏话",
    "说你不行", "说你不", "吐槽你", "背后说",
)
AT_PATTERN = re.compile(r"[@＠]([^\s@＠，,。:：!！?？]{1,20})")
QUESTION_TAIL = ("吗", "呢", "么", "?", "？")
QUESTION_WORDS = (
    "怎么", "为什么", "为啥", "是不是", "能不能", "要不要",
    "有没有", "什么", "啥", "谁", "多少", "多久",
    "哪个", "哪里", "哪儿", "哪种",
)
_RETOLD_DYNAMIC = re.compile(
    r"(?:^|[\s，,。！？])(?!(?:话说|别说|再说|别讲|要说|虽说|瞎说|乱说))"
    r"[\u4e00-\u9fffA-Za-z0-9_]{1,6}(?:在)?(?:说|讲|评价)(?:你|他|她)"
)


class MentionInfo:
    """一次提及的判定结果，供上下文与决策层使用。"""

    __slots__ = ("at", "text_at", "reply", "quoted", "retold", "targets",
                 "direct_question", "self_called")

    def __init__(self, at=False, text_at=False, reply=False, quoted="",
                 retold=False, targets=None, direct_question=False,
                 self_called=False):
        self.at = bool(at)
        self.text_at = bool(text_at)
        self.reply = bool(reply)
        self.quoted = str(quoted or "")
        self.retold = bool(retold)
        self.targets = list(targets or [])
        self.direct_question = bool(direct_question)
        self.self_called = bool(self_called)

    @property
    def mentioned(self):
        return self.at or self.text_at or self.reply or self.self_called

    def as_dict(self):
        return {name: getattr(self, name) for name in self.__slots__}

    def __repr__(self):
        return "MentionInfo({})".format(self.as_dict())


def is_question(text):
    clean = str(text or "").strip()
    if not clean:
        return False
    for line in clean.splitlines():
        line = line.strip()
        if line.endswith(QUESTION_TAIL):
            return True
    return any(word in clean for word in QUESTION_WORDS)


def is_retold(text):
    clean = str(text or "")
    if not clean:
        return False
    if any(marker in clean for marker in RETOLD_MARKERS):
        return True
    return bool(_RETOLD_DYNAMIC.search(clean))


def extract_targets(text, known_names=(), self_names=()):
    """抽出这句话在对谁说话：@ 的人、开头称呼、已知群友名。"""
    clean = str(text or "")
    targets = [name.strip() for name in AT_PATTERN.findall(clean) if name.strip()]
    names = list(known_names or []) + list(self_names or [])
    heads = [line.lstrip("，,。. ") for line in clean.splitlines() if line.strip()]
    for name in sorted(names, key=len, reverse=True):
        if not name:
            continue
        if any(h.startswith(name) or h.startswith("@" + name) or h.startswith("＠" + name) for h in heads):
            if name not in targets:
                targets.append(name)
            break
    return targets[:3]


def detect(text, *, at=False, self_names=(), quoted="", reply=False):
    """合成一次提及判定。`at` 由平台事件提供，文本 @ 在这里补。"""
    clean = str(text or "")
    lowered = clean.lower()
    names = [name for name in (self_names or []) if name]
    text_at = False
    for name in names:
        low_name = name.lower()
        if ("@" + name in clean or "＠" + name in clean
                or "@" + low_name in lowered or "＠" + low_name in lowered):
            text_at = True
            break
    targets = extract_targets(clean, self_names=names)
    self_called = False
    heads = [line.lstrip("，,。. ") for line in clean.splitlines() if line.strip()]
    for name in names:
        if not name:
            continue
        # 句首直呼，或同一句里出现名字且带问句，才算在叫自己。
        if any(h.startswith(name) for h in heads):
            self_called = True
            break
        if any(name in line and is_question(line) for line in clean.splitlines()):
            self_called = True
            break
    return MentionInfo(
        at=at,
        text_at=text_at,
        reply=reply,
        quoted=quoted,
        retold=is_retold(clean),
        targets=targets,
        direct_question=is_question(clean),
        self_called=self_called,
    )


def render_block(info, self_names=()):
    """把判定结果写成一句上下文提示；没有信息时返回空串。"""
    if info is None:
        return ""
    bits = []
    if info.mentioned:
        bits.append("这次有人明确点到了你，必须回应")
    if info.retold:
        bits.append("有人在你面前转述别人对你的评价，冲转述的人回，别去分析评价本身")
    if info.direct_question and not info.mentioned:
        bits.append("这句话带着问句，但不一定是问你的，接不上就别硬接")
    if info.quoted:
        bits.append("被引用的是：{}".format(info.quoted[:100]))
    return "；".join(bits)

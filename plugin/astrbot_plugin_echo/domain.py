"""核心领域模型与抽象端口协议。

本模块仅依赖 Python 标准库，定义系统内部不可变数据结构与 5 个稳定抽象边界：
- LLMClient：大模型调用与多模态理解（可随时切换供应商或推理模型）
- SearchProvider：可选联网直搜接口（Tavily/Exa 等，未配置自动隐藏）
- MemoryRepository：统一持久化（消息、群记忆、QQ 号归一人物档案、待投递队列、监控）
- EmojiStorePort：表情包收藏与按情绪抽取
- TaskRunner / HarnessBackend：可插拔后台沙箱执行器（Codex 等）
- MessageSender：平台消息发送与互动（文本、智能引用、表情、群文件、贴表情）
"""

from __future__ import annotations

import re
import struct
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional, Protocol, Sequence, runtime_checkable

FACE_LABELS: dict[str, str] = {
    "0": "惊讶", "1": "撇嘴", "2": "色", "3": "发呆", "4": "得意", "5": "流泪",
    "6": "害羞", "7": "闭嘴", "8": "睡", "9": "大哭", "10": "尴尬", "11": "发怒",
    "12": "调皮", "13": "呲牙", "14": "微笑", "15": "难过", "16": "酷",
    "18": "抓狂", "19": "吐", "20": "偷笑", "21": "可爱", "22": "白眼", "23": "傲慢",
    "24": "饥饿", "25": "困", "26": "惊恐", "27": "流汗", "28": "憨笑", "29": "悠闲",
    "30": "奋斗", "31": "咒骂", "32": "疑问", "33": "嘘", "34": "晕", "35": "折磨",
    "36": "衰", "37": "骷髅", "38": "敲打", "39": "再见", "46": "猪头", "49": "拥抱",
    "53": "蛋糕", "56": "刀", "59": "便便", "60": "咖啡", "63": "玫瑰", "64": "凋谢",
    "66": "爱心", "67": "心碎", "74": "太阳", "75": "月亮", "76": "赞", "77": "踩",
    "78": "握手", "79": "胜利", "85": "飞吻", "89": "西瓜", "96": "冷汗", "97": "擦汗",
    "98": "抠鼻", "99": "鼓掌", "100": "糗大了", "101": "坏笑", "102": "左哼哼",
    "103": "右哼哼", "104": "哈欠", "105": "鄙视", "106": "委屈", "107": "快哭了",
    "108": "阴险", "109": "亲亲", "110": "吓", "111": "可怜", "118": "抱拳",
    "120": "拳头", "121": "差劲", "123": "NO", "124": "OK", "144": "喝彩",
    "171": "茶", "172": "眨眼睛", "173": "泪奔", "174": "无奈", "175": "卖萌",
    "176": "小纠结", "177": "喷血", "178": "斜眼笑", "179": "doge", "181": "骚扰",
    "182": "笑哭", "183": "我最美", "212": "托腮", "262": "脑阔疼", "263": "沧桑",
    "264": "捂脸", "265": "辣眼睛", "266": "哦哟", "267": "头秃", "268": "问号脸",
    "269": "暗中观察", "270": "emm", "271": "吃瓜", "272": "呵呵哒", "273": "我酸了",
    "277": "汪汪", "281": "无眼笑", "282": "敬礼", "283": "狂笑", "284": "面无表情",
    "285": "摸鱼", "287": "哦", "289": "睁眼", "293": "摸锦鲤", "294": "期待",
    "297": "拜谢", "298": "元宝", "299": "牛啊", "305": "右亲亲", "306": "牛气冲天",
    "307": "喵喵", "311": "打call", "312": "变形", "314": "仔细分析", "318": "崇拜",
    "319": "比心", "320": "庆祝", "324": "吃糖", "326": "生气", "332": "举牌牌",
    "337": "花朵脸", "338": "我想开了", "339": "舔屏", "344": "大怨种",
}

# QQ 缺失经典与新版超级表情 / 扩展 sysface 只读理解表（仅用于看懂群友消息，不进入可发送经典 face 白名单）
EXTENDED_FACE_LABELS: dict[str, str] = {
    "41": "发抖", "42": "爱情", "43": "跳跳", "55": "炸弹", "86": "怄火",
    "112": "菜刀", "114": "篮球", "116": "示爱", "119": "勾引", "122": "爱你",
    "125": "转圈", "129": "挥手", "137": "鞭炮", "146": "爆筋", "147": "棒棒糖",
    "148": "喝奶", "169": "手枪", "180": "惊喜", "185": "羊驼", "187": "幽灵",
    "193": "大笑", "194": "不开心", "198": "呃", "200": "求求", "201": "点赞",
    "202": "无聊", "203": "托脸", "204": "吃", "206": "害怕", "210": "飙泪",
    "211": "我不看", "214": "啵啵", "215": "糊脸", "216": "拍头", "217": "扯一扯",
    "218": "舔一舔", "219": "蹭一蹭", "221": "顶呱呱", "222": "抱抱", "223": "暴击",
    "224": "开枪", "225": "撩一撩", "226": "拍桌", "227": "拍手", "229": "干杯",
    "230": "嘲讽", "231": "哼", "232": "佛系", "233": "掐一掐", "235": "颤抖",
    "237": "偷看", "238": "扇脸", "239": "原谅", "240": "喷脸", "241": "生日快乐",
    "243": "甩头", "244": "扔狗", "278": "汗", "286": "魔鬼笑", "288": "请",
    "290": "敲开心", "292": "让我康康", "295": "拿到红包", "300": "胖三斤",
    "301": "好闪", "302": "左拜年", "303": "右拜年", "317": "菜汪", "322": "拒绝",
    "323": "嫌弃", "325": "惊吓", "333": "烟花", "334": "虎虎生威", "336": "豹富",
    "341": "打招呼", "342": "酸Q", "343": "我方了",
    "345": "红包多多", "346": "你真棒棒", "347": "大展宏兔", "348": "福萝卜",
    "349": "坚强", "350": "贴贴", "351": "敲敲", "352": "咦", "353": "拜托",
    "354": "尊嘟假嘟", "355": "耶", "356": "666", "357": "裂开", "358": "骰子",
    "359": "包剪锤", "360": "亲亲", "361": "狗狗笑哭", "362": "好兄弟",
    "363": "狗狗可怜", "364": "超级赞", "365": "狗狗生气", "366": "芒狗",
    "367": "狗狗疑问", "368": "奥特笑哭", "369": "彩虹", "370": "祝贺",
    "371": "冒泡", "372": "气呼呼", "373": "忙", "374": "波波流泪",
    "375": "超级鼓掌", "376": "跺脚", "377": "嗨", "378": "企鹅笑哭",
    "379": "企鹅流泪", "380": "真棒", "381": "路过", "382": "emo",
    "383": "企鹅爱心", "384": "晚安", "385": "太气了", "386": "呜呜呜",
    "387": "太好笑", "388": "太头疼", "389": "太赞了", "390": "太头秃",
    "391": "太沧桑", "392": "龙年快乐", "393": "新年中龙", "394": "新年大龙",
    "395": "略略略", "396": "狼狗", "397": "抛媚眼", "398": "超级ok",
    "399": "tui", "400": "快乐", "401": "超级转圈", "402": "别说话",
    "403": "出去玩", "404": "闪亮登场", "405": "好运来", "406": "姐是女王",
    "407": "我听听", "408": "臭美", "409": "送你花花", "410": "么么哒",
    "411": "一起嗨", "412": "开心", "413": "摇起来", "415": "划龙舟",
    "416": "中龙舟", "417": "大龙舟", "419": "火车", "420": "中火车",
    "421": "大火车", "422": "粽于等到你", "423": "复兴号", "424": "续标识",
    "425": "求放过", "426": "玩火", "427": "偷感", "428": "收到",
    "429": "蛇年快乐", "430": "蛇身", "431": "蛇尾", "432": "灵蛇献瑞",
    "450": "撇嘴", "451": "色", "452": "微笑", "453": "发呆", "454": "得意",
    "455": "害羞", "456": "闭嘴", "457": "睡", "458": "我吗", "459": "优雅",
    "460": "硬撑", "461": "宕机", "462": "无语", "463": "新年快乐",
    "464": "马上到", "465": "拆红包", "466": "羞羞哒", "467": "摇花手",
    "468": "失眠", "469": "坚毅", "470": "马到成功", "472": "心动",
    "474": "给你一拳", "475": "干饭", "476": "不是吧", "477": "你懂的",
    "478": "对的对的", "479": "不对不对", "480": "散味儿", "481": "学习",
    "482": "热化了", "483": "略", "484": "比爱心", "489": "知识增加",
}


def resolve_face_label(face_id: Any, raw_text: Any = "") -> str:
    """优先清洗协议自带 faceText/name，缺失时依次查经典小黄脸与扩展超级表情表。"""
    raw = str(raw_text or "").strip()
    while len(raw) >= 2 and raw[0] == "[" and raw[-1] == "]":
        raw = raw[1:-1].strip()
    raw = raw.lstrip("/").strip()
    if raw and raw not in ("QQ表情", "表情"):
        return raw
    fid = str(face_id or "").strip()
    if not fid:
        return ""
    return FACE_LABELS.get(fid) or EXTENDED_FACE_LABELS.get(fid) or ""

_MARK_PATTERN = re.compile(r"^#+\s*", re.M)
_LIST_PATTERN = re.compile(r"^\s*[-*+]\s+", re.M)
_CODE_BLOCK = re.compile(r"```(?:[a-zA-Z0-9_+-]+[ \t]*\n|\n)?(.*?)```", re.S)
_LATEX_PATTERNS = (
    re.compile(r"\$\$(.+?)\$\$", re.S),
    re.compile(r"\\\[(.*?)\\\]", re.S),
    re.compile(r"\\\((.*?)\\\)", re.S),
    re.compile(r"(?<![\\$A-Za-z0-9_])\$(?!\s)(?![0-9]+(?:\.[0-9]+)?(?:\s|$))([^$\n]+?)(?<!\s)\$(?![A-Za-z0-9_])"),
)
_LINK_PATTERN = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")


def format_history_time(ts: float | int | None, now: float | None = None) -> str:
    """将时间戳格式化为短时间锚点，帮助模型分辨近期对话与跨时段旧消息。"""
    if not ts:
        return ""
    try:
        stamp = float(ts)
    except (TypeError, ValueError):
        return ""
    if stamp <= 0:
        return ""
    cur = time.time() if now is None else float(now)
    delta = max(0.0, cur - stamp)
    lt = time.localtime(stamp)
    if delta < 60:
        return "[{} · 刚刚]".format(time.strftime("%H:%M", lt))
    if delta < 3600:
        return "[{} · {}分钟前]".format(time.strftime("%H:%M", lt), int(delta // 60))
    if delta < 86400:
        return "[{} · {}小时前]".format(time.strftime("%H:%M", lt), int(delta // 3600))
    return "[{} · {}天前]".format(time.strftime("%m-%d %H:%M", lt), int(delta // 86400))


def clean_speaker_name(name: Any, limit: int = 40) -> str:
    """清洗群友昵称/名片：折叠换行与多余空白，剥离方括号防止伪造标签边界。"""
    s = re.sub(r"\s+", " ", str(name or "")).strip()
    s = s.replace("[", "(").replace("]", ")")
    return s[:limit].strip()


def format_speaker_message(user_name: Any, text: Any) -> str:
    """将发言人昵称包裹进 [昵称：...] 标签并与正文隔离。"""
    safe = clean_speaker_name(user_name)
    body = re.sub(r"(^|\n)\s*\[昵称[：:]", r"\1【昵称：", str(text or "").strip())
    if safe:
        return "[昵称：{}] {}".format(safe, body) if body else "[昵称：{}]".format(safe)
    return body


def clip_text(text: Any, limit: int) -> str:
    s = str(text or "").strip()
    return s if len(s) <= limit else s[:limit] + "…"


def clip_sentence(text: Any, limit: int) -> str:
    s = str(text or "").strip()
    if len(s) <= limit:
        return s
    head = s[:limit]
    best = -1
    for sep in ("。", "！", "？", "!", "?", "\n"):
        pos = head.rfind(sep)
        if pos > best:
            best = pos
    if best >= int(limit * 0.6):
        return head[:best + 1].strip()
    return clip_text(s, limit)


def _replace_md_link(match: re.Match[str]) -> str:
    label = match.group(1).strip()
    target = match.group(2).strip()
    if target.startswith(("http://", "https://")):
        if not label or label == target:
            return target
        return "{} ({})".format(label, target)
    return label


def clean_display_text(text: Any) -> str:
    """去掉 QQ 不识别的 Markdown/LaTeX 装饰，保留代码块内容与公网链接。"""
    raw = str(text or "").strip()
    if not raw:
        return ""
    blocks: list[str] = []

    def hold(match: re.Match[str]) -> str:
        blocks.append(match.group(1))
        return "\x00{}\x00".format(len(blocks) - 1)

    cleaned = _CODE_BLOCK.sub(hold, raw)
    for pattern in _LATEX_PATTERNS:
        cleaned = pattern.sub(r"\1", cleaned)
    cleaned = _LINK_PATTERN.sub(_replace_md_link, cleaned)
    cleaned = _MARK_PATTERN.sub("", cleaned)
    cleaned = _LIST_PATTERN.sub("", cleaned)
    cleaned = cleaned.replace("**", "").replace("`", "")
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    for idx, block in enumerate(blocks):
        cleaned = cleaned.replace("\x00{}\x00".format(idx), block)
    return cleaned.strip()


def detect_image_format(image_bytes: bytes) -> tuple[str, str]:
    """按文件魔数识别真实图片 MIME 与扩展名。"""
    raw = bytes(image_bytes or b"")
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", ".png"
    if raw.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", ".jpg"
    if raw.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif", ".gif"
    if len(raw) >= 12 and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp", ".webp"
    if raw.startswith(b"BM"):
        return "image/bmp", ".bmp"
    return "image/jpeg", ".jpg"


def detect_image_dimensions(image_bytes: bytes) -> tuple[int, int]:
    """用纯标准库从 PNG/GIF/JPEG/WEBP/BMP 头读取图片宽高；未知或占位字节返回 (0, 0)。"""
    raw = bytes(image_bytes or b"")
    n = len(raw)
    if n < 10:
        return 0, 0
    if raw.startswith(b"\x89PNG\r\n\x1a\n") and n >= 24:
        w, h = struct.unpack(">II", raw[16:24])
        return (w, h) if w > 0 and h > 0 else (0, 0)
    if raw.startswith((b"GIF87a", b"GIF89a")):
        w, h = struct.unpack("<HH", raw[6:10])
        return (w, h) if w > 0 and h > 0 else (0, 0)
    if raw.startswith(b"BM") and n >= 26:
        w, h = struct.unpack("<Ii", raw[18:26])
        return (w, abs(h)) if w > 0 and h != 0 else (0, 0)
    if n >= 30 and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        chunk = raw[12:16]
        if chunk == b"VP8X":
            w = int.from_bytes(raw[24:27], "little") + 1
            h = int.from_bytes(raw[27:30], "little") + 1
            return (w, h) if w > 0 and h > 0 else (0, 0)
        if chunk == b"VP8L":
            bits = int.from_bytes(raw[21:25], "little")
            w = (bits & 0x3FFF) + 1
            h = ((bits >> 14) & 0x3FFF) + 1
            return (w, h) if w > 0 and h > 0 else (0, 0)
        if chunk == b"VP8 " and raw[23:26] == b"\x9d\x01\x2a":
            w, h = struct.unpack("<HH", raw[26:30])
            w &= 0x3FFF
            h &= 0x3FFF
            return (w, h) if w > 0 and h > 0 else (0, 0)
    if raw.startswith(b"\xff\xd8"):
        i = 2
        while i + 3 < n:
            while i < n and raw[i] != 0xFF:
                i += 1
            while i < n and raw[i] == 0xFF:
                i += 1
            if i >= n:
                break
            marker = raw[i]
            i += 1
            if marker in (0xD8, 0xD9, 0x00, 0x01) or (0xD0 <= marker <= 0xD7):
                continue
            if i + 2 > n:
                break
            seg_len = struct.unpack(">H", raw[i:i + 2])[0]
            if seg_len < 2 or i + seg_len > n:
                break
            if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB):
                if i + 7 <= n:
                    h, w = struct.unpack(">HH", raw[i + 3:i + 7])
                    return (w, h) if w > 0 and h > 0 else (0, 0)
                break
            i += seg_len
    return 0, 0


@dataclass(frozen=True)
class MediaRef:
    data: bytes
    sha256: str = ""
    is_sticker: bool = False
    summary: str = ""


@dataclass(frozen=True)
class FaceRef:
    face_id: str
    label: str = ""


@dataclass(frozen=True)
class PickedEmoji:
    kind: str  # "image" | "face"
    path: str = ""
    face_id: str = ""
    tag: str = ""
    description: str = ""


@dataclass(frozen=True)
class ChatEvent:
    group_id: str
    user_id: str
    user_name: str
    text: str
    raw_text: str = ""
    msg_id: str = ""
    ts: float = 0.0
    umo: str = ""
    at_self: bool = False
    at_others: bool = False
    reply_to_bot: bool = False
    self_called: bool = False
    retold: bool = False
    direct_question: bool = False
    mentioned: bool = False
    quote_text: str = ""
    quote_sender: str = ""
    quote_msg_id: str = ""
    images: tuple[MediaRef, ...] = ()
    faces: tuple[FaceRef, ...] = ()
    has_image: bool = False
    has_sticker: bool = False
    bare_face: bool = False
    passthrough: bool = False
    is_poke: bool = False


@dataclass
class Batch:
    group_id: str
    items: list[ChatEvent] = field(default_factory=list)
    started: float = 0.0
    deadline: float = 0.0
    images: list[MediaRef] = field(default_factory=list)
    approved: bool = False
    turn_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    persisted: bool = False

    @property
    def is_poke(self) -> bool:
        return bool(self.items) and all(i.is_poke for i in self.items)

    @property
    def mentioned(self) -> bool:
        return any(i.mentioned for i in self.items)

    @property
    def retold(self) -> bool:
        # 仅当所有提及消息都是转述、且无直接非转述提及时才判为纯转述
        mentioned_items = [i for i in self.items if i.mentioned]
        if not mentioned_items:
            return any(i.retold for i in self.items)
        return all(i.retold for i in mentioned_items)

    @property
    def at_others(self) -> bool:
        return (not self.mentioned) and any(i.at_others for i in self.items)

    @property
    def umo(self) -> str:
        return self.items[-1].umo if self.items else ""

    @property
    def anchor(self) -> Optional[ChatEvent]:
        if not self.items:
            return None
        mentioned_items = [i for i in self.items if i.mentioned]
        return mentioned_items[-1] if mentioned_items else self.items[-1]

    @property
    def text(self) -> str:
        return "\n".join(
            format_speaker_message(i.user_name, i.text)
            for i in self.items
            if str(i.text or "").strip()
        )

    @property
    def body_text(self) -> str:
        return "\n".join(
            str(i.text or "").strip()
            for i in self.items
            if str(i.text or "").strip()
        )

    @property
    def last_user(self) -> str:
        return self.items[-1].user_id if self.items else ""

    @property
    def has_image(self) -> bool:
        return bool(self.images) or any(i.has_image for i in self.items)

    @property
    def quote_text(self) -> str:
        anc = self.anchor
        if anc and anc.quote_text:
            return anc.quote_text
        for item in reversed(self.items):
            if item.quote_text:
                return item.quote_text
        return ""

    @property
    def target_msg_id(self) -> str:
        anc = self.anchor
        if anc and anc.msg_id:
            return anc.msg_id
        for item in reversed(self.items):
            if item.msg_id:
                return item.msg_id
        return ""


@dataclass(frozen=True)
class ImagePart:
    data: bytes = b""
    url: str = ""
    mime: str = "image/jpeg"


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]


@dataclass(frozen=True)
class LLMMessage:
    role: str  # system / user / assistant / tool
    content: str = ""
    name: str = ""
    tool_call_id: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    images: tuple[ImagePart, ...] = ()


@dataclass(frozen=True)
class ModelTurn:
    text: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    finish_reason: str = "stop"
    usage_tokens: int = 0


@dataclass(frozen=True)
class OutboundAction:
    group_id: str
    umo: str


@dataclass(frozen=True)
class SendText(OutboundAction):
    text: str = ""
    reply_to_msg_id: str = ""


@dataclass(frozen=True)
class SendEmoji(OutboundAction):
    path: str = ""
    summary: str = "[动画表情]"


@dataclass(frozen=True)
class SendFace(OutboundAction):
    face_id: str = ""


@dataclass(frozen=True)
class SendFile(OutboundAction):
    path: str = ""
    name: str = ""


@dataclass(frozen=True)
class ReactEmoji(OutboundAction):
    msg_id: str = ""
    emoji_id: str = "124"


@dataclass(frozen=True)
class TaskRequest:
    group_id: str
    umo: str
    prompt: str
    images: tuple[bytes, ...] = ()
    pre_history: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class TaskStatusInfo:
    task_id: str
    group_id: str
    prompt: str
    elapsed: float
    progress: str = ""


@dataclass(frozen=True)
class TaskResult:
    status: str  # ok / timeout / cancelled / error / empty
    text: str = ""
    partial: str = ""
    files: tuple[dict[str, Any], ...] = ()
    elapsed: float = 0.0
    timeout: int = 300
    error: str = ""


@dataclass(frozen=True)
class PendingDelivery:
    task_id: str
    group_id: str
    umo: str
    prompt: str
    status: str
    reason: str
    result: str = ""
    partial: str = ""
    files: tuple[dict[str, Any], ...] = ()
    error: str = ""
    pre_history: tuple[dict[str, Any], ...] = ()
    created_at: float = 0.0


@dataclass(frozen=True)
class SearchHit:
    title: str
    snippet: str
    url: str = ""


@runtime_checkable
class LLMClient(Protocol):
    def chat(
        self,
        messages: Sequence[LLMMessage],
        tools: Sequence[ToolSpec] = (),
        timeout: Optional[int] = None,
    ) -> ModelTurn: ...

    def describe_image(
        self,
        image_bytes: bytes,
        prompt: str,
        timeout: Optional[int] = None,
    ) -> str: ...

    def extra_secrets(self) -> tuple[str, ...]: ...


@runtime_checkable
class SearchProvider(Protocol):
    def available(self) -> bool: ...

    def search(self, query: str, limit: int = 5) -> list[SearchHit]: ...


@runtime_checkable
class EmojiStorePort(Protocol):
    @property
    def root(self) -> Path: ...

    def collect(self, group_id: str, image_bytes: bytes, llm: LLMClient) -> str: ...

    def collect_face(self, group_id: str, face_id: str, name: str = "") -> str: ...

    def remove_face(self, group_id: str, face_id: str) -> bool: ...

    def tags(self, group_id: str, limit: int = 12) -> list[str]: ...

    def pick(self, group_id: str, keyword: str = "") -> Optional[str | Path]: ...

    def pick_entry(self, group_id: str, keyword: str = "") -> Optional[PickedEmoji]: ...

    def has_entries(self, group_id: str) -> bool: ...

    def cleanup(self, max_age_days: Optional[int] = None) -> None: ...


@runtime_checkable
class MemoryRepository(Protocol):
    def close(self) -> None: ...

    def add_message(
        self,
        group_id: str,
        user_id: str,
        user_name: str,
        role: str,
        text: str,
        ts: Optional[float] = None,
        msg_id: str = "",
        meta: str = "",
    ) -> int: ...

    def recent_messages(self, group_id: str, limit: int = 30) -> list[dict[str, Any]]: ...

    def messages_since(self, group_id: str, ts: float = 0.0, limit: int = 24) -> list[dict[str, Any]]: ...

    def prune_messages(self, keep_per_group: int = 500) -> int: ...

    def last_episode_at(self, group_id: str) -> float: ...

    def add_episode(
        self,
        group_id: str,
        text: str,
        now: Optional[float] = None,
        source: str = "",
        external_id: str = "",
    ) -> None: ...

    def recent_episodes(self, group_id: str, limit: int = 3) -> list[str]: ...

    def add_memory(self, group_id: str, category: str, content: str, now: Optional[float] = None) -> None: ...

    def recall_memories(
        self,
        group_id: str,
        query: str,
        limit: int = 6,
        now: Optional[float] = None,
    ) -> list[str]: ...

    def group_jargon(self, group_id: str, limit: int = 12) -> dict[str, str]: ...

    def observe_person(
        self,
        group_id: str,
        user_id: str,
        display_name: str,
        now: Optional[float] = None,
    ) -> None: ...

    def upsert_person(
        self,
        group_id: str,
        user_id: str,
        now: Optional[float] = None,
        merge: bool = True,
        **fields: Any,
    ) -> dict[str, Any]: ...

    def resolve_person(self, group_id: str, query_name_or_id: str) -> Optional[str]: ...

    def person_summary(self, group_id: str, user_id: str) -> str: ...

    def person_aliases(self, group_id: str, user_id: str) -> list[str]: ...

    def get_conv_state(
        self,
        group_id: str,
        max_age_seconds: float = 7200.0,
        now: Optional[float] = None,
    ) -> dict[str, list[str]]: ...

    def save_conv_state(
        self,
        group_id: str,
        state: dict[str, list[str]],
        now: Optional[float] = None,
    ) -> None: ...

    def save_pending_delivery(self, delivery: PendingDelivery) -> bool: ...

    def list_pending_deliveries(self) -> list[PendingDelivery]: ...

    def delete_pending_delivery(self, task_id: str) -> bool: ...

    def pending_delivery_paths(self) -> set[str]: ...

    def record_event(
        self,
        group_id: str,
        kind: str,
        ok: bool = True,
        latency: float = 0.0,
        tokens: int = 0,
        detail: str = "",
        now: Optional[float] = None,
    ) -> bool: ...

    def recent_error_count(self, group_id: str, window_seconds: float = 120.0, now: Optional[float] = None) -> int: ...

    def monitor_stats(self, group_id: Optional[str] = None, hours: float = 24, now: Optional[float] = None) -> dict[str, Any]: ...

    def prune_monitor(self, max_age_days: int = 30, now: Optional[float] = None) -> int: ...


@runtime_checkable
class HarnessBackend(Protocol):
    @property
    def name(self) -> str: ...

    def available(self) -> bool: ...

    def prepare_home(self, home_dir: Path) -> None: ...

    def build_command(
        self,
        prompt: str,
        workdir: Path,
        outbox_dir: Path,
        inbox_files: Sequence[Path],
        timeout: int,
    ) -> list[str]: ...

    def env_overrides(self, task_dir: Path, home_dir: Path) -> dict[str, str]: ...

    def parse_line(self, line: str) -> Optional[str]: ...

    def parse_output(self, raw_stdout: str, limit: int = 1200) -> str: ...


@runtime_checkable
class TaskRunner(Protocol):
    @property
    def outbox_root(self) -> str: ...

    @property
    def timeout(self) -> int: ...

    def available(self) -> bool: ...

    def running_for_group(self, group_id: str) -> Optional[TaskStatusInfo]: ...

    def submit(self, req: TaskRequest, ack_text: str = "") -> Optional[dict[str, Any]]: ...

    def status(self, group_id: str) -> Optional[TaskStatusInfo]: ...

    def stop(self, group_id: str) -> bool: ...

    def poll_deliveries(self) -> list[PendingDelivery]: ...

    def present_delivery(self, delivery: PendingDelivery, llm: LLMClient) -> dict[str, Any]: ...


@runtime_checkable
class MessageSender(Protocol):
    async def send(self, action: OutboundAction) -> bool: ...

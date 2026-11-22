"""统一的群消息结构。平台适配器负责把各自的消息转换成它。"""

import re
import time
from dataclasses import dataclass


def clean_speaker_name(name, limit=40):
    """清洗群友昵称/名片：折叠换行与多余空白，剥离方括号避免伪造结构边界。"""
    s = re.sub(r"\s+", " ", str(name or "")).strip()
    s = s.replace("[", "(").replace("]", ")")
    return s[:limit].strip()


def format_speaker_message(user_name, text):
    """把发言人昵称包裹进显式标签并与正文隔离，避免长昵称或签名被误读为发言内容，也防正文伪造昵称边界。"""
    safe = clean_speaker_name(user_name)
    body = re.sub(r"(^|\n)\s*\[昵称[：:]", r"\1【昵称：", str(text or "").strip())
    if safe:
        return "[昵称：{}] {}".format(safe, body) if body else "[昵称：{}]".format(safe)
    return body


@dataclass
class Message:
    group_id: str
    user_id: str
    user_name: str
    text: str
    mentioned: bool = False
    umo: str = ""
    ts: float = 0.0

    def __post_init__(self):
        if not self.ts:
            self.ts = time.time()

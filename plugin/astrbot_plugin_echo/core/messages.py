"""统一的群消息结构。平台适配器负责把各自的消息转换成它。"""

import time
from dataclasses import dataclass


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

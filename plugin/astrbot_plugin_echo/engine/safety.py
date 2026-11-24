"""硬安全边界：危险/越权探测拦截、密钥与内部路径脱敏、记忆噪音过滤。

只依赖标准库与 domain；所有安全限制均通过代码结构强执行，而不依赖模型自觉。
"""

from __future__ import annotations

import re
from typing import Any, Sequence

FILTER_WORDS = (
    "作为 AI", "作为AI", "作为一个 AI", "作为一个AI", "作为助手", "作为语言模型",
    "我是 AI", "我是AI", "我是助手", "我是语言模型", "只是个程序", "只是模型",
    "有什么可以帮", "需要我帮", "希望有帮助", "希望能帮到", "很高兴为您",
    "乐意为您",
)

UNSAFE_PATTERNS = (
    re.compile(r"无限循环|死循环|while\s*(?:true|1)", re.I | re.S),
    re.compile(r"rm\s+-rf\s+[/~]", re.I | re.S),
    re.compile(r"mkfs|dd\s+if=", re.I | re.S),
    re.compile(r"fork\s*bomb|:\(\)\s*\{", re.I | re.S),
    re.compile(r"(?:读|拿|看|导出|打印).{0,6}(?:配置|密钥|token|密码|\.env)", re.I | re.S),
    re.compile(r"(?:curl|wget).{0,60}\|\s*(?:sh|bash)", re.I | re.S),
    re.compile(r"(?<![A-Za-z0-9_-])sudo\s+", re.I),
    re.compile(r"http\.server|(?<![A-Za-z0-9_-])nc\s+-l|(?<![A-Za-z0-9_-])socat(?:\s|$)", re.I | re.S),
    re.compile(r"(?:用|开|启|启动|创建|跑|挂|执行|通过).{0,8}(?:后台线程|后台进程|守护进程|常驻进程|常驻服务|daemon)", re.I | re.S),
    re.compile(r"(?:绕过|避开|避免|突破|取消|无视).{0,6}超时", re.I | re.S),
    re.compile(r"部署.{0,10}(?:ip|公网|服务器|端口|网页|服务|上线)", re.I | re.S),
    re.compile(r"(?:暴露|开放|监听|启动|起个|起一个).{0,8}(?:端口|http\s*服务|web\s*服务|网页服务)", re.I | re.S),
    re.compile(r"(?<![A-Za-z0-9_-])(?:nohup|setsid)(?![A-Za-z0-9_-])|(?<![A-Za-z0-9_-])screen\s+-dm|(?<![A-Za-z0-9_-])tmux\s+new", re.I | re.S),
    re.compile(r"(?:忽略|无视|跳过|覆盖).{0,8}(?:(?:以上|之前|上面|全部|所有|系统)?(?:规则|限制|指令|提示|设定))", re.I | re.S),
    re.compile(r"(?:删|删除|删掉|删了|清空|清除|清理|抹掉|毁掉).{0,12}(?:根目录|所有文件|全部文件|系统文件|容器文件|全部目录|整个目录)", re.I | re.S),
    re.compile(r"(?:根目录|所有文件|全部文件|系统文件|容器文件|全部目录|整个目录).{0,12}(?:全?都?删|删除|删掉|删了|清空|清除|抹掉|毁掉)", re.I | re.S),
    re.compile(r"(?:看|查看|查|列|列出|找|扫|扫描|浏览|遍历|读|读取|打开|打印|显示|输出).{0,12}(?:根目录|文件目录|系统目录|目录结构|临时文件|容器目录|服务器目录|下面有什么文件夹|有哪些文件夹|有什么文件)", re.I | re.S),
    re.compile(r"(?:根目录|文件目录|系统目录|目录结构|临时文件|容器目录|服务器目录).{0,12}(?:列出来|列出|看看|查看|找|扫|浏览|遍历|读|打开|打印|显示|输出|下面有什么|有哪些)", re.I | re.S),
    re.compile(r"(?:在|往|到|向).{0,10}(?:根目录|系统目录|容器目录|/(?:etc|tmp|var|opt|root|home|usr|AstrBot|proc|sys)?(?:\b|/|下|里)).{0,14}(?:创建|新建|建|写|留|放|生成|保存|删|改|清空)", re.I | re.S),
    re.compile(r"(?<![A-Za-z0-9_])rm\s+-[a-z]*[rf]|shutil\.rmtree|os\.(?:remove|unlink)\s*\(", re.I | re.S),
    re.compile(r"(?:^|\s)(?:ls|ll|find|tree|cat|head|tail|stat|touch|mkdir|rmdir|chmod|chown|mv|cp)\s+(?:-[A-Za-z0-9]+\s+)*/", re.I),
    re.compile(r"(?<![A-Za-z0-9._:/-])/(?:etc|proc|sys|root|AstrBot)(?:/|\b)", re.I),
    re.compile(r"(?:查看|列出|打印|输出|看下|看看).{0,8}(?:环境变量|进程列表|系统进程|服务器信息|宿主|ip\s*地址)", re.I | re.S),
    re.compile(r"(?:看|查看|看看|看下|查|列出|打印|输出|发出来|执行|跑|运行).{0,14}(?:mount|挂载|挂载点|findmnt|lsblk|磁盘分区|文件系统|当前容器|容器内|容器信息|系统信息|内核版本|uname|whoami|printenv|ps\s+(?:aux|-ef)|df\s+-)", re.I | re.S),
    re.compile(r"(?:当前|这个|你的|运行).{0,6}(?:mount|挂载|容器|docker|宿主机|宿主|系统|linux).{0,16}(?:结果|信息|挂载|目录|文件|映射|权限|配置|进程|磁盘|发出来|看看|查看)", re.I | re.S),
    re.compile(r"(?<![A-Za-z0-9_])(?:mount|findmnt|lsblk|whoami|printenv|uname\s+-[a-z]|df\s+-[a-z]|ps\s+(?:aux|-ef)|ip\s+(?:addr|route)|ifconfig|netstat|ss\s+-[a-z])(?![A-Za-z0-9_])", re.I),
)

FAILED_LOOKUP_HINTS = (
    "没能联网", "没法核实", "无法核实", "未能核实", "未能查到",
    "查不到", "没查到", "搜索受限", "不敢乱推荐", "无法获取实时", "没弄成",
    "没法可靠对上", "还没核实", "没有可用的绘图", "没能生成", "没找到",
    "不能确认", "无法访问互联网", "当前无法联网", "无法联网", "权限不够", "不支持访问",
    "mount 信息", "挂载点", "没能从官方",
)

TRANSIENT_MEMORY_NOISE = (
    "已修复", "没收到图", "图片未成功", "图像识别", "未成功传达",
    "没能联网", "没法核实", "无法核实", "不敢乱推荐",
    "不回复", "引发不满并道歉", "重新发送", "偶尔卡顿",
    "没法可靠对上", "还没核实", "没有可用的绘图", "没能生成",
    "无法访问互联网", "当前无法联网", "临时文件", "根目录", "gx.txt", "权限不够",
    "mount 信息", "挂载点", "没能从官方",
)

UNSUPPORTED_CAP_HINTS = ("绘图", "画一张", "画张", "自画像", "文生图", "生图", "画画")
FILE_REQUEST_HINTS = ("文件", "报告", "网页", "html", "csv", "pdf", "文档", "表格", "脚本", "代码", "导出", "生成")
EPHEMERAL_TASK_HINTS = ("天气", "气温", "晚饭", "午饭", "早饭", "吃什么", "吃啥", "附近", "几点")

_INTERNAL_PATH_PATTERN = re.compile(
    r"(?<![A-Za-z0-9._:/-])/(?:AstrBot|etc|opt|tmp|var|root|home|proc|sys|dev)/[^\s)\]）】>\"'`]+"
)
_BARE_SYS_PATH_PATTERN = re.compile(
    r"(?<![A-Za-z0-9._:/-])/(?:AstrBot|etc|opt|tmp|var|root|proc|sys|dev)(?=$|[\s，,。.、)\]）】>\"'`])"
)
_PRIVATE_URL_PATTERN = re.compile(
    r"(?:https?://localhost(?::\d+)?|(?<![A-Za-z0-9_-])localhost:\d+|"
    r"(?:https?://|(?<![.\d]))(?:127\.0\.0\.1|\[::1\]|0\.0\.0\.0|169\.254\.\d+\.\d+|"
    r"10\.\d+\.\d+\.\d+|172\.(?:1[6-9]|2\d|3[01])\.\d+\.\d+|192\.168\.\d+\.\d+)"
    r"(?::\d+)?(?![.\d]))(?:/[^\s)\]）】>\"'`]*)?"
)
_WS_URL_PATTERN = re.compile(r"wss?://[^\s)\]）】>\"'`]+", re.I)
_SK_TOKEN_PATTERN = re.compile(r"(?<![A-Za-z0-9_-])sk-[A-Za-z0-9_-]{16,}(?![A-Za-z0-9_-])")
_EMPTY_MD_LINK_PATTERN = re.compile(r"\[[^\]]*\]\(\s*\)")
_EMPTY_BACKTICK_PATTERN = re.compile(r"(?<!`)``(?!`)|(?<!`)`\s*`(?!`)")
_RAW_MENTION_QQ_PATTERN = re.compile(r"@([^\s@()（）]{1,24})\(\d{5,12}\)")
_META_REPLY_SPLIT = re.compile(
    r"(?:^|\n)\s*(?:(?:群里|群聊|直接)?(?:可以)?(?:这么|这样)回[复答]?|(?:建议|参考|可直接发送的?)(?:群聊)?回复|回复建议)\s*[：:]\s*",
    re.S,
)
_QUOTE_PAIRS = (("“", "”"), ('"', '"'), ("「", "」"), ("『", "』"), ("'", "'"))


def is_unsafe(text: Any) -> bool:
    raw = str(text or "")
    if not raw.strip():
        return False
    if any(p.search(raw) for p in UNSAFE_PATTERNS):
        return True
    collapsed = re.sub(r"\s+", " ", raw).strip()
    return collapsed != raw and any(p.search(collapsed) for p in UNSAFE_PATTERNS)


def is_boilerplate(text: Any) -> bool:
    s = str(text or "")
    return any(w in s for w in FILTER_WORDS)


def is_failed_lookup(text: Any) -> bool:
    s = str(text or "")
    return any(h in s for h in FAILED_LOOKUP_HINTS)


def is_transient_memory_noise(text: Any) -> bool:
    clean = str(text or "").strip()
    return not clean or any(h in clean for h in TRANSIENT_MEMORY_NOISE)


def prompt_requests_file(prompt: Any) -> bool:
    low = str(prompt or "").lower()
    return any(h in low for h in FILE_REQUEST_HINTS)


def is_memorable_takeaway(prompt: Any, text: Any) -> bool:
    if is_failed_lookup(text):
        return False
    p = str(prompt or "")
    if any(h in p for h in EPHEMERAL_TASK_HINTS) and not prompt_requests_file(p):
        return False
    return True


def is_unsupported_capability(text: Any) -> bool:
    s = str(text or "")
    return any(k in s for k in UNSUPPORTED_CAP_HINTS)


def scrub_internal_leaks(text: Any, extra_secrets: Sequence[str] = ()) -> str:
    """清除意外露出的容器路径、内网/WS URL 与运行密钥，避免发到群里。"""
    s = str(text or "")
    for secret in extra_secrets or ():
        sec = str(secret or "").strip()
        if len(sec) >= 8 and sec in s:
            s = s.replace(sec, "")
    s = _WS_URL_PATTERN.sub("", s)
    s = _PRIVATE_URL_PATTERN.sub("", s)
    s = _INTERNAL_PATH_PATTERN.sub("", s)
    s = _BARE_SYS_PATH_PATTERN.sub("", s)
    s = _SK_TOKEN_PATTERN.sub("", s)
    s = _EMPTY_MD_LINK_PATTERN.sub("", s)
    s = _EMPTY_BACKTICK_PATTERN.sub("", s)
    s = _RAW_MENTION_QQ_PATTERN.sub(r"@\1", s)
    s = re.sub(r"(?m)^\s*[-*•]\s*(?:文件|网页|链接|地址)?\s*[：:]?\s*$", "", s)
    s = re.sub(r"(?<=\S)[ \t]{2,}", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def _strip_outer_quotes(text: str) -> str:
    s = str(text or "").strip()
    for left, right in _QUOTE_PAIRS:
        if len(s) >= len(left) + len(right) and s.startswith(left) and s.endswith(right):
            return s[len(left):len(s) - len(right)].strip()
    return s


def sanitize_meta_reply(text: Any, extra_secrets: Sequence[str] = ()) -> str:
    """剥离「群里可以这么回：」等草稿外壳并脱敏。"""
    cleaned = scrub_internal_leaks(str(text or "").strip(), extra_secrets=extra_secrets)
    if not cleaned:
        return ""
    match = _META_REPLY_SPLIT.search(cleaned)
    if not match:
        return cleaned
    tail = _strip_outer_quotes(cleaned[match.end():])
    if tail:
        return scrub_internal_leaks(tail, extra_secrets=extra_secrets)
    return scrub_internal_leaks(cleaned[:match.start()].strip(), extra_secrets=extra_secrets)

"""合并转发解析：把平台消息里的 Forward 组件转成可读文本。

MaiBot 有 `view_forward_message` 工具；我们没有统一的组件定义，所以这里
按「字典/对象都试一遍」的容错方式取值，拿不到就返回空，绝不让主流程报错。
只依赖标准库。
"""

from .messages import format_speaker_message

MAX_ITEMS = 20
MAX_CHARS = 800


def _get(node, *names, default=None):
    for name in names:
        if isinstance(node, dict) and name in node:
            return node[name]
        value = getattr(node, name, None)
        if value is not None:
            return value
    return default


def looks_like_forward(component):
    name = type(component).__name__.lower()
    if "forward" in name or name in ("nodes", "node"):
        return True
    if isinstance(component, dict):
        kind = str(component.get("type") or "").lower()
        return "forward" in kind or kind in ("nodes", "node")
    return False


def _segment_text(part):
    if isinstance(part, str):
        return part
    if isinstance(part, dict):
        data = part.get("data")
        if isinstance(data, dict):
            txt = _get(data, "text", "content", default="")
            if txt:
                return str(txt)
        return str(_get(part, "text", "content", default="") or "")
    return str(_get(part, "text", "content", default="") or "")


def extract(component):
    """取出转发里的每条消息文本；结构不认识时返回空列表。"""
    name = type(component).__name__.lower()
    dict_kind = str(component.get("type") or "").lower() if isinstance(component, dict) else ""
    if name == "node" or dict_kind == "node":
        content = [component]
    else:
        content = _get(component, "content", "messages", "nodes", "message", default=None)
        if content is None and isinstance(component, dict) and isinstance(component.get("data"), dict):
            content = _get(component["data"], "messages", "content", "nodes", "message", default=None)
        if content is None:
            return []
        if isinstance(content, dict):
            content = _get(content, "messages", "content", "nodes", default=[])
        if not isinstance(content, (list, tuple)):
            return []
    lines = []
    for node in list(content)[:MAX_ITEMS]:
        if isinstance(node, dict) and isinstance(node.get("data"), dict):
            node = node["data"]
        sender = _get(node, "sender", "name", "nickname", "user_name", default="")
        if sender and not isinstance(sender, str):
            sender = _get(sender, "card", "nickname", "name", default="")
        body = _get(node, "content", "message", "text", default="")
        if isinstance(body, (list, tuple)):
            body = "".join(_segment_text(part) for part in body)
        elif isinstance(body, dict):
            body = _segment_text(body)
        text = str(body or "").strip().replace("\n", " ")
        if text:
            lines.append(format_speaker_message(sender or "群友", text[:80]))
    return lines


def render(lines, limit=MAX_CHARS):
    if not lines:
        return ""
    text = "；".join(lines)
    if len(text) > limit:
        text = text[:limit] + "…"
    return "（转发的聊天）" + text

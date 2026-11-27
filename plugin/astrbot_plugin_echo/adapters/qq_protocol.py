"""QQ / NapCat / OneBot 协议辅助函数、SSRF 公网下载校验、图片区分与合并转发解包。"""

from __future__ import annotations

import ipaddress
import re
import socket
import struct
import types
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional

from ..domain import detect_image_dimensions, format_speaker_message, resolve_face_label

MAX_DOWNLOAD_BYTES = 2 * 1024 * 1024
MAX_FORWARD_ITEMS = 20
MAX_FORWARD_CHARS = 800

RETOLD_MARKERS = (
    "他说你", "她说你", "他们说", "有人说你", "听说你", "人家说你",
    "跟我讲你", "跟你讲你", "转述", "评价你", "骂你", "说你坏话",
    "说你不行", "说你不", "吐槽你", "背后说",
)
QUESTION_TAIL = ("吗", "呢", "么", "?", "？")
QUESTION_WORDS = (
    "怎么", "为什么", "为啥", "是不是", "能不能", "要不要",
    "有没有", "什么", "啥", "谁", "多少", "多久", "哪个", "哪里", "哪儿", "哪种",
)
_NON_RETOLD_PREFIXES = (
    "话说", "别说", "再说", "别讲", "要说", "虽说", "瞎说", "乱说",
    "怎么说", "这么说", "那样说", "这样说", "就是说", "也就是说", "刚才说",
)
_SELF_SPEAKER_SUBJ = re.compile(
    r"^(?:但是|不过|其实|刚才|刚刚|然后|所以|反正|而且|但|可|可是|那|嗯|哎|诶)?"
    r"(?:我们|咱们|你们|我|咱|俺|你|您)"
    r"(?:不是|只是|就是|本来|其实|刚才|刚刚|已经|真的|没有|不会|不要|不能|不想|不敢|"
    r"跟我|跟你|和他|和她|对你|对我|跟|和|对|就|是|想|只|才|也|还|又|都|刚|先|再|真|没|不|别){0,2}$"
)
_RETOLD_DYNAMIC = re.compile(
    r"(?:^|[\s，,。！？])"
    r"(?P<subj>[\u4e00-\u9fffA-Za-z0-9_]{1,6})"
    r"(?:在)?(?:说|讲|评价)(?:你|他|她)"
)
_RPS_NAMES = {"1": "布", "2": "剪刀", "3": "石头"}


def is_question(text: str) -> bool:
    clean = str(text or "").strip()
    if not clean:
        return False
    for line in clean.splitlines():
        if line.strip().endswith(QUESTION_TAIL):
            return True
    return any(w in clean for w in QUESTION_WORDS)


def is_retold(text: str) -> bool:
    clean = str(text or "")
    if not clean:
        return False
    if any(m in clean for m in RETOLD_MARKERS):
        return True
    for m in _RETOLD_DYNAMIC.finditer(clean):
        matched = m.group(0).lstrip(" \t\r\n，,。！？")
        if matched.startswith(_NON_RETOLD_PREFIXES):
            continue
        if _SELF_SPEAKER_SUBJ.match(m.group("subj")):
            continue
        return True
    return False


def detect_mentions(
    text: str,
    *,
    at_self: bool = False,
    reply_to_bot: bool = False,
    self_names: tuple[str, ...] = ("大肥鱼", "回声"),
) -> tuple[bool, bool, bool, bool]:
    """返回 (mentioned, self_called, retold, direct_question)。"""
    clean = str(text or "")
    lowered = clean.lower()
    names = [n for n in self_names if n]
    text_at = any(
        ("@" + n in clean or "＠" + n in clean or "@" + n.lower() in lowered or "＠" + n.lower() in lowered)
        for n in names
    )
    self_called = False
    heads = [line.lstrip("，,。. ") for line in clean.splitlines() if line.strip()]
    for n in names:
        if any(h.startswith(n) for h in heads):
            self_called = True
            break
        if any(n in line and is_question(line) for line in clean.splitlines()):
            self_called = True
            break
    mentioned = bool(at_self or text_at or reply_to_bot or self_called)
    return mentioned, self_called, is_retold(clean), is_question(clean)


def is_public_http_url(url: str) -> bool:
    """校验 URL 必须为公网 http/https，解析 DNS 后拦截回环、内网与链路本地地址（防 SSRF）。"""
    try:
        parsed = urllib.parse.urlsplit(str(url or "").strip())
    except ValueError:
        return False
    if parsed.scheme.lower() not in ("http", "https"):
        return False
    host = parsed.hostname
    if not host:
        return False
    port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError:
        return False
    if not infos:
        return False
    for info in infos:
        try:
            ip_obj = ipaddress.ip_address(info[4][0])
            mapped = getattr(ip_obj, "ipv4_mapped", None)
            if mapped is not None:
                ip_obj = mapped
        except ValueError:
            return False
        if not ip_obj.is_global:
            return False
    return True


def looks_like_image(data: bytes) -> bool:
    if not data or len(data) < 4 or len(data) > MAX_DOWNLOAD_BYTES:
        return False
    if data.startswith(b"\x89PNG\r\n\x1a\n") or data.startswith(b"\xff\xd8\xff"):
        return True
    if data.startswith((b"GIF87a", b"GIF89a")):
        return True
    if data.startswith(b"BM"):
        if len(data) < 26:
            return False
        dib_size = struct.unpack("<I", data[14:18])[0]
        return dib_size in (12, 40, 52, 56, 108, 124)
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return True
    return False


def raw_image_metas(raw_msg: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_msg, dict) or not isinstance(raw_msg.get("message"), list):
        return []
    return [
        seg["data"]
        for seg in raw_msg["message"]
        if isinstance(seg, dict) and seg.get("type") == "image" and isinstance(seg.get("data"), dict)
    ]


def raw_face_texts(raw_msg: Any) -> dict[str, str]:
    out: dict[str, str] = {}
    if not isinstance(raw_msg, dict) or not isinstance(raw_msg.get("message"), list):
        return out
    for seg in raw_msg["message"]:
        if not isinstance(seg, dict) or seg.get("type") != "face":
            continue
        data = seg.get("data") or {}
        fid = str(data.get("id") or "").strip()
        if not fid or fid in out:
            continue
        raw_obj = data.get("raw") if isinstance(data.get("raw"), dict) else {}
        ft = str(raw_obj.get("faceText") or data.get("faceText") or "").strip()
        if ft:
            out[fid] = ft
    return out


def raw_special_tags(raw_msg: Any) -> list[str]:
    out: list[str] = []
    if not isinstance(raw_msg, dict) or not isinstance(raw_msg.get("message"), list):
        return out
    for seg in raw_msg["message"]:
        if not isinstance(seg, dict):
            continue
        stype = str(seg.get("type") or "").strip().lower()
        data = seg.get("data") if isinstance(seg.get("data"), dict) else {}
        if stype == "dice":
            res = str(data.get("result") or "").strip()
            out.append("[骰子:{}点]".format(res) if res else "[骰子]")
        elif stype == "rps":
            res = str(data.get("result") or "").strip()
            label = _RPS_NAMES.get(res, res)
            out.append("[猜拳:{}]".format(label) if label else "[猜拳]")
        if len(out) >= 3:
            break
    return out


def match_raw_image_meta(part: Any, raw_metas: list[dict[str, Any]], index: int) -> Optional[dict[str, Any]]:
    if not raw_metas:
        return None
    part_url = str(getattr(part, "url", "") or "").strip()
    part_file = str(getattr(part, "file", "") or "").strip()
    for meta in raw_metas:
        m_url = str(meta.get("url") or "").strip()
        m_file = str(meta.get("file") or "").strip()
        if (part_url and m_url and part_url == m_url) or (part_file and m_file and part_file == m_file):
            return meta
    return raw_metas[index] if 0 <= index < len(raw_metas) else None


def is_sticker_image(part: Any, data: bytes, raw_meta: Optional[dict[str, Any]] = None) -> bool:
    """fail-closed 区分表情包与普通截图/照片：先卡物理几何尺寸，再读 NapCat raw_message 元数据。"""
    w, h = detect_image_dimensions(data)
    if w > 0 and h > 0:
        long_side = max(w, h)
        if long_side > 1280 or (long_side / float(min(w, h))) > 1.85:
            return False
    meta = raw_meta if isinstance(raw_meta, dict) else {}
    sub_type = meta.get("sub_type")
    if sub_type is None:
        sub_type = meta.get("subType")
    if sub_type is None:
        sub_type = getattr(part, "subType", None)
    if sub_type is None:
        sub_type = getattr(part, "sub_type", None)
    summary = str(meta.get("summary") or getattr(part, "summary", "") or "").strip()

    if sub_type is not None and str(sub_type).strip() == "0":
        return False
    if sub_type is not None and str(sub_type).strip().lower() in ("1", "2", "sticker", "emoji", "mface"):
        return True
    if "表情" in summary:
        return True
    if summary in ("[图片]", "图片"):
        return False
    if data.startswith((b"GIF87a", b"GIF89a")) and len(data) <= 500 * 1024:
        return True
    return False


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        hops = getattr(req, "_echo_hops", 0) + 1
        if hops > 3 or not is_public_http_url(newurl):
            raise urllib.error.URLError("blocked unsafe redirect")
        nxt = super().redirect_request(req, fp, code, msg, headers, newurl)
        if nxt is not None:
            nxt._echo_hops = hops
        return nxt


def _download_public_bytes(url: str) -> bytes:
    if not is_public_http_url(url):
        return b""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _SafeRedirectHandler())
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://qq.com/"})
    with opener.open(request, timeout=15) as resp:
        data = resp.read(MAX_DOWNLOAD_BYTES + 1)
    return b"" if len(data) > MAX_DOWNLOAD_BYTES else data


def _get_attr_or_key(node: Any, *names: str, default: Any = None) -> Any:
    for name in names:
        if isinstance(node, dict) and name in node:
            val = node[name]
            if val is not None and val != "" and val != []:
                return val
            continue
        val = getattr(node, name, None)
        if val is not None and val != "" and val != []:
            return val
    return default


def looks_like_forward(comp: Any) -> bool:
    name = type(comp).__name__.lower()
    if "forward" in name or name in ("nodes", "node"):
        return True
    if isinstance(comp, dict):
        kind = str(comp.get("type") or "").lower()
        return "forward" in kind or kind in ("nodes", "node")
    return False


def unpack_forward(comp: Any, _depth: int = 0) -> str:
    if _depth > 2:
        return ""
    name = type(comp).__name__.lower()
    dict_kind = str(comp.get("type") or "").lower() if isinstance(comp, dict) else ""
    if name == "node" or dict_kind == "node":
        content = [comp]
    else:
        content = _get_attr_or_key(comp, "content", "messages", "nodes", "message", default=None)
        if content is None and isinstance(comp, dict) and isinstance(comp.get("data"), dict):
            content = _get_attr_or_key(comp["data"], "messages", "content", "nodes", "message", default=None)
        if isinstance(content, dict):
            content = _get_attr_or_key(content, "messages", "content", "nodes", default=[])
        if not isinstance(content, (list, tuple)):
            return ""

    def seg_text(p: Any) -> str:
        if isinstance(p, str):
            return p
        if isinstance(p, (list, tuple)):
            return "".join(seg_text(x) for x in p)
        if isinstance(p, dict):
            p_type = str(p.get("type") or "").lower()
            pdata = p.get("data") if isinstance(p.get("data"), dict) else p
            if p_type in ("forward", "node", "nodes"):
                nested = unpack_forward(p, _depth=_depth + 1)
                return (" " + nested + " ") if nested else ""
            if p_type == "image":
                return "[图片]"
            if p_type == "mface":
                msum = str(pdata.get("summary") or "").strip().strip("[]")
                return "[商城表情:{}]".format(msum or "表情")
            if p_type == "face":
                fid = str(pdata.get("id") or "").strip()
                raw_obj = pdata.get("raw") if isinstance(pdata.get("raw"), dict) else {}
                ft = str(raw_obj.get("faceText") or pdata.get("faceText") or pdata.get("name") or "").strip()
                fn = resolve_face_label(fid, ft)
                return "[QQ表情:{}]".format(fn or (("ID" + fid) if fid else "表情"))
            t = _get_attr_or_key(pdata, "text", "content", default="")
            if isinstance(t, (list, tuple, dict)):
                return seg_text(t)
            return str(t or "")
        if looks_like_forward(p):
            nested = unpack_forward(p, _depth=_depth + 1)
            return (" " + nested + " ") if nested else ""
        t = _get_attr_or_key(p, "text", "content", default="")
        if isinstance(t, (list, tuple, dict)):
            return seg_text(t)
        return str(t or "")

    lines = []
    for node in list(content)[:MAX_FORWARD_ITEMS]:
        if isinstance(node, dict) and isinstance(node.get("data"), dict):
            node = node["data"]
        sender = _get_attr_or_key(node, "sender", "name", "nickname", "user_name", default="")
        if sender and not isinstance(sender, str):
            sender = _get_attr_or_key(sender, "card", "nickname", "name", default="")
        body = _get_attr_or_key(node, "content", "message", "text", default="")
        if isinstance(body, (list, tuple)):
            body = "".join(seg_text(x) for x in body)
        elif isinstance(body, dict) or looks_like_forward(body):
            body = seg_text(body)
        txt = re.sub(r"\s+", " ", str(body or "")).strip()
        if txt:
            lines.append(format_speaker_message(sender or "群友", txt[:80]))
    if not lines:
        return ""
    joined = "；".join(lines)
    if len(joined) > MAX_FORWARD_CHARS:
        joined = joined[:MAX_FORWARD_CHARS] + "…"
    return joined if _depth > 0 else ("（转发的聊天）" + joined)


def extract_replied_media_jobs(
    raw_replied: Any,
) -> tuple[list[tuple[Any, Optional[dict[str, Any]], bool]], list[str], list[str]]:
    """从 OneBot `get_msg` 响应中提取引用消息里的图片/商城表情下载任务、文案标签与嵌套转发 ID。"""
    img_jobs: list[tuple[Any, Optional[dict[str, Any]], bool]] = []
    extra_tags: list[str] = []
    forward_ids: list[str] = []
    if not isinstance(raw_replied, dict):
        return img_jobs, extra_tags, forward_ids
    raw_segs = raw_replied.get("message")
    if not isinstance(raw_segs, list) and isinstance(raw_replied.get("data"), dict):
        raw_segs = raw_replied["data"].get("message")
    if not isinstance(raw_segs, list):
        return img_jobs, extra_tags, forward_ids

    for seg in raw_segs:
        if not isinstance(seg, dict):
            continue
        stype = str(seg.get("type") or "").strip().lower()
        sdata = seg.get("data") if isinstance(seg.get("data"), dict) else {}
        if stype == "mface":
            msum = str(sdata.get("summary") or "").strip().strip("[]")
            murl = str(sdata.get("url") or "").strip()
            mtag = "[商城表情:{}]".format(msum or "表情")
            if mtag not in extra_tags:
                extra_tags.append(mtag)
            if murl and len(img_jobs) < 2:
                img_jobs.append((
                    types.SimpleNamespace(url=murl, file="", path="", base64="", subType="1", summary=msum),
                    {"sub_type": "1", "summary": msum or "表情"},
                    False,
                ))
        elif stype == "image":
            iurl = str(sdata.get("url") or "").strip()
            ifile = str(sdata.get("file") or "").strip()
            funiq = str(sdata.get("file_unique") or "").strip()
            if (iurl or ifile or funiq) and len(img_jobs) < 2:
                img_jobs.append((
                    types.SimpleNamespace(url=iurl, file=ifile, file_unique=funiq, path="", base64=""),
                    sdata,
                    False,
                ))
        elif stype == "forward":
            fwd_q = unpack_forward(seg)
            if fwd_q:
                if fwd_q not in extra_tags:
                    extra_tags.append(fwd_q)
            elif sdata.get("id"):
                forward_ids.append(str(sdata["id"]))
    return img_jobs, extra_tags, forward_ids

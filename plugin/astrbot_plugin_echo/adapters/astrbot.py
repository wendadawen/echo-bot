"""AstrBot + NapCat 协议防腐层：彻底隔离框架组件怪癖与协议字段丢失。

职责边界：
- Inbound（parse_event）：结合 AstrBot event.get_messages() 与 OneBot/NapCat raw_message，
  一次性还原真实 msg_id、sub_type/summary、mface、合并转发、引用消息、@他人、转述与安全图片下载，
  对外产出干净不可变的 ChatEvent。
- Outbound（AstrBotSender）：执行 SendText（含智能 Reply(id=...)）、SendFace、SendEmoji（jail 到表情库）、
  SendFile（jail 到 outbox_root 并清理）、ReactEmoji（set_msg_emoji_like）与滑动窗口限流。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import ipaddress
import re
import socket
import struct
import time
import types
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from ..domain import (
    ChatEvent,
    FaceRef,
    MediaRef,
    OutboundAction,
    ReactEmoji,
    SendEmoji,
    SendFace,
    SendFile,
    SendText,
    clean_display_text,
    detect_image_dimensions,
    format_speaker_message,
    resolve_face_label,
)
from ..workers.sandbox import cleanup_file_entry

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


_RPS_NAMES = {"1": "布", "2": "剪刀", "3": "石头"}


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


class MessageLimiter:
    """滑动窗口发送限流：每群每分钟最多 per_minute 条，避免刷屏。"""

    def __init__(self, per_minute: int = 8):
        self.per_minute = max(0, int(per_minute))
        self._sent: dict[str, list[float]] = {}

    def _trim(self, group_id: str, now: float) -> list[float]:
        gid = str(group_id)
        old = self._sent.get(gid)
        if not old:
            return []
        rows = [ts for ts in old if now - ts < 60.0]
        if rows:
            self._sent[gid] = rows
        else:
            self._sent.pop(gid, None)
        return rows

    def allow(self, group_id: str, count: int = 1, now: Optional[float] = None) -> bool:
        cur = time.monotonic() if now is None else float(now)
        return len(self._trim(group_id, cur)) + count <= self.per_minute

    def record(self, group_id: str, count: int = 1, now: Optional[float] = None) -> int:
        cur = time.monotonic() if now is None else float(now)
        if self.per_minute <= 0:
            self._sent.pop(str(group_id), None)
            return 0
        rows = self._trim(group_id, cur)
        rows.extend([cur] * max(0, int(count)))
        kept = rows[-self.per_minute:]
        if kept:
            self._sent[str(group_id)] = kept
        else:
            self._sent.pop(str(group_id), None)
        return len(kept)

    def try_acquire(self, group_id: str, count: int = 1, now: Optional[float] = None) -> bool:
        """原子校验并预占发送额度，防止并发协程下的 check-then-act 穿透。"""
        cur = time.monotonic() if now is None else float(now)
        if self.per_minute <= 0 or count <= 0:
            return False
        rows = self._trim(group_id, cur)
        if len(rows) + int(count) > self.per_minute:
            return False
        rows.extend([cur] * int(count))
        self._sent[str(group_id)] = rows[-self.per_minute:]
        return True


class AstrBotInboundParser:
    """把 AstrBot AstrMessageEvent + NapCat raw_message 清洗为标准 ChatEvent。"""

    def __init__(
        self,
        comp_types: dict[str, Any],
        downloader: Optional[Callable[[str], bytes]] = None,
        allowed_local_roots: Sequence[str | Path] = (),
    ):
        self._comp = comp_types
        self._download = downloader or _download_public_bytes
        self._download_sem: Optional[asyncio.Semaphore] = None
        self._allowed_roots: tuple[Path, ...] = tuple(
            Path(r).resolve(strict=False) for r in (allowed_local_roots or ()) if str(r or "").strip()
        )

    def _is_safe_local_file(self, raw_path: str) -> Optional[Path]:
        """fail-closed 本地图片路径校验：必须在显式白名单目录内且路径链条无任何符号链接。"""
        if not raw_path or not self._allowed_roots:
            return None
        try:
            p = Path(raw_path)
            resolved = p.resolve(strict=True)
        except OSError:
            return None
        if not resolved.is_file():
            return None
        for root in self._allowed_roots:
            if root in resolved.parents:
                cur = p
                while True:
                    if cur.is_symlink():
                        return None
                    try:
                        if cur.resolve(strict=False) == root:
                            return resolved
                    except OSError:
                        return None
                    parent = cur.parent
                    if parent == cur:
                        break
                    cur = parent
        return None

    @staticmethod
    def _extract_raw_text(event: Any, parts: list[Any], plain_cls: Any) -> str:
        msg_str = (event.get_message_str() or "").strip()
        if msg_str.startswith(("/", "／")):
            return msg_str
        plain_str = "".join(
            str(getattr(p, "text", "") or "")
            for p in parts
            if isinstance(p, plain_cls)
        ).strip()
        obj_str = str(getattr(getattr(event, "message_obj", None), "message_str", "") or "").strip()
        for cand in (plain_str, obj_str):
            stripped = cand.lstrip()
            if stripped.startswith(("/", "／")) and (not msg_str or stripped[1:].strip() == msg_str):
                return stripped
        return msg_str

    @staticmethod
    def _has_passthrough_component(parts: list[Any]) -> bool:
        pass_types = {"json", "share", "record", "video", "music", "location"}
        for part in parts:
            cls_name = type(part).__name__.lower()
            comp_type = str(getattr(part, "type", "") or "").lower()
            if cls_name in pass_types or comp_type in pass_types:
                return True
        return False

    def _extract_quote(self, parts: list[Any]) -> tuple[str, str, str]:
        reply_cls = self._comp["Reply"]
        img_cls = self._comp["Image"]
        face_cls = self._comp["Face"]
        for part in parts:
            if not isinstance(part, reply_cls):
                continue
            qid = str(getattr(part, "id", "") or "").strip()
            sname = str(getattr(part, "sender_nickname", "") or "").strip()
            chain = getattr(part, "chain", None) or []
            body = str(getattr(part, "message_str", "") or getattr(part, "text", "") or "").strip()
            if not body:
                segs = []
                for seg in chain:
                    if isinstance(seg, img_cls):
                        segs.append("[图片]")
                    elif isinstance(seg, face_cls):
                        fid = str(getattr(seg, "id", "") or "").strip()
                        fn = resolve_face_label(fid, getattr(seg, "name", ""))
                        segs.append("[QQ表情:{}]".format(fn or (("ID" + fid) if fid else "表情")))
                    else:
                        txt = getattr(seg, "text", None)
                        if txt:
                            segs.append(str(txt))
                body = " ".join(s.strip() for s in segs if str(s or "").strip()).strip()
            if sname and body:
                return format_speaker_message(sname, body), sname, qid
            if body:
                return body, sname, qid
            if sname:
                return "回复 {}".format(sname), sname, qid
        return "", "", ""

    async def _download_part_bytes(self, part: Any) -> bytes:

        url = str(getattr(part, "url", "") or "").strip()
        file_ref = str(getattr(part, "file", "") or "").strip()
        path_ref = str(getattr(part, "path", "") or "").strip()
        b64_ref = str(getattr(part, "base64", "") or "").strip()
        data = b""
        if self._download_sem is None:
            self._download_sem = asyncio.Semaphore(4)
        async with self._download_sem:
            for remote_url in dict.fromkeys((url, file_ref)):
                if not data and remote_url.startswith(("http://", "https://")):
                    try:
                        data = await asyncio.to_thread(self._download, remote_url)
                    except Exception:
                        data = b""
            if not data:
                for b64_src in (b64_ref, file_ref):
                    if b64_src.startswith("base64://") and len(b64_src) < 3 * 1024 * 1024:
                        try:
                            data = base64.b64decode(b64_src[9:], validate=False)
                        except Exception:
                            data = b""
                        break
            if not data:
                candidates = []
                if path_ref:
                    candidates.append(path_ref)
                if file_ref.startswith("file://"):
                    candidates.append(urllib.parse.unquote(urllib.parse.urlsplit(file_ref).path))
                elif file_ref and not file_ref.startswith(("http://", "https://", "base64://")):
                    candidates.append(file_ref)
                for lp in candidates:
                    safe_local = self._is_safe_local_file(lp)
                    if safe_local is None:
                        continue
                    try:
                        if safe_local.stat().st_size <= MAX_DOWNLOAD_BYTES:
                            data = await asyncio.to_thread(safe_local.read_bytes)
                            if data:
                                break
                    except OSError:
                        data = b""
        return data if looks_like_image(data) else b""

    async def parse(self, event: Any, last_bot_reply_at: float = 0.0) -> Optional[ChatEvent]:
        raw_gid = event.get_group_id()
        if not raw_gid:
            return None
        group_id = "{}:{}".format(event.get_platform_name(), raw_gid)
        umo = str(event.unified_msg_origin or "")
        sender = str(event.get_sender_id() or "")
        user_name = str(event.get_sender_name() or sender)
        self_id = str(event.get_self_id() or "")
        parts = list(event.get_messages() or ())
        msg_obj = getattr(event, "message_obj", None)
        raw_msg = getattr(msg_obj, "raw_message", None)

        # 戳一戳事件：仅响应针对大肥鱼的戳一戳，群友互戳或自身回声一律放行忽略
        poke_part = None
        for part in parts:
            cls_name = type(part).__name__.lower()
            comp_type = str(getattr(part, "type", "") or "").lower()
            if cls_name in ("poke", "_poke") or comp_type == "poke" or comp_type.endswith(".poke"):
                poke_part = part
                break
        is_raw_poke = isinstance(raw_msg, dict) and str(raw_msg.get("sub_type") or "").lower() == "poke"
        if poke_part is not None or is_raw_poke:
            target_id = ""
            if poke_part is not None:
                for attr in ("id", "qq"):
                    val = str(getattr(poke_part, attr, "") or "").strip()
                    if val and val != "0":
                        target_id = val
                        break
            if not target_id and isinstance(raw_msg, dict):
                val = str(raw_msg.get("target_id") or "").strip()
                if val and val != "0":
                    target_id = val
            if not self_id or target_id != self_id or sender == self_id:
                return ChatEvent(
                    group_id=group_id,
                    user_id=sender,
                    user_name=user_name,
                    text="",
                    raw_text="",
                    msg_id="",
                    ts=time.time(),
                    umo=umo,
                    passthrough=True,
                )
            return ChatEvent(
                group_id=group_id,
                user_id=sender,
                user_name=user_name,
                text="（戳了戳你）",
                raw_text="",
                msg_id="",
                ts=time.time(),
                umo=umo,
                mentioned=True,
                is_poke=True,
                passthrough=False,
            )

        msg_id = str(
            (raw_msg.get("message_id") if isinstance(raw_msg, dict) else None)
            or getattr(msg_obj, "message_id", "")
            or ""
        ).strip()

        raw_text = self._extract_raw_text(event, parts, self._comp["Plain"])
        text = raw_text

        at_cls = self._comp["At"]
        reply_cls = self._comp["Reply"]
        face_cls = self._comp["Face"]
        img_cls = self._comp["Image"]

        at_self = False
        at_others = False
        reply_to_bot = False
        for part in parts:
            if isinstance(part, at_cls):
                qq = str(getattr(part, "qq", "") or "")
                if self_id and qq == self_id:
                    at_self = True
                elif qq and qq != "all":
                    at_others = True
            elif isinstance(part, reply_cls) and self_id and str(getattr(part, "sender_id", "")) == self_id:
                reply_to_bot = True

        mentioned, self_called, retold, direct_question = detect_mentions(
            text, at_self=at_self, reply_to_bot=reply_to_bot
        )
        if not mentioned and text.startswith(("/", "／")):
            mentioned = bool(getattr(event, "is_at_or_wake_command", lambda: False)())

        # 合并转发解包（支持夹带 @/正文的合并转发与多层回查）
        fwd_chunks = []
        bot = getattr(event, "bot", None)
        for comp in parts:
            if looks_like_forward(comp):
                rendered = unpack_forward(comp)
                if not rendered:
                    fid = getattr(comp, "id", None)
                    if bot and fid and hasattr(bot, "call_action"):
                        try:
                            raw_fwd = await asyncio.wait_for(bot.call_action("get_forward_msg", id=fid), timeout=5.0)
                            rendered = unpack_forward(raw_fwd)
                        except Exception:
                            rendered = ""
                if rendered:
                    fwd_chunks.append(rendered)
        forward_text = " ".join(fwd_chunks).strip()

        # 含链接且未 @，或直通媒体卡片：交还后续插件放行
        if ("http://" in text or "https://" in text) and not mentioned:
            return ChatEvent(
                group_id=group_id, user_id=sender, user_name=user_name, text="",
                raw_text=raw_text, msg_id=msg_id, ts=time.time(), umo=umo, passthrough=True,
            )
        if not text and not forward_text and self._has_passthrough_component(parts):
            return ChatEvent(
                group_id=group_id, user_id=sender, user_name=user_name, text="",
                raw_text=raw_text, msg_id=msg_id, ts=time.time(), umo=umo, passthrough=True,
            )

        faces: list[FaceRef] = []
        face_tags: list[str] = []
        mface_tags: list[str] = []
        quote_extra_tags: list[str] = []
        img_jobs: list[tuple[Any, Optional[dict[str, Any]], bool]] = []
        reply_parts: list[Any] = []
        raw_metas = raw_image_metas(raw_msg)
        face_texts = raw_face_texts(raw_msg)
        special_tags = raw_special_tags(raw_msg)
        img_idx = 0

        # 优先收集当前消息自身的表情、图片与商城大表情（防止引用图抢占主消息图片配额）
        for part in parts:
            if isinstance(part, face_cls):
                fid = str(getattr(part, "id", "") or "").strip()
                fname = (
                    resolve_face_label(fid, face_texts.get(fid, ""))
                    or resolve_face_label(fid, getattr(part, "name", ""))
                )
                if fid:
                    faces.append(FaceRef(face_id=fid, label=fname))
                tag_lbl = fname or (("ID" + fid) if fid else "")
                if tag_lbl and len(face_tags) < 3:
                    face_tags.append("[QQ表情:{}]".format(tag_lbl))
            elif isinstance(part, img_cls):
                meta = match_raw_image_meta(part, raw_metas, img_idx)
                img_idx += 1
                if len(img_jobs) < 2:
                    img_jobs.append((part, meta, True))
            elif isinstance(part, reply_cls):
                reply_parts.append(part)

        if isinstance(raw_msg, dict) and isinstance(raw_msg.get("message"), list):
            for seg in raw_msg["message"]:
                if isinstance(seg, dict) and seg.get("type") == "mface":
                    mdata = seg.get("data") or {}
                    msum = str(mdata.get("summary") or "").strip().strip("[]")
                    murl = str(mdata.get("url") or "").strip()
                    if len(mface_tags) < 2:
                        mface_tags.append("[商城表情:{}]".format(msum or "表情"))
                    if murl and len(img_jobs) < 2:
                        img_jobs.append((
                            types.SimpleNamespace(url=murl, file="", path="", base64="", subType="1", summary=msum),
                            {"sub_type": "1", "summary": msum or "表情"},
                            True,
                        ))

        if mentioned:
            for part in reply_parts:
                found_chain_img = False
                for qseg in getattr(part, "chain", None) or ():
                    if isinstance(qseg, img_cls):
                        found_chain_img = True
                        if len(img_jobs) < 2:
                            # 引用图片供看图，绝不收入表情库
                            img_jobs.append((qseg, None, False))
                reply_id = str(getattr(part, "id", "") or "").strip()
                if not found_chain_img and len(img_jobs) < 2 and reply_id and bot and hasattr(bot, "call_action"):
                    try:
                        qid_arg: Any = int(reply_id) if reply_id.isdigit() else reply_id
                        raw_replied = await asyncio.wait_for(bot.call_action("get_msg", message_id=qid_arg), timeout=4.0)
                    except Exception:
                        raw_replied = None
                    raw_segs: Any = None
                    if isinstance(raw_replied, dict):
                        raw_segs = raw_replied.get("message")
                        if not isinstance(raw_segs, list) and isinstance(raw_replied.get("data"), dict):
                            raw_segs = raw_replied["data"].get("message")
                    if isinstance(raw_segs, list):
                        for seg in raw_segs:
                            if not isinstance(seg, dict):
                                continue
                            stype = str(seg.get("type") or "").strip().lower()
                            sdata = seg.get("data") if isinstance(seg.get("data"), dict) else {}
                            if stype == "mface":
                                msum = str(sdata.get("summary") or "").strip().strip("[]")
                                murl = str(sdata.get("url") or "").strip()
                                mtag = "[商城表情:{}]".format(msum or "表情")
                                if mtag not in quote_extra_tags:
                                    quote_extra_tags.append(mtag)
                                if murl and len(img_jobs) < 2:
                                    img_jobs.append((
                                        types.SimpleNamespace(url=murl, file="", path="", base64="", subType="1", summary=msum),
                                        {"sub_type": "1", "summary": msum or "表情"},
                                        False,
                                    ))
                            elif stype == "image":
                                iurl = str(sdata.get("url") or "").strip()
                                ifile = str(sdata.get("file") or "").strip()
                                if (iurl or ifile) and len(img_jobs) < 2:
                                    img_jobs.append((
                                        types.SimpleNamespace(url=iurl, file=ifile, path="", base64=""),
                                        sdata,
                                        False,
                                    ))
                            elif stype == "forward":
                                fwd_q = unpack_forward(seg)
                                if not fwd_q:
                                    fwd_id = sdata.get("id")
                                    if fwd_id and bot and hasattr(bot, "call_action"):
                                        try:
                                            raw_inner_fwd = await asyncio.wait_for(
                                                bot.call_action("get_forward_msg", id=fwd_id), timeout=4.0
                                            )
                                            fwd_q = unpack_forward(raw_inner_fwd)
                                        except Exception:
                                            fwd_q = ""
                                if fwd_q and fwd_q not in quote_extra_tags:
                                    quote_extra_tags.append(fwd_q)

        media_refs: list[MediaRef] = []
        if img_jobs:
            raw_bytes_list = await asyncio.gather(
                *(self._download_part_bytes(p) for p, _, _ in img_jobs),
                return_exceptions=True,
            )
            for (part_obj, meta, allow_emoji), raw_data in zip(img_jobs, raw_bytes_list):
                if isinstance(raw_data, (bytes, bytearray)) and raw_data:
                    b = bytes(raw_data)
                    digest = hashlib.sha256(b).hexdigest()
                    sticker = bool(allow_emoji and is_sticker_image(part_obj, b, raw_meta=meta))
                    summary = str((meta or {}).get("summary") or getattr(part_obj, "summary", "") or "")
                    media_refs.append(MediaRef(data=b, sha256=digest, is_sticker=sticker, summary=summary))

        has_image = bool(media_refs) or any(isinstance(p, img_cls) for p in parts) or bool(mface_tags)
        has_sticker = any(m.is_sticker for m in media_refs)
        media_prefix = " ".join(face_tags + mface_tags + special_tags).strip()
        bare_face = False

        if not text and not forward_text:
            if not mentioned:
                if has_image or mface_tags:
                    marker = " ".join(mface_tags + (["[图片]"] if (media_refs or any(isinstance(p, img_cls) for p in parts)) else [])).strip()
                    text = marker
                    bare_face = True  # 只落库与附带表情/图片，不触发发言
                elif (face_tags or special_tags) and time.time() - last_bot_reply_at <= 120.0:
                    text = " ".join(face_tags + special_tags).strip()
                    bare_face = True
                elif faces or special_tags:
                    bare_face = True
            elif media_prefix:
                text = (media_prefix + (" [图片]" if any(isinstance(p, img_cls) for p in parts) else "")).strip()
            elif has_image:
                text = "[图片]"
            else:
                text = "（只 @ 了你一下，没说话）"
        elif not text.startswith(("/", "／")):
            if media_prefix:
                text = "{} {}".format(media_prefix, text).strip()
            if has_image and "[图片]" not in text:
                text = "{} [图片]".format(text).strip()

        final_text = (text + (" " + forward_text if forward_text else "")).strip()
        quote_text, quote_sender, quote_msg_id = self._extract_quote(parts)
        if quote_extra_tags:
            extra_q = " ".join(t for t in quote_extra_tags if t and t not in quote_text).strip()
            if extra_q:
                if not quote_text or (quote_sender and quote_text == "回复 {}".format(quote_sender)):
                    quote_text = format_speaker_message(quote_sender, extra_q) if quote_sender else extra_q
                else:
                    quote_text = "{} {}".format(quote_text, extra_q).strip()

        return ChatEvent(
            group_id=group_id,
            user_id=sender,
            user_name=user_name,
            text=final_text,
            raw_text=raw_text,
            msg_id=msg_id,
            ts=time.time(),
            umo=umo,
            at_self=at_self,
            at_others=at_others,
            reply_to_bot=reply_to_bot,
            self_called=self_called,
            retold=retold,
            direct_question=direct_question,
            mentioned=mentioned,
            quote_text=quote_text,
            quote_sender=quote_sender,
            quote_msg_id=quote_msg_id,
            images=tuple(media_refs),
            faces=tuple(faces),
            has_image=has_image,
            has_sticker=has_sticker,
            bare_face=bare_face,
            passthrough=False,
        )


_INVALID_FACE_ERROR_MARKERS = ("1200", "unsupported", "不支持", "invalid", "face")


class AstrBotSender:
    """统一出口发送器：限流 + 智能引用 + 表情/文件目录牢笼 + QQ 消息贴表情。"""

    def __init__(
        self,
        context: Any,
        comp_types: dict[str, Any],
        chain_cls: Any,
        emoji_root: Path | str = "",
        outbox_root: Path | str = "",
        limiter: Optional[MessageLimiter] = None,
        bot_provider: Optional[Callable[..., Any]] = None,
        repo: Any = None,
        logger: Any = None,
        on_invalid_face: Optional[Callable[[str, str], Any]] = None,
    ):
        self._ctx = context
        self._comp = comp_types
        self._Chain = chain_cls
        self._emoji_root = Path(emoji_root) if emoji_root else None
        self._outbox_root = Path(outbox_root) if outbox_root else None
        self._limiter = limiter or MessageLimiter()
        self._bot_provider = bot_provider
        self._repo = repo
        self._logger = logger
        self._on_invalid_face = on_invalid_face

    @staticmethod
    def _action_succeeded(res: Any) -> bool:
        if isinstance(res, dict):
            if str(res.get("status") or "").strip().lower() == "failed":
                return False
            retcode = res.get("retcode")
            if retcode is not None and retcode != 0:
                return False
        return True

    async def send(self, action: OutboundAction) -> bool:
        if not action.umo:
            return False
        if isinstance(action, SendText):
            return await self._send_text(action)
        if isinstance(action, SendFace):
            return await self._send_face(action)
        if isinstance(action, SendEmoji):
            return await self._send_emoji(action)
        if isinstance(action, SendFile):
            return await self._send_file(action)
        if isinstance(action, ReactEmoji):
            return await self._react(action)
        return False

    async def _send_text(self, action: SendText) -> bool:
        single = clean_display_text(action.text)
        if not single or not self._limiter.try_acquire(action.group_id):
            return False
        chain = []
        if action.reply_to_msg_id and "Reply" in self._comp:
            try:
                chain.append(self._comp["Reply"](id=action.reply_to_msg_id))
            except Exception:
                pass
        chain.append(self._comp["Plain"](single))
        try:
            await self._ctx.send_message(action.umo, self._Chain(chain))
        except Exception:
            if len(chain) > 1:
                await self._ctx.send_message(action.umo, self._Chain([self._comp["Plain"](single)]))
            else:
                raise
        return True

    async def _send_face(self, action: SendFace) -> bool:
        if not action.face_id or not self._limiter.try_acquire(action.group_id):
            return False
        fid = str(action.face_id).strip()
        try:
            face = self._comp["Face"](id=int(fid))
        except (TypeError, ValueError):
            if callable(self._on_invalid_face):
                self._on_invalid_face(action.group_id, fid)
            return False
        try:
            await self._ctx.send_message(action.umo, self._Chain([face]))
        except Exception as exc:
            err_low = str(exc or "").lower()
            is_invalid = any(m in err_low for m in _INVALID_FACE_ERROR_MARKERS)
            if self._logger:
                self._logger.warning("QQ表情发送失败（face_id=%s，invalid=%s）：%s", fid, is_invalid, exc)
            if is_invalid and callable(self._on_invalid_face):
                try:
                    self._on_invalid_face(action.group_id, fid)
                except Exception:
                    pass
            return False
        return True

    def _get_bot(self, group_id: str) -> Any:
        if not callable(self._bot_provider):
            return None
        try:
            return self._bot_provider(group_id)
        except TypeError:
            return self._bot_provider()

    @staticmethod
    def _extract_raw_group_id(group_id: str) -> Optional[int]:
        raw = str(group_id or "").rsplit(":", 1)[-1].strip()
        return int(raw) if raw.isdigit() else None

    async def _send_emoji(self, action: SendEmoji) -> bool:
        if not action.path or not self._emoji_root or not self._limiter.try_acquire(action.group_id):
            return False
        try:
            root = self._emoji_root.resolve(strict=False)
            raw = Path(action.path)
            if raw.is_symlink():
                return False
            target = raw.resolve(strict=True)
        except OSError:
            return False
        if target == root or root not in target.parents or not target.is_file():
            return False
        bot = self._get_bot(action.group_id)
        raw_gid = self._extract_raw_group_id(action.group_id)
        if bot is not None and hasattr(bot, "call_action") and raw_gid is not None:
            try:
                img_bytes = await asyncio.to_thread(target.read_bytes)
                b64 = base64.b64encode(img_bytes).decode("ascii")
                res = await asyncio.wait_for(
                    bot.call_action(
                        "send_group_msg",
                        group_id=raw_gid,
                        message=[{
                            "type": "image",
                            "data": {
                                "file": "base64://" + b64,
                                "sub_type": 1,
                                "summary": action.summary or "[动画表情]",
                            },
                        }],
                    ),
                    timeout=10.0,
                )
                if not self._action_succeeded(res):
                    raise RuntimeError("send_group_msg failed: {}".format(res))
                return True
            except asyncio.TimeoutError as exc:
                if self._logger:
                    self._logger.warning("sub_type=1 表情发送超时（避免重复双发）：%s", exc)
                return False
            except Exception as exc:
                if self._logger:
                    self._logger.warning("sub_type=1 表情发送失败，回退普通图片通道：%s", exc)
        await self._ctx.send_message(action.umo, self._Chain([self._comp["Image"](file=target.as_uri())]))
        return True

    async def _send_file(self, action: SendFile) -> bool:
        target = Path(action.path) if action.path else None
        display_name = action.name or (target.name if target else "")
        out_root = str(self._outbox_root) if self._outbox_root else ""
        try:
            if not target or not self._outbox_root or target.is_symlink() or not target.is_file():
                return False
            resolved = target.resolve(strict=True)
            root_resolved = self._outbox_root.resolve(strict=False)
            if resolved == root_resolved or root_resolved not in resolved.parents:
                return False
            await self._ctx.send_message(
                action.umo,
                self._Chain([self._comp["File"](name=display_name, file=str(resolved))]),
            )
            if self._repo is not None and action.group_id and display_name:
                try:
                    await asyncio.to_thread(
                        self._repo.add_message,
                        action.group_id,
                        "bot",
                        "大肥鱼",
                        "bot",
                        "[已发送群文件：{}]".format(display_name),
                    )
                except Exception as log_exc:
                    if self._logger:
                        self._logger.warning("群文件发送后落库失败（%s）：%s", display_name, log_exc)
            return True
        except Exception as exc:
            if self._logger:
                self._logger.error("文件发送失败（%s）：%s", display_name or action.path, exc)
            try:
                await self._ctx.send_message(
                    action.umo,
                    self._Chain([self._comp["Plain"]("文件「{}」没发出去，稍后再试试。".format(display_name or "附件"))]),
                )
            except Exception:
                pass
            return False
        finally:
            if action.path and out_root:
                cleanup_file_entry(action.path, out_root)

    async def _react(self, action: ReactEmoji) -> bool:
        if not action.msg_id:
            return False
        bot = self._get_bot(action.group_id)
        if not bot or not hasattr(bot, "call_action"):
            return False
        try:
            res = await asyncio.wait_for(
                bot.call_action(
                    "set_msg_emoji_like",
                    message_id=int(action.msg_id) if str(action.msg_id).isdigit() else action.msg_id,
                    emoji_id=str(action.emoji_id or "124"),
                ),
                timeout=5.0,
            )
            return self._action_succeeded(res)
        except Exception:
            return False

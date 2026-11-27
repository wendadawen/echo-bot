"""AstrBot + NapCat 入站解析适配器与对外重导出门面（拆分子模块至 qq_protocol / sender）。"""

from __future__ import annotations

import asyncio
import hashlib
import re
import time
import types
import urllib.parse
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from ..domain import ChatEvent, FaceRef, MediaRef, format_speaker_message, resolve_face_label
from .qq_protocol import (
    MAX_DOWNLOAD_BYTES,
    MAX_FORWARD_CHARS,
    MAX_FORWARD_ITEMS,
    QUESTION_TAIL,
    QUESTION_WORDS,
    RETOLD_MARKERS,
    _SafeRedirectHandler,
    _download_public_bytes,
    _get_attr_or_key,
    detect_mentions,
    is_public_http_url,
    is_question,
    is_retold,
    is_sticker_image,
    looks_like_forward,
    looks_like_image,
    match_raw_image_meta,
    raw_face_texts,
    raw_image_metas,
    raw_special_tags,
    unpack_forward,
)
from .sender import _INVALID_FACE_ERROR_MARKERS, AstrBotSender, MessageLimiter

__all__ = [
    "AstrBotInboundParser",
    "AstrBotSender",
    "MAX_DOWNLOAD_BYTES",
    "MessageLimiter",
    "detect_mentions",
    "is_public_http_url",
    "is_question",
    "is_retold",
    "is_sticker_image",
    "looks_like_forward",
    "looks_like_image",
    "unpack_forward",
]


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

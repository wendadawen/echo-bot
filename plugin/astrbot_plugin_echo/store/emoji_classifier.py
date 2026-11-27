"""表情包三层识别与 fail-closed 准入门禁（物理尺寸 + VLM 结构化分类 + 内容/标签净化）。"""

from __future__ import annotations

import json
import re
from typing import Any, Optional

from ..domain import LLMClient, detect_image_dimensions

STICKER_MAX_DIM = 1280
STICKER_MAX_ASPECT = 1.85

ANALYZE_PROMPT = (
    "先严格分类这张图是不是适合日常群聊的真正表情包或搞笑梗图，仅输出一行 JSON，不要任何解释或 Markdown。\n"
    '输出格式：{"kind":"sticker|chat|screenshot|news|photo|chart|nsfw|other","usable":true,"tag":"2到4字情绪词","desc":"一句话画面描述"}\n'
    "严格标准：\n"
    "1. 只有熊猫头、萌宠/动漫/卡通配文梗图、搞笑动图，才允许填 kind=\"sticker\" 且 usable=true；\n"
    "2. 只要是手机或电脑截屏、微信/QQ聊天记录截图、带真人头像/群聊昵称/私人隐私文字的截图、新闻报道页面、网页、表格/PPT/图表、代码/终端日志、普通生活照/自拍、二维码，或低俗/竖中指/露骨图片，"
    '一律填对应的非 sticker 类别，usable 必须为 false，tag 填 "非表情"。'
)
FILTER_WORDS = ("二维码", "付款码", "身份证", "验证码", "密码", "银行卡号")
NON_EMOJI_WORDS = ("非表情", "不是表情")
_BAD_EMOJI_WORDS = ("低俗", "色情", "黄图", "露骨", "性暗示", "nsfw", "porn", "竖起中指", "竖中指")
_NON_STICKER_DESC_WORDS = (
    "新闻报道", "审查调查", "违纪", "聊天记录", "聊天截图", "录屏", "截图", "网页",
    "柱状图", "折线图", "统计图", "表格", "数据图表", "一张图表", "对阵表", "终端",
    "thinking process", "thought process", "思考过程", "token每秒",
    "need chinese", "en?", "must describe",
)
_BAD_TAG_PREFIXES = ("这是", "这张", "那个", "一个", "口语化", "截图", "聊天", "网页", "图片", "画面")


def is_sticker_geometry(image_bytes: Optional[bytes]) -> bool:
    """物理几何门禁：拦截高分辨率截屏与细长手机截图（如 1179x2556），极小测试桩放行。"""
    if not image_bytes:
        return True
    w, h = detect_image_dimensions(image_bytes)
    if w <= 0 or h <= 0:
        return True
    long_side = max(w, h)
    short_side = min(w, h)
    if long_side > STICKER_MAX_DIM:
        return False
    if long_side / float(short_side) > STICKER_MAX_ASPECT:
        return False
    return True


def _clean_tag(raw: Any) -> str:
    return re.sub(r"[*`\[\]【】()（）「」\"'：:]+", "", str(raw or "")).strip()


def _is_valid_tag(tag: Any) -> bool:
    raw = str(tag or "").strip()
    if not raw or not (1 <= len(raw) <= 6):
        return False
    if re.search(r"[\s:：()（）\[\]【】*`]", raw):
        return False
    if raw in NON_EMOJI_WORDS or raw.endswith(("的", "了", "着")):
        return False
    low = raw.lower()
    if any(w in low for w in _BAD_EMOJI_WORDS):
        return False
    if any(raw.startswith(p) for p in _BAD_TAG_PREFIXES):
        return False
    return True


def acceptable(description: str, image_bytes: Optional[bytes] = None) -> bool:
    """fail-closed 内容与几何过滤：空描述、非表情、截屏描述、低俗、几何不符或超 2MB 一律拒收。"""
    text = str(description or "").strip()
    if not text:
        return False
    low = text.lower()
    if any(w in text for w in NON_EMOJI_WORDS):
        return False
    if any(w in low for w in _BAD_EMOJI_WORDS):
        return False
    if any(w in low for w in _NON_STICKER_DESC_WORDS):
        return False
    if any(w in text for w in FILTER_WORDS):
        return False
    if image_bytes:
        if len(image_bytes) > 2 * 1024 * 1024 or not is_sticker_geometry(image_bytes):
            return False
    return True


def analyze_emoji(llm: LLMClient, image_bytes: bytes) -> tuple[str, str]:
    """三层表情识别门禁：先验物理尺寸 -> 再经 VLM 结构化分类（JSON 优先，兼容单行 标签|描述）。"""
    if not image_bytes:
        return "", "随手"
    if not is_sticker_geometry(image_bytes):
        return "非表情", "非表情"
    try:
        raw = str(llm.describe_image(image_bytes, ANALYZE_PROMPT) or "").strip()
    except Exception:
        return "", "随手"
    if not raw:
        return "", "随手"

    cleaned_raw = re.sub(r"```(?:json)?", "", raw, flags=re.I).strip()
    m = re.search(r"\{.*\}", cleaned_raw, re.S)
    if m:
        try:
            obj = json.loads(m.group(0))
        except (ValueError, TypeError):
            return "非表情", "非表情"
        if not isinstance(obj, dict):
            return "非表情", "非表情"
        kind = str(obj.get("kind") or "").strip().lower()
        usable = obj.get("usable")
        label = _clean_tag(obj.get("tag"))
        desc = str(obj.get("desc") or obj.get("description") or "").strip()
        if kind != "sticker" or usable is not True or not _is_valid_tag(label) or not acceptable(desc):
            return "非表情", "非表情"
        return desc, label

    low = raw.lower()
    if raw.startswith(NON_EMOJI_WORDS) or any(w in raw for w in NON_EMOJI_WORDS) or any(w in low for w in _BAD_EMOJI_WORDS):
        return "非表情", "非表情"
    first_line = next((ln.strip() for ln in raw.splitlines() if ln.strip()), "")
    if "|" not in first_line and "｜" not in first_line:
        return "非表情", "非表情"
    left, right = re.split(r"[|｜]", first_line, maxsplit=1)
    label = _clean_tag(left)
    desc = str(right or "").strip()
    if not _is_valid_tag(label) or not acceptable(desc):
        return "非表情", "非表情"
    return desc, label

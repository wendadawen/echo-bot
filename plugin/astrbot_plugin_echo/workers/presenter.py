"""后台任务交付呈现器：代码/散文拆分、外发文件牢笼校验、敏感词脱敏与自然口语转述。"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Optional

from ..domain import (
    LLMClient,
    LLMMessage,
    MemoryRepository,
    PendingDelivery,
    clean_display_text,
    clip_sentence,
    clip_text,
)
from ..engine.safety import (
    is_boilerplate,
    is_failed_lookup,
    is_memorable_takeaway,
    prompt_requests_file,
    sanitize_meta_reply,
    scrub_internal_leaks,
)
from .sandbox import cleanup_file_entry

_CLOSED_CODE_BLOCK = re.compile(r"```(?:[a-zA-Z0-9_+-]+[ \t]*\n|\n)?(.*?)```", re.S)
_OPEN_CODE_BLOCK = re.compile(r"```(?:[a-zA-Z0-9_+-]+[ \t]*\n|\n)?(.*)\Z", re.S)

_PRESENT_PROMPT = (
    "你现在以群友「大肥鱼」的口吻在 QQ 群里汇报刚才后台工具查到或做成的结果。"
    "不要输出 JSON，不要照抄内部汇报语气，也不要出现“群里可以这么回”“回复建议”等草稿话术。\n"
    "要求：1-3 句简短自然口语，忠于事实与局限；不暴露后台工具名、命令行或本地路径；"
    "不要翻前面闲聊或反复道歉。"
)
_PRESENT_CODE_PROMPT = (
    "你现在以「大肥鱼」的身份在 QQ 群里写 1-2 句简短自然的开场说明（后面的代码块由系统原样附上）。"
    "不要输出 JSON，不要重复贴代码，不要暴露内部工具名或本地路径。"
)


def _split_code_blocks(text: str) -> tuple[str, list[str]]:
    raw = str(text or "").strip()
    blocks: list[str] = []

    def collect(m: re.Match[str]) -> str:
        body = m.group(1).strip("\r\n")
        if body:
            blocks.append(body)
        return ""

    prose = _CLOSED_CODE_BLOCK.sub(collect, raw)
    prose = _OPEN_CODE_BLOCK.sub(collect, prose)
    prose = re.sub(r"\n{3,}", "\n\n", prose).strip()
    return prose, blocks


def _format_file_size(num_bytes: int) -> str:
    size = max(0, int(num_bytes or 0))
    if size < 1024:
        return "{} B".format(size)
    if size < 1024 * 1024:
        return "{} KB".format(max(1, round(size / 1024.0)))
    return "{:.1f} MB".format(size / (1024.0 * 1024.0))


def _describe_files(files: tuple[dict[str, Any], ...] | list[dict[str, Any]]) -> str:
    parts = []
    for item in files or ():
        if not isinstance(item, dict):
            continue
        raw_name = item.get("name") or Path(str(item.get("path") or "")).name
        name = Path(str(raw_name)).name
        if not name:
            continue
        size = item.get("size")
        parts.append("{} ({})".format(name, _format_file_size(int(size))) if size else name)
    return "、".join(parts)


def filter_valid_files(
    files: tuple[dict[str, Any], ...],
    outbox_root: str = "",
) -> tuple[dict[str, Any], ...]:
    if not files:
        return ()
    root = Path(outbox_root).resolve(strict=False) if outbox_root else None
    valid = []
    for item in files:
        if not isinstance(item, dict):
            continue
        p_str = str(item.get("path") or "").strip()
        if not p_str:
            continue
        p = Path(p_str)
        try:
            if root is not None:
                resolved = p.resolve(strict=False)
                if resolved == root or root not in resolved.parents:
                    continue
            if p.is_file():
                valid.append(item)
        except OSError:
            continue
    return tuple(valid)


def rewrite_success(
    prompt: str,
    raw_result: str,
    files: tuple[dict[str, Any], ...],
    llm: Optional[LLMClient],
    secrets: tuple[str, ...],
) -> str:
    raw = str(raw_result or "").strip()
    prose, blocks = _split_code_blocks(raw) if raw else ("", [])
    safe_blocks = [b for b in (scrub_internal_leaks(b, extra_secrets=secrets) for b in blocks) if b]
    fallback = sanitize_meta_reply(prose if safe_blocks else raw, extra_secrets=secrets)
    file_summary = _describe_files(files)

    rewritten = ""
    if llm is not None:
        instruction = _PRESENT_CODE_PROMPT if safe_blocks else _PRESENT_PROMPT
        body = clip_sentence(prose if safe_blocks else raw, 600)
        if safe_blocks and not body:
            body = "（已完成代码/数据生成，请写一句简短开场白引出下文）"
        elif files and not body:
            body = "（已生成待发文件，请写一句简短提示让群友看文件）"
        parts = []
        if prompt:
            parts.append("原任务：" + clip_text(prompt, 200))
        if body:
            parts.append("后台结果：\n" + body)
        if file_summary:
            parts.append("已生成待发文件（系统会自动发送，不要复述本地路径）：" + file_summary)
        try:
            turn = llm.chat(
                [
                    LLMMessage(role="system", content=instruction),
                    LLMMessage(role="user", content="\n\n".join(parts)),
                ],
                tools=(),
            )
            candidate = sanitize_meta_reply(turn.text, extra_secrets=secrets)
            if candidate and not is_boilerplate(candidate):
                rewritten = candidate
        except Exception:
            pass

    if safe_blocks:
        intro = rewritten or (fallback if (fallback and not is_boilerplate(fallback)) else "")
        intro = clean_display_text(intro) if intro else ""
        code_part = "\n\n".join(safe_blocks)
        return (intro + "\n" + code_part).strip() if intro else code_part
    if rewritten:
        return clean_display_text(rewritten)
    if fallback and not is_boilerplate(fallback):
        return clip_sentence(clean_display_text(fallback), 800)
    if files:
        return "做好啦，文件发群里了。"
    return "查完了，不过这会儿没组织好词，你再问我一句。"


class DeliveryPresenter:
    """将后台 PendingDelivery 格式化为群聊友好结果、脱敏并写入历史与记忆。"""

    def __init__(
        self,
        *,
        outbox_root: str = "",
        timeout: int = 180,
        repo: Optional[MemoryRepository] = None,
    ):
        self.outbox_root = str(outbox_root or "")
        self.timeout = int(timeout or 180)
        self._repo = repo

    def present_delivery(
        self,
        delivery: PendingDelivery,
        llm: Optional[LLMClient],
        secrets: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        raw_files = tuple(delivery.files or ())
        files = filter_valid_files(raw_files, self.outbox_root)
        raw_prompt = str(delivery.prompt or "").strip()
        short_prompt = clip_text(raw_prompt, 20)
        ok = delivery.status == "completed" and (bool(delivery.result) or bool(files))

        if ok and files and is_failed_lookup(delivery.result) and not prompt_requests_file(raw_prompt):
            for f in files:
                cleanup_file_entry(f.get("path", ""), self.outbox_root)
            files = ()

        text = ""
        if ok:
            text = rewrite_success(raw_prompt, delivery.result, files, llm, secrets)
            if files and is_failed_lookup(text) and not prompt_requests_file(raw_prompt):
                for f in files:
                    cleanup_file_entry(f.get("path", ""), self.outbox_root)
                files = ()
        elif delivery.reason == "cancelled":
            text = (
                "好，刚才那个「{}」的后台任务我已经帮你停掉了。".format(short_prompt)
                if short_prompt else "好，刚才那个后台任务我已经帮你停掉了。"
            )
        elif delivery.reason == "timeout":
            partial = clip_text(sanitize_meta_reply(delivery.partial, extra_secrets=secrets), 220)
            prefix = (
                "刚才「{}」查了 {} 秒超时了，没来得及全整理完。".format(short_prompt, self.timeout)
                if short_prompt
                else "刚才这个任务查了 {} 秒超时了，没来得及全整理完。".format(self.timeout)
            )
            text = (
                "{}目前查到一半的进展：{}\n你可以把范围缩窄一点再让我查。".format(prefix, partial)
                if partial
                else prefix + "你可以把范围缩窄一点再让我查。"
            )
        else:
            text = (
                "刚才「{}」没弄成，等下再试。".format(short_prompt)
                if short_prompt else "刚才那个任务没弄成，等下再试。"
            )

        text = scrub_internal_leaks(text, extra_secrets=secrets) or (
            "刚才「{}」没弄成，等下再试。".format(short_prompt) if short_prompt else "没弄成，等下再试。"
        )
        if not ok:
            for item in raw_files:
                cleanup_file_entry(item.get("path", ""), self.outbox_root)
            files = ()
        else:
            kept = {f.get("path") for f in files}
            for item in raw_files:
                if item.get("path") not in kept:
                    cleanup_file_entry(item.get("path", ""), self.outbox_root)

        if self._repo is not None:
            self._repo.add_message(
                delivery.group_id, "bot", "大肥鱼", "bot", text, meta="tools=run_task" if ok else ""
            )
            if ok and short_prompt and not is_failed_lookup(delivery.result) and is_memorable_takeaway(raw_prompt, text):
                takeaway = clip_text(re.sub(r"\s+", " ", text), 80)
                if takeaway:
                    self._repo.add_memory(
                        delivery.group_id, "event", "完成「{}」：{}".format(short_prompt, takeaway)[:120]
                    )

        return {
            "task_id": delivery.task_id,
            "group_id": delivery.group_id,
            "umo": delivery.umo,
            "text": text,
            "files": list(files),
            "ok": ok,
            "error": delivery.error,
        }

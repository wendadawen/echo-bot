"""核心对话与工具编排引擎：原生 Tool Calling、强门禁、多模态直传、智能引用与异步记忆归纳。

零 AstrBot /零 SQLite /零 HTTP /零子进程依赖，只依赖 domain 协议。
"""

from __future__ import annotations

import re
import time
from typing import Any, Callable, Optional

from ..domain import (
    Batch,
    EmojiStorePort,
    ImagePart,
    LLMClient,
    LLMMessage,
    MemoryRepository,
    OutboundAction,
    ReactEmoji,
    SearchProvider,
    SendEmoji,
    SendFace,
    SendText,
    TaskRequest,
    TaskRunner,
    ToolSpec,
    clean_display_text,
)
from .context import ContextAssembler, estimate_tokens, should_quote_reply
from .memory import MemoryConsolidator, SUMMARY_INTERVAL
from .safety import (
    is_boilerplate,
    is_transient_memory_noise,
    is_unsafe,
    is_unsupported_capability,
    scrub_internal_leaks,
)
from .scheduler import Scheduler

MAX_TOOL_ROUNDS = 4
REPEAT_WINDOW = 30.0
ATTACH_EMOJI_COOLDOWN = 45.0
HELP_TEXT = "群聊里直接 @大肥鱼 即可对话、看图、写代码/生成文件；也可以随时问我后台任务进度或让我停掉它。"

_OFFER_HINTS = (
    "你要的话", "要不要", "需要的话", "发我", "告诉我",
    "帮你查", "帮你排", "想吃什么", "哪个", "哪种", "什么方向",
)
_EXPLICIT_EMOJI_HINTS = ("表情包", "斗图", "发个表情", "发表情", "再来一张", "换一张", "发张表情")
_VAGUE_TASK_PROMPTS = frozenset({
    "查一下", "去查一下", "查一查", "帮我查", "帮我查一下",
    "搜一下", "去搜一下", "搜一搜", "看看", "去看看", "查查看", "查",
})


def _is_open_offer(text: str) -> bool:
    s = str(text or "").strip()
    return bool(s) and (s.endswith(("?", "？")) or any(h in s for h in _OFFER_HINTS))


def _repeat_key(group_id: str, user_id: str, text: str) -> tuple[str, str, str]:
    clean = re.sub(r"[\s，,。.！!？?～~、…]+", "", str(text or ""))
    return (str(group_id), str(user_id), clean)


_OVERLOAD_ERROR_KEYWORDS = (
    "529",
    "429",
    "503",
    "overloaded",
    "pool_overloaded",
    "rate_limit",
    "rate limit",
    "too many requests",
    "capacity",
    "号池负载",
)
_NETWORK_ERROR_KEYWORDS = (
    "timeout",
    "timed out",
    "urlopen",
    "connection",
    "remote end closed",
    "reset by peer",
    "502",
    "504",
)


def _format_model_error_notice(exc: Exception) -> str:
    """将上游模型异常映射为简短自然的群聊提示，不泄露状态码或内部错误原文。"""
    msg = str(exc or "").lower()
    if any(k in msg for k in _OVERLOAD_ERROR_KEYWORDS):
        return "刚才接口这边有点挤，等几秒再喊我试试。"
    if any(k in msg for k in _NETWORK_ERROR_KEYWORDS):
        return "刚才网络卡了一下没连上，稍后再喊我试试。"
    return "刚才我这边卡壳了一下，稍后再喊我试试。"


def _is_vague_task_prompt(task_prompt: str) -> bool:
    clean = re.sub(r"[\s，,。.！!？?～~、…]+", "", str(task_prompt or ""))
    return not clean or clean in _VAGUE_TASK_PROMPTS or len(clean) < 6


class EchoAgent:
    """核心决策引擎：收 Batch -> 调工具/生成回复 -> 产出 OutboundAction 列表。"""

    def __init__(
        self,
        repo: MemoryRepository,
        llm: LLMClient,
        scheduler: Scheduler,
        *,
        emoji_store: Optional[EmojiStorePort] = None,
        task_runner: Optional[TaskRunner] = None,
        search_provider: Optional[SearchProvider] = None,
        history_limit: int = 30,
        persona: Optional[str] = None,
    ):
        self._repo = repo
        self._llm = llm
        self._scheduler = scheduler
        self._emoji = emoji_store
        self._tasks = task_runner
        self._search = search_provider
        self.context = ContextAssembler(
            repo,
            emoji_store=emoji_store,
            history_limit=history_limit,
            **({"persona": persona} if persona else {}),
        )
        self.memory = MemoryConsolidator(repo, llm)
        self._repeat_seen: dict[tuple[str, str, str], float] = {}
        self._last_bot_reply: dict[str, tuple[tuple[Any, ...], float]] = {}
        self._last_emoji_at: dict[str, float] = {}

    def _secrets(self) -> tuple[str, ...]:
        out = list(self._llm.extra_secrets() or ())
        return tuple(s for s in out if len(str(s)) >= 8)

    def _persist_batch_items(self, batch: Batch) -> None:
        if batch.persisted:
            return
        for item in batch.items[-8:]:
            self._repo.observe_person(batch.group_id, item.user_id, item.user_name, now=item.ts or None)
        for item in batch.items:
            t = str(item.text or "").strip()
            if not t:
                continue
            self._repo.add_message(
                batch.group_id,
                item.user_id,
                item.user_name,
                "user",
                t,
                ts=item.ts or None,
                msg_id=item.msg_id,
            )
        batch.persisted = True

    def observe_skipped(self, batch: Batch) -> None:
        """未通过抽签、退避或忙时跳过的批：依然完整入历史与人物档案，不丢消息。"""
        self._persist_batch_items(batch)

    def _build_tools(
        self,
        batch: Batch,
        *,
        allow_task: bool,
        allow_silent: bool,
        has_emoji: bool,
        has_search: bool,
        has_running_task: bool,
        can_react: bool,
    ) -> tuple[list[ToolSpec], dict[str, Callable[[dict[str, Any]], Any]]]:
        specs: list[ToolSpec] = [
            ToolSpec(
                name="recall_memory",
                description="从本群长期记忆和情节里检索更早的约定、事实或群黑话。",
                parameters={
                    "type": "object",
                    "properties": {"query": {"type": "string", "description": "要查找的关键词或问题"}},
                    "required": ["query"],
                },
            ),
            ToolSpec(
                name="query_person",
                description="查看某位群友的个人档案与历史印象（支持群内常用昵称，自动跨改名识别）。",
                parameters={
                    "type": "object",
                    "properties": {"name": {"type": "string", "description": "群友名字或 QQ 号"}},
                    "required": ["name"],
                },
            ),
            ToolSpec(
                name="save_memory",
                description="记住一条值得长期保留的事实或群内黑话（黑话格式：词=解释）。",
                parameters={
                    "type": "object",
                    "properties": {
                        "content": {"type": "string", "description": "简洁的一句话事实（<=60字）"},
                        "category": {"type": "string", "enum": ["event", "preference", "jargon"], "default": "event"},
                    },
                    "required": ["content"],
                },
            ),
        ]

        if has_search:
            specs.append(ToolSpec(
                name="web_search",
                description="快速联网搜索实时事实、新闻或短问短答（无需启动重型后台任务）。",
                parameters={
                    "type": "object",
                    "properties": {"query": {"type": "string", "description": "简洁搜索关键词"}},
                    "required": ["query"],
                },
            ))

        if has_emoji:
            raw_moods = self._emoji.tags(batch.group_id, limit=10) if self._emoji is not None else ()
            moods = [str(m).strip() for m in raw_moods if str(m).strip()] if isinstance(raw_moods, (list, tuple)) else []
            mood_desc = (
                "2到4字情绪词（本群可用：{}；也可填常见心情词）".format("、".join(moods))
                if moods
                else "2到4字情绪或梗图描述，例如 无语、开心、呆萌"
            )
            specs.append(ToolSpec(
                name="send_emoji",
                description="在本群发送一张匹配当前心情的表情包；闲聊、吐槽、惊讶、无语、开心或斗图时顺手配图很有氛围。文字配图时传 reply_text，纯丢图只填 mood。",
                parameters={
                    "type": "object",
                    "properties": {
                        "mood": {"type": "string", "description": mood_desc},
                        "reply_text": {"type": "string", "description": "可选：同时发送的群聊文字回复；留空则纯发表情（若本轮已写正文会自动合并发送）"},
                    },
                    "required": ["mood"],
                },
            ))

        if can_react:
            specs.append(ToolSpec(
                name="react_message",
                description="对指定群消息点一个 QQ 贴表情回应而不刷屏说话（适合未 @ 时轻轻回应）。",
                parameters={
                    "type": "object",
                    "properties": {
                        "msg_id": {"type": "string", "description": "消息短编号（如 m3），默认回应最后一条"},
                        "emoji_id": {"type": "string", "description": "QQ表情编号（76=赞, 124=OK, 182=笑哭, 179=doge），默认 76"},
                    },
                },
            ))

        if allow_task:
            if not has_running_task:
                specs.append(ToolSpec(
                    name="run_task",
                    description="启动后台执行器完成需要联网深度调研、写代码、分析表格/图片或生成并发送本地文件（HTML/Markdown/CSV/文档）的重任务。",
                    parameters={
                        "type": "object",
                        "properties": {
                            "task": {
                                "type": "string",
                                "description": "必须结合上下文写成独立完整的任务目标（含具体对象/关键词，禁止仅写'去查一下'等模糊词）",
                            },
                            "ack_text": {"type": "string", "description": "可选：启动前先对群友说的简短口语（<=40字）"},
                        },
                        "required": ["task"],
                    },
                ))
            else:
                specs.append(ToolSpec(
                    name="task_status",
                    description="查询当前本群正在运行的后台任务进度与已耗时。",
                    parameters={"type": "object", "properties": {}},
                ))
                specs.append(ToolSpec(
                    name="task_stop",
                    description="当群友要求取消、算了或停止当前后台任务时，立即终止后台进程。",
                    parameters={"type": "object", "properties": {}},
                ))

        if allow_silent:
            specs.append(ToolSpec(
                name="stay_silent",
                description="没人找你或只是群友互相闲聊时保持沉默，不插话。",
                parameters={"type": "object", "properties": {}},
            ))

        return specs, {}

    def _dispatch_background(
        self,
        batch: Batch,
        prompt: str,
        pre_history: list[dict[str, Any]],
        ack_text: str = "",
    ) -> list[OutboundAction]:
        gid = batch.group_id
        umo = batch.umo
        if is_unsafe(prompt):
            return [SendText(gid, umo, "这个不干。")]
        if is_unsupported_capability(prompt):
            return [SendText(gid, umo, "这方面我这边暂时没接画图/语音接口，做不到呢。")]
        if self._tasks is None or not self._tasks.available():
            return []
        non_sticker_imgs = tuple(m.data for m in batch.images if m.data)[:2]
        req = TaskRequest(
            group_id=gid,
            umo=umo,
            prompt=prompt,
            images=non_sticker_imgs,
            pre_history=tuple(pre_history[-6:]),
        )
        clean_ack = clean_display_text(scrub_internal_leaks(ack_text, extra_secrets=self._secrets())) if ack_text else ""
        res = self._tasks.submit(req, ack_text=clean_ack)
        if not res:
            return []
        notice = clean_display_text(str(res.get("notice") or "").strip())
        if not notice:
            return []
        anchor = batch.anchor
        self._repo.add_message(gid, "bot", "大肥鱼", "bot", notice, meta="tools=run_task")
        self._scheduler.record_bot_reply(
            gid,
            user_id=anchor.user_id if anchor else "",
            from_mentioned=bool(batch.mentioned and not batch.retold),
            now=time.time(),
        )
        return [SendText(gid, umo, notice)]

    def _resolve_quote_mid(self, gid: str, scene: str, anchor: Any) -> str:
        """群聊思考或报错期间若已有插话，自动引用触发该轮的原始消息 ID。"""
        if scene == "group" and anchor and anchor.msg_id:
            latest = self._repo.recent_messages(gid, limit=12)
            return should_quote_reply(anchor.msg_id, anchor.user_id, latest)
        return ""

    def handle_batch(self, batch: Batch) -> dict[str, Any]:
        """核心单批决策入口，返回 `{replied, actions, reason, error}`。"""
        gid = batch.group_id
        umo = batch.umo
        if not batch.items:
            return {"replied": False, "actions": [], "reason": "empty"}

        anchor = batch.anchor
        raw_text = (anchor.text if anchor else "").strip()
        if not raw_text:
            return {"replied": False, "actions": [], "reason": "empty"}

        # 0) 极简帮助提示（其余未识别 / 命令在未 @ 时不瞎插话）
        if raw_text in ("/帮助", "／帮助", "/help"):
            self._persist_batch_items(batch)
            self._repo.add_message(gid, "bot", "大肥鱼", "bot", HELP_TEXT)
            self._scheduler.record_bot_reply(gid, user_id=anchor.user_id if anchor else "")
            return {"replied": True, "actions": [SendText(gid, umo, HELP_TEXT)], "reason": "help"}

        # 1) 硬安全边界（防删根目录/查看系统目录/mount 探测/无限循环/部署端口）
        if any(is_unsafe(i.text) for i in batch.items) or is_unsafe(raw_text) or is_unsafe(batch.quote_text):
            self._persist_batch_items(batch)
            if batch.mentioned:
                return {"replied": True, "actions": [SendText(gid, umo, "这个不干。")], "reason": "unsafe"}
            return {"replied": False, "actions": [], "reason": "unsafe_silent"}

        # 2) 先取历史再落本批消息（防止 history 与 current turn 重复）
        prev_history = self._repo.recent_messages(gid, limit=self.context.history_limit)
        self._persist_batch_items(batch)

        # 3) 重复文本消息 30 秒内防重（仅对显式无图相同短句防刷）
        now = time.time()
        repeat_keys: list[str] = []
        if not batch.has_image:
            repeat_keys = [
                _repeat_key(gid, i.user_id, i.text)
                for i in batch.items
                if str(i.text or "").strip()
            ]
            if repeat_keys and all(now - self._repeat_seen.get(k, 0.0) < REPEAT_WINDOW for k in repeat_keys):
                return {"replied": False, "actions": [], "reason": "repeat"}
            for k in repeat_keys:
                self._repeat_seen[k] = now
            if len(self._repeat_seen) > 500:
                cutoff = now - REPEAT_WINDOW
                self._repeat_seen = {k: v for k, v in self._repeat_seen.items() if v >= cutoff}

        # 4) 仅在正式进入模型决策时非破坏性绑定近期群图（绝不让被跳过的批消费未看图）
        self._scheduler.merge_recent_images(batch, now=now)

        running_task = self._tasks.running_for_group(gid) if self._tasks is not None else None
        allow_task = bool(
            self._tasks is not None
            and self._tasks.available()
            and ((batch.mentioned and not batch.retold) or batch.scene == "private")
        )
        allow_silent = not (batch.mentioned and not batch.retold)
        has_emoji = bool(
            batch.scene == "group"
            and self._emoji is not None
            and self._emoji.has_entries(gid)
        )
        has_search = bool(self._search is not None and self._search.available())
        can_react = bool(batch.scene == "group" and batch.target_msg_id)

        tool_specs, _ = self._build_tools(
            batch,
            allow_task=allow_task,
            allow_silent=allow_silent,
            has_emoji=has_emoji,
            has_search=has_search,
            has_running_task=running_task is not None,
            can_react=can_react,
        )

        system, compacted_history, real_to_alias, alias_to_real = self.context.build(
            batch,
            prev_history,
            running_task=running_task,
            tools_summary="",
            now=now,
        )

        llm_messages: list[LLMMessage] = [LLMMessage(role="system", content=system)]
        for row in compacted_history:
            r_role, r_content = self.context.format_history_row(row, real_to_alias, now)
            llm_messages.append(LLMMessage(role=r_role, content=r_content))

        batch_prompt = self.context.format_batch_prompt(batch, real_to_alias, now)
        inline_imgs = tuple(ImagePart(data=m.data) for m in batch.images[:2] if m.data)
        llm_messages.append(LLMMessage(role="user", content=batch_prompt, images=inline_imgs))

        started = time.monotonic()
        total_tokens = 0
        tools_used: list[str] = []
        saw_images = bool(inline_imgs)
        marked_viewed = False
        reply_text = ""
        attach_mood = ""
        terminal_actions: Optional[list[OutboundAction]] = None
        terminal_reason = "reply"

        def _record_tool(tool_name: str) -> None:
            if tool_name and tool_name not in tools_used:
                tools_used.append(tool_name)

        for _ in range(MAX_TOOL_ROUNDS):
            try:
                turn = self._llm.chat(llm_messages, tools=tool_specs)
                total_tokens += int(turn.usage_tokens or 0)
                if not marked_viewed and batch.images:
                    self._scheduler.mark_images_viewed(gid, batch.images, now=now)
                    marked_viewed = True
            except Exception as exc:
                elapsed = time.monotonic() - started
                self._repo.record_event(gid, "model_error", ok=False, latency=elapsed, detail=str(exc))
                for k in repeat_keys:
                    self._repeat_seen.pop(k, None)
                if (batch.mentioned and not batch.retold) or batch.scene == "private":
                    quote_mid = self._resolve_quote_mid(gid, batch.scene, anchor)
                    notice = _format_model_error_notice(exc)
                    return {
                        "replied": True,
                        "actions": [SendText(gid, umo, notice, reply_to_msg_id=quote_mid)],
                        "reason": "model_error",
                        "error": str(exc),
                        "text": notice,
                    }
                return {"replied": False, "actions": [], "reason": "model_error", "error": str(exc), "text": ""}

            if not turn.tool_calls:
                reply_text = str(turn.text or "").strip()
                break

            llm_messages.append(
                LLMMessage(role="assistant", content=turn.text or "", tool_calls=turn.tool_calls)
            )
            should_stop_loop = False
            for tc in turn.tool_calls:
                args = tc.arguments if isinstance(tc.arguments, dict) else {}
                name = tc.name

                if name == "stay_silent":
                    if allow_silent:
                        elapsed = time.monotonic() - started
                        self._repo.record_event(gid, "silent", ok=True, latency=elapsed, tokens=total_tokens)
                        return {"replied": False, "actions": [], "reason": "silent"}
                    llm_messages.append(LLMMessage(
                        role="tool", tool_call_id=tc.id, name=name,
                        content="有人明确点名问你，不能保持沉默，请直接回复或调用工具。",
                    ))
                    continue

                if name == "react_message":
                    if can_react:
                        raw_mid = str(args.get("msg_id") or "").strip()
                        real_mid = alias_to_real.get(raw_mid, "") or (
                            raw_mid if raw_mid in real_to_alias else batch.target_msg_id
                        )
                        emoji_id = str(args.get("emoji_id") or "76").strip() or "76"
                        if real_mid:
                            elapsed = time.monotonic() - started
                            self._repo.record_event(gid, "react", ok=True, latency=elapsed, tokens=total_tokens, detail=emoji_id)
                            terminal_actions = [ReactEmoji(gid, umo, real_mid, emoji_id)]
                            terminal_reason = "react"
                            should_stop_loop = True
                            break
                    llm_messages.append(LLMMessage(role="tool", tool_call_id=tc.id, name=name, content="没有可贴表情的目标消息。"))
                    continue

                if name == "send_emoji":
                    if not has_emoji or self._emoji is None:
                        llm_messages.append(LLMMessage(role="tool", tool_call_id=tc.id, name=name, content="本群表情库暂无可用表情，请直接用文字回复。"))
                        continue
                    mood = str(args.get("mood") or "").strip()[:30] or "随手"
                    inline_text = str(args.get("reply_text") or "").strip() or str(turn.text or "").strip()
                    if inline_text:
                        reply_text = inline_text
                        attach_mood = mood
                        should_stop_loop = True
                        break
                    picked = self._emoji.pick(gid, mood)
                    if picked is None:
                        elapsed = time.monotonic() - started
                        self._repo.record_event(gid, "silent", ok=True, latency=elapsed)
                        return {"replied": False, "actions": [], "reason": "silent"}
                    elapsed = time.monotonic() - started
                    self._repo.record_event(gid, "send_face", ok=True, latency=elapsed, tokens=total_tokens, detail=mood)
                    self._repo.add_message(gid, "bot", "大肥鱼", "bot", "[发了一张表情包]", meta="tools=send_emoji")
                    self._scheduler.record_bot_reply(
                        gid,
                        user_id=anchor.user_id if anchor else "",
                        from_mentioned=bool(batch.mentioned and not batch.retold),
                        now=time.time(),
                    )
                    if str(picked).startswith("face:"):
                        terminal_actions = [SendFace(gid, umo, str(picked).split(":", 1)[1])]
                    else:
                        terminal_actions = [SendEmoji(gid, umo, str(picked))]
                    terminal_reason = "face"
                    should_stop_loop = True
                    break

                if name == "run_task":
                    if not allow_task:
                        llm_messages.append(LLMMessage(
                            role="tool", tool_call_id=tc.id, name=name,
                            content="没有明确 @ 或点名你，禁止启动后台工具！请调用 stay_silent 或直接简要回复。",
                        ))
                        continue
                    t_prompt = str(args.get("task") or "").strip()
                    if _is_vague_task_prompt(t_prompt):
                        llm_messages.append(LLMMessage(
                            role="tool",
                            tool_call_id=tc.id,
                            name=name,
                            content="task 描述缺少具体对象。请结合上文把要查的具体主题/关键词写清楚，再调 web_search 或 run_task；若上文也没写明，直接问群友要查什么。",
                        ))
                        continue
                    ack = str(args.get("ack_text") or "").strip()
                    dispatched = self._dispatch_background(batch, t_prompt, prev_history, ack_text=ack)
                    if dispatched:
                        elapsed = time.monotonic() - started
                        self._repo.record_event(gid, "task", ok=True, latency=elapsed, tokens=total_tokens)
                        return {"replied": True, "actions": dispatched, "reason": "task"}
                    llm_messages.append(LLMMessage(role="tool", tool_call_id=tc.id, name=name, content="后台执行器当前不可用。"))
                    continue

                if name == "task_status":
                    info = self._tasks.status(gid) if self._tasks else None
                    _record_tool(name)
                    msg = (
                        "任务「{}」已运行 {:.0f} 秒，最新进展：{}".format(info.prompt, info.elapsed, info.progress)
                        if info else "当前本群没有正在运行的后台任务。"
                    )
                    llm_messages.append(LLMMessage(role="tool", tool_call_id=tc.id, name=name, content=msg))
                    continue

                if name == "task_stop":
                    if not allow_task:
                        llm_messages.append(LLMMessage(role="tool", tool_call_id=tc.id, name=name, content="未点名不可停止后台任务。"))
                        continue
                    stopped = self._tasks.stop(gid) if self._tasks else False
                    _record_tool(name)
                    msg = "已立即停止当前后台任务。" if stopped else "当前没有正在运行的后台任务。"
                    llm_messages.append(LLMMessage(role="tool", tool_call_id=tc.id, name=name, content=msg))
                    continue

                if name == "recall_memory":
                    q = str(args.get("query") or "").strip()
                    rows = self._repo.recall_memories(gid, q, limit=6)
                    _record_tool(name)
                    llm_messages.append(LLMMessage(
                        role="tool", tool_call_id=tc.id, name=name,
                        content="；".join(rows) if rows else "没查到相关长期记忆",
                    ))
                    continue

                if name == "query_person":
                    target_name = str(args.get("name") or "").strip()
                    uid = self._repo.resolve_person(gid, target_name)
                    summary = self._repo.person_summary(gid, uid) if uid else ""
                    _record_tool(name)
                    llm_messages.append(LLMMessage(
                        role="tool", tool_call_id=tc.id, name=name,
                        content=summary or "没记着这人的特别档案",
                    ))
                    continue

                if name == "save_memory":
                    content = str(args.get("content") or "").strip()
                    cat = str(args.get("category") or "event").strip()
                    if content and not is_transient_memory_noise(content) and not is_unsafe(content):
                        self._repo.add_memory(gid, cat, content)
                        _record_tool(name)
                        msg = "记下了：" + content
                    else:
                        msg = "这条临时或敏感内容不用记。"
                    llm_messages.append(LLMMessage(role="tool", tool_call_id=tc.id, name=name, content=msg))
                    continue

                if name == "web_search":
                    q = str(args.get("query") or "").strip()
                    if self._search and self._search.available() and q:
                        try:
                            hits = self._search.search(q, limit=4)
                            _record_tool(name)
                            txt = "\n".join(
                                "- {}：{} ({})".format(h.title, h.snippet, h.url) for h in hits
                            ) or "未搜索到相关结果"
                        except Exception as exc:
                            txt = "搜索失败：{}".format(exc)
                    else:
                        txt = "未配置联网搜索服务"
                    llm_messages.append(LLMMessage(role="tool", tool_call_id=tc.id, name=name, content=txt))
                    continue

                llm_messages.append(LLMMessage(role="tool", tool_call_id=tc.id, name=name, content="未知工具"))

            if should_stop_loop:
                break

        if terminal_actions is not None:
            return {"replied": True, "actions": terminal_actions, "reason": terminal_reason}

        elapsed = time.monotonic() - started
        if not reply_text:
            self._repo.record_event(gid, "silent", ok=True, latency=elapsed, tokens=total_tokens)
            return {"replied": False, "actions": [], "reason": "silent"}

        if is_boilerplate(reply_text):
            self._repo.record_event(gid, "filtered", ok=False, latency=elapsed)
            return {"replied": False, "actions": [], "reason": "filtered"}

        cleaned = clean_display_text(scrub_internal_leaks(reply_text, extra_secrets=self._secrets()))
        if not cleaned:
            self._repo.record_event(gid, "filtered", ok=False, latency=elapsed)
            return {"replied": False, "actions": [], "reason": "filtered"}

        # 防 30 秒内同句复读
        rkey = (_repeat_key(gid, "bot", cleaned), _repeat_key(gid, anchor.user_id if anchor else "", raw_text))
        last_bot = self._last_bot_reply.get(gid)
        if last_bot and last_bot[0] == rkey and time.time() - last_bot[1] < REPEAT_WINDOW:
            return {"replied": False, "actions": [], "reason": "repeat_reply"}
        self._last_bot_reply[gid] = (rkey, time.time())
        if len(self._last_bot_reply) > 200:
            self._last_bot_reply.clear()

        # 智能 QQ 引用判断（处理模型思考期间是否有人插话）
        quote_mid = self._resolve_quote_mid(gid, batch.scene, anchor)

        now_done = time.time()
        extra_actions: list[OutboundAction] = []
        explicit_emoji_req = any(h in (batch.body_text or "") for h in _EXPLICIT_EMOJI_HINTS)
        emoji_cooldown_ok = explicit_emoji_req or (now_done - self._last_emoji_at.get(gid, 0.0) >= ATTACH_EMOJI_COOLDOWN)
        if attach_mood and has_emoji and self._emoji is not None and emoji_cooldown_ok:
            picked = self._emoji.pick(gid, attach_mood)
            if picked is not None:
                self._last_emoji_at[gid] = now_done
                _record_tool("send_emoji")
                self._repo.record_event(gid, "send_face", ok=True, latency=0.0, detail="attach:" + attach_mood)
                if str(picked).startswith("face:"):
                    extra_actions.append(SendFace(gid, umo, str(picked).split(":", 1)[1]))
                else:
                    extra_actions.append(SendEmoji(gid, umo, str(picked)))

        prov: list[str] = []
        if saw_images:
            prov.append("seen_image")
        if tools_used:
            prov.append("tools=" + ",".join(tools_used))
        meta_str = ";".join(prov)

        self._repo.add_message(gid, "bot", "大肥鱼", "bot", cleaned, meta=meta_str)
        self._scheduler.record_bot_reply(
            gid,
            user_id=anchor.user_id if anchor else "",
            offered=_is_open_offer(cleaned),
            from_mentioned=bool(batch.mentioned and not batch.retold),
            now=now_done,
        )
        self._maybe_summarize(gid)

        tokens = total_tokens or (estimate_tokens(system) + estimate_tokens(batch_prompt) + estimate_tokens(cleaned))
        self._repo.record_event(gid, "reply", ok=True, latency=elapsed, tokens=tokens, detail=meta_str)

        actions: list[OutboundAction] = [SendText(gid, umo, cleaned, reply_to_msg_id=quote_mid), *extra_actions]
        return {"replied": True, "actions": actions, "reason": "reply", "text": cleaned}

    # ---- 后台异步群记忆归纳 + 滚动状态更新（委托给 MemoryConsolidator） ----
    def wait_background(self, timeout: float = 2.0) -> None:
        self.memory.wait_background(timeout=timeout)

    def _maybe_summarize(self, group_id: str) -> None:
        self.memory.maybe_summarize(group_id, llm=self._llm)

    def _summarize_sync(self, group_id: str, transcript: str, episode_ts: Optional[float] = None) -> None:
        self.memory.summarize_sync(group_id, transcript, episode_ts=episode_ts, llm=self._llm)

"""核心对话与工具编排引擎：原生 Tool Calling、强门禁、多模态直传、智能引用与异步记忆归纳。

零 AstrBot /零 SQLite /零 HTTP /零子进程依赖，只依赖 domain 协议。
"""

from __future__ import annotations

import json
import re
import threading
import time
from typing import Any, Callable, Optional, Sequence

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
    clip_text,
    format_speaker_message,
)
from .context import BEHAVIOR_RULES, ContextAssembler, estimate_tokens, should_quote_reply
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
ATTACH_EMOJI_COOLDOWN = 90.0
SUMMARY_INTERVAL = 600.0
HELP_TEXT = "群聊里直接 @大肥鱼 即可对话、看图、写代码/生成文件；也可以随时问我后台任务进度或让我停掉它。"

_PROMISE_LOOKUP_HINTS = (
    "我查一下", "我去查", "我去搜", "我这就查", "我现在去查",
    "马上核实", "我去核实", "我来查一下", "帮你查一下", "查一下近期",
)
_OFFER_HINTS = (
    "你要的话", "要不要", "需要的话", "发我", "告诉我",
    "帮你查", "帮你排", "想吃什么", "哪个", "哪种", "什么方向",
)

_MEMORY_SUMMARIZE_PROMPT = (
    "把下面这段群聊提炼成值得长期记住的事实记忆与滚动话题状态，只输出一行 JSON，不要解释：\n"
    '{"episode":"一句话概括这段在聊什么结论（无长期价值可留空）",'
    '"persons":[{"name":"具体群友昵称","category":"traits|preferences|events|relation","content":"稳定特征/偏好/事件"}],'
    '"points":[{"category":"event|preference|jargon|relation","content":"一条可复用事实；群内黑话用 jargon 且写成 词=解释"}],'
    '"state":{"topics":["当前活跃话题(<=3个)"],"progress":["已确认或解决的事项"],"pending":["待办或未完事项"]}}\n'
    "铁律：\n"
    "1. `[昵称：...]` 只是群名片或昵称，绝不是发言内容；禁止从昵称里编造事实，不要用“用户/群友”当人物 name。\n"
    "2. 禁止记录超时报错、没收到图、无法联网、连续道歉或临时问路天气等一次性噪音。\n"
    '3. 若无值得记住的内容就输出 {"episode":"","points":[],"persons":[],"state":{"topics":[],"progress":[],"pending":[]}}。'
)


def _is_open_offer(text: str) -> bool:
    s = str(text or "").strip()
    return bool(s) and (s.endswith(("?", "？")) or any(h in s for h in _OFFER_HINTS))


def _repeat_key(group_id: str, user_id: str, text: str) -> tuple[str, str, str]:
    clean = re.sub(r"[\s，,。.！!？?～~、…]+", "", str(text or ""))
    return (str(group_id), str(user_id), clean)


def _parse_jargon(content: str) -> Optional[tuple[str, str]]:
    raw = str(content or "").strip()
    for sep in ("=", "：", ":"):
        if sep in raw:
            w, a = (p.strip() for p in raw.split(sep, 1))
            if 1 <= len(w) <= 8 and a and w != a and len(a) <= 30 and "询问" not in w:
                return w, a
    return None


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
        self._repeat_seen: dict[tuple[str, str, str], float] = {}
        self._last_bot_reply: dict[str, tuple[tuple[Any, ...], float]] = {}
        self._last_emoji_at: dict[str, float] = {}
        self._summary_lock = threading.Lock()
        self._summary_threads_lock = threading.Lock()
        self._summary_threads: set[threading.Thread] = set()
        self._last_summary_attempt: dict[str, float] = {}

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
            specs.append(ToolSpec(
                name="send_emoji",
                description="在本群发送一张匹配当前心情的表情包；斗图时可只填 mood；日常闲聊顺手配图可同时传 reply_text（文字后自动附一张图）。",
                parameters={
                    "type": "object",
                    "properties": {
                        "mood": {"type": "string", "description": "2到4字情绪或梗图描述，例如 无语、开心、呆萌"},
                        "reply_text": {"type": "string", "description": "可选：同时发送的群聊文字回复；留空则纯发表情"},
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
                            "task": {"type": "string", "description": "完整具体的任务描述"},
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
        res = self._tasks.submit(req, ack_text=ack_text)
        if not res:
            return []
        notice = str(res.get("notice") or "").strip()
        if not notice:
            return []
        self._repo.add_message(gid, "bot", "大肥鱼", "bot", notice)
        self._scheduler.record_bot_reply(gid, now=time.time())
        return [SendText(gid, umo, notice)]

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
            tools_summary="请优先用天然群聊口语回应；需要查询或操作时直接调用合适的工具。",
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
        reply_text = ""
        attach_mood = ""
        terminal_actions: Optional[list[OutboundAction]] = None
        terminal_reason = "reply"

        for _ in range(MAX_TOOL_ROUNDS):
            try:
                turn = self._llm.chat(llm_messages, tools=tool_specs)
                total_tokens += int(turn.usage_tokens or 0)
            except Exception as exc:
                elapsed = time.monotonic() - started
                self._repo.record_event(gid, "model_error", ok=False, latency=elapsed, detail=str(exc))
                return {"replied": False, "actions": [], "reason": "model_error", "error": str(exc)}

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
                    inline_text = str(args.get("reply_text") or "").strip()
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
                    self._scheduler.record_bot_reply(gid, user_id=anchor.user_id if anchor else "", now=time.time())
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
                    t_prompt = str(args.get("task") or "").strip() or batch.body_text
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
                    msg = "已立即停止当前后台任务。" if stopped else "当前没有正在运行的后台任务。"
                    llm_messages.append(LLMMessage(role="tool", tool_call_id=tc.id, name=name, content=msg))
                    continue

                if name == "recall_memory":
                    q = str(args.get("query") or "").strip()
                    rows = self._repo.recall_memories(gid, q, limit=6)
                    llm_messages.append(LLMMessage(
                        role="tool", tool_call_id=tc.id, name=name,
                        content="；".join(rows) if rows else "没查到相关长期记忆",
                    ))
                    continue

                if name == "query_person":
                    target_name = str(args.get("name") or "").strip()
                    uid = self._repo.resolve_person(gid, target_name)
                    summary = self._repo.person_summary(gid, uid) if uid else ""
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

        # 若明确被 @ 且无进行中任务，但模型口头承诺“我去查”却忘了调 run_task，自动替它真正发起一次
        if (
            allow_task
            and running_task is None
            and not is_unsupported_capability(batch.body_text)
            and any(h in reply_text for h in _PROMISE_LOOKUP_HINTS)
        ):
            dispatched = self._dispatch_background(batch, batch.body_text, prev_history, ack_text=reply_text[:40])
            if dispatched:
                self._repo.record_event(gid, "task", ok=True, latency=elapsed, tokens=total_tokens)
                return {"replied": True, "actions": dispatched, "reason": "task"}

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
        quote_mid = ""
        if batch.scene == "group" and anchor and anchor.msg_id:
            latest = self._repo.recent_messages(gid, limit=12)
            quote_mid = should_quote_reply(anchor.msg_id, anchor.user_id, latest)

        self._repo.add_message(gid, "bot", "大肥鱼", "bot", cleaned)
        now_done = time.time()
        self._scheduler.record_bot_reply(
            gid,
            user_id=anchor.user_id if anchor else "",
            offered=_is_open_offer(cleaned),
            now=now_done,
        )
        self._maybe_summarize(gid)

        tokens = total_tokens or (estimate_tokens(system) + estimate_tokens(batch_prompt) + estimate_tokens(cleaned))
        self._repo.record_event(gid, "reply", ok=True, latency=elapsed, tokens=tokens)

        actions: list[OutboundAction] = [SendText(gid, umo, cleaned, reply_to_msg_id=quote_mid)]
        if attach_mood and has_emoji and self._emoji is not None:
            if now_done - self._last_emoji_at.get(gid, 0.0) >= ATTACH_EMOJI_COOLDOWN:
                picked = self._emoji.pick(gid, attach_mood)
                if picked is not None:
                    self._last_emoji_at[gid] = now_done
                    self._repo.record_event(gid, "send_face", ok=True, latency=0.0, detail="attach:" + attach_mood)
                    if str(picked).startswith("face:"):
                        actions.append(SendFace(gid, umo, str(picked).split(":", 1)[1]))
                    else:
                        actions.append(SendEmoji(gid, umo, str(picked)))

        return {"replied": True, "actions": actions, "reason": "reply", "text": cleaned}

    # ---- 后台异步群记忆归纳 + 滚动状态更新 ----
    def wait_background(self, timeout: float = 2.0) -> None:
        deadline = time.monotonic() + max(0.0, float(timeout or 0.0))
        with self._summary_threads_lock:
            threads = [t for t in self._summary_threads if t.is_alive()]
        for th in threads:
            rem = max(0.0, deadline - time.monotonic())
            th.join(timeout=rem)

    def _collect_summary_transcript(self, group_id: str) -> tuple[str, float]:
        last_ep = self._repo.last_episode_at(group_id)
        rows = self._repo.messages_since(group_id, ts=last_ep, limit=24)
        if not rows or not any(r.get("role") == "user" for r in rows):
            return "", 0.0
        lines: list[str] = []
        chars = 0
        max_ts = 0.0
        for r in rows:
            body = clip_text(re.sub(r"\s+", " ", str(r.get("text") or "").strip()), 160)
            if not body:
                continue
            rts = float(r.get("ts") or 0.0)
            max_ts = max(max_ts, rts)
            line = (
                ("大肥鱼：" + body)
                if r.get("role") in ("bot", "assistant")
                else format_speaker_message(r.get("user_name") or "群友", body)
            )
            if chars + len(line) > 2400 and lines:
                break
            lines.append(line)
            chars += len(line) + 1
        return "\n".join(lines), max_ts

    def _maybe_summarize(self, group_id: str) -> None:
        now = time.time()
        last = max(self._repo.last_episode_at(group_id), self._last_summary_attempt.get(group_id, 0.0))
        if last and now - last < SUMMARY_INTERVAL:
            return
        transcript, max_ts = self._collect_summary_transcript(group_id)
        if not transcript:
            return
        if not self._summary_lock.acquire(blocking=False):
            return
        self._last_summary_attempt[group_id] = now

        def _work() -> None:
            th = threading.current_thread()
            try:
                self._summarize_sync(group_id, transcript, episode_ts=max_ts or None)
            finally:
                with self._summary_threads_lock:
                    self._summary_threads.discard(th)
                self._summary_lock.release()

        th = threading.Thread(target=_work, name="echo-summary", daemon=True)
        with self._summary_threads_lock:
            self._summary_threads = {t for t in self._summary_threads if t.is_alive()}
            self._summary_threads.add(th)
        try:
            th.start()
        except Exception:
            with self._summary_threads_lock:
                self._summary_threads.discard(th)
            self._summary_lock.release()

    def _summarize_sync(self, group_id: str, transcript: str, episode_ts: Optional[float] = None) -> None:
        try:
            turn = self._llm.chat(
                [
                    LLMMessage(role="system", content="你是冷静精准的群聊记忆整理员。"),
                    LLMMessage(role="user", content=_MEMORY_SUMMARIZE_PROMPT + "\n\n" + transcript),
                ],
                tools=(),
            )
        except Exception:
            return
        raw = re.sub(r"```(?:json)?", "", str(turn.text or "").strip()).strip()
        m = re.search(r"\{.*\}", raw, re.S)
        if not m:
            return
        try:
            data = json.loads(m.group(0))
        except (ValueError, TypeError):
            return
        if not isinstance(data, dict):
            return

        ep = str(data.get("episode") or "").strip()[:120]
        if ep and not is_transient_memory_noise(ep) and not is_unsafe(ep):
            self._repo.add_episode(group_id, ep, now=episode_ts, source="chat")

        for item in (data.get("points") or [])[:4]:
            if not isinstance(item, dict):
                continue
            cat = str(item.get("category") or "event").strip()[:20]
            content = str(item.get("content") or "").strip()[:80]
            if not content or is_transient_memory_noise(content) or is_unsafe(content):
                continue
            if cat == "jargon":
                parsed_j = _parse_jargon(content)
                if parsed_j:
                    self._repo.add_memory(group_id, "jargon", "{}={}".format(*parsed_j))
            else:
                self._repo.add_memory(group_id, cat, content)

        generic_names = {"用户", "群友", "发言者", "对方", "我", "你", "大肥鱼", "回声"}
        for p in (data.get("persons") or [])[:3]:
            if not isinstance(p, dict):
                continue
            pname = str(p.get("name") or "").strip()
            pcontent = str(p.get("content") or "").strip()
            if not pname or pname in generic_names or not pcontent or is_transient_memory_noise(pcontent):
                continue
            uid = self._repo.resolve_person(group_id, pname)
            if not uid:
                continue
            field = {
                "person": "traits",
                "traits": "traits",
                "preference": "preferences",
                "preferences": "preferences",
                "event": "events",
                "events": "events",
                "relation": "relation",
                "relationship": "relation",
            }.get(str(p.get("category") or "traits").lower(), "traits")
            self._repo.upsert_person(group_id, uid, **{field: pcontent})

        state = data.get("state")
        if isinstance(state, dict):
            self._repo.save_conv_state(group_id, state)

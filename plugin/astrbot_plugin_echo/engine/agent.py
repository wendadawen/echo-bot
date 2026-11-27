"""核心对话与工具编排引擎：原生 Tool Calling、强门禁、多模态直传、智能引用与异步记忆归纳。

零 AstrBot /零 SQLite /零 HTTP /零子进程依赖，只依赖 domain 协议。
"""

from __future__ import annotations

import re
import threading
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
    SearchProvider,
    SendText,
    TaskRunner,
    ToolSpec,
    clean_display_text,
)
from .context import ContextAssembler, estimate_tokens, should_quote_reply
from .memory import MemoryConsolidator
from .safety import is_boilerplate, is_unsafe, scrub_internal_leaks
from .scheduler import Scheduler
from .tools import (
    TERMINAL_TOOLS as _TERMINAL_TOOLS,
    AgentToolkit,
    format_emoji_meta as _format_emoji_meta,
)

MAX_TOOL_ROUNDS = 4
REPEAT_WINDOW = 30.0
ATTACH_EMOJI_COOLDOWN = 45.0
HELP_TEXT = "群聊里直接 @大肥鱼 即可对话、看图、写代码/生成文件；也可以随时问我后台任务进度或让我停掉它。"

_OFFER_HINTS = (
    "你要的话", "要不要", "需要的话", "发我", "告诉我",
    "帮你查", "帮你排", "想吃什么", "哪个", "哪种", "什么方向",
)

_OVERLOAD_ERROR_KEYWORDS = (
    "529", "429", "503", "overloaded", "pool_overloaded",
    "rate_limit", "rate limit", "too many requests", "capacity", "号池负载",
)
_NETWORK_ERROR_KEYWORDS = (
    "timeout", "timed out", "urlopen", "connection",
    "remote end closed", "reset by peer", "502", "504",
)


def _is_open_offer(text: str) -> bool:
    s = str(text or "").strip()
    return bool(s) and (s.endswith(("?", "？")) or any(h in s for h in _OFFER_HINTS))


def _repeat_key(
    group_id: str,
    user_id: str,
    text: str,
    mentioned: bool = False,
    quote_msg_id: str = "",
) -> tuple[str, str, str, bool, str]:
    clean = re.sub(r"[\s，,。.！!？?～~、…]+", "", str(text or ""))
    return (str(group_id), str(user_id), clean, bool(mentioned), str(quote_msg_id or ""))


def _format_model_error_notice(exc: Exception) -> str:
    """将上游模型异常映射为简短自然的群聊提示，不泄露状态码或内部错误原文。"""
    msg = str(exc or "").lower()
    if any(k in msg for k in _OVERLOAD_ERROR_KEYWORDS):
        return "刚才接口这边有点挤，等几秒再喊我试试。"
    if any(k in msg for k in _NETWORK_ERROR_KEYWORDS):
        return "刚才网络卡了一下没连上，稍后再喊我试试。"
    return "刚才我这边卡壳了一下，稍后再喊我试试。"


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
        self.context = ContextAssembler(
            repo,
            history_limit=history_limit,
            **({"persona": persona} if persona else {}),
        )
        self.memory = MemoryConsolidator(repo, llm)
        self._toolkit = AgentToolkit(
            repo,
            scheduler,
            emoji_store=emoji_store,
            task_runner=task_runner,
            search_provider=search_provider,
            secrets_fn=self._secrets,
        )
        self._state_lock = threading.Lock()
        self._repeat_seen: dict[tuple[str, str, str, bool, str], float] = {}
        self._last_bot_reply: dict[str, tuple[tuple[Any, ...], float]] = {}
        self._last_emoji_at: dict[str, float] = {}

    @property
    def _emoji(self) -> Optional[EmojiStorePort]:
        return self._toolkit._emoji

    @_emoji.setter
    def _emoji(self, val: Optional[EmojiStorePort]) -> None:
        self._toolkit._emoji = val

    @property
    def _tasks(self) -> Optional[TaskRunner]:
        return self._toolkit._tasks

    @_tasks.setter
    def _tasks(self, val: Optional[TaskRunner]) -> None:
        self._toolkit._tasks = val

    @property
    def _search(self) -> Optional[SearchProvider]:
        return self._toolkit._search

    @_search.setter
    def _search(self, val: Optional[SearchProvider]) -> None:
        self._toolkit._search = val

    def _secrets(self) -> tuple[str, ...]:
        out = list(self._llm.extra_secrets() or ())
        if self._search is not None:
            extra_fn = getattr(self._search, "extra_secrets", None)
            if callable(extra_fn):
                out.extend(extra_fn() or ())
            else:
                skey = getattr(self._search, "api_key", "")
                if skey:
                    out.append(str(skey))
        return tuple(dict.fromkeys(str(s) for s in out if len(str(s)) >= 8))

    def _release_repeat_keys(self, keys: list[tuple[str, str, str, bool, str]]) -> None:
        if not keys:
            return
        with self._state_lock:
            for k in keys:
                self._repeat_seen.pop(k, None)

    def _persist_batch_items(self, batch: Batch) -> None:
        if batch.persisted:
            return
        unpersisted = [i for i in batch.items if not getattr(i, "persisted", False)]
        for item in unpersisted[-8:]:
            self._repo.observe_person(batch.group_id, item.user_id, item.user_name, now=item.ts or None)
        for item in unpersisted:
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

    def _record_emoji_sent(self, gid: str, now_ts: float) -> None:
        with self._state_lock:
            self._last_emoji_at[gid] = now_ts

    def _resolve_quote_mid(self, gid: str, anchor: Any) -> str:
        """群聊思考或报错期间若已有插话，自动引用触发该轮的原始消息 ID。"""
        if anchor and anchor.msg_id:
            latest = self._repo.recent_messages(gid, limit=12)
            return should_quote_reply(anchor.msg_id, anchor.user_id, latest)
        return ""

    def _check_repeat_guard(
        self,
        batch: Batch,
        now: float,
    ) -> tuple[list[tuple[str, str, str, bool, str]], Optional[dict[str, Any]]]:
        if batch.has_image:
            return [], None
        gid = batch.group_id
        repeat_keys = [
            _repeat_key(
                gid,
                i.user_id,
                i.text,
                mentioned=bool(i.mentioned or batch.mentioned),
                quote_msg_id=i.quote_msg_id,
            )
            for i in batch.items
            if str(i.text or "").strip()
        ]
        if not repeat_keys:
            return [], None
        with self._state_lock:
            if all(now - self._repeat_seen.get(k, 0.0) < REPEAT_WINDOW for k in repeat_keys):
                return [], {"replied": False, "actions": [], "reason": "repeat"}
            for k in repeat_keys:
                self._repeat_seen[k] = now
            if len(self._repeat_seen) > 500:
                cutoff = now - REPEAT_WINDOW
                self._repeat_seen = {k: v for k, v in self._repeat_seen.items() if v >= cutoff}
        return repeat_keys, None

    def _model_error_result(
        self,
        batch: Batch,
        exc: Exception,
        started: float,
        repeat_keys: list[tuple[str, str, str, bool, str]],
    ) -> dict[str, Any]:
        gid = batch.group_id
        umo = batch.umo
        anchor = batch.anchor
        elapsed = time.monotonic() - started
        self._repo.record_event(gid, "model_error", ok=False, latency=elapsed, detail=str(exc))
        self._release_repeat_keys(repeat_keys)
        if batch.mentioned and not batch.retold:
            quote_mid = self._resolve_quote_mid(gid, anchor)
            notice = _format_model_error_notice(exc)
            return {
                "replied": True,
                "actions": [SendText(gid, umo, notice, reply_to_msg_id=quote_mid)],
                "reason": "model_error",
                "error": str(exc),
                "text": notice,
            }
        return {"replied": False, "actions": [], "reason": "model_error", "error": str(exc), "text": ""}

    def _run_tool_loop(
        self,
        batch: Batch,
        prev_history: list[dict[str, Any]],
        llm_messages: list[LLMMessage],
        tool_specs: list[ToolSpec],
        alias_maps: tuple[dict[str, str], dict[str, str]],
        caps: dict[str, bool],
        *,
        now: float,
        started: float,
        repeat_keys: list[tuple[str, str, str, bool, str]],
    ) -> dict[str, Any]:
        gid = batch.group_id
        real_to_alias, alias_to_real = alias_maps
        total_tokens = 0
        tools_used: list[str] = []
        reply_text = ""
        attach_mood = ""
        marked_viewed = False

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
                return {"early": self._model_error_result(batch, exc, started, repeat_keys)}

            if not turn.tool_calls:
                reply_text = str(turn.text or "").strip()
                break

            llm_messages.append(LLMMessage(role="assistant", content=turn.text or "", tool_calls=turn.tool_calls))
            should_stop_loop = False
            ordered_tcs = [tc for tc in turn.tool_calls if tc.name not in _TERMINAL_TOOLS] + [
                tc for tc in turn.tool_calls if tc.name in _TERMINAL_TOOLS
            ]
            for tc in ordered_tcs:
                args = tc.arguments if isinstance(tc.arguments, dict) else {}
                outcome = self._toolkit.execute_tool_call(
                    tc.name,
                    args,
                    turn_text=turn.text or "",
                    batch=batch,
                    prev_history=prev_history,
                    real_to_alias=real_to_alias,
                    alias_to_real=alias_to_real,
                    **caps,
                    tools_used=tools_used,
                    total_tokens=total_tokens,
                    started=started,
                    record_tool_fn=_record_tool,
                    on_emoji_sent=self._record_emoji_sent,
                    on_summarize=self._maybe_summarize,
                )
                status = outcome["status"]
                if status == "early":
                    if outcome.get("release_repeat"):
                        self._release_repeat_keys(repeat_keys)
                    return {"early": outcome["result"]}
                if status == "attach":
                    reply_text = outcome["reply_text"]
                    attach_mood = outcome["attach_mood"]
                    should_stop_loop = True
                    break
                llm_messages.append(
                    LLMMessage(role="tool", tool_call_id=tc.id, name=tc.name, content=outcome["content"])
                )

            if should_stop_loop:
                break

        if not reply_text and not attach_mood and tools_used:
            try:
                wrap_turn = self._llm.chat(llm_messages, tools=())
                total_tokens += int(wrap_turn.usage_tokens or 0)
                reply_text = str(wrap_turn.text or "").strip()
            except Exception as exc:
                return {"early": self._model_error_result(batch, exc, started, repeat_keys)}

        return {
            "reply_text": reply_text,
            "attach_mood": attach_mood,
            "total_tokens": total_tokens,
            "tools_used": tools_used,
        }

    def _finalize_reply(
        self,
        batch: Batch,
        loop_res: dict[str, Any],
        *,
        has_emoji: bool,
        saw_images: bool,
        prompt_tokens: int,
        started: float,
        repeat_keys: list[tuple[str, str, str, bool, str]],
    ) -> dict[str, Any]:
        gid = batch.group_id
        umo = batch.umo
        anchor = batch.anchor
        raw_text = (anchor.text if anchor else "").strip()
        elapsed = time.monotonic() - started
        reply_text = loop_res["reply_text"]
        attach_mood = loop_res["attach_mood"]
        tools_used = loop_res["tools_used"]
        total_tokens = loop_res["total_tokens"]

        if not reply_text:
            self._release_repeat_keys(repeat_keys)
            self._repo.record_event(gid, "silent", ok=True, latency=elapsed, tokens=total_tokens)
            return {"replied": False, "actions": [], "reason": "silent"}

        if is_boilerplate(reply_text):
            self._release_repeat_keys(repeat_keys)
            self._repo.record_event(gid, "filtered", ok=False, latency=elapsed)
            return {"replied": False, "actions": [], "reason": "filtered"}

        cleaned = clean_display_text(scrub_internal_leaks(reply_text, extra_secrets=self._secrets()))
        if not cleaned:
            self._release_repeat_keys(repeat_keys)
            self._repo.record_event(gid, "filtered", ok=False, latency=elapsed)
            return {"replied": False, "actions": [], "reason": "filtered"}

        now_done = time.time()
        rkey = (_repeat_key(gid, "bot", cleaned), _repeat_key(gid, anchor.user_id if anchor else "", raw_text))
        with self._state_lock:
            last_bot = self._last_bot_reply.get(gid)
            if last_bot and last_bot[0] == rkey and now_done - last_bot[1] < REPEAT_WINDOW:
                return {"replied": False, "actions": [], "reason": "repeat_reply"}
            self._last_bot_reply[gid] = (rkey, now_done)
            if len(self._last_bot_reply) > 200:
                cutoff_bot = now_done - REPEAT_WINDOW
                self._last_bot_reply = {k: v for k, v in self._last_bot_reply.items() if v[1] >= cutoff_bot}
            last_emoji_ts = self._last_emoji_at.get(gid, 0.0)

        quote_mid = self._resolve_quote_mid(gid, anchor)
        extra_actions: list[OutboundAction] = []
        emoji_act, emoji_meta = self._toolkit.attach_reply_emoji(
            gid,
            umo,
            attach_mood=attach_mood,
            has_emoji=has_emoji,
            body_text=batch.body_text or "",
            last_emoji_ts=last_emoji_ts,
            now_done=now_done,
            cooldown_sec=ATTACH_EMOJI_COOLDOWN,
            tools_used=tools_used,
        )
        if emoji_act is not None:
            self._record_emoji_sent(gid, now_done)
            extra_actions.append(emoji_act)

        prov = (
            (["seen_image"] if saw_images else [])
            + (["tools=" + ",".join(tools_used)] if tools_used else [])
            + ([emoji_meta] if emoji_meta else [])
        )
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

        tokens = total_tokens or (prompt_tokens + estimate_tokens(cleaned))
        self._repo.record_event(gid, "reply", ok=True, latency=elapsed, tokens=tokens, detail=meta_str)
        actions: list[OutboundAction] = [SendText(gid, umo, cleaned, reply_to_msg_id=quote_mid), *extra_actions]
        return {"replied": True, "actions": actions, "reason": "reply", "text": cleaned}

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

        if raw_text in ("/帮助", "／帮助", "/help"):
            self._persist_batch_items(batch)
            self._repo.add_message(gid, "bot", "大肥鱼", "bot", HELP_TEXT)
            self._scheduler.record_bot_reply(gid, user_id=anchor.user_id if anchor else "")
            return {"replied": True, "actions": [SendText(gid, umo, HELP_TEXT)], "reason": "help"}

        if any(is_unsafe(i.text) for i in batch.items) or is_unsafe(raw_text) or is_unsafe(batch.quote_text):
            self._persist_batch_items(batch)
            if batch.mentioned:
                return {"replied": True, "actions": [SendText(gid, umo, "这个不干。")], "reason": "unsafe"}
            return {"replied": False, "actions": [], "reason": "unsafe_silent"}

        raw_history = self._repo.recent_messages(gid, limit=self.context.history_limit + len(batch.items))
        batch_seqs = {
            int(getattr(i, "db_seq", 0) or 0)
            for i in batch.items
            if int(getattr(i, "db_seq", 0) or 0) > 0
        }
        batch_mids = {str(i.msg_id) for i in batch.items if str(i.msg_id or "").strip()}
        prev_history = [
            r
            for r in raw_history
            if (not batch_seqs or int(r.get("seq") or 0) not in batch_seqs)
            and (not r.get("msg_id") or str(r.get("msg_id")) not in batch_mids)
        ][-self.context.history_limit:]
        self._persist_batch_items(batch)

        now = time.time()
        repeat_keys, repeat_early = self._check_repeat_guard(batch, now)
        if repeat_early is not None:
            return repeat_early

        self._scheduler.merge_recent_images(batch, now=now)

        running_task = self._tasks.running_for_group(gid) if self._tasks is not None else None
        caps = {
            "allow_task": bool(
                self._tasks is not None
                and self._tasks.available()
                and batch.mentioned
                and not batch.retold
                and not batch.is_poke
            ),
            "allow_silent": not (batch.mentioned and not batch.retold),
            "has_emoji": bool(self._emoji is not None and self._emoji.has_entries(gid)),
            "can_react": bool(batch.target_msg_id),
        }
        has_search = bool(self._search is not None and self._search.available())

        tool_specs, _ = self._toolkit.build_tools(
            batch, **caps, has_search=has_search, has_running_task=running_task is not None,
        )

        system, compacted_history, real_to_alias, alias_to_real = self.context.build(
            batch, prev_history, running_task=running_task, now=now,
        )

        llm_messages: list[LLMMessage] = [LLMMessage(role="system", content=system)]
        for row in compacted_history:
            r_role, r_content = self.context.format_history_row(row, real_to_alias, now)
            llm_messages.append(LLMMessage(role=r_role, content=r_content))

        batch_prompt = self.context.format_batch_prompt(batch, real_to_alias, now)
        inline_imgs = tuple(ImagePart(data=m.data) for m in batch.images[:2] if m.data)
        llm_messages.append(LLMMessage(role="user", content=batch_prompt, images=inline_imgs))

        started = time.monotonic()
        loop_res = self._run_tool_loop(
            batch, prev_history, llm_messages, tool_specs, (real_to_alias, alias_to_real),
            caps, now=now, started=started, repeat_keys=repeat_keys,
        )
        if "early" in loop_res:
            return loop_res["early"]

        return self._finalize_reply(
            batch, loop_res,
            has_emoji=caps["has_emoji"],
            saw_images=bool(inline_imgs),
            prompt_tokens=estimate_tokens(system) + estimate_tokens(batch_prompt),
            started=started,
            repeat_keys=repeat_keys,
        )

    # ---- 后台异步群记忆归纳 + 滚动状态更新（委托给 MemoryConsolidator） ----
    def wait_background(self, timeout: float = 2.0) -> None:
        self.memory.wait_background(timeout=timeout)

    def _maybe_summarize(self, group_id: str) -> None:
        self.memory.maybe_summarize(group_id, llm=self._llm)

    def _summarize_sync(self, group_id: str, transcript: str, episode_ts: Optional[float] = None) -> None:
        self.memory.summarize_sync(group_id, transcript, episode_ts=episode_ts, llm=self._llm)

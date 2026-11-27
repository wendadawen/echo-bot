"""Agent 工具规格构建、非终结工具调度、后台任务派发与表情抽取辅助模块。"""

from __future__ import annotations

import re
import time
from typing import Any, Callable, Optional

from ..domain import (
    Batch,
    EmojiStorePort,
    MemoryRepository,
    OutboundAction,
    PickedEmoji,
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
from .safety import (
    is_transient_memory_noise,
    is_unsafe,
    is_unsupported_capability,
    scrub_internal_leaks,
)
from .scheduler import Scheduler

TERMINAL_TOOLS = frozenset({"stay_silent", "react_message", "send_emoji", "run_task"})
EXPLICIT_EMOJI_HINTS = ("表情包", "斗图", "发个表情", "发表情", "再来一张", "换一张", "发张表情", "配个图", "配张图")
VAGUE_TASK_PROMPTS = frozenset({
    "查一下", "去查一下", "查一查", "帮我查", "帮我查一下",
    "搜一下", "去搜一下", "搜一搜", "看看", "去看看", "查查看", "查",
})


def is_vague_task_prompt(task_prompt: str) -> bool:
    clean = re.sub(r"[\s，,。.！!？?～~、…]+", "", str(task_prompt or ""))
    return not clean or clean in VAGUE_TASK_PROMPTS or len(clean) < 6


def format_emoji_meta(picked: PickedEmoji) -> str:
    tag = re.sub(r"[;\r\n]+", " ", str(picked.tag or "")).strip()[:20]
    desc = re.sub(r"[;\r\n]+", " ", str(picked.description or "")).strip()[:40]
    if picked.kind == "face":
        label = tag or picked.face_id
        return "emoji=QQ表情:{}".format(label) if label else ""
    if desc and desc != tag:
        head = "{}（{}）".format(tag, desc) if tag else desc
        return "emoji={}".format(head)
    return "emoji={}".format(tag or "表情")


class AgentToolkit:
    """统一管理 EchoAgent 的 ToolSpec 生成、非终结工具执行、后台任务派发与表情选择。"""

    def __init__(
        self,
        repo: MemoryRepository,
        scheduler: Scheduler,
        *,
        emoji_store: Optional[EmojiStorePort] = None,
        task_runner: Optional[TaskRunner] = None,
        search_provider: Optional[SearchProvider] = None,
        secrets_fn: Optional[Callable[[], tuple[str, ...]]] = None,
    ):
        self._repo = repo
        self._scheduler = scheduler
        self._emoji = emoji_store
        self._tasks = task_runner
        self._search = search_provider
        self._secrets_fn = secrets_fn

    def build_tools(
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

    def dispatch_background(
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
        non_sticker_imgs = (
            tuple(m.data for m in batch.images if m.data and not m.is_sticker)[:2]
            or tuple(m.data for m in batch.images if m.data)[:2]
        )
        req = TaskRequest(
            group_id=gid,
            umo=umo,
            prompt=prompt,
            images=non_sticker_imgs,
            pre_history=tuple(pre_history[-6:]),
        )
        secrets = self._secrets_fn() if callable(self._secrets_fn) else ()
        clean_ack = clean_display_text(scrub_internal_leaks(ack_text, extra_secrets=secrets)) if ack_text else ""
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

    def pick_emoji(self, gid: str, mood: str) -> Optional[PickedEmoji]:
        if self._emoji is None:
            return None
        pick_entry = getattr(self._emoji, "pick_entry", None)
        if callable(pick_entry):
            picked = pick_entry(gid, mood)
            if isinstance(picked, PickedEmoji):
                return picked
        raw = self._emoji.pick(gid, mood)
        if raw is None:
            return None
        s = str(raw)
        if s.startswith("face:"):
            return PickedEmoji(kind="face", face_id=s.split(":", 1)[1], tag=mood)
        return PickedEmoji(kind="image", path=s, tag=mood)

    def attach_reply_emoji(
        self,
        gid: str,
        umo: str,
        *,
        attach_mood: str,
        has_emoji: bool,
        body_text: str,
        last_emoji_ts: float,
        now_done: float,
        cooldown_sec: float,
        tools_used: list[str],
    ) -> tuple[Optional[OutboundAction], str]:
        """文字回复完成后按冷却与意图附带一张表情动作，返回 (action, emoji_meta)。"""
        explicit_req = any(h in (body_text or "") for h in EXPLICIT_EMOJI_HINTS)
        cooldown_ok = explicit_req or (now_done - last_emoji_ts >= cooldown_sec)
        if not (attach_mood and has_emoji and self._emoji is not None and cooldown_ok):
            return None, ""
        picked = self.pick_emoji(gid, attach_mood)
        if picked is None:
            return None, ""
        if "send_emoji" not in tools_used:
            tools_used.append("send_emoji")
        emeta = format_emoji_meta(picked)
        self._repo.record_event(gid, "send_face", ok=True, latency=0.0, detail="attach:" + attach_mood)
        act: OutboundAction = (
            SendFace(gid, umo, picked.face_id) if picked.kind == "face" else SendEmoji(gid, umo, picked.path)
        )
        return act, emeta

    def execute_tool_call(
        self,
        name: str,
        args: dict[str, Any],
        *,
        turn_text: str,
        batch: Batch,
        prev_history: list[dict[str, Any]],
        real_to_alias: dict[str, str],
        alias_to_real: dict[str, str],
        allow_task: bool,
        allow_silent: bool,
        has_emoji: bool,
        can_react: bool,
        tools_used: list[str],
        total_tokens: int,
        started: float,
        record_tool_fn: Callable[[str], None],
        on_emoji_sent: Optional[Callable[[str, float], None]] = None,
        on_summarize: Optional[Callable[[str], None]] = None,
    ) -> dict[str, Any]:
        """统一调度终结工具与非终结工具，返回 status=early|attach|continue 的结构化指令。"""
        gid = batch.group_id
        umo = batch.umo
        anchor = batch.anchor

        if name == "stay_silent":
            if allow_silent:
                elapsed = time.monotonic() - started
                self._repo.record_event(gid, "silent", ok=True, latency=elapsed, tokens=total_tokens)
                return {
                    "status": "early",
                    "release_repeat": True,
                    "result": {"replied": False, "actions": [], "reason": "silent"},
                }
            return {"status": "continue", "content": "有人明确点名问你，不能保持沉默，请直接回复或调用工具。"}

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
                    return {
                        "status": "early",
                        "release_repeat": False,
                        "result": {"replied": True, "actions": [ReactEmoji(gid, umo, real_mid, emoji_id)], "reason": "react"},
                    }
            return {"status": "continue", "content": "没有可贴表情的目标消息。"}

        if name == "send_emoji":
            if not has_emoji or self._emoji is None:
                return {"status": "continue", "content": "本群表情库暂无可用表情，请直接用文字回复。"}
            mood = str(args.get("mood") or "").strip()[:30] or "随手"
            inline_text = str(args.get("reply_text") or "").strip() or str(turn_text or "").strip()
            if inline_text:
                return {"status": "attach", "reply_text": inline_text, "attach_mood": mood}
            picked = self.pick_emoji(gid, mood)
            if picked is None:
                if batch.mentioned and not batch.retold:
                    return {"status": "continue", "content": "本群表情库暂未匹配到合适表情，请直接用简洁文字回复群友。"}
                elapsed = time.monotonic() - started
                self._repo.record_event(gid, "silent", ok=True, latency=elapsed)
                return {
                    "status": "early",
                    "release_repeat": True,
                    "result": {"replied": False, "actions": [], "reason": "silent"},
                }
            now_emoji = time.time()
            if callable(on_emoji_sent):
                on_emoji_sent(gid, now_emoji)
            self._repo.record_event(
                gid, "send_face", ok=True, latency=time.monotonic() - started, tokens=total_tokens, detail=mood,
            )
            record_tool_fn("send_emoji")
            emeta = format_emoji_meta(picked)
            prov_meta = ["tools=" + ",".join(tools_used)]
            if emeta:
                prov_meta.append(emeta)
            self._repo.add_message(gid, "bot", "大肥鱼", "bot", "[发了一张表情包]", meta=";".join(prov_meta))
            self._scheduler.record_bot_reply(
                gid,
                user_id=anchor.user_id if anchor else "",
                from_mentioned=bool(batch.mentioned and not batch.retold),
                now=now_emoji,
            )
            if callable(on_summarize):
                on_summarize(gid)
            act: OutboundAction = (
                SendFace(gid, umo, picked.face_id) if picked.kind == "face" else SendEmoji(gid, umo, picked.path)
            )
            return {"status": "early", "release_repeat": False, "result": {"replied": True, "actions": [act], "reason": "face"}}

        if name == "run_task":
            if not allow_task:
                return {
                    "status": "continue",
                    "content": "没有明确 @ 或点名你，禁止启动后台工具！请调用 stay_silent 或直接简要回复。",
                }
            t_prompt = str(args.get("task") or "").strip()
            if is_vague_task_prompt(t_prompt):
                return {
                    "status": "continue",
                    "content": "task 描述缺少具体对象。请结合上文把要查的具体主题/关键词写清楚，再调 web_search 或 run_task；若上文也没写明，直接问群友要查什么。",
                }
            ack = str(args.get("ack_text") or "").strip()
            dispatched = self.dispatch_background(batch, t_prompt, prev_history, ack_text=ack)
            if dispatched:
                elapsed = time.monotonic() - started
                self._repo.record_event(gid, "task", ok=True, latency=elapsed, tokens=total_tokens)
                return {
                    "status": "early",
                    "release_repeat": False,
                    "result": {"replied": True, "actions": dispatched, "reason": "task"},
                }
            return {"status": "continue", "content": "后台执行器当前不可用。"}

        return {
            "status": "continue",
            "content": self.execute_non_terminal(
                name, args, gid=gid, allow_task=allow_task, record_tool_fn=record_tool_fn,
            ),
        }

    def execute_non_terminal(
        self,
        name: str,
        args: dict[str, Any],
        *,
        gid: str,
        allow_task: bool,
        record_tool_fn: Callable[[str], None],
    ) -> str:
        if name == "task_status":
            info = self._tasks.status(gid) if self._tasks else None
            record_tool_fn(name)
            return (
                "任务「{}」已运行 {:.0f} 秒，最新进展：{}".format(info.prompt, info.elapsed, info.progress)
                if info else "当前本群没有正在运行的后台任务。"
            )
        if name == "task_stop":
            if not allow_task:
                return "未点名不可停止后台任务。"
            stopped = self._tasks.stop(gid) if self._tasks else False
            record_tool_fn(name)
            return "已立即停止当前后台任务。" if stopped else "当前没有正在运行的后台任务。"
        if name == "recall_memory":
            q = str(args.get("query") or "").strip()
            rows = self._repo.recall_memories(gid, q, limit=6)
            record_tool_fn(name)
            return "；".join(rows) if rows else "没查到相关长期记忆"
        if name == "query_person":
            target_name = str(args.get("name") or "").strip()
            uid = self._repo.resolve_person(gid, target_name)
            summary = self._repo.person_summary(gid, uid) if uid else ""
            record_tool_fn(name)
            return summary or "没记着这人的特别档案"
        if name == "save_memory":
            content = str(args.get("content") or "").strip()
            cat = str(args.get("category") or "event").strip()
            if content and not is_transient_memory_noise(content) and not is_unsafe(content):
                self._repo.add_memory(gid, cat, content)
                record_tool_fn(name)
                return "记下了：" + content
            return "这条临时或敏感内容不用记。"
        if name == "web_search":
            q = str(args.get("query") or "").strip()
            if self._search and self._search.available() and q:
                try:
                    hits = self._search.search(q, limit=4)
                    record_tool_fn(name)
                    return "\n".join(
                        "- {}：{} ({})".format(h.title, h.snippet, h.url) for h in hits
                    ) or "未搜索到相关结果"
                except Exception as exc:
                    return "搜索失败：{}".format(exc)
            return "未配置联网搜索服务"
        return "未知工具"

"""异步群记忆归纳与滚动话题状态提炼：去重防抖、防自身说教入库与防运行噪音污染。"""

from __future__ import annotations

import json
import re
import threading
import time
from typing import Optional

from ..domain import LLMMessage, MemoryRepository, clip_text, format_speaker_message
from .safety import is_transient_memory_noise, is_unsafe

SUMMARY_INTERVAL = 600.0

_PLACEHOLDER_VALUES = frozenset({"-", "—", "–", "无", "暂无", "未知", "none", "null", "n/a"})
_BOT_SELF_MEMORY_PREFIXES = (
    "大肥鱼对",
    "大肥鱼曾",
    "大肥鱼建议",
    "大肥鱼认为",
    "大肥鱼称",
    "大肥鱼在",
    "机器人",
)
_GENERIC_JARGON_TERMS = frozenset({
    "软连接", "软链接", "符号链接", "symlink", "docker", "api", "token",
    "proxy", "cuda", "ssh", "git", "python", "json", "html", "csv", "http",
})
_GENERIC_TECH_EXPLANATIONS = ("符号链接", "symlink")

_MEMORY_SUMMARIZE_PROMPT = (
    "把下面这段群聊提炼成值得长期记住的事实记忆与滚动话题状态，只输出一行 JSON，不要解释：\n"
    '{"episode":"一句话概括这段在聊什么结论（无长期价值可留空）",'
    '"persons":[{"name":"具体群友昵称","category":"traits|preferences|events|relation","content":"稳定特征/偏好/事件"}],'
    '"points":[{"category":"event|preference|jargon|relation","content":"一条可复用事实；仅限本群特有的梗/代号写成 词=解释"}],'
    '"state":{"topics":["当前活跃话题(<=3个)"],"progress":["已确认或解决的事项"],"pending":["待办或未完事项"]}}\n'
    "铁律：\n"
    "1. `[昵称：...]` 只是群名片或昵称，绝不是发言内容；禁止从昵称编造事实，不要用“用户/群友”当人物 name。\n"
    "2. 禁止记录超时报错、529/429、没收到图、无法联网、未核实、免责自述、道歉，以及大肥鱼自己的建议/观点。\n"
    "3. 禁止把常见编程/Linux/AI 通用术语（如软连接、Docker、API、symlink）当作群黑话；没有明确内容就留空，不要填“-”。\n"
    '4. 若无值得记住的内容就输出 {"episode":"","points":[],"persons":[],"state":{"topics":[],"progress":[],"pending":[]}}。'
)


def parse_jargon(content: str) -> Optional[tuple[str, str]]:
    raw = str(content or "").strip()
    for sep in ("=", "：", ":"):
        if sep in raw:
            w, a = (p.strip() for p in raw.split(sep, 1))
            if (
                1 <= len(w) <= 8
                and a
                and w != a
                and len(a) <= 30
                and "询问" not in w
                and w.lower() not in _GENERIC_JARGON_TERMS
                and not any(t in a.lower() for t in _GENERIC_TECH_EXPLANATIONS)
            ):
                return w, a
    return None


def _is_valid_memory_text(text: str) -> bool:
    s = str(text or "").strip()
    if not s or s.lower() in _PLACEHOLDER_VALUES:
        return False
    if is_transient_memory_noise(s) or is_unsafe(s):
        return False
    if s.startswith(_BOT_SELF_MEMORY_PREFIXES):
        return False
    return True


class MemoryConsolidator:
    """后台非阻塞群聊记忆整理器（每群带冷却节流与单并发锁）。"""

    def __init__(
        self,
        repo: MemoryRepository,
        llm,
        *,
        summary_interval: float = SUMMARY_INTERVAL,
    ):
        self._repo = repo
        self._llm = llm
        self._summary_interval = float(summary_interval)
        self._summary_lock = threading.Lock()
        self._threads_lock = threading.Lock()
        self._threads: set[threading.Thread] = set()
        self._last_attempt: dict[str, float] = {}

    def wait_background(self, timeout: float = 2.0) -> None:
        deadline = time.monotonic() + max(0.0, float(timeout or 0.0))
        with self._threads_lock:
            threads = [t for t in self._threads if t.is_alive()]
        for th in threads:
            rem = max(0.0, deadline - time.monotonic())
            th.join(timeout=rem)

    def _collect_transcript(self, group_id: str) -> tuple[str, float]:
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

    def maybe_summarize(self, group_id: str, llm=None) -> None:
        active_llm = llm or self._llm
        now = time.time()
        last = max(self._repo.last_episode_at(group_id), self._last_attempt.get(group_id, 0.0))
        if last and now - last < self._summary_interval:
            return
        transcript, max_ts = self._collect_transcript(group_id)
        if not transcript:
            return
        if not self._summary_lock.acquire(blocking=False):
            return
        self._last_attempt[group_id] = now

        def _work() -> None:
            th = threading.current_thread()
            try:
                self.summarize_sync(group_id, transcript, episode_ts=max_ts or None, llm=active_llm)
            finally:
                with self._threads_lock:
                    self._threads.discard(th)
                self._summary_lock.release()

        th = threading.Thread(target=_work, name="echo-summary", daemon=True)
        with self._threads_lock:
            self._threads = {t for t in self._threads if t.is_alive()}
            self._threads.add(th)
        try:
            th.start()
        except Exception:
            with self._threads_lock:
                self._threads.discard(th)
            self._summary_lock.release()

    def summarize_sync(
        self,
        group_id: str,
        transcript: str,
        episode_ts: Optional[float] = None,
        llm=None,
    ) -> None:
        active_llm = llm or self._llm
        try:
            turn = active_llm.chat(
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
        if _is_valid_memory_text(ep):
            self._repo.add_episode(group_id, ep, now=episode_ts, source="chat")

        for item in (data.get("points") or [])[:4]:
            if not isinstance(item, dict):
                continue
            cat = str(item.get("category") or "event").strip()[:20]
            content = str(item.get("content") or "").strip()[:80]
            if not _is_valid_memory_text(content):
                continue
            if cat == "jargon":
                parsed_j = parse_jargon(content)
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
            if (
                not pname
                or pname in generic_names
                or not _is_valid_memory_text(pcontent)
            ):
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
            clean_state = {
                k: [
                    str(v).strip()
                    for v in (state.get(k) or [])
                    if _is_valid_memory_text(str(v or ""))
                ][:5]
                for k in ("topics", "progress", "pending")
            }
            self._repo.save_conv_state(group_id, clean_state)

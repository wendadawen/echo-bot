"""核心领域模型与抽象端口协议。

本模块仅依赖 Python 标准库，定义系统内部不可变数据结构与 5 个稳定抽象边界：
- LLMClient：大模型调用与多模态理解（可随时切换供应商或推理模型）
- SearchProvider：可选联网直搜接口（Tavily/Exa 等，未配置自动隐藏）
- MemoryRepository：统一持久化（消息、群记忆、QQ 号归一人物档案、待投递队列、监控）
- EmojiStorePort：表情包收藏与按情绪抽取
- TaskRunner / HarnessBackend：可插拔后台沙箱执行器（Codex 等）
- MessageSender：平台消息发送与互动（文本、智能引用、表情、群文件、贴表情）
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional, Protocol, Sequence, runtime_checkable

from .text_media import (
    EXTENDED_FACE_LABELS,
    FACE_LABELS,
    _replace_md_link,
    clean_display_text,
    clean_speaker_name,
    clip_sentence,
    clip_text,
    detect_image_dimensions,
    detect_image_format,
    format_history_time,
    format_speaker_message,
    resolve_face_label,
)


@dataclass(frozen=True)
class MediaRef:
    data: bytes
    sha256: str = ""
    is_sticker: bool = False
    summary: str = ""


@dataclass(frozen=True)
class FaceRef:
    face_id: str
    label: str = ""


@dataclass(frozen=True)
class PickedEmoji:
    kind: str  # "image" | "face"
    path: str = ""
    face_id: str = ""
    tag: str = ""
    description: str = ""


@dataclass(frozen=True)
class ChatEvent:
    group_id: str
    user_id: str
    user_name: str
    text: str
    raw_text: str = ""
    msg_id: str = ""
    ts: float = 0.0
    umo: str = ""
    at_self: bool = False
    at_others: bool = False
    reply_to_bot: bool = False
    self_called: bool = False
    retold: bool = False
    direct_question: bool = False
    mentioned: bool = False
    quote_text: str = ""
    quote_sender: str = ""
    quote_msg_id: str = ""
    images: tuple[MediaRef, ...] = ()
    faces: tuple[FaceRef, ...] = ()
    has_image: bool = False
    has_sticker: bool = False
    bare_face: bool = False
    passthrough: bool = False
    is_poke: bool = False
    persisted: bool = False
    db_seq: int = 0


@dataclass
class Batch:
    group_id: str
    items: list[ChatEvent] = field(default_factory=list)
    started: float = 0.0
    deadline: float = 0.0
    images: list[MediaRef] = field(default_factory=list)
    approved: bool = False
    turn_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    persisted: bool = False

    @property
    def is_poke(self) -> bool:
        return bool(self.items) and all(i.is_poke for i in self.items)

    @property
    def mentioned(self) -> bool:
        return any(i.mentioned for i in self.items)

    @property
    def retold(self) -> bool:
        # 仅当所有提及消息都是转述、且无直接非转述提及时才判为纯转述
        mentioned_items = [i for i in self.items if i.mentioned]
        if not mentioned_items:
            return any(i.retold for i in self.items)
        return all(i.retold for i in mentioned_items)

    @property
    def at_others(self) -> bool:
        return (not self.mentioned) and any(i.at_others for i in self.items)

    @property
    def umo(self) -> str:
        return self.items[-1].umo if self.items else ""

    @property
    def anchor(self) -> Optional[ChatEvent]:
        if not self.items:
            return None
        mentioned_items = [i for i in self.items if i.mentioned]
        return mentioned_items[-1] if mentioned_items else self.items[-1]

    @property
    def text(self) -> str:
        return "\n".join(
            format_speaker_message(i.user_name, i.text)
            for i in self.items
            if str(i.text or "").strip()
        )

    @property
    def body_text(self) -> str:
        return "\n".join(
            str(i.text or "").strip()
            for i in self.items
            if str(i.text or "").strip()
        )

    @property
    def last_user(self) -> str:
        return self.items[-1].user_id if self.items else ""

    @property
    def has_image(self) -> bool:
        return bool(self.images) or any(i.has_image for i in self.items)

    @property
    def quote_text(self) -> str:
        anc = self.anchor
        if anc and anc.quote_text:
            return anc.quote_text
        for item in reversed(self.items):
            if item.quote_text:
                return item.quote_text
        return ""

    @property
    def target_msg_id(self) -> str:
        anc = self.anchor
        if anc and anc.msg_id:
            return anc.msg_id
        for item in reversed(self.items):
            if item.msg_id:
                return item.msg_id
        return ""


@dataclass(frozen=True)
class ImagePart:
    data: bytes = b""
    url: str = ""
    mime: str = "image/jpeg"


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]


@dataclass(frozen=True)
class LLMMessage:
    role: str  # system / user / assistant / tool
    content: str = ""
    name: str = ""
    tool_call_id: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    images: tuple[ImagePart, ...] = ()


@dataclass(frozen=True)
class ModelTurn:
    text: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    finish_reason: str = "stop"
    usage_tokens: int = 0


@dataclass(frozen=True)
class OutboundAction:
    group_id: str
    umo: str


@dataclass(frozen=True)
class SendText(OutboundAction):
    text: str = ""
    reply_to_msg_id: str = ""


@dataclass(frozen=True)
class SendEmoji(OutboundAction):
    path: str = ""
    summary: str = "[动画表情]"


@dataclass(frozen=True)
class SendFace(OutboundAction):
    face_id: str = ""


@dataclass(frozen=True)
class SendFile(OutboundAction):
    path: str = ""
    name: str = ""


@dataclass(frozen=True)
class ReactEmoji(OutboundAction):
    msg_id: str = ""
    emoji_id: str = "124"


@dataclass(frozen=True)
class TaskRequest:
    group_id: str
    umo: str
    prompt: str
    images: tuple[bytes, ...] = ()
    pre_history: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class TaskStatusInfo:
    task_id: str
    group_id: str
    prompt: str
    elapsed: float
    progress: str = ""


@dataclass(frozen=True)
class TaskResult:
    status: str  # ok / timeout / cancelled / error / empty
    text: str = ""
    partial: str = ""
    files: tuple[dict[str, Any], ...] = ()
    elapsed: float = 0.0
    timeout: int = 300
    error: str = ""


@dataclass(frozen=True)
class PendingDelivery:
    task_id: str
    group_id: str
    umo: str
    prompt: str
    status: str
    reason: str
    result: str = ""
    partial: str = ""
    files: tuple[dict[str, Any], ...] = ()
    error: str = ""
    pre_history: tuple[dict[str, Any], ...] = ()
    created_at: float = 0.0


@dataclass(frozen=True)
class SearchHit:
    title: str
    snippet: str
    url: str = ""


@runtime_checkable
class LLMClient(Protocol):
    def chat(
        self,
        messages: Sequence[LLMMessage],
        tools: Sequence[ToolSpec] = (),
        timeout: Optional[int] = None,
    ) -> ModelTurn: ...

    def describe_image(
        self,
        image_bytes: bytes,
        prompt: str,
        timeout: Optional[int] = None,
    ) -> str: ...

    def extra_secrets(self) -> tuple[str, ...]: ...


@runtime_checkable
class SearchProvider(Protocol):
    def available(self) -> bool: ...

    def search(self, query: str, limit: int = 5) -> list[SearchHit]: ...


@runtime_checkable
class EmojiStorePort(Protocol):
    @property
    def root(self) -> Path: ...

    def collect(self, group_id: str, image_bytes: bytes, llm: LLMClient) -> str: ...

    def collect_face(self, group_id: str, face_id: str, name: str = "") -> str: ...

    def remove_face(self, group_id: str, face_id: str) -> bool: ...

    def tags(self, group_id: str, limit: int = 12) -> list[str]: ...

    def pick(self, group_id: str, keyword: str = "") -> Optional[str | Path]: ...

    def pick_entry(self, group_id: str, keyword: str = "") -> Optional[PickedEmoji]: ...

    def has_entries(self, group_id: str) -> bool: ...

    def cleanup(self, max_age_days: Optional[int] = None, now: Optional[float] = None) -> None: ...

    def close(self) -> None: ...


@runtime_checkable
class MemoryRepository(Protocol):
    def close(self) -> None: ...

    def add_message(
        self,
        group_id: str,
        user_id: str,
        user_name: str,
        role: str,
        text: str,
        ts: Optional[float] = None,
        msg_id: str = "",
        meta: str = "",
    ) -> int: ...

    def recent_messages(self, group_id: str, limit: int = 30) -> list[dict[str, Any]]: ...

    def messages_since(self, group_id: str, ts: float = 0.0, limit: int = 24) -> list[dict[str, Any]]: ...

    def prune_messages(self, keep_per_group: int = 500) -> int: ...

    def last_episode_at(self, group_id: str) -> float: ...

    def add_episode(
        self,
        group_id: str,
        text: str,
        now: Optional[float] = None,
        source: str = "",
        external_id: str = "",
    ) -> None: ...

    def recent_episodes(self, group_id: str, limit: int = 3) -> list[str]: ...

    def add_memory(self, group_id: str, category: str, content: str, now: Optional[float] = None) -> None: ...

    def recall_memories(
        self,
        group_id: str,
        query: str,
        limit: int = 6,
        now: Optional[float] = None,
    ) -> list[str]: ...

    def group_jargon(self, group_id: str, limit: int = 12) -> dict[str, str]: ...

    def observe_person(
        self,
        group_id: str,
        user_id: str,
        display_name: str,
        now: Optional[float] = None,
    ) -> None: ...

    def upsert_person(
        self,
        group_id: str,
        user_id: str,
        now: Optional[float] = None,
        merge: bool = True,
        **fields: Any,
    ) -> dict[str, Any]: ...

    def resolve_person(self, group_id: str, query_name_or_id: str) -> Optional[str]: ...

    def person_summary(self, group_id: str, user_id: str) -> str: ...

    def person_aliases(self, group_id: str, user_id: str) -> list[str]: ...

    def person_facts(self, group_id: str, user_id: str, field: Optional[str] = None) -> list[dict[str, Any]]: ...

    def find_mentioned_persons(
        self,
        group_id: str,
        text: str,
        *,
        exclude_uids: Sequence[str] = (),
        limit: int = 2,
    ) -> list[tuple[str, str]]: ...

    def get_conv_state(
        self,
        group_id: str,
        max_age_seconds: float = 7200.0,
        now: Optional[float] = None,
    ) -> dict[str, list[str]]: ...

    def save_conv_state(
        self,
        group_id: str,
        state: dict[str, list[str]],
        now: Optional[float] = None,
    ) -> None: ...

    def save_pending_delivery(self, delivery: PendingDelivery) -> bool: ...

    def list_pending_deliveries(self) -> list[PendingDelivery]: ...

    def delete_pending_delivery(self, task_id: str) -> bool: ...

    def pending_delivery_paths(self) -> set[str]: ...

    def record_event(
        self,
        group_id: str,
        kind: str,
        ok: bool = True,
        latency: float = 0.0,
        tokens: int = 0,
        detail: str = "",
        now: Optional[float] = None,
    ) -> bool: ...

    def recent_error_count(self, group_id: str, window_seconds: float = 120.0, now: Optional[float] = None) -> int: ...

    def monitor_stats(self, group_id: Optional[str] = None, hours: float = 24, now: Optional[float] = None) -> dict[str, Any]: ...

    def prune_monitor(self, max_age_days: int = 30, now: Optional[float] = None) -> int: ...


@runtime_checkable
class HarnessBackend(Protocol):
    @property
    def name(self) -> str: ...

    def available(self) -> bool: ...

    def prepare_home(self, home_dir: Path) -> None: ...

    def build_command(
        self,
        prompt: str,
        workdir: Path,
        outbox_dir: Path,
        inbox_files: Sequence[Path],
        timeout: int,
    ) -> list[str]: ...

    def env_overrides(self, task_dir: Path, home_dir: Path) -> dict[str, str]: ...

    def parse_line(self, line: str) -> Optional[str]: ...

    def parse_output(self, raw_stdout: str, limit: int = 1200) -> str: ...


@runtime_checkable
class TaskRunner(Protocol):
    @property
    def outbox_root(self) -> str: ...

    @property
    def timeout(self) -> int: ...

    def available(self) -> bool: ...

    def running_for_group(self, group_id: str) -> Optional[TaskStatusInfo]: ...

    def submit(self, req: TaskRequest, ack_text: str = "") -> Optional[dict[str, Any]]: ...

    def status(self, group_id: str) -> Optional[TaskStatusInfo]: ...

    def stop(self, group_id: str) -> bool: ...

    def poll_deliveries(self) -> list[PendingDelivery]: ...

    def present_delivery(self, delivery: PendingDelivery, llm: LLMClient) -> dict[str, Any]: ...

    def ack_delivery(self, task_id: str) -> None: ...

    def release_delivery(self, task_id: str) -> None: ...

    def shutdown(self, timeout: float = 3.0) -> None: ...


@runtime_checkable
class MessageSender(Protocol):
    async def send(self, action: OutboundAction) -> bool: ...

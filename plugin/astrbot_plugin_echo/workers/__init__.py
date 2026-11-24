"""可插拔后台沙箱执行层：通用安全沙箱 + 可替换后端（Codex / 通用 ChatAgent）+ 任务编排管理器。"""

from .backends import ChatAgentBackend, CodexBackend, select_backend, uses_codex_model
from .manager import TaskManager
from .sandbox import (
    DEFAULT_TIMEOUT,
    MAX_FILE_BYTES,
    MAX_OUTPUT,
    MAX_OUTPUT_FILES,
    MAX_TIMEOUT,
    MIN_TIMEOUT,
    OUTBOX_TTL_SECONDS,
    SandboxRunner,
    atomic_write_bytes,
    atomic_write_text_no_follow,
    cleanup_file_entry,
    cleanup_outbox,
    contains_secret_bytes,
    is_safe_export_name,
    parse_timeout,
    sanitize_export_name,
)

__all__ = [
    "ChatAgentBackend",
    "CodexBackend",
    "DEFAULT_TIMEOUT",
    "MAX_FILE_BYTES",
    "MAX_OUTPUT",
    "MAX_OUTPUT_FILES",
    "MAX_TIMEOUT",
    "MIN_TIMEOUT",
    "OUTBOX_TTL_SECONDS",
    "SandboxRunner",
    "TaskManager",
    "atomic_write_bytes",
    "atomic_write_text_no_follow",
    "cleanup_file_entry",
    "cleanup_outbox",
    "contains_secret_bytes",
    "is_safe_export_name",
    "parse_timeout",
    "sanitize_export_name",
    "select_backend",
    "uses_codex_model",
]

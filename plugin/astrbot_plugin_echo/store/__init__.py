"""持久化存储子包：统一 SQLite 仓库（含无损迁移）与表情包仓库。"""

from .cache_cleanup import cleanup_runtime_caches
from .emoji_classifier import acceptable, analyze_emoji, is_sticker_geometry
from .emoji_store import EmojiStore
from .memory_store import query_terms
from .migration import migrate_legacy_v1_dbs
from .repository import Repository

__all__ = [
    "EmojiStore",
    "Repository",
    "acceptable",
    "analyze_emoji",
    "cleanup_runtime_caches",
    "is_sticker_geometry",
    "migrate_legacy_v1_dbs",
    "query_terms",
]

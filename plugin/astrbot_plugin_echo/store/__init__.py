"""持久化存储子包：统一 SQLite 仓库（含无损迁移）与表情包仓库。"""

from .emoji_store import EmojiStore, acceptable, analyze_emoji
from .migration import migrate_legacy_v1_dbs
from .repository import Repository, cleanup_runtime_caches, query_terms

__all__ = [
    "EmojiStore",
    "Repository",
    "acceptable",
    "analyze_emoji",
    "cleanup_runtime_caches",
    "migrate_legacy_v1_dbs",
    "query_terms",
]

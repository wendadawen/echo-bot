"""符号链接安全的 AstrBot / NapCat 临时文件与日志缓存清理。"""

from __future__ import annotations

import fnmatch
import time
from pathlib import Path
from typing import Optional


def _has_symlink_component(start: Path, end: Path) -> bool:
    cur = end
    while True:
        if cur.is_symlink():
            return True
        if cur == start:
            return False
        parent = cur.parent
        if parent == cur:
            return False
        cur = parent


def _purge_old_files(
    target_dir: Path,
    root_dir: Path,
    resolved_root: Path,
    cutoff: float,
    pattern: str = "*",
    recursive: bool = False,
) -> int:
    if not target_dir.exists() or target_dir.is_symlink() or not target_dir.is_dir():
        return 0
    if _has_symlink_component(root_dir, target_dir):
        return 0
    try:
        resolved_dir = target_dir.resolve()
    except OSError:
        return 0
    if resolved_dir != resolved_root and resolved_root not in resolved_dir.parents:
        return 0
    removed = 0
    stack = [target_dir]
    while stack:
        cur = stack.pop()
        try:
            entries = list(cur.iterdir())
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_symlink():
                    continue
                if cur == target_dir and pattern != "*" and not fnmatch.fnmatch(entry.name, pattern):
                    continue
                if entry.is_dir():
                    if recursive:
                        stack.append(entry)
                    continue
                if not entry.is_file():
                    continue
                resolved_entry = entry.resolve()
                if resolved_root not in resolved_entry.parents:
                    continue
                if entry.lstat().st_mtime <= cutoff:
                    entry.unlink(missing_ok=True)
                    removed += 1
            except OSError:
                continue
    return removed


def cleanup_runtime_caches(
    data_root: str | Path,
    now: Optional[float] = None,
    temp_max_age: int = 86400,
    media_max_age: int = 259200,
) -> int:
    """定期清理 AstrBot 临时图片及 QQ 日志/媒体缓存，严禁触碰登录态与数据库。"""
    if not data_root:
        return 0
    root = Path(data_root)
    if root.is_symlink() or not root.is_dir():
        return 0
    try:
        resolved_root = root.resolve()
    except OSError:
        return 0

    now_ts = time.time() if now is None else float(now)
    temp_cutoff = now_ts - max(0, int(temp_max_age))
    media_cutoff = now_ts - max(0, int(media_max_age))
    removed = 0

    removed += _purge_old_files(
        root / "temp", root, resolved_root, temp_cutoff, pattern="media_*", recursive=False
    )

    napcat_cutoff = now_ts - 7 * 86400
    for sub in ("logs", "cache"):
        removed += _purge_old_files(
            root / "napcat" / sub, root, resolved_root, napcat_cutoff, recursive=True
        )

    qq_dir = root / "napcat" / "home" / ".config" / "QQ"
    if qq_dir.is_dir() and not qq_dir.is_symlink() and not _has_symlink_component(root, qq_dir):
        removed += _purge_old_files(
            qq_dir / "global" / "nt_data" / "Log", root, resolved_root, temp_cutoff, recursive=True
        )
        removed += _purge_old_files(
            qq_dir / "NapCat" / "temp", root, resolved_root, temp_cutoff, recursive=True
        )
        try:
            acc_dirs = list(qq_dir.glob("nt_qq_*"))
        except OSError:
            acc_dirs = []
        for acc in acc_dirs:
            if not acc.is_dir() or acc.is_symlink():
                continue
            removed += _purge_old_files(
                acc / "nt_temp", root, resolved_root, temp_cutoff, recursive=True
            )
            nt_data = acc / "nt_data"
            if not nt_data.is_dir() or nt_data.is_symlink():
                continue
            for sub in ("log", "log-cache", "Pic", "Video", "File", "Ptt"):
                removed += _purge_old_files(
                    nt_data / sub, root, resolved_root, media_cutoff, recursive=True
                )
    return removed

"""沙箱文件系统防线与导出安全守卫：无符号链接原子写、后缀/密文校验、超限裁剪与过期产物清理。"""

from __future__ import annotations

import os
import re
import shutil
import stat
import time
import uuid
from pathlib import Path
from typing import Optional, Sequence

NOBODY_UID = 65534
NOBODY_GID = 65534

MAX_OUTPUT = 1200
DEFAULT_TIMEOUT = 300
MIN_TIMEOUT = 180
MAX_TIMEOUT = 600

MAX_OUTPUT_FILES = 3
MAX_FILE_BYTES = 20 * 1024 * 1024
OUTBOX_TTL_SECONDS = 3600

SAFE_EXPORT_SUFFIXES = frozenset({
    ".md", ".txt", ".html", ".htm", ".csv", ".tsv",
    ".py", ".js", ".ts", ".css", ".svg",
    ".png", ".jpg", ".jpeg", ".webp", ".gif",
    ".pdf", ".docx", ".xlsx", ".pptx", ".zip",
})
_UNSAFE_SUFFIXES = (
    ".env", ".pem", ".key", ".p12", ".pfx", ".crt", ".sqlite", ".sqlite3", ".db",
    ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".sh",
)
_UNSAFE_EXACT = {"config.toml", "cmd_config.json", "id_rsa", "id_ed25519", "onebot_token"}
_UNSAFE_WORDS = ("secret", "password", "passwd", "credential", "private_key", "token")
_BIDI_AND_CTRL = re.compile(r"[\x00-\x1f\x7f\u200e\u200f\u202a-\u202e\u2066-\u2069/\\]+")
_SECRET_CONTENT_PATTERNS = (
    re.compile(rb"-----BEGIN\s+[A-Z0-9 ]*PRIVATE KEY-----"),
    re.compile(rb"(?:ws_reverse_token|napcat_webui_token|onebot_token)\s*[\"':=]", re.I),
    re.compile(rb"[\"']?(?:api_key|openai_api_key|echo_api_key)[\"']?\s*[:=]\s*[\"'][^\"']{8,}", re.I),
    re.compile(rb"(?<![A-Za-z0-9_-])sk-[A-Za-z0-9_-]{16,}(?![A-Za-z0-9_-])"),
)


def parse_timeout(value, default: int = DEFAULT_TIMEOUT) -> int:
    if value is None or isinstance(value, bool):
        return default
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(MIN_TIMEOUT, min(MAX_TIMEOUT, parsed))


def sanitize_export_name(name: str) -> str:
    raw = str(name or "").replace("\\", "/")
    base = Path(raw).name
    clean = _BIDI_AND_CTRL.sub("", base).strip()
    if len(clean) > 100:
        stem, suffix = Path(clean).stem, Path(clean).suffix
        clean = (stem[:100 - len(suffix)] + suffix) if (suffix and len(suffix) < 20) else clean[:100]
    return clean


def is_safe_export_name(name: str) -> bool:
    clean = sanitize_export_name(name)
    if not clean or clean in (".", "..") or clean.startswith("."):
        return False
    lower = clean.lower()
    if lower in _UNSAFE_EXACT:
        return False
    if any(lower.endswith(sfx) for sfx in _UNSAFE_SUFFIXES):
        return False
    if any(word in lower for word in _UNSAFE_WORDS):
        return False
    return Path(lower).suffix in SAFE_EXPORT_SUFFIXES


def contains_secret_bytes(data: bytes, extra_secrets: Sequence[str] = ()) -> bool:
    if not data:
        return False
    if data.startswith(b"SQLite format 3"):
        return True
    sample = data[:min(len(data), 1024 * 1024)]
    for pattern in _SECRET_CONTENT_PATTERNS:
        if pattern.search(sample):
            return True
    for secret in extra_secrets or ():
        s = str(secret or "").strip()
        if len(s) >= 8:
            enc = s.encode("utf-8", errors="ignore")
            if enc and enc in data:
                return True
    return False


def atomic_write_text_no_follow(path: str | Path, text: str, mode: int = 0o644) -> None:
    target = Path(path)
    if target.is_symlink():
        raise OSError("refuse to overwrite symlink: {}".format(target))
    parent = target.parent
    if parent.is_symlink():
        raise OSError("refuse to write under symlink dir: {}".format(parent))
    tmp_path = parent / ".{}.{}.tmp".format(target.name, uuid.uuid4().hex[:8])
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(str(tmp_path), flags, mode)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        try:
            os.chmod(str(tmp_path), mode)
        except OSError:
            pass
        os.replace(str(tmp_path), str(target))
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def atomic_write_bytes(path: str | Path, data: bytes, mode: int = 0o600) -> None:
    target = Path(path)
    if target.is_symlink():
        raise OSError("refuse to overwrite symlink: {}".format(target))
    parent = target.parent
    if parent.is_symlink():
        raise OSError("refuse to write under symlink dir: {}".format(parent))
    tmp_path = parent / ".{}.{}.tmp".format(target.name, uuid.uuid4().hex[:8])
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(str(tmp_path), flags, mode)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        try:
            os.chmod(str(tmp_path), mode)
        except OSError:
            pass
        os.replace(str(tmp_path), str(target))
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def cleanup_outbox(
    outbox_root: str | Path,
    max_age: int = OUTBOX_TTL_SECONDS,
    keep_paths: Optional[set[str]] = None,
) -> None:
    if not outbox_root:
        return
    root = Path(outbox_root)
    if root.is_symlink() or not root.is_dir():
        return
    protected_dirs = set()
    for p in keep_paths or ():
        if not p:
            continue
        try:
            protected_dirs.add(str(Path(p).resolve(strict=False).parent))
        except OSError:
            continue
    cutoff = time.time() - max(0, int(max_age))
    for entry in list(root.iterdir()):
        try:
            if not entry.is_symlink() and str(entry.resolve(strict=False)) in protected_dirs:
                continue
            mtime = entry.lstat().st_mtime
        except OSError:
            continue
        if mtime <= cutoff:
            try:
                if entry.is_dir() and not entry.is_symlink():
                    shutil.rmtree(entry, ignore_errors=True)
                else:
                    entry.unlink(missing_ok=True)
            except OSError:
                pass


def cleanup_file_entry(path: str | Path, outbox_root: str | Path) -> bool:
    if not path or not outbox_root:
        return False
    try:
        root = Path(outbox_root).resolve(strict=False)
        target = Path(path).resolve(strict=False)
    except OSError:
        return False
    if target == root or root not in target.parents:
        return False
    try:
        target.unlink(missing_ok=True)
    except OSError:
        return False
    parent = target.parent
    if parent != root and parent.is_dir():
        try:
            if not any(parent.iterdir()):
                parent.rmdir()
        except OSError:
            pass
    return True


def safe_rel_path(root: Path, raw_path: str, *, for_write: bool = False) -> Path:
    """校验工作区相对路径：拦截空路径、绝对路径、..、保留目录与符号链接穿越。"""
    clean = str(raw_path or "").strip()
    if not clean:
        raise ValueError("空路径")
    p = Path(clean)
    if p.is_absolute():
        raise ValueError("禁止使用绝对路径")
    if any(part == ".." for part in p.parts):
        raise ValueError("禁止路径穿越")
    if p.parts and (p.parts[0].startswith(".chat_agent") or (for_write and p.parts[0] == "inbox")):
        raise ValueError("禁止访问保留目录")
    root_resolved = root.resolve(strict=False)
    cur = root_resolved
    for part in p.parts:
        cur = cur / part
        if cur.is_symlink():
            raise ValueError("禁止访问符号链接")
    resolved = (root_resolved / p).resolve(strict=False)
    if resolved != root_resolved and root_resolved not in resolved.parents:
        raise ValueError("路径越界")
    return resolved


def read_workspace_file(root: Path, raw_path: str, max_chars: int = 12000) -> str:
    try:
        target = safe_rel_path(root, raw_path, for_write=False)
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(str(target), flags)
        with os.fdopen(fd, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read(max_chars)
    except Exception as exc:
        return "读取失败：{}".format(exc)


def write_workspace_file(root: Path, raw_path: str, content: str) -> str:
    try:
        target = safe_rel_path(root, raw_path, for_write=True)
        root_resolved = root.resolve(strict=False)
        rel = target.relative_to(root_resolved)
        cur = root_resolved
        for part in rel.parts[:-1]:
            cur = cur / part
            if cur.is_symlink():
                raise ValueError("禁止访问符号链接")
            if not cur.exists():
                cur.mkdir(exist_ok=True)
                try:
                    os.chown(cur, NOBODY_UID, NOBODY_GID)
                    os.chmod(cur, 0o755)
                except OSError:
                    pass
        atomic_write_text_no_follow(target, str(content or ""), mode=0o644)
        try:
            os.chown(target, NOBODY_UID, NOBODY_GID)
        except OSError:
            pass
        return "已写入 {}".format(rel.as_posix())
    except Exception as exc:
        return "写入失败：{}".format(exc)


def collect_outbox(
    outbox_root: str | Path,
    outbox_dir: Path,
    task_id: str,
    expected_task_dir: Path,
    extra_secrets: Sequence[str] = (),
) -> tuple[dict[str, object], ...]:
    """用 O_NOFOLLOW + fstat + 后缀白名单 + 密钥扫描，从任务 outbox 安全收取导出产物。"""
    if not outbox_root or outbox_dir.is_symlink() or not outbox_dir.is_dir():
        return ()
    try:
        resolved_outbox = outbox_dir.resolve(strict=True)
        expected = expected_task_dir.resolve(strict=True) / "outbox"
        if resolved_outbox != expected:
            return ()
    except OSError:
        return ()

    dir_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        dir_fd = os.open(str(resolved_outbox), dir_flags)
    except OSError:
        return ()

    candidates = []
    file_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        try:
            names = os.listdir(dir_fd)
        except OSError:
            names = []
        for name in names:
            if not is_safe_export_name(name):
                continue
            safe_name = sanitize_export_name(name)
            if not safe_name:
                continue
            fd = -1
            try:
                fd = os.open(name, file_flags, dir_fd=dir_fd)
                st = os.fstat(fd)
                if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
                    continue
                if st.st_size <= 0 or st.st_size > MAX_FILE_BYTES:
                    continue
                with os.fdopen(fd, "rb", closefd=True) as fh:
                    fd = -1
                    data = fh.read(MAX_FILE_BYTES + 1)
            except OSError:
                continue
            finally:
                if fd != -1:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
            if not data or len(data) > MAX_FILE_BYTES:
                continue
            if contains_secret_bytes(data, extra_secrets=extra_secrets):
                continue
            candidates.append((st.st_mtime, safe_name, data, len(data)))
    finally:
        try:
            os.close(dir_fd)
        except OSError:
            pass

    if not candidates:
        return ()

    candidates.sort(key=lambda row: (-row[0], row[1]))
    out_root = Path(outbox_root)
    dest_dir = out_root / task_id
    try:
        out_root.mkdir(parents=True, exist_ok=True)
        os.chmod(out_root, 0o700)
        dest_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(dest_dir, 0o700)
    except OSError:
        return ()

    collected = []
    used_names: set[str] = set()
    for _, safe_name, data, size in candidates[:MAX_OUTPUT_FILES]:
        final_name = safe_name
        if final_name in used_names:
            stem, suffix = Path(safe_name).stem, Path(safe_name).suffix
            idx = 2
            while "{}_{}{}".format(stem, idx, suffix) in used_names:
                idx += 1
            final_name = "{}_{}{}".format(stem, idx, suffix)
        used_names.add(final_name)
        dest_file = dest_dir / final_name
        try:
            atomic_write_bytes(dest_file, data, mode=0o600)
        except OSError:
            continue
        collected.append({"name": final_name, "path": str(dest_file), "size": size})
    return tuple(collected)

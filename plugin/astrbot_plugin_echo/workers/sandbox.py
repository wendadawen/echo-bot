"""通用安全沙箱执行底座：与具体 CLI 后端解耦。

保留全部容器边界与安全防线：
- 每任务独立临时工作区（含只读输入 inbox/ 与受控导出 outbox/）与独立 HOME；
- Linux root 下通过 setpriv --reuid=65534 --regid=65534 --no-new-privs 降权到 nobody；
- 最小环境变量白名单（剥离空白代理变量，严禁透传 ONEBOT_TOKEN 等宿主机密钥）；
- 原子写（O_NOFOLLOW）配置与导出文件，拒收软硬链接、越界路径、非白名单后缀、数据库或含密钥文件；
- 支持超时强杀与 cancel_event 实时喊停（先 killpg 再 wait，绝不在管道未关闭时死锁），
  同时支持流式汇报最新进展。
"""

from __future__ import annotations

import collections
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Callable, Optional, Sequence

from ..domain import HarnessBackend, TaskRequest, TaskResult, detect_image_format

MAX_OUTPUT = 1200
DEFAULT_TIMEOUT = 300
MIN_TIMEOUT = 180
MAX_TIMEOUT = 600
MAX_STDOUT_BYTES = 4 * 1024 * 1024
MAX_STDERR_BYTES = 8192

NOBODY_UID = 65534
NOBODY_GID = 65534
SETPRIV_BIN = "/usr/bin/setpriv"
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
_SAFE_ENV_KEYS = (
    "PATH", "LANG", "LC_ALL", "TZ",
    "SSL_CERT_FILE", "SSL_CERT_DIR",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
    "http_proxy", "https_proxy", "no_proxy",
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
    tmp_path = target.parent / ".{}.{}.tmp".format(target.name, uuid.uuid4().hex[:8])
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


class SandboxRunner:
    """通用隔离沙箱：配合任意 HarnessBackend 在独立目录内降权执行任务。"""

    def __init__(
        self,
        workdir_root: str | Path,
        outbox_root: str | Path = "",
        timeout: Any = DEFAULT_TIMEOUT,
    ):
        self.workdir_root = str(workdir_root or "")
        if outbox_root:
            self.outbox_root = str(outbox_root)
        elif self.workdir_root:
            self.outbox_root = str(Path(self.workdir_root).parent / "outbox")
        else:
            self.outbox_root = ""
        self.timeout = parse_timeout(timeout)

    @staticmethod
    def use_setpriv() -> bool:
        return (
            sys.platform.startswith("linux")
            and hasattr(os, "geteuid")
            and os.geteuid() == 0
        )

    def available(self, backend: HarnessBackend) -> bool:
        if not backend.available():
            return False
        if self.use_setpriv() and not os.path.exists(SETPRIV_BIN):
            return False
        return bool(self.workdir_root)

    def _prepare_roots(self) -> Path:
        work_path = Path(self.workdir_root)
        if work_path.is_symlink():
            raise OSError("workdir_root cannot be a symlink")
        work_path.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(work_path, 0o711)
        except OSError:
            pass
        if self.outbox_root:
            out_path = Path(self.outbox_root)
            if not out_path.is_symlink():
                try:
                    out_path.mkdir(parents=True, exist_ok=True)
                    os.chmod(out_path, 0o700)
                except OSError:
                    pass
        return work_path

    def _wrap_command(self, exec_args: list[str]) -> list[str]:
        if self.use_setpriv():
            return [
                SETPRIV_BIN,
                "--reuid={}".format(NOBODY_UID),
                "--regid={}".format(NOBODY_GID),
                "--clear-groups",
                "--no-new-privs",
                *exec_args,
            ]
        return exec_args

    @staticmethod
    def _build_env(task_dir: Path, home_dir: Path, extra_env: dict[str, str]) -> dict[str, str]:
        env: dict[str, str] = {}
        for key in _SAFE_ENV_KEYS:
            val = os.environ.get(key)
            if val is None:
                continue
            if "proxy" in key.lower() and not str(val).strip():
                continue
            env[key] = val
        env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
        env["HOME"] = str(task_dir)
        env["TMPDIR"] = str(task_dir)
        env["TMP"] = str(task_dir)
        env["TEMP"] = str(task_dir)
        for k, v in (extra_env or {}).items():
            if v is not None:
                env[str(k)] = str(v)
        return env

    def _collect_outbox(
        self,
        outbox_dir: Path,
        task_id: str,
        expected_task_dir: Path,
        extra_secrets: Sequence[str] = (),
    ) -> tuple[dict[str, object], ...]:
        if not self.outbox_root or outbox_dir.is_symlink() or not outbox_dir.is_dir():
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
        out_root = Path(self.outbox_root)
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

    def run(
        self,
        req: TaskRequest,
        backend: HarnessBackend,
        cancel_event: Optional[threading.Event] = None,
        on_progress: Optional[Callable[[str], None]] = None,
        extra_secrets: Sequence[str] = (),
    ) -> TaskResult:
        prompt = str(req.prompt or "").strip()
        if not prompt or not self.available(backend):
            return TaskResult(status="empty", timeout=self.timeout)
        try:
            workdir_base = self._prepare_roots()
        except OSError as exc:
            return TaskResult(status="error", timeout=self.timeout, error=str(exc))

        task_id = "task-" + uuid.uuid4().hex[:10]
        task_dir = workdir_base / task_id
        outbox_dir = task_dir / "outbox"
        inbox_dir = task_dir / "inbox"
        home_dir = task_dir / ".{}".format(backend.name or "agent")
        inbox_files: list[Path] = []
        try:
            task_dir.mkdir(parents=True, exist_ok=True)
            outbox_dir.mkdir(parents=True, exist_ok=True)
            home_dir.mkdir(parents=True, exist_ok=True)
            backend.prepare_home(home_dir)
            if req.images:
                inbox_dir.mkdir(parents=True, exist_ok=True)
                for idx, img_bytes in enumerate(list(req.images)[:2], 1):
                    if not img_bytes:
                        continue
                    _, ext = detect_image_format(img_bytes)
                    fpath = inbox_dir / "input_{}{}".format(idx, ext)
                    atomic_write_bytes(fpath, img_bytes, mode=0o644)
                    inbox_files.append(fpath)
            for path, mode in (
                (task_dir, 0o711),
                (outbox_dir, 0o700),
                (home_dir, 0o755),
                (inbox_dir, 0o755),
            ):
                if path.exists() and not path.is_symlink():
                    try:
                        if path in (task_dir, outbox_dir, home_dir):
                            os.chown(path, NOBODY_UID, NOBODY_GID)
                        os.chmod(path, mode)
                    except OSError:
                        pass
        except OSError as exc:
            shutil.rmtree(task_dir, ignore_errors=True)
            return TaskResult(status="error", timeout=self.timeout, error=str(exc))

        extra_env = backend.env_overrides(task_dir, home_dir)
        env = self._build_env(task_dir, home_dir, extra_env)
        exec_args = backend.build_command(prompt, task_dir, outbox_dir, inbox_files, self.timeout)
        cmd = self._wrap_command(exec_args)

        started = time.monotonic()
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=str(task_dir),
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
        except OSError as exc:
            shutil.rmtree(task_dir, ignore_errors=True)
            return TaskResult(status="error", timeout=self.timeout, error=str(exc))

        proc_pid = getattr(proc, "pid", None)
        timed_out = False
        cancelled = False
        stdout = ""
        stderr_tail = ""
        collected: tuple[dict[str, object], ...] = ()
        proc_stdout = getattr(proc, "stdout", None)
        proc_stderr = getattr(proc, "stderr", None)

        def _kill_process_group() -> None:
            if isinstance(proc_pid, int) and proc_pid > 0:
                try:
                    os.killpg(proc_pid, signal.SIGKILL)
                    return
                except (ProcessLookupError, PermissionError, OSError):
                    pass
            kill_fn = getattr(proc, "kill", None)
            if callable(kill_fn):
                try:
                    kill_fn()
                except OSError:
                    pass

        done_event = threading.Event()
        cancel_triggered = [False]
        watcher: Optional[threading.Thread] = None
        if cancel_event is not None:
            if cancel_event.is_set():
                cancel_triggered[0] = True
                _kill_process_group()
            else:
                def _watch_cancel() -> None:
                    while not done_event.is_set():
                        if cancel_event.wait(timeout=0.05):
                            if not done_event.is_set():
                                cancel_triggered[0] = True
                                _kill_process_group()
                            return

                watcher = threading.Thread(target=_watch_cancel, name="echo-sandbox-cancel", daemon=True)
                watcher.start()

        try:
            if proc_stdout is not None:
                data_lock = threading.Lock()
                kept_lines: list[str] = []
                tail_important: collections.deque[str] = collections.deque(maxlen=32)
                total_bytes = 0
                stderr_lines: collections.deque[str] = collections.deque(maxlen=64)
                stderr_bytes = 0

                def _drain() -> None:
                    nonlocal total_bytes
                    try:
                        for line in proc_stdout:
                            line_len = len(line.encode("utf-8", errors="ignore"))
                            step = backend.parse_line(line)
                            if step and on_progress is not None:
                                try:
                                    on_progress(step)
                                except Exception:
                                    pass
                            with data_lock:
                                if '"agent_message"' in line:
                                    tail_important.append(line)
                                if total_bytes < MAX_STDOUT_BYTES:
                                    kept_lines.append(line)
                                    total_bytes += line_len
                    except Exception:
                        pass

                def _drain_stderr() -> None:
                    nonlocal stderr_bytes
                    try:
                        for line in proc_stderr:
                            line_len = len(line.encode("utf-8", errors="ignore"))
                            with data_lock:
                                stderr_lines.append(line[-400:] if len(line) > 400 else line)
                                stderr_bytes += line_len
                                while stderr_bytes > MAX_STDERR_BYTES and len(stderr_lines) > 1:
                                    dropped = stderr_lines.popleft()
                                    stderr_bytes -= len(dropped.encode("utf-8", errors="ignore"))
                    except Exception:
                        pass

                reader = threading.Thread(target=_drain, name="echo-sandbox-stdout", daemon=True)
                reader.start()
                stderr_reader: Optional[threading.Thread] = None
                if proc_stderr is not None:
                    stderr_reader = threading.Thread(target=_drain_stderr, name="echo-sandbox-stderr", daemon=True)
                    stderr_reader.start()
                try:
                    proc.wait(timeout=self.timeout)
                except subprocess.TimeoutExpired:
                    if not cancel_triggered[0]:
                        timed_out = True
                    _kill_process_group()
                    try:
                        proc.wait(timeout=5)
                    except (subprocess.TimeoutExpired, OSError):
                        pass
                finally:
                    done_event.set()
                    if watcher is not None:
                        watcher.join(timeout=0.5)
                    if getattr(proc, "returncode", None) is None:
                        _kill_process_group()
                    reader.join(timeout=2)
                    if not reader.is_alive():
                        try:
                            proc_stdout.close()
                        except OSError:
                            pass
                    if stderr_reader is not None:
                        stderr_reader.join(timeout=1)
                        if not stderr_reader.is_alive():
                            try:
                                proc_stderr.close()
                            except OSError:
                                pass
                with data_lock:
                    if total_bytes >= MAX_STDOUT_BYTES and tail_important:
                        stdout = "".join(kept_lines) + "\n" + "".join(tail_important)
                    else:
                        stdout = "".join(kept_lines)
                    stderr_tail = "".join(stderr_lines).strip()[-400:]
            else:
                try:
                    comm_res = proc.communicate(timeout=self.timeout)
                    if isinstance(comm_res, tuple) and len(comm_res) >= 2:
                        stdout, stderr_raw = comm_res[0], comm_res[1]
                    else:
                        stdout, stderr_raw = comm_res, ""
                    stderr_tail = str(stderr_raw or "").strip()[-400:]
                except subprocess.TimeoutExpired:
                    if not cancel_triggered[0]:
                        timed_out = True
                    _kill_process_group()
                    try:
                        proc.communicate(timeout=5)
                    except (subprocess.TimeoutExpired, OSError):
                        pass
                finally:
                    done_event.set()
                    if watcher is not None:
                        watcher.join(timeout=0.5)

            cancelled = bool(cancel_triggered[0] or (cancel_event is not None and cancel_event.is_set()))
        finally:
            rc = getattr(proc, "returncode", 0)
            if not timed_out and not cancelled and rc in (0, None):
                collected = self._collect_outbox(
                    outbox_dir, task_id, expected_task_dir=task_dir, extra_secrets=extra_secrets
                )
            shutil.rmtree(task_dir, ignore_errors=True)

        elapsed = round(time.monotonic() - started, 1)
        if cancelled:
            return TaskResult(
                status="cancelled",
                partial=backend.parse_output(stdout, limit=400),
                elapsed=elapsed,
                timeout=self.timeout,
                error="cancelled",
            )
        if timed_out:
            return TaskResult(
                status="timeout",
                partial=backend.parse_output(stdout, limit=400),
                elapsed=elapsed,
                timeout=self.timeout,
                error="timeout",
            )
        answer = backend.parse_output(stdout, limit=MAX_OUTPUT)
        rc = getattr(proc, "returncode", 0)
        if rc not in (0, None):
            err_msg = "exit {}".format(rc) + (": " + stderr_tail if stderr_tail else "")
            return TaskResult(
                status="error",
                partial=answer[:400],
                elapsed=elapsed,
                timeout=self.timeout,
                error=err_msg[:400],
            )
        if answer or collected:
            return TaskResult(status="ok", text=answer, files=collected, elapsed=elapsed, timeout=self.timeout)
        return TaskResult(status="empty", elapsed=elapsed, timeout=self.timeout, error="empty output")

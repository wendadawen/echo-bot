"""通用安全沙箱执行底座：与具体 CLI 后端解耦。

保留并强化全部容器边界与安全防线：
- 每任务独立临时工作区（含只读输入 inbox/ 与受控导出 outbox/）与独立 HOME；
- Linux root 下通过 setpriv --reuid=65534 --regid=65534 --no-new-privs 降权到 nobody；
- Linux 下叠加 prlimit（CPU/文件大小/打开文件数/进程数/虚拟内存）防资源耗尽；
- 最小环境变量白名单（剥离空白代理变量，严禁透传 ONEBOT_TOKEN 等宿主机密钥）；
- 支持宿主编排型后端的零密钥沙箱命令执行（run_sandboxed_command）；
- 原子写（O_NOFOLLOW）与受控 outbox 收集，超时或喊停直接 killpg 绝不留孤儿进程。
"""

from __future__ import annotations

import collections
import os
import resource
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from ..domain import HarnessBackend, TaskRequest, TaskResult, detect_image_format
from .fs_guard import (
    DEFAULT_TIMEOUT,
    MAX_FILE_BYTES,
    MAX_OUTPUT,
    MAX_OUTPUT_FILES,
    MAX_TIMEOUT,
    MIN_TIMEOUT,
    NOBODY_GID,
    NOBODY_UID,
    OUTBOX_TTL_SECONDS,
    SAFE_EXPORT_SUFFIXES,
    atomic_write_bytes,
    atomic_write_text_no_follow,
    cleanup_file_entry,
    cleanup_outbox,
    collect_outbox,
    contains_secret_bytes,
    is_safe_export_name,
    parse_timeout,
    sanitize_export_name,
)

MAX_STDOUT_BYTES = 4 * 1024 * 1024
MAX_STDERR_BYTES = 8192
MAX_CMD_STDOUT_BYTES = 256 * 1024

SETPRIV_BIN = "/usr/bin/setpriv"
PRLIMIT_BIN = "/usr/bin/prlimit"

DEFAULT_RLIMIT_FSIZE = 64 * 1024 * 1024
DEFAULT_RLIMIT_NOFILE = 256
DEFAULT_RLIMIT_NPROC = 256
DEFAULT_RLIMIT_AS_CMD = 2 * 1024 * 1024 * 1024
DEFAULT_RLIMIT_AS_CLI = 3 * 1024 * 1024 * 1024

_SAFE_ENV_KEYS = (
    "PATH", "LANG", "LC_ALL", "TZ",
    "SSL_CERT_FILE", "SSL_CERT_DIR",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
    "http_proxy", "https_proxy", "no_proxy",
)


def _kill_process_group(proc: Any) -> None:
    proc_pid = getattr(proc, "pid", None)
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


def _iter_lines(stream: Any, max_chars: int = 65536):
    readline = getattr(stream, "readline", None)
    if callable(readline):
        while True:
            try:
                line = readline(max_chars)
            except TypeError:
                line = readline()
            if not line:
                break
            yield line
        return
    for line in stream:
        yield line


def _start_cancel_watcher(
    cancel_event: Optional[threading.Event],
    done_event: threading.Event,
    kill_fn: Callable[[], None],
) -> tuple[list[bool], Optional[threading.Thread]]:
    cancel_triggered = [False]
    if cancel_event is None:
        return cancel_triggered, None
    if cancel_event.is_set():
        cancel_triggered[0] = True
        kill_fn()
        return cancel_triggered, None

    def _watch_cancel() -> None:
        while not done_event.is_set():
            if cancel_event.wait(timeout=0.05):
                if not done_event.is_set():
                    cancel_triggered[0] = True
                    kill_fn()
                return

    watcher = threading.Thread(target=_watch_cancel, name="echo-sandbox-cancel", daemon=True)
    watcher.start()
    return cancel_triggered, watcher


def _close_quiet(stream: Any, thread: Optional[threading.Thread]) -> None:
    if thread is not None:
        thread.join(timeout=2)
        if thread.is_alive():
            return
    if stream is not None:
        try:
            stream.close()
        except OSError:
            pass


def _wait_proc_with_cancel(
    proc: Any, timeout: int, cancel_triggered: list[bool], done_event: threading.Event, watcher: Optional[threading.Thread],
) -> bool:
    timed_out = False
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        if not cancel_triggered[0]:
            timed_out = True
        _kill_process_group(proc)
        try:
            proc.wait(timeout=5)
        except (subprocess.TimeoutExpired, OSError):
            pass
    finally:
        done_event.set()
        if watcher is not None:
            watcher.join(timeout=0.5)
        if getattr(proc, "returncode", None) is None:
            _kill_process_group(proc)
    return timed_out


def _exec_piped_process(
    cmd: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout: int,
    cancel_event: Optional[threading.Event],
    max_stdout_bytes: int,
    capture_stderr: bool = False,
    on_line: Optional[Callable[[str], None]] = None,
    keep_agent_messages: bool = False,
) -> tuple[Optional[int], str, str, bool, bool]:
    proc = subprocess.Popen(
        cmd,
        cwd=str(cwd),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=(subprocess.PIPE if capture_stderr else subprocess.STDOUT),
        text=True,
        start_new_session=True,
    )
    timed_out = False
    stdout = ""
    stderr_tail = ""
    proc_stdout, proc_stderr = getattr(proc, "stdout", None), getattr(proc, "stderr", None)
    done_event = threading.Event()
    cancel_triggered, watcher = _start_cancel_watcher(cancel_event, done_event, lambda: _kill_process_group(proc))

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
                for line in _iter_lines(proc_stdout):
                    line_len = len(line.encode("utf-8", errors="ignore"))
                    if on_line is not None:
                        try:
                            on_line(line)
                        except Exception:
                            pass
                    with data_lock:
                        if keep_agent_messages and '"agent_message"' in line:
                            tail_important.append(line)
                        if total_bytes < max_stdout_bytes:
                            kept_lines.append(line)
                            total_bytes += line_len
            except Exception:
                pass

        def _drain_stderr() -> None:
            nonlocal stderr_bytes
            try:
                for line in _iter_lines(proc_stderr):
                    chunk = line[-400:] if len(line) > 400 else line
                    chunk_len = len(chunk.encode("utf-8", errors="ignore"))
                    with data_lock:
                        if stderr_lines.maxlen is not None and len(stderr_lines) >= stderr_lines.maxlen:
                            stderr_bytes -= len(stderr_lines.popleft().encode("utf-8", errors="ignore"))
                        stderr_lines.append(chunk)
                        stderr_bytes += chunk_len
                        while stderr_bytes > MAX_STDERR_BYTES and len(stderr_lines) > 1:
                            stderr_bytes -= len(stderr_lines.popleft().encode("utf-8", errors="ignore"))
                        stderr_bytes = max(0, stderr_bytes)
            except Exception:
                pass

        reader = threading.Thread(target=_drain, name="echo-sandbox-stdout", daemon=True)
        reader.start()
        stderr_reader = threading.Thread(target=_drain_stderr, name="echo-sandbox-stderr", daemon=True) if (capture_stderr and proc_stderr is not None) else None
        if stderr_reader is not None:
            stderr_reader.start()
        try:
            timed_out = _wait_proc_with_cancel(proc, timeout, cancel_triggered, done_event, watcher)
        finally:
            _close_quiet(proc_stdout, reader)
            _close_quiet(proc_stderr, stderr_reader)
        with data_lock:
            stdout = (
                "".join(kept_lines) + "\n" + "".join(tail_important)
                if (keep_agent_messages and total_bytes >= max_stdout_bytes and tail_important)
                else "".join(kept_lines)
            )
            stderr_tail = "".join(stderr_lines).strip()[-400:]
    else:
        try:
            comm_res = proc.communicate(timeout=timeout)
            stdout, stderr_raw = comm_res[:2] if (isinstance(comm_res, tuple) and len(comm_res) >= 2) else (comm_res, "")
            stderr_tail = str(stderr_raw or "").strip()[-400:]
        except subprocess.TimeoutExpired:
            if not cancel_triggered[0]:
                timed_out = True
            _kill_process_group(proc)
            try:
                proc.communicate(timeout=5)
            except (subprocess.TimeoutExpired, OSError):
                pass
        finally:
            done_event.set()
            if watcher is not None:
                watcher.join(timeout=0.5)

    rc = getattr(proc, "returncode", None)
    cancelled = bool(cancel_triggered[0] or (cancel_event is not None and cancel_event.is_set() and rc != 0))
    return rc, str(stdout or ""), stderr_tail, timed_out, cancelled


class SandboxRunner:
    """通用隔离沙箱：配合任意 HarnessBackend 在独立目录内降权并限额执行任务。"""

    def __init__(self, workdir_root: str | Path, outbox_root: str | Path = "", timeout: Any = DEFAULT_TIMEOUT):
        self.workdir_root = str(workdir_root or "")
        self.outbox_root = str(outbox_root) if outbox_root else (str(Path(self.workdir_root).parent / "outbox") if self.workdir_root else "")
        self.timeout = parse_timeout(timeout)

    @staticmethod
    def use_setpriv() -> bool:
        return sys.platform.startswith("linux") and hasattr(os, "geteuid") and os.geteuid() == 0

    @staticmethod
    def use_prlimit() -> bool:
        return sys.platform.startswith("linux") and os.path.exists(PRLIMIT_BIN)

    @staticmethod
    def _clamp_rlimit(res_id: int, requested: int) -> int:
        try:
            _, hard = resource.getrlimit(res_id)
        except (ValueError, OSError):
            return int(requested)
        return min(int(requested), int(hard)) if (hard != resource.RLIM_INFINITY and hard > 0) else int(requested)

    def _prlimit_prefix(self, *, cpu_limit: int, as_limit: int) -> list[str]:
        cpu = self._clamp_rlimit(resource.RLIMIT_CPU, max(10, int(cpu_limit)))
        fsize = self._clamp_rlimit(resource.RLIMIT_FSIZE, DEFAULT_RLIMIT_FSIZE)
        nofile = self._clamp_rlimit(resource.RLIMIT_NOFILE, DEFAULT_RLIMIT_NOFILE)
        nproc_id = getattr(resource, "RLIMIT_NPROC", None)
        nproc = self._clamp_rlimit(nproc_id, DEFAULT_RLIMIT_NPROC) if nproc_id is not None else DEFAULT_RLIMIT_NPROC
        as_bytes = self._clamp_rlimit(resource.RLIMIT_AS, int(as_limit))
        return [PRLIMIT_BIN, f"--cpu={cpu}", f"--fsize={fsize}", f"--nofile={nofile}", f"--nproc={nproc}", f"--as={as_bytes}", "--"]

    def available(self, backend: HarnessBackend) -> bool:
        if not backend.available():
            return False
        if os.environ.get("ECHO_SANDBOX_REQUIRE_ISOLATION") == "1" and not (self.use_setpriv() and os.path.exists(SETPRIV_BIN)):
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

    def _wrap_command(self, exec_args: list[str], *, cpu_limit: Optional[int] = None, as_limit: Optional[int] = None) -> list[str]:
        prefix: list[str] = []
        if self.use_setpriv():
            prefix.extend([SETPRIV_BIN, f"--reuid={NOBODY_UID}", f"--regid={NOBODY_GID}", "--clear-groups", "--no-new-privs"])
        if self.use_prlimit():
            prefix.extend(self._prlimit_prefix(cpu_limit=max(10, int(cpu_limit or self.timeout)), as_limit=int(as_limit or DEFAULT_RLIMIT_AS_CLI)))
        return [*prefix, *exec_args]

    @staticmethod
    def _build_env(task_dir: Path, home_dir: Path, extra_env: dict[str, str]) -> dict[str, str]:
        env: dict[str, str] = {}
        for key in _SAFE_ENV_KEYS:
            val = os.environ.get(key)
            if val is not None and not ("proxy" in key.lower() and not str(val).strip()):
                env[key] = val
        env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
        env.update({"HOME": str(task_dir), "TMPDIR": str(task_dir), "TMP": str(task_dir), "TEMP": str(task_dir)})
        for k, v in (extra_env or {}).items():
            if v is not None:
                env[str(k)] = str(v)
        return env

    def run_sandboxed_command(
        self, command: str, *, task_dir: Path, timeout_sec: int = 30, cancel_event: Optional[threading.Event] = None, max_output_chars: int = 6000,
    ) -> tuple[int, str, bool, bool]:
        """在降权且零密钥的隔离环境内执行一条 shell 命令，返回 (rc, output, timed_out, cancelled)。"""
        shell_bin = shutil.which("bash") or "/bin/sh"
        sec = max(1, int(timeout_sec or 30))
        cmd = self._wrap_command([shell_bin, "-c", str(command or "")], cpu_limit=max(15, sec), as_limit=DEFAULT_RLIMIT_AS_CMD)
        try:
            rc, body, _, timed_out, cancelled = _exec_piped_process(
                cmd, cwd=task_dir, env=self._build_env(task_dir, task_dir, {}), timeout=sec, cancel_event=cancel_event, max_stdout_bytes=MAX_CMD_STDOUT_BYTES,
            )
        except OSError as exc:
            return -1, str(exc), False, False
        text = body.strip()
        limit = max(200, int(max_output_chars or 6000))
        if len(text) > limit:
            head, tail = max(100, limit // 2), max(100, limit - max(100, limit // 2) - 500)
            text = text[:head] + "\n...（已截断）...\n" + text[-tail:]
        return (int(rc) if rc is not None else -1), text, timed_out, cancelled

    def _collect_outbox(self, outbox_dir: Path, task_id: str, expected_task_dir: Path, extra_secrets: Sequence[str] = ()) -> tuple[dict[str, object], ...]:
        return collect_outbox(self.outbox_root, outbox_dir, task_id, expected_task_dir, extra_secrets=extra_secrets)

    def _prepare_workspace(self, req: TaskRequest, backend: HarnessBackend, workdir_base: Path, task_id: str) -> tuple[Path, Path, Path, list[Path]]:
        task_dir, outbox_dir, inbox_dir, home_dir = workdir_base / task_id, workdir_base / task_id / "outbox", workdir_base / task_id / "inbox", workdir_base / task_id / f".{backend.name or 'agent'}"
        inbox_files: list[Path] = []
        for d in (task_dir, outbox_dir, home_dir):
            d.mkdir(parents=True, exist_ok=True)
        backend.prepare_home(home_dir)
        if req.images:
            inbox_dir.mkdir(parents=True, exist_ok=True)
            for idx, img_bytes in enumerate(list(req.images)[:2], 1):
                if not img_bytes:
                    continue
                _, ext = detect_image_format(img_bytes)
                fpath = inbox_dir / f"input_{idx}{ext}"
                atomic_write_bytes(fpath, img_bytes, mode=0o644)
                inbox_files.append(fpath)
        for path, mode in ((task_dir, 0o711), (outbox_dir, 0o700), (home_dir, 0o755), (inbox_dir, 0o755)):
            if path.exists() and not path.is_symlink():
                try:
                    if path in (task_dir, outbox_dir, home_dir):
                        os.chown(path, NOBODY_UID, NOBODY_GID)
                    os.chmod(path, mode)
                except OSError:
                    pass
        return task_dir, outbox_dir, home_dir, inbox_files

    def run(
        self, req: TaskRequest, backend: HarnessBackend, cancel_event: Optional[threading.Event] = None,
        on_progress: Optional[Callable[[str], None]] = None, extra_secrets: Sequence[str] = (),
    ) -> TaskResult:
        prompt = str(req.prompt or "").strip()
        if not prompt:
            return TaskResult(status="empty", timeout=self.timeout)
        if not self.available(backend):
            return TaskResult(status="error", timeout=self.timeout, error="sandbox_unavailable")
        try:
            workdir_base = self._prepare_roots()
        except OSError as exc:
            return TaskResult(status="error", timeout=self.timeout, error=str(exc))

        task_id = "task-" + uuid.uuid4().hex[:10]
        try:
            task_dir, outbox_dir, home_dir, inbox_files = self._prepare_workspace(req, backend, workdir_base, task_id)
        except OSError as exc:
            shutil.rmtree(workdir_base / task_id, ignore_errors=True)
            return TaskResult(status="error", timeout=self.timeout, error=str(exc))

        collected: tuple[dict[str, object], ...] = ()
        exec_fn = getattr(backend, "execute_in_workspace", None)
        if callable(exec_fn):
            res = TaskResult(status="error", timeout=self.timeout, error="unknown")
            try:
                res = exec_fn(runner=self, prompt=prompt, task_dir=task_dir, outbox_dir=outbox_dir, inbox_files=inbox_files, timeout=self.timeout, cancel_event=cancel_event, on_progress=on_progress)
            finally:
                if res.status == "ok":
                    collected = self._collect_outbox(outbox_dir, task_id, task_dir, extra_secrets=extra_secrets)
                shutil.rmtree(task_dir, ignore_errors=True)
            if res.status != "ok":
                return res
            answer = str(res.text or "").strip()[:MAX_OUTPUT]
            return TaskResult(status="ok", text=answer, files=collected, elapsed=res.elapsed, timeout=self.timeout) if (answer or collected) else TaskResult(status="empty", elapsed=res.elapsed, timeout=self.timeout, error="empty output")

        env = self._build_env(task_dir, home_dir, backend.env_overrides(task_dir, home_dir))
        cmd = self._wrap_command(backend.build_command(prompt, task_dir, outbox_dir, inbox_files, self.timeout), cpu_limit=max(60, self.timeout), as_limit=DEFAULT_RLIMIT_AS_CLI)
        started = time.monotonic()

        def _on_line(line: str) -> None:
            step = backend.parse_line(line)
            if step and on_progress is not None:
                on_progress(step)

        try:
            rc, stdout, stderr_tail, timed_out, cancelled = _exec_piped_process(
                cmd, cwd=task_dir, env=env, timeout=self.timeout, cancel_event=cancel_event,
                max_stdout_bytes=MAX_STDOUT_BYTES, capture_stderr=True, on_line=_on_line, keep_agent_messages=True,
            )
            if not timed_out and not cancelled and rc == 0:
                collected = self._collect_outbox(outbox_dir, task_id, task_dir, extra_secrets=extra_secrets)
        except OSError as exc:
            return TaskResult(status="error", timeout=self.timeout, error=str(exc))
        finally:
            shutil.rmtree(task_dir, ignore_errors=True)

        elapsed = round(time.monotonic() - started, 1)
        if cancelled:
            return TaskResult(status="cancelled", partial=backend.parse_output(stdout, limit=400), elapsed=elapsed, timeout=self.timeout, error="cancelled")
        if timed_out:
            return TaskResult(status="timeout", partial=backend.parse_output(stdout, limit=400), elapsed=elapsed, timeout=self.timeout, error="timeout")
        answer = backend.parse_output(stdout, limit=MAX_OUTPUT)
        if rc != 0:
            return TaskResult(status="error", partial=answer[:400], elapsed=elapsed, timeout=self.timeout, error=(f"exit {rc}: {stderr_tail}" if stderr_tail else f"exit {rc}")[:400])
        if answer or collected:
            return TaskResult(status="ok", text=answer, files=collected, elapsed=elapsed, timeout=self.timeout)
        return TaskResult(status="empty", elapsed=elapsed, timeout=self.timeout, error="empty output")

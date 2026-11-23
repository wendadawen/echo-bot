"""Codex CLI harness：把任务交给 headless Codex，在独立目录里干活。

规划、工具循环、上下文压缩都由 Codex 负责；本项目只负责派任务、限时、
截断结果和失败兜底。二进制锁定在镜像里（见 Dockerfile），不联网安装。
每个群共用稳定工作根前缀，但每次任务在独立子目录与独立 CODEX_HOME 中降权执行。
"""

import collections
import json
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

MAX_OUTPUT = 800
DEFAULT_TIMEOUT = 300
MIN_TIMEOUT = 180
MAX_TIMEOUT = 600
MAX_STDOUT_BYTES = 4 * 1024 * 1024

# 容器本身就是隔离边界，容器内再套 bwrap 会因 user namespace 限制直接失败。
# 所以用 danger-full-access，靠外层容器的目录权限、独立任务 HOME 和降权进程收边界。
SANDBOX = "danger-full-access"
NOBODY_UID = 65534
NOBODY_GID = 65534
SETPRIV_BIN = "/usr/bin/setpriv"
MAX_OUTPUT_FILES = 3
MAX_FILE_BYTES = 20 * 1024 * 1024
OUTBOX_TTL_SECONDS = 3600

_SAFE_EXPORT_SUFFIXES = frozenset({
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
    re.compile(rb"\bsk-[A-Za-z0-9_-]{16,}\b"),
)
_SAFE_ENV_KEYS = (
    "PATH", "LANG", "LC_ALL", "TZ",
    "SSL_CERT_FILE", "SSL_CERT_DIR",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
    "http_proxy", "https_proxy", "no_proxy",
)


def _parse_timeout(value, default=DEFAULT_TIMEOUT):
    """解析后台任务超时（秒），支持数字与数字字符串并限制在 [180, 600]。"""
    if value is None or isinstance(value, bool):
        return default
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return default
        value = stripped
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(MIN_TIMEOUT, min(MAX_TIMEOUT, parsed))


def _sanitize_export_name(name):
    """提取纯文件名，去掉控制字符、BiDi 伪装字符、路径片段并限制长度。"""
    raw = str(name or "").replace("\\", "/")
    base = Path(raw).name
    clean = _BIDI_AND_CTRL.sub("", base).strip()
    if len(clean) > 100:
        stem, suffix = Path(clean).stem, Path(clean).suffix
        if suffix and len(suffix) < 20:
            clean = stem[:100 - len(suffix)] + suffix
        else:
            clean = clean[:100]
    return clean


def _is_safe_export_name(name):
    """严格白名单后缀 + 黑名单敏感词拦截，防止越权导出配置/数据库/脚本。"""
    clean = _sanitize_export_name(name)
    if not clean or clean in (".", "..") or clean.startswith("."):
        return False
    lower = clean.lower()
    if lower in _UNSAFE_EXACT:
        return False
    if any(lower.endswith(sfx) for sfx in _UNSAFE_SUFFIXES):
        return False
    if any(word in lower for word in _UNSAFE_WORDS):
        return False
    suffix = Path(lower).suffix
    if suffix not in _SAFE_EXPORT_SUFFIXES:
        return False
    return True


def _contains_secret_bytes(data, extra_secrets=()):
    """检测文件字节流是否包含 SQLite 库头、私钥、Token 或已知运行密钥。"""
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
            encoded = s.encode("utf-8", errors="ignore")
            if encoded and encoded in data:
                return True
    return False


def _atomic_write_text_no_follow(path, text, mode=0o644):
    """在不跟随符号链接的前提下原子写入文本文件。"""
    target = Path(path)
    if target.is_symlink():
        raise OSError("refuse to overwrite symlink: {}".format(target))
    parent = target.parent
    if parent.is_symlink():
        raise OSError("refuse to write under symlink dir: {}".format(parent))
    tmp_name = ".{}.{}.tmp".format(target.name, uuid.uuid4().hex[:8])
    tmp_path = parent / tmp_name
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


def _atomic_write_bytes(path, data, mode=0o600):
    """在不跟随符号链接的前提下原子写入二进制文件。"""
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


def cleanup_outbox(outbox_root, max_age=OUTBOX_TTL_SECONDS, keep_paths=None):
    """清理过期的 outbox 任务目录；跳过仍在待补发队列里的文件所在任务目录。"""
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
            resolved = Path(p).resolve(strict=False)
            protected_dirs.add(str(resolved.parent))
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


def cleanup_file_entry(path, outbox_root):
    """只删除 outbox_root 内部的单个待发文件及其空父目录，拒绝路径穿越。"""
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


class CodexHarness:
    def __init__(self, binary="/usr/local/bin/codex", home="", workdir_root="",
                 api_base="", api_key="", model="", timeout=DEFAULT_TIMEOUT,
                 outbox_root=""):
        self.binary = str(binary or "")
        self.home = str(home or "")
        self.workdir_root = str(workdir_root or "")
        if outbox_root:
            self.outbox_root = str(outbox_root)
        elif self.workdir_root:
            self.outbox_root = str(Path(self.workdir_root).parent / "outbox")
        else:
            self.outbox_root = ""
        self.api_base = str(api_base or "")
        self.api_key = str(api_key or "")
        self.model = str(model or "")
        self.timeout = _parse_timeout(timeout)
        self._config_lock = threading.Lock()

    def available(self):
        if not (self.binary and self.home and os.path.exists(self.binary)):
            return False
        if sys.platform.startswith("linux") and hasattr(os, "geteuid") and os.geteuid() == 0:
            if not os.path.exists(SETPRIV_BIN):
                return False
        return True

    def _prepare_dirs(self):
        """准备配置目录、工作根目录与私密 outbox 目录，避免暴露给低权限子进程。"""
        home_path = Path(self.home)
        if not home_path.is_symlink():
            try:
                home_path.mkdir(parents=True, exist_ok=True)
                os.chmod(home_path, 0o755)
            except OSError:
                pass
        work_path = Path(self.workdir_root)
        if not work_path.is_symlink():
            try:
                work_path.mkdir(parents=True, exist_ok=True)
                os.chmod(work_path, 0o711)
            except OSError:
                pass
        if self.outbox_root:
            outbox_path = Path(self.outbox_root)
            if not outbox_path.is_symlink():
                try:
                    outbox_path.mkdir(parents=True, exist_ok=True)
                    os.chmod(outbox_path, 0o700)
                except OSError:
                    pass

    def _config_text(self):
        base = (self.api_base or "https://api.openai.com/v1").rstrip("/")
        model = self.model or "gpt-4o-mini"
        return (
            "model = {}\n"
            'model_provider = "echo"\n\n'
            "[model_providers.echo]\n"
            'name = "echo"\n'
            "base_url = {}\n"
            'env_key = "ECHO_API_KEY"\n'
            'wire_api = "responses"\n'
        ).format(json.dumps(model), json.dumps(base))

    def _ensure_config(self, target_dir=None):
        """按插件配置生成 Codex provider 配置；原子写入且拒绝跟随符号链接。"""
        base_dir = Path(target_dir) if target_dir else (Path(self.home) if self.home else None)
        if not base_dir or base_dir.is_symlink():
            return
        path = base_dir / "config.toml"
        text = self._config_text()
        with self._config_lock:
            try:
                base_dir.mkdir(parents=True, exist_ok=True)
                if path.exists() and not path.is_symlink():
                    try:
                        if path.read_text(encoding="utf-8") == text:
                            return
                    except OSError:
                        pass
                if path.is_symlink():
                    path.unlink(missing_ok=True)
                _atomic_write_text_no_follow(path, text, mode=0o644)
            except OSError:
                pass

    def _workdir(self, group_id):
        """工作根目录固定，实际任务各自落在独立 task-<id> 子目录。"""
        path = Path(self.workdir_root)
        if path.is_symlink():
            raise OSError("workdir_root cannot be a symlink")
        path.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(path, 0o711)
        except OSError:
            pass
        return path

    def _command(self, task):
        """降权到 nobody（附加 --no-new-privs）再用当前 binary 启动 Codex。"""
        return [
            SETPRIV_BIN,
            "--reuid={}".format(NOBODY_UID),
            "--regid={}".format(NOBODY_GID),
            "--clear-groups",
            "--no-new-privs",
            self.binary or "codex",
            "exec", "--json", "--skip-git-repo-check",
            "--sandbox", SANDBOX, "--", task,
        ]

    @staticmethod
    def _clean_workdir(workdir):
        """清理工作目录内临时生成的文件与子目录，避免跨任务残留。"""
        root = Path(workdir)
        if not root.exists():
            return
        for entry in list(root.iterdir()):
            try:
                if entry.is_dir() and not entry.is_symlink():
                    shutil.rmtree(entry, ignore_errors=True)
                else:
                    entry.unlink(missing_ok=True)
            except OSError:
                pass

    def _build_env(self, task_dir, task_codex_home):
        """构建最小环境变量白名单，严禁透传 ONEBOT_TOKEN 等宿主密钥。"""
        env = {}
        for key in _SAFE_ENV_KEYS:
            val = os.environ.get(key)
            if val is not None:
                env[key] = val
        env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
        env["CODEX_HOME"] = str(task_codex_home)
        env["HOME"] = str(task_dir)
        env["TMPDIR"] = str(task_dir)
        env["TMP"] = str(task_dir)
        env["TEMP"] = str(task_dir)
        if self.api_key:
            env["ECHO_API_KEY"] = self.api_key
        return env

    def _collect_outbox(self, outbox_dir, task_id, expected_task_dir=None):
        """从任务专属 ./outbox/ 中安全收集最多 MAX_OUTPUT_FILES 个合格文件。"""
        outbox_path = Path(outbox_dir)
        if not self.outbox_root or outbox_path.is_symlink() or not outbox_path.is_dir():
            return []
        try:
            resolved_outbox = outbox_path.resolve(strict=True)
            if expected_task_dir is not None:
                expected = Path(expected_task_dir).resolve(strict=True) / "outbox"
                if resolved_outbox != expected:
                    return []
        except OSError:
            return []

        dir_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            dir_fd = os.open(str(resolved_outbox), dir_flags)
        except OSError:
            return []

        candidates = []
        file_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        try:
            try:
                names = os.listdir(dir_fd)
            except OSError:
                names = []
            for name in names:
                if not _is_safe_export_name(name):
                    continue
                safe_name = _sanitize_export_name(name)
                if not safe_name:
                    continue
                try:
                    fd = os.open(name, file_flags, dir_fd=dir_fd)
                except OSError:
                    continue
                try:
                    st = os.fstat(fd)
                    if not stat.S_ISREG(st.st_mode):
                        continue
                    if st.st_nlink != 1:
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
                if _contains_secret_bytes(data, extra_secrets=(self.api_key,)):
                    continue
                candidates.append((st.st_mtime, safe_name, data, len(data)))
        finally:
            try:
                os.close(dir_fd)
            except OSError:
                pass

        if not candidates:
            return []

        candidates.sort(key=lambda row: (-row[0], row[1]))
        outbox_root = Path(self.outbox_root)
        dest_dir = outbox_root / task_id
        try:
            outbox_root.mkdir(parents=True, exist_ok=True)
            os.chmod(outbox_root, 0o700)
            dest_dir.mkdir(parents=True, exist_ok=True)
            os.chmod(dest_dir, 0o700)
        except OSError:
            return []

        collected = []
        used_names = set()
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
                _atomic_write_bytes(dest_file, data, mode=0o600)
            except OSError:
                continue
            collected.append({"name": final_name, "path": str(dest_file), "size": size})
        return collected

    def run_with_files(self, group_id, task):
        """在独立临时目录跑 Codex，流式捕获进度并返回结构化状态与 ./outbox/ 文件。"""
        def _empty_result(status="empty", partial="", error="", elapsed=0.0):
            return {
                "status": status,
                "text": "",
                "partial": partial,
                "files": [],
                "elapsed": elapsed,
                "timeout": self.timeout,
                "error": error,
            }

        task = str(task or "").strip()
        if not task or not self.available():
            return _empty_result(status="empty")
        self._prepare_dirs()
        try:
            workdir = self._workdir(group_id)
        except OSError as exc:
            return _empty_result(status="error", error=str(exc))

        task_id = "task-" + uuid.uuid4().hex[:10]
        task_dir = workdir / task_id
        outbox_dir = task_dir / "outbox"
        task_codex_home = task_dir / ".codex"
        try:
            task_dir.mkdir(parents=True, exist_ok=True)
            outbox_dir.mkdir(parents=True, exist_ok=True)
            task_codex_home.mkdir(parents=True, exist_ok=True)
            self._ensure_config(task_codex_home)
            for path, mode in ((task_dir, 0o700), (outbox_dir, 0o700), (task_codex_home, 0o755)):
                if not path.is_symlink():
                    try:
                        if path != task_codex_home:
                            os.chown(path, NOBODY_UID, NOBODY_GID)
                        os.chmod(path, mode)
                    except OSError:
                        pass
            if not task_codex_home.is_symlink():
                try:
                    os.chown(task_codex_home, NOBODY_UID, NOBODY_GID)
                except OSError:
                    pass
        except OSError as exc:
            shutil.rmtree(task_dir, ignore_errors=True)
            return _empty_result(status="error", error=str(exc))

        env = self._build_env(task_dir, task_codex_home)
        cmd = self._command(task)
        started = time.monotonic()
        try:
            proc = subprocess.Popen(
                cmd, cwd=str(task_dir), env=env, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, start_new_session=True,
            )
        except OSError as exc:
            shutil.rmtree(task_dir, ignore_errors=True)
            return _empty_result(status="error", error=str(exc))

        proc_pid = getattr(proc, "pid", None)
        timed_out = False
        stdout = ""
        collected = []
        proc_stdout = getattr(proc, "stdout", None)

        def _kill_process_group():
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

        try:
            if proc_stdout is not None:
                kept_lines = []
                tail_agent_lines = collections.deque(maxlen=32)
                total_bytes = 0

                def _drain():
                    nonlocal total_bytes
                    try:
                        for line in proc_stdout:
                            line_len = len(line.encode("utf-8", errors="ignore"))
                            if '"agent_message"' in line:
                                tail_agent_lines.append(line)
                            if total_bytes < MAX_STDOUT_BYTES:
                                kept_lines.append(line)
                                total_bytes += line_len
                    except Exception:
                        pass

                reader = threading.Thread(
                    target=_drain, name="echo-codex-stdout", daemon=True
                )
                reader.start()
                try:
                    proc.wait(timeout=self.timeout)
                except subprocess.TimeoutExpired:
                    timed_out = True
                    _kill_process_group()
                    try:
                        proc.wait(timeout=5)
                    except (subprocess.TimeoutExpired, OSError):
                        pass
                finally:
                    if getattr(proc, "returncode", None) is None:
                        _kill_process_group()
                    reader.join(timeout=2)
                    if not reader.is_alive():
                        try:
                            proc_stdout.close()
                        except OSError:
                            pass
                if total_bytes >= MAX_STDOUT_BYTES and tail_agent_lines:
                    stdout = "".join(kept_lines) + "\n" + "".join(tail_agent_lines)
                else:
                    stdout = "".join(kept_lines)
            else:
                try:
                    stdout, _ = proc.communicate(timeout=self.timeout)
                except subprocess.TimeoutExpired:
                    timed_out = True
                    _kill_process_group()
                    try:
                        proc.communicate(timeout=5)
                    except (subprocess.TimeoutExpired, OSError):
                        pass
        finally:
            if not timed_out:
                collected = self._collect_outbox(
                    outbox_dir, task_id, expected_task_dir=task_dir
                )
            shutil.rmtree(task_dir, ignore_errors=True)
            cleanup_outbox(self.outbox_root)

        elapsed = round(time.monotonic() - started, 1)
        if timed_out:
            partial = parse_events(stdout, limit=400)
            return {
                "status": "timeout",
                "text": "",
                "partial": partial,
                "files": [],
                "elapsed": elapsed,
                "timeout": self.timeout,
                "error": "timeout",
            }
        answer = parse_events(stdout, MAX_OUTPUT)
        rc = getattr(proc, "returncode", 0)
        if answer or collected:
            status, err_msg = "ok", ""
        elif rc not in (0, None):
            status, err_msg = "error", "exit {}".format(rc)
        else:
            status, err_msg = "empty", "empty output"
        return {
            "status": status,
            "text": answer,
            "partial": "",
            "files": collected,
            "elapsed": elapsed,
            "timeout": self.timeout,
            "error": err_msg,
        }

    def run(self, group_id, task):
        result = self.run_with_files(group_id, task)
        for item in result.get("files") or []:
            cleanup_file_entry(item.get("path", ""), self.outbox_root)
        return result.get("text", "")


def parse_events(raw, limit=MAX_OUTPUT):
    """从 Codex 的 JSONL 事件里取最后一条助手消息。"""
    answer = ""
    for line in str(raw or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("type") != "item.completed":
            continue
        item = event.get("item") or {}
        if item.get("type") != "agent_message":
            continue
        text = str(item.get("text") or "").strip()
        if text:
            answer = text
    return answer[:limit]

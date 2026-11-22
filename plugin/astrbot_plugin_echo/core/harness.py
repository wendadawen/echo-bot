"""Codex CLI harness：把任务交给 headless Codex，在独立目录里干活。

规划、工具循环、上下文压缩都由 Codex 负责；本项目只负责派任务、限时、
截断结果和失败兜底。二进制锁定在镜像里（见 Dockerfile），不联网安装。
每个群一个工作目录，Codex 只能在这个目录里读写，不碰仓库源码。
"""

import json
import os
import re
import shutil
import signal
import stat
import subprocess
import time
import uuid
from pathlib import Path

MAX_OUTPUT = 800
DEFAULT_TIMEOUT = 150
# 容器本身就是隔离边界，容器内再套 bwrap 会因 user namespace 限制直接失败。
# 所以用 danger-full-access，靠外层容器的目录权限和降权进程收边界。
SANDBOX = "danger-full-access"
NOBODY_UID = 65534
NOBODY_GID = 65534
MAX_OUTPUT_FILES = 3
MAX_FILE_BYTES = 20 * 1024 * 1024
OUTBOX_TTL_SECONDS = 3600

_UNSAFE_SUFFIXES = (
    ".env", ".pem", ".key", ".p12", ".pfx", ".crt", ".sqlite", ".sqlite3", ".db",
)
_UNSAFE_EXACT = {"config.toml", "cmd_config.json", "id_rsa", "id_ed25519"}
_UNSAFE_WORDS = ("secret", "password", "passwd", "credential", "private_key", "token")


def _sanitize_export_name(name):
    """提取纯文件名，去掉控制字符、路径片段并限制长度。"""
    raw = str(name or "").replace("\\", "/")
    base = Path(raw).name
    clean = re.sub(r"[\x00-\x1f\x7f/\\]+", "", base).strip()
    if len(clean) > 100:
        stem, suffix = Path(clean).stem, Path(clean).suffix
        if suffix and len(suffix) < 20:
            clean = stem[:100 - len(suffix)] + suffix
        else:
            clean = clean[:100]
    return clean


def _is_safe_export_name(name):
    """拦截隐藏文件、配置、私钥与数据库文件，防止越权导出。"""
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
    return True


def cleanup_outbox(outbox_root, max_age=OUTBOX_TTL_SECONDS):
    """清理过期的 outbox 任务目录，防止发送异常遗留文件长期占盘。"""
    if not outbox_root:
        return
    root = Path(outbox_root)
    if not root.is_dir():
        return
    cutoff = time.time() - max(0, int(max_age))
    for entry in list(root.iterdir()):
        try:
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
        self.timeout = int(timeout)

    def available(self):
        return bool(self.binary and self.home and os.path.exists(self.binary))

    def _prepare_dirs(self):
        """Codex 以 nobody 运行，只给它自己的配置目录和工作目录。"""
        for path in (Path(self.home), Path(self.workdir_root)):
            try:
                path.mkdir(parents=True, exist_ok=True)
                os.chown(path, NOBODY_UID, NOBODY_GID)
                os.chmod(path, 0o700)
            except OSError:
                pass
        if self.outbox_root:
            try:
                Path(self.outbox_root).mkdir(parents=True, exist_ok=True)
            except OSError:
                pass

    def _ensure_config(self):
        """按插件配置生成 Codex 的 provider 配置；key 只走环境变量，不落盘。"""
        if not self.home:
            return
        path = Path(self.home) / "config.toml"
        base = (self.api_base or "https://api.openai.com/v1").rstrip("/")
        model = self.model or "gpt-4o-mini"
        text = (
            'model = "%s"\n'
            'model_provider = "echo"\n\n'
            '[model_providers.echo]\n'
            'name = "echo"\n'
            'base_url = "%s"\n'
            'env_key = "ECHO_API_KEY"\n'
            'wire_api = "responses"\n'
        ) % (model, base)
        try:
            if path.exists() and path.read_text(encoding="utf-8") == text:
                return
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            os.chmod(path, 0o644)
        except OSError:
            pass

    def _workdir(self, group_id):
        """所有群共用一个目录，前缀稳定，缓存才容易命中。"""
        path = Path(self.workdir_root)
        path.mkdir(parents=True, exist_ok=True)
        try:
            os.chown(path, NOBODY_UID, NOBODY_GID)
            os.chmod(path, 0o700)
        except OSError:
            pass
        return path

    @staticmethod
    def _command(task):
        """降权到 nobody 再启动 Codex，避免群聊输入把容器 root 拿去做事。"""
        return [
            "/usr/bin/setpriv",
            "--reuid={}".format(NOBODY_UID),
            "--regid={}".format(NOBODY_GID),
            "--clear-groups",
            "codex", "exec", "--json", "--skip-git-repo-check",
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

    def _collect_outbox(self, outbox_dir, task_id):
        """从任务专属 ./outbox/ 中安全收集最多 MAX_OUTPUT_FILES 个合格文件。"""
        outbox_path = Path(outbox_dir)
        if not self.outbox_root or not outbox_path.is_dir():
            return []
        try:
            resolved_outbox = outbox_path.resolve()
        except OSError:
            return []

        candidates = []
        for entry in list(outbox_path.iterdir()):
            try:
                if entry.is_symlink():
                    continue
                st = entry.lstat()
                if not stat.S_ISREG(st.st_mode):
                    continue
                if entry.resolve().parent != resolved_outbox:
                    continue
                if not _is_safe_export_name(entry.name):
                    continue
                safe_name = _sanitize_export_name(entry.name)
            except OSError:
                continue
            if st.st_size <= 0 or st.st_size > MAX_FILE_BYTES:
                continue
            candidates.append((st.st_mtime, safe_name, entry, st.st_size))

        if not candidates:
            return []

        candidates.sort(key=lambda row: (-row[0], row[1]))
        dest_dir = Path(self.outbox_root) / task_id
        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            return []

        collected = []
        used_names = set()
        for _, safe_name, entry, size in candidates[:MAX_OUTPUT_FILES]:
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
                shutil.copyfile(entry, dest_file)
                os.chmod(dest_file, 0o644)
            except OSError:
                continue
            collected.append({"name": final_name, "path": str(dest_file), "size": size})
        return collected

    def run_with_files(self, group_id, task):
        """在独立临时目录跑 Codex，并收集 ./outbox/ 中安全合规的文件供回群发送。"""
        task = str(task or "").strip()
        if not task or not self.available():
            return {"text": "", "files": []}
        self._prepare_dirs()
        self._ensure_config()
        try:
            workdir = self._workdir(group_id)
        except OSError:
            return {"text": "", "files": []}

        task_id = "task-" + uuid.uuid4().hex[:10]
        task_dir = workdir / task_id
        outbox_dir = task_dir / "outbox"
        try:
            outbox_dir.mkdir(parents=True, exist_ok=True)
            for path in (task_dir, outbox_dir):
                try:
                    os.chown(path, NOBODY_UID, NOBODY_GID)
                    os.chmod(path, 0o700)
                except OSError:
                    pass
        except OSError:
            shutil.rmtree(task_dir, ignore_errors=True)
            return {"text": "", "files": []}

        env = dict(os.environ)
        env["CODEX_HOME"] = self.home
        env["HOME"] = self.home
        env["TMPDIR"] = str(task_dir)
        if self.api_key:
            env["ECHO_API_KEY"] = self.api_key
        cmd = self._command(task)
        try:
            proc = subprocess.Popen(
                cmd, cwd=str(task_dir), env=env, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, start_new_session=True,
            )
        except OSError:
            shutil.rmtree(task_dir, ignore_errors=True)
            return {"text": "", "files": []}

        timed_out = False
        stdout = ""
        collected = []
        try:
            stdout, _ = proc.communicate(timeout=self.timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            # Codex 还会起 bwrap / shell，必须杀整个进程组，避免遗留子进程。
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                proc.kill()
            proc.communicate()
        finally:
            # 无论正常退出还是超时，都杀掉残余进程组并清空本次任务目录；仅在未超时时回收 outbox 文件。
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError, OSError):
                    pass
            if not timed_out:
                collected = self._collect_outbox(outbox_dir, task_id)
            shutil.rmtree(task_dir, ignore_errors=True)
            cleanup_outbox(self.outbox_root)

        if timed_out:
            return {"text": "", "files": []}
        return {"text": parse_events(stdout, MAX_OUTPUT), "files": collected}

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

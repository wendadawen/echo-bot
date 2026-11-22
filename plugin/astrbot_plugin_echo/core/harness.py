"""Codex CLI harness：把任务交给 headless Codex，在独立目录里干活。

规划、工具循环、上下文压缩都由 Codex 负责；本项目只负责派任务、限时、
截断结果和失败兜底。二进制锁定在镜像里（见 Dockerfile），不联网安装。
每个群一个工作目录，Codex 只能在这个目录里读写，不碰仓库源码。
"""

import json
import os
import signal
import subprocess
from pathlib import Path

MAX_OUTPUT = 800
DEFAULT_TIMEOUT = 150
# 容器内核不允许非特权 user namespace，bwrap 的 workspace-write 用不了。
# 默认只读：能分析、能写文本，不能执行命令或改文件。
SANDBOX = "read-only"


class CodexHarness:
    def __init__(self, binary="/usr/local/bin/codex", home="", workdir_root="",
                 api_base="", api_key="", model="", timeout=DEFAULT_TIMEOUT):
        self.binary = str(binary or "")
        self.home = str(home or "")
        self.workdir_root = str(workdir_root or "")
        self.api_base = str(api_base or "")
        self.api_key = str(api_key or "")
        self.model = str(model or "")
        self.timeout = int(timeout)

    def available(self):
        return bool(self.binary and self.home and os.path.exists(self.binary))

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
        except OSError:
            pass

    def _workdir(self, group_id):
        safe = "".join(ch for ch in str(group_id) if ch.isalnum() or ch in "-_")
        path = Path(self.workdir_root) / (safe or "group")
        path.mkdir(parents=True, exist_ok=True)
        return path

    def run(self, group_id, task):
        task = str(task or "").strip()
        if not task or not self.available():
            return ""
        self._ensure_config()
        try:
            workdir = self._workdir(group_id)
        except OSError:
            return ""
        env = dict(os.environ)
        env["CODEX_HOME"] = self.home
        env["HOME"] = self.home
        if self.api_key:
            env["ECHO_API_KEY"] = self.api_key
        cmd = [self.binary, "exec", "--json", "--skip-git-repo-check",
               "--sandbox", SANDBOX, "--", task]
        try:
            proc = subprocess.Popen(
                cmd, cwd=str(workdir), env=env, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, start_new_session=True,
            )
        except OSError:
            return ""
        try:
            stdout, _ = proc.communicate(timeout=self.timeout)
        except subprocess.TimeoutExpired:
            # Codex 还会起 bwrap / shell，必须杀整个进程组，避免遗留子进程。
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                proc.kill()
            proc.communicate()
            return ""
        return parse_events(stdout, MAX_OUTPUT)


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

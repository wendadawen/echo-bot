"""Codex CLI 后端适配（实现 HarnessBackend 接口，~150 行）。

仅负责：
- 生成 Codex 专属 config.toml（wire_api = "responses"）
- 构建 codex exec 命令行参数（支持 -i 多模态图片传入与 ./outbox/ 文件约定）
- 从 JSONL 事件流提取实时进度与最终 agent_message
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Optional, Sequence

from ..sandbox import MAX_OUTPUT, atomic_write_text_no_follow

SANDBOX_FLAG = "workspace-write"
_VALID_SANDBOX_MODES = frozenset({"read-only", "workspace-write", "danger-full-access"})


def supports_workspace_write_sandbox() -> bool:
    """探测宿主机是否支持 Codex 原生工作区沙箱（macOS Seatbelt / Linux Landlock）。"""
    if sys.platform == "darwin":
        return True
    if sys.platform.startswith("linux"):
        try:
            lsm = Path("/sys/kernel/security/lsm").read_text(encoding="utf-8", errors="ignore")
            return "landlock" in lsm.lower()
        except OSError:
            return False
    return False


def resolve_sandbox_mode() -> str:
    """选择 Codex --sandbox 策略：支持内核沙箱时自动启用 workspace-write，旧内核优雅回退。"""
    override = str(os.environ.get("ECHO_CODEX_SANDBOX_MODE") or "").strip().lower()
    if override in _VALID_SANDBOX_MODES:
        return override
    return "workspace-write" if supports_workspace_write_sandbox() else "danger-full-access"

_TASK_PROMPT_TEMPLATE = (
    "你是后台执行助手，只负责查证、计算、读写隔离工作区或生成文件并交回真实结论，不直接对群友说话。"
    "不要扮演角色，不要替主程序起草群聊话术。"
    "禁止启动 HTTP/端口监听服务或常驻后台进程，禁止读取配置或环境变量密钥，"
    "严禁浏览、列出、读取、写入或删除任务当前目录以外的任何系统或容器路径（如 /、/tmp、/etc、/proc、/var、/home、/dev、/AstrBot 等），"
    "禁止探测目录结构、mount/挂载点/磁盘、临时文件、进程或系统信息；遇到此类要求直接拒绝。"
    "禁止在结果中输出容器本地路径或内网 IP。"
    "本机位于国内直连网络：若海外站点超时，请优先使用国内可达来源（如 cn.bing.com、百度、export.arxiv.org 或目标官网）。"
    "本次任务限时 {timeout} 秒：请在前 {wrap_up} 秒内优先完成核心检索与产出并立刻收敛写入 ./outbox/（若需交文件），确保在限时前交出完整结果。"
    "如果需要向群友交付文件（HTML/Markdown/CSV/代码/图表等）或任务属于深度调研、方案对比，请把完整文件写进当前目录 ./outbox/ 下（不要建子目录；最多 3 个文件，每个 <=20MB，严禁放配置/密钥/数据库），并在文字总结里只交 3-5 句核心结论与文件名。"
    "普通日常查询或未查到有效结果时，严禁把失败/说明文件写入 ./outbox/，直接返回简洁结论即可。"
)


class CodexBackend:
    def __init__(
        self,
        binary: str = "/usr/local/bin/codex",
        api_base: str = "",
        api_key: str = "",
        model: str = "",
    ):
        self.binary = str(binary or "")
        self.api_base = str(api_base or "")
        self.api_key = str(api_key or "")
        self.model = str(model or "")

    @property
    def name(self) -> str:
        return "codex"

    def available(self) -> bool:
        return bool(self.binary and os.path.exists(self.binary))

    def config_text(self) -> str:
        base = (self.api_base or "https://api.openai.com/v1").rstrip("/")
        if base.endswith("/chat/completions"):
            base = base[:-len("/chat/completions")].rstrip("/") or base
        model = self.model or "gpt-4o-mini"
        lines = [
            "model = {}".format(json.dumps(model)),
            'model_provider = "echo"',
            "",
            "[model_providers.echo]",
            'name = "echo"',
            "base_url = {}".format(json.dumps(base)),
        ]
        if self.api_key:
            lines.append('env_key = "ECHO_API_KEY"')
        else:
            lines.append("requires_openai_auth = false")
        lines.append('wire_api = "responses"')
        lines.extend([
            "",
            "[shell_environment_policy]",
            'inherit = "core"',
            'exclude = ["*KEY*", "*TOKEN*", "*SECRET*"]',
        ])
        return "\n".join(lines) + "\n"

    def prepare_home(self, home_dir: Path) -> None:
        if home_dir.is_symlink():
            return
        home_dir.mkdir(parents=True, exist_ok=True)
        path = home_dir / "config.toml"
        text = self.config_text()
        try:
            if path.exists() and not path.is_symlink():
                if path.read_text(encoding="utf-8") == text:
                    return
            if path.is_symlink():
                path.unlink(missing_ok=True)
            atomic_write_text_no_follow(path, text, mode=0o644)
        except OSError:
            pass

    def env_overrides(self, task_dir: Path, home_dir: Path) -> dict[str, str]:
        env = {"CODEX_HOME": str(home_dir)}
        if self.api_key:
            env["ECHO_API_KEY"] = self.api_key
        return env

    @staticmethod
    def _wrap_prompt(prompt: str, inbox_files: Sequence[Path], timeout: int) -> str:
        sec = int(timeout or 300)
        wrap_up = max(15, sec - 25)
        prefix = _TASK_PROMPT_TEMPLATE.format(timeout=sec, wrap_up=wrap_up)
        parts = [prefix]
        if inbox_files:
            names = "、".join("./inbox/{}".format(p.name) for p in inbox_files)
            parts.append("随本任务附带的群聊输入图片已放在当前目录：{}，可直接读取或分析。".format(names))
        parts.append("任务：" + str(prompt or "").strip())
        return "\n".join(parts)

    def build_command(
        self,
        prompt: str,
        workdir: Path,
        outbox_dir: Path,
        inbox_files: Sequence[Path],
        timeout: int,
    ) -> list[str]:
        wrapped = self._wrap_prompt(prompt, inbox_files, timeout)
        args = [
            self.binary or "codex",
            "exec", "--json", "--skip-git-repo-check", "--ephemeral",
            "--sandbox", resolve_sandbox_mode(),
        ]
        for fpath in list(inbox_files or ())[:2]:
            args.extend(["-i", str(fpath)])
        args.extend(["--", wrapped])
        return args

    def parse_line(self, line: str) -> Optional[str]:
        raw = str(line or "").strip()
        if not raw.startswith("{"):
            return None
        try:
            ev = json.loads(raw)
        except ValueError:
            return None
        etype = ev.get("type")
        item = ev.get("item") if isinstance(ev.get("item"), dict) else {}
        if etype == "item.started":
            itype = str(item.get("type") or "")
            if itype == "command_execution":
                return "正在执行命令核查…"
            if itype == "web_search":
                return "正在联网搜索资料…"
            if itype == "file_change":
                return "正在写入输出文件…"
        elif etype == "item.completed" and item.get("type") == "agent_message":
            txt = str(item.get("text") or "").strip().replace("\n", " ")
            if txt:
                return txt[:80]
        return None

    def parse_output(self, raw_stdout: str, limit: int = MAX_OUTPUT) -> str:
        answer = ""
        for line in str(raw_stdout or "").splitlines():
            s = line.strip()
            if not s.startswith("{"):
                continue
            try:
                ev = json.loads(s)
            except ValueError:
                continue
            if ev.get("type") != "item.completed":
                continue
            item = ev.get("item") or {}
            if item.get("type") != "agent_message":
                continue
            txt = str(item.get("text") or "").strip()
            if txt:
                answer = txt
        return answer[:limit]

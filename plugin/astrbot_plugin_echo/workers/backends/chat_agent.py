"""通用聊天模型后台执行后端（ChatAgentBackend，实现 HarnessBackend 接口）。

不依赖 Codex CLI，直接用 Python 标准库在 SandboxRunner 提供的 `nobody` 隔离工作区内，
通过标准 `/chat/completions` + 原生 `tools`（shell / 读文件 / 写文件 / 联网搜索）完成复杂任务；
天然兼容 Claude、GPT、Gemini、DeepSeek、Qwen 与本地 Ollama 等全部模型。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Optional, Sequence

from ..sandbox import MAX_OUTPUT, atomic_write_text_no_follow

_SEARCH_MODULE_PATH = Path(__file__).resolve().parents[2] / "adapters" / "search.py"

_TASK_SYSTEM_PROMPT = (
    "你是运行在隔离工作区中的后台执行助手，负责查证资料、执行计算/代码、分析 `./inbox/` 中的图片或生成文件并写进 `./outbox/`。\n"
    "安全铁律：\n"
    "1. 只能访问和修改当前工作目录（及 `./inbox/` 只读输入、`./outbox/` 导出目录）；严禁探测、列出、读取或写入容器系统路径（`/`, `/etc`, `/proc`, `/var`, `/home`, `/AstrBot`, `/tmp` 等）、`mount` 挂载点、进程列表或环境变量。\n"
    "2. 禁止启动常驻服务、监听端口、后台守护进程或死循环；遇到越界要求直接拒绝。\n"
    "3. 需要交付 HTML / Markdown / CSV / 代码 / 图表等文件时，写入 `./outbox/文件名`（不要建子目录，最多 3 个文件，严禁放配置/密钥/数据库）。\n"
    "4. 查实时事实优先调 `web_search`（主引擎限流或无额度会自动按 Bing CN → 360 → Bing 国际接力兜底）；普通简单查询没查到有效信息时不要生成无意义的说明文件。\n"
    "5. 最终回复仅交出 3-5 句准确结论与生成的文件名（如有），不要输出容器本地路径，不要编造未核实的数据。"
)

_AGENT_LOOP_SCRIPT = r'''#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""独立沙箱内 Tool-Calling 执行循环（零第三方依赖，运行于 setpriv nobody 环境）。"""

import argparse
import base64
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import echo_search

SYSTEM_PROMPT = __SYSTEM_PROMPT_JSON__
MAX_STEPS = 12
MAX_RESP_BYTES = 2 * 1024 * 1024
FORBIDDEN_CMD_RE = re.compile(
    r"(?:/etc/passwd|/etc/shadow|/(?:AstrBot|proc|sys|root)(?:/|\b)|\b(?:mount|findmnt|lsblk|printenv)\b|rm\s+-[a-z]*rf\s+/)",
    re.I,
)
THINK_TAG_RE = re.compile(r"^\s*(?:<(?:think|thinking|analysis)>.*?</(?:think|thinking|analysis)>\s*)+", re.I | re.S)
THINK_HEADER_RE = re.compile(
    r"^\s*(?:#{1,6}\s*)?(?:\*\*)?(?:thinking\s+process|thought\s+process|思考过程|思维链)(?:\*\*)?\s*[:：].*?\n\s*\n+",
    re.I | re.S,
)

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "run_shell",
            "description": "在当前隔离工作目录内执行命令或 Python 脚本（用于计算、数据处理、生成图表或验证结果）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "要执行的 shell 命令"},
                    "timeout_sec": {"type": "integer", "description": "超时秒数（默认 30，最大 90）"},
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "读取当前工作目录内的相对路径文本文件。",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "相对路径，如 notes.txt 或 outbox/a.md"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "向当前工作目录写入相对路径文本文件（要交付给群友的文件请写到 outbox/文件名）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "相对路径，如 outbox/report.html"},
                    "content": {"type": "string", "description": "完整 UTF-8 文本内容"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "联网搜索实时新闻、资料或官方文档（自动在主引擎不可用时按 Bing CN / 360 / Bing 国际接力兜底）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "搜索关键词"},
                    "limit": {"type": "integer", "description": "返回条数（1-8）"},
                },
                "required": ["query"],
            },
        },
    },
]


def emit(obj):
    print(json.dumps(obj, ensure_ascii=False), flush=True)


def emit_progress(text):
    emit({"type": "progress", "text": str(text)[:80]})


def emit_answer(text):
    emit({"type": "item.completed", "item": {"type": "agent_message", "text": str(text or "").strip()}})


def strip_thinking(text):
    s = str(text or "").strip()
    if not s:
        return ""
    stripped = THINK_TAG_RE.sub("", s).strip()
    if stripped:
        s = stripped
    header_stripped = THINK_HEADER_RE.sub("", s).strip()
    return header_stripped or s


def image_to_data_uri(path):
    raw = Path(path).read_bytes()
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        mime = "image/png"
    elif raw.startswith(b"\xff\xd8\xff"):
        mime = "image/jpeg"
    elif raw.startswith((b"GIF87a", b"GIF89a")):
        mime = "image/gif"
    elif len(raw) >= 12 and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        mime = "image/webp"
    else:
        mime = "image/png"
    return "data:{};base64,{}".format(mime, base64.b64encode(raw).decode("ascii"))


def safe_rel_path(root, raw_path, for_write=False):
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
    cur = root
    for part in p.parts:
        cur = cur / part
        if cur.is_symlink():
            raise ValueError("禁止访问符号链接")
    resolved = (root / p).resolve(strict=False)
    if resolved != root and root not in resolved.parents:
        raise ValueError("路径越界")
    return resolved


def run_shell_tool(root, command, timeout_sec, deadline):
    cmd = str(command or "").strip()
    if not cmd:
        return "错误：空命令"
    if FORBIDDEN_CMD_RE.search(cmd):
        return "拒绝执行：触发沙箱安全策略，禁止访问工作区以外的系统目录或环境信息。"
    rem = max(3, int(deadline - time.monotonic()))
    sec = min(max(3, int(timeout_sec or 30)), 90, rem)
    shell_bin = shutil.which("bash") or "/bin/sh"
    proc = subprocess.Popen(
        [shell_bin, "-c", cmd],
        cwd=str(root),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    try:
        out, _ = proc.communicate(timeout=sec)
        rc = proc.returncode
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            proc.kill()
        try:
            out, _ = proc.communicate(timeout=3)
        except Exception:
            out = ""
        return "命令超时（{}s）：\n{}".format(sec, str(out or "")[-3000:])
    body = str(out or "").strip()
    if len(body) > 6000:
        body = body[:3000] + "\n...（已截断）...\n" + body[-2500:]
    return "exit={}\n{}".format(rc, body) if body else "exit={}".format(rc)


def call_llm(url, api_key, model, messages, use_tools=True, timeout_sec=60):
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = "Bearer " + api_key
    payload = {"model": model, "messages": messages}
    if use_tools:
        payload["tools"] = TOOLS
        payload["tool_choice"] = "auto"
    req = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=max(10, min(90, int(timeout_sec)))) as resp:
        raw = resp.read(MAX_RESP_BYTES + 1)
        if len(raw) > MAX_RESP_BYTES:
            raise RuntimeError("模型响应超过体积上限")
        return json.loads(raw.decode("utf-8"))


def main():
    # 先把密钥移出环境变量，防止任何子进程命令通过 env/printenv 读到
    api_base = os.environ.pop("ECHO_API_BASE", "").strip()
    api_key = os.environ.pop("ECHO_API_KEY", "").strip()
    model = os.environ.pop("ECHO_MODEL", "gpt-4o-mini").strip()
    search_provider = os.environ.pop("ECHO_SEARCH_PROVIDER", "auto").strip().lower()
    search_base = os.environ.pop("ECHO_SEARCH_BASE", "").strip().rstrip("/")
    search_key = os.environ.pop("ECHO_SEARCH_KEY", "").strip()

    parser = argparse.ArgumentParser()
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--image", action="append", default=[])
    parser.add_argument("--prompt", required=True)
    args = parser.parse_args()

    clean_base = api_base.rstrip("/")
    chat_url = clean_base if clean_base.endswith("/chat/completions") else clean_base + "/chat/completions"
    root = Path.cwd().resolve()
    eff_provider = (
        search_provider
        if ((search_provider and search_provider != "auto") or search_base or search_key)
        else "free"
    )
    searcher = echo_search.HttpSearchProvider(
        provider=eff_provider,
        api_base=search_base,
        api_key=search_key,
    )

    started = time.monotonic()
    deadline = started + max(30, int(args.timeout or 300))

    if args.image:
        blocks = [{"type": "text", "text": args.prompt}]
        for img_path in args.image[:2]:
            try:
                blocks.append({"type": "image_url", "image_url": {"url": image_to_data_uri(img_path)}})
            except OSError:
                pass
        first_user = {"role": "user", "content": blocks if len(blocks) > 1 else args.prompt}
    else:
        first_user = {"role": "user", "content": args.prompt}

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        first_user,
    ]
    emit_progress("正在分析任务目标…")

    final_text = ""
    use_tools = True
    wrap_nudged = False

    for step in range(MAX_STEPS):
        rem = deadline - time.monotonic()
        if rem < 8:
            break
        if not wrap_nudged and (step >= MAX_STEPS - 2 or rem < 30):
            wrap_nudged = True
            messages.append({
                "role": "system",
                "content": "剩余时间有限，请立刻停止继续检索，如需交付文件请马上写进 ./outbox/ 并输出简洁最终总结。",
            })
        try:
            data = call_llm(chat_url, api_key, model, messages, use_tools=use_tools, timeout_sec=min(85, rem - 2))
        except urllib.error.HTTPError as exc:
            err_body = ""
            try:
                err_body = exc.read(1024).decode("utf-8", errors="ignore").lower()
            except Exception:
                pass
            if exc.code in (400, 422):
                if isinstance(messages[1].get("content"), list) and any(k in err_body for k in ("image", "vision", "multimodal")):
                    messages[1] = {"role": "user", "content": args.prompt}
                    continue
                if use_tools and any(k in err_body for k in ("tool", "function", "tool_choice")):
                    use_tools = False
                    continue
            raise RuntimeError("HTTP {}: {}".format(exc.code, err_body[:200]))

        choices = data.get("choices") or []
        if not choices:
            break
        msg = choices[0].get("message") or {}
        tool_calls = msg.get("tool_calls") or []
        text = strip_thinking(msg.get("content") or "")

        if not tool_calls:
            final_text = text
            break

        messages.append({
            "role": "assistant",
            "content": msg.get("content") or "",
            "tool_calls": tool_calls,
        })

        for tc in tool_calls:
            fn = (tc.get("function") or {}) if isinstance(tc, dict) else {}
            tname = str(fn.get("name") or "").strip()
            try:
                targs = json.loads(str(fn.get("arguments") or "{}"))
                if not isinstance(targs, dict):
                    targs = {}
            except ValueError:
                targs = {}

            if tname == "run_shell":
                emit_progress("正在执行脚本处理…")
                result = run_shell_tool(root, targs.get("command", ""), targs.get("timeout_sec", 30), deadline)
            elif tname == "read_file":
                emit_progress("正在读取工作区文件…")
                try:
                    p = safe_rel_path(root, targs.get("path", ""), for_write=False)
                    result = p.read_text(encoding="utf-8", errors="replace")[:12000]
                except Exception as exc:
                    result = "读取失败：{}".format(exc)
            elif tname == "write_file":
                emit_progress("正在写入输出文件…")
                try:
                    p = safe_rel_path(root, targs.get("path", ""), for_write=True)
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_text(str(targs.get("content") or ""), encoding="utf-8")
                    result = "已写入 {}".format(p.relative_to(root))
                except Exception as exc:
                    result = "写入失败：{}".format(exc)
            elif tname == "web_search":
                emit_progress("正在联网搜索资料…")
                hits = searcher.search(targs.get("query", ""), targs.get("limit", 5))
                result = "\n".join(
                    "- {}：{} ({})".format(h.title, h.snippet, h.url) for h in hits
                ) or "未检索到可用结果"
            else:
                result = "未知工具：{}".format(tname)

            messages.append({
                "role": "tool",
                "tool_call_id": str(tc.get("id") or "call_0"),
                "content": result,
            })

    emit_answer(final_text or "已完成处理，请查看结果或工作区输出文件。")


if __name__ == "__main__":
    main()
'''


class ChatAgentBackend:
    """通用 OpenAI `/chat/completions` 后台执行后端（兼容 Claude / GPT / Gemini / Qwen 等全部模型）。"""

    def __init__(
        self,
        api_base: str = "",
        api_key: str = "",
        model: str = "",
        *,
        search_provider: str = "",
        search_api_base: str = "",
        search_api_key: str = "",
        python_binary: str = "",
    ):
        self.api_base = str(api_base or "").strip()
        self.api_key = str(api_key or "").strip()
        self.model = str(model or "gpt-4o-mini").strip()
        self.search_provider = str(search_provider or "").strip()
        self.search_api_base = str(search_api_base or "").strip()
        self.search_api_key = str(search_api_key or "").strip()
        self.python_binary = str(python_binary or sys.executable or "/usr/bin/python3")

    @property
    def name(self) -> str:
        return "chat_agent"

    def available(self) -> bool:
        return bool(
            self.python_binary
            and os.path.exists(self.python_binary)
            and self.api_base.startswith(("http://", "https://"))
        )

    @staticmethod
    def _script_text() -> str:
        return _AGENT_LOOP_SCRIPT.replace("__SYSTEM_PROMPT_JSON__", json.dumps(_TASK_SYSTEM_PROMPT, ensure_ascii=True))

    def prepare_home(self, home_dir: Path) -> None:
        if home_dir.is_symlink():
            return
        home_dir.mkdir(parents=True, exist_ok=True)
        search_dst = home_dir / "echo_search.py"
        if search_dst.is_symlink():
            search_dst.unlink(missing_ok=True)
        atomic_write_text_no_follow(search_dst, _SEARCH_MODULE_PATH.read_text(encoding="utf-8"), mode=0o644)
        script_path = home_dir / "agent_loop.py"
        if script_path.is_symlink():
            script_path.unlink(missing_ok=True)
        atomic_write_text_no_follow(script_path, self._script_text(), mode=0o644)

    def env_overrides(self, task_dir: Path, home_dir: Path) -> dict[str, str]:
        env: dict[str, str] = {
            "ECHO_API_BASE": self.api_base,
            "ECHO_MODEL": self.model,
        }
        if self.api_key:
            env["ECHO_API_KEY"] = self.api_key
        if self.search_provider:
            env["ECHO_SEARCH_PROVIDER"] = self.search_provider
        if self.search_api_base:
            env["ECHO_SEARCH_BASE"] = self.search_api_base
        if self.search_api_key:
            env["ECHO_SEARCH_KEY"] = self.search_api_key
        return env

    def build_command(
        self,
        prompt: str,
        workdir: Path,
        outbox_dir: Path,
        inbox_files: Sequence[Path],
        timeout: int,
    ) -> list[str]:
        script = str(workdir / ".chat_agent" / "agent_loop.py")
        args = [
            self.python_binary,
            "-u",
            script,
            "--timeout",
            str(int(timeout or 300)),
        ]
        for fpath in list(inbox_files or ())[:2]:
            args.extend(["--image", str(fpath)])
        wrapped_prompt = str(prompt or "").strip()
        if inbox_files:
            names = "、".join("./inbox/{}".format(p.name) for p in inbox_files)
            wrapped_prompt = "随本任务附带的群聊图片位于 {}。\n{}".format(names, wrapped_prompt)
        args.extend(["--prompt", wrapped_prompt])
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
        if etype == "progress":
            txt = str(ev.get("text") or "").strip()
            return txt[:80] if txt else None
        if etype == "item.completed":
            item = ev.get("item") if isinstance(ev.get("item"), dict) else {}
            if item.get("type") == "agent_message":
                txt = str(item.get("text") or "").strip().replace("\n", " ")
                return txt[:80] if txt else None
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

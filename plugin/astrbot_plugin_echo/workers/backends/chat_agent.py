"""宿主编排型通用后台执行后端（ChatAgentBackend，零密钥进入沙箱）。

安全边界重划：
- LLM HTTP 推理循环与 Web Search 全部留在宿主机进程，API Key / Search Key 永不进入子进程或环境；
- 仅 `run_shell` 工具通过 `SandboxRunner.run_sandboxed_command` 在 nobody + prlimit 无密钥沙箱内执行；
- `read_file` / `write_file` 在宿主机做符号链接与相对路径越界拦截后原子读写，彻底复用 `strip_thinking` 与 `HttpSearchProvider`。
"""

from __future__ import annotations

import base64
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from ...adapters.llm import strip_thinking
from ...adapters.search import HttpSearchProvider
from ...domain import TaskResult, detect_image_format
from ..fs_guard import read_workspace_file, safe_rel_path, write_workspace_file
from ..sandbox import MAX_OUTPUT

MAX_STEPS = 12
MAX_RESP_BYTES = 2 * 1024 * 1024
_FORBIDDEN_CMD_RE = re.compile(
    r"(?:/etc/passwd|/etc/shadow|/(?:AstrBot|proc|sys|root)(?:/|\b)|\b(?:mount|findmnt|lsblk|printenv)\b|rm\s+-[a-z]*rf\s+/)",
    re.I,
)

_TASK_SYSTEM_PROMPT = (
    "你是运行在隔离工作区中的后台执行助手，负责查证资料、执行计算/代码、分析 `./inbox/` 中的图片或生成文件并写进 `./outbox/`。\n"
    "安全铁律：\n"
    "1. 只能访问和修改当前工作目录（及 `./inbox/` 只读输入、`./outbox/` 导出目录）；严禁探测、列出、读取或写入容器系统路径（`/`, `/etc`, `/proc`, `/var`, `/home`, `/AstrBot`, `/tmp` 等）、`mount` 挂载点、进程列表或环境变量。\n"
    "2. 禁止启动常驻服务、监听端口、后台守护进程或死循环；遇到越界要求直接拒绝。\n"
    "3. 需要交付 HTML / Markdown / CSV / 代码 / 图表等文件时，写入 `./outbox/文件名`（不要建子目录，最多 3 个文件，严禁放配置/密钥/数据库）。\n"
    "4. 查实时事实优先调 `web_search`（主引擎限流或无额度会自动按 Bing CN → 360 → Bing 国际接力兜底）；普通简单查询没查到有效信息时不要生成无意义的说明文件。\n"
    "5. 最终回复仅交出 3-5 句准确结论与生成的文件名（如有），不要输出容器本地路径，不要编造未核实的数据。"
)


def _tool_spec(name: str, desc: str, props: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "function", "function": {"name": name, "description": desc, "parameters": {"type": "object", "properties": props, "required": required}}}


TOOLS = [
    _tool_spec("run_shell", "在当前无密钥隔离工作目录内执行命令或 Python 脚本（计算、数据处理、生成图表或验证结果）。",
               {"command": {"type": "string", "description": "要执行的 shell 命令"}, "timeout_sec": {"type": "integer", "description": "超时秒数（默认30，最大90）"}}, ["command"]),
    _tool_spec("read_file", "读取当前工作目录内的相对路径文本文件。", {"path": {"type": "string", "description": "相对路径，如 notes.txt 或 outbox/a.md"}}, ["path"]),
    _tool_spec("write_file", "向当前工作目录写入相对路径文本文件（交付给群友的文件写到 outbox/文件名）。",
               {"path": {"type": "string", "description": "相对路径，如 outbox/report.html"}, "content": {"type": "string", "description": "完整 UTF-8 文本内容"}}, ["path", "content"]),
    _tool_spec("web_search", "联网搜索实时新闻、资料或官方文档（自动按主引擎→Bing CN/360/Bing 国际兜底）。",
               {"query": {"type": "string", "description": "搜索关键词"}, "limit": {"type": "integer", "description": "返回条数（1-8）"}}, ["query"]),
]


class _HttpCallError(Exception):
    def __init__(self, code: int, body: str) -> None:
        super().__init__("HTTP {}: {}".format(code, body[:200]))
        self.code = int(code)
        self.body = body


_read_file_tool = read_workspace_file
_write_file_tool = write_workspace_file


def _image_to_data_uri(path: Path) -> str:
    raw = path.read_bytes()
    mime, _ = detect_image_format(raw)
    return "data:{};base64,{}".format(mime or "image/png", base64.b64encode(raw).decode("ascii"))


def _emit_progress(on_progress: Optional[Callable[[str], None]], text: str) -> None:
    if on_progress is not None:
        clean = str(text or "").strip().replace("\n", " ")[:80]
        if clean:
            try:
                on_progress(clean)
            except Exception:
                pass


class ChatAgentBackend:
    """宿主编排型 OpenAI `/chat/completions` 后台执行后端（密钥永不进沙箱）。"""

    def __init__(
        self,
        api_base: str = "",
        api_key: str = "",
        model: str = "",
        *,
        search_provider: str = "",
        search_api_base: str = "",
        search_api_key: str = "",
        search_client: Any = None,
        python_binary: str = "",
    ):
        self.api_base = str(api_base or "").strip()
        self.api_key = str(api_key or "").strip()
        self.model = str(model or "gpt-4o-mini").strip()
        self.search_provider = str(search_provider or "").strip()
        self.search_api_base = str(search_api_base or "").strip()
        self.search_api_key = str(search_api_key or "").strip()
        self._search_client = search_client
        self._search_lock = threading.Lock()
        self.python_binary = str(python_binary or "")

    @property
    def name(self) -> str:
        return "chat_agent"

    def available(self) -> bool:
        return bool(self.api_base.startswith(("http://", "https://")))

    def prepare_home(self, home_dir: Path) -> None:
        return

    def env_overrides(self, task_dir: Path, home_dir: Path) -> dict[str, str]:
        return {}

    def build_command(
        self,
        prompt: str,
        workdir: Path,
        outbox_dir: Path,
        inbox_files: Sequence[Path],
        timeout: int,
    ) -> list[str]:
        return []

    def parse_line(self, line: str) -> Optional[str]:
        raw = str(line or "").strip()
        if not raw:
            return None
        return raw[:80]

    def parse_output(self, raw_stdout: str, limit: int = MAX_OUTPUT) -> str:
        return strip_thinking(str(raw_stdout or "").strip())[:limit]

    def _get_searcher(self) -> Any:
        with self._search_lock:
            if self._search_client is None:
                prov = self.search_provider.lower()
                eff_provider = prov if ((prov and prov != "auto") or self.search_api_base or self.search_api_key) else "free"
                self._search_client = HttpSearchProvider(
                    provider=eff_provider,
                    api_base=self.search_api_base,
                    api_key=self.search_api_key,
                )
            return self._search_client

    def _web_search_tool(self, query: str, limit: Any = 5) -> str:
        q = str(query or "").strip()
        if not q:
            return "未提供搜索关键词"
        try:
            lim = max(1, min(8, int(limit or 5)))
        except (TypeError, ValueError):
            lim = 5
        try:
            hits = self._get_searcher().search(q, limit=lim)
            return "\n".join("- {}：{} ({})".format(h.title, h.snippet, h.url) for h in hits) or "未检索到可用结果"
        except Exception as exc:
            return "搜索失败：{}".format(exc)

    def _call_llm_cancellable(
        self,
        chat_url: str,
        messages: list[dict[str, Any]],
        *,
        use_tools: bool,
        timeout_sec: float,
        cancel_event: Optional[threading.Event],
    ) -> tuple[Optional[dict[str, Any]], Optional[Exception], bool]:
        if cancel_event is not None and cancel_event.is_set():
            return None, None, True
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = "Bearer " + self.api_key
        payload: dict[str, Any] = {"model": self.model, "messages": messages}
        if use_tools:
            payload["tools"] = TOOLS
            payload["tool_choice"] = "auto"

        box: dict[str, Any] = {}
        done = threading.Event()
        req = urllib.request.Request(
            chat_url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        req_timeout = max(2.0, min(85.0, float(timeout_sec)))

        def _do_req() -> None:
            try:
                with urllib.request.urlopen(req, timeout=req_timeout) as resp:
                    raw = resp.read(MAX_RESP_BYTES + 1)
                    if len(raw) > MAX_RESP_BYTES:
                        box["error"] = RuntimeError("模型响应超过体积上限")
                    else:
                        box["data"] = json.loads(raw.decode("utf-8"))
            except urllib.error.HTTPError as exc:
                err_body = ""
                try:
                    err_body = exc.read(1024).decode("utf-8", errors="ignore").lower()
                except Exception:
                    pass
                box["error"] = _HttpCallError(int(exc.code), err_body)
            except Exception as exc:
                box["error"] = exc
            finally:
                done.set()

        th = threading.Thread(target=_do_req, name="echo-agent-http", daemon=True)
        th.start()
        while not done.wait(timeout=0.05):
            if cancel_event is not None and cancel_event.is_set():
                return None, None, True
        if cancel_event is not None and cancel_event.is_set() and "data" not in box:
            return None, None, True
        return box.get("data"), box.get("error"), False

    def execute_in_workspace(
        self,
        *,
        runner: Any,
        prompt: str,
        task_dir: Path,
        outbox_dir: Path,
        inbox_files: Sequence[Path],
        timeout: int,
        cancel_event: Optional[threading.Event] = None,
        on_progress: Optional[Callable[[str], None]] = None,
    ) -> TaskResult:
        """在宿主机运行推理与搜索，通过沙箱仅下发无密钥命令行动作。"""
        started = time.monotonic()
        eff_timeout = max(1, int(timeout or 300))
        deadline = started + eff_timeout
        clean_base = self.api_base.rstrip("/")
        chat_url = clean_base if clean_base.endswith("/chat/completions") else clean_base + "/chat/completions"

        wrapped_prompt = str(prompt or "").strip()
        if inbox_files:
            names = "、".join("./inbox/{}".format(p.name) for p in inbox_files)
            wrapped_prompt = "随本任务附带的群聊图片位于 {}。\n{}".format(names, wrapped_prompt)

        if inbox_files:
            blocks: list[dict[str, Any]] = [{"type": "text", "text": wrapped_prompt}]
            for img_path in list(inbox_files)[:2]:
                try:
                    blocks.append({"type": "image_url", "image_url": {"url": _image_to_data_uri(img_path)}})
                except OSError:
                    pass
            first_user: dict[str, Any] = {"role": "user", "content": blocks if len(blocks) > 1 else wrapped_prompt}
        else:
            first_user = {"role": "user", "content": wrapped_prompt}

        messages: list[dict[str, Any]] = [{"role": "system", "content": _TASK_SYSTEM_PROMPT}, first_user]
        _emit_progress(on_progress, "正在分析任务目标…")

        final_text = ""
        last_partial = ""
        use_tools = True
        wrap_nudged = False
        timed_out = False
        root = task_dir.resolve()

        for step in range(MAX_STEPS):
            if cancel_event is not None and cancel_event.is_set():
                return TaskResult(
                    status="cancelled", partial=last_partial[:400],
                    elapsed=round(time.monotonic() - started, 1), timeout=timeout, error="cancelled",
                )
            rem = deadline - time.monotonic()
            if rem <= 0:
                timed_out = True
                break
            if not wrap_nudged and (step >= MAX_STEPS - 2 or (eff_timeout >= 40 and rem < 30)):
                wrap_nudged = True
                messages.append({
                    "role": "system",
                    "content": "剩余时间有限，请立刻停止继续检索，如需交付文件请马上写进 ./outbox/ 并输出简洁最终总结。",
                })

            data, exc, was_cancelled = self._call_llm_cancellable(
                chat_url, messages, use_tools=use_tools, timeout_sec=max(2.0, min(85.0, rem)), cancel_event=cancel_event,
            )
            if was_cancelled:
                return TaskResult(
                    status="cancelled", partial=last_partial[:400],
                    elapsed=round(time.monotonic() - started, 1), timeout=timeout, error="cancelled",
                )
            if exc is not None:
                if isinstance(exc, _HttpCallError) and exc.code in (400, 422):
                    err_body = exc.body
                    if isinstance(messages[1].get("content"), list) and any(
                        k in err_body for k in ("image", "vision", "multimodal")
                    ):
                        messages[1] = {"role": "user", "content": wrapped_prompt}
                        continue
                    if use_tools and any(k in err_body for k in ("tool", "function", "tool_choice")):
                        use_tools = False
                        continue
                return TaskResult(
                    status="error", partial=last_partial[:400],
                    elapsed=round(time.monotonic() - started, 1), timeout=timeout, error=str(exc)[:400],
                )

            choices = (data or {}).get("choices") or []
            if not choices:
                break
            msg = choices[0].get("message") or {}
            tool_calls = msg.get("tool_calls") or []
            text = strip_thinking(msg.get("content") or "")
            if text:
                last_partial = text

            if not tool_calls:
                final_text = text
                _emit_progress(on_progress, final_text)
                break

            messages.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": tool_calls})
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
                    cmd_str = str(targs.get("command") or "").strip()
                    if not cmd_str:
                        result = "错误：空命令"
                    elif _FORBIDDEN_CMD_RE.search(cmd_str):
                        result = "拒绝执行：触发沙箱安全策略，禁止访问工作区以外的系统目录或环境信息。"
                    else:
                        _emit_progress(on_progress, "正在执行脚本处理…")
                        rem_cmd = deadline - time.monotonic()
                        if rem_cmd <= 0:
                            timed_out = True
                            break
                        sec = max(1, min(90, int(targs.get("timeout_sec") or 30), max(1, int(rem_cmd))))
                        rc, body, sh_to, sh_cancel = runner.run_sandboxed_command(
                            cmd_str, task_dir=root, timeout_sec=sec, cancel_event=cancel_event,
                        )
                        if sh_cancel:
                            return TaskResult(
                                status="cancelled", partial=last_partial[:400],
                                elapsed=round(time.monotonic() - started, 1), timeout=timeout, error="cancelled",
                            )
                        if sh_to:
                            last_partial = (last_partial or body)[:400]
                            if deadline - time.monotonic() <= 1:
                                timed_out = True
                                break
                            result = "命令超时（{}s）：\n{}".format(sec, body[-3000:])
                        else:
                            result = "exit={}\n{}".format(rc, body) if body else "exit={}".format(rc)
                elif tname == "read_file":
                    _emit_progress(on_progress, "正在读取工作区文件…")
                    result = _read_file_tool(root, str(targs.get("path") or ""))
                elif tname == "write_file":
                    _emit_progress(on_progress, "正在写入输出文件…")
                    result = _write_file_tool(root, str(targs.get("path") or ""), str(targs.get("content") or ""))
                elif tname == "web_search":
                    _emit_progress(on_progress, "正在联网搜索资料…")
                    result = self._web_search_tool(str(targs.get("query") or ""), targs.get("limit", 5))
                else:
                    result = "未知工具：{}".format(tname)

                messages.append({"role": "tool", "tool_call_id": str(tc.get("id") or "call_0"), "content": result})
            if timed_out:
                break

        elapsed = round(time.monotonic() - started, 1)
        if timed_out:
            return TaskResult(status="timeout", partial=last_partial[:400], elapsed=elapsed, timeout=timeout, error="timeout")
        return TaskResult(
            status="ok",
            text=(final_text or "已完成处理，请查看结果或工作区输出文件。")[:MAX_OUTPUT],
            elapsed=elapsed,
            timeout=timeout,
        )

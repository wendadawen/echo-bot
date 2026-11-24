"""workers 层测试：安全沙箱隔离、降权/进程组强杀、CodexBackend 与 TaskManager 生命周期。"""

import json
import os
import signal
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = ROOT / "plugin"
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from astrbot_plugin_echo.domain import LLMMessage, ModelTurn, PendingDelivery, TaskRequest  # noqa: E402
from astrbot_plugin_echo.store.repository import Repository  # noqa: E402
from astrbot_plugin_echo.workers.backends import ChatAgentBackend, CodexBackend, select_backend  # noqa: E402
from astrbot_plugin_echo.workers.manager import TaskManager  # noqa: E402
from astrbot_plugin_echo.workers.sandbox import (  # noqa: E402
    DEFAULT_TIMEOUT,
    MAX_TIMEOUT,
    MIN_TIMEOUT,
    SandboxRunner,
    atomic_write_text_no_follow,
    cleanup_outbox,
    contains_secret_bytes,
    is_safe_export_name,
    parse_timeout,
    sanitize_export_name,
)

TINY_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


class _FakePythonBackend:
    """使用当前 Python 解释器模拟可控子进程 CLI，不依赖真实 codex 可执行文件。"""

    def __init__(self, script_code: str, name: str = "fake"):
        self._code = script_code
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    def available(self) -> bool:
        return True

    def prepare_home(self, home_dir: Path) -> None:
        home_dir.mkdir(parents=True, exist_ok=True)
        (home_dir / "runner.py").write_text(self._code, encoding="utf-8")

    def env_overrides(self, task_dir: Path, home_dir: Path) -> dict[str, str]:
        return {"CODEX_HOME": str(home_dir)}

    def build_command(self, prompt, workdir: Path, outbox_dir: Path, inbox_files, timeout: int) -> list[str]:
        script = str(workdir / f".{self._name}" / "runner.py")
        cmd = [sys.executable, "-u", script, "--"]
        for p in inbox_files:
            cmd.append(str(p))
        cmd.append(prompt)
        return cmd

    def parse_line(self, line: str):
        s = str(line or "").strip()
        if s.startswith("PROGRESS:"):
            return s.split(":", 1)[1].strip()
        return None

    def parse_output(self, raw_stdout: str, limit: int = 1200) -> str:
        ans = ""
        for line in str(raw_stdout or "").splitlines():
            if line.startswith("ANSWER:"):
                ans = line.split(":", 1)[1].strip()
        return ans[:limit]


class _FakeLLM:
    def __init__(self, reply: str = "这是为您整理好的结果："):
        self.reply = reply
        self.calls = []

    def chat(self, messages, tools=(), timeout=None):
        self.calls.append(messages)
        return ModelTurn(text=self.reply)

    def describe_image(self, image_bytes, prompt, timeout=None):
        return ""

    def extra_secrets(self):
        return ()


class SandboxSecurityTests(unittest.TestCase):
    def test_timeout_export_name_and_secret_filters(self):
        self.assertEqual(parse_timeout(None), DEFAULT_TIMEOUT)
        self.assertEqual(parse_timeout(True), DEFAULT_TIMEOUT)
        self.assertEqual(parse_timeout("abc"), DEFAULT_TIMEOUT)
        self.assertEqual(parse_timeout(10), MIN_TIMEOUT)
        self.assertEqual(parse_timeout(9999), MAX_TIMEOUT)
        self.assertEqual(parse_timeout("240"), 240)

        self.assertEqual(sanitize_export_name("../../evil\u202ereport.md"), "evilreport.md")
        self.assertTrue(is_safe_export_name("report.html"))
        self.assertTrue(is_safe_export_name("chart.png"))
        self.assertFalse(is_safe_export_name(".env"))
        self.assertFalse(is_safe_export_name("secrets.txt"))
        self.assertFalse(is_safe_export_name("data.sqlite3"))
        self.assertFalse(is_safe_export_name("config.toml"))
        self.assertFalse(is_safe_export_name("run.sh"))
        self.assertFalse(is_safe_export_name("meta.json"))

        self.assertTrue(contains_secret_bytes(b"SQLite format 3\x00"))
        self.assertTrue(contains_secret_bytes(b"-----BEGIN RSA PRIVATE KEY-----\nabc"))
        self.assertTrue(contains_secret_bytes(b"key=sk-1234567890abcdef123456"))
        self.assertTrue(contains_secret_bytes(b"leaked super-secret-value-99", extra_secrets=("super-secret-value-99",)))
        self.assertFalse(contains_secret_bytes(b"<h1>hello</h1>", extra_secrets=("super-secret-value-99",)))

    def test_atomic_write_no_follow_and_wrap_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "real.txt"
            target.write_text("old", encoding="utf-8")
            link = root / "link.txt"
            link.symlink_to(target)
            with self.assertRaises(OSError):
                atomic_write_text_no_follow(link, "new")

            sdir = root / "sdir"
            (root / "realdir").mkdir()
            sdir.symlink_to(root / "realdir")
            with self.assertRaises(OSError):
                atomic_write_text_no_follow(sdir / "x.txt", "new")

            runner = SandboxRunner(root / "work", root / "outbox", timeout=240)
            with patch.object(SandboxRunner, "use_setpriv", return_value=True):
                wrapped = runner._wrap_command(["codex", "exec"])
            self.assertEqual(
                wrapped[:5],
                ["/usr/bin/setpriv", "--reuid=65534", "--regid=65534", "--clear-groups", "--no-new-privs"],
            )

            with patch.dict(
                os.environ,
                {
                    "PATH": "/usr/bin",
                    "HTTP_PROXY": "   ",
                    "HTTPS_PROXY": "http://proxy.local:8080",
                    "ONEBOT_TOKEN": "top-secret-onebot",
                    "BOT_API_KEY": "sk-secret",
                },
                clear=True,
            ):
                env = runner._build_env(root / "t1", root / "h1", {"ECHO_API_KEY": "k1"})
            self.assertNotIn("HTTP_PROXY", env)
            self.assertEqual(env["HTTPS_PROXY"], "http://proxy.local:8080")
            self.assertNotIn("ONEBOT_TOKEN", env)
            self.assertNotIn("BOT_API_KEY", env)
            self.assertEqual(env["HOME"], str(root / "t1"))
            self.assertEqual(env["TMPDIR"], str(root / "t1"))
            self.assertEqual(env["ECHO_API_KEY"], "k1")


class CodexBackendTests(unittest.TestCase):
    def test_config_command_and_jsonl_parsing(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "codex_home"
            backend = CodexBackend(
                binary="/usr/local/bin/codex",
                api_base="http://127.0.0.1:8787/v1/chat/completions",
                api_key="sk-test-1234",
                model="gpt-6-luna",
            )
            cfg = backend.config_text()
            self.assertIn('wire_api = "responses"', cfg)
            self.assertIn('base_url = "http://127.0.0.1:8787/v1"', cfg)
            self.assertIn('env_key = "ECHO_API_KEY"', cfg)

            backend.prepare_home(home)
            self.assertEqual((home / "config.toml").read_text(encoding="utf-8"), cfg)

            img1 = Path(tmp) / "input_1.png"
            cmd = backend.build_command("分析这张图", Path(tmp), Path(tmp) / "outbox", [img1], timeout=300)
            self.assertIn("--sandbox", cmd)
            idx = cmd.index("-i")
            self.assertEqual(cmd[idx + 1], str(img1))

            self.assertEqual(
                backend.parse_line(json.dumps({"type": "item.started", "item": {"type": "web_search"}})),
                "正在联网搜索资料…",
            )
            raw = "\n".join([
                json.dumps({"type": "item.completed", "item": {"type": "reasoning", "text": "skip"}}),
                json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "查到最终结果"}}),
            ])
            self.assertEqual(backend.parse_output(raw), "查到最终结果")


class SandboxRunnerExecutionTests(unittest.TestCase):
    def test_sandbox_stages_inbox_filters_outbox_and_cleans_workdir(self):
        script = """
import os, sys
from pathlib import Path
print("PROGRESS:正在整理输出", flush=True)
out = Path("outbox")
(out / "report.html").write_text("<h1>OK</h1>", encoding="utf-8")
(out / "leak.md").write_text("token sk-1234567890abcdef123456", encoding="utf-8")
(out / "bad.sqlite3").write_bytes(b"SQLite format 3\\x00abc")
secret_src = Path("outside_target.txt")
secret_src.write_text("secret", encoding="utf-8")
try:
    (out / "link.md").symlink_to(secret_src)
except OSError:
    pass
hl = Path("hardlink_src.md")
hl.write_text("hardlink body", encoding="utf-8")
try:
    os.link(hl, out / "hard.md")
except OSError:
    pass
inbox_exists = (Path("inbox") / "input_1.png").is_file()
print(f"ANSWER:完成 inbox={inbox_exists}", flush=True)
"""
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp) / "work"
            outbox = Path(tmp) / "outbox"
            runner = SandboxRunner(work, outbox, timeout=180)
            backend = _FakePythonBackend(script)
            steps = []

            res = runner.run(
                TaskRequest(group_id="g1", umo="u1", prompt="生成网页", images=(TINY_PNG,)),
                backend,
                on_progress=steps.append,
                extra_secrets=("custom-api-secret-xyz",),
            )
            self.assertEqual(res.status, "ok")
            self.assertEqual(res.text, "完成 inbox=True")
            self.assertEqual(steps, ["正在整理输出"])
            self.assertEqual(len(res.files), 1)
            self.assertEqual(res.files[0]["name"], "report.html")
            self.assertTrue(Path(res.files[0]["path"]).is_file())
            # 任务工作目录已彻底回收
            self.assertEqual(list(work.iterdir()), [])

    def test_sandbox_timeout_kills_process_group(self):
        script = """
import subprocess, sys, time
print("ANSWER:已经查到前半段结论", flush=True)
subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
time.sleep(30)
"""
        with tempfile.TemporaryDirectory() as tmp:
            runner = SandboxRunner(Path(tmp) / "work", Path(tmp) / "outbox", timeout=180)
            runner.timeout = 1
            backend = _FakePythonBackend(script)
            t0 = time.monotonic()
            with patch("os.killpg", wraps=os.killpg) as killpg_spy:
                res = runner.run(TaskRequest(group_id="g1", umo="u1", prompt="长任务"), backend)
            elapsed = time.monotonic() - t0
            self.assertLess(elapsed, 6.0)
            self.assertEqual(res.status, "timeout")
            self.assertIn("前半段结论", res.partial)
            self.assertGreaterEqual(killpg_spy.call_count, 1)
            self.assertEqual(killpg_spy.call_args_list[0][0][1], signal.SIGKILL)

    def test_sandbox_cancel_event_stops_immediately(self):
        script = """
import time
print("PROGRESS:准备长跑", flush=True)
time.sleep(30)
"""
        with tempfile.TemporaryDirectory() as tmp:
            runner = SandboxRunner(Path(tmp) / "work", Path(tmp) / "outbox", timeout=180)
            backend = _FakePythonBackend(script)
            cancel = threading.Event()
            threading.Timer(0.25, cancel.set).start()

            t0 = time.monotonic()
            res = runner.run(TaskRequest(group_id="g1", umo="u1", prompt="取消测试"), backend, cancel_event=cancel)
            self.assertLess(time.monotonic() - t0, 5.0)
            self.assertEqual(res.status, "cancelled")


class TaskManagerTests(unittest.TestCase):
    def test_single_flight_status_stop_and_reload_delivery(self):
        script = """
import time
print("PROGRESS:第1步检索中", flush=True)
time.sleep(2.0)
print("ANSWER:完成", flush=True)
"""
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "echo.sqlite3"
            repo = Repository(db)
            runner = SandboxRunner(Path(tmp) / "work", Path(tmp) / "outbox", timeout=180)
            backend = _FakePythonBackend(script)
            mgr = TaskManager(runner, backend, repo=repo)

            first = mgr.submit(TaskRequest(group_id="g1", umo="u1", prompt="深度查一下"), ack_text="好嘞我去看下")
            self.assertTrue(first["accepted"])
            self.assertIn("好嘞我去看下", first["notice"])

            dup = mgr.submit(TaskRequest(group_id="g1", umo="u1", prompt="第二个任务"))
            self.assertFalse(dup["accepted"])
            self.assertEqual(dup["reason"], "task_running")

            # 等待进度出现，然后停止任务
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                st = mgr.status("g1")
                if st and "第1步" in st.progress:
                    break
                time.sleep(0.05)
            self.assertIsNotNone(mgr.status("g1"))
            self.assertTrue(mgr.stop("g1"))

            deliveries = []
            deadline = time.monotonic() + 4.0
            while time.monotonic() < deadline and not deliveries:
                deliveries = mgr.poll_deliveries()
                time.sleep(0.05)
            self.assertEqual(len(deliveries), 1)
            self.assertEqual(deliveries[0].reason, "cancelled")
            rendered = mgr.present_delivery(deliveries[0], llm=_FakeLLM())
            self.assertIn("停掉", rendered["text"])
            self.assertEqual(repo.list_pending_deliveries(), [])
            repo.close()

    def test_present_delivery_preserves_code_timeout_and_failed_lookup_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            outbox = Path(tmp) / "outbox"
            task_out = outbox / "t_x"
            task_out.mkdir(parents=True)
            html_file = task_out / "summary.html"
            html_file.write_text("<h1>demo</h1>", encoding="utf-8")

            repo = Repository(":memory:")
            runner = SandboxRunner(Path(tmp) / "work", outbox, timeout=240)
            mgr = TaskManager(
                runner,
                _FakePythonBackend(""),
                repo=repo,
                extra_secrets_fn=lambda: ("sk-top-secret-value-8888",),
            )

            # 1) 成功带代码块：保留代码原貌且脱敏路径与密钥
            d_code = PendingDelivery(
                task_id="t_code",
                group_id="g1",
                umo="u1",
                prompt="写个快排",
                status="completed",
                reason="ok",
                result="写完了\n```python\ndef qsort(a): return a\n```",
                files=({"name": "summary.html", "path": str(html_file)},),
            )
            r_code = mgr.present_delivery(d_code, llm=_FakeLLM("给你写好啦："))
            self.assertTrue(r_code["ok"])
            self.assertIn("给你写好啦", r_code["text"])
            self.assertIn("def qsort(a): return a", r_code["text"])
            self.assertEqual(len(r_code["files"]), 1)

            # 2) 超时仅回报 partial 进度
            d_to = PendingDelivery(
                task_id="t_to",
                group_id="g1",
                umo="u1",
                prompt="检索所有论文",
                status="failed",
                reason="timeout",
                partial="已找到 /AstrBot/data/x 的部分结果，密钥 sk-top-secret-value-8888",
            )
            r_to = mgr.present_delivery(d_to, llm=None)
            self.assertFalse(r_to["ok"])
            self.assertIn("超时了", r_to["text"])
            self.assertNotIn("sk-top-secret-value-8888", r_to["text"])
            self.assertNotIn("/AstrBot/data", r_to["text"])

            # 3) 普通查询但未能查出有效结果 -> 清理附带无用文件
            spurious = outbox / "t_fail" / "empty.md"
            spurious.parent.mkdir(parents=True)
            spurious.write_text("sorry not found", encoding="utf-8")
            d_fail_lookup = PendingDelivery(
                task_id="t_lookup_fail",
                group_id="g1",
                umo="u1",
                prompt="今天天气咋样",
                status="completed",
                reason="ok",
                result="未查到有效结果，抱歉我暂时无法核实。",
                files=({"name": "empty.md", "path": str(spurious)},),
            )
            r_fail = mgr.present_delivery(d_fail_lookup, llm=_FakeLLM("没查到相关结果"))
            self.assertEqual(r_fail["files"], [])
            self.assertFalse(spurious.exists())

            # 4) cleanup_outbox 跳过 keep_paths
            keep_dir = outbox / "t_keep"
            keep_dir.mkdir()
            kept_f = keep_dir / "keep.md"
            kept_f.write_text("keep", encoding="utf-8")
            os.utime(keep_dir, (1.0, 1.0))
            cleanup_outbox(outbox, max_age=10, keep_paths={str(kept_f)})
            self.assertTrue(kept_f.exists())
            repo.close()


class ChatAgentBackendAndRoutingTests(unittest.TestCase):
    def test_select_backend_routes_gpt_to_codex_and_others_to_chat_agent(self):
        with patch("os.path.exists", return_value=True):
            self.assertIsInstance(
                select_backend(api_base="http://127.0.0.1:8787/v1", api_key="k", model="gpt-6-luna"),
                CodexBackend,
            )
            self.assertIsInstance(
                select_backend(api_base="http://127.0.0.1:8787/v1", api_key="k", model="claude-haiku-5-5"),
                ChatAgentBackend,
            )
            self.assertIsInstance(
                select_backend(api_base="http://127.0.0.1:8787/v1", api_key="k", model="deepseek-chat"),
                ChatAgentBackend,
            )

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / ".chat_agent"
            backend = ChatAgentBackend(api_base="http://127.0.0.1:8787/v1", api_key="k", model="claude-haiku-5-5")
            backend.prepare_home(home)
            self.assertTrue((home / "agent_loop.py").is_file())
            self.assertTrue((home / "echo_search.py").is_file())
            self.assertEqual(
                (home / "echo_search.py").read_text(encoding="utf-8"),
                (PLUGIN_ROOT / "astrbot_plugin_echo" / "adapters" / "search.py").read_text(encoding="utf-8"),
            )
            loop_src = (home / "agent_loop.py").read_text(encoding="utf-8")
            self.assertNotIn("class SearchRunner", loop_src)
            self.assertNotIn("def bing_search_cn", loop_src)

    def test_chat_agent_backend_runs_in_sandbox_strips_secrets_and_exports_outbox(self):
        from http.server import BaseHTTPRequestHandler, HTTPServer

        captured_requests = []

        class _MockLLMHandler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length).decode("utf-8"))
                captured_requests.append(body)
                round_idx = len(captured_requests)
                if round_idx == 1:
                    resp = {
                        "choices": [{
                            "message": {
                                "content": "",
                                "tool_calls": [
                                    {
                                        "id": "tc_1",
                                        "type": "function",
                                        "function": {
                                            "name": "run_shell",
                                            "arguments": json.dumps({
                                                "command": (
                                                    f"{sys.executable} -c "
                                                    "'import os; print(\"LEAK\" if (\"ECHO_API_KEY\" in os.environ or \"ECHO_SEARCH_KEY\" in os.environ) else \"CLEAN\")'"
                                                )
                                            }),
                                        },
                                    },
                                    {
                                        "id": "tc_2",
                                        "type": "function",
                                        "function": {
                                            "name": "write_file",
                                            "arguments": json.dumps({
                                                "path": "outbox/result.html",
                                                "content": "<h1>ok</h1>",
                                            }),
                                        },
                                    },
                                    {
                                        "id": "tc_3",
                                        "type": "function",
                                        "function": {
                                            "name": "write_file",
                                            "arguments": json.dumps({
                                                "path": "../escape.txt",
                                                "content": "bad",
                                            }),
                                        },
                                    },
                                ],
                            }
                        }]
                    }
                else:
                    tool_msgs = [m["content"] for m in body["messages"] if m.get("role") == "tool"]
                    status_tag = "CLEAN" if any("CLEAN" in t for t in tool_msgs) else "FAIL"
                    resp = {"choices": [{"message": {"content": f"处理完毕（{status_tag}），文件 result.html 已生成。"}}]}
                raw = json.dumps(resp, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        server = HTTPServer(("127.0.0.1", 0), _MockLLMHandler)
        port = server.server_address[1]
        th = threading.Thread(target=server.serve_forever, daemon=True)
        th.start()

        try:
            with tempfile.TemporaryDirectory() as tmp:
                work = Path(tmp) / "work"
                outbox = Path(tmp) / "outbox"
                runner = SandboxRunner(work, outbox, timeout=180)
                backend = ChatAgentBackend(
                    api_base=f"http://127.0.0.1:{port}/v1",
                    api_key="sk-secret-key-999999",
                    model="claude-haiku-5-5",
                    search_provider="tavily",
                    search_api_key="tvly-secret-88888",
                )
                steps = []
                res = runner.run(
                    TaskRequest(group_id="g1", umo="u1", prompt="看图并生成报告", images=(TINY_PNG,)),
                    backend,
                    on_progress=steps.append,
                    extra_secrets=("sk-secret-key-999999", "tvly-secret-88888"),
                )
                self.assertEqual(res.status, "ok")
                self.assertIn("CLEAN", res.text)
                self.assertNotIn("LEAK", res.text)
                self.assertEqual(len(res.files), 1)
                self.assertEqual(res.files[0]["name"], "result.html")
                self.assertFalse((Path(tmp) / "escape.txt").exists())
                self.assertGreaterEqual(len(steps), 2)
                self.assertEqual(list(work.iterdir()), [])
                # 首轮请求含 inline image_url 且不带 temperature / max_tokens
                self.assertNotIn("temperature", captured_requests[0])
                self.assertNotIn("max_tokens", captured_requests[0])
                self.assertEqual(captured_requests[0]["messages"][1]["content"][1]["type"], "image_url")
                # 越界写逃逸已被拒绝并回传给模型
                tool_replies = [m["content"] for m in captured_requests[1]["messages"] if m.get("role") == "tool"]
                self.assertTrue(any("写入失败" in t for t in tool_replies), tool_replies)
        finally:
            server.shutdown()
            server.server_close()

    def test_chat_agent_cancel_event(self):
        from http.server import BaseHTTPRequestHandler, HTTPServer

        class _SlowHandler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass

            def do_POST(self):
                time.sleep(2.5)
                try:
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b"{}")
                except OSError:
                    pass

        server = HTTPServer(("127.0.0.1", 0), _SlowHandler)
        port = server.server_address[1]
        th = threading.Thread(target=server.serve_forever, daemon=True)
        th.start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                runner = SandboxRunner(Path(tmp) / "work", Path(tmp) / "outbox", timeout=180)
                backend = ChatAgentBackend(
                    api_base=f"http://127.0.0.1:{port}/v1",
                    api_key="sk-test-cancel",
                    model="claude-haiku-5-5",
                )
                cancel = threading.Event()
                threading.Timer(0.25, cancel.set).start()
                t0 = time.monotonic()
                res = runner.run(TaskRequest(group_id="g1", umo="u1", prompt="测试取消"), backend, cancel_event=cancel)
                self.assertLess(time.monotonic() - t0, 3.0)
                self.assertEqual(res.status, "cancelled")
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()

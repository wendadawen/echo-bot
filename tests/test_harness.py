"""Codex harness 单测：事件解析、可用性判断、配置生成。全部离线。"""

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugin"))

from astrbot_plugin_echo.core.harness import (  # noqa: E402
    CodexHarness, MAX_FILE_BYTES, MAX_OUTPUT_FILES,
    cleanup_file_entry, cleanup_outbox, parse_events,
)


class TestHarness(unittest.TestCase):
    def test_parse_takes_last_agent_message(self):
        raw = "\n".join([
            json.dumps({"type": "thread.started"}),
            json.dumps({"type": "item.completed",
                        "item": {"type": "agent_message", "text": "第一版"}}),
            json.dumps({"type": "item.completed",
                        "item": {"type": "agent_message", "text": "最终答案"}}),
            "not json at all",
        ])
        self.assertEqual("最终答案", parse_events(raw))

    def test_parse_ignores_non_agent_items(self):
        raw = json.dumps({"type": "item.completed",
                          "item": {"type": "tool_call", "text": "x"}})
        self.assertEqual("", parse_events(raw))

    def test_parse_truncates(self):
        raw = json.dumps({"type": "item.completed",
                          "item": {"type": "agent_message", "text": "长" * 100}})
        self.assertEqual(10, len(parse_events(raw, limit=10)))

    def test_unavailable_without_binary(self):
        harness = CodexHarness(binary="/nope/codex", home="/tmp/nowhere")
        self.assertFalse(harness.available())
        self.assertEqual("", harness.run("g", "任务"))

    def test_workdir_is_shared(self):
        with tempfile.TemporaryDirectory() as folder:
            harness = CodexHarness(
                binary="/bin/true",
                home=str(Path(folder) / "home"),
                workdir_root=str(Path(folder) / "work"),
            )
            self.assertEqual(harness._workdir("g1"), harness._workdir("g2"))

    def test_config_written_with_responses_api(self):
        with tempfile.TemporaryDirectory() as folder:
            binary = Path(folder) / "codex"
            binary.write_text("", encoding="utf-8")
            harness = CodexHarness(
                binary=str(binary),
                home=str(Path(folder) / "home"),
                workdir_root=str(Path(folder) / "work"),
                api_base="http://example/v1",
                model="gpt-6-luna",
            )
            self.assertTrue(harness.available())
            harness._ensure_config()
            text = (Path(folder) / "home" / "config.toml").read_text(encoding="utf-8")
            self.assertIn('wire_api = "responses"', text)
            self.assertIn("http://example/v1", text)
            self.assertIn("gpt-6-luna", text)

    def test_run_with_files_collects_safe_files_and_blocks_leaks(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            binary = root / "codex"
            binary.write_text("", encoding="utf-8")
            outside_secret = root / "outside_secret.txt"
            outside_secret.write_text("TOP_SECRET", encoding="utf-8")

            harness = CodexHarness(
                binary=str(binary),
                home=str(root / "home"),
                workdir_root=str(root / "work"),
                outbox_root=str(root / "outbox"),
            )

            seen_cwd = []

            class FakeProc:
                pid = 999999

                def communicate(self, timeout=None):
                    return (
                        json.dumps({
                            "type": "item.completed",
                            "item": {"type": "agent_message", "text": "网页做好了"},
                        }) + "\n",
                        "",
                    )

            def fake_popen(cmd, **kwargs):
                task_dir = Path(kwargs["cwd"])
                seen_cwd.append(task_dir)
                outbox = task_dir / "outbox"
                # 合法文件 4 个（应按 mtime 倒序最多取 3 个）
                (outbox / "older.txt").write_text("older", encoding="utf-8")
                os.utime(outbox / "older.txt", (100, 100))
                for idx, name in enumerate(("app.js", "demo.html", "report.csv"), start=1):
                    p = outbox / name
                    p.write_text("content-{}".format(idx), encoding="utf-8")
                    os.utime(p, (200 + idx, 200 + idx))
                # 必须被拦掉的不安全/无效条目
                (outbox / ".env").write_text("API_KEY=1", encoding="utf-8")
                (outbox / "server.key").write_text("PRIVATE", encoding="utf-8")
                (outbox / "dump.sqlite3").write_text("SQLite", encoding="utf-8")
                (outbox / "config.toml").write_text("model='x'", encoding="utf-8")
                (outbox / "my_secret_notes.txt").write_text("secret", encoding="utf-8")
                (outbox / "empty.txt").write_text("", encoding="utf-8")
                (outbox / "sub").mkdir()
                (outbox / "sub" / "nested.html").write_text("<html></html>", encoding="utf-8")
                (outbox / "link.html").symlink_to(outside_secret)
                # 稀疏超大文件（> 20MB，实际几乎不占磁盘）
                big = outbox / "huge.bin"
                with open(big, "wb") as fh:
                    fh.seek(MAX_FILE_BYTES + 10)
                    fh.write(b"x")
                os.utime(big, (999, 999))
                return FakeProc()

            with patch("subprocess.Popen", side_effect=fake_popen):
                res = harness.run_with_files("g1", "生成控制台")

            self.assertEqual("网页做好了", res["text"])
            files = res["files"]
            self.assertEqual(MAX_OUTPUT_FILES, len(files))
            self.assertEqual(["report.csv", "demo.html", "app.js"], [f["name"] for f in files])
            for f in files:
                self.assertTrue(Path(f["path"]).is_file())
            # 临时任务目录必须已销毁，符号链接目标不受损
            self.assertFalse(seen_cwd[0].exists())
            self.assertTrue(outside_secret.exists())

            # cleanup_file_entry 只删除 outbox_root 内部文件，并自动删除空任务目录
            for f in files:
                self.assertTrue(cleanup_file_entry(f["path"], harness.outbox_root))
                self.assertFalse(Path(f["path"]).exists())
            self.assertEqual([], list(Path(harness.outbox_root).iterdir()))
            # 拒绝删除 outbox_root 外部文件
            self.assertFalse(cleanup_file_entry(str(outside_secret), harness.outbox_root))
            self.assertFalse(
                cleanup_file_entry(
                    str(Path(harness.outbox_root) / ".." / "outside_secret.txt"),
                    harness.outbox_root,
                )
            )
            self.assertTrue(outside_secret.exists())

            # run() 若底层产出文件，应自动清理而不残留到 outbox_root
            with patch("subprocess.Popen", side_effect=fake_popen):
                txt = harness.run("g1", "只要文字")
            self.assertEqual("网页做好了", txt)
            self.assertEqual([], list(Path(harness.outbox_root).iterdir()))

            # cleanup_outbox 仅清掉过期子目录，保留未过期的
            expired_dir = Path(harness.outbox_root) / "task-old"
            fresh_dir = Path(harness.outbox_root) / "task-new"
            expired_dir.mkdir(parents=True)
            fresh_dir.mkdir(parents=True)
            old_ts = time.time() - 7200
            os.utime(expired_dir, (old_ts, old_ts))
            cleanup_outbox(harness.outbox_root, max_age=3600)
            self.assertFalse(expired_dir.exists())
            self.assertTrue(fresh_dir.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)

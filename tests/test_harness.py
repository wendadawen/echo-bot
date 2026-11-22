"""Codex harness 单测：事件解析、可用性判断、配置生成。全部离线。"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugin"))

from astrbot_plugin_echo.core.harness import CodexHarness, parse_events  # noqa: E402


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


if __name__ == "__main__":
    unittest.main(verbosity=2)

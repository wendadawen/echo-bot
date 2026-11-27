"""容器部署、权限边界、缓存清理与插件包结构的静态/动态契约测试。"""

import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = ROOT / "plugin"
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from astrbot_plugin_echo.store.repository import cleanup_runtime_caches  # noqa: E402


class InfraContractTests(unittest.TestCase):
    def test_dockerfile_contracts(self):
        text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("ARG BASE_IMAGE=soulter/astrbot:v4.29.4", text)
        self.assertIn("ARG APT_MIRROR=", text)
        self.assertIn("ARG GITHUB_PROXY=", text)
        self.assertIn("ARG PIP_INDEX_URL=", text)
        self.assertIn("/etc/apt/sources.list.d/debian.sources", text)
        self.assertIn("codex", text)

    def test_compose_no_host_bind_mount_and_healthcheck_proxy_bypass(self):
        text = (ROOT / "compose.yaml").read_text(encoding="utf-8")
        self.assertIn("echo_bot_data:/AstrBot/data", text)
        self.assertNotIn("./data:/AstrBot/data", text)
        self.assertIn("ProxyHandler({})", text)
        for key in (
            "GITHUB_PROXY",
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "NO_PROXY",
            "NAPCAT_ZIP_SHA256",
            "LINK_RESOLVER_URL",
            "LINK_RESOLVER_SHA256",
            "QQ_DEB_SHA256",
            "BOT_SEARCH_PROVIDER",
            "BOT_SEARCH_API_BASE",
            "BOT_SEARCH_API_KEY",
        ):
            self.assertIn(key, text)

        env_ex = (ROOT / ".env.example").read_text(encoding="utf-8")
        for needle in ("24 字节", "BOT_API_KEY", "BOT_SEARCH_PROVIDER", "MEM_LIMIT", "PIDS_LIMIT"):
            self.assertIn(needle, env_ex)

    def test_entrypoint_and_supervisord_proxy_contracts(self):
        entry = (ROOT / "docker" / "entrypoint.sh").read_text(encoding="utf-8")
        self.assertIn("/usr/local/bin/bootstrap.sh", entry)
        norm_idx = entry.index("http_p=")
        boot_idx = entry.index("/usr/local/bin/bootstrap.sh")
        self.assertLess(norm_idx, boot_idx)
        self.assertIn("unset ONEBOT_TOKEN NAPCAT_WEBUI_TOKEN", entry)

        sup = (ROOT / "docker" / "supervisord.conf").read_text(encoding="utf-8")
        self.assertIn(
            "env -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY -u http_proxy -u https_proxy -u all_proxy"
            " -u BOT_API_KEY -u BOT_API_BASE -u BOT_SEARCH_API_KEY -u BOT_SEARCH_API_BASE",
            sup,
        )

    def test_bootstrap_security_contracts(self):
        boot = (ROOT / "docker" / "bootstrap.sh").read_text(encoding="utf-8")
        main_idx = boot.index("# --- bootstrap main ---")
        for helper in ("normalize_github_proxy()", "verify_artifact()", "download_file()"):
            self.assertLess(boot.index(helper), main_idx)
        self.assertIn("m.issym() or m.islnk()", boot)
        self.assertIn("KEEP_BOOTSTRAP_CACHE:-0", boot)
        self.assertIn("NAPCAT_WEBUI_TOKEN", boot)
        self.assertIn("enableForcePushEvent", boot)

    def test_up_script_cn_mode_and_no_data_mkdir(self):
        up = (ROOT / "scripts" / "up.sh").read_text(encoding="utf-8")
        for flag in ("--cn", "--build", "--build-only", "DEPLOY_MODE", "upsert_env_key"):
            self.assertIn(flag, up)
        self.assertNotIn("mkdir -p data", up)
        self.assertNotIn('mkdir -p "$root/data"', up)
        env_loop_idx = up.index("for _k in BASE_IMAGE")
        cn_if_idx = up.index('if [[ "$mode_cn" -eq 1 ]]')
        self.assertLess(env_loop_idx, cn_if_idx)

    def test_cleanup_runtime_caches_respects_symlinks_and_preserves_dbs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            temp_dir = data_dir / "temp"
            db_dir = data_dir / "plugin_data" / "astrbot_plugin_echo"
            nc_logs = data_dir / "napcat" / "logs"
            qq_pic = data_dir / "napcat" / "home" / ".config" / "QQ" / "nt_qq_123" / "nt_data" / "Pic"
            for d in (temp_dir, db_dir, nc_logs, qq_pic):
                d.mkdir(parents=True)

            old_media = temp_dir / "media_old.png"
            old_media.write_bytes(b"123")
            os.utime(old_media, (1000.0, 1000.0))

            fresh_media = temp_dir / "media_new.png"
            fresh_media.write_bytes(b"456")

            old_log = nc_logs / "old.log"
            old_log.write_text("log", encoding="utf-8")
            os.utime(old_log, (1000.0, 1000.0))

            old_pic = qq_pic / "old.jpg"
            old_pic.write_bytes(b"jpg")
            os.utime(old_pic, (1000.0, 1000.0))

            sqlite_file = db_dir / "echo.sqlite3"
            sqlite_file.write_bytes(b"SQLite format 3")
            os.utime(sqlite_file, (1000.0, 1000.0))

            outside_file = root / "outside.txt"
            outside_file.write_text("keep me", encoding="utf-8")
            os.utime(outside_file, (1000.0, 1000.0))
            (temp_dir / "media_sym.png").symlink_to(outside_file)

            removed = cleanup_runtime_caches(data_dir, now=10_000_000.0)
            self.assertEqual(removed, 3)
            self.assertFalse(old_media.exists())
            self.assertFalse(old_log.exists())
            self.assertFalse(old_pic.exists())
            self.assertTrue(fresh_media.exists())
            self.assertTrue(sqlite_file.exists())
            self.assertTrue(outside_file.exists())

    def test_validate_plugin_and_zero_core_legacy(self):
        self.assertFalse((PLUGIN_ROOT / "astrbot_plugin_echo" / "core").exists())
        proc = subprocess.run(
            [sys.executable, str(ROOT / "tools" / "validate_plugin.py")],
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_wave3_vertical_module_boundaries_and_facade_exports(self):
        from astrbot_plugin_echo import domain, text_media
        from astrbot_plugin_echo.adapters import astrbot, qq_protocol, sender
        from astrbot_plugin_echo.engine import agent, tools
        from astrbot_plugin_echo.store import (
            cache_cleanup,
            emoji_classifier,
            emoji_store,
            memory_store,
            person_store,
            repository,
            schema,
        )
        from astrbot_plugin_echo.workers import manager, presenter

        self.assertIs(domain.FACE_LABELS, text_media.FACE_LABELS)
        self.assertIs(domain.detect_image_dimensions, text_media.detect_image_dimensions)
        self.assertIs(repository.cleanup_runtime_caches, cache_cleanup.cleanup_runtime_caches)
        self.assertEqual(repository.SCHEMA_VERSION, schema.SCHEMA_VERSION)
        self.assertIs(repository.query_terms, memory_store.query_terms)
        self.assertIs(repository._merge_field, person_store._merge_field)
        self.assertIs(emoji_store.acceptable, emoji_classifier.acceptable)
        self.assertIs(emoji_store.analyze_emoji, emoji_classifier.analyze_emoji)
        self.assertIs(astrbot.AstrBotSender, sender.AstrBotSender)
        self.assertIs(astrbot.MessageLimiter, sender.MessageLimiter)
        self.assertIs(astrbot.is_public_http_url, qq_protocol.is_public_http_url)
        self.assertIs(agent._format_emoji_meta, tools.format_emoji_meta)
        self.assertIs(manager._split_code_blocks, presenter._split_code_blocks)

        pkg = PLUGIN_ROOT / "astrbot_plugin_echo"
        self.assertLess(len((pkg / "domain.py").read_text(encoding="utf-8").splitlines()), 550)
        self.assertLess(len((pkg / "store" / "repository.py").read_text(encoding="utf-8").splitlines()), 450)
        self.assertLess(len((pkg / "adapters" / "astrbot.py").read_text(encoding="utf-8").splitlines()), 550)
        self.assertLess(len((pkg / "workers" / "manager.py").read_text(encoding="utf-8").splitlines()), 350)
        self.assertLess(len((pkg / "engine" / "agent.py").read_text(encoding="utf-8").splitlines()), 550)

    def test_embedded_python_in_shell_scripts_syntax_and_behavior(self):
        pattern = re.compile(r"<<'PY'\n(.*?)\nPY(?=\r?\n|$)", re.S)
        up_blocks = pattern.findall((ROOT / "scripts" / "up.sh").read_text(encoding="utf-8"))
        boot_blocks = pattern.findall((ROOT / "docker" / "bootstrap.sh").read_text(encoding="utf-8"))
        self.assertEqual(len(up_blocks), 1)
        self.assertEqual(len(boot_blocks), 3)
        for src in (*up_blocks, *boot_blocks):
            compile(src, "<embedded-py>", "exec")

        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / ".env"
            env_file.write_text("# comment keep\nBOT_IMAGE=old-image\n", encoding="utf-8")
            subprocess.run(
                [sys.executable, "-c", up_blocks[0], str(env_file), "BOT_IMAGE", "echo-bot:cn"],
                check=True,
            )
            subprocess.run(
                [sys.executable, "-c", up_blocks[0], str(env_file), "DEPLOY_MODE", "cn"],
                check=True,
            )
            content = env_file.read_text(encoding="utf-8")
            self.assertIn("# comment keep\n", content)
            self.assertIn("BOT_IMAGE=echo-bot:cn\n", content)
            self.assertIn("DEPLOY_MODE=cn\n", content)
            self.assertNotIn("old-image", content)
            self.assertEqual(env_file.stat().st_mode & 0o777, 0o600)

    def test_ci_and_release_workflows_have_test_gates(self):
        img_wf = (ROOT / ".github" / "workflows" / "image.yml").read_text(encoding="utf-8")
        self.assertIn("verify:", img_wf)
        self.assertIn("needs: verify", img_wf)
        for needle in ("validate_plugin.py", "unittest discover", "simulate.py"):
            self.assertIn(needle, img_wf)

        rel_wf = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
        for needle in ("validate_plugin.py", "unittest discover", "simulate.py", "smoke_import.py"):
            self.assertIn(needle, rel_wf)

    def test_wave4_sandbox_boundaries_and_facade_exports(self):
        from astrbot_plugin_echo.workers import fs_guard, sandbox
        from astrbot_plugin_echo.workers.backends import chat_agent

        self.assertIs(sandbox.is_safe_export_name, fs_guard.is_safe_export_name)
        self.assertIs(sandbox.atomic_write_text_no_follow, fs_guard.atomic_write_text_no_follow)
        self.assertFalse(hasattr(chat_agent, "_AGENT_LOOP_SCRIPT"))
        self.assertEqual(
            chat_agent.ChatAgentBackend(api_base="https://api.example.com/v1", api_key="sk-secret").env_overrides(
                Path("."), Path(".")
            ),
            {},
        )
        pkg = PLUGIN_ROOT / "astrbot_plugin_echo"
        self.assertLess(len((pkg / "workers" / "sandbox.py").read_text(encoding="utf-8").splitlines()), 560)
        self.assertLess(len((pkg / "workers" / "backends" / "chat_agent.py").read_text(encoding="utf-8").splitlines()), 420)


if __name__ == "__main__":
    unittest.main()

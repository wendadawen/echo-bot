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
        ):
            self.assertIn(key, text)

    def test_entrypoint_and_supervisord_proxy_contracts(self):
        entry = (ROOT / "docker" / "entrypoint.sh").read_text(encoding="utf-8")
        self.assertIn("/usr/local/bin/bootstrap.sh", entry)
        norm_idx = entry.index("http_p=")
        boot_idx = entry.index("/usr/local/bin/bootstrap.sh")
        self.assertLess(norm_idx, boot_idx)
        self.assertIn("unset ONEBOT_TOKEN NAPCAT_WEBUI_TOKEN", entry)

        sup = (ROOT / "docker" / "supervisord.conf").read_text(encoding="utf-8")
        self.assertIn(
            "env -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY -u http_proxy -u https_proxy -u all_proxy",
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


if __name__ == "__main__":
    unittest.main()

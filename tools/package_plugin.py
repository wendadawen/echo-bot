"""把插件目录打成可直接放进 AstrBot data/plugins/ 的 zip。由 CI 调用。"""

import re
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugin" / "astrbot_plugin_echo"
DIST = ROOT / "dist"


def _should_skip(rel_path):
    for part in rel_path.parts:
        if part.startswith(".") or part == "__pycache__":
            return True
    name = rel_path.name.lower()
    if name.endswith((".pyc", ".pyo", ".pyd", ".sqlite", ".sqlite3", ".db", ".wal", ".shm")):
        return True
    return False


def main():
    metadata = (PLUGIN / "metadata.yaml").read_text(encoding="utf-8")
    match = re.search(r"^version:\s*(\S+)", metadata, re.M)
    version = match.group(1) if match else "dev"
    DIST.mkdir(exist_ok=True)
    target = DIST / "astrbot_plugin_echo-{}.zip".format(version)
    count = 0
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(PLUGIN.rglob("*")):
            if path.is_symlink() or not path.is_file():
                continue
            rel = path.relative_to(PLUGIN)
            if _should_skip(rel):
                continue
            archive.write(path, (Path(PLUGIN.name) / rel).as_posix())
            count += 1
    print("已打包 {} 个文件 -> {}".format(count, target))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

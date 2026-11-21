"""把插件目录打成可直接放进 AstrBot data/plugins/ 的 zip。由 CI 调用。"""

import re
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugin" / "astrbot_plugin_echo"
DIST = ROOT / "dist"


def main():
    metadata = (PLUGIN / "metadata.yaml").read_text(encoding="utf-8")
    match = re.search(r"^version:\s*(\S+)", metadata, re.M)
    version = match.group(1) if match else "dev"
    DIST.mkdir(exist_ok=True)
    target = DIST / "astrbot_plugin_echo-{}.zip".format(version)
    count = 0
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(PLUGIN.rglob("*")):
            if not path.is_file():
                continue
            if "__pycache__" in path.parts or path.suffix == ".pyc":
                continue
            archive.write(path, str(Path(PLUGIN.name) / path.relative_to(PLUGIN)))
            count += 1
    print("已打包 {} 个文件 -> {}".format(count, target))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

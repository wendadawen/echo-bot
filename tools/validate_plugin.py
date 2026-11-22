"""校验插件目录结构、元数据与配置 schema。只用标准库。"""

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugin" / "astrbot_plugin_echo"

REQUIRED = [
    "main.py",
    "metadata.yaml",
    "_conf_schema.json",
    "requirements.txt",
    "README.md",
    "core/__init__.py",
    "core/brain.py",
    "core/messages.py",
    "core/models.py",
    "core/store.py",
    "core/rhythm.py",
    "core/actions.py",
    "core/expression.py",
    "core/memory.py",
    "core/vision.py",
]

META_FIELDS = ["name", "display_name", "desc", "version", "author", "repo", "astrbot_version"]


def main():
    errors = []
    for rel in REQUIRED:
        if not (PLUGIN / rel).is_file():
            errors.append("缺少文件：{}".format(rel))

    metadata = (PLUGIN / "metadata.yaml").read_text(encoding="utf-8")
    for field in META_FIELDS:
        if not re.search(r"^{}:".format(field), metadata, re.M):
            errors.append("metadata.yaml 缺少字段：{}".format(field))
    name = re.search(r"^name:\s*(\S+)", metadata, re.M)
    if name and name.group(1) != PLUGIN.name:
        errors.append("metadata.yaml 的 name 必须与插件目录名一致")

    schema = json.loads((PLUGIN / "_conf_schema.json").read_text(encoding="utf-8"))
    if not isinstance(schema, dict) or not schema:
        errors.append("_conf_schema.json 必须是非空对象")
    for key, spec in schema.items():
        if not isinstance(spec, dict):
            errors.append("配置项 {} 必须是对象".format(key))
            continue
        for field in ("description", "type", "default"):
            if field not in spec:
                errors.append("配置项 {} 缺少 {}".format(key, field))
        if spec.get("type") not in {"string", "int", "float", "bool", "list", "object"}:
            errors.append("配置项 {} 的 type 不合法".format(key))

    if errors:
        for line in errors:
            print("ERROR " + line)
        return 1
    print("插件结构校验通过：{} 个文件，{} 个配置项".format(len(REQUIRED), len(schema)))
    return 0


if __name__ == "__main__":
    sys.exit(main())

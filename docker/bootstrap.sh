#!/usr/bin/env bash
# 第一次启动时按 CPU 架构补齐 QQ 客户端和 NapCat。
# 这两个包不进镜像：它们是第三方二进制，由容器自己下载，下载结果留在数据卷里。
set -euo pipefail

DATA=/AstrBot/data
CACHE="$DATA/bootstrap"
QQROOT=/opt/qq
APP="$QQROOT/opt/QQ/resources/app"
NC="$DATA/napcat"

arch="$(dpkg --print-architecture)"
case "$arch" in
  amd64) qq_url="${QQ_DEB_AMD64:-https://qqdl.gtimg.cn/qqfile/QQNT/9.9.37/beta/816b3c79/linuxqq_3.2.35-54395_amd64.deb}" ;;
  arm64) qq_url="${QQ_DEB_ARM64:-https://qqdl.gtimg.cn/qqfile/QQNT/9.9.37/beta/816b3c79/linuxqq_3.2.35-54395_arm64.deb}" ;;
  *) echo "[bootstrap] 不支持的架构：$arch" >&2; exit 1 ;;
esac
napcat_url="${NAPCAT_ZIP_URL:-https://github.com/XingYunStar/NapCatQQ/releases/download/v4.18.42/NapCat.Shell.zip}"

mkdir -p "$CACHE" "$NC/config" "$NC/logs" "$NC/cache" "$NC/plugins" "$NC/home" "$DATA/plugins"

# 没挂源码时，用镜像里自带的插件补一份
if [[ ! -d "$DATA/plugins/astrbot_plugin_echo" ]]; then
  cp -a /opt/echo/plugin/astrbot_plugin_echo "$DATA/plugins/"
fi

# 链接解析插件为第三方 AGPL 项目，源码不进镜像，按固定版本下载到数据卷。
resolver_ref="${LINK_RESOLVER_REF:-5562b96b5b528a78bd9851197f4ea35d3285d9e4}"
resolver_dir="$DATA/plugins/astrbot_plugin_link_resolver"
if [[ ! -f "$resolver_dir/metadata.yaml" ]]; then
  echo "[bootstrap] 下载链接解析插件"
  curl -fL --retry 3 -o "$CACHE/link_resolver.tar.gz" \
    "https://github.com/vacacia/astrbot_plugin_link_resolver/archive/${resolver_ref}.tar.gz"
  rm -rf "$CACHE/link_resolver" "$resolver_dir"
  mkdir -p "$CACHE/link_resolver"
  tar -xzf "$CACHE/link_resolver.tar.gz" -C "$CACHE/link_resolver" --strip-components=1
  mv "$CACHE/link_resolver" "$resolver_dir"
  echo "[bootstrap] 链接解析插件就绪"
fi

if [[ ! -f "$APP/loadNapCat.cjs" ]]; then
  echo "[bootstrap] 第一次启动，下载 QQ 客户端（$arch）和 NapCat，可能要几分钟"
  curl -fL --retry 3 -o "$CACHE/qq.deb" "$qq_url"
  curl -fL --retry 3 -o "$CACHE/napcat.zip" "$napcat_url"
  mkdir -p "$QQROOT"
  dpkg-deb -x "$CACHE/qq.deb" "$QQROOT"
  python3 - "$APP" "$CACHE/napcat.zip" <<'PY'
import json, sys, zipfile
from pathlib import Path

app, archive = map(Path, sys.argv[1:])
with zipfile.ZipFile(archive) as release:
    for name in release.namelist():
        if not name.startswith("config/"):
            release.extract(name, app / "napcat")

package = app / "package.json"
settings = json.loads(package.read_text(encoding="utf-8"))
settings["main"] = "./loadNapCat.cjs"
package.write_text(json.dumps(settings, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
(app / "loadNapCat.cjs").write_text(
    "const { pathToFileURL } = require('node:url');\n"
    "const path = require('node:path');\n"
    "import(pathToFileURL(path.join(__dirname, 'napcat', 'napcat.mjs')).href);\n",
    encoding="utf-8",
)
PY
  for name in config logs cache plugins; do
    [[ -e "$APP/napcat/$name" ]] || ln -s "$NC/$name" "$APP/napcat/$name"
  done
  echo "[bootstrap] 客户端装好了"
fi

# NapCat 的反向 WebSocket 连到同一个容器里的 AstrBot
python3 - "$NC/config" "${ONEBOT_TOKEN:-}" "${BOT_QQ:-}" <<'PY'
import json, sys
from pathlib import Path

cfg_dir, token, qq = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
client = {
    "enable": True,
    "name": "AstrBot",
    "url": "ws://127.0.0.1:6199/ws",
    "reportSelfMessage": False,
    "enableForcePushEvent": True,
    "messagePostFormat": "array",
    "token": token,
    "debug": False,
    "heartInterval": 30000,
}
config = {
    "network": {
        "httpServers": [], "httpSseServers": [], "httpClients": [],
        "websocketServers": [], "websocketClients": [client], "plugins": [],
    },
    "musicSignUrl": "", "enableLocalFile2Url": False,
    "parseMultMsg": False, "imageDownloadProxy": "",
}
names = ["onebot11.json"] + ([f"onebot11_{qq}.json"] if qq.isdigit() else [])
for name in names:
    path = cfg_dir / name
    if not path.exists():
        path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY

# 把 OneBot v11 平台补进面板配置，省得手动创建
python3 - "${ONEBOT_TOKEN:-}" <<'PY'
import copy, importlib, json, sys
from pathlib import Path

path = Path("/AstrBot/data/cmd_config.json")
token = sys.argv[1]
try:
    cfg = json.loads(path.read_text(encoding="utf-8-sig"))
except FileNotFoundError:
    try:
        mod = importlib.import_module("astrbot.core.config.default")
        found = [getattr(mod, n) for n in dir(mod)
                 if isinstance(getattr(mod, n, None), dict) and "platform" in getattr(mod, n)]
        cfg = copy.deepcopy(found[0]) if found else None
    except Exception:
        cfg = None

if cfg is None:
    print("[bootstrap] 面板配置还没生成，若没看到 OneBot 平台请手动添加一次")
else:
    platforms = cfg.setdefault("platform", [])
    if not any(isinstance(x, dict) and x.get("type") == "aiocqhttp" for x in platforms):
        platforms.append({
            "id": "napcat", "type": "aiocqhttp", "enable": True,
            "ws_reverse_host": "0.0.0.0", "ws_reverse_port": 6199,
            "ws_reverse_token": token,
        })
        path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY

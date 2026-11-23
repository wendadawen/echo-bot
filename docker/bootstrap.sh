#!/usr/bin/env bash
# 第一次启动时按 CPU 架构补齐 QQ 客户端和 NapCat，并同步内置插件进命名卷。
# 第三方二进制由容器自己下载，下载结果留在命名卷里；默认不挂载宿主机目录。
set -euo pipefail
umask 077

CURL_OPTS=(--proto '=https' --proto-redir '=https' --connect-timeout 15 --max-time 300 --retry 2)

normalize_github_proxy() {
  local raw="${1:-}"
  if [[ -z "$raw" ]]; then
    return 0
  fi
  if [[ "$raw" =~ ^https://[^[:space:]]+$ ]]; then
    printf '%s/\n' "${raw%/}"
  else
    echo "[bootstrap] 忽略无效的 GITHUB_PROXY（仅支持 https:// 前缀）" >&2
  fi
}

verify_artifact() {
  local path="$1"
  local expected_sha256="${2:-}"
  local mode="${3:-}"
  python3 - "$path" "$expected_sha256" "$mode" <<'PY'
import hashlib, posixpath, sys, tarfile, zipfile
from pathlib import Path, PurePosixPath

path = Path(sys.argv[1])
expected_sha = sys.argv[2].strip().lower()
mode = sys.argv[3].strip()

if not path.is_file() or path.stat().st_size <= 0:
    sys.exit(1)

if expected_sha:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(1048576)
            if not chunk:
                break
            h.update(chunk)
    if h.hexdigest().lower() != expected_sha:
        sys.exit(2)

if mode == "zip":
    try:
        with zipfile.ZipFile(path) as zf:
            if zf.testzip() is not None:
                sys.exit(3)
            for name in zf.namelist():
                if name.startswith("/") or ".." in PurePosixPath(name).parts:
                    sys.exit(4)
    except Exception:
        sys.exit(3)
elif mode == "tar_plugin":
    found_metadata = False
    try:
        with tarfile.open(path, "r:gz") as tf:
            for m in tf.getmembers():
                if m.name.startswith("/") or ".." in PurePosixPath(m.name).parts:
                    sys.exit(5)
                if m.ischr() or m.isblk() or m.isfifo() or m.issym() or m.islnk() or m.isdev():
                    sys.exit(6)
                parts = PurePosixPath(m.name).parts
                if m.isfile() and len(parts) == 2 and parts[1] == "metadata.yaml":
                    found_metadata = True
    except Exception:
        sys.exit(5)
    if not found_metadata:
        sys.exit(8)
elif mode == "deb":
    with path.open("rb") as fh:
        if fh.read(8) != b"!<arch>\n":
            sys.exit(9)
PY
}

download_file() {
  local url="$1"
  local dest="$2"
  local expected_sha256="${3:-}"
  local mode="${4:-}"

  if [[ -s "$dest" ]]; then
    if verify_artifact "$dest" "$expected_sha256" "$mode" 2>/dev/null; then
      return 0
    fi
    echo "[bootstrap] 缓存文件校验未通过，重新下载：$dest" >&2
    rm -f "$dest"
  fi

  local gh_proxy
  gh_proxy="$(normalize_github_proxy "${GITHUB_PROXY:-}")"
  local candidates=()
  if [[ -n "$gh_proxy" && "$url" =~ ^https://(github\.com|raw\.githubusercontent\.com)/ ]]; then
    candidates=("${gh_proxy}${url}" "$url")
  else
    candidates=("$url")
  fi

  local candidate
  for candidate in "${candidates[@]}"; do
    rm -f "${dest}.part"
    if curl -fL "${CURL_OPTS[@]}" -o "${dest}.part" -- "$candidate" \
       && verify_artifact "${dest}.part" "$expected_sha256" "$mode" 2>/dev/null; then
      mv -f "${dest}.part" "$dest"
      return 0
    fi
    echo "[bootstrap] 下载或校验失败，尝试下一来源：$candidate" >&2
    rm -f "${dest}.part"
  done

  echo "[bootstrap] 下载失败：$url" >&2
  return 1
}

# --- bootstrap main ---
DATA=/AstrBot/data
CACHE="$DATA/bootstrap"
QQROOT=/opt/qq
APP="$QQROOT/opt/QQ/resources/app"
NC="$DATA/napcat"

DEFAULT_NAPCAT_URL="https://github.com/XingYunStar/NapCatQQ/releases/download/v4.18.42/NapCat.Shell.zip"
DEFAULT_NAPCAT_SHA256="02e5ff0f79916c1e04c81c1b4590056406fb4293f1f1300c955e71fd08b2829d"

arch="$(dpkg --print-architecture)"
case "$arch" in
  amd64) qq_url="${QQ_DEB_AMD64:-https://qqdl.gtimg.cn/qqfile/QQNT/9.9.37/beta/816b3c79/linuxqq_3.2.35-54395_amd64.deb}" ;;
  arm64) qq_url="${QQ_DEB_ARM64:-https://qqdl.gtimg.cn/qqfile/QQNT/9.9.36/beta/9ee04bef/linuxqq_3.2.34-53644_arm64.deb}" ;;
  *) echo "[bootstrap] 不支持的架构：$arch" >&2; exit 1 ;;
esac
napcat_url="${NAPCAT_ZIP_URL:-$DEFAULT_NAPCAT_URL}"
napcat_sha256="${NAPCAT_ZIP_SHA256:-}"
if [[ -z "$napcat_sha256" && "$napcat_url" == "$DEFAULT_NAPCAT_URL" ]]; then
  napcat_sha256="$DEFAULT_NAPCAT_SHA256"
fi

mkdir -p "$CACHE" "$NC/config" "$NC/logs" "$NC/cache" "$NC/plugins" "$NC/home" \
  "$DATA/plugins" "$DATA/config" "$DATA/plugin_data"
chmod 700 "$CACHE" "$NC/config" "$NC/home" "$DATA/config" "$DATA/plugin_data" 2>/dev/null || true

# 默认同步镜像内置插件到数据卷；仅在显式独立挂载了插件目录时保留外部挂载内容。
if ! grep -Fq " $DATA/plugins/astrbot_plugin_echo " /proc/self/mountinfo 2>/dev/null; then
  rm -rf "$DATA/plugins/astrbot_plugin_echo"
  cp -a /opt/echo/plugin/astrbot_plugin_echo "$DATA/plugins/astrbot_plugin_echo"
  chmod -R u=rwX,go=rX "$DATA/plugins/astrbot_plugin_echo"
fi

# 链接解析插件为第三方 AGPL 项目，源码不进镜像，按固定版本下载到数据卷。
resolver_ref="${LINK_RESOLVER_REF:-5562b96b5b528a78bd9851197f4ea35d3285d9e4}"
resolver_url="${LINK_RESOLVER_URL:-https://github.com/vacacia/astrbot_plugin_link_resolver/archive/${resolver_ref}.tar.gz}"
resolver_dir="$DATA/plugins/astrbot_plugin_link_resolver"
if [[ ! -f "$resolver_dir/metadata.yaml" ]]; then
  echo "[bootstrap] 下载链接解析插件"
  download_file "$resolver_url" "$CACHE/link_resolver.tar.gz" "${LINK_RESOLVER_SHA256:-}" "tar_plugin"
  rm -rf "$CACHE/link_resolver" "$resolver_dir"
  mkdir -p "$CACHE/link_resolver"
  tar --no-same-owner --no-same-permissions -xzf "$CACHE/link_resolver.tar.gz" -C "$CACHE/link_resolver" --strip-components=1
  mv "$CACHE/link_resolver" "$resolver_dir"
  [[ "${KEEP_BOOTSTRAP_CACHE:-0}" == "1" ]] || rm -f "$CACHE/link_resolver.tar.gz"
  echo "[bootstrap] 链接解析插件就绪"
fi

if [[ ! -f "$APP/loadNapCat.cjs" ]]; then
  echo "[bootstrap] 第一次启动，下载 QQ 客户端（$arch）和 NapCat，可能要几分钟"
  download_file "$qq_url" "$CACHE/qq.deb" "${QQ_DEB_SHA256:-}" "deb"
  download_file "$napcat_url" "$CACHE/napcat.zip" "$napcat_sha256" "zip"
  mkdir -p "$QQROOT"
  dpkg-deb -x "$CACHE/qq.deb" "$QQROOT"
  python3 - "$APP" "$CACHE/napcat.zip" <<'PY'
import json, sys, zipfile
from pathlib import Path, PurePosixPath

app, archive = map(Path, sys.argv[1:])
target_root = app / "napcat"
target_root.mkdir(parents=True, exist_ok=True)
resolved_root = target_root.resolve()
with zipfile.ZipFile(archive) as release:
    for member in release.infolist():
        name = member.filename
        if name.startswith("config/"):
            continue
        if name.startswith("/") or ".." in PurePosixPath(name).parts:
            raise RuntimeError(f"unsafe zip path: {name}")
        dest = (resolved_root / name).resolve()
        if dest != resolved_root and resolved_root not in dest.parents:
            raise RuntimeError(f"zip path escapes root: {name}")
        release.extract(member, target_root)

package = app / "package.json"
settings = json.loads(package.read_text(encoding="utf-8"))
settings["main"] = "./loadNapCat.cjs"
package.write_text(json.dumps(settings, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY
  for name in config logs cache plugins; do
    [[ -e "$APP/napcat/$name" ]] || ln -s "$NC/$name" "$APP/napcat/$name"
  done
  cat > "$APP/loadNapCat.cjs" <<'JS'
const { pathToFileURL } = require('node:url');
const path = require('node:path');
import(pathToFileURL(path.join(__dirname, 'napcat', 'napcat.mjs')).href);
JS
  [[ "${KEEP_BOOTSTRAP_CACHE:-0}" == "1" ]] || rm -f "$CACHE/qq.deb" "$CACHE/napcat.zip"
  echo "[bootstrap] 客户端装好了"
fi

# 单一事实来源管理 OneBot 鉴权 Token，写 NapCat webui / onebot11 配置并同步 AstrBot cmd_config.json。
NAPCAT_WEBUI_TOKEN_ENV="${NAPCAT_WEBUI_TOKEN:-}" \
ONEBOT_TOKEN_ENV="${ONEBOT_TOKEN:-}" \
BOT_QQ_VALUE="${BOT_QQ:-}" \
python3 - "$CACHE/onebot_token" "$NC/config" "/AstrBot/data/cmd_config.json" <<'PY'
import copy, importlib, json, os, secrets, sys
from pathlib import Path

token_path = Path(sys.argv[1])
nc_cfg_dir = Path(sys.argv[2])
cmd_cfg_path = Path(sys.argv[3])
env_token = os.environ.get("ONEBOT_TOKEN_ENV", "").strip()
webui_token = os.environ.get("NAPCAT_WEBUI_TOKEN_ENV", "").strip()
qq = os.environ.get("BOT_QQ_VALUE", "").strip()

def atomic_write_secret(path, text):
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.chmod(str(tmp), 0o600)
    os.replace(str(tmp), str(path))

if webui_token:
    webui_path = nc_cfg_dir / "webui.json"
    webui_cfg = {}
    if webui_path.exists():
        try:
            loaded = json.loads(webui_path.read_text(encoding="utf-8-sig"))
            if isinstance(loaded, dict):
                webui_cfg = loaded
        except Exception:
            webui_cfg = {}
    if webui_cfg.get("token") != webui_token:
        webui_cfg["token"] = webui_token
        atomic_write_secret(webui_path, json.dumps(webui_cfg, ensure_ascii=False, indent=2) + "\n")

try:
    cmd_cfg = json.loads(cmd_cfg_path.read_text(encoding="utf-8-sig"))
    cmd_cfg_loaded = True
except FileNotFoundError:
    cmd_cfg_loaded = False
    try:
        mod = importlib.import_module("astrbot.core.config.default")
        found = [getattr(mod, n) for n in dir(mod)
                 if isinstance(getattr(mod, n, None), dict) and "platform" in getattr(mod, n)]
        cmd_cfg = copy.deepcopy(found[0]) if found else None
    except Exception:
        cmd_cfg = None

cached_token = ""
if token_path.is_file():
    try:
        cached_token = token_path.read_text(encoding="utf-8").strip()
    except OSError:
        cached_token = ""

cmd_token = ""
if isinstance(cmd_cfg, dict):
    for p in cmd_cfg.get("platform") or []:
        if isinstance(p, dict) and p.get("type") == "aiocqhttp":
            t = str(p.get("ws_reverse_token") or "").strip()
            if t:
                cmd_token = t
                break

if env_token:
    token = env_token
elif cached_token:
    token = cached_token
elif cmd_token:
    token = cmd_token
else:
    token = secrets.token_hex(24)

if token != cached_token or not token_path.exists():
    atomic_write_secret(token_path, token + "\n")

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
default_nc_cfg = {
    "network": {
        "httpServers": [], "httpSseServers": [], "httpClients": [],
        "websocketServers": [], "websocketClients": [client], "plugins": [],
    },
    "musicSignUrl": "", "enableLocalFile2Url": False,
    "parseMultMsg": False, "imageDownloadProxy": "",
}
target_nc_paths = {nc_cfg_dir / "onebot11.json"}
if qq.isdigit():
    target_nc_paths.add(nc_cfg_dir / f"onebot11_{qq}.json")
for existing in nc_cfg_dir.glob("onebot11*.json"):
    if existing.is_file() and not existing.name.endswith(".tmp"):
        target_nc_paths.add(existing)

for path in sorted(target_nc_paths):
    if not path.exists():
        atomic_write_secret(path, json.dumps(default_nc_cfg, ensure_ascii=False, indent=2) + "\n")
        continue
    try:
        nc_cfg = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        continue
    if not isinstance(nc_cfg, dict):
        continue
    clients = (nc_cfg.get("network") or {}).get("websocketClients")
    if isinstance(clients, list):
        nc_changed = False
        for c in clients:
            if isinstance(c, dict) and (c.get("name") == "AstrBot" or ":6199" in str(c.get("url") or "")):
                if c.get("token") != token:
                    c["token"] = token
                    nc_changed = True
        if nc_changed:
            atomic_write_secret(path, json.dumps(nc_cfg, ensure_ascii=False, indent=2) + "\n")

if cmd_cfg is None:
    print("[bootstrap] 面板配置还没生成，若没看到 OneBot 平台请手动添加一次")
else:
    cfg_changed = not cmd_cfg_loaded
    platforms = cmd_cfg.setdefault("platform", [])
    matched = [x for x in platforms if isinstance(x, dict) and x.get("type") == "aiocqhttp"]
    if not matched:
        platforms.append({
            "id": "napcat", "type": "aiocqhttp", "enable": True,
            "ws_reverse_host": "127.0.0.1", "ws_reverse_port": 6199,
            "ws_reverse_token": token,
        })
        cfg_changed = True
    else:
        for p in matched:
            if p.get("ws_reverse_token") != token:
                p["ws_reverse_token"] = token
                cfg_changed = True
    ps = cmd_cfg.setdefault("platform_settings", {})
    if isinstance(ps, dict):
        if ps.get("empty_mention_waiting") is not False:
            ps["empty_mention_waiting"] = False
            cfg_changed = True
        if ps.get("empty_mention_waiting_need_reply") is not False:
            ps["empty_mention_waiting_need_reply"] = False
            cfg_changed = True
    if cfg_changed:
        atomic_write_secret(cmd_cfg_path, json.dumps(cmd_cfg, ensure_ascii=False, indent=2) + "\n")
PY

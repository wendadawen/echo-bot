#!/usr/bin/env bash
# 一条命令起整套。脚本只认自己的位置，数据留在 Docker 命名卷，不挂载宿主机目录。
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"

usage() {
  cat <<'EOF'
用法：./scripts/up.sh [选项]

选项：
  --cn          国内云服务器一键加速模式（本地构建镜像 + 配置 GitHub 加速写入 .env 并启动）
  --build       使用当前环境变量本地构建镜像并启动
  --build-only  仅执行构建并写入 .env，不启动容器（可配合 --cn 使用）
  -h, --help    显示此帮助
EOF
}

read_env_value() {
  local file="$1" key="$2"
  [[ -f "$file" ]] || return 0
  awk -F= -v k="$key" '
    /^[[:space:]]*#/ || /^[[:space:]]*$/ { next }
    {
      line_k = $1
      sub(/^[[:space:]]+/, "", line_k)
      sub(/[[:space:]]+$/, "", line_k)
      if (line_k == k) {
        val = substr($0, index($0, "=") + 1)
        sub(/^[[:space:]]+/, "", val)
        sub(/[[:space:]]+$/, "", val)
        gsub(/^["'\'']|["'\'']$/, "", val)
        last = val
      }
    }
    END { if (last != "") print last }
  ' "$file"
}

upsert_env_key() {
  local file="$1" key="$2" value="$3"
  if command -v python3 >/dev/null 2>&1; then
    python3 - "$file" "$key" "$value" <<'PY'
import os, sys
from pathlib import Path

path = Path(sys.argv[1])
key, val = sys.argv[2], sys.argv[3]
lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
out = []
replaced = False
for line in lines:
    stripped = line.strip()
    if stripped and not stripped.startswith("#") and "=" in line:
        k = line.split("=", 1)[0].strip()
        if k == key:
            if not replaced:
                out.append(f"{key}={val}")
                replaced = True
            continue
    out.append(line)
if not replaced:
    out.append(f"{key}={val}")
tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}")
fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w", encoding="utf-8") as fh:
    fh.write("\n".join(out) + ("\n" if out else ""))
os.chmod(str(tmp), 0o600)
os.replace(str(tmp), str(path))
PY
  else
    local tmp="${file}.tmp.$$"
    touch "$file"
    awk -v k="$key" -v v="$value" '
      BEGIN { done = 0 }
      {
        raw = $0
        if (raw !~ /^[[:space:]]*#/ && index(raw, "=") > 0) {
          line_k = substr(raw, 1, index(raw, "=") - 1)
          gsub(/^[[:space:]]+|[[:space:]]+$/, "", line_k)
          if (line_k == k) {
            if (!done) {
              print k "=" v
              done = 1
            }
            next
          }
        }
        print raw
      }
      END { if (!done) print k "=" v }
    ' "$file" > "$tmp"
    chmod 600 "$tmp"
    mv -f "$tmp" "$file"
  fi
}

mode_cn=0
do_build=0
build_only=0

for arg in "$@"; do
  case "$arg" in
    --cn)
      mode_cn=1
      do_build=1
      ;;
    --build)
      do_build=1
      ;;
    --build-only)
      do_build=1
      build_only=1
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "未知参数：$arg" >&2
      usage >&2
      exit 2
      ;;
  esac
done

env_file="$root/.env"
if [[ "$mode_cn" -eq 0 && "$(read_env_value "$env_file" "DEPLOY_MODE")" == "cn" ]]; then
  mode_cn=1
  do_build=1
fi

command -v docker >/dev/null 2>&1 || { echo "没找到 Docker" >&2; exit 1; }
docker compose version >/dev/null 2>&1 || { echo "需要 Docker Compose V2" >&2; exit 1; }

if [[ "$mode_cn" -eq 1 ]]; then
  BASE_IMAGE="${BASE_IMAGE:-soulter/astrbot:v4.29.4}"
  APT_MIRROR="${APT_MIRROR:-http://mirrors.cloud.tencent.com}"
  PIP_INDEX_URL="${PIP_INDEX_URL:-https://mirrors.cloud.tencent.com/pypi/simple}"
  GITHUB_PROXY="${GITHUB_PROXY:-https://gh-proxy.com}"
  BOT_IMAGE="${BOT_IMAGE:-echo-bot:cn}"
fi

if [[ "$do_build" -eq 1 ]]; then
  target_image="${BOT_IMAGE:-echo-bot:local}"
  build_args=()
  [[ -n "${BASE_IMAGE:-}" ]] && build_args+=(--build-arg "BASE_IMAGE=${BASE_IMAGE}")
  [[ -n "${APT_MIRROR:-}" ]] && build_args+=(--build-arg "APT_MIRROR=${APT_MIRROR}")
  [[ -n "${PIP_INDEX_URL:-}" ]] && build_args+=(--build-arg "PIP_INDEX_URL=${PIP_INDEX_URL}")
  [[ -n "${GITHUB_PROXY:-}" ]] && build_args+=(--build-arg "GITHUB_PROXY=${GITHUB_PROXY}")

  echo "[up] 构建镜像 ${target_image} ..."
  if [[ ${#build_args[@]} -gt 0 ]]; then
    docker build "${build_args[@]}" -t "$target_image" .
  else
    docker build -t "$target_image" .
  fi

  upsert_env_key "$env_file" "BOT_IMAGE" "$target_image"
  if [[ -n "${GITHUB_PROXY:-}" ]]; then
    upsert_env_key "$env_file" "GITHUB_PROXY" "$GITHUB_PROXY"
  fi
fi

if [[ "$build_only" -eq 1 ]]; then
  echo "[up] 镜像构建完成，未启动容器（--build-only）。"
  exit 0
fi

docker compose up -d --wait

panel="$(docker compose port astrbot 6185 2>/dev/null | tail -1 || true)"
napcat="$(docker compose port astrbot 6099 2>/dev/null | tail -1 || true)"
panel_port="${panel##*:}"
napcat_port="${napcat##*:}"
echo
echo "起来了："
echo "  AstrBot 面板： http://127.0.0.1:${panel_port:-6185}"
echo "  NapCat  扫码： http://127.0.0.1:${napcat_port:-6099}"
echo "首次登录用户名一般是 astrbot，密码在日志里："
echo "  docker compose logs --tail 100 astrbot | grep -i password"
echo "进面板启用「回声」插件并填配置。"
echo "停掉： ./scripts/down.sh（Docker 命名卷 echo_bot_data 会留着）"

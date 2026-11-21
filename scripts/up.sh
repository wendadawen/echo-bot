#!/usr/bin/env bash
# 一条命令起整套。脚本只认自己的位置，换目录、换机器都不用改。
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"

command -v docker >/dev/null 2>&1 || { echo "没找到 Docker" >&2; exit 1; }
docker compose version >/dev/null 2>&1 || { echo "需要 Docker Compose V2" >&2; exit 1; }

mkdir -p data/plugins
docker compose up -d --wait

panel="$(docker compose port astrbot 6185 2>/dev/null | tail -1)"
port="${panel##*:}"
echo
echo "起来了： http://127.0.0.1:${port:-6185}"
echo "首次登录用户名一般是 astrbot，密码在日志里："
echo "  docker compose logs --tail 100 astrbot | grep -i password"
echo "进面板启用「回声」插件并填配置。"
echo "停掉： ./scripts/down.sh（data/ 会留着）"

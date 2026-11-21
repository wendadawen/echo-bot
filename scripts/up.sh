#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"

if ! command -v docker >/dev/null 2>&1; then
  echo "没找到 Docker，先装 Docker Desktop"
  exit 1
fi

mkdir -p data/plugins
docker compose up -d

echo
echo "面板：http://127.0.0.1:6185"
echo "初始用户名一般是 astrbot，密码在启动日志里："
echo "  docker compose logs --tail 100 astrbot | grep -i password"
echo "插件已挂在容器里，进面板启用「回声」并填配置。"
echo "停掉：docker compose down（data/ 里的配置和聊天记录会留着）"

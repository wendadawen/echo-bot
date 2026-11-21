#!/usr/bin/env bash
# 停掉这套服务，保留配置和聊天记录。
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"
docker compose down
echo "停好了。data/ 里的配置和聊天记录还在。"

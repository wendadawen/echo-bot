#!/usr/bin/env bash
# 先补齐第三方客户端，再把 QQ/NapCat 和 AstrBot 一起交给 supervisord 管。
set -euo pipefail
/usr/local/bin/bootstrap.sh
exec /usr/bin/supervisord -n -c /etc/supervisor/echo-bot.conf

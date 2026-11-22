#!/usr/bin/env bash
# 先补齐第三方客户端，再把 QQ/NapCat 和 AstrBot 一起交给 supervisord 管。
set -euo pipefail
/usr/local/bin/bootstrap.sh
unset ONEBOT_TOKEN NAPCAT_WEBUI_TOKEN QQ_DEB_AMD64 QQ_DEB_ARM64 NAPCAT_ZIP_URL LINK_RESOLVER_REF
exec /usr/bin/supervisord -n -c /etc/supervisor/echo-bot.conf

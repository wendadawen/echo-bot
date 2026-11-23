#!/usr/bin/env bash
# 先补齐第三方客户端，再把 QQ/NapCat 和 AstrBot 一起交给 supervisord 管。
set -euo pipefail

# 统一代理环境变量，保证环回地址直连；未配置代理时清理空值以免影响 curl/Python/Codex。
http_p="${HTTP_PROXY:-${http_proxy:-}}"
https_p="${HTTPS_PROXY:-${https_proxy:-}}"
if [[ -n "$http_p" || -n "$https_p" ]]; then
  if [[ -n "$http_p" ]]; then
    export HTTP_PROXY="$http_p" http_proxy="$http_p"
  else
    unset HTTP_PROXY http_proxy
  fi
  if [[ -n "$https_p" ]]; then
    export HTTPS_PROXY="$https_p" https_proxy="$https_p"
  else
    unset HTTPS_PROXY https_proxy
  fi
  base_no_proxy="${NO_PROXY:-${no_proxy:-}}"
  merged_no_proxy="$base_no_proxy"
  for host in 127.0.0.1 localhost ::1; do
    case ",${merged_no_proxy}," in
      *,"${host}",*) ;;
      *) merged_no_proxy="${merged_no_proxy:+${merged_no_proxy},}${host}" ;;
    esac
  done
  export NO_PROXY="$merged_no_proxy" no_proxy="$merged_no_proxy"
else
  unset HTTP_PROXY HTTPS_PROXY NO_PROXY http_proxy https_proxy no_proxy
fi

/usr/local/bin/bootstrap.sh
unset ONEBOT_TOKEN NAPCAT_WEBUI_TOKEN QQ_DEB_AMD64 QQ_DEB_ARM64 QQ_DEB_SHA256 NAPCAT_ZIP_URL NAPCAT_ZIP_SHA256 LINK_RESOLVER_REF LINK_RESOLVER_URL LINK_RESOLVER_SHA256 GITHUB_PROXY
exec /usr/bin/supervisord -n -c /etc/supervisor/echo-bot.conf

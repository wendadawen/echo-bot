# 全合一镜像：AstrBot + 回声插件 + 首次启动时补齐 QQ/NapCat 与链接解析插件
# 官方基础镜像自带 Python 3.12 与 AstrBot 全部依赖，并提供 amd64 / arm64
ARG BASE_IMAGE=soulter/astrbot:v4.29.4
FROM ${BASE_IMAGE}

ENV DEBIAN_FRONTEND=noninteractive \
    TZ=Asia/Shanghai \
    PYTHONUNBUFFERED=1

# QQ 客户端要一块虚拟屏幕和一批图形/网络库；ffmpeg 用于 B站音视频合并。
RUN apt-get update && apt-get install -y --no-install-recommends \
      xvfb xauth supervisor unzip ca-certificates ffmpeg curl \
      libgtk-3-0t64 libasound2t64 libatk-bridge2.0-0t64 libnss3 libgbm1 \
      libxss1 libxkbcommon0 libsecret-1-0 libxtst6 libx11-xcb1 libxrandr2 libcups2t64 \
    && rm -rf /var/lib/apt/lists/*

# Codex CLI（headless harness），锁定版本并按架构校验，包内自带 bwrap。
ARG CODEX_VERSION=0.171.0
ARG CODEX_SHA_ARM64=d8c43bee6cf11b01eb18b95f4d7961092bc40228180808859e70dabb7784c1c9
ARG CODEX_SHA_AMD64=8391d67757496bfd14893eca6cbf0c96994bf2627566ff1f018f70600c9ef6b3
ARG GITHUB_PROXY=""
ARG PIP_INDEX_URL=""
RUN set -eux; \
    arch="$(dpkg --print-architecture)"; \
    case "$arch" in \
      arm64) pkg="codex-package-aarch64-unknown-linux-musl.tar.gz"; sha="$CODEX_SHA_ARM64" ;; \
      amd64) pkg="codex-package-x86_64-unknown-linux-musl.tar.gz"; sha="$CODEX_SHA_AMD64" ;; \
      *) echo "unsupported architecture: $arch"; exit 1 ;; \
    esac; \
    codex_url="https://github.com/openai/codex/releases/download/rust-v${CODEX_VERSION}/${pkg}"; \
    downloaded=0; \
    if printf '%s' "$GITHUB_PROXY" | grep -Eq '^https://[^[:space:]]+$'; then \
      proxy_url="${GITHUB_PROXY%/}/${codex_url}"; \
      if curl -fsSL --proto '=https' --proto-redir '=https' --connect-timeout 15 --max-time 300 -o /tmp/codex.tgz -- "$proxy_url" \
         && echo "$sha  /tmp/codex.tgz" | sha256sum -c -; then \
        downloaded=1; \
      else \
        rm -f /tmp/codex.tgz; \
      fi; \
    fi; \
    if [ "$downloaded" -eq 0 ]; then \
      curl -fsSL --proto '=https' --proto-redir '=https' --connect-timeout 15 --max-time 300 -o /tmp/codex.tgz -- "$codex_url"; \
      echo "$sha  /tmp/codex.tgz" | sha256sum -c -; \
    fi; \
    mkdir -p /opt/codex; \
    tar -xzf /tmp/codex.tgz -C /opt/codex; \
    ln -sf /opt/codex/bin/codex /usr/local/bin/codex; \
    rm -f /tmp/codex.tgz; \
    /usr/local/bin/codex --version

# 链接解析插件的源码在首次启动时下载，依赖预装在镜像里。
RUN set -eux; \
    set -- httpx "bilibili-api-python>=17.4.0,<18.0.0" msgspec Pillow aiohttp \
      "gmssl>=3.2.2,<4.0.0" "yt-dlp>=2026.8.19"; \
    if [ -n "$PIP_INDEX_URL" ]; then \
      pip install --no-cache-dir -i "$PIP_INDEX_URL" "$@"; \
    else \
      pip install --no-cache-dir "$@"; \
    fi

# 插件内置在镜像里，启动时由 bootstrap 同步进数据卷（默认不挂载宿主机目录）。
COPY plugin/astrbot_plugin_echo /opt/echo/plugin/astrbot_plugin_echo
COPY docker/bootstrap.sh /usr/local/bin/bootstrap.sh
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
COPY docker/supervisord.conf /etc/supervisor/echo-bot.conf
RUN chmod +x /usr/local/bin/bootstrap.sh /usr/local/bin/entrypoint.sh \
    && mkdir -p /AstrBot/data/plugins /opt/qq

VOLUME ["/AstrBot/data"]
EXPOSE 6185 6099

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]

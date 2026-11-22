# 全合一镜像：AstrBot + 回声插件 + 首次启动时补齐 QQ/NapCat 与链接解析插件
# 官方基础镜像自带 Python 3.12 与 AstrBot 全部依赖，并提供 amd64 / arm64
FROM soulter/astrbot:v4.29.4

ENV DEBIAN_FRONTEND=noninteractive \
    TZ=Asia/Shanghai \
    PYTHONUNBUFFERED=1

# QQ 客户端要一块虚拟屏幕和一批图形/网络库；ffmpeg 用于 B站音视频合并。
RUN apt-get update && apt-get install -y --no-install-recommends \
      xvfb xauth supervisor unzip ca-certificates ffmpeg \
      libgtk-3-0t64 libasound2t64 libatk-bridge2.0-0t64 libnss3 libgbm1 \
      libxss1 libxkbcommon0 libsecret-1-0 libxtst6 libx11-xcb1 libxrandr2 libcups2t64 \
    && rm -rf /var/lib/apt/lists/*

# 链接解析插件的源码在首次启动时下载，依赖预装在镜像里。
RUN pip install --no-cache-dir \
      httpx "bilibili-api-python>=17.4.0,<18.0.0" msgspec Pillow aiohttp \
      "gmssl>=3.2.2,<4.0.0" "yt-dlp>=2026.8.19"

# 插件自带一份；compose 会挂仓库源码覆盖它，改代码即时生效。
COPY plugin/astrbot_plugin_echo /opt/echo/plugin/astrbot_plugin_echo
COPY docker/bootstrap.sh /usr/local/bin/bootstrap.sh
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
COPY docker/supervisord.conf /etc/supervisor/echo-bot.conf
RUN chmod +x /usr/local/bin/bootstrap.sh /usr/local/bin/entrypoint.sh \
    && mkdir -p /AstrBot/data/plugins /opt/qq

VOLUME ["/AstrBot/data"]
EXPOSE 6185 6199 6099

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]

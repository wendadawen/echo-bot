# echo-bot

群聊机器人 echo-bot。

## 部署

```bash
git clone https://github.com/wendadawen/echo-bot.git
cd echo-bot
./scripts/up.sh        # 海外/直连环境
./scripts/up.sh --cn   # 国内云服务器一键加速（腾讯云 APT/PyPI + gh-proxy.com）
```

数据与配置持久化在 Docker 命名卷 `echo_bot_data`（不挂载宿主机目录）。

| 端口 | 用途 |
| --- | --- |
| 6185 | AstrBot 管理面板（默认仅本机 `127.0.0.1`） |
| 6099 | NapCat WebUI（扫码登录，默认仅本机 `127.0.0.1`） |

## 国内云服务器镜像加速

1. 直接执行 `./scripts/up.sh --cn` 即可自动完成国内加速构建与启动（会在 `.env` 写入 `BOT_IMAGE=echo-bot:cn` 和 `GITHUB_PROXY=https://gh-proxy.com`）；若希望以后裸跑 `./scripts/up.sh` 也沿用加速构建，可在 `.env` 中加 `DEPLOY_MODE=cn`。
2. 也可复制 `.env.example` 为 `.env`，按需覆盖 `IMAGE_REGISTRY`（GHCR 镜像站前缀）、`BOT_IMAGE`（自定义镜像仓，如个人阿里云 ACR）、`APT_MIRROR`、`PIP_INDEX_URL`、`GITHUB_PROXY` 或 `HTTP_PROXY`（QQ 进程已强制直连，不走容器代理）。
3. 如需在 GitHub Actions 构建时同步双推到国内个人镜像仓（如阿里云 ACR），在仓库 Settings → Variables/Secrets 配置 `MIRROR_IMAGE`、`MIRROR_USERNAME`、`MIRROR_PASSWORD`（可选 `MIRROR_REGISTRY`）即可自动开启双推。
4. 宿主机 Docker daemon 可按云厂商文档在 `/etc/docker/daemon.json` 配置 `registry-mirrors` 加速基础镜像拉取。

## 许可

MIT，见 [LICENSE](LICENSE)。

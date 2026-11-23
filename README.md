# echo-bot

群聊机器人 echo-bot。

## 部署

```bash
git clone https://github.com/wendadawen/echo-bot.git
cd echo-bot
./scripts/up.sh
```

数据与配置持久化在 Docker 命名卷 `echo_bot_data`（不挂载宿主机目录）。

| 端口 | 用途 |
| --- | --- |
| 6185 | AstrBot 管理面板（默认仅本机 `127.0.0.1`） |
| 6099 | NapCat WebUI（扫码登录，默认仅本机 `127.0.0.1`） |

## 国内云服务器镜像加速

1. 复制 `.env.example` 为 `.env`，按需开启 `IMAGE_REGISTRY`（GHCR 镜像站前缀）或 `BOT_IMAGE`（自定义镜像仓，如个人阿里云 ACR），以及 `GITHUB_PROXY` / `HTTP_PROXY`（QQ 进程已强制直连，不走容器代理）。
2. 如需在 GitHub Actions 构建时同步双推到国内个人镜像仓（如阿里云 ACR），在仓库 Settings → Variables/Secrets 配置 `MIRROR_IMAGE`、`MIRROR_USERNAME`、`MIRROR_PASSWORD`（可选 `MIRROR_REGISTRY`）即可自动开启双推。
3. 宿主机 Docker daemon 可按云厂商文档在 `/etc/docker/daemon.json` 配置 `registry-mirrors` 加速基础镜像拉取。

## 许可

MIT，见 [LICENSE](LICENSE)。

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

## 许可

MIT，见 [LICENSE](LICENSE)。

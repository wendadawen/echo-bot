# echo-bot

基于 AstrBot 的中文群聊机器人插件，提供 @ 唤醒、按概率接话与定时提醒。消息历史与提醒按群隔离。

## 部署

环境要求：Docker、`docker compose` V2 及以上。

```bash
git clone https://github.com/wendadawen/echo-bot.git
cd echo-bot
./scripts/up.sh
```

脚本等待健康检查通过后返回。首次启动会下载 QQ 客户端与 NapCat，缓存于 `data/bootstrap`；后续启动直接使用缓存。停止服务执行 `./scripts/down.sh`。

| 端口 | 用途 |
| --- | --- |
| 6185 | AstrBot 管理面板 |
| 6199 | OneBot v11 反向 WebSocket |
| 6099 | NapCat WebUI（扫码登录） |

首次部署的面板初始用户名、密码及 WebUI 令牌见容器日志：

```bash
docker compose logs --tail 100 astrbot
```

## 配置

面板路径：插件 → 回声。

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `api_key` | 空 | 模型凭据；留空使用内置模拟回复 |
| `api_base` | `https://api.openai.com/v1` | OpenAI 兼容接口地址 |
| `model` | `gpt-4o-mini` | 模型 ID |
| `reply_chance` | `20` | 非 @ 消息接话概率（%） |
| `reminder_enabled` | `true` | 定时提醒开关 |

## 群内命令

| 命令 | 说明 |
| --- | --- |
| `/帮助` | 命令列表 |
| `/待办` | 列出本群未完成的提醒 |
| `/提醒 内容` | 按描述的时间 @ 发起人 |

## 许可

MIT，见 [LICENSE](LICENSE)。

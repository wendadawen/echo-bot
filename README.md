# echo-bot

给群聊用的一个小机器人：@ 它就回，平时偶尔接一句，还能定时提醒大家。

仓库里连 AstrBot 一起编排好了，clone 下来一条命令就能起整套，不用另外装框架。

## 装起来

需要 Docker。clone 之后：

```bash
./scripts/up.sh
```

打开 http://127.0.0.1:6185，初始用户名一般是 `astrbot`，密码在启动日志里（`docker compose logs --tail 100 astrbot`）。进面板启用「回声」，填几个配置：

- `api_key`：模型凭据。留空就用内置的模拟回复，先把流程跑通挺方便
- `api_base` / `model`：接口地址和模型名，接本机 Ollama 也行
- `reply_chance`：不 @ 时主动接话的概率，默认 20
- `persona`：人设，默认叫「回声」
- `reminder_enabled`：定时提醒开关

插件代码直接从 `plugin/` 挂进容器，改完 `docker compose restart` 就生效。配置和聊天记录都在 `data/`，不会被提交。停掉用 `docker compose down`。

Docker Hub 拉不动的话换个镜像：`ASTRBOT_IMAGE=m.daocloud.io/docker.io/soulter/astrbot:v4.29.4 ./scripts/up.sh`

接 QQ 群还得在面板配 `aiocqhttp`，另一端用 NapCat + Linux QQ 扫码登录一个小号，反向 WebSocket 连回 6199。这一步要一台 Linux amd64 的机器；macOS 本机跑得了框架，跑不了 Linux QQ。小号拉进群，@ 它发 `/帮助`，就该有反应了。

## 群里能发什么

`/提醒 5分钟 交周报`，到点在群里 @ 你；`/待办` 看还没到点的提醒；`/帮助` 列全部命令。

@ 一定回；不 @ 就按 `reply_chance` 碰运气。聊天记录和提醒都按群分开，不会串台。

## 开发

不用起容器也能跑核心逻辑：

```bash
python tools/validate_plugin.py
python -m unittest discover -s tests -p "test_*.py" -v
python tools/simulate.py
```

push 到 main 会在 Actions 里跑 3.12 / 3.13 的检查、校验 compose 编排，再装一遍 AstrBot 确认插件能加载，最后打出 zip。推 `v*` 标签自动发 Release。

本机 Docker 起过一遍，插件加载正常；真 QQ 群还没实测过。插件只接群消息，私聊不理。

代码 MIT，见 LICENSE。

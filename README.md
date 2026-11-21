# echo-bot

给群聊用的一个小机器人：@ 它就回，平时偶尔接一句，还能定时提醒大家。

一个镜像装下整套：AstrBot、回声插件，以及第一次启动时自己补齐的 QQ 客户端和 NapCat。镜像由 GitHub Actions 构建，本机不用装 Python，也不用自己编译。

## 装起来

需要 Docker（Docker Desktop 或 Colima 都行）和 Compose V2。clone 之后：

```bash
./scripts/up.sh
```

脚本会等面板真正就绪才返回。换台机器、换个目录，还是这一条命令。想改端口、容器名或下载源，把 `.env.example` 复制成 `.env` 再改；不建 `.env` 用默认值就能跑。

第一次启动，容器会自己下载 QQ 客户端和 NapCat（几百 MB，缓存在 `data/bootstrap`），要联网，可能等几分钟；之后重启直接用缓存。

起来之后有三个地址：

- `http://127.0.0.1:6099` 扫码页，用小号 QQ 登录；登录口令在日志里：`docker compose logs --tail 100 astrbot`
- `http://127.0.0.1:6185` 面板，初始用户名一般是 `astrbot`，密码也在同一份日志里
- `6199` 是 NapCat 连回来的端口，脚本已经配好，不用管

进面板填几个配置：

- `api_key`：模型凭据。留空就用内置的模拟回复，先把流程跑通挺方便
- `api_base` / `model`：接口地址和模型名，接本机 Ollama 也行
- `reply_chance`：不 @ 时主动接话的概率，默认 20
- `persona`：人设，默认叫「回声」
- `reminder_enabled`：定时提醒开关

小号拉进群，@ 它发 `/帮助`，就该有反应了。面板里如果没自动出现 OneBot 平台，手动加一个：类型 `OneBot v11`，反向 WS 主机 `0.0.0.0`、端口 `6199`。

插件代码直接从 `plugin/` 挂进容器，改完 `docker compose restart` 就生效。配置、聊天记录和 QQ 登录态都在 `data/`，不会被提交。停掉用 `./scripts/down.sh`。

镜像里不含 QQ 客户端和 NapCat——它们是第三方程序，不适合打进公开镜像里分发；启动时按当前 CPU 架构下载官方包。腾讯链接失效时，在 `.env` 里改 `QQ_DEB_AMD64` / `QQ_DEB_ARM64` / `NAPCAT_ZIP_URL`。

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

push 到 main 会在 Actions 里跑 3.12 / 3.13 的检查、校验编排和脚本语法，再装一遍 AstrBot 确认插件能加载，最后打出 zip；同时构建 amd64 / arm64 镜像推到 `ghcr.io/wendadawen/echo-bot`。推 `v*` 标签自动发 Release。

真 QQ 群的扫码和收发还没在本机实测过；插件只接群消息，私聊不理。

代码 MIT，见 LICENSE。

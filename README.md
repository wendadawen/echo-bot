# echo-bot

给群聊用的一个小机器人：按群记住外号和梗，@ 它就回，平时偶尔接一句，还能定时提醒大家。

它是 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 的插件，消息收发、后台面板、模型配置都交给框架，这里只写「收到一句话之后该干嘛」。

## 装起来

先装好 AstrBot（4.29.4 以上，Python 3.12+），把 `plugin/astrbot_plugin_echo/` 整个目录放进它的 `data/plugins/`。也可以去 Actions 下载打好的 zip，解压放进去。

重启 AstrBot，在面板里启用插件，填几个配置：

- `api_key`：模型凭据。留空就用内置的模拟回复，先把流程跑通挺方便
- `api_base` / `model`：接口地址和模型名，接本机 Ollama 也行
- `reply_chance`：不 @ 时主动接话的概率，默认 20
- `persona`：人设，默认叫「回声」
- `reminder_enabled`：定时提醒开关

接 QQ 群还得在 AstrBot 配 `aiocqhttp` 平台，另一端用 NapCat + Linux QQ 扫码登录一个小号，把反向 WebSocket 连回来。这一步要一台 Linux amd64 的机器；macOS 本机跑得了框架，跑不了 Linux QQ。

小号拉进群，@ 它发 `/帮助`，就该有反应了。

## 群里能发什么

`/记 阿伟=摸鱼冠军` 或者「记住 暗号是吃火锅」，`/记忆` 看这个群记了什么。
`/提醒 5分钟 交周报`，到点在群里 @ 你；`/待办` 看还没到点的提醒。
`/帮助` 列全部命令。

@ 一定回；不 @ 就按 `reply_chance` 碰运气。记忆、聊天记录、提醒都按群分开，不会串台。

## 开发

不用装 AstrBot 也能跑核心逻辑：

```bash
python tools/validate_plugin.py
python -m unittest discover -s tests -p "test_*.py" -v
python tools/simulate.py
```

push 到 main 会在 Actions 里跑 3.12 / 3.13 的检查，再装一遍 AstrBot 确认插件能加载，最后打出 zip。推 `v*` 标签会自动发 Release。

目前只在 CI 里验证过插件能被加载，真群还没实测过；插件只接群消息，私聊不理。数据写在 AstrBot 的数据目录里，别挪到插件目录下。

代码 MIT，见 LICENSE。

# 群友 · AstrBot 群聊插件

一个轻量的中文群聊机器人插件：按群隔离地记住外号与梗，`@` 必回、平时按概率接话，还能定时提醒。模型可接任意 OpenAI 兼容接口，不填凭据时用本地模拟回复，零配置即可试用。

当前版本 `v0.1.0`，实验性项目。

## 灵感来源与许可

本项目的设计思路（AstrBot 插件形态、按群记忆、群内提醒）受到 [lixiang0405/si「祀」](https://github.com/lixiang0405/si) 启发。**本项目为独立实现，未复制该项目的代码、素材或人设内容**；参考项目自有代码采用 MIT 许可。

使用本插件会依赖 [AstrBot](https://github.com/AstrBotDevs/AstrBot)（AGPL-3.0）。若通过 NapCat / Linux QQ 接入，还需遵守其各自的许可与腾讯客户端条款（NapCat 为有限再分发许可，Linux QQ 为闭源客户端）。本项目自有代码采用 MIT 许可，见 [LICENSE](LICENSE)。

## 安装

1. 准备 AstrBot `>=4.29.4,<5`（Python 3.12+）。
2. 把 `plugin/astrbot_plugin_qunyou/` 整个目录复制到 AstrBot 的 `data/plugins/`。
3. 在 WebUI 的「插件」页重载插件，然后在配置面板填写：

| 配置项 | 说明 | 默认 |
| --- | --- | --- |
| `reply_chance` | 普通群消息主动接话概率（%） | `20` |
| `persona` | 机器人人设，作为系统提示词 | 见配置面板 |
| `api_base` | OpenAI 兼容接口地址，可指向本机 Ollama | `https://api.openai.com/v1` |
| `api_key` | 模型凭据，留空使用本地模拟回复 | 空 |
| `model` | 模型 ID | `gpt-4o-mini` |
| `reminder_enabled` | 是否启用定时提醒 | `true` |

4. 接 QQ 群：在 AstrBot 配置 `aiocqhttp` 平台，另一端用 NapCat + Linux QQ（OneBot v11 反向 WebSocket）。这一步需要 Linux amd64 机器；macOS 本机可先不接平台，用下面的离线模拟器验证逻辑。

数据（消息历史、群记忆、提醒）由 AstrBot 的数据目录管理，写在 `data/plugin_data/` 下，不在插件目录内，升级插件不会丢。

## 群内命令

| 命令 | 作用 |
| --- | --- |
| `/记 键=值` | 记一条本群记忆，例如 `/记 阿伟=摸鱼冠军` |
| `记住 X 是 Y` | 自然语言写法，效果同上 |
| `/记忆` | 列出本群记忆 |
| `/提醒 5分钟 事情` | 到点在原群 `@` 发起人，支持秒 / 分钟 / 小时 |
| `/待办` | 查看本群尚未到点的提醒 |
| `/帮助` | 命令说明 |

`@` 机器人一定回复；不 `@` 时按 `reply_chance` 概率接话。记忆、历史和提醒都按「平台 + 群 ID」隔离。

## 构建与 CI

**本地不安装框架、不打包。** 所有构建和验证由 GitHub Actions 完成：

- `CI`：Python 3.12 / 3.13 矩阵 → 插件结构校验、语法检查、核心逻辑单测、离线模拟器；再单独安装 `astrbot==4.29.4` 做插件加载冒烟，最后把插件打成 zip 上传为 artifact。
- `Release`：推送 `v*` 标签时自动打包并创建 GitHub Release。

纯标准库的自检可以在本地跑（不需要 AstrBot）：

```bash
python tools/validate_plugin.py
python -m unittest discover -s tests -p "test_*.py" -v
python tools/simulate.py
```

## 目录结构

```
.github/workflows/           CI 与发布流水线
plugin/astrbot_plugin_qunyou/  可直接放进 data/plugins/ 的插件
  main.py                     平台适配：事件转换、回复、提醒发送
  metadata.yaml / _conf_schema.json
  core/                       零依赖核心逻辑，不 import 框架
tests/                       核心逻辑单测
tools/                       结构校验、加载冒烟、打包、离线模拟器
```

## 已知限制

- 第一版用单一群消息处理器自行解析命令，建议在 AstrBot 面板关闭默认的群聊 LLM 自动回复，避免重复应答。
- 定时提醒通过框架的主动发送接口投递；若平台适配器不支持，会记录日志但不重试，提醒内容仍可用 `/待办` 查询。
- 尚未实现图片/文件、群内网页牌桌、反馈自动修复流水线。
- 数据库第一版未做结构迁移；修改表结构后需删除数据文件重建。

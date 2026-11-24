# echo-bot (v0.2.0)

基于第一性原理与领域抽象重构的 QQ 群聊机器人「大肥鱼」（AstrBot 插件）。

## 架构概览

- `domain.py`：不可变数据模型与 6 个核心端口协议（`LLMClient` / `SearchProvider` / `MemoryRepository` / `EmojiStorePort` / `TaskRunner` / `MessageSender`）
- `adapters/`：`astrbot.py`（AstrBot+NapCat 协议防腐层）、`llm.py`（原生 `tools` + 多模态直传 + 自动降级 + 思考块清洗）、`search.py`（可插拔直搜）
- `store/`：`repository.py`（单库 `echo.sqlite3` + FTS5 记忆 + QQ 号归一人物 + 自动无损迁移旧库）、`emoji_store.py`（严进宽用表情库）
- `workers/`：`sandbox.py`（降权与防泄露沙箱 + 实时进度与喊停）、`backends/codex.py`、`manager.py`
- `engine/`：`safety.py`（硬安全防线）、`scheduler.py`（`@` 优先快车道 + 极克制偶尔接茬）、`context.py`（记忆/黑话/滚动状态/智能引用）、`agent.py`

## 许可

MIT，见 [LICENSE](https://github.com/wendadawen/echo-bot/blob/main/LICENSE)。

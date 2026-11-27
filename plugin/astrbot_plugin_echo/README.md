# echo-bot (v0.2.0)

基于第一性原理与领域抽象重构的 QQ 群聊机器人「大肥鱼」（AstrBot 插件，零外部 pip 依赖）。

## 架构概览

- `domain.py` & `text_media.py`：不可变数据模型、6 个核心端口协议（`LLMClient` / `SearchProvider` / `MemoryRepository` / `EmojiStorePort` / `TaskRunner` / `MessageSender`）与 QQ 文本/图片/表情基础处理
- `adapters/`：`astrbot.py`（入站入口）、`qq_protocol.py`（QQ/NapCat/SSRF 防御与转发解包）、`sender.py`（限流/发送与无效表情自愈）、`llm.py`（原生 `tools` + 多模态直传 + 自动降级 + 思考块清洗）、`search.py`（多级兜底联网直搜）
- `store/`：`schema.py`（WAL + 版本化 Schema 迁移）、`repository.py`（门面仓储 + 投递队列 + 监控）、`memory_store.py`（FTS5 `trigram` 记忆检索）、`person_store.py`（结构化人物档案）、`migration.py`（v1 旧库无损迁移）、`cache_cleanup.py`（符号链接安全缓存清理）、`emoji_store.py` & `emoji_classifier.py`（SQLite + `shared/` 全局共享高质量表情池与三层识别门禁）
- `workers/`：`fs_guard.py`（无符号链接原子写与导出守卫）、`sandbox.py`（`setpriv` + `prlimit` 降权与限额隔离执行）、`backends/chat_agent.py`（宿主编排型零密钥执行后端）、`backends/codex.py`（Codex CLI 沙箱后端）、`presenter.py` & `manager.py`（后台任务生命周期与重载持久化投递）
- `engine/`：`safety.py`（硬安全防线）、`scheduler.py`（`@` 优先快车道与克制接茬）、`context.py`（记忆/黑话/人物档案/智能引用）、`memory.py`（按群并发控制记忆归纳）、`tools.py` & `agent.py`（工具调度与核心决策引擎）

## 许可

MIT，见 [LICENSE](https://github.com/wendadawen/echo-bot/blob/main/LICENSE)。

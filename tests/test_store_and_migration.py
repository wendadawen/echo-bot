"""store 层测试：统一 SQLite 仓库、FTS5/双字群记忆、表情包筛选与 v1 迁移。"""

import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = ROOT / "plugin"
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from astrbot_plugin_echo.domain import PendingDelivery, PickedEmoji, detect_image_dimensions  # noqa: E402
from astrbot_plugin_echo.store.emoji_store import (  # noqa: E402
    EmojiStore,
    GLOBAL_POOL_KEY,
    acceptable,
    analyze_emoji,
    is_sticker_geometry,
)
from astrbot_plugin_echo.store.repository import Repository  # noqa: E402

TINY_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
TINY_GIF = b"GIF89a\x01\x00\x01\x00\x00\x00\x00;"


def _png_sized(w: int, h: int) -> bytes:
    return b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + w.to_bytes(4, "big") + h.to_bytes(4, "big") + b"\x08\x06\x00\x00\x00" + b"\x00" * 16


class RepositoryTests(unittest.TestCase):
    def setUp(self):
        self.repo = Repository(":memory:")

    def tearDown(self):
        self.repo.close()

    def test_messages_history_and_prune(self):
        for i in range(65):
            self.repo.add_message(
                "g1", "1001", "阿明", "user", f"第{i}条消息", ts=1000.0 + i, msg_id=f"m{i}"
            )
        recent = self.repo.recent_messages("g1", limit=10)
        self.assertEqual(len(recent), 10)
        self.assertEqual(recent[0]["msg_id"], "m55")
        self.assertEqual(recent[-1]["msg_id"], "m64")

        since = self.repo.messages_since("g1", ts=1060.0, limit=10)
        self.assertEqual([r["msg_id"] for r in since], ["m61", "m62", "m63", "m64"])

        deleted = self.repo.prune_messages(keep_per_group=50)
        self.assertEqual(deleted, 15)
        self.assertEqual(self.repo.stats("g1")["messages"], 50)

        self.repo.add_message("g1", "bot", "大肥鱼", "bot", "看到娄文启了", ts=1100.0, meta="seen_image;tools=web_search")
        latest = self.repo.recent_messages("g1", limit=1)[-1]
        self.assertEqual(latest["meta"], "seen_image;tools=web_search")

    def test_episodes_and_fts_chinese_bigram_recall(self):
        self.repo.add_episode("g1", "小和提到周末要在苏州实验室做实验", now=1000.0, source="chat", external_id="ep1")
        self.repo.add_episode("g1", "重复ID不该插入", now=1001.0, source="chat", external_id="ep1")
        self.assertEqual(self.repo.stats("g1")["episodes"], 1)

        self.repo.add_memory("g1", "event", "小和正在苏州实验室调试超导样品", now=1000.0)
        self.repo.add_memory("g1", "preference", "阿明最喜欢喝冰美式咖啡", now=1000.0, protected=True)
        self.repo.add_memory("g1", "jargon", "kk=快看看", now=1000.0)

        hits = self.repo.recall_memories("g1", "苏州实验室", limit=5, now=1005.0)
        self.assertTrue(any("苏州实验室" in h for h in hits), hits)

        # 非空无关查询不应硬塞无关记忆
        unrelated = self.repo.recall_memories("g1", "微积分期末考", limit=5, now=1005.0)
        self.assertEqual(unrelated, [])

        self.assertEqual(self.repo.group_jargon("g1"), {"kk": "快看看"})

    def test_person_identity_merge_across_rename(self):
        self.repo.observe_person("g1", "10001", "阿明", now=100.0)
        self.repo.observe_person("g1", "10001", "阿明改名了", now=200.0)
        self.repo.upsert_person(
            "g1", "10001", now=200.0, traits="爱吐槽", preferences="冰美式"
        )
        self.repo.upsert_person(
            "g1", "10001", now=210.0, traits="写Python", relation="实验室同学"
        )

        self.assertEqual(self.repo.resolve_person("g1", "阿明"), "10001")
        self.assertEqual(self.repo.resolve_person("g1", "阿明改名了"), "10001")
        self.assertEqual(self.repo.resolve_person("g1", "10001"), "10001")
        self.assertIsNone(self.repo.resolve_person("g1", "不存在的路人甲"))

        aliases = self.repo.person_aliases("g1", "10001")
        self.assertEqual(set(aliases), {"阿明", "阿明改名了"})
        summary = self.repo.person_summary("g1", "10001")
        self.assertIn("爱吐槽", summary)
        self.assertIn("写Python", summary)
        self.assertIn("冰美式", summary)
        self.assertIn("实验室同学", summary)

        # 过滤 "-" 占位词与近义重复句去重
        self.repo.upsert_person("g1", "10001", now=220.0, traits="-", events="暂无")
        self.repo.upsert_person("g1", "10001", now=221.0, relation="常拿猪调侃大肥鱼")
        self.repo.upsert_person("g1", "10001", now=222.0, relation="常拿“猪”调侃大肥鱼")
        summary2 = self.repo.person_summary("g1", "10001")
        self.assertNotIn("：", summary2.replace("印象：", "").replace("偏好：", "").replace("关系：", ""))
        self.assertEqual(summary2.count("调侃大肥鱼"), 1)

    def test_conv_state_ttl_and_clean_person_aliases(self):
        # 纯 QQ 号和通用称谓“群友”绝不写入 person_aliases（防止戳一戳等通知污染）
        self.repo.observe_person("g1", "10002", "10002", now=100.0)
        self.repo.observe_person("g1", "10002", "群友", now=101.0)
        self.assertEqual(self.repo.person_aliases("g1", "10002"), [])
        self.repo.observe_person("g1", "10002", "小张", now=102.0)
        self.assertEqual(self.repo.person_aliases("g1", "10002"), ["小张"])

        self.repo.save_conv_state(
            "g1",
            {
                "topics": ["讨论重构", "调试Docker", "t3", "t4", "t5", "t6"],
                "progress": ["已迁移存储"],
                "pending": ["跑测试"],
            },
            now=1000.0,
        )
        state = self.repo.get_conv_state("g1", now=1100.0)
        self.assertEqual(len(state["topics"]), 5)
        self.assertEqual(state["progress"], ["已迁移存储"])
        self.assertEqual(state["pending"], ["跑测试"])
        # 超过默认 2 小时 TTL 自动过期为空
        expired_state = self.repo.get_conv_state("g1", now=9000.0)
        self.assertEqual(expired_state, {"topics": [], "progress": [], "pending": []})

    def test_reload_safe_pending_delivery_after_close(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "echo.sqlite3"
            repo = Repository(db_path)
            repo.close()

            delivery = PendingDelivery(
                task_id="t123",
                group_id="g1",
                umo="qq:group:g1",
                prompt="分析日志",
                status="done",
                reason="done",
                result="发现3个报错",
                files=({"path": str(Path(tmp) / "report.md"), "name": "report.md"},),
                created_at=1234.5,
            )
            self.assertTrue(repo.save_pending_delivery(delivery))

            reopened = Repository(db_path)
            try:
                items = reopened.list_pending_deliveries()
                self.assertEqual(len(items), 1)
                self.assertEqual(items[0].task_id, "t123")
                self.assertEqual(items[0].result, "发现3个报错")
                self.assertEqual(
                    reopened.pending_delivery_paths(), {str(Path(tmp) / "report.md")}
                )
                self.assertTrue(reopened.delete_pending_delivery("t123"))
                self.assertEqual(reopened.list_pending_deliveries(), [])
            finally:
                reopened.close()

    def test_monitor_events_and_error_window(self):
        self.repo.record_event("g1", "reply", ok=True, latency=1.2, tokens=100, now=1000.0)
        self.repo.record_event("g1", "model_error", ok=False, latency=5.0, detail="timeout", now=1050.0)
        self.assertEqual(self.repo.recent_error_count("g1", window_seconds=120.0, now=1100.0), 1)
        self.assertEqual(self.repo.recent_error_count("g1", window_seconds=120.0, now=1300.0), 0)
        stats = self.repo.monitor_stats("g1", hours=24, now=1100.0)
        self.assertEqual(stats["total"], 2)
        self.assertEqual(stats["errors"], 1)
        self.assertEqual(stats["tokens"], 100)


class EmojiStoreTests(unittest.TestCase):
    def test_acceptable_and_analyze_gate(self):
        self.assertFalse(acceptable(""))
        self.assertFalse(acceptable("非表情"))
        self.assertFalse(acceptable("这是一张二维码截图"))
        self.assertFalse(acceptable("数据图表展示各模型耗时"))
        self.assertTrue(acceptable("这张图表达了累到有些无奈的状态"))
        self.assertFalse(acceptable("猫猫笑", b"x" * (2 * 1024 * 1024 + 1)))
        self.assertTrue(acceptable("一只猫翻白眼", TINY_PNG))

        llm = MagicMock()
        llm.describe_image.return_value = "无语|一只猫翻白眼"
        desc, label = analyze_emoji(llm, TINY_PNG)
        self.assertEqual(label, "无语")
        self.assertEqual(desc, "一只猫翻白眼")

        llm.describe_image.return_value = "非表情"
        desc, label = analyze_emoji(llm, TINY_PNG)
        self.assertEqual((desc, label), ("非表情", "非表情"))

        # 缺失 "|"、句子截断前缀、低俗内容均 fail-closed 拒收
        llm.describe_image.return_value = "这是一张卡通猫在哭的图片"
        self.assertEqual(analyze_emoji(llm, TINY_PNG), ("非表情", "非表情"))

        llm.describe_image.return_value = "这是一张表情包：|企鹅吐槽"
        self.assertEqual(analyze_emoji(llm, TINY_PNG), ("非表情", "非表情"))

        llm.describe_image.return_value = "低俗|露骨谐音图片"
        self.assertEqual(analyze_emoji(llm, TINY_PNG), ("非表情", "非表情"))

        # 物理尺寸门禁 + 结构化 JSON 分类门禁（拒绝 1179x2556 手机新闻长截图与 chat/news JSON）
        shot_png = _png_sized(1179, 2556)
        meme_png = _png_sized(240, 240)
        self.assertEqual(detect_image_dimensions(shot_png), (1179, 2556))
        self.assertEqual(detect_image_dimensions(TINY_GIF), (1, 1))
        self.assertFalse(is_sticker_geometry(shot_png))
        self.assertTrue(is_sticker_geometry(meme_png))

        llm.describe_image.reset_mock()
        self.assertEqual(analyze_emoji(llm, shot_png), ("非表情", "非表情"))
        llm.describe_image.assert_not_called()

        llm.describe_image.return_value = '{"kind":"news","usable":false,"tag":"非表情","desc":"新闻报道截屏"}'
        self.assertEqual(analyze_emoji(llm, meme_png), ("非表情", "非表情"))

        llm.describe_image.return_value = '{"kind":"sticker","usable":true,"tag":"委屈","desc":"新闻报道陈吉宁审查"}'
        self.assertEqual(analyze_emoji(llm, meme_png), ("非表情", "非表情"))

        llm.describe_image.return_value = '{"kind":"sticker","usable":true,"tag":"委屈巴巴","desc":"小猫捂脸委屈"}'
        self.assertEqual(analyze_emoji(llm, meme_png), ("小猫捂脸委屈", "委屈巴巴"))

    def test_collect_negative_cache_pick_and_cleanup(self):
        import json as _json

        with tempfile.TemporaryDirectory() as tmp:
            store = EmojiStore(tmp, limit=10, max_age_days=30)
            llm = MagicMock()
            llm.describe_image.side_effect = ["非表情", "开心|熊猫头鼓掌", "无语|猫猫摊手"]

            # 截图被拒收并写入负缓存，二次传入同图不重复调 VLM
            self.assertEqual(store.collect("g1", TINY_PNG, llm), "")
            self.assertEqual(store.collect("g1", TINY_PNG, llm), "")
            self.assertEqual(llm.describe_image.call_count, 1)

            # 真表情被收录至 shared/ 并同步进入 _global 共享池，新群 g2 立即可查可用且含描述
            b1 = TINY_GIF + b"1"
            b2 = TINY_GIF + b"2"
            self.assertEqual(store.collect("g1", b1, llm), "开心")
            self.assertEqual(store.collect("g1", b2, llm), "无语")
            self.assertEqual(store.collect_face("g1", "489", "QQ表情"), "")
            store.collect_face("g1", "178", "滑稽")

            self.assertTrue(store.has_entries("g1"))
            self.assertTrue(store.has_entries("g2"))
            self.assertIn("开心", store.tags("g1"))
            self.assertIn("开心", store.tags("g2"))
            picked_g2 = store.pick_entry("g2", "开心")
            self.assertIsInstance(picked_g2, PickedEmoji)
            self.assertEqual(picked_g2.kind, "image")
            self.assertEqual(picked_g2.tag, "开心")
            self.assertEqual(picked_g2.description, "熊猫头鼓掌")
            self.assertIn(str(Path(tmp) / "shared"), picked_g2.path)
            self.assertTrue(Path(picked_g2.path).is_file())

            # 经典 QQ 小黄脸仅本群可用，不跨群进 _global；g1 同关键词命中，remove_face 可踢除失效 face_id
            self.assertEqual(store.pick("g1", "滑稽"), "face:178")
            self.assertNotEqual(store.pick("g2", "滑稽"), "face:178")
            self.assertIsInstance(store.pick("g1", "未知心情"), Path)
            self.assertTrue(store.remove_face("g1", "178"))
            self.assertFalse(any(e.get("face_id") == "178" for e in store._index["g1"]))
            self.assertNotEqual(store.pick("g1", "滑稽"), "face:178")

            # 引用计数安全删除：从 g1 弹出条目时若 _global 仍引用，绝不误删 shared/ 下底层图片
            b1_path = Path(picked_g2.path)
            store._index["g1"] = [e for e in store._index["g1"] if e.get("path") != str(b1_path)]
            store._save()
            store._unlink_if_unreferenced(str(b1_path))
            self.assertTrue(b1_path.is_file())

            # 重载 _migrate_and_dedup 自动清洗历史坏 face_id、截断脏标签与 1179x2556 截屏图，并把旧群目录合法图迁移进 shared/
            (Path(tmp) / "g1").mkdir(exist_ok=True)
            legacy_good = Path(tmp) / "g1" / "legacy_ok.gif"
            legacy_bytes = TINY_GIF + b"legacy_meme"
            legacy_good.write_bytes(legacy_bytes)
            dirty_file = Path(tmp) / "g1" / "dirty.png"
            dirty_file.write_bytes(TINY_PNG)
            long_shot = Path(tmp) / "g1" / "news_shot.png"
            long_shot.write_bytes(_png_sized(1179, 2556))
            raw_idx = _json.loads((Path(tmp) / "index.json").read_text(encoding="utf-8"))
            raw_idx["g1"].extend([
                {"face_id": "489", "tag": "QQ表情", "description": "QQ表情", "ts": time.time()},
                {"path": str(dirty_file), "sha256": "deadbeef", "tag": "这是一张卡通猫的", "description": "猫", "ts": time.time()},
                {"path": str(long_shot), "sha256": "news1179", "tag": "震惊", "description": "新闻报道正接受审查", "ts": time.time()},
                {"path": str(legacy_good), "sha256": "", "tag": "呆萌", "description": "老群收藏小猫", "ts": time.time()},
            ])
            (Path(tmp) / "index.json").write_text(_json.dumps(raw_idx, ensure_ascii=False), encoding="utf-8")
            reloaded = EmojiStore(tmp, limit=10, max_age_days=30)
            self.assertFalse(dirty_file.exists())
            self.assertFalse(long_shot.exists())
            self.assertFalse(legacy_good.exists())  # 旧位置重复副本已迁移到 shared/ 后安全清理
            self.assertIn("呆萌", reloaded.tags("g2"))
            self.assertEqual([e for e in reloaded._index["g1"] if e.get("face_id") == "489"], [])

            # 超期文件清理（全桶包括 _global 过期后彻底清空并删除共享文件）
            for bucket in (reloaded._index.get("g1", []), reloaded._index.get(GLOBAL_POOL_KEY, [])):
                for entry in bucket:
                    entry["ts"] = time.time() - 40 * 86400
            reloaded.cleanup(max_age_days=30)
            self.assertFalse(reloaded.has_entries("g1"))
            self.assertFalse(reloaded.has_entries("g2"))
            self.assertFalse(b1_path.exists())


class LegacyMigrationTests(unittest.TestCase):
    def test_lossless_idempotent_migration_from_v1_dbs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            t0 = time.time() - 3600.0

            # 1) 旧版 echo.sqlite3 (无 msg_id 列 + pending_deliveries)
            with sqlite3.connect(root / "echo.sqlite3") as c:
                c.executescript(
                    f"""
                    CREATE TABLE messages (
                        seq INTEGER PRIMARY KEY AUTOINCREMENT,
                        group_id TEXT NOT NULL,
                        user_id TEXT NOT NULL,
                        user_name TEXT NOT NULL,
                        role TEXT NOT NULL,
                        text TEXT NOT NULL,
                        ts REAL NOT NULL
                    );
                    INSERT INTO messages VALUES (1, 'g1', '101', '大明', 'user', '在干嘛', {t0});
                    INSERT INTO messages VALUES (2, 'g1', 'bot', '大肥鱼', 'assistant', '摸鱼中', {t0 + 1});
                    """
                )

            # 2) 旧版 persons.sqlite3 (同时含 individuals + identities + profiles)
            with sqlite3.connect(root / "persons.sqlite3") as c:
                c.executescript(
                    f"""
                    CREATE TABLE individuals (
                        user_id TEXT PRIMARY KEY,
                        msg_count INTEGER NOT NULL DEFAULT 0,
                        first_seen REAL NOT NULL,
                        last_seen REAL NOT NULL
                    );
                    CREATE TABLE identities (
                        group_id TEXT NOT NULL,
                        user_id TEXT NOT NULL,
                        name TEXT NOT NULL,
                        kind TEXT NOT NULL DEFAULT 'nick',
                        hits INTEGER NOT NULL DEFAULT 1,
                        updated_at REAL NOT NULL,
                        PRIMARY KEY (group_id, user_id, name)
                    );
                    CREATE TABLE profiles (
                        group_id TEXT NOT NULL,
                        user_id TEXT NOT NULL,
                        traits TEXT NOT NULL DEFAULT '',
                        preferences TEXT NOT NULL DEFAULT '',
                        events TEXT NOT NULL DEFAULT '',
                        relation TEXT NOT NULL DEFAULT '',
                        intimacy REAL NOT NULL DEFAULT 0.5,
                        updated_at REAL NOT NULL,
                        PRIMARY KEY (group_id, user_id)
                    );
                    INSERT INTO individuals VALUES ('101', 42, {t0 - 50}, {t0});
                    INSERT INTO identities VALUES ('g1', '101', '大明', 'nick', 10, {t0});
                    INSERT INTO profiles VALUES ('g1', '101', '夜猫子', '乌龙茶', '考研中', '群友', 0.8, {t0});
                    """
                )

            # 3) 旧版 memory.sqlite3
            with sqlite3.connect(root / "memory.sqlite3") as c:
                c.executescript(
                    f"""
                    CREATE TABLE episodes (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        group_id TEXT NOT NULL,
                        text TEXT NOT NULL,
                        created_at REAL NOT NULL,
                        source TEXT NOT NULL DEFAULT '',
                        external_id TEXT NOT NULL DEFAULT '',
                        state TEXT NOT NULL DEFAULT 'ready'
                    );
                    CREATE TABLE memory_points (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        group_id TEXT NOT NULL,
                        category TEXT NOT NULL,
                        content TEXT NOT NULL,
                        weight REAL NOT NULL DEFAULT 1.0,
                        last_hit REAL NOT NULL,
                        state TEXT NOT NULL DEFAULT 'active',
                        protected INTEGER NOT NULL DEFAULT 0
                    );
                    CREATE TABLE jargon (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        group_id TEXT NOT NULL,
                        word TEXT NOT NULL,
                        alias TEXT NOT NULL
                    );
                    CREATE TABLE relations (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        group_id TEXT NOT NULL,
                        a TEXT NOT NULL,
                        b TEXT NOT NULL,
                        relation TEXT NOT NULL,
                        weight REAL NOT NULL DEFAULT 1.0,
                        valid_from REAL NOT NULL,
                        valid_to REAL
                    );
                    CREATE TABLE person_profiles (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        group_id TEXT NOT NULL,
                        user_name TEXT NOT NULL,
                        category TEXT NOT NULL,
                        content TEXT NOT NULL,
                        last_hit REAL NOT NULL
                    );
                    INSERT INTO episodes VALUES (1, 'g1', '昨晚一起联机打游戏', {t0}, 'chat', '', 'ready');
                    INSERT INTO memory_points VALUES (1, 'g1', 'event', '周末要去苏州实验室', 2.0, {t0}, 'active', 1);
                    INSERT INTO jargon VALUES (1, 'g1', 'yyds', '永远的神');
                    INSERT INTO relations VALUES (1, 'g1', '大明', '小和', '同门师兄弟', 2.0, {t0}, NULL);
                    INSERT INTO relations VALUES (2, 'g1', '大明', '路人', '已过时关系', 1.0, {t0 - 200}, {t0 - 100});
                    INSERT INTO person_profiles VALUES (1, 'g1', '大明', 'preferences', '喜欢冰镇可乐', {t0 + 10});
                    """
                )

            # 4) 旧版 monitor.sqlite3
            with sqlite3.connect(root / "monitor.sqlite3") as c:
                c.executescript(
                    f"""
                    CREATE TABLE events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        group_id TEXT NOT NULL,
                        kind TEXT NOT NULL,
                        ok INTEGER NOT NULL,
                        latency REAL NOT NULL,
                        tokens INTEGER NOT NULL,
                        detail TEXT NOT NULL,
                        ts REAL NOT NULL
                    );
                    INSERT INTO events VALUES (1, 'g1', 'reply', 1, 1.5, 80, '', {t0});
                    """
                )

            repo = Repository(root / "echo.sqlite3")
            try:
                stats = repo.stats("g1")
                self.assertEqual(stats["messages"], 2)
                self.assertEqual(stats["episodes"], 1)
                # memory_points(1) + jargon(1) + active relation(1) = 3
                self.assertEqual(stats["memories"], 3)
                self.assertEqual(stats["persons"], 1)

                # 验证 individuals 的 msg_count 与 profiles 的 traits/preferences 都保留且未被 INSERT OR IGNORE 覆盖丢失
                summary = repo.person_summary("g1", "101")
                self.assertIn("夜猫子", summary)
                self.assertIn("乌龙茶", summary)
                self.assertIn("冰镇可乐", summary)
                self.assertIn("同门师兄弟", " ".join(repo.recall_memories("g1", "小和 同门")))
                self.assertEqual(repo.group_jargon("g1"), {"yyds": "永远的神"})
                self.assertEqual(repo.monitor_stats("g1", hours=24)["total"], 1)
                self.assertEqual(repo.recent_messages("g1")[0]["msg_id"], "")
                self.assertEqual(repo.recent_messages("g1")[0]["meta"], "")
            finally:
                repo.close()

            # 二次打开必须完全幂等
            repo2 = Repository(root / "echo.sqlite3")
            try:
                again = repo2.migrate_legacy_dbs(root)
                self.assertEqual(again, {"episodes": 0, "memories": 0, "aliases": 0, "persons": 0, "events": 0})
                self.assertEqual(repo2.stats("g1")["memories"], 3)
                self.assertEqual(repo2.stats("g1")["persons"], 1)
            finally:
                repo2.close()


if __name__ == "__main__":
    unittest.main()

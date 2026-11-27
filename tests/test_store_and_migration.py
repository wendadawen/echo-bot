"""store 层测试：统一 SQLite 仓库、FTS5/双字群记忆、表情包筛选与 v1 迁移。"""

import shutil
import sqlite3
import sys
import tempfile
import threading
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

    def test_wave2_schema_version_trigram_pure_read_and_person_facts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # 1) user_version == 2 + FTS5 trigram + idempotent add_message + pure-read recall
            db_path = root / "wave2.sqlite3"
            repo = Repository(db_path)
            try:
                ver = int(repo._db.execute("PRAGMA user_version").fetchone()[0])
                self.assertEqual(ver, 2)
                if sqlite3.sqlite_version_info >= (3, 34, 0):
                    self.assertTrue(repo._fts_trigram)

                s1 = repo.add_message("g1", "10001", "阿明", "user", "你好大肥鱼", ts=100.0, msg_id="dup_mid")
                s2 = repo.add_message("g1", "10001", "阿明", "user", "你好大肥鱼", ts=100.0, msg_id="dup_mid")
                self.assertGreater(s1, 0)
                self.assertEqual(s1, s2)
                self.assertEqual(repo.stats("g1")["messages"], 1)

                repo.add_memory("g1", "event", "小和正在苏州实验室调试量子芯片", now=1000.0)
                repo.add_memory("g1", "relation", "大明和小和是同门师兄弟", now=1000.0)
                if repo._fts_trigram:
                    self.assertTrue(repo._fts_hits("g1", "苏州实验室", 5))
                self.assertTrue(any("苏州" in h for h in repo.recall_memories("g1", "苏州", limit=5, now=1010.0)))
                self.assertTrue(any("同门师兄弟" in h for h in repo.recall_memories("g1", "小和 同门", limit=5, now=1010.0)))

                before = repo._db.execute(
                    "SELECT weight, last_hit FROM memories WHERE group_id='g1' AND content LIKE '%苏州实验室%'"
                ).fetchone()
                for t_read in (2000.0, 2100.0, 2200.0):
                    hits = repo.recall_memories("g1", "苏州实验室", limit=5, now=t_read)
                    self.assertTrue(any("苏州实验室" in h for h in hits))
                after = repo._db.execute(
                    "SELECT weight, last_hit FROM memories WHERE group_id='g1' AND content LIKE '%苏州实验室%'"
                ).fetchone()
                self.assertEqual(tuple(before), tuple(after))

                # 2) WAL 文件库并发读不阻塞写
                errs = []
                def _writer():
                    try:
                        for i in range(25):
                            repo.add_message("g_conc", "200", "甲", "user", f"w{i}", ts=3000.0 + i, msg_id=f"w{i}")
                    except Exception as exc:
                        errs.append(exc)
                def _reader():
                    try:
                        for _ in range(25):
                            repo.recent_messages("g_conc", limit=5)
                            repo.recall_memories("g1", "苏州实验室", limit=3, now=3100.0)
                    except Exception as exc:
                        errs.append(exc)
                threads = [threading.Thread(target=_writer), threading.Thread(target=_reader), threading.Thread(target=_reader)]
                for th in threads:
                    th.start()
                for th in threads:
                    th.join(timeout=5.0)
                self.assertEqual(errs, [])

                # 3) 结构化 person_facts + 中英子串/词边界匹配
                repo.observe_person("g1", "10001", "阿明", now=100.0)
                repo.observe_person("g1", "10002", "al", now=100.0)
                repo.upsert_person("g1", "10001", now=200.0, traits="早起；极客；写Python")
                repo.upsert_person("g1", "10001", now=210.0, traits="早起喜欢看书")
                facts = repo.person_facts("g1", "10001", field="traits")
                fact_texts = [f["fact"] for f in facts]
                self.assertIn("早起喜欢看书", fact_texts)
                matched = [f for f in facts if f["fact"] == "早起喜欢看书"][0]
                self.assertEqual(matched["hits"], 2)
                self.assertIn("早起喜欢看书", repo.person_summary("g1", "10001"))

                self.assertEqual(repo.find_mentioned_persons("g1", "阿明今天去实验室了吗"), [("10001", "阿明")])
                self.assertEqual(repo.find_mentioned_persons("g1", "already done"), [])
                self.assertEqual(repo.find_mentioned_persons("g1", "hey al look"), [("10002", "al")])
            finally:
                repo.close()

            # 4) 二次打开不会重建已存在的 v2 FTS，且 legacy v1 含重复 msg_id 与旧 persons 可无损升到 v2
            v1_db = root / "v1_upgrade.sqlite3"
            with sqlite3.connect(v1_db) as c:
                c.executescript(
                    """
                    PRAGMA user_version = 1;
                    CREATE TABLE schema_meta (k TEXT PRIMARY KEY, v TEXT NOT NULL DEFAULT '');
                    CREATE TABLE messages (
                        seq INTEGER PRIMARY KEY AUTOINCREMENT,
                        group_id TEXT NOT NULL, user_id TEXT NOT NULL, user_name TEXT NOT NULL,
                        role TEXT NOT NULL, text TEXT NOT NULL, ts REAL NOT NULL,
                        msg_id TEXT NOT NULL DEFAULT '', meta TEXT NOT NULL DEFAULT ''
                    );
                    INSERT INTO messages VALUES (1, 'g1', '10', '甲', 'user', '第一条', 100.0, 'dup', '');
                    INSERT INTO messages VALUES (2, 'g1', '10', '甲', 'user', '重复条', 101.0, 'dup', '');
                    CREATE TABLE episodes (id INTEGER PRIMARY KEY AUTOINCREMENT, group_id TEXT NOT NULL, text TEXT NOT NULL, created_at REAL NOT NULL, source TEXT NOT NULL DEFAULT '', external_id TEXT NOT NULL DEFAULT '', state TEXT NOT NULL DEFAULT 'ready');
                    CREATE TABLE memories (id INTEGER PRIMARY KEY AUTOINCREMENT, group_id TEXT NOT NULL, category TEXT NOT NULL, content TEXT NOT NULL, weight REAL NOT NULL DEFAULT 1.0, last_hit REAL NOT NULL, state TEXT NOT NULL DEFAULT 'active', protected INTEGER NOT NULL DEFAULT 0);
                    INSERT INTO memories VALUES (1, 'g1', 'event', '周末一起去苏州实验室', 1.0, 100.0, 'active', 0);
                    CREATE TABLE person_aliases (group_id TEXT NOT NULL, user_id TEXT NOT NULL, name TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'nick', hits INTEGER NOT NULL DEFAULT 1, updated_at REAL NOT NULL, PRIMARY KEY (group_id, user_id, name));
                    CREATE TABLE persons (group_id TEXT NOT NULL, user_id TEXT NOT NULL, traits TEXT NOT NULL DEFAULT '', preferences TEXT NOT NULL DEFAULT '', events TEXT NOT NULL DEFAULT '', relation TEXT NOT NULL DEFAULT '', intimacy REAL NOT NULL DEFAULT 0.5, msg_count INTEGER NOT NULL DEFAULT 0, first_seen REAL NOT NULL DEFAULT 0, last_seen REAL NOT NULL DEFAULT 0, updated_at REAL NOT NULL DEFAULT 0, PRIMARY KEY (group_id, user_id));
                    INSERT INTO persons(group_id, user_id, traits, updated_at) VALUES ('g1', '10', '熬夜；话痨', 200.0);
                    CREATE TABLE conv_state (group_id TEXT PRIMARY KEY, topics_json TEXT NOT NULL DEFAULT '[]', progress_json TEXT NOT NULL DEFAULT '[]', pending_json TEXT NOT NULL DEFAULT '[]', updated_at REAL NOT NULL);
                    CREATE TABLE monitor_events (id INTEGER PRIMARY KEY AUTOINCREMENT, group_id TEXT NOT NULL DEFAULT '', kind TEXT NOT NULL, ok INTEGER NOT NULL DEFAULT 1, latency REAL NOT NULL DEFAULT 0, tokens INTEGER NOT NULL DEFAULT 0, detail TEXT NOT NULL DEFAULT '', ts REAL NOT NULL);
                    CREATE TABLE pending_deliveries (task_id TEXT PRIMARY KEY, group_id TEXT NOT NULL, umo TEXT NOT NULL DEFAULT '', prompt TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'failed', reason TEXT NOT NULL DEFAULT 'error', result TEXT NOT NULL DEFAULT '', partial TEXT NOT NULL DEFAULT '', files_json TEXT NOT NULL DEFAULT '[]', error TEXT NOT NULL DEFAULT '', pre_history_json TEXT NOT NULL DEFAULT '[]', created_at REAL NOT NULL);
                    """
                )
            migrated = Repository(v1_db)
            try:
                self.assertEqual(int(migrated._db.execute("PRAGMA user_version").fetchone()[0]), 2)
                self.assertEqual(migrated.stats("g1")["messages"], 1)
                self.assertEqual([f["fact"] for f in migrated.person_facts("g1", "10", field="traits")], ["熬夜", "话痨"])
                self.assertTrue(migrated.recall_memories("g1", "苏州实验室", limit=3, now=150.0))
            finally:
                migrated.close()


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

            # 真表情被收录至 shared/ 并同步进入共享池，新群 g2 立即可查可用且含描述
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

            # 经典 QQ 小黄脸仅本群可用，不跨群共享；g1 同关键词命中，remove_face 可踢除失效 face_id
            self.assertEqual(store.pick("g1", "滑稽"), "face:178")
            self.assertNotEqual(store.pick("g2", "滑稽"), "face:178")
            self.assertIsInstance(store.pick("g1", "未知心情"), Path)
            self.assertTrue(store.remove_face("g1", "178"))
            with store._read() as conn:
                self.assertIsNone(
                    conn.execute("SELECT 1 FROM emoji_faces WHERE group_id='g1' AND face_id='178'").fetchone()
                )
            self.assertNotEqual(store.pick("g1", "滑稽"), "face:178")

            # 引用计数安全删除：从 g1 删除群关联后 emoji_items 仍在，绝不误删 shared/ 下底层图片
            b1_path = Path(picked_g2.path)
            rel_b1 = b1_path.relative_to(Path(tmp).resolve(strict=False)).as_posix()
            with store._write() as conn:
                conn.execute("DELETE FROM emoji_group_links WHERE group_id='g1'")
            store._unlink_unreferenced([rel_b1])
            self.assertTrue(b1_path.is_file())

            # 超期文件清理（全池过期后彻底清空并删除共享文件）
            store.cleanup(max_age_days=30, now=time.time() + 40 * 86400)
            self.assertFalse(store.has_entries("g1"))
            self.assertFalse(store.has_entries("g2"))
            self.assertFalse(b1_path.exists())
            store.close()

            # 旧版 index.json 无损迁移 + 自动清洗坏 face_id / 截屏图并迁移老路径进 shared/，重命名 *.migrated
            legacy_dir = Path(tmp) / "legacy_mig"
            (legacy_dir / "g1").mkdir(parents=True)
            legacy_good = legacy_dir / "g1" / "legacy_ok.gif"
            legacy_good.write_bytes(TINY_GIF + b"legacy_meme")
            dirty_file = legacy_dir / "g1" / "dirty.png"
            dirty_file.write_bytes(TINY_PNG)
            long_shot = legacy_dir / "g1" / "news_shot.png"
            long_shot.write_bytes(_png_sized(1179, 2556))
            raw_idx = {
                "g1": [
                    {"face_id": "489", "tag": "QQ表情", "description": "QQ表情", "ts": time.time()},
                    {"path": str(dirty_file), "sha256": "deadbeef", "tag": "这是一张卡通猫的", "description": "猫", "ts": time.time()},
                    {"path": str(long_shot), "sha256": "news1179", "tag": "震惊", "description": "新闻报道正接受审查", "ts": time.time()},
                    {"path": str(legacy_good), "sha256": "", "tag": "呆萌", "description": "老群收藏小猫", "ts": time.time()},
                ]
            }
            (legacy_dir / "index.json").write_text(_json.dumps(raw_idx, ensure_ascii=False), encoding="utf-8")
            reloaded = EmojiStore(legacy_dir, limit=10, max_age_days=30)
            try:
                self.assertFalse(dirty_file.exists())
                self.assertFalse(long_shot.exists())
                self.assertFalse(legacy_good.exists())
                self.assertTrue((legacy_dir / "index.json.migrated").is_file())
                self.assertIn("呆萌", reloaded.tags("g2"))
                with reloaded._read() as conn:
                    self.assertIsNone(
                        conn.execute("SELECT 1 FROM emoji_faces WHERE group_id='g1' AND face_id='489'").fetchone()
                    )
            finally:
                reloaded.close()

    def test_wave2_sqlite_shared_relocatable_singleflight_and_weighted_pick(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # 1) 共享 Repository 模式：无 index.json / emoji_meta.sqlite3，且存储相对 shared/ 路径
            repo = Repository(root / "echo.sqlite3")
            shared_dir = root / "emojis"
            store = EmojiStore(shared_dir, limit=10, max_age_days=30, repo=repo)
            llm = MagicMock()
            llm.describe_image.return_value = '{"kind":"sticker","usable":true,"tag":"偷笑","desc":"小猫偷笑"}'
            b1 = _png_sized(120, 120) + b"a"
            self.assertEqual(store.collect("g1", b1, llm), "偷笑")
            self.assertFalse((shared_dir / "index.json").exists())
            self.assertFalse((shared_dir / "emoji_meta.sqlite3").exists())
            with repo.read_conn() as conn:
                rel_row = conn.execute("SELECT rel_path FROM emoji_items").fetchone()
            self.assertIsNotNone(rel_row)
            self.assertTrue(str(rel_row["rel_path"]).startswith("shared/"))
            store.close()
            repo.close()

            # 2) 目录整体迁移（relocation）：拷贝独立目录到新路径仍可用且解析在新目录内
            orig_dir = root / "emojis_orig"
            moved_dir = root / "emojis_moved"
            s_orig = EmojiStore(orig_dir, limit=10)
            llm2 = MagicMock()
            llm2.describe_image.return_value = "点赞|熊猫头点赞"
            b_move = _png_sized(140, 140) + b"move"
            self.assertEqual(s_orig.collect("g1", b_move, llm2), "点赞")
            s_orig.close()
            shutil.copytree(orig_dir, moved_dir)
            s_moved = EmojiStore(moved_dir, limit=10)
            try:
                picked = s_moved.pick_entry("g1", "点赞")
                self.assertIsNotNone(picked)
                self.assertTrue(Path(picked.path).is_file())
                self.assertIn(str(moved_dir.resolve(strict=False)), str(Path(picked.path).resolve(strict=False)))
            finally:
                s_moved.close()

            # 3) Singleflight 并发收录同一张图：VLM 仅调 1 次，且双群同时获得标签并入库
            sf_dir = root / "emojis_sf"
            s_sf = EmojiStore(sf_dir, limit=10)
            call_count = 0
            barrier = threading.Barrier(2)

            class _SlowLLM:
                def describe_image(self, image_bytes, prompt, timeout=None):
                    nonlocal call_count
                    call_count += 1
                    time.sleep(0.15)
                    return "摸鱼|小猫摸鱼"

            b_sf = _png_sized(160, 160) + b"sf"
            res_map = {}

            def _worker(gid):
                barrier.wait(timeout=3.0)
                res_map[gid] = s_sf.collect(gid, b_sf, _SlowLLM())

            t1 = threading.Thread(target=_worker, args=("g1",))
            t2 = threading.Thread(target=_worker, args=("g2",))
            t1.start()
            t2.start()
            t1.join(timeout=5.0)
            t2.join(timeout=5.0)
            self.assertEqual(call_count, 1)
            self.assertEqual(res_map, {"g1": "摸鱼", "g2": "摸鱼"})
            self.assertEqual(s_sf.stats("g1")["g1"], 1)
            self.assertEqual(s_sf.stats("g2")["g2"], 1)
            s_sf.close()

            # 4) 加权挑选与使用计数记录：3 张图排除连发锁定，低频/非刚用图权重显著高于高频图并更新 use_count / last_used_at
            w_dir = root / "emojis_weighted"
            s_w = EmojiStore(w_dir, limit=10)
            llm_w = MagicMock()
            llm_w.describe_image.side_effect = ["开心|开心猫1", "开心|开心猫2", "开心|开心猫3"]
            b_heavy = _png_sized(150, 150) + b"heavy"
            b_fresh = _png_sized(150, 150) + b"fresh"
            b_fresh2 = _png_sized(150, 150) + b"fresh2"
            self.assertEqual(s_w.collect("g1", b_heavy, llm_w), "开心")
            self.assertEqual(s_w.collect("g1", b_fresh, llm_w), "开心")
            self.assertEqual(s_w.collect("g1", b_fresh2, llm_w), "开心")
            import hashlib as _hashlib
            h_heavy = _hashlib.sha256(b_heavy).hexdigest()
            h_fresh = _hashlib.sha256(b_fresh).hexdigest()
            with s_w._write() as conn:
                conn.execute(
                    "UPDATE emoji_items SET use_count = 80, last_used_at = ? WHERE sha256 = ?",
                    (time.time(), h_heavy),
                )
            picked_paths = [Path(s_w.pick_entry("g1", "开心").path).name for _ in range(30)]
            heavy_hits = sum(1 for p in picked_paths if h_heavy[:16] in p)
            fresh_hits = 30 - heavy_hits
            self.assertGreater(fresh_hits, heavy_hits * 2)
            with s_w._read() as conn:
                row_f = conn.execute("SELECT use_count, last_used_at FROM emoji_items WHERE sha256 = ?", (h_fresh,)).fetchone()
            self.assertGreater(int(row_f["use_count"]), 0)
            self.assertGreater(float(row_f["last_used_at"]), 0.0)
            s_w.close()


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

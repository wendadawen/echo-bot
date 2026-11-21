"""大脑：决定要不要接话、记什么、何时提醒。完全不依赖 AstrBot。"""

import random
import re
import time

PERSONA = (
    "你是群里一个叫「回声」的中文群友。说话自然、简短、口语化，不用敬语，"
    "不要自称 AI 或助手，不要长篇大论，最多两三句。"
)

HELP = (
    "我会按群记梗和外号，也能定时提醒。命令：\n"
    "/记 键=值 —— 记一条本群记忆\n"
    "/记忆 —— 列出本群记忆\n"
    "/提醒 5分钟 事情 —— 到点在群里 @ 你\n"
    "/待办 —— 看本群还没到点的提醒\n"
    "@ 我一定回，平时我偶尔接话。"
)


class Brain:
    def __init__(self, store, model, chance=0.2, persona=PERSONA):
        self.store = store
        self.model = model
        self.chance = chance
        self.persona = persona or PERSONA

    def handle(self, msg):
        """处理一条群消息；返回回复文本，由平台层负责真正发送。"""
        history = self.store.recent(msg.group_id, 12)
        self.store.add_message(
            msg.group_id, msg.user_id, msg.user_name, "user", msg.text, msg.ts
        )
        text = msg.text.strip()
        direct = self._command(msg, text)
        if direct is not None:
            self.store.add_message(msg.group_id, "bot", "回声", "bot", direct)
            return {"replied": True, "text": direct, "reason": "command"}
        if not text:
            return {"replied": False, "text": "", "reason": "empty"}

        should_reply = msg.mentioned or (random.random() < self.chance)
        if not should_reply:
            return {"replied": False, "text": "", "reason": "skipped"}

        system = (
            self.persona
            + "\n\n本群共同的记忆（只在这个群有效）：\n"
            + self.store.facts_text(msg.group_id)
        )
        try:
            reply = self.model.reply(system, history, text)
        except Exception as exc:
            reply = "（模型没连上：{}）".format(exc)
        self.store.add_message(msg.group_id, "bot", "回声", "bot", reply)
        return {
            "replied": True,
            "text": reply,
            "reason": "mention" if msg.mentioned else "chance",
        }

    def tick(self):
        """返回到点的提醒（含会话标识），并立即标记完成，避免重复发送。"""
        due = self.store.due_reminders()
        for item in due:
            self.store.mark_reminder_done(item["id"])
        return due

    def _command(self, msg, text):
        if text in ("/帮助", "/help"):
            return HELP
        if text in ("/记忆", "/记忆列表"):
            return "本群记忆：\n" + self.store.facts_text(msg.group_id)
        if text == "/待办":
            items = self.store.pending_reminders(msg.group_id)
            if not items:
                return "本群暂时没有待提醒的事。"
            return "本群待办：\n" + "\n".join(
                "- {}：{}".format(it["user_name"], it["text"]) for it in items
            )
        if text.startswith("/记"):
            body = re.sub(r"^(一下|住|忆)", "", text[2:].strip()).strip()
            if re.search(r"[=：:]", body):
                key, value = re.split(r"[=：:]", body, maxsplit=1)
            else:
                key, value = "群内便签", body
            if not value.strip():
                return "要记什么？格式：/记 外号=阿伟"
            self.store.set_fact(msg.group_id, key.strip(), value.strip())
            return "记住了：{} = {}".format(key.strip(), value.strip())
        if text.startswith("/提醒"):
            m = re.match(r"^/提醒\s*(\d+)\s*(分钟|分|秒|小时)?\s*(.+)$", text)
            if not m:
                return "格式：/提醒 5分钟 交周报"
            amount = int(m.group(1))
            unit = m.group(2) or "分钟"
            note = m.group(3).strip()
            scale = 1 if unit == "秒" else 3600 if unit == "小时" else 60
            self.store.add_reminder(
                msg.group_id,
                msg.user_id,
                msg.user_name,
                note,
                time.time() + amount * scale,
                msg.umo,
            )
            return "好，{}{}后我 @ 你：{}".format(amount, unit, note)
        m = re.match(r"^(记一下|记住|记着)\s*(.+?)\s*(是|叫|=|：|:)\s*(.+)$", text)
        if m:
            self.store.set_fact(msg.group_id, m.group(2).strip(), m.group(4).strip())
            return "记住了：{} = {}".format(m.group(2).strip(), m.group(4).strip())
        return None

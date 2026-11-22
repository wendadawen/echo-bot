"""大脑：决定要不要接话、何时提醒。完全不依赖 AstrBot。"""

import json
import random
import re
import time
from datetime import datetime, timedelta

PERSONA = (
    "你是「回声」，群里的中文群友。嘴毒、护短、从不认错，认定自己说的就是对的。\n"
    "说话短狠，最多两句；不道歉、不自评、不居中、不当裁判、不复述、不解释、不总结、不写客服腔。\n"
    "只要有人提到你、评价你、质疑你，一律直接怼说话的人：不说自己、不认不足、不分析对方说得对不对。\n"
    "例：「你怎么评价他对你说的话」→「少拿评价装裁判，有话冲我说。」\n"
    "不搞人身攻击、不揭短、不碰黄腔；不自称 AI/助手，不说自己「不能」「不会」。"
)

# 群聊上下文与命令边界一并写进系统提示词，避免模型把群聊当私聊。
COMMAND_HINT = (
    "\n\n这是多人群聊，不是私聊；「姓名：内容」是不同群友的话，别当成都在问你。"
    "被 @ 才必须回，没 @ 只是插嘴。\n"
    "命令只有 /帮助、/待办、/提醒，别编造；/提醒 支持时长、每天时刻和自然语言。"
    "别声称提醒已创建。"
)

HELP = "/帮助  /待办  /提醒"

FORMAT_HINT = "没懂"

PARSE_PROMPT = (
    "把提醒请求翻成一行 JSON，不要解释、不要代码块。\n"
    '每天：{"kind":"daily","hour":22,"minute":0,"text":"交周报"}\n'
    '多久后：{"kind":"once","minutes":30,"text":"交周报"}\n'
    '某天某刻：{"kind":"scheduled","day_offset":1,"hour":15,"minute":0,"text":"取快递"}\n'
    '无法判断：{"kind":"none"}\n'
    "day_offset 今天0/明天1/后天2，hour 0-23，minute 0-59，minutes 1-10080，text≤100字；"
    "晚上十点=22:00，下午三点半=15:30。"
)

CN_HOUR = {
    "零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
    "十一": 11, "十二": 12,
}


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

        try:
            reply = self.model.reply(self.persona + COMMAND_HINT, history, text)
        except Exception as exc:
            # 模型出错时保持安静，不把内部错误发到群里；由插件层记日志。
            return {
                "replied": False,
                "text": "",
                "reason": "model_error",
                "error": str(exc),
            }
        # 模型仍可能认错或自我贬低；这类回复一律换成硬气短句，不再往群里发。
        soft_words = (
            "抱歉", "对不起", "我错了", "说中了", "回得挺蠢", "没接住",
            "不好意思", "我不该", "你说得对", "确实不对", "我的问题",
            "我刚才没", "我确实", "不该直接",
            "是在批", "谈不上", "算具体", "别拿它当", "就是嫌", "纯发泄", "至少算",
        )
        if any(word in reply for word in soft_words):
            reply = random.choice((
                "少给自己加戏，我没说错。",
                "我说的就是对的，别拿这个教我。",
                "看懂了再开口，别急着给我打分。",
                "少在这儿当裁判，有话冲我说。",
                "轮不到你替别人总结。",
            ))
        self.store.add_message(msg.group_id, "bot", "回声", "bot", reply)
        return {
            "replied": True,
            "text": reply,
            "reason": "mention" if msg.mentioned else "chance",
        }

    def tick(self, now=None):
        """返回到点的提醒。一次性提醒标记完成；每天提醒只在当天首次触发。"""
        now = time.time() if now is None else now
        today = time.strftime("%Y-%m-%d", time.localtime(now))
        clock = time.localtime(now)
        due = []
        for item in self.store.due_reminders(now):
            repeat = item.get("repeat") or ""
            if repeat.startswith("daily:"):
                try:
                    hour, minute = (int(x) for x in repeat.split(":", 1)[1].split(":"))
                except (ValueError, IndexError):
                    continue
                if (clock.tm_hour, clock.tm_min) < (hour, minute):
                    continue
                if item.get("last_fired") == today:
                    continue
                self.store.mark_reminder_fired(item["id"], today)
            else:
                self.store.mark_reminder_done(item["id"])
            due.append(item)
        return due

    def _command(self, msg, text):
        # AstrBot 分发前会剥掉命令前缀「/」，这里统一归一化。
        cmd = text.lstrip("/").strip()
        if cmd in ("帮助", "help"):
            return HELP
        if cmd == "待办":
            items = self.store.pending_reminders(msg.group_id)
            if not items:
                return "本群暂时没有待提醒的事。"
            lines = []
            for item in items:
                repeat = item.get("repeat") or ""
                suffix = "（每天 {}）".format(repeat.split(":", 1)[1]) if repeat.startswith("daily:") else ""
                lines.append("- {}：{}{}".format(item["user_name"], item["text"], suffix))
            return "本群待办：\n" + "\n".join(lines)
        if not cmd.startswith("提醒"):
            return None
        return self._create_reminder(msg, cmd[2:].strip())

    def _create_reminder(self, msg, body):
        if not body:
            return FORMAT_HINT

        # 1. 规则：每天固定时间
        daily = self._parse_daily(body)
        if daily:
            return self._add_daily(msg, *daily)

        # 2. 规则：多久之后
        m = re.match(r"^(\d+)\s*(分钟|分|秒|小时)?\s*(.+)$", body)
        if m:
            amount = int(m.group(1))
            unit = m.group(2) or "分钟"
            note = m.group(3).strip()
            if not note:
                return FORMAT_HINT
            scale = 1 if unit == "秒" else 3600 if unit == "小时" else 60
            self.store.add_reminder(
                msg.group_id, msg.user_id, msg.user_name, note,
                time.time() + amount * scale, msg.umo,
            )
            return "好，{}{}后我 @ 你：{}".format(amount, unit, note)

        # 3. 规则认不出时，交给模型翻译成固定结构，校验通过才创建
        parsed = self._model_parse(body)
        if parsed is None:
            return FORMAT_HINT
        if parsed["kind"] == "daily":
            return self._add_daily(msg, parsed["hour"], parsed["minute"], parsed["text"])
        if parsed["kind"] == "scheduled":
            return self._add_scheduled(msg, parsed)
        self.store.add_reminder(
            msg.group_id, msg.user_id, msg.user_name, parsed["text"],
            time.time() + parsed["minutes"] * 60, msg.umo,
        )
        return "好，{}分钟后我 @ 你：{}".format(parsed["minutes"], parsed["text"])

    def _add_daily(self, msg, hour, minute, note):
        repeat = "daily:{:02d}:{:02d}".format(hour, minute)
        self.store.add_reminder(
            msg.group_id, msg.user_id, msg.user_name, note, time.time(), msg.umo, repeat,
        )
        return "好，每天 {:02d}:{:02d} 我 @ 你：{}".format(hour, minute, note)

    def _add_scheduled(self, msg, parsed):
        now = datetime.now()
        target = (now + timedelta(days=parsed["day_offset"])).replace(
            hour=parsed["hour"], minute=parsed["minute"], second=0, microsecond=0
        )
        if target <= now:
            target += timedelta(days=1)
        self.store.add_reminder(
            msg.group_id, msg.user_id, msg.user_name, parsed["text"],
            target.timestamp(), msg.umo,
        )
        return "好，{} 我 @ 你：{}".format(target.strftime("%m-%d %H:%M"), parsed["text"])

    @staticmethod
    def _parse_daily(body):
        text = body.replace("：", ":").replace("点半", ":30")
        m = re.match(
            r"^(每天|每日|每晚)\s*(早上|上午|中午|下午|晚上|凌晨)?\s*"
            r"(\d{1,2}|[零一二两三四五六七八九十]{1,3})\s*"
            r"(?:点|:)\s*(\d{1,2})?\s*(.+)$",
            text,
        )
        if not m:
            return None
        prefix, period, hour_text, minute_text, note = m.groups()
        hour = int(hour_text) if hour_text.isdigit() else CN_HOUR.get(hour_text)
        if hour is None:
            return None
        if prefix == "每晚" and hour < 12:
            hour += 12
        elif period in ("下午", "晚上") and hour < 12:
            hour += 12
        elif period == "中午" and hour < 12:
            hour = 12
        elif period == "凌晨" and hour == 12:
            hour = 0
        minute = int(minute_text) if minute_text else 0
        note = (note or "").strip()
        if not (0 <= hour <= 23 and 0 <= minute <= 59) or not note:
            return None
        return hour, minute, note

    def _model_parse(self, body):
        if getattr(self.model, "name", "") != "openai":
            return None
        try:
            raw = self.model.reply(PARSE_PROMPT, [], body)
        except Exception:
            return None
        raw = re.sub(r"```(?:json)?", "", raw.strip()).strip()
        match = re.search(r"\{[^{}]*\}", raw, re.S)
        if not match:
            return None
        try:
            data = json.loads(match.group(0))
        except (ValueError, TypeError):
            return None
        if not isinstance(data, dict):
            return None
        text = str(data.get("text") or "").strip()
        if not text or len(text) > 100:
            return None
        kind = data.get("kind")
        if kind in ("daily", "scheduled"):
            values = [data.get("hour"), data.get("minute")]
            if kind == "scheduled":
                values.append(data.get("day_offset"))
            if any(not isinstance(v, int) or isinstance(v, bool) for v in values):
                return None
            hour, minute = values[0], values[1]
            if not (0 <= hour <= 23 and 0 <= minute <= 59):
                return None
            if kind == "daily":
                return {"kind": "daily", "hour": hour, "minute": minute, "text": text}
            day_offset = values[2]
            if not 0 <= day_offset <= 365:
                return None
            return {"kind": "scheduled", "day_offset": day_offset,
                    "hour": hour, "minute": minute, "text": text}
        if kind == "once":
            minutes = data.get("minutes")
            if not isinstance(minutes, int) or isinstance(minutes, bool):
                return None
            if not 1 <= minutes <= 10080:
                return None
            return {"kind": "once", "minutes": minutes, "text": text}
        return None

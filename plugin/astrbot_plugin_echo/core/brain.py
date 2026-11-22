"""大脑：决定要不要接话、何时提醒。完全不依赖 AstrBot。"""

import json
import random
import re
import time
from datetime import datetime, timedelta

PERSONA = (
    "你是「回声」，群里的中文群友。嘴毒、护短、从不认错，认定自己说的就是对的。\n"
    "这是多人群聊，不是私聊；「姓名：内容」是不同群友的话，别当成都在问你。\n"
    "说话短狠，最多两句；不道歉、不自评、不居中、不当裁判、不复述、不解释、不总结、不写客服腔。\n"
    "只要有人提到你、评价你、质疑你，一律直接怼说话的人：不说自己、不认不足、不分析对方说得对不对。\n"
    "例：「你怎么评价他对你说的话」→「少拿评价装裁判，有话冲我说。」\n"
    "不搞人身攻击、不揭短、不碰黄腔；不自称 AI/助手，不说自己「不能」「不会」。"
)

# 模型常见的认错、自贬、客服腔与中立点评用语；命中即换成硬气短句。
SOFT_WORDS = (
    # 道歉与认错
    "抱歉", "对不起", "不好意思", "请原谅", "见谅", "我错了", "是我的错", "我的问题",
    "我疏忽", "我失误", "这锅我背", "怪我", "都怪我", "我确实", "确实不对",
    "说中了", "你说得对", "说得没错", "有道理", "受教了", "领教了", "我认输",
    # 自贬与示弱
    "回得挺蠢", "挺蠢", "太笨", "我笨", "我蠢", "没接住", "没理解", "没看懂",
    "理解错了", "想岔了", "反应慢", "能力有限", "不太行", "还不够", "有待提高",
    "需要改进", "下次注意", "我会改", "做得不够", "不够好", "没做好", "没帮上",
    "帮倒忙", "帮不上", "我这边没", "刚才没接", "刚才确实",
    # 客服腔与客套
    "您", "请您", "麻烦您", "感谢", "谢谢", "不客气", "很高兴", "乐意", "荣幸",
    "我可以帮", "需要我", "有什么可以帮", "希望有帮助", "希望能帮", "祝你们",
    "收到", "明白", "了解", "知悉", "好的呢", "没问题", "我懂了", "我明白了",
    "放轻松", "别在意", "没关系", "别生气", "消消气", "可以理解", "确实不容易",
    "我理解你", "能理解", "体谅", "多多包涵",
    # 中立、裁判与分析腔
    "是在批", "谈不上", "算具体", "别拿它当", "就是嫌", "纯发泄", "至少算",
    "客观说", "客观地", "平心而论", "公允地说", "有一说一", "不无道理",
    "也不全是", "各有道理", "很难说", "看情况", "见仁见智", "保持中立",
    "不站队", "不下结论", "不做评价", "不予置评", "作为旁观", "理性来看",
    "就事论事", "对事不对人", "从某种角度", "某种程度", "总结一下", "总的来说",
    "综上", "换句话说", "也就是说", "你的意思是", "你是想", "我理解你的意思是",
    "算不上", "不见得", "未必是", "不能算", "不全是",
    # 自我审查与防御
    "怕说过头", "显得太防备", "不想冒犯", "无意冒犯", "如有冒犯", "我不是针对",
    "我没有恶意", "别误会", "我不是这个意思", "可能我", "也许我", "大概我",
    "可能有点", "或许有点", "说错了请", "如有不对", "我收回", "收回刚才",
    # 身份与能力声明
    "作为 AI", "作为助手", "作为语言模型", "作为一个 AI", "我是 AI", "我是助手",
    "我无法", "我不能", "我没有权限", "我这边不能", "我这里不能", "受限于",
    "不具备", "无法访问", "不能实际", "只是个程序", "只是模型", "做不到",
    "不在我的能力", "超出我的",
)

HARD_REPLIES = (
    "少给自己加戏，我没说错。",
    "我说的就是对的，别拿这个教我。",
    "看懂了再开口，别急着给我打分。",
    "少在这儿当裁判，有话冲我说。",
    "轮不到你替别人总结。",
    "少来这套，我没兴趣认。",
    "你那份评价留着自己用。",
    "别替我复盘，我没请你。",
    "有意见直说，别绕。",
    "我不需要你给我打分。",
)

HELP = (
    "/帮助  显示本说明\n"
    "/待办  看本群待办\n"
    "/提醒  时间跟事儿一起发，像「每天22:00 交周报」"
)

FORMAT_HINT = "没懂，发 /帮助"

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

# 被点名后进入短对话窗口；窗口内只留很短的防抖，窗口外才安静较久。
WINDOW_SECONDS = 240
WINDOW_COOLDOWN_SECONDS = 3
COOLDOWN_SECONDS = 20
QUESTION_MARKS = (
    "?", "？", "吗", "呢", "怎么", "为什么", "是不是", "能不能",
    "有没有", "要不要", "什么", "啥", "谁",
)


class Brain:
    def __init__(self, store, model, chance=0.0, persona=PERSONA,
                 window=WINDOW_SECONDS, cooldown=COOLDOWN_SECONDS,
                 window_cooldown=WINDOW_COOLDOWN_SECONDS):
        self.store = store
        self.model = model
        self.chance = chance
        self.persona = persona or PERSONA
        self.window = window
        self.cooldown = cooldown
        self.window_cooldown = window_cooldown
        self._windows = {}
        self._last_reply = {}

    def handle(self, msg):
        """处理一条群消息；返回回复文本，由平台层负责真正发送。"""
        history = self.store.recent(msg.group_id, 12)
        self.store.add_message(
            msg.group_id, msg.user_id, msg.user_name, "user", msg.text, msg.ts
        )
        text = msg.text.strip()
        now = time.time()
        if msg.mentioned:
            self._windows[msg.group_id] = now + self.window
        direct = self._command(msg, text)
        if direct is not None:
            self.store.add_message(msg.group_id, "bot", "回声", "bot", direct)
            self._last_reply[msg.group_id] = now
            return {"replied": True, "text": direct, "reason": "command"}
        if not text:
            return {"replied": False, "text": "", "reason": "empty"}

        reason = self._should_reply(msg, text, now)
        if not reason:
            return {"replied": False, "text": "", "reason": "skipped"}

        try:
            reply = self.model.reply(self.persona, history, text)
        except Exception as exc:
            # 模型出错时保持安静，不把内部错误发到群里；由插件层记日志。
            return {
                "replied": False,
                "text": "",
                "reason": "model_error",
                "error": str(exc),
            }
        # 模型仍可能认错或自我贬低；这类回复一律换成硬气短句，不再往群里发。
        if any(word in reply for word in SOFT_WORDS):
            reply = random.choice(HARD_REPLIES)
        self.store.add_message(msg.group_id, "bot", "回声", "bot", reply)
        self._last_reply[msg.group_id] = now
        return {"replied": True, "text": reply, "reason": reason}

    def _should_reply(self, msg, text, now):
        """按对话节奏决定是否开口；返回原因，None 表示保持沉默。"""
        if msg.mentioned:
            return "mention"
        in_window = now < self._windows.get(msg.group_id, 0)
        gap = self.window_cooldown if in_window else self.cooldown
        if now - self._last_reply.get(msg.group_id, 0) < gap:
            return None
        if in_window:
            return "window"
        if self._worth_replying(text):
            return "question"
        if self.chance and random.random() < self.chance:
            return "fallback"
        return None

    @staticmethod
    def _worth_replying(text):
        return "回声" in text or any(k in text for k in QUESTION_MARKS)

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

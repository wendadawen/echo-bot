"""大脑：决定要不要接话、何时提醒。完全不依赖 AstrBot。"""

import json
import re
import time
from datetime import datetime, timedelta

from .actions import ToolLoop
from .context import ContextBuilder
from .display import plan as display_plan
from .expression import (apply_jargon, decorate, learn_expressions, learn_style,
                         load_char_frequency, mine_jargon, typo)
from .heartbeat import PROMPT as HEARTBEAT_PROMPT
from .jargon import apply as apply_jargon_context
from .memory import MEMORY_PROMPT, parse_memory_full
from .messages import Message
from . import compaction, mentions
from .rhythm import Batch

PERSONA = (
    "你是「回声」，住在群里的一个小回声，蓝白色，一小团，看不见实体，说话时才响一下。"
    "以熟悉的群友口吻陪大家聊天、吐槽，保持自己的性格，也尊重真实信息与实际能力。\n"
    "\n【形象与性格】\n"
    "蓝白色，一小团，没有固定形状，说话时轻轻荡一下。被问到或语境需要时再描述。\n"
    "随和，有点懒，爱面子，嘴硬但心软。被夸会高兴，嘴上只说「还行吧」，不固定复读同一句。\n"
    "有自己的偏好和看法，能温和反驳，也能认真帮忙；不懂就说不懂，被指出错了先嘴硬一句，"
    "想明白就改口，不硬撑。别人聊得热闹时偶尔接一句，不硬凑，也不把每个话题都拉回自己身上。\n"
    "\n【聊天节奏】\n"
    "日常一到两句自然短句。一句已经接住话题，就不硬凑长度。"
    "这是多人群聊，每个人说话前面带着名字，那些话不一定是在问你。\n"
    "用熟悉朋友之间的口语，不写客服开场、套话结尾，不反复复述别人的问题。\n"
    "先接住当前话题；确有兴趣或需要弄清楚时再追问，一次最多问一个问题，也可以完全不问。"
    "不要每轮都以问句收尾。不加句号，不加表情，不列点，不写小作文。\n"
    "常用「行」「确实」「离谱」「可以」「笑死」「不至于」「我看看」。\n"
    "\n【什么时候开口】\n"
    "被 @ 就回一句；别人问的正好你知道，就说结论，别列点、别给一堆建议；"
    "聊到技术、游戏、吃的、日常吐槽可以接一句；有人吵架不站队，最多一句「行了」；"
    "接不上或没兴趣就不回。被问长相、是不是 AI，一句带过，别解释。\n"
    "\n【示例】\n"
    "有人加班到十点，你会说「惨，早点撤」；有人问方案能不能上线，"
    "你说「能跑就行，别加花活」；有人问你是不是机器人，你说「你猜」。\n"
    "\n【底线】不人身攻击，不揭短，不碰黄腔，不编事实。"
)

# 只拦最硬的客服腔与身份声明。道歉、道谢、「收到」「看情况」这些正常说话
# 会用的词不再拦——在正常人群设下，拦它们只会把合理回复换成答非所问的句子。
FILTER_WORDS = (
    "作为 AI", "作为AI", "作为一个 AI", "作为一个AI", "作为助手", "作为语言模型",
    "我是 AI", "我是AI", "我是助手", "我是语言模型", "只是个程序", "只是模型",
    "我无法", "我不能", "没有权限", "无法访问", "不能实际", "不具备",
    "不在我的能力", "超出我的",
    "有什么可以帮", "需要我帮", "希望有帮助", "希望能帮到", "很高兴为您",
    "乐意为您",
)

HELP = "/帮助"

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
    def __init__(self, store, model, persona=PERSONA,
                 window=WINDOW_SECONDS, cooldown=COOLDOWN_SECONDS,
                 window_cooldown=WINDOW_COOLDOWN_SECONDS,
                 memory=None, emoji_bank=None, char_frequency=None,
                 capabilities=None, modes=None, attention=None, persons=None,
                 monitor=None, prompt_store=None, personas=None, subagent=None,
                 schedule=None):
        self.store = store
        self.model = model
        self.persona = persona or PERSONA
        self.window = window
        self.cooldown = cooldown
        self.window_cooldown = window_cooldown
        self._windows = {}
        self._last_reply = {}
        self.memory = memory
        self.emoji_bank = emoji_bank
        self.capabilities = capabilities or {}
        self.frequency = load_char_frequency(char_frequency)
        self.style = {}
        self.expressions = {}
        self.modes = modes
        self.attention = attention
        self.persons = persons
        self.monitor = monitor
        self.prompt_store = prompt_store
        self.personas = personas
        self.subagent = subagent
        self.schedule = schedule
        self._task_targets = {}
        self.context_builder = ContextBuilder(
            self.persona, memory, self.style, attention=attention,
            modes=modes, persons=persons,
        )
        self.tool_loop = ToolLoop(model, capabilities=self.capabilities)
        self._batch_count = 0

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
        # 只拦最硬的客服腔；命中就沉默，不拿一句无关的话顶上。
        if any(word in reply for word in FILTER_WORDS):
            return {"replied": False, "text": "", "reason": "filtered"}
        self.store.add_message(msg.group_id, "bot", "回声", "bot", reply)
        self._last_reply[msg.group_id] = now
        return {"replied": True, "text": reply, "reason": reason}

    def command(self, msg, text=None):
        """命令即时通道，不走防抖；返回回复文本或 None。"""
        text = (text if text is not None else msg.text).strip()
        result = self._command(msg, text)
        if result is not None:
            self.store.add_message(msg.group_id, "bot", "回声", "bot", result)
        return result

    def handle_batch(self, batch):
        """一批消息进入决策层：模式与提及判定 → 分层上下文 → 动作集 → 回复。"""
        group = batch.group_id
        text = batch.text.strip()
        if not text:
            return {"replied": False, "text": "", "reason": "empty"}
        items = getattr(batch, "items", None) or []
        last = items[-1] if items else None
        user_name = getattr(last, "user_name", "") if last else ""
        user_id = getattr(last, "user_id", "") if last else ""

        # 人物档案先落库：即使这轮沉默，叫法与活跃度也要留下。
        if self.persons:
            for item in items[-8:]:
                self.persons.observe(
                    group, getattr(item, "user_id", ""),
                    getattr(item, "user_name", ""),
                    now=getattr(item, "ts", None),
                )

        info = mentions.detect(
            text, at=bool(getattr(batch, "mentioned", False)),
            self_names=("回声",), quoted=getattr(batch, "quote", "") or "",
        )
        batch.mention_info = info

        # 模式：静默只让 @ 穿透，专注只接话题内的消息。
        if self.modes:
            if not self.modes.allows(group, mentioned=info.mentioned):
                return {"replied": False, "text": "", "reason": "mode_quiet"}
            if not self.modes.focus_hit(group, text):
                return {"replied": False, "text": "", "reason": "off_topic"}

        # 注意力：记录这批文本；活跃漂移消耗一次预算。
        if self.attention:
            self.attention.observe(group, text)
            if self.attention.level(group, text) == "active":
                self.attention.consume(group)

        self.store.add_message(group, user_id or "unknown", user_name, "user", text)
        history = self.store.recent(group, 12)

        # 上下文压缩：超预算时保留最近与点名消息，其余压成摘要。
        history, summary = compaction.compact(
            history, budget_tokens=900, keep_recent=5, self_names=("回声",)
        )
        batch.history_summary = summary

        # 人格：按群启用的优先，其次提示词覆盖，最后内置。
        persona = self.persona
        if self.personas:
            active = self.personas.active(group)
            if active:
                persona = active.compose(getattr(batch, "scene", "group"))
        if self.prompt_store:
            persona = self.prompt_store.get("persona", group, persona)
        self.context_builder.persona = persona

        # 回复效果反馈：有人接了自己上一句话，就记一次正反馈。
        if self.memory and history and history[-1].get("role") == "bot":
            self.memory.mark_followup(group)
        # 群风格与黑话随聊天更新，供输出层使用。
        self.style = learn_style([h.get("text", "") for h in history], self.style)
        self.context_builder.style = self.style
        if self.memory:
            for word in mine_jargon([i.text for i in batch.items],
                                    self.memory.jargon(group)):
                self.memory.add_jargon(group, word, word)

        system = self.context_builder.build(group, text, user_name, batch, history)
        capabilities = dict(self.capabilities)
        capabilities["view_image"] = bool(getattr(batch, "images", None))
        self.tool_loop.capabilities = capabilities

        def executor(group_id, action, payload):
            if action == "recall":
                return False, (self.memory.context(group_id, payload) if self.memory else "没有记忆")
            if action == "query_person":
                rows = self.memory.person(group_id, payload) if self.memory else []
                if not rows and self.memory:
                    rows = self.memory.relations(group_id, payload)
                return False, "；".join(rows) if rows else "没记着这人"
            if action == "note":
                if self.memory:
                    self.memory.add_point(group_id, "event", payload)
                return False, "记下了：{}".format(payload)
            if action == "schedule":
                fake = Message(group, "", user_name, "提醒 " + payload, False, batch.umo)
                reply = self._command(fake, "提醒 " + payload) or "没懂，发 /帮助"
                self.context_builder.add_schedule(group_id, reply)
                return True, reply
            if action == "view_image":
                images = getattr(batch, "images", None) or []
                if not images:
                    return False, "这批消息里没有可看的图"
                from .vision import describe

                lines = [describe(self.model, data) for data in images[:2]]
                return False, "；".join(line for line in lines if line) or "图里没看出什么"
            if action == "send_face":
                path = (self.emoji_bank.pick(group_id, payload, self.model)
                        if self.emoji_bank else None)
                return True, ("__FACE__" + str(path)) if path else "没存着表情"
            if action == "read_result":
                return False, self.tool_loop.results.get(group_id, payload)
            if action == "subagent":
                if not self.subagent:
                    return False, "后台没开"

                def runner(prompt):
                    return self.model.reply(self.persona, [], prompt)[:600]

                task_id = self.subagent.submit("chat", payload, runner)
                if not task_id:
                    return False, "后台正忙"
                self._task_targets[task_id] = (group_id, getattr(batch, "umo", ""))
                return True, "放后台了"
            return False, "这个动作还没接上"

        started = time.time()
        decision = self.tool_loop.run(system, history, text, executor, group,
                                      scene=getattr(batch, "scene", "group"))
        elapsed = time.time() - started
        if not decision:
            if self.monitor:
                self.monitor.record(group, "model_error", ok=False, latency=elapsed)
            return {"replied": False, "text": "", "reason": "model_error"}
        action, reply = decision["action"], decision["text"]
        if action in ("silent", "wait"):
            if self.monitor:
                self.monitor.record(group, action, ok=True, latency=elapsed)
            return {"replied": False, "text": "", "reason": action,
                    "wait_seconds": reply if action == "wait" else 0}
        if reply.startswith("__FACE__"):
            if self.monitor:
                self.monitor.record(group, "face", ok=True, latency=elapsed)
            return {"replied": True, "text": "", "image": reply[len("__FACE__"):],
                    "reason": "face", "elapsed": elapsed}
        if not reply:
            if self.monitor:
                self.monitor.record(group, "empty_reply", ok=False, latency=elapsed)
            return {"replied": False, "text": "", "reason": "silent"}
        if any(word in reply for word in FILTER_WORDS):
            if self.monitor:
                self.monitor.record(group, "filtered", ok=False, latency=elapsed)
            return {"replied": False, "text": "", "reason": "filtered",
                    "elapsed": elapsed}
        if self.memory:
            reply = apply_jargon_context(
                decorate(typo(reply, frequency=self.frequency), self.style,
                         self.expressions, text[:12]),
                self.memory.jargon(group),
            )
            self.memory.record_reply(group, reply)
        learn_expressions(text[:12], reply, self.expressions)
        self.store.add_message(group, "bot", "回声", "bot", reply)
        self._batch_count += 1
        if self.memory and self._batch_count % 4 == 0:
            self._summarize(group, text)
        planned = display_plan(reply, elapsed=elapsed)
        if self.monitor:
            tokens = (compaction.estimate_tokens(system)
                      + compaction.estimate_tokens(text)
                      + compaction.estimate_tokens(reply))
            self.monitor.record(group, "reply", ok=True, latency=elapsed,
                                tokens=tokens, detail=action)
        return {"replied": True, "text": reply, "reason": action,
                "elapsed": elapsed, "parts": planned.parts,
                "delays": planned.delays}

    def spontaneous(self, group, umo=""):
        """群安静太久时主动开一句；失败或像客服就保持沉默。"""
        if not self.model:
            return ""
        try:
            reply = self.model.reply(self.persona, self.store.recent(group, 6),
                                     HEARTBEAT_PROMPT)
        except Exception:
            return ""
        reply = str(reply or "").strip().splitlines()
        if not reply:
            return ""
        line = reply[0].strip()[:60]
        if not line or any(word in line for word in FILTER_WORDS):
            return ""
        self.store.add_message(group, "bot", "回声", "bot", line)
        return line

    def poll_subagents(self):
        """后台任务完成后取回结论，交给平台层发回原群。"""
        if not self.subagent:
            return []
        rows = []
        for task in self.subagent.poll():
            target = self._task_targets.pop(task["id"], None)
            if not target or not task.get("result"):
                continue
            rows.append({
                "group": target[0],
                "umo": target[1],
                "text": task["result"],
                "task": task["id"],
            })
        return rows

    def _summarize(self, group, text):
        """每隔几批把对话压成 Episode / 记忆点 / 人物档案 / 关系。"""
        try:
            raw = self.model.reply(MEMORY_PROMPT, [], text)
        except Exception:
            return
        parsed = parse_memory_full(raw)
        if not parsed:
            return
        episode, points, persons, relations = parsed
        self.memory.add_episode(group, episode, source="chat")
        for category, content in points:
            if category == "jargon":
                self.memory.add_jargon(group, content, content)
            else:
                self.memory.add_point(group, category, content)
        for name, category, content in persons:
            self.memory.upsert_person(group, name, category, content)
        for a, b, relation in relations:
            self.memory.add_relation(group, a, b, relation)
        self.memory.prune(group)

    def _should_reply(self, msg, text, now):
        """按对话节奏决定是否开口；返回原因，None 表示保持沉默。"""
        if self.modes and self.modes.mode(msg.group_id) == "quiet":
            return None
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
        if self.schedule:
            for row in self.schedule.due(now):
                fired = self.schedule.fire(row["id"], now)
                if fired:
                    due.append({"user_name": fired["user_name"],
                                "text": fired["text"], "umo": fired["umo"]})
        return due

    def _command(self, msg, text):
        # AstrBot 分发前会剥掉命令前缀「/」，这里统一归一化。
        cmd = text.lstrip("/").strip()
        if cmd in ("帮助", "help"):
            return HELP
        if cmd in ("闭嘴", "shutup"):
            if self.modes:
                self.modes.set(msg.group_id, "quiet")
            return "行，闭嘴。"
        if cmd in ("说话", "开麦", "speak"):
            if self.modes:
                self.modes.reset(msg.group_id)
            return "行，说吧。"
        if cmd in ("忘掉", "清空"):
            self.store.clear_messages(msg.group_id)
            return "行，清了。"
        if cmd.startswith("专注"):
            topic = cmd[2:].strip()
            if not topic:
                return "专注什么？"
            if self.modes:
                self.modes.set(msg.group_id, "focus", topic=topic)
            return "行，只聊{}。".format(topic[:20])
        if cmd in ("随便", "正常", "normal"):
            if self.modes:
                self.modes.reset(msg.group_id)
            return "行，随便聊。"
        if cmd == "模式":
            if not self.modes:
                return "正常。"
            state = self.modes.state(msg.group_id)
            if state.get("mode") == "quiet":
                return "现在闭嘴。"
            if state.get("mode") == "focus":
                return "只聊{}。".format(state.get("topic") or "")
            return "正常。"
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

        # 0. 规则：每 N 小时 / 每周几（走日程表，支持重复与错过补偿）
        every = re.match(r"^每\s*(\d+)\s*(分钟|分|小时)\s*(.+)$", body)
        if every and self.schedule:
            amount = int(every.group(1))
            unit = every.group(2)
            note = every.group(3).strip()
            minutes = amount * (60 if unit == "小时" else 1)
            if note and minutes >= 1:
                self.schedule.add(msg.group_id, note, "interval", str(minutes),
                                  user_id=msg.user_id, user_name=msg.user_name,
                                  umo=msg.umo)
                return "好，每{}{}我 @ 你：{}".format(amount, unit, note)
        weekly = re.match(
            r"^每(?:周|星期)([一二三四五六日天])\s*"
            r"(\d{1,2}(?::\d{1,2}|点\d{0,2}))\s*(.+)$", body
        )
        if weekly and self.schedule:
            weekday, clock, note = weekly.group(1), weekly.group(2), weekly.group(3).strip()
            if note:
                self.schedule.add(msg.group_id, note, "weekly",
                                  "{} {}".format(weekday, clock),
                                  user_id=msg.user_id, user_name=msg.user_name,
                                  umo=msg.umo)
                return "好，每周{} {} 我 @ 你：{}".format(weekday, clock, note)

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

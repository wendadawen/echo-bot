"""决策层（完整版）：动作注册表、场景化工具发现、参数校验、结果压缩与暂存、
多轮工具循环、动作合理性判定。

对齐 MaiBot `src/maisaka/builtin_tool`、`tool_discovery.py`、`tool_arguments.py`、
`tool_result_condenser.py`、`tool_result_store.py`、`planner_action_judge.py`。
模型只从有限动作里选一个，程序执行动作、压缩结果、回填观察，再决定是否继续。
只用标准库。
"""

import json
import re
import threading

# name -> (说明, 参数名, 是否终态, 需要的场景)
ACTION_SPECS = {
    "subagent": ("能执行命令、跑代码、读写并生成群文件（HTML/脚本/文档等）、联网查资料，给真实结果",
                 "text", True, "any"),
    "reply": ("说一句话", "text", True, "any"),
    "silent": ("这次不说话", "", True, "any"),
    "wait": ("等下一批，可带秒数", "seconds", True, "group"),
    "recall": ("查本群记忆与情节", "text", False, "any"),
    "note": ("记一笔", "text", False, "any"),
    "query_person": ("查某人档案", "text", False, "any"),
    "send_face": ("发一个表情", "text", True, "group"),
    "view_image": ("看这张图", "text", False, "any"),
    "schedule": ("设一个提醒", "text", False, "any"),
    "read_result": ("读取上一轮工具结果", "text", False, "any"),
}

ACTIONS = tuple(ACTION_SPECS)

SCENE_ACTIONS = {
    "group": ACTIONS,
    "private": tuple(a for a in ACTIONS if a not in ("wait", "send_face", "schedule")),
}

ACTION_PROMPT = (
    "你只输出一行 JSON，不要解释、不要代码块。\n"
    '格式：{"action":"动作名","text":"内容或参数"}；wait 可用 seconds 代替 text。\n'
    "{actions}\n"
    "先看是不是在跟你说话：没 @ 你、没点名、没引用你，只是群友互相闲聊/附和"
    "（例如“有道理”“修好了”“看来有效”）时，默认选 silent，别强行插话。\n"
    "再判断需要什么动作：\n"
    "- 需要实时信息、执行代码/命令、生成并发送本地文件（HTML 网页、代码、文档、表格、压缩包等）、"
    "多步查资料，或你自己没把握 → subagent，把具体任务写进 text（不要让它代写群聊回复）；\n"
    '  派 subagent 时可额外带 "reply" 字段（最多 40 字）先回群友一句，'
    '例：{"action":"subagent","reply":"抱歉刚才漏了，我现在补查。","text":"调研目前先进的 MoE offloading 论文并输出报告"}。\n'
    "  严禁在纯 reply 里口头承诺“我现在去查/补查/马上帮你查/我去搜一下”却不派 subagent；"
    "决定要查或要做就必须直接选 action=\"subagent\"。\n"
    "- 纯闲聊、情绪、玩笑、稳定常识、群内已知信息 → reply，最多两句；\n"
    "- 确实不该接话 → silent。\n"
    "群聊支持生成并发送本地文件（HTML/代码/文档等）到群里，但无法部署到公网/IP、"
    "无法起常驻或监听服务、也无法用后台线程绕过超时；"
    "遇到部署公网、起服务或绕过超时的要求，直接 reply 婉拒，绝不要派 subagent。\n"
    "不要因为怕答错就 silent，怕答错就用 subagent。\n"
    "一批里有多条在问你，就在一条回复里分别接住，不要只挑最后一条；"
    "实在接不下时，优先回 @ 你、问得最具体的那条，其余用半句带过。\n"
    '例：{"action":"reply","text":"少来这套"}'
)

MAX_ROUNDS = 6
RESULT_LIMIT = 260
RESULT_STORE_LIMIT = 800
MAX_PARAM_TEXT = 300
MAX_SUBAGENT_TEXT = 1200
MAX_REPLY_TEXT = 2000
_CTRL_PATTERN = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def available(scene="group", capabilities=None):
    """工具发现：只暴露当前场景与已接能力支持的动作。"""
    names = SCENE_ACTIONS.get(scene, SCENE_ACTIONS["group"])
    if capabilities is None:
        return names
    return tuple(n for n in names if capabilities.get(n, True))


def render_prompt(scene="group", capabilities=None):
    lines = []
    for name in available(scene, capabilities):
        desc, arg, terminal, _ = ACTION_SPECS[name]
        lines.append("- {}：{}{}".format(name, desc, "（终态）" if terminal else ""))
    # 模板里本身有 JSON 花括号，不能用 str.format。
    return ACTION_PROMPT.replace("{actions}", "动作列表：\n" + "\n".join(lines))


def _strip_code_fence(raw):
    cleaned = str(raw or "").strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.I)
        cleaned = re.sub(r"\s*```\s*$", "", cleaned)
    return cleaned.strip()


def _looks_like_control_json(raw):
    text = _strip_code_fence(raw)
    if not text:
        return False
    return bool(re.search(r"""\{\s*['"]action['"]\s*:""", text))


def _payload_limit(action):
    if action == "reply":
        return MAX_REPLY_TEXT
    if action == "subagent":
        return MAX_SUBAGENT_TEXT
    return MAX_PARAM_TEXT


def parse(raw):
    """解析模型输出；失败返回 None，由调用方降级。保持只返回 action/text 两键。"""
    full = parse_full(raw)
    if full is None:
        return None
    return {"action": full["action"], "text": full["text"]}


def parse_full(raw):
    if not isinstance(raw, str):
        return None
    cleaned = _strip_code_fence(raw)
    if not cleaned:
        return None
    decoder = json.JSONDecoder()
    idx = 0
    while idx < len(cleaned):
        pos = cleaned.find("{", idx)
        if pos < 0:
            break
        try:
            data, end = decoder.raw_decode(cleaned, pos)
        except (ValueError, TypeError):
            idx = pos + 1
            continue
        if isinstance(data, dict) and data.get("action") in ACTIONS:
            action = data["action"]
            if action == "wait":
                raw_val = data.get("seconds") if data.get("seconds") is not None else data.get("text")
            else:
                raw_val = data.get("text") if data.get("text") is not None else data.get("seconds")
            limit = _payload_limit(action)
            text = str(raw_val if raw_val is not None else "").strip()[:limit]
            reply = str(data.get("reply") or "").strip()[:120]
            return {"action": action, "text": text, "seconds": data.get("seconds"), "reply": reply}
        idx = max(pos + 1, end)
    return None


def validate(action, payload, scene="group", capabilities=None, reply=""):
    """参数校验：防住明显不合法的动作、超长与控制字符输入。"""
    if action not in available(scene, capabilities):
        if action in ("silent", "wait") and capabilities is not None and not capabilities.get("silent", True):
            return False, "有人明确点到了你，请直接 reply 或 subagent，不要 silent 或 wait"
        return False, "这个动作在当前场景不可用"
    spec = ACTION_SPECS[action]
    if spec[1] == "text" and not payload and action not in ("view_image", "read_result"):
        return False, "缺少参数"
    if len(payload) > _payload_limit(action):
        return False, "参数过长"
    if _CTRL_PATTERN.search(payload) or (reply and _CTRL_PATTERN.search(str(reply))):
        return False, "参数含控制字符"
    if action == "wait":
        try:
            seconds = float(payload or 0)
        except (TypeError, ValueError):
            return False, "等待秒数不合法"
        if seconds < 0 or seconds > 600:
            return False, "等待秒数超出范围"
    return True, ""


def condense(result, limit=RESULT_LIMIT):
    """压缩工具结果：去空白、去重复的礼貌话、截断。"""
    text = re.sub(r"\s+", " ", str(result or "")).strip()
    for prefix in ("好的", "没问题", "根据搜索结果", "我已经为你"):
        if text.startswith(prefix):
            text = text[len(prefix):].lstrip("，,：: ")
    if len(text) > limit:
        text = text[:limit] + "…"
    return text or "（无结果）"


class ToolResultStore:
    """暂存每轮工具结果，供 read_result 动作取回，也用于审计。"""

    def __init__(self, limit=20):
        self.limit = limit
        self._rows = []
        self._lock = threading.Lock()

    def add(self, group_id, action, result):
        with self._lock:
            self._rows.append((group_id, action, condense(result, limit=RESULT_STORE_LIMIT)))
            del self._rows[:-self.limit]
            return len(self._rows)

    def get(self, group_id, index=None):
        with self._lock:
            rows = [r for r in self._rows if r[0] == group_id]
        if index is None or str(index).strip() == "":
            return rows[-1][2] if rows else ""
        key = str(index).strip()
        try:
            return rows[int(key)][2]
        except (ValueError, IndexError, TypeError):
            for _, action, text in reversed(rows):
                if action == key:
                    return text
            return ""

    def recent(self, group_id, limit=3):
        with self._lock:
            rows = [r for r in self._rows if r[0] == group_id]
        return ["[{}] {}".format(action, text) for _, action, text in rows[-limit:]]


def judge(action, payload, history, last_action, allow_silent=True):
    """动作合理性判定：避免连续 wait、空回复、复读同一句。"""
    if action == "wait" and last_action == "wait":
        return ("reply", "嗯，你们继续聊") if not allow_silent else ("silent", "")
    if action == "reply":
        if not payload:
            return ("silent", "") if allow_silent else ("reply", "在呢。")
        if allow_silent and history and payload.strip() == str(history[-1]).strip():
            return "silent", ""
        if len(payload) > MAX_REPLY_TEXT:
            return "reply", payload[:MAX_REPLY_TEXT]
    return action, payload


class ToolLoop:
    """模型选动作 → 校验 → 执行 → 压缩 → 暂存 → 回填观察 → 继续。

    executor(group_id, action, payload) 返回 (是否终态, 结果文本)。
    """

    def __init__(self, model, max_rounds=MAX_ROUNDS, capabilities=None):
        self.model = model
        self.max_rounds = max_rounds
        self.capabilities = capabilities
        self.results = ToolResultStore()
        self._local = threading.local()

    @property
    def last_error(self):
        return getattr(self._local, "last_error", "")

    def _set_error(self, value):
        self._local.last_error = str(value or "")

    def run(self, system, history, user_text, executor, group_id, scene="group",
            capabilities=None):
        self._set_error("")
        caps = self.capabilities if capabilities is None else capabilities
        allow_silent = bool(caps is None or caps.get("silent", True))
        bot_replies = [
            str(h.get("text") or "")
            for h in (history or ())
            if isinstance(h, dict) and h.get("role") == "bot"
        ]
        prompt = system + "\n\n" + render_prompt(scene, caps)
        observations = []
        last_action = None
        rounds = 0
        for _ in range(self.max_rounds):
            rounds += 1
            round_prompt = prompt
            if observations:
                round_prompt += "\n\n前几轮工具结果：\n" + "\n".join(observations[-3:])
            try:
                raw = self.model.reply(round_prompt, history, user_text)
            except Exception as exc:
                self._set_error(exc)
                return None
            full = parse_full(raw)
            if full is None:
                if _looks_like_control_json(raw):
                    observations.append("[格式错误] JSON 不合法或未闭合，请输出完整单行 JSON")
                    continue
                # 模型按普通口语直答时当作普通回复。
                return {"action": "reply", "text": str(raw or "").strip()[:MAX_REPLY_TEXT], "rounds": rounds}
            action, payload = full["action"], full["text"]
            ok, reason = validate(action, payload, scene, capabilities=caps, reply=full.get("reply", ""))
            if not ok:
                observations.append("[校验失败] {}".format(reason))
                continue
            action, payload = judge(action, payload, bot_replies, last_action, allow_silent=allow_silent)
            last_action = action
            if action == "read_result":
                observations.append("[read_result] {}".format(
                    self.results.get(group_id, payload)))
                continue
            spec = ACTION_SPECS[action]
            if spec[2]:  # 终态
                out = {"action": action, "text": payload, "rounds": rounds}
                if full.get("reply"):
                    out["reply"] = full["reply"]
                return out
            done, result = executor(group_id, action, payload)
            text = condense(result)
            self.results.add(group_id, action, result)
            observations.append("[{}] {}".format(action, text))
            if done:
                return {"action": "reply", "text": text[:MAX_REPLY_TEXT], "rounds": rounds}
        return {"action": "silent", "text": "", "rounds": rounds}

"""决策层（完整版）：动作注册表、场景化工具发现、参数校验、结果压缩与暂存、
多轮工具循环、动作合理性判定。

对齐 MaiBot `src/maisaka/builtin_tool`、`tool_discovery.py`、`tool_arguments.py`、
`tool_result_condenser.py`、`tool_result_store.py`、`planner_action_judge.py`。
模型只从有限动作里选一个，程序执行动作、压缩结果、回填观察，再决定是否继续。
只用标准库。
"""

import json
import re

# name -> (说明, 参数名, 是否终态, 需要的场景)
ACTION_SPECS = {
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
    "拿不准就 silent。text 最多两句，别写 markdown、别列点、别解释。\n"
    '例：{"action":"reply","text":"少来这套"}'
)

MAX_ROUNDS = 6
RESULT_LIMIT = 260


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


def parse(raw):
    """解析模型输出；失败返回 None，由调用方降级。保持只返回 action/text 两键。"""
    full = parse_full(raw)
    if full is None:
        return None
    return {"action": full["action"], "text": full["text"]}


def parse_full(raw):
    if not isinstance(raw, str):
        return None
    cleaned = re.sub(r"```(?:json)?", "", raw.strip()).strip()
    match = re.search(r"\{[^{}]*\}", cleaned, re.S)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    action = data.get("action")
    if action not in ACTIONS:
        return None
    text = str(data.get("text") or data.get("seconds") or "").strip()[:300]
    return {"action": action, "text": text, "seconds": data.get("seconds")}


def validate(action, payload, scene="group"):
    """参数校验：防住明显不合法的动作与超长/控制字符输入。"""
    if action not in available(scene):
        return False, "这个动作在当前场景不可用"
    spec = ACTION_SPECS[action]
    if spec[1] == "text" and not payload and action != "view_image":
        return False, "缺少参数"
    if len(payload) > 300:
        return False, "参数过长"
    if re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", payload):
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

    def add(self, group_id, action, result):
        self._rows.append((group_id, action, condense(result)))
        del self._rows[:-self.limit]
        return len(self._rows)

    def get(self, group_id, index=None):
        rows = [r for r in self._rows if r[0] == group_id]
        if index is None:
            return rows[-1][2] if rows else ""
        try:
            return rows[int(index)][2]
        except (ValueError, IndexError, TypeError):
            return ""

    def recent(self, group_id, limit=3):
        rows = [r for r in self._rows if r[0] == group_id]
        return ["[{}] {}".format(action, text) for _, action, text in rows[-limit:]]


def judge(action, payload, history, last_action):
    """动作合理性判定：避免连续 wait、空回复、复读同一句。"""
    if action == "wait" and last_action == "wait":
        return "silent", ""
    if action == "reply":
        if not payload:
            return "silent", ""
        if history and payload.strip() == str(history[-1]).strip():
            return "silent", ""
        if len(payload) > 160:
            return "reply", payload[:160]
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

    def run(self, system, history, user_text, executor, group_id, scene="group"):
        prompt = system + "\n\n" + render_prompt(scene, self.capabilities)
        observations = []
        last_action = None
        for _ in range(self.max_rounds):
            round_prompt = prompt
            if observations:
                round_prompt += "\n\n前几轮工具结果：\n" + "\n".join(observations[-3:])
            try:
                raw = self.model.reply(round_prompt, history, user_text)
            except Exception:
                return None
            full = parse_full(raw)
            if full is None:
                # 模型没按格式输出时把原文当作一次普通回复。
                return {"action": "reply", "text": str(raw or "").strip()[:300]}
            action, payload = full["action"], full["text"]
            ok, reason = validate(action, payload, scene)
            if not ok:
                observations.append("[校验失败] {}".format(reason))
                last_action = action
                continue
            action, payload = judge(action, payload, observations, last_action)
            last_action = action
            if action == "read_result":
                observations.append("[read_result] {}".format(
                    self.results.get(group_id, payload)))
                continue
            spec = ACTION_SPECS[action]
            if spec[2]:  # 终态
                return {"action": action, "text": payload}
            done, result = executor(group_id, action, payload)
            text = condense(result)
            self.results.add(group_id, action, text)
            observations.append("[{}] {}".format(action, text))
            if done:
                return {"action": "reply", "text": text[:300]}
        return {"action": "silent", "text": ""}

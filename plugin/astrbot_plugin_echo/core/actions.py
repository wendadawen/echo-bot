"""决策层：有限动作集、模型输出解析与多轮工具循环。

参考 MaiBot 的 builtin_tool / Planner：模型只从有限动作里选一个，
程序执行动作并把结果回填，再决定是否继续下一轮。只用标准库。
"""

import json
import re

ACTIONS = (
    "reply", "silent", "wait", "recall", "note",
    "query_person", "send_face", "view_image", "schedule",
)

ACTION_PROMPT = (
    "你只输出一行 JSON，不要解释、不要代码块。\n"
    '格式：{"action":"动作名","text":"内容或参数"}\n'
    "动作：reply 说话；silent 不说；wait 等下一批；recall 查本群记忆；"
    "note 记一笔；query_person 查某人档案；send_face 发个表情；"
    "view_image 看图片；schedule 设提醒。\n"
    "拿不准就 silent。text 最多两句，别写 markdown、别列点、别解释。\n"
    '例：{"action":"reply","text":"少来这套"}'
)

MAX_ROUNDS = 4


def parse(raw):
    """解析模型输出；失败返回 None，由调用方降级。"""
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
    if not isinstance(data, dict) or data.get("action") not in ACTIONS:
        return None
    text = str(data.get("text") or "").strip()[:300]
    return {"action": data["action"], "text": text}


class ToolLoop:
    """模型选动作 → 执行器执行 → 结果回填 → 继续，直到产出终态或轮数用尽。

    executor(group_id, action, text) 返回 (是否终态, 结果文本)。
    """

    def __init__(self, model, max_rounds=MAX_ROUNDS):
        self.model = model
        self.max_rounds = max_rounds

    def run(self, system, history, user_text, executor, group_id):
        observations = []
        for _ in range(self.max_rounds):
            prompt = system + ACTION_PROMPT
            if observations:
                prompt += "\n\n前几轮工具结果：\n" + "\n".join(observations[-3:])
            try:
                raw = self.model.reply(prompt, history, user_text)
            except Exception:
                return None
            action = parse(raw)
            if action is None:
                # 模型没按格式输出时，把原文当作一次普通回复。
                return {"action": "reply", "text": str(raw or "").strip()[:300]}
            if action["action"] in ("reply", "silent", "wait", "send_face"):
                return action
            done, result = executor(group_id, action["action"], action["text"])
            observations.append("[{}] {}".format(action["action"], result[:200]))
            if done:
                return {"action": "reply", "text": result[:300]}
        return {"action": "silent", "text": ""}

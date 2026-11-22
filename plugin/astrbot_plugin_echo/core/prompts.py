"""提示词库与人格库：内置默认、运行时覆盖、按群覆盖、导入导出。

对齐 MaiBot `src/config/personality_service.py` 与 `docs/personality-import.md`
的字段划分（人格 / 行为 / 回复风格分开写），实现与校验自行编写。
只依赖标准库，不 import 框架。
"""

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

MAX_TEXT = 65536

# 可被覆盖的提示词名；没有文件时回落到调用方传入的默认值。
PROMPT_NAMES = ("persona", "action", "memory", "parse", "drift", "supplement")


@dataclass
class Persona:
    """一套人格。字段与 MaiBot 人格导入协议一一对应，便于互换。"""

    id: str
    name: str
    personality: str = ""
    behavior_style: str = ""
    reply_style: str = ""
    group_chat_prompt: str = ""
    private_chat_prompt: str = ""
    boundaries: str = ""
    examples: list = field(default_factory=list)

    def compose(self, scene="group"):
        """把分字段人格拼成一段 system；顺序固定，便于模型稳定遵循。"""
        parts = []
        if self.personality:
            parts.append("【性格】\n" + self.personality.strip())
        if self.behavior_style:
            parts.append("【行为】\n" + self.behavior_style.strip())
        if self.reply_style:
            parts.append("【说话】\n" + self.reply_style.strip())
        scene_prompt = self.group_chat_prompt if scene == "group" else self.private_chat_prompt
        if scene_prompt:
            parts.append("【场景】\n" + scene_prompt.strip())
        if self.examples:
            lines = []
            for item in self.examples[:8]:
                if isinstance(item, dict):
                    ask = str(item.get("ask") or item.get("user") or "").strip()
                    ans = str(item.get("answer") or item.get("bot") or "").strip()
                    if ask and ans:
                        lines.append("群友：{}\n你：{}".format(ask, ans))
                elif isinstance(item, str) and item.strip():
                    lines.append(item.strip())
            if lines:
                parts.append("【示例】\n" + "\n".join(lines))
        if self.boundaries:
            parts.append("【底线】" + self.boundaries.strip())
        return "\n\n".join(parts)


class PromptStore:
    """文件式提示词覆盖层：`data/prompts/<name>.txt` 与 `data/prompts/<群>/<name>.txt`。"""

    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, name, group_id=None):
        base = self.root / group_id if group_id else self.root
        return base / "{}.txt".format(name)

    def get(self, name, group_id=None, default=""):
        for group in (group_id, None):
            if group == "":
                continue
            path = self._path(name, group)
            try:
                text = path.read_text(encoding="utf-8").strip()
            except (OSError, ValueError):
                continue
            if text:
                return text
        return default

    def set(self, name, text, group_id=None):
        if name not in PROMPT_NAMES:
            raise ValueError("未知提示词：{}".format(name))
        text = str(text or "").strip()
        if not text:
            raise ValueError("提示词不能为空")
        if len(text) > MAX_TEXT:
            raise ValueError("提示词过长")
        path = self._path(name, group_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text + "\n", encoding="utf-8")
        return path

    def reset(self, name, group_id=None):
        path = self._path(name, group_id)
        if path.exists():
            path.unlink()
            return True
        return False

    def describe(self, group_id=None):
        rows = []
        for name in PROMPT_NAMES:
            path = self._path(name, group_id)
            source = "默认"
            size = 0
            if path.exists():
                source = "本群覆盖" if group_id else "全局覆盖"
                size = path.stat().st_size
            rows.append({"name": name, "source": source, "bytes": size})
        return rows


class PersonaLibrary:
    """内置人格 + `data/personas/*.json`；按群记录当前启用的人格。"""

    def __init__(self, root, builtin=None):
        self.root = Path(root)
        self.builtin = builtin
        self.root.mkdir(parents=True, exist_ok=True)

    def _file(self, persona_id):
        safe = "".join(ch for ch in persona_id if ch.isalnum() or ch in "-_")
        return self.root / "{}.json".format(safe or "persona")

    def list(self):
        rows = []
        if self.builtin:
            rows.append(self.builtin)
        for path in sorted(self.root.glob("*.json")):
            data = self._read(path)
            if data:
                rows.append(data)
        return rows

    def get(self, persona_id):
        if self.builtin and persona_id == self.builtin.id:
            return self.builtin
        for path in self.root.glob("*.json"):
            data = self._read(path)
            if data and data.id == persona_id:
                return data
        return None

    def save(self, persona):
        if not isinstance(persona, Persona):
            raise TypeError("需要 Persona")
        path = self._file(persona.id)
        path.write_text(
            json.dumps(asdict(persona), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return path

    def _read(self, path):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return self._from_dict(data)

    def _from_dict(self, data):
        if not isinstance(data, dict):
            return None
        pid = str(data.get("id") or data.get("name") or "").strip()
        if not pid:
            return None
        examples = data.get("examples")
        if not isinstance(examples, list):
            examples = []
        return Persona(
            id=pid,
            name=str(data.get("name") or pid),
            personality=str(data.get("personality") or ""),
            behavior_style=str(data.get("behavior_style") or ""),
            reply_style=str(data.get("reply_style") or ""),
            group_chat_prompt=str(data.get("group_chat_prompt") or ""),
            private_chat_prompt=str(data.get("private_chat_prompt") or ""),
            boundaries=str(data.get("boundaries") or ""),
            examples=examples,
        )

    # ---- 按群启用 ----
    def _active_path(self):
        return self.root / "active.json"

    def _active_map(self):
        try:
            data = json.loads(self._active_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def activate(self, group_id, persona_id):
        persona = self.get(persona_id)
        if persona is None:
            raise ValueError("没有这个人格：{}".format(persona_id))
        data = self._active_map()
        data[str(group_id)] = persona_id
        self._active_path().write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        return persona

    def active(self, group_id):
        pid = self._active_map().get(str(group_id))
        persona = self.get(pid) if pid else None
        return persona or self.builtin

    # ---- 导入导出（兼容 MaiBot 协议）----
    def import_json(self, raw):
        try:
            data = json.loads(raw)
        except ValueError as exc:
            raise ValueError("不是合法 JSON：{}".format(exc))
        if not isinstance(data, dict):
            raise ValueError("顶层必须是对象")
        if data.get("format") == "maibot.personality":
            if int(data.get("version") or 0) != 1:
                raise ValueError("只支持 version 1")
            profile = data.get("profile")
            if not isinstance(profile, dict):
                raise ValueError("缺少 profile")
            allowed = {
                "name", "personality", "behavior_style", "reply_style",
                "group_chat_prompt", "private_chat_prompts", "boundaries",
                "multiple_reply_style", "multiple_probability",
            }
            extra = set(profile) - allowed
            if extra:
                raise ValueError("不认识的字段：{}".format("、".join(sorted(extra))))
            name = str(profile.get("name") or "").strip()
            if not name:
                raise ValueError("name 不能为空")
            for key in ("personality", "behavior_style", "reply_style",
                        "group_chat_prompt", "private_chat_prompts"):
                value = profile.get(key, "")
                if not isinstance(value, str):
                    raise ValueError("{} 必须是字符串".format(key))
                if len(value) > MAX_TEXT:
                    raise ValueError("{} 过长".format(key))
            persona = Persona(
                id="import-" + str(int(time.time())),
                name=name,
                personality=profile.get("personality", ""),
                behavior_style=profile.get("behavior_style", ""),
                reply_style=profile.get("reply_style", ""),
                group_chat_prompt=profile.get("group_chat_prompt", ""),
                private_chat_prompt=profile.get("private_chat_prompts", ""),
                boundaries=str(profile.get("boundaries") or ""),
            )
        else:
            persona = self._from_dict(data)
            if persona is None:
                raise ValueError("缺少 id 或 name")
            for value in (persona.personality, persona.behavior_style,
                          persona.reply_style, persona.boundaries):
                if len(value) > MAX_TEXT:
                    raise ValueError("字段过长")
        return persona

    def export_json(self, persona_id):
        persona = self.get(persona_id)
        if persona is None:
            raise ValueError("没有这个人格：{}".format(persona_id))
        return json.dumps(
            {"format": "maibot.personality", "version": 1, "profile": {
                "name": persona.name,
                "personality": persona.personality,
                "behavior_style": persona.behavior_style,
                "reply_style": persona.reply_style,
                "group_chat_prompt": persona.group_chat_prompt,
                "private_chat_prompts": persona.private_chat_prompt,
                "boundaries": persona.boundaries,
                "multiple_reply_style": [],
                "multiple_probability": 0,
            }},
            ensure_ascii=False,
            indent=2,
        )

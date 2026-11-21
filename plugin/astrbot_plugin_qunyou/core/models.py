"""可插拔的模型层：默认本地模拟，配置后接任意 OpenAI 兼容接口。

插件通过 asyncio.to_thread 调用本模块，同步网络请求不会阻塞事件循环。
"""

import json
import os
import re
import urllib.request


class BaseModel:
    name = "base"

    def reply(self, system, history, user_text):
        raise NotImplementedError


class MockModel(BaseModel):
    """零配置回复，用于在没有 API 凭据时验证整条链路。"""

    name = "mock"

    def reply(self, system, history, user_text):
        text = user_text.strip()
        memory = "；".join(line[2:] for line in system.splitlines() if line.startswith("- "))
        if any(k in text for k in ("你好", "在吗", "哈喽", "早", "hi")):
            return "在呢，怎么啦？"
        if "?" in text or "？" in text or any(
            k in text for k in ("怎么", "为什么", "是不是", "能不能", "如何")
        ):
            if memory:
                return "我记得群里说过 {}，这事我倾向先按那个来，你觉得呢？".format(memory)
            return "这个我得想想……你先说说前因后果？"
        if any(k in text for k in ("哈哈", "笑死", "绝了", "牛", "离谱")):
            return "哈哈哈确实，这梗我能记一整年。"
        if re.search(r"外号|叫.*什么|记一下|记住", text):
            return "记下啦，以后我就这么叫。"
        if memory:
            return "接一句：{} 这事儿还挺有意思的。".format(memory)
        return "嗯嗯，我在听。你们继续聊，有需要喊我一声。"


class OpenAIModel(BaseModel):
    """OpenAI 兼容的 chat/completions 接口，也可指向本机 Ollama。"""

    name = "openai"

    def __init__(self, api_key, api_base, model, timeout=40):
        self.api_key = api_key
        self.url = api_base.rstrip("/") + "/chat/completions"
        self.model = model
        self.timeout = timeout

    def reply(self, system, history, user_text):
        messages = [{"role": "system", "content": system}]
        for item in history:
            role = "assistant" if item["role"] == "bot" else "user"
            content = item["text"]
            if role == "user" and item.get("user_name"):
                content = "{}：{}".format(item["user_name"], content)
            messages.append({"role": role, "content": content})
        messages.append({"role": "user", "content": user_text})
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": 0.8,
            "max_tokens": 300,
        }
        req = urllib.request.Request(
            self.url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + self.api_key,
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data["choices"][0]["message"]["content"].strip()


def build_model(config=None):
    """优先读插件面板配置，其次读环境变量；都没有就用本地模拟。"""
    config = config or {}
    key = str(config.get("api_key") or os.environ.get("BOT_API_KEY", "")).strip()
    if not key:
        return MockModel()
    base = str(
        config.get("api_base") or os.environ.get("BOT_API_BASE", "https://api.openai.com/v1")
    ).strip()
    model = str(config.get("model") or os.environ.get("BOT_MODEL", "gpt-4o-mini")).strip()
    return OpenAIModel(key, base, model)

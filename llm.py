# -*- coding: utf-8 -*-
"""调用任意 OpenAI 兼容的聊天接口（DeepSeek、OpenAI、Moonshot、本地 Ollama…），只用标准库。"""
import json
import urllib.request
from urllib.error import HTTPError, URLError

import config


class LLMError(RuntimeError):
    pass


def available() -> bool:
    return bool(config.load()["api_key"])


def chat(messages: list[dict], max_tokens: int = 600, temperature: float = 0.7, json_mode: bool = False) -> str:
    settings = config.load()
    if not settings["api_key"]:
        raise LLMError("还没有填 API Key（见 README「填配置」）。")
    body = {
        "model": settings["model"],
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": False,
    }
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    request = urllib.request.Request(
        settings["api_base"].rstrip("/") + "/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={"Authorization": f"Bearer {settings['api_key']}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return str(payload["choices"][0]["message"]["content"] or "").strip()
    except HTTPError as error:
        raise LLMError(f"接口返回 {error.code}，检查 API Key、地址和模型名。") from error
    except (URLError, TimeoutError) as error:
        raise LLMError("连不上模型接口，检查网络和 api_base。") from error
    except (KeyError, IndexError, TypeError, ValueError) as error:
        raise LLMError("模型接口返回的格式不对。") from error


def chat_json(messages: list[dict], max_tokens: int = 300) -> dict:
    text = chat(messages, max_tokens=max_tokens, temperature=0.1, json_mode=True)
    text = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        data = json.loads(text or "{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}

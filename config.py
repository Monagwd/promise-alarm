# -*- coding: utf-8 -*-
"""读 config.json（没有就用默认值）。环境变量 PROMISE_API_KEY 可以代替文件里的 api_key。"""
import json
import os
from datetime import timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"

DEFAULTS = {
    "api_base": "https://api.deepseek.com/v1",
    "api_key": "",
    "model": "deepseek-chat",
    "utc_offset_hours": 8,
    "char_name": "小机",
    "persona": "你是小机，她的 AI 恋人。说话温柔、简短、像微信聊天，偶尔调侃。",
    "webhook_url": "",
}


def load() -> dict:
    settings = dict(DEFAULTS)
    try:
        saved = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        if isinstance(saved, dict):
            settings.update({key: value for key, value in saved.items() if key in DEFAULTS})
    except (OSError, ValueError):
        pass
    settings["api_key"] = os.environ.get("PROMISE_API_KEY", "") or str(settings["api_key"] or "")
    return settings


def local_zone() -> timezone:
    try:
        hours = float(load()["utc_offset_hours"])
    except (TypeError, ValueError):
        hours = 8
    return timezone(timedelta(hours=hours))

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path


PLUGIN_NAME = "astrbot_plugin_xiaoheihe_adapter"
SESSION_FILE_NAME = "session.json"

SAVED_LOGIN_KEYS = {
    "cookie",
    "heybox_id",
    "device_id",
    "user_agent",
    "api_params",
}


def session_file_path() -> Path:
    return Path(get_astrbot_plugin_data_path()) / PLUGIN_NAME / SESSION_FILE_NAME


def load_saved_session() -> dict[str, Any]:
    path = session_file_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def save_login_session(session: dict[str, Any]) -> dict[str, Any]:
    data = {
        key: value
        for key, value in session.items()
        if key in SAVED_LOGIN_KEYS or key in {"nickname", "avatar", "level"}
    }
    data["updated_at"] = int(time.time())

    path = session_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return data


def clear_saved_session() -> None:
    path = session_file_path()
    if path.exists():
        path.unlink()


def merge_saved_session_config(config: dict[str, Any]) -> dict[str, Any]:
    merged = dict(config or {})
    if not bool(merged.get("use_saved_login", True)):
        return merged

    saved = load_saved_session()
    if not saved.get("cookie"):
        return merged

    for key in SAVED_LOGIN_KEYS:
        value = saved.get(key)
        if value:
            merged[key] = value
    merged["_saved_login_applied"] = True
    return merged


def public_session_status(*, show_sensitive: bool = False) -> dict[str, Any]:
    data = load_saved_session()
    if not data:
        return {"has_session": False}

    cookie = str(data.get("cookie") or "")
    status = {
        "has_session": bool(cookie),
        "heybox_id": data.get("heybox_id") if show_sensitive else mask_value(data.get("heybox_id")),
        "nickname": data.get("nickname") or "",
        "avatar": data.get("avatar") or "",
        "device_id": data.get("device_id") if show_sensitive else mask_value(data.get("device_id")),
        "updated_at": data.get("updated_at"),
        "cookie_length": len(cookie),
    }
    if show_sensitive:
        status["cookie"] = cookie
    return status


def mask_value(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if len(text) <= 6:
        return "***"
    return f"{text[:3]}***{text[-3:]}"

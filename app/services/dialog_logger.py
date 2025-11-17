#app/services/dialog_logger.py
from __future__ import annotations

import asyncio
import re
import unicodedata

from pathlib import Path
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from app.config import settings


def _get_tz():
    try:
        return ZoneInfo(getattr(settings, "DEFAULT_TZ", "UTC") or "UTC")
    except Exception:
        return timezone.utc


def _now_str() -> str:
    return datetime.now(timezone.utc).astimezone(_get_tz()).strftime("%d.%m.%Y-%H:%M:%S")


def _sanitize(text: str | None) -> str:
    if text is None:
        return ""
    s = unicodedata.normalize("NFKC", str(text))
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    s = re.sub(r"\s+", " ", s.strip())
    return s


async def _write_line(user_id: int, line: str) -> None:
    try:
        base = Path(getattr(settings, "DIALOGS_DIR", "dialogs"))
    except Exception:
        base = Path("dialogs")
    try:
        base.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass

    path = base / f"{user_id}.txt"

    def _sync_write():
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")

    await asyncio.to_thread(_sync_write)


async def log_user_message(user_id: int, user_name: str, text: str) -> None:
    if not settings.ENABLE_DIALOG_LOGGING:
        return
    line = f"{_now_str()} [{_sanitize(user_name) or str(user_id)}] - {_sanitize(text)}"
    await _write_line(user_id, line)


async def log_bot_message(user_id: int, bot_name: str, text: str) -> None:
    if not settings.ENABLE_DIALOG_LOGGING:
        return
    line = f"{_now_str()} [{_sanitize(bot_name) or 'BOT'}] - {_sanitize(text)}"
    await _write_line(user_id, line)

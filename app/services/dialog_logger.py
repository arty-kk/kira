#app/services/dialog_logger.py
from __future__ import annotations

import asyncio
import logging
import re
import unicodedata

from pathlib import Path
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from app.config import settings


logger = logging.getLogger(__name__)
_WARNED_ISSUES: set[str] = set()


def _warn_once(issue_scope: str, reason: Exception, dialogs_dir: str) -> None:
    issue_key = f"{issue_scope}:{dialogs_dir}:{type(reason).__name__}:{reason}"
    if issue_key in _WARNED_ISSUES:
        return
    _WARNED_ISSUES.add(issue_key)
    logger.warning(
        "Dialog logging is unavailable for DIALOGS_DIR=%s: %s",
        dialogs_dir,
        reason,
    )


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
    dialogs_dir = "dialogs"
    try:
        dialogs_dir = getattr(settings, "DIALOGS_DIR", "dialogs")
        base = Path(dialogs_dir)
    except Exception:
        base = Path("dialogs")
    try:
        base.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        _warn_once("mkdir", exc, str(dialogs_dir))
        return

    path = base / f"{user_id}.txt"

    def _sync_write():
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")

    try:
        await asyncio.to_thread(_sync_write)
    except Exception as exc:
        _warn_once("write", exc, str(dialogs_dir))


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

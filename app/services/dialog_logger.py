#app/services/dialog_logger.py
from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import unicodedata

from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from app.config import settings


logger = logging.getLogger(__name__)
_WARNED_ISSUES: set[str] = set()

_DIALOG_LOG_QUEUE: asyncio.Queue[tuple[int, str] | None] | None = None
_DIALOG_LOG_WRITER_TASK: asyncio.Task[None] | None = None
_DIALOG_LOG_START_LOCK = asyncio.Lock()
_QUEUE_FULL_REASON = RuntimeError("dialog logger queue is full; dropping newest line")


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


def _dialogs_dir() -> str:
    try:
        return str(getattr(settings, "DIALOGS_DIR", "dialogs"))
    except Exception:
        return "dialogs"


def _queue_maxsize() -> int:
    try:
        value = int(getattr(settings, "DIALOG_LOGGER_QUEUE_MAXSIZE", 2000))
        return max(1, value)
    except Exception:
        return 2000


def _write_batch_sync(paths_to_lines: dict[Path, list[str]]) -> None:
    for path, lines in paths_to_lines.items():
        with open(path, "a", encoding="utf-8") as f:
            f.write("\n".join(lines))
            f.write("\n")


async def _flush_batch(batch: list[tuple[int, str]], dialogs_dir: str) -> None:
    if not batch:
        return

    base = Path(dialogs_dir)
    try:
        await asyncio.to_thread(base.mkdir, parents=True, exist_ok=True)
    except Exception as exc:
        _warn_once("mkdir", exc, dialogs_dir)
        return

    grouped: dict[Path, list[str]] = defaultdict(list)
    for user_id, line in batch:
        grouped[base / f"{user_id}.txt"].append(line)

    try:
        await asyncio.to_thread(_write_batch_sync, grouped)
    except Exception as exc:
        _warn_once("write", exc, dialogs_dir)


async def _dialog_writer_loop(queue: asyncio.Queue[tuple[int, str] | None]) -> None:
    dialogs_dir = _dialogs_dir()
    while True:
        item = await queue.get()
        processed_items = 1
        batch: list[tuple[int, str]] = []
        stop = item is None
        if item is not None:
            batch.append(item)

        while True:
            try:
                nxt = queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            processed_items += 1
            if nxt is None:
                stop = True
            else:
                batch.append(nxt)

        await _flush_batch(batch, dialogs_dir)

        for _ in range(processed_items):
            queue.task_done()
        if stop:
            return


async def _ensure_dialog_writer_started() -> None:
    global _DIALOG_LOG_QUEUE, _DIALOG_LOG_WRITER_TASK
    if _DIALOG_LOG_WRITER_TASK is not None and not _DIALOG_LOG_WRITER_TASK.done() and _DIALOG_LOG_QUEUE is not None:
        return
    async with _DIALOG_LOG_START_LOCK:
        if _DIALOG_LOG_WRITER_TASK is not None and not _DIALOG_LOG_WRITER_TASK.done() and _DIALOG_LOG_QUEUE is not None:
            return
        _DIALOG_LOG_QUEUE = asyncio.Queue(maxsize=_queue_maxsize())
        _DIALOG_LOG_WRITER_TASK = asyncio.create_task(_dialog_writer_loop(_DIALOG_LOG_QUEUE), name="dialog_logger_writer")


async def start_dialog_logger() -> None:
    if not getattr(settings, "ENABLE_DIALOG_LOGGING", False):
        return
    await _ensure_dialog_writer_started()


async def shutdown_dialog_logger() -> None:
    global _DIALOG_LOG_QUEUE, _DIALOG_LOG_WRITER_TASK

    writer_task = _DIALOG_LOG_WRITER_TASK
    queue = _DIALOG_LOG_QUEUE
    if writer_task is None or queue is None:
        _DIALOG_LOG_WRITER_TASK = None
        _DIALOG_LOG_QUEUE = None
        return

    try:
        if not writer_task.done():
            try:
                queue.put_nowait(None)
            except asyncio.QueueFull:
                with contextlib.suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
                    queue.task_done()
                try:
                    queue.put_nowait(None)
                except asyncio.QueueFull:
                    await queue.put(None)
                _warn_once("queue_full", _QUEUE_FULL_REASON, _dialogs_dir())
        with contextlib.suppress(asyncio.CancelledError):
            await writer_task
    finally:
        _DIALOG_LOG_WRITER_TASK = None
        _DIALOG_LOG_QUEUE = None


async def _enqueue_line(user_id: int, line: str) -> None:
    if not getattr(settings, "ENABLE_DIALOG_LOGGING", False):
        return

    await _ensure_dialog_writer_started()
    queue = _DIALOG_LOG_QUEUE
    if queue is None:
        return

    try:
        queue.put_nowait((user_id, line))
    except asyncio.QueueFull:
        _warn_once("queue_full", _QUEUE_FULL_REASON, _dialogs_dir())


async def log_user_message(user_id: int, user_name: str, text: str) -> None:
    if not settings.ENABLE_DIALOG_LOGGING:
        return
    line = f"{_now_str()} [{_sanitize(user_name) or str(user_id)}] - {_sanitize(text)}"
    await _enqueue_line(user_id, line)


async def log_bot_message(user_id: int, bot_name: str, text: str) -> None:
    if not settings.ENABLE_DIALOG_LOGGING:
        return
    line = f"{_now_str()} [{_sanitize(bot_name) or 'BOT'}] - {_sanitize(text)}"
    await _enqueue_line(user_id, line)

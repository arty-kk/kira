"""Celery entrypoint for launching group battles from bot handlers.

Use `battle_launch_task.delay(challenger_id, opponent_id, chat_id)` to enqueue a battle
start and execute the async service via `run_coro_sync(...)` inside worker process.
"""

from __future__ import annotations

import logging

from app.services.addons.group_battle import (
    launch_battle,
    check_battle_timeout,
    check_move_timeout,
)
from app.tasks.celery_app import celery, run_coro_sync


logger = logging.getLogger(__name__)

_METRICS_TIMEOUT_INVALID_PAYLOAD = "metrics:celery:battle:timeout_invalid_payload"


async def _metrics_incr_invalid_payload() -> None:
    try:
        from app.bot.components.constants import redis_client

        await redis_client.incr(_METRICS_TIMEOUT_INVALID_PAYLOAD)
    except Exception:
        logger.debug("battle timeout invalid payload metric write failed", exc_info=True)


def _truncate(value: object, limit: int = 64) -> str:
    text = str(value)
    return text if len(text) <= limit else f"{text[:limit]}..."


def _payload_field_types(payload: dict[str, object]) -> dict[str, str]:
    return {key: type(value).__name__ for key, value in payload.items()}


def _parse_timeout_payload(
    payload: dict[str, object],
    task_context: str,
) -> tuple[bool, str, str | None, int | None, dict[str, object]]:
    gid_raw = payload.get("gid")
    gid = str(gid_raw or "").strip()

    expected_phase_version_raw = payload.get("expected_phase_version")
    if expected_phase_version_raw is None:
        expected_phase_version_raw = payload.get("expected_version")

    safe_context: dict[str, object] = {
        "task": task_context,
        "gid_present": "gid" in payload,
        "gid_type": type(gid_raw).__name__,
        "gid_preview": _truncate(gid_raw) if gid_raw is not None else None,
        "expected_phase_version_raw": expected_phase_version_raw,
        "expected_phase_version_raw_type": type(expected_phase_version_raw).__name__,
        "expected_version_raw": payload.get("expected_version"),
        "expected_version_raw_type": type(payload.get("expected_version")).__name__,
        "payload_field_types": _payload_field_types(payload),
    }

    if not gid:
        return False, "gid is empty after normalization", gid, None, safe_context

    try:
        expected_phase_version = (
            int(expected_phase_version_raw) if expected_phase_version_raw is not None else None
        )
    except (TypeError, ValueError):
        return (
            False,
            "expected_phase_version is not int-convertible",
            gid,
            None,
            safe_context,
        )

    return True, "", gid, expected_phase_version, safe_context


@celery.task(name="battle.launch")
def battle_launch_task(challenger_id: str, opponent_id: str, chat_id: int) -> None:
    async def _run_task() -> None:
        await launch_battle(challenger_id, opponent_id, chat_id=chat_id)

    run_coro_sync(_run_task())


@celery.task(name="battle.start_timeout_check")
def battle_start_timeout_check_task(payload: dict[str, object]) -> None:
    async def _run_task() -> None:
        is_valid, reason, gid, expected_phase_version, safe_context = _parse_timeout_payload(
            payload,
            "battle.start_timeout_check",
        )
        if not is_valid:
            logger.warning("battle timeout invalid payload: %s; context=%s", reason, safe_context)
            await _metrics_incr_invalid_payload()
            return
        assert gid is not None
        await check_battle_timeout(gid, expected_phase_version=expected_phase_version)

    run_coro_sync(_run_task())


@celery.task(name="battle.move_timeout_check")
def battle_move_timeout_check_task(payload: dict[str, object]) -> None:
    async def _run_task() -> None:
        is_valid, reason, gid, expected_phase_version, safe_context = _parse_timeout_payload(
            payload,
            "battle.move_timeout_check",
        )
        if not is_valid:
            logger.warning("battle timeout invalid payload: %s; context=%s", reason, safe_context)
            await _metrics_incr_invalid_payload()
            return
        assert gid is not None
        await check_move_timeout(gid, expected_phase_version=expected_phase_version)

    run_coro_sync(_run_task())

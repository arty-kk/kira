from __future__ import annotations

"""Celery entrypoint for launching group battles from bot handlers.

Use `battle_launch_task.delay(challenger_id, opponent_id, chat_id)` to enqueue a battle
start and execute the async service via `_run(...)` inside worker process.
"""

from app.services.addons.group_battle import (
    launch_battle,
    check_battle_timeout,
    check_move_timeout,
)
from app.tasks.celery_app import celery, _run


@celery.task(name="battle.launch")
def battle_launch_task(challenger_id: str, opponent_id: str, chat_id: int) -> None:
    async def _run_task() -> None:
        await launch_battle(challenger_id, opponent_id, chat_id=chat_id)

    _run(_run_task())


@celery.task(name="battle.start_timeout_check")
def battle_start_timeout_check_task(payload: dict[str, object]) -> None:
    async def _run_task() -> None:
        gid = str(payload.get("gid") or "")
        expected_phase_version_raw = payload.get("expected_phase_version")
        if expected_phase_version_raw is None:
            expected_phase_version_raw = payload.get("expected_version")
        try:
            expected_phase_version = (
                int(expected_phase_version_raw) if expected_phase_version_raw is not None else None
            )
        except (TypeError, ValueError):
            return
        if not gid:
            return
        await check_battle_timeout(gid, expected_phase_version=expected_phase_version)

    _run(_run_task())


@celery.task(name="battle.move_timeout_check")
def battle_move_timeout_check_task(payload: dict[str, object]) -> None:
    async def _run_task() -> None:
        gid = str(payload.get("gid") or "")
        expected_phase_version_raw = payload.get("expected_phase_version")
        if expected_phase_version_raw is None:
            expected_phase_version_raw = payload.get("expected_version")
        try:
            expected_phase_version = (
                int(expected_phase_version_raw) if expected_phase_version_raw is not None else None
            )
        except (TypeError, ValueError):
            return
        if not gid:
            return
        await check_move_timeout(gid, expected_phase_version=expected_phase_version)

    _run(_run_task())

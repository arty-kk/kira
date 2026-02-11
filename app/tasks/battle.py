from __future__ import annotations

"""Celery entrypoint for launching group battles from bot handlers.

Use `battle_launch_task.delay(challenger_id, opponent_id, chat_id)` to enqueue a battle
start and execute the async service via `_run(...)` inside worker process.
"""

from app.services.addons.group_battle import launch_battle
from app.tasks.celery_app import celery, _run


@celery.task(name="battle.launch")
def battle_launch_task(challenger_id: str, opponent_id: str, chat_id: int) -> None:
    async def _run_task() -> None:
        await launch_battle(challenger_id, opponent_id, chat_id=chat_id)

    _run(_run_task())

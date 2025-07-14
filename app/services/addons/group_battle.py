cat >app/services/addons/group_battle.py<< EOF
#app/services/addons/group_battle.py
from __future__ import annotations

import asyncio
import logging
import random
import uuid

from datetime import datetime, timedelta, timezone
from typing import Coroutine, Any

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, CallbackQuery

from app.clients.telegram_client import get_bot
from app.config import settings
from app.core.memory import get_redis

logger = logging.getLogger(__name__)

bot = get_bot()

timedelta_ = timedelta
T_START = timedelta_(minutes=10)
T_MOVE = timedelta_(seconds=60)
SAFETY = timedelta_(seconds=60)
CHAT_ID = settings.ALLOWED_GROUP_ID

def create_task_safe(coro: Coroutine[Any, Any, Any]) -> asyncio.Task:

    task = asyncio.create_task(coro)
    logger.debug("Scheduled battle task %s", task)

    def _log_task(fut: asyncio.Future) -> None:
        try:
            fut.result()
        except Exception:
            logger.exception("Unhandled exception in battle task")

    task.add_done_callback(_log_task)
    return task


async def launch_battle(p1_id: str, p2_id: str) -> None:

    try:
        redis = get_redis()

        m1 = await bot.get_chat_member(CHAT_ID, int(p1_id))
        m2 = await bot.get_chat_member(CHAT_ID, int(p2_id))
        p1_name = m1.user.username or m1.user.full_name
        p2_name = m2.user.username or m2.user.full_name

        gid = str(uuid.uuid4())
        key = f"game:{gid}"
        started_ts = datetime.now(timezone.utc).isoformat()

        await redis.hset(
            key,
            mapping={
                "state": "CREATED",
                "ts": started_ts,
                "player1_id": p1_id,
                "player2_id": p2_id,
                "player1_name": p1_name,
                "player2_name": p2_name,
                "choice1": "",
                "choice2": "",
            },
        )

        ttl_start = int((T_START + SAFETY).total_seconds())
        await redis.set(f"active_game:{CHAT_ID}", gid, ex=ttl_start)
        await redis.expire(key, ttl_start)

        create_task_safe(_battle_start_timeout(gid))

        text = (
            f"⚔️ <b>Battle Time!</b> ⚔️\n"
            f"<a href='tg://user?id={p1_id}'>{p1_name}</a> vs "
            f"<a href='tg://user?id={p2_id}'>{p2_name}</a>\n\n"
            "Press <b>Start Battle!</b> to accept."
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="▶️ Start Battle!", callback_data=f"battle_start:{gid}",),
        ]])
        msg = await bot.send_message(CHAT_ID, text, parse_mode="HTML", reply_markup=kb)
        await redis.hset(key, mapping={"msg_id": msg.message_id})

        logger.info("New battle %s between %s and %s", gid, p1_name, p2_name)
    except Exception:
        logger.exception("Error in launch_battle")


async def start_battle_job() -> None:

    try:
        redis = get_redis()

        now = asyncio.get_event_loop().time()
        window = settings.ACTIVE_RECENT_SECONDS

        opt_out = await redis.smembers("battle:opt_out")
        members = await redis.zrangebyscore(f"user_last_ts:{CHAT_ID}", now - window, now)
        candidates = [uid for uid in members if uid not in opt_out]

        if await redis.get(f"active_game:{CHAT_ID}") or len(candidates) < 2:
            return

        p1, p2 = random.sample(candidates, 2)
        await launch_battle(p1, p2)
    except Exception:
        logger.exception("Error in start_battle_job")


async def _battle_start_timeout(gid: str) -> None:

    await asyncio.sleep(T_START.total_seconds())
    try:
        await check_battle_timeout(gid)
    except Exception:
        logger.exception("Error in battle start timeout for %s", gid)


async def check_battle_timeout(gid: str) -> None:

    try:
        redis = get_redis()

        key = f"game:{gid}"
        game = await redis.hgetall(key)
        if game.get("state") != "CREATED":
            return

        ready1 = await redis.get(f"ready:{gid}:{game['player1_id']}")
        ready2 = await redis.get(f"ready:{gid}:{game['player2_id']}")
        if not (ready1 and ready2):
            await bot.send_message(
                CHAT_ID,
                "❌ <b>Battle canceled</b>: someone didn’t accept in time.",
                parse_mode="HTML",
            )
            await _cleanup_data_only(gid)
            logger.info("Battle %s canceled by timeout", gid)
    except Exception:
        logger.exception("Error in check_battle_timeout")


async def _battle_move_timeout(gid: str) -> None:

    await asyncio.sleep((T_MOVE + SAFETY).total_seconds())
    try:

        redis = get_redis()

        game = await redis.hgetall(f"game:{gid}")
        if game.get("state") == "STARTED":
            await bot.send_message(
                CHAT_ID,
                "❌ <b>Battle canceled</b>: move not made in time.",
                parse_mode="HTML",
            )
            await _cleanup_data_only(gid)
    except Exception:
        logger.exception("Error in battle move timeout for %s", gid)


async def on_battle_start(query: CallbackQuery) -> None:

    try:
        redis = get_redis()

        await query.answer(cache_time=2)

        _, gid = query.data.split(":", 1)
        key = f"game:{gid}"
        game = await redis.hgetall(key)
        if game.get("state") != "CREATED":
            return

        uid = str(query.from_user.id)
        ready_key = f"ready:{gid}:{uid}"
        if await redis.get(ready_key):
            return

        await redis.set(ready_key, "1", ex=int((T_START + SAFETY).total_seconds()))

        ready1 = await redis.get(f"ready:{gid}:{game['player1_id']}")
        ready2 = await redis.get(f"ready:{gid}:{game['player2_id']}")
        if ready1 and ready2:
            async with redis.lock(f"lock:game_start:{gid}", timeout=5, blocking=False) as lock:
                if not lock:
                    logger.debug("Battle %s: already started", gid)
                    return
                new_ts = datetime.now(timezone.utc).isoformat()
                await redis.hset(key, mapping={"state": "STARTED", "ts": new_ts})

                ttl_move = int((T_MOVE + SAFETY).total_seconds())
                await redis.expire(key, ttl_move)
                await redis.expire(f"active_game:{CHAT_ID}", ttl_move)
                create_task_safe(_battle_move_timeout(gid))

                updated = await redis.hgetall(key)
                msg_id = int(updated["msg_id"])
                kb = InlineKeyboardMarkup(inline_keyboard=[[  
                    InlineKeyboardButton(text="🪨 Rock", callback_data=f"battle_move:{gid}:rock"),
                    InlineKeyboardButton(text="📄 Paper", callback_data=f"battle_move:{gid}:paper"),
                    InlineKeyboardButton(text="✂️ Scissors", callback_data=f"battle_move:{gid}:scissors"),
                ]])
                await bot.edit_message_text(
                    chat_id=CHAT_ID,
                    message_id=msg_id,
                    text=(
                        f"🏁 <b>Battle started!</b>\n"
                        f"<a href='tg://user?id={updated['player1_id']}'>{updated['player1_name']}</a> vs "
                        f"<a href='tg://user?id={updated['player2_id']}'>{updated['player2_name']}</a>\n\n"
                        f"Choose your move ({int(T_MOVE.total_seconds())}s):"
                    ),
                    parse_mode="HTML",
                    reply_markup=kb,
                )
                logger.info("Battle %s entered move phase", gid)
            return

        uname = query.from_user.username or query.from_user.full_name or uid
        opp_id = game['player2_id'] if uid == game['player1_id'] else game['player1_id']
        opp_name = game['player2_name'] if uid == game['player1_id'] else game['player1_name']
        await query.message.edit_text(
            f"✅ <a href='tg://user?id={uid}'>@{uname}</a> is ready!\n"
            f"▶️ <a href='tg://user?id={opp_id}'>{opp_name}</a>, press Start Battle to accept.",
            parse_mode="HTML",
            reply_markup=query.message.reply_markup,
        )
    except Exception:
        logger.exception("Error in on_battle_start")


async def on_battle_move(query: CallbackQuery) -> None:

    try:
        redis = get_redis()

        await query.answer(cache_time=2)

        _, gid, choice = query.data.split(":", 2)
        key = f"game:{gid}"
        game = await redis.hgetall(key)
        if game.get("state") != "STARTED":
            return

        uid = str(query.from_user.id)
        field = "choice1" if uid == game['player1_id'] else ("choice2" if uid == game['player2_id'] else None)
        if not field or game.get(field):
            return

        await redis.hset(key, field, choice)

        updated = await redis.hgetall(key)
        if updated.get("choice1") and updated.get("choice2"):
            async with redis.lock(f"lock:game:{gid}", timeout=5, blocking=False) as lock:
                if not lock:
                    return
                await conclude_game(gid)
            return

        ts_start = datetime.fromisoformat(game['ts'])
        elapsed = datetime.now(timezone.utc) - ts_start
        rem = max(0, int(T_MOVE.total_seconds() - elapsed.total_seconds()))
        msg_id = int(game['msg_id'])
        uname = query.from_user.username or query.from_user.full_name or uid
        wait_id = game['player2_id'] if field == "choice1" else game['player1_id']
        wait_name = game['player2_name'] if field == "choice1" else game['player1_name']
        await bot.edit_message_text(
            chat_id=CHAT_ID,
            message_id=msg_id,
            text=(
                f"✅ <a href='tg://user?id={uid}'>@{uname}</a> locked in a move.\n"
                f"⌛ Waiting for <a href='tg://user?id={wait_id}'>{wait_name}</a> ({rem}s left)…"
            ),
            parse_mode="HTML",
            reply_markup=query.message.reply_markup,
        )
    except Exception:
        logger.exception("Error in on_battle_move")


async def conclude_game(gid: str) -> None:

    try:
        redis = get_redis()

        key = f"game:{gid}"
        game = await redis.hgetall(key)
        c1, c2 = game['choice1'], game['choice2']

        if c1 == c2:
            result = (
                "🤝 <b>It's a tie!</b>\n"
                f"🔹 {game['player1_name']}: {c1.capitalize()}\n"
                f"🔸 {game['player2_name']}: {c2.capitalize()}"
            )
        else:
            win_map = {"rock": "scissors", "scissors": "paper", "paper": "rock"}
            winner = game['player1_name'] if win_map[c1] == c2 else game['player2_name']
            result = (
                f"🏆 <b>{winner} wins!</b>\n"
                f"🔹 {game['player1_name']}: {c1.capitalize()}\n"
                f"🔸 {game['player2_name']}: {c2.capitalize()}"
            )

        await bot.edit_message_text(
            chat_id=CHAT_ID,
            message_id=int(game['msg_id']),
            text=result,
            parse_mode="HTML",
        )
    except Exception:
        logger.exception("Error in conclude_game")
    finally:
        await _cleanup_data_only(gid)


async def _cleanup_data_only(gid: str) -> None:

    try:
        redis = get_redis()

        key = f"game:{gid}"
        game = await redis.hgetall(key)
        async with redis.pipeline() as pipe:
            pipe.delete(f"active_game:{CHAT_ID}")
            pipe.delete(key)
            for pid in (game.get("player1_id"), game.get("player2_id")):
                if pid:
                    pipe.delete(f"ready:{gid}:{pid}")
            await pipe.execute()
    except Exception:
        logger.exception("Error in cleanup_data_only")
EOF
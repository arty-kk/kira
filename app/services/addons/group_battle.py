#app/services/addons/group_battle.py
from __future__ import annotations

import asyncio
import logging
import random
import uuid
import time as _time
import json

from datetime import datetime, timedelta, timezone
from typing import Coroutine, Any
from html import escape

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, CallbackQuery
from aiogram.exceptions import TelegramBadRequest

from app.clients.telegram_client import get_bot
from app.config import settings
from app.core.memory import get_redis, _b2s
from app.bot.components.constants import BOT_ID as SELF_BOT_ID
from app.bot.utils.debouncer import buffer_message_for_response
from redis.exceptions import LockError

logger = logging.getLogger(__name__)

bot = get_bot()

timedelta_ = timedelta
T_START = timedelta_(minutes=10)
T_MOVE = timedelta_(seconds=60)
SAFETY = timedelta_(seconds=60)

def _default_chat_id() -> int | None:
    ids = [int(x) for x in (getattr(settings, "ALLOWED_GROUP_IDS", []) or []) if str(x).strip()]
    return ids[0] if ids else None

def _decode_hmap(d: dict | None) -> dict[str, str]:
    return { _b2s(k): _b2s(v) for k, v in (d or {}).items() }

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


async def launch_battle(p1_id: str, p2_id: str, chat_id: int | str | None = None) -> None:

    try:
        redis = get_redis()
        ttl_start = int((T_START + SAFETY).total_seconds())
        chat_id = chat_id or _default_chat_id()
        if chat_id is None:
            logger.warning("launch_battle: no ALLOWED_GROUP_IDS configured; skip")
            return
        chat_id = int(chat_id)

        gid = str(uuid.uuid4())
        key = f"game:{gid}"

        reserved = await redis.set(f"active_game:{chat_id}", gid, ex=ttl_start, nx=True)
        if not reserved:
            logger.info("launch_battle skipped in %s: active game already present", chat_id)
            try:
                await bot.send_message(chat_id, "⚠️ A battle is already in progress. Please wait.")
            except Exception:
                pass
            return

        try:
            m1 = await bot.get_chat_member(chat_id, int(p1_id))
            m2 = await bot.get_chat_member(chat_id, int(p2_id))
        except Exception:
            await redis.delete(f"active_game:{chat_id}")
            logger.info("launch_battle: participant not found in chat %s", chat_id)
            try:
                await bot.send_message(chat_id, "⚠️ Both participants must be in this chat.")
            except Exception:
                pass
            return

        def _disp(u): 
            return escape(f"@{u.username}" if u.username else (u.full_name or str(u.id)))
        p1_name = _disp(m1.user)
        p2_name = _disp(m2.user)

        started_ts = datetime.now(timezone.utc).isoformat()
        text = (
            f"⚔️ <b>Battle Time!</b> ⚔️\n"
            f"<a href='tg://user?id={p1_id}'>{p1_name}</a> vs "
            f"<a href='tg://user?id={p2_id}'>{p2_name}</a>\n\n"
            "Press <b>Start Battle!</b> to accept."
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="▶️ Start Battle!", callback_data=f"battle_start:{gid}"),
        ]])

        try:
            msg = await bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=kb)
        except Exception:
            try:
                await redis.delete(f"active_game:{chat_id}")
            except Exception:
                pass
            raise

        async with redis.pipeline(transaction=True) as pipe:
            pipe.hset(key, mapping={
                "state":          "CREATED",
                "ts":             started_ts,
                "chat_id":        str(chat_id),
                "player1_id":     p1_id,
                "player2_id":     p2_id,
                "player1_name":   p1_name,
                "player2_name":   p2_name,
                "choice1":        "",
                "choice2":        "",
                "msg_id":         msg.message_id,
            })
            pipe.expire(f"active_game:{chat_id}", ttl_start)
            pipe.expire(key, ttl_start)
            await pipe.execute()

        if str(SELF_BOT_ID) in (p1_id, p2_id):
            try:
                await redis.set(f"ready:{gid}:{SELF_BOT_ID}", "1", ex=ttl_start)
            except Exception:
                logger.exception("Failed to pre-mark bot ready for game %s", gid)

        create_task_safe(_battle_start_timeout(gid))
        logger.info("New battle %s between %s and %s", gid, p1_name, p2_name)
    except Exception:
        logger.exception("Error in launch_battle")


async def start_battle_job(chat_id: int | str | None = None) -> None:

    try:
        redis = get_redis()

        if chat_id is None:
            ids = [int(x) for x in (getattr(settings, "ALLOWED_GROUP_IDS", []) or []) if str(x).strip()]
            for cid in ids:
                try:
                    await start_battle_job(cid)
                except Exception:
                    logger.exception("Error in start_battle_job for chat %s", cid)
            return

        now = _time.time()
        window = settings.GROUP_PING_ACTIVE_RECENT_SECONDS
        chat_id = int(chat_id or (_default_chat_id() or 0))
        if chat_id == 0:
            logger.warning("start_battle_job: no ALLOWED_GROUP_IDS configured; skip")
            return

        opt_out_raw = await redis.smembers("battle:opt_out")
        opt_out = {_b2s(x) for x in (opt_out_raw or set())}
        members_raw = await redis.zrangebyscore(f"user_last_ts:{chat_id}", now - window, now)
        members = [_b2s(x) for x in (members_raw or [])]
        candidates = [uid for uid in members if uid and uid not in opt_out]

        if await redis.get(f"active_game:{chat_id}") or len(candidates) < 2:
            return

        p1, p2 = random.sample(candidates, 2)
        await launch_battle(p1, p2, chat_id=chat_id)
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
        game = _decode_hmap(await redis.hgetall(key))
        if not game:
            return
        if game.get("state") != "CREATED":
            return

        chat_id = game.get("chat_id") or _default_chat_id()
        if chat_id is None:
            return
        chat_id = int(chat_id)

        ready1 = await redis.get(f"ready:{gid}:{game['player1_id']}")
        ready2 = await redis.get(f"ready:{gid}:{game['player2_id']}")
        if not (ready1 and ready2):
            await bot.send_message(
                chat_id,
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

        key = f"game:{gid}"
        game = _decode_hmap(await redis.hgetall(key))
        if not game:
            return
        if game.get("state") == "STARTED":
            chat_id = game.get("chat_id") or _default_chat_id()
            if chat_id is None:
                return
            chat_id = int(chat_id)
            await bot.send_message(
                chat_id,
                "❌ <b>Battle canceled</b>: move not made in time.",
                parse_mode="HTML",
            )
            await _cleanup_data_only(gid)
    except Exception:
        logger.exception("Error in battle move timeout for %s", gid)


async def on_battle_start(query: CallbackQuery) -> None:

    try:
        redis = get_redis()

        try:
            await query.answer(cache_time=0)
        except TelegramBadRequest as e:
            msg = str(e).lower()
            if "query is too old" in msg or "query id is invalid" in msg:
                logger.debug("Ignoring stale/invalid callback query in on_battle_start: %s", e)
            else:
                logger.warning("Callback answer failed in on_battle_start: %s", e)

        _, gid = query.data.split(":", 1)
        key = f"game:{gid}"
        game = _decode_hmap(await redis.hgetall(key))
        if not game or game.get("state") != "CREATED":
            return
        
        chat_id = game.get("chat_id") or _default_chat_id()
        if chat_id is None:
            return
        chat_id = int(chat_id)

        uid = str(query.from_user.id)
        if uid not in (game.get("player1_id"), game.get("player2_id")):
            logger.debug("on_battle_start: user %s is not a participant of game %s", uid, gid)
            return

        ready_key = f"ready:{gid}:{uid}"
        if await redis.get(ready_key):
            return

        await redis.set(ready_key, "1", ex=int((T_START + SAFETY).total_seconds()))

        ready1 = await redis.get(f"ready:{gid}:{game['player1_id']}")
        ready2 = await redis.get(f"ready:{gid}:{game['player2_id']}")
        if ready1 and ready2:
            lock = redis.lock(f"lock:game_start:{gid}", timeout=5, blocking_timeout=0)
            acquired = await lock.acquire()
            if not acquired:
                logger.debug("Battle %s: already started by another task", gid)
                return
            try:
                ttl_move = int((T_MOVE + SAFETY).total_seconds())
                new_ts = datetime.now(timezone.utc).isoformat()
                async with redis.pipeline(transaction=True) as pipe:
                    pipe.hset(key, mapping={
                        "state": "STARTED",
                        "ts": new_ts,
                    })
                    pipe.expire(key, ttl_move)
                    pipe.expire(f"active_game:{chat_id}", ttl_move)
                    await pipe.execute()
                create_task_safe(_battle_move_timeout(gid))

                updated = _decode_hmap(await redis.hgetall(key))
                if not updated:
                    return
                msg_id = int(updated["msg_id"])
                kb = InlineKeyboardMarkup(inline_keyboard=[[  
                    InlineKeyboardButton(text="🪨 Rock", callback_data=f"battle_move:{gid}:rock"),
                    InlineKeyboardButton(text="📄 Paper", callback_data=f"battle_move:{gid}:paper"),
                    InlineKeyboardButton(text="✂️ Scissors", callback_data=f"battle_move:{gid}:scissors"),
                ]])
                try:
                    await bot.edit_message_text(
                        chat_id=chat_id,
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
                except TelegramBadRequest as e:
                    if "message is not modified" in str(e).lower():
                        logger.debug("Start-phase edit skipped: message not modified")
                    else:
                        raise
                logger.info("Battle %s entered move phase", gid)
                return
            finally:
                try:
                    await lock.release()
                except LockError:
                    logger.warning("Game-start lock for %s was not held", gid)


        raw_name = query.from_user.username or query.from_user.full_name or uid
        uname = f"@{raw_name}" if query.from_user.username else raw_name
        uname = escape(uname)
        opp_id = game['player2_id'] if uid == game['player1_id'] else game['player1_id']
        opp_name = escape(game['player2_name'] if uid == game['player1_id'] else game['player1_name'])
        try:
            await query.message.edit_text(
                f"✅ <a href='tg://user?id={uid}'>{uname}</a> is ready!\n"
                f"▶️ <a href='tg://user?id={opp_id}'>{opp_name}</a>, press Start Battle to accept.",
                parse_mode="HTML",
                reply_markup=query.message.reply_markup,
            )
        except TelegramBadRequest as e:
            if "message is not modified" in str(e).lower():
                logger.debug("Ready-phase edit skipped: message not modified")
            else:
                raise
    except Exception:
        logger.exception("Error in on_battle_start")


async def on_battle_move(query: CallbackQuery) -> None:

    try:
        redis = get_redis()

        try:
            await query.answer(cache_time=0)
        except TelegramBadRequest as e:
            msg = str(e).lower()
            if "query is too old" in msg or "query id is invalid" in msg:
                logger.debug("Ignoring stale/invalid callback query in on_battle_move: %s", e)
            else:
                logger.warning("Callback answer failed in on_battle_move: %s", e)

        _, gid, choice = query.data.split(":", 2)
        if choice not in {"rock", "paper", "scissors"}:
            return
            
        key = f"game:{gid}"
        game = _decode_hmap(await redis.hgetall(key))
        if not game or game.get("state") != "STARTED":
            return
        
        chat_id = game.get("chat_id") or _default_chat_id()
        if chat_id is None:
            return
        chat_id = int(chat_id)

        uid = str(query.from_user.id)
        field = "choice1" if uid == game['player1_id'] else ("choice2" if uid == game['player2_id'] else None)
        if not field or game.get(field):
            return

        await redis.hset(key, field, choice)

        updated = _decode_hmap(await redis.hgetall(key))
        bot_id_str = str(SELF_BOT_ID)
        bot_field = ("choice2" if updated.get("player2_id") == bot_id_str
                     else "choice1" if updated.get("player1_id") == bot_id_str
                     else None)
        if bot_field and not updated.get(bot_field):
            try:
                bot_choice = random.choice(("rock", "paper", "scissors"))
                await redis.hset(key, bot_field, bot_choice)
                updated = _decode_hmap(await redis.hgetall(key))
            except Exception:
                logger.exception("Failed to set bot move for game %s", gid)

        if updated.get("choice1") and updated.get("choice2"):
            lock = redis.lock(f"lock:game:{gid}", timeout=5, blocking_timeout=0)
            acquired = await lock.acquire()
            if not acquired:
                return
            try:
                await conclude_game(gid)
            finally:
                try:
                    await lock.release()
                except LockError:
                    logger.warning("Game-move lock for %s was not held", gid)
            return

        ts_start = datetime.fromisoformat(game['ts'])
        elapsed = datetime.now(timezone.utc) - ts_start
        rem = max(0, int(T_MOVE.total_seconds() - elapsed.total_seconds()))
        msg_id = int(game['msg_id'])

        raw_name = query.from_user.username or query.from_user.full_name or uid
        uname = f"@{raw_name}" if query.from_user.username else raw_name
        uname = escape(uname)
        wait_id = game['player2_id'] if field == "choice1" else game['player1_id']
        wait_name = escape(game['player2_name'] if field == "choice1" else game['player1_name'])
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=msg_id,
                text=(
                    f"✅ <a href='tg://user?id={uid}'>{uname}</a> locked in a move.\n"
                    f"⌛ Waiting for <a href='tg://user?id={wait_id}'>{wait_name}</a> ({rem}s left)…"
                ),
                parse_mode="HTML",
                reply_markup=query.message.reply_markup,
            )
        except TelegramBadRequest as e:
            if "message is not modified" in str(e).lower():
                logger.debug("Move-phase edit skipped: message not modified")
            else:
                raise
    except Exception:
        logger.exception("Error in on_battle_move")


async def conclude_game(gid: str) -> None:

    try:
        redis = get_redis()

        key = f"game:{gid}"
        game = _decode_hmap(await redis.hgetall(key))

        c1, c2 = game['choice1'], game['choice2']
        win_map = {"rock": "scissors", "scissors": "paper", "paper": "rock"}

        if c1 == c2:
            outcome = "tie"
            winner_id = None
            result = (
                "🤝 <b>It's a tie!</b>\n"
                f"🔹 {game['player1_name']}: {c1.capitalize()}\n"
                f"🔸 {game['player2_name']}: {c2.capitalize()}"
            )
        else:
            p1_wins = (win_map[c1] == c2)
            outcome = "p1" if p1_wins else "p2"
            winner_id = game['player1_id'] if p1_wins else game['player2_id']
            winner = game['player1_name'] if p1_wins else game['player2_name']
            result = (
                f"🏆 <b>{winner} wins!</b>\n"
                f"🔹 {game['player1_name']}: {c1.capitalize()}\n"
                f"🔸 {game['player2_name']}: {c2.capitalize()}"
            )

        chat_id = game.get("chat_id") or _default_chat_id()
        if chat_id is None:
            return
        chat_id = int(chat_id)

        bot_id_str = str(SELF_BOT_ID)
        bot_participates = bot_id_str in (game.get("player1_id"), game.get("player2_id"))
        bot_outcome = "none"
        if bot_participates:
            if c1 == c2:
                bot_outcome = "tie"
            else:
                p1_wins = (win_map[c1] == c2)
                if game.get("player1_id") == bot_id_str:
                    bot_outcome = "win" if p1_wins else "loss"
                else:
                    bot_outcome = "loss" if p1_wins else "win"

            human_name = game['player2_name'] if game['player1_id'] == bot_id_str else game['player1_name']
            bot_name   = game['player1_name'] if game['player1_id'] == bot_id_str else game['player2_name']
            result += f"\n\n<b>{bot_name}</b>: {bot_outcome.upper()} vs {human_name}"

            try:
                r = get_redis()
                pipe = r.pipeline()
                pipe.hincrby(f"battle:bot_stats:{chat_id}", bot_outcome, 1)
                human_id = game['player2_id'] if game['player1_id'] == bot_id_str else game['player1_id']
                pipe.hincrby(f"battle:bot_vs:{chat_id}:{human_id}", bot_outcome, 1)
                await pipe.execute()
            except Exception:
                logger.exception("Failed to store bot outcome stats for game %s", gid)

        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=int(game['msg_id']),
            text=result,
            parse_mode="HTML",
        )
        try:
            ctx = {
                "type": "battle_result",
                "gid": gid,
                "chat_id": chat_id,
                "player1": {
                    "id": game["player1_id"],
                    "name": game["player1_name"],
                    "choice": c1,
                },
                "player2": {
                    "id": game["player2_id"],
                    "name": game["player2_name"],
                    "choice": c2,
                },
                "outcome": outcome,            # "p1" | "p2" | "tie"
                "winner_id": winner_id,        # None при ничьей
                "bot_outcome": bot_outcome,    # "win" | "loss" | "tie" | "none"
                "ts": datetime.now(timezone.utc).isoformat(),
            }
            payload = {
                "chat_id": chat_id,
                "text": "[battle_result] " + json.dumps(ctx, ensure_ascii=False),
                "user_id": SELF_BOT_ID,
                "reply_to": int(game["msg_id"]),
                "is_group": True,
                "msg_id": int(game["msg_id"]),
                "is_channel_post": False,
                "channel_id": None,
                "channel_title": None,
                "trigger": "system_battle_result",
            }
            buffer_message_for_response(payload)
        except Exception:
            logger.exception("Failed to push battle result into responder buffer")
    except Exception:
        logger.exception("Error in conclude_game")
    finally:
        await _cleanup_data_only(gid)


async def _cleanup_data_only(gid: str) -> None:

    try:
        redis = get_redis()

        key = f"game:{gid}"
        game = _decode_hmap(await redis.hgetall(key))
        async with redis.pipeline(transaction=True) as pipe:
            try:
                chat_id = int((game.get("chat_id") if game else None) or (_default_chat_id() or 0))
                if chat_id == 0:
                    await pipe.execute()
                    return
                cur = _b2s(await redis.get(f"active_game:{chat_id}"))
                if cur == gid:
                    pipe.delete(f"active_game:{chat_id}")
            except Exception:
                pass
            pipe.delete(key)
            for pid in (game.get("player1_id"), game.get("player2_id")):
                if pid:
                    pipe.delete(f"ready:{gid}:{pid}")
            await pipe.execute()
    except Exception:
        logger.exception("Error in cleanup_data_only")
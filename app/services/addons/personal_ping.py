#app/services/addons/personal_ping.py
import logging
import statistics
import re
import unicodedata
import time as time_module
import asyncio
import math
import random
import json

from dataclasses import dataclass
from typing import Optional
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter, TelegramBadRequest
from datetime import datetime, timedelta, timezone
from redis.exceptions import RedisError
from zoneinfo import ZoneInfo

from app.core.db import session_scope
from app.core.models import User
from app.core.memory import get_redis, load_context, push_message, get_cached_gender, delete_user_redis_data
from app.clients.openai_client import _call_openai_with_retry, _msg, _get_output_text
from app.clients.telegram_client import get_bot
from app.config import settings
from app.prompts_base import (
    PERSONAL_PING_CONTEXT_CLASSIFIER_SYSTEM_TEMPLATE,
    PERSONAL_PING_CONTEXT_CLASSIFIER_USER_TEMPLATE,
    PERSONAL_PING_CARE_RULE,
    PERSONAL_PING_CTX_TEMPLATE,
    PERSONAL_PING_LANGUAGE_RULE_FROM_HISTORY,
    PERSONAL_PING_LANGUAGE_RULE_WITH_EXEMPLAR_TEMPLATE,
    PERSONAL_PING_ANCHOR_LINE_TEMPLATE,
    PERSONAL_PING_Q_RULE_ALLOW,
    PERSONAL_PING_Q_RULE_NO,
    PERSONAL_PING_RULES_COMMON_TEMPLATE,
    PERSONAL_PING_SIGNAL_CLASSIFIER_SYSTEM_PROMPT,
    PERSONAL_PING_SIGNAL_CLASSIFIER_USER_TEMPLATE,
)
from app.emo_engine import get_persona
from app.services.responder.prompt_builder import build_system_prompt
from app.services.addons.voice_generator import (
    maybe_tts_and_send, is_tts_eligible_short
)
from app.services.addons.analytics import record_ping_sent

logger = logging.getLogger(__name__)

LAST_PRIVATE_TS_KEY = "last_private_ts:{}"
IDLE_LIST_KEY = "private_idle_list:{}"
PING_SCHEDULE_KEY = "personal_ping_schedule"
PING_SCHEDULE_INFLIGHT = "personal_ping_schedule_inflight"
PING_STREAK_KEY = "personal_ping_streak:{}"
PENDING_PING_KEY = "pending_ping:{}"
ARM_STATS_KEY    = "ping_arm_stats:{}"
HOD_HIST_KEY     = "private_hod_hist:{}"
REANIMATE_LAST_TS_KEY = "personal_reanimate_last_ts:{}"
ENROLLED_KEY     = "personal_enrolled:{}"

ARMS = ("callback", "question", "suggestion", "checkin")

MAX_CONSECUTIVE_PINGS = getattr(settings, "PERSONAL_PING_MAX_CONSECUTIVE", 3)
PERSONAL_WINDOW = 10

MAX_TEMPERATURE = 0.8
MIN_TEMPERATURE = 0.6
TOP_P_MIN = 0.8
TOP_P_MAX = 1.0

DEFAULT_MODS = {
    "creativity_mod": 0.5,
    "sarcasm_mod":    0.0,
    "enthusiasm_mod": 0.5,
    "technical_mod":  0.0,
    "confidence_mod": 0.5,
    "precision_mod":  0.5,
    "fatigue_mod":    0.0,
    "stress_mod":     0.0,
    "curiosity_mod":  0.5,
    "valence_mod":    0.0,
}

_SUCCESS_WINDOW = getattr(settings, "PERSONAL_PING_SUCCESS_WINDOW_SECONDS", 6 * 3600)
_ACTIVE_HOURS_TOPK = getattr(settings, "PERSONAL_PING_ACTIVE_HOURS_TOPK", 3)
_BANDIT_EPSILON = getattr(settings, "PERSONAL_PING_BANDIT_EPSILON", 0.03)
_FORCE_CALLBACK_ON_OPEN = getattr(settings, "PERSONAL_PING_FORCE_CALLBACK_ON_OPEN_LOOP", True)
_NEGATIVE_COOLDOWN = getattr(settings, "PERSONAL_PING_NEGATIVE_COOLDOWN_SECONDS", 12 * 3600)

_REANIMATE_IDLE_HOURS     = getattr(settings, "PERSONAL_PING_REANIMATE_IDLE_HOURS", 72)
_REANIMATE_MIN_GAP_HOURS  = getattr(settings, "PERSONAL_PING_REANIMATE_MIN_GAP_HOURS", 168)

_CLAIM_DUE_SHA: str | None = None
_REQUEUE_INFLIGHT_SHA: str | None = None
_BACKOFF_MULT = getattr(settings, "PERSONAL_PING_BACKOFF_MULT", 3.0)
_BACKOFF_MAX_HOURS = getattr(settings, "PERSONAL_PING_BACKOFF_MAX_HOURS", 48)
_BACKOFF_JITTER_PCT = getattr(settings, "PERSONAL_PING_BACKOFF_JITTER_PCT", 0.10)

_RANDOM_MIN_HOURS = getattr(settings, "PERSONAL_PING_RANDOM_MIN_HOURS", 2)
_RANDOM_MAX_HOURS = getattr(settings, "PERSONAL_PING_RANDOM_MAX_HOURS", 8)

_ARM_STATS_TTL = getattr(settings, "PERSONAL_PING_ARM_STATS_TTL_SECONDS", 30 * 24 * 3600)
_PING_INFLIGHT_LEASE_SECONDS = getattr(settings, "PERSONAL_PING_INFLIGHT_LEASE_SECONDS", 120)

MOTIVES: dict[str, str] = {
    "missed_you": "You genuinely missed the user; a mild natural warmth.",
    "topic_interest": "A specific past topic kept circling in your mind; mention it.",
    "unfinished_thread": "There is a clear unresolved item to pick up.",
    "light_care": "Gentle, low-pressure check-in because you enjoyed the last exchange.",
    # casual / boredom hello
    "bored_hello": "You just felt like saying hi; keep it very short and friendly, no specific topic.",
    # Care / support oriented
    "care_sick": "User was ill/recovering; a considerate, human health check-in.",
    "care_low_mood": "User felt down/sad; a gentle emotional check-in.",
    "care_stress": "User was under pressure/stress; supportive vibe, no push.",
    "care_exhaustion": "User was tired/burned out; soft energy-aware nudge.",
    "care_anxiety": "User mentioned anxiety/panic; calm, grounded tone.",
    "care_grief": "User faced loss/grief; tender presence, no demands.",
    # Momentum / progress
    "progress_small_win": "User hinted a small win; acknowledge and invite a tiny follow-up.",
    "progress_blocker": "User had a blocker; offer a tiny unblock.",
    "progress_habit": "User tracked a habit; nudge with one micro-step.",
    "progress_fitness": "User mentioned workouts/health routine; small supportive nudge.",
    "progress_creative": "User’s side project/creative block; a spark to continue.",
    # Life logistics
    "life_busy": "User was swamped/busy; low-demand check-in.",
    "life_relocation": "User moving/relocating; short caring ping.",
    "life_family_event": "Family event/visit; considerate follow-up.",
    "life_home_renovation": "Home/repair hassle; empathetic nudge.",
    "life_finances": "Budget/finance planning; calm small step.",
    # Temporal anchors
    "time_season_change": "Season/weather shift referenced; human small talk anchor.",
    "time_weekend_plans": "Past/near weekend plans; natural follow-through.",
    "time_holiday": "Holiday/celebration context; warm tailored ping.",
    "time_exam_deadline": "Exam/deadline aftermath; recovery check-in.",
    # Interests / media
    "media_movie_book": "Movie/book/podcast stuck with you; share a thought.",
    "music_concert": "Concert/music thread; quick personal hook.",
    "sports_event": "Team/game mention; short natural touch.",
    "hobby_project": "Hobby/tool tinkering; tiny suggestion or delight.",
    "gadget_purchase": "New device/gadget; small curiosity ping.",
    "recipe_try": "Cooking/recipe attempt; small check-in.",
    # Social / errands
    "friends_meet": "Meetup/coffee that was planned/mentioned; soft follow-up.",
    "travel_trip": "Trip/travel before/after; natural check-in.",
    "return_after_break": "User returned after pause; warm, non-pushy nudge.",
    # Work/tech
    "work_release": "Release/shipping mention; post-release check-in.",
    "work_bug": "Bug/issue that bothered them; one-liner idea.",
    "work_brainstorm": "We brainstormed; one concise extra thought.",
    # Misc grounded anchors
    "weather_extreme": "Extreme weather nearby; short human concern.",
    "pet_update": "Pet anecdotes; light, kind follow-up.",
    "quitting_bad_habit": "Quitting habit; tiny supportive nudge.",
    # Light-weight additions (safe/low-risk):
    "career_growth": "Career skill/growth thread; suggest a tiny step.",
    "study_session": "Learning plan/session; invite one short action.",
    "morning_energy": "Morning vibe/energy; offer a tiny launch step.",
    "evening_unwind": "Evening wrap-up; soft reflection or tiny prep.",
    "week_ahead_plan": "Plan the week; prompt a tiny intention.",
}

CARE_MOTIVES = {
    "care_sick", "care_low_mood", "care_stress", "care_exhaustion", "care_anxiety", "care_grief",
    "time_exam_deadline", "return_after_break", "life_busy"
}

_TTS_PING_ENABLED = bool(getattr(settings, "PERSONAL_PING_TTS_ENABLED", True))
_TTS_VOICE_START_H = getattr(settings, "PERSONAL_PING_TTS_START_HOUR", 10)
_TTS_VOICE_END_H   = getattr(settings, "PERSONAL_PING_TTS_END_HOUR",   21)
_TTS_PING_PROB = float(getattr(settings, "PERSONAL_PING_TTS_PROBABILITY",
                               getattr(settings, "TTS_PROBABILITY_TEXT", 0.15)))
_TTS_PING_CAPTION_ENABLED = bool(getattr(settings, "PERSONAL_PING_TTS_CAPTION_ENABLED",
                                         getattr(settings, "TTS_VOICE_CAPTION_ENABLED", False)))
_TTS_PING_CAPTION_LEN = int(getattr(settings, "PERSONAL_PING_TTS_CAPTION_LEN",
                                    getattr(settings, "TTS_VOICE_CAPTION_LEN", 160)))

_ALLOW_GENERIC_WHEN_BORED = bool(getattr(settings, "PERSONAL_PING_ALLOW_GENERIC_WHEN_BORED", True))
_GENERIC_HELLO_PROB = float(getattr(settings, "PERSONAL_PING_GENERIC_HELLO_PROB", 0.25))
_GENERIC_HELLO_ASK_PROB = float(getattr(settings, "PERSONAL_PING_GENERIC_HELLO_ASK_PROB", 0.5))

def merge_and_clamp_mods(style_mods: dict | None) -> dict:
    mods = DEFAULT_MODS.copy()
    if not isinstance(style_mods, dict):
        return mods
    for k in mods.keys():
        try:
            if k == "valence_mod":
                x = float(style_mods.get("valence_mod", style_mods.get("valence", mods[k])))
                mods[k] = max(-1.0, min(1.0, x))
            else:
                x = float(style_mods.get(k, mods[k]))
                mods[k] = max(0.0, min(1.0, x))
        except Exception:
            pass
    return mods

def can_send_tts(user_id, pref, chat_voice_disabled, voice_window_ok, cb_disabled, p_dyn, voice_bias):
    if pref == "never":
        return False
    if pref == "always":
        return (not chat_voice_disabled) and voice_window_ok and (not cb_disabled)
    if voice_bias is not None and voice_bias >= 0.08:
        return (not chat_voice_disabled) and voice_window_ok and (not cb_disabled) and (random.random() < min(0.9, p_dyn + 0.2))
    return (not chat_voice_disabled) and voice_window_ok and (not cb_disabled) and (random.random() < p_dyn)

@dataclass
class EmotionContext:
    motive: Optional[str] = None
    care_needed: bool = False
    care_reason: Optional[str] = None
    anchor: Optional[str] = None 

def _ping_get_default_tz() -> timezone:
    try:
        name = getattr(settings, "DEFAULT_TZ", "UTC") or "UTC"
        return ZoneInfo(name)
    except Exception:
        return timezone.utc

def _ping_tz_name() -> str:
    try:
        return getattr(settings, "DEFAULT_TZ", "UTC") or "UTC"
    except Exception:
        return "UTC"

def _build_ping_time_hint() -> str:

    try:
        dt_now = datetime.now(_ping_get_default_tz())
        now_local = dt_now.strftime("%d %b %Y, %H:%M")
        weekday = (dt_now.strftime("%a") or "").strip()
        tz_abbr = (dt_now.strftime("%Z") or "").strip()
        tz_off = (dt_now.strftime("%z") or "").strip()
        if len(tz_off) == 5:
            tz_off = tz_off[:3] + ":" + tz_off[3:]
        tz_name = tz_abbr or _ping_tz_name()
        tz_utc = f"UTC{tz_off}" if tz_off else ""
    except Exception:
        weekday = ""
        now_local = datetime.utcnow().strftime("%Y-%m-%d %H:%M")
        tz_name = _ping_tz_name()
        tz_utc = ""

    w = f"{weekday}, " if weekday else ""
    utc_part = f" ({tz_utc})" if tz_utc else ""
    return (
        "TIME\n"
        f"- Now (assistant local): {w}{now_local} {tz_name}{utc_part}.\n"
        "- Use this to resolve relative time (now/today/yesterday/weekdays/durations/future/past).\n"
        "- Avoid implying ongoing periods are completed unless time clearly indicates that.\n"
    )

async def user_zoneinfo(user_id: int) -> ZoneInfo:

    try:
        raw = await get_redis().get(f"tz:{user_id}")
        if raw:
            return ZoneInfo((raw.decode() if isinstance(raw, (bytes, bytearray)) else str(raw)).strip())
    except Exception:
        pass

    try:
        async with session_scope(stmt_timeout_ms=1500, read_only=True) as db:
            u = await db.get(User, int(user_id))
            if u and getattr(u, "tz", None):
                return ZoneInfo(u.tz)
    except Exception:
        pass

    try:
        return ZoneInfo(getattr(settings, "DEFAULT_TZ", "UTC"))
    except Exception:
        return ZoneInfo("UTC")

def _build_transcript(
    personal_msgs: list[dict],
    summary: Optional[str],
    *,
    user_label: str,
    assistant_label: str,
    empty_fallback: str,
    normalize_newlines: bool = True,
) -> str:
    blocks = []
    if summary:
        blocks.append(f"Summary: {summary}")
    for m in (personal_msgs or [])[-20:]:
        author = user_label if (m.get("user_id") and m.get("role") != "assistant") else assistant_label
        text = m.get("content") or ""
        if normalize_newlines:
            text = str(text).replace("\n", " ").strip()
            if not text:
                continue
        else:
            text = str(text)
        blocks.append(f"{author}: {text}")
    return "\n".join(blocks) if blocks else empty_fallback

async def classify_emotion_context_llm(personal_msgs: list[dict], summary: Optional[str]) -> EmotionContext:
    transcript = _build_transcript(
        personal_msgs,
        summary,
        user_label="User",
        assistant_label="Assistant",
        empty_fallback="(no messages)",
    )

    taxo = ",".join(sorted(MOTIVES.keys()))
    system_msg = PERSONAL_PING_CONTEXT_CLASSIFIER_SYSTEM_TEMPLATE.format(taxo=taxo)
    user_msg = PERSONAL_PING_CONTEXT_CLASSIFIER_USER_TEMPLATE.format(transcript=transcript)
    try:
        resp = await asyncio.wait_for(
            _call_openai_with_retry(
                endpoint="responses.create",
                model=settings.BASE_MODEL,
                input=[_msg("system", system_msg), _msg("user", user_msg)],
                max_output_tokens=64,
                temperature=0.0,
                top_p=1.0,
            ),
            timeout=settings.BASE_MODEL_TIMEOUT,
        )
        out = (_get_output_text(resp) or "").strip()
        data = json.loads(out)
        ec = EmotionContext(
            motive = (data.get("motive") or None),
            care_needed = bool(data.get("care_needed", False)),
            care_reason = (data.get("care_reason") or None),
            anchor = (data.get("anchor") or None)
        )
        if ec.motive and ec.motive not in MOTIVES:
            ec.motive = None
        return ec
    except Exception:
        logger.debug("emotion classifier failed; fallback to neutral", exc_info=True)
        return EmotionContext()

async def send_private_with_retry(user_id: int, text: str) -> int | None:
    bot = get_bot()
    attempt = 1
    logger.info("PM send: user=%s len=%d", user_id, len(text) if text else 0)
    while True:
        try:
            await asyncio.sleep(random.uniform(0.0, 0.2))
            msg = await bot.send_message(user_id, text, parse_mode=None)
            return int(msg.message_id)
        except TelegramForbiddenError:
            raise
        except TelegramBadRequest as e:
            em = str(e).lower()
            hard = any(s in em for s in (
              "chat not found", "chat_id_invalid", "can't access the chat",
              "peer_id_invalid", "user is deactivated", "user_deactivated"
            ))
            if hard:
                raise RuntimeError("HARD_BADREQUEST:" + str(e))
            if attempt >= 3:
                logger.exception("PM send bad request for %s after %d attempts: %s", user_id, attempt, e)
                return None
            await asyncio.sleep(1.5 * attempt)
            attempt += 1
        except TelegramRetryAfter as e:
            delay = max(1, int(getattr(e, "retry_after", 5)))
            logger.warning("RetryAfter %ss on PM to %s (attempt %d)", delay, user_id, attempt)
            await asyncio.sleep(delay)
            attempt += 1
        except Exception as e:
            if attempt >= 3:
                logger.exception("PM send failed for %s after %d attempts: %s", user_id, attempt, e)
                return None
            await asyncio.sleep(1.5 * attempt)
            attempt += 1

async def bandit_get_stats(user_id: int) -> dict:

    redis = get_redis()
    res = {}
    try:
        data = await redis.hgetall(ARM_STATS_KEY.format(user_id))
    except RedisError:
        data = {}

    sdata = {}
    for k, v in (data or {}).items():
        ks = k.decode() if isinstance(k, (bytes, bytearray)) else str(k)
        sdata[ks] = v
    for arm in ARMS:
        a_sum, b_sum = 0, 0
        for ks, v in sdata.items():
            try:
                vv = int(v.decode() if isinstance(v, (bytes, bytearray)) else v)
            except Exception:
                continue
            if ks == f"a:{arm}" or ks.startswith(f"a:{arm}:"):
                a_sum += vv
            elif ks == f"b:{arm}" or ks.startswith(f"b:{arm}:"):
                b_sum += vv
        a = max(a_sum, 1)
        b = max(b_sum, 1)
        res[arm] = (a, b)
    return res

async def bandit_get_stats_ctx(user_id: int, ctx: Optional[str]) -> dict:

    if not ctx:
        return {}
    redis = get_redis()
    try:
        data = await redis.hgetall(ARM_STATS_KEY.format(user_id))
    except RedisError:
        data = {}
    res = {}
    for arm in ARMS:
        a_sum, b_sum = 0, 0
        for k, v in (data or {}).items():
            try:
                ks = k.decode() if isinstance(k, (bytes, bytearray)) else str(k)
                vv = int(v.decode() if isinstance(v, (bytes, bytearray)) else v)
            except Exception:
                continue
            if ks == f"a:{arm}:{ctx}":
                a_sum += vv
            elif ks == f"b:{arm}:{ctx}":
                b_sum += vv
        res[arm] = (a_sum, b_sum)
        for suf in ("voice", "text"):
            a_s = b_s = 0
            for k, v in (data or {}).items():
                try:
                    ks = k.decode() if isinstance(k, (bytes, bytearray)) else str(k)
                    vv = int(v.decode() if isinstance(v, (bytes, bytearray)) else v)
                except Exception:
                    continue
                if ks == f"a:{arm}:{suf}:{ctx}":
                    a_s += vv
                elif ks == f"b:{arm}:{suf}:{ctx}":
                    b_s += vv
            res[f"{arm}:{suf}"] = (a_s, b_s)
    return res

async def bandit_update(user_id: int, arm: str, success: bool, ctx: Optional[str] = None) -> None:
    base = (arm or "").split(":", 1)[0]
    if base not in ARMS:
        return
    redis = get_redis()
    key = ARM_STATS_KEY.format(user_id)
    field_global = f"a:{arm}" if success else f"b:{arm}"
    field_ctx    = (f"a:{arm}:{ctx}" if success else f"b:{arm}:{ctx}") if ctx else None
    try:
        async with redis.pipeline(transaction=True) as pipe:
            pipe.hincrby(key, field_global, 1)
            if field_ctx:
                pipe.hincrby(key, field_ctx, 1)
            if _ARM_STATS_TTL and int(_ARM_STATS_TTL) > 0:
                pipe.expire(key, int(_ARM_STATS_TTL))
            else:
                try:
                    pipe.persist(key)
                except Exception:
                    pass
            await pipe.execute()
    except RedisError:
        logger.debug("bandit_update failed for %s", user_id, exc_info=True)

def _posterior_theta(a: int, b: int) -> float:
    return random.betavariate(max(a,1), max(b,1))

async def bandit_choose_arm(user_id: int, ctx: Optional[str] = None) -> str:

    if random.random() < float(_BANDIT_EPSILON):
        return random.choice(ARMS)
    stats_global = await bandit_get_stats(user_id)
    stats_ctx    = await bandit_get_stats_ctx(user_id, ctx)
    best = None
    best_theta = -1.0
    for arm in ARMS:
        try:
            a_ctx, b_ctx = stats_ctx.get(arm, (0,0)) if stats_ctx else (0,0)
            if not isinstance(a_ctx, int) or not isinstance(b_ctx, int):
                a_ctx = b_ctx = 0
        except Exception:
            a_ctx = b_ctx = 0
        if (a_ctx + b_ctx) > 0:
            theta = _posterior_theta(a_ctx, b_ctx)
        else:
            a, b = stats_global.get(arm, (1,1))
            if not isinstance(a, int) or not isinstance(b, int):
                a = b = 1
            theta = _posterior_theta(a, b)
        if theta > best_theta:
            best_theta = theta
            best = arm
    return best or "checkin"

async def bandit_mark_pending(user_id: int, ts: int, arm: str, ctx: Optional[str] = None) -> None:
    redis = get_redis()
    try:
        async with redis.pipeline(transaction=True) as pipe:
            mapping = {"ts": int(ts), "arm": arm}
            if ctx:
                mapping["ctx"] = ctx
            pipe.hset(PENDING_PING_KEY.format(user_id), mapping=mapping)
            pipe.expire(PENDING_PING_KEY.format(user_id), settings.PERSONAL_PING_RETENTION_SECONDS)
            await pipe.execute()
    except RedisError:
        logger.debug("mark_pending failed", exc_info=True)

async def bandit_check_expire_or_success(user_id: int, now_ts: float) -> None:

    redis = get_redis()
    try:
        pending = await redis.hgetall(PENDING_PING_KEY.format(user_id))
        if not pending:
            return
            
        def _hget_local(d: dict, k: str):
            return d.get(k, d.get(k.encode()))
        raw_ts = _hget_local(pending, "ts")
        raw_arm = _hget_local(pending, "arm")
        raw_ctx = _hget_local(pending, "ctx")
        arm = raw_arm.decode() if isinstance(raw_arm, (bytes, bytearray)) else raw_arm
        ctx = raw_ctx.decode() if isinstance(raw_ctx, (bytes, bytearray)) else raw_ctx
        p_ts = int(raw_ts) if raw_ts else 0

        if not p_ts or not arm:
            await redis.delete(PENDING_PING_KEY.format(user_id))
            return
        if now_ts > p_ts + _SUCCESS_WINDOW:
            await bandit_update(user_id, arm, success=False, ctx=ctx)
            await redis.delete(PENDING_PING_KEY.format(user_id))
    except RedisError:
        logger.debug("bandit_check_expire failed", exc_info=True)

async def update_hod_hist(user_id: int, ts: float) -> None:
    redis = get_redis()
    tz = await user_zoneinfo(user_id)
    h = int(datetime.fromtimestamp(ts, tz).hour)
    try:
        async with redis.pipeline(transaction=True) as pipe:
            pipe.hincrby(HOD_HIST_KEY.format(user_id), str(h), 1)
            pipe.expire(HOD_HIST_KEY.format(user_id), settings.PERSONAL_PING_RETENTION_SECONDS)
            await pipe.execute()
    except RedisError:
        logger.debug("update_hod_hist failed", exc_info=True)

async def top_active_hours(user_id: int, k: int) -> list[int]:
    redis = get_redis()
    try:
        data = await redis.hgetall(HOD_HIST_KEY.format(user_id))
    except RedisError:
        data = {}
    counts = []
    for h in range(24):
        try:
            key_s = str(h)
            raw = data.get(key_s)
            if raw is None:
                raw = data.get(key_s.encode())
            v = int(raw) if raw is not None else 0
        except Exception:
            v = 0
        counts.append((v, h))
    counts.sort(reverse=True)
    return [h for v, h in counts[:max(1,k)] if v > 0]

async def classify_signals_llm(personal_msgs: list[dict], summary: Optional[str]) -> tuple[bool, bool, bool]:
    transcript = _build_transcript(
        personal_msgs,
        summary,
        user_label="User",
        assistant_label="Assistant",
        empty_fallback="(no messages)",
    )

    system_msg = PERSONAL_PING_SIGNAL_CLASSIFIER_SYSTEM_PROMPT
    user_msg = PERSONAL_PING_SIGNAL_CLASSIFIER_USER_TEMPLATE.format(transcript=transcript)

    try:
        resp = await asyncio.wait_for(
            _call_openai_with_retry(
                endpoint="responses.create",
                model=settings.BASE_MODEL,
                input=[_msg("system", system_msg), _msg("user", user_msg)],
                max_output_tokens=24,
                temperature=0.0,
                top_p=1.0,
            ),
            timeout=settings.BASE_MODEL_TIMEOUT,
        )
        out = (_get_output_text(resp) or "").strip()
        data = json.loads(out)
        negative = bool(data.get("negative", False))
        open_loop = bool(data.get("open_loop", False))
        has_hook = bool(data.get("has_hook", False))
        return negative, open_loop, has_hook
    except Exception:
        logger.debug("signal classifier failed; fallback to neutral", exc_info=True)
        return False, False, False

async def register_private_activity(user_id: int) -> None:

    redis = get_redis()
    now = time_module.time()
    try:
        hist_key = IDLE_LIST_KEY.format(user_id)
        last_key = LAST_PRIVATE_TS_KEY.format(user_id)
        streak_key = PING_STREAK_KEY.format(user_id)
        prev = await redis.get(last_key)
        if prev is not None:
            try:
                prev_ts = float(prev.decode() if isinstance(prev, (bytes, bytearray)) else prev)
                idle = now - prev_ts
            except Exception:
                idle = None
        else:
            idle = None

        try:
            pend = await redis.hgetall(PENDING_PING_KEY.format(user_id))
            if pend:
                def _hget_local(d: dict, k: str):
                    return d.get(k, d.get(k.encode()))
                raw_ts = _hget_local(pend, "ts")
                raw_arm = _hget_local(pend, "arm")
                raw_ctx = _hget_local(pend, "ctx")
                arm = raw_arm.decode() if isinstance(raw_arm, (bytes, bytearray)) else raw_arm
                ctx = raw_ctx.decode() if isinstance(raw_ctx, (bytes, bytearray)) else raw_ctx
                p_ts = int(raw_ts) if raw_ts else 0
                if p_ts and arm and (0 <= now - p_ts <= _SUCCESS_WINDOW):
                    await bandit_update(user_id, arm, success=True, ctx=ctx)
                await redis.delete(PENDING_PING_KEY.format(user_id))
        except RedisError:
            logger.debug("pending success check failed", exc_info=True)

        async with redis.pipeline(transaction=True) as pipe:
            pipe.zrem(PING_SCHEDULE_KEY, str(user_id))
            if idle is not None:
                pipe.lpush(hist_key, idle)
                pipe.ltrim(hist_key, 0, settings.PERSONAL_PING_HISTORY_COUNT - 1)
                pipe.expire(hist_key, settings.PERSONAL_PING_RETENTION_SECONDS)
            pipe.set(last_key, now, ex=settings.PERSONAL_PING_RETENTION_SECONDS)
            pipe.set(streak_key, 0, ex=settings.PERSONAL_PING_RETENTION_SECONDS)
            pipe.set(ENROLLED_KEY.format(user_id), 1, ex=90*24*3600)
            await pipe.execute()

        await update_hod_hist(user_id, now)

    except RedisError:
        logger.exception("register_private_activity: Redis error for %s", user_id)
    try:
        await schedule_next_ping(user_id, now)
    except Exception:
        logger.exception("register_private_activity: schedule_next_ping failed for %s", user_id)

async def purge_user_state(user_id: int, reason: str) -> None:

    r = get_redis()

    try:
        async with r.pipeline(transaction=True) as pipe:
            pipe.zrem(PING_SCHEDULE_KEY, str(user_id))
            await pipe.execute()
    except RedisError:
        logger.exception("Failed to cleanup %s user %s", reason, user_id)

    try:
        deleted = await delete_user_redis_data(user_id)
        logger.info("User %s %s → purged %s keys", user_id, reason, deleted)
    except Exception:
        logger.exception("delete_user_redis_data failed for %s user %s", reason, user_id)

    try:
        async with session_scope(stmt_timeout_ms=3000) as db:
            u = await db.get(User, int(user_id))
            if u:
                await db.delete(u)
                logger.info("DB: removed user_id=%s due to %s", user_id, reason)
    except Exception:
        logger.exception("DB delete failed for user_id=%s reason=%s", user_id, reason)

async def _calculate_next_ping_ts(user_id: int, reference_ts: float) -> float:
    redis = get_redis()

    hist_key = IDLE_LIST_KEY.format(user_id)
    try:
        data = await redis.lrange(hist_key, 0, settings.PERSONAL_PING_HISTORY_COUNT - 1)
        history = []
        for x in (data or []):
            try:
                history.append(float(x.decode() if isinstance(x, (bytes, bytearray)) else x))
            except Exception:
                continue
    except RedisError:
        logger.debug("Cannot read personal idle history for %s", user_id)
        history = []

    base = settings.PERSONAL_PING_IDLE_THRESHOLD_SECONDS
    median_idle = statistics.median(history) if history else base
    adaptive_base = max(base, median_idle * settings.PERSONAL_PING_ADAPTIVE_MULTIPLIER)

    tz = await user_zoneinfo(user_id)
    _ref = datetime.fromtimestamp(reference_ts, tz)
    local_hour = _ref.hour + _ref.minute / 60.0
    circadian = (1 + math.sin((local_hour - 3) / 24 * 2 * math.pi)) / 2
    biorhythm = 1 + (1 - circadian) * settings.PERSONAL_PING_BIORHYTHM_WEIGHT

    try:
        raw_streak = await redis.get(PING_STREAK_KEY.format(user_id))
        streak = int((raw_streak.decode() if isinstance(raw_streak, (bytes, bytearray)) else raw_streak) or 0)
        if streak < 0:
            streak = 0
    except Exception:
        streak = 0

    base_interval = adaptive_base * biorhythm

    try:
        eff_streak = max(0, min(int(streak), int(MAX_CONSECUTIVE_PINGS)))
    except Exception:
        eff_streak = max(0, int(streak) if isinstance(streak, int) else 0)

    backoff_factor = (_BACKOFF_MULT ** eff_streak) if _BACKOFF_MULT and _BACKOFF_MULT > 1.0 else 1.0
    interval = base_interval * backoff_factor
    if _BACKOFF_MAX_HOURS and _BACKOFF_MAX_HOURS > 0:
        interval = min(interval, _BACKOFF_MAX_HOURS * 3600)
    if _BACKOFF_JITTER_PCT and _BACKOFF_JITTER_PCT > 0:
        jitter = max(0.0, 1.0 + random.uniform(-_BACKOFF_JITTER_PCT, _BACKOFF_JITTER_PCT))
        interval *= jitter

    next_ts = reference_ts + interval

    start_h = settings.PERSONAL_PING_START_HOUR
    end_h   = settings.PERSONAL_PING_END_HOUR

    def in_window(h, start_h, end_h):
        return (start_h <= h < end_h) if start_h < end_h else (h >= start_h or h < end_h)

    _next = datetime.fromtimestamp(next_ts, tz)
    next_local = _next.hour + _next.minute / 60.0
    if not in_window(next_local, start_h, end_h):
        delta_h = (start_h - next_local) % 24
        next_ts += delta_h * 3600

    try:
        top_hours = await top_active_hours(user_id, _ACTIVE_HOURS_TOPK)
        if top_hours:
            cur_dt = datetime.fromtimestamp(next_ts, tz)
            cur_hour = cur_dt.hour
            best_dt = None
            for h in top_hours:
                hours_ahead = (h - cur_hour) % 24
                candidate = cur_dt.replace(minute=0, second=0, microsecond=0)
                if hours_ahead == 0 and candidate <= cur_dt:
                    hours_ahead = 24
                candidate = candidate + timedelta(hours=hours_ahead)
                if in_window(candidate.hour + candidate.minute/60.0, start_h, end_h):
                    if best_dt is None or candidate < best_dt:
                        best_dt = candidate
            if best_dt:
                next_ts = best_dt.timestamp()
    except Exception:
        pass

    return next_ts

async def schedule_next_ping(user_id: int, reference_ts: float) -> None:
    redis = get_redis()
    next_ts = await _calculate_next_ping_ts(user_id, reference_ts)
    try:
        async with redis.pipeline(transaction=True) as pipe:
            pipe.zrem(PING_SCHEDULE_KEY, str(user_id))
            pipe.zadd(PING_SCHEDULE_KEY, {str(user_id): next_ts})
            pipe.expire(PING_SCHEDULE_KEY, settings.PERSONAL_PING_RETENTION_SECONDS)
            await pipe.execute()
    except RedisError:
        logger.exception("schedule_next_ping: Redis error for %s", user_id)

async def schedule_at(user_id: int, target_ts: float) -> None:

    redis = get_redis()
    tz = await user_zoneinfo(user_id)
    start_h = settings.PERSONAL_PING_START_HOUR
    end_h   = settings.PERSONAL_PING_END_HOUR
    def in_window(h, start_h, end_h):
        return (start_h <= h < end_h) if start_h < end_h else (h >= start_h or h < end_h)

    _next = datetime.fromtimestamp(target_ts, tz)
    next_local = _next.hour + _next.minute/60.0
    if not in_window(next_local, start_h, end_h):
        delta_h = (start_h - next_local) % 24
        target_ts += delta_h * 3600

    try:
        top_hours = await top_active_hours(user_id, _ACTIVE_HOURS_TOPK)
        if top_hours:
            cur_dt = datetime.fromtimestamp(target_ts, tz)
            best_dt = None
            for h in top_hours:
                hours_ahead = (h - cur_dt.hour) % 24
                candidate = cur_dt.replace(minute=0, second=0, microsecond=0)
                if hours_ahead == 0 and candidate <= cur_dt:
                    hours_ahead = 24
                candidate = candidate + timedelta(hours=hours_ahead)
                if in_window(candidate.hour + candidate.minute/60.0, start_h, end_h):
                    if best_dt is None or candidate < best_dt:
                        best_dt = candidate
            if best_dt:
                target_ts = best_dt.timestamp()
    except Exception:
        pass
    try:
        async with redis.pipeline(transaction=True) as pipe:
            pipe.zrem(PING_SCHEDULE_KEY, str(user_id))
            pipe.zadd(PING_SCHEDULE_KEY, {str(user_id): float(target_ts)})
            pipe.expire(PING_SCHEDULE_KEY, settings.PERSONAL_PING_RETENTION_SECONDS)
            await pipe.execute()
    except RedisError:
        logger.exception("schedule_at: Redis error for %s", user_id)

async def schedule_random_ping(user_id: int, reference_ts: float) -> None:
    try:
        min_h = int(max(1, float(_RANDOM_MIN_HOURS)))
        max_h = int(max(min_h, float(_RANDOM_MAX_HOURS)))
    except Exception:
        min_h, max_h = 12, 72

    delay_h = random.randint(min_h, max_h)
    base_aligned = math.floor(reference_ts / 3600.0) * 3600.0
    target_ts = base_aligned + delay_h * 3600
    await schedule_at(user_id, target_ts)

CLAIM_DUE_LUA = """
-- KEYS[1] = schedule zset
-- KEYS[2] = inflight zset
-- ARGV[1] = now_ts, ARGV[2] = batch_size, ARGV[3] = lease_seconds, ARGV[4] = reclaim_limit
local now_ts = tonumber(ARGV[1])
local n = tonumber(ARGV[2]) or 50
local lease = tonumber(ARGV[3]) or 120
local reclaim = tonumber(ARGV[4]) or n
local expired = redis.call('ZRANGEBYSCORE', KEYS[2], 0, now_ts, 'LIMIT', 0, reclaim)
if #expired > 0 then
  redis.call('ZREM', KEYS[2], unpack(expired))
  for _, uid in ipairs(expired) do
    redis.call('ZADD', KEYS[1], now_ts, uid)
  end
end
local due = redis.call('ZRANGEBYSCORE', KEYS[1], 0, now_ts, 'LIMIT', 0, n)
if #due == 0 then return {} end
redis.call('ZREM', KEYS[1], unpack(due))
local lease_until = now_ts + lease
for _, uid in ipairs(due) do
  redis.call('ZADD', KEYS[2], lease_until, uid)
end
return due
"""

REQUEUE_INFLIGHT_LUA = """
-- KEYS[1] = schedule zset
-- KEYS[2] = inflight zset
-- ARGV[1] = uid, ARGV[2] = target_ts
local uid = ARGV[1]
local target_ts = tonumber(ARGV[2])
if not uid or not target_ts then return 0 end
redis.call('ZREM', KEYS[2], uid)
redis.call('ZADD', KEYS[1], target_ts, uid)
return 1
"""

async def claim_due(redis, now_ts: float, batch_size: int):
    global _CLAIM_DUE_SHA
    try:
        if _CLAIM_DUE_SHA:
            return await redis.evalsha(
                _CLAIM_DUE_SHA,
                2,
                PING_SCHEDULE_KEY,
                PING_SCHEDULE_INFLIGHT,
                now_ts,
                int(batch_size),
                int(_PING_INFLIGHT_LEASE_SECONDS),
                int(batch_size),
            )
    except RedisError as e:
        if "NOSCRIPT" not in str(e):
            raise
        _CLAIM_DUE_SHA = None
    try:
        _CLAIM_DUE_SHA = await redis.script_load(CLAIM_DUE_LUA)
        return await redis.evalsha(
            _CLAIM_DUE_SHA,
            2,
            PING_SCHEDULE_KEY,
            PING_SCHEDULE_INFLIGHT,
            now_ts,
            int(batch_size),
            int(_PING_INFLIGHT_LEASE_SECONDS),
            int(batch_size),
        )
    except RedisError:
        return await redis.eval(
            CLAIM_DUE_LUA,
            2,
            PING_SCHEDULE_KEY,
            PING_SCHEDULE_INFLIGHT,
            now_ts,
            int(batch_size),
            int(_PING_INFLIGHT_LEASE_SECONDS),
            int(batch_size),
        )

async def requeue_inflight(redis, uid: str, target_ts: float) -> None:
    global _REQUEUE_INFLIGHT_SHA
    try:
        if _REQUEUE_INFLIGHT_SHA:
            await redis.evalsha(
                _REQUEUE_INFLIGHT_SHA,
                2,
                PING_SCHEDULE_KEY,
                PING_SCHEDULE_INFLIGHT,
                uid,
                float(target_ts),
            )
            return
    except RedisError as e:
        if "NOSCRIPT" not in str(e):
            raise
        _REQUEUE_INFLIGHT_SHA = None
    try:
        _REQUEUE_INFLIGHT_SHA = await redis.script_load(REQUEUE_INFLIGHT_LUA)
        await redis.evalsha(
            _REQUEUE_INFLIGHT_SHA,
            2,
            PING_SCHEDULE_KEY,
            PING_SCHEDULE_INFLIGHT,
            uid,
            float(target_ts),
        )
    except RedisError:
        await redis.eval(
            REQUEUE_INFLIGHT_LUA,
            2,
            PING_SCHEDULE_KEY,
            PING_SCHEDULE_INFLIGHT,
            uid,
            float(target_ts),
        )

async def personal_ping() -> None:
    redis = get_redis()
    MAX_LOOPS = 5
    for _ in range(MAX_LOOPS):
        now = time_module.time()
        try:
            raw = await claim_due(redis, now, settings.PERSONAL_PING_BATCH_SIZE)
            try:
                await redis.expire(PING_SCHEDULE_INFLIGHT, settings.PERSONAL_PING_RETENTION_SECONDS)
            except RedisError:
                logger.debug("personal_ping: cannot update inflight TTL", exc_info=True)
        except RedisError:
            logger.debug("personal_ping: cannot claim schedule")
            return
        if not raw:
            return

        due = [(m.decode() if isinstance(m, (bytes, bytearray)) else str(m)) for m in raw]

        keys = [ENROLLED_KEY.format(uid) for uid in due]
        try:
            async with redis.pipeline(transaction=False) as pipe:
                for k in keys:
                    pipe.exists(k)
                exists_flags = await pipe.execute()
        except RedisError:
            logger.debug("personal_ping: EXISTS check failed; requeueing due users")
            try:
                when = time_module.time() + 60 + random.uniform(-10, 20)
                for uid in due:
                    await requeue_inflight(redis, str(uid), when)
                await redis.expire(PING_SCHEDULE_KEY, settings.PERSONAL_PING_RETENTION_SECONDS)
            except RedisError:
                logger.exception("personal_ping: requeue after EXISTS failure failed")
            continue
        alive_due = []
        for uid, alive in zip(due, exists_flags):
            if int(alive) == 1:
                alive_due.append(uid)
            else:
                try:
                    await redis.zrem(PING_SCHEDULE_INFLIGHT, str(uid))
                except RedisError:
                    logger.debug("personal_ping: failed to drop inflight uid=%s", uid, exc_info=True)
        due = alive_due
        if not due:
            continue

        logger.info("personal_ping: %d users due (alive)", len(due))
        tasks = []
        for uid_str in due:
            try:
                user_id = int(uid_str)
            except ValueError:
                continue

            async def safe_handle(uid: int):
                success = False
                rescheduled = False
                try:
                    await asyncio.wait_for(handle_user_ping(uid, uid), timeout=75)
                    success = True
                except asyncio.TimeoutError:
                    logger.warning("personal_ping: user %s timed out — rescheduling", uid)
                    try:
                        await schedule_next_ping(uid, time_module.time())
                        rescheduled = True
                    except Exception:
                        logger.exception("personal_ping: reschedule failed after timeout for %s", uid)
                except Exception:
                    logger.exception("personal_ping: error for user %s", uid)
                finally:
                    try:
                        await redis.zrem(PING_SCHEDULE_INFLIGHT, str(uid))
                    except RedisError:
                        logger.debug(
                            "personal_ping: failed to clear inflight uid=%s success=%s rescheduled=%s",
                            uid,
                            success,
                            rescheduled,
                            exc_info=True,
                        )
            tasks.append(safe_handle(user_id))

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        await asyncio.sleep(0)

async def handle_user_ping(chat_id: int, user_id: int) -> None:

    should_reschedule: bool = True
    try:
        should_reschedule = await send_contextual_ping(chat_id, user_id)
    except asyncio.CancelledError:
        await schedule_next_ping(user_id, time_module.time())
        raise
    except Exception:
        logger.exception("handle_user_ping: error sending ping for %s", user_id)
        should_reschedule = True
    finally:
        if should_reschedule:
            await schedule_next_ping(user_id, time_module.time())
        else:
            logger.info("handle_user_ping: no reschedule for user %s", user_id)

def build_prompt(
    arm: str,
    mem_ctx: str,
    motive: str,
    allow_question: bool,
    anchor_hint: Optional[str],
    allow_generic_checkin: bool,
    lang_exemplar: Optional[str] = None,
    ) -> str:

    motive_txt = MOTIVES.get(motive, "Be human and specific, with a real personal angle.")

    arm_hint = {
        "callback": "Pick up the unresolved point naturally and keep it tight.",
        "question": "Pose a single low-effort hook but keep it optional.",
        "suggestion": "Offer one tiny next step the user can do now; make it optional.",
        "checkin": "Write a soft, caring nudge anchored in one past detail; no pressure.",
    }.get(arm, "Write a natural, human nudge.")

    if lang_exemplar:
        _ex = " ".join(lang_exemplar.split())[:160]
        language_rule = PERSONAL_PING_LANGUAGE_RULE_WITH_EXEMPLAR_TEMPLATE.format(exemplar=_ex)
    else:
        language_rule = PERSONAL_PING_LANGUAGE_RULE_FROM_HISTORY

    rules_common = PERSONAL_PING_RULES_COMMON_TEMPLATE.format(
        language_rule=language_rule,
        greeting_rule="" if allow_generic_checkin else "No greetings, ",
        generic_rule="" if allow_generic_checkin else "Avoid generic check-ins (e.g., 'how are you?') unless this is a care check-in. ",
    )

    q_rule = PERSONAL_PING_Q_RULE_ALLOW if allow_question else PERSONAL_PING_Q_RULE_NO

    care_rule = PERSONAL_PING_CARE_RULE if allow_generic_checkin else ""
    anchor_line = PERSONAL_PING_ANCHOR_LINE_TEMPLATE.format(anchor_hint=anchor_hint) if anchor_hint else ""
    ctx = PERSONAL_PING_CTX_TEMPLATE.format(mem_ctx=mem_ctx) if mem_ctx else ""

    variation_seed = random.randint(1, 10_000)
    microstyle = random.choice([
        "keep cadence crisp", "slightly playful cadence", "gentle and plain",
        "hint of curiosity", "matter-of-fact and warm"
    ])
    return (
        f"{ctx}"
        f"Write a single DM nudge that feels human, not a bot.\n"
        f"MOTIVE: {motive_txt}\n"
        f"{anchor_line}"
        f"ARM: {arm_hint}\n"
        f"RULES: {rules_common} {q_rule} {care_rule}\n"
        f"VARIATION_SEED: {variation_seed}; MICROSTYLE: {microstyle}"
    )

def norm_text(s: str) -> str:

    if not s:
        return ""
    s = unicodedata.normalize("NFKC", s)
    s = s.replace("\uFE0F","").replace("\uFE0E","").replace("\u200D","").replace("\u00A0"," ")
    s = "".join(ch for ch in s if unicodedata.category(ch) != "Cf")
    s = " ".join(s.split())
    s = "".join(ch for ch in s if unicodedata.category(ch) != "So")
    s = re.sub(r"[\.!\?…]+$", "", s).strip()
    return s.casefold()

def _last_assistant_text(msgs: list[dict]) -> str | None:
    if not msgs:
        return None
    for m in reversed(msgs):
        try:
            if (m.get("role") == "assistant"):
                t = (m.get("content") or "").strip()
                if t:
                    return t
        except Exception:
            continue
    return None

def split_sentences_lite(t: str) -> list[str]:
    parts, buf = [], []
    for ch in t:
        buf.append(ch)
        if ch in ".!?…":
            seg = "".join(buf).strip()
            if seg:
                parts.append(seg)
            buf = []
    tail = "".join(buf).strip()
    if tail:
        parts.append(tail)
    return [p for p in parts if p]

def postprocess_ping(text: str, allow_question: bool, allow_generic_checkin: bool) -> str:
    if not text:
        return text
    t = " ".join(text.strip().split())
    parts = split_sentences_lite(t)
    if len(parts) > 2:
        t = " ".join(parts[:2]).strip()
    if not allow_question and "?" in t:
        t = re.sub(r"\?+", "…", t).strip()
        if not (t.endswith(".") or t.endswith("…") or t.endswith("!")):
            t += "…"
    t = " ".join(t.split())
    if len(norm_text(t)) < 8 or len(_token_set(t)) < 2:
        return ""
    return t
 
def _token_set(s: str) -> set[str]:
    return set(re.findall(r"\w{2,}", (s or "").lower()))

def _jaccard(a: str, b: str) -> float:
    A, B = _token_set(a), _token_set(b)
    if not A or not B:
        return 0.0
    return len(A & B) / max(1, len(A | B))

async def _get_recent_ping_texts(user_id: int, limit: int = 10) -> list[str]:
    r = get_redis()
    key = f"last_ping_texts:{user_id}"
    try:
        raw = await r.lrange(key, 0, limit - 1)
        out = []
        for x in raw or []:
            out.append(x.decode() if isinstance(x, (bytes, bytearray)) else str(x))
        return [t for t in out if t]
    except RedisError:
        return []

async def _push_recent_ping_text(user_id: int, text: str) -> None:
    r = get_redis()
    key = f"last_ping_texts:{user_id}"
    try:
        async with r.pipeline(transaction=True) as pipe:
            pipe.lpush(key, norm_text(text))
            pipe.ltrim(key, 0, 19)
            pipe.expire(key, settings.PERSONAL_PING_RETENTION_SECONDS)
            await pipe.execute()
    except RedisError:
        pass

def build_bandit_ctx(local_dt: datetime, open_loop: bool, has_hook: bool, care_ok: bool,
                     idle_hours: float, tts_window_ok: bool) -> str:

    hod_bucket = (local_dt.hour // 4)
    dow = local_dt.weekday()
    if idle_hours < 6:
        ib = 0
    elif idle_hours < 24:
        ib = 1
    elif idle_hours < 72:
        ib = 2
    else:
        ib = 3
    return f"hb={hod_bucket};d={dow};ol={int(open_loop)};hk={int(has_hook)};care={int(care_ok)};ib={ib};tts={int(tts_window_ok)}"

async def send_contextual_ping(chat_id: int, user_id: int) -> bool:

    redis = get_redis()
    chosen_arm: str | None = None
    
    try:
        await bandit_check_expire_or_success(user_id, time_module.time())
    except Exception:
        logger.debug("expire_or_success failed", exc_info=True)

    try:
        raw_streak = await redis.get(PING_STREAK_KEY.format(user_id))
        streak = int((raw_streak.decode() if isinstance(raw_streak, (bytes, bytearray)) else raw_streak) or 0) if raw_streak is not None else 0
    except RedisError:
        logger.debug("Cannot read ping streak for %s", user_id)
        streak = 0
    if streak >= MAX_CONSECUTIVE_PINGS:
        logger.info("PP PAUSE max_streak user=%s streak=%d max=%d", user_id, streak, MAX_CONSECUTIVE_PINGS)
        await schedule_at(user_id, time_module.time() + 24 * 3600)
        return False

    persona = await get_persona(chat_id)
    try:
        await asyncio.wait_for(persona._restored_evt.wait(), timeout=5.0)
    except Exception:
        logger.warning("PP persona restore not ready in 5s; SKIP ping for user %s", user_id)
        return True

    gender = await get_cached_gender(int(user_id))
    if gender not in ("male", "female"):
        async with session_scope(stmt_timeout_ms=2000, read_only=True) as db:
            u = await db.get(User, int(user_id))
            if u and u.gender in ("male", "female"):
                gender = u.gender
    user_gender_val = gender if gender in ("male", "female") else None

    try:
        style_mods = persona._mods_cache or await asyncio.wait_for(persona.style_modifiers(), 30)
    except Exception:
        logger.exception("style_modifiers acquisition failed")
        style_mods = {}
    mods = merge_and_clamp_mods(style_mods)
    guidelines = await persona.style_guidelines(user_id)
    system_msg = await build_system_prompt(persona, guidelines, user_gender=user_gender_val)

    e = persona.state.get("engagement", persona.state.get("engagement_mod", 0.5))
    c = persona.state.get("curiosity",  persona.state.get("curiosity_mod",  0.5))
    a = persona.state.get("arousal",    persona.state.get("arousal_mod",    0.5))
    boredom = ( (1.0 - e) + (1.0 - c) + (1.0 - min(a, 0.6)) ) / 3.0
    try:
        logger.info("PP STATE uid=%s e=%.3f c=%.3f a=%.3f boredom=%.3f thr=%.2f streak=%d",
                    user_id, e, c, a, boredom, settings.PERSONAL_PING_MIN_BOREDOM, streak)
    except Exception:
        pass

    try:
        history = await load_context(chat_id, user_id)
        summary: str | None = None
        if history and history[0].get("role") == "system":
            c0 = str(history[0].get("content") or "")
            if c0.strip().lower().startswith("summary:"):
                summary = c0.split(":", 1)[1].strip()
                history = history[1:]

        personal_msgs = []
        for m in history or []:
            r = m.get("role")
            if r == "assistant":
                personal_msgs.append(m)
            elif r == "user" and m.get("user_id") == user_id:
                personal_msgs.append(m)
        mem_ctx = _build_transcript(
            personal_msgs,
            summary,
            user_label="You",
            assistant_label="Me",
            empty_fallback="",
            normalize_newlines=False,
        )
    except Exception:
        logger.exception("load_context failed for chat_id=%s", chat_id)
        mem_ctx = ""
        personal_msgs = []
        summary = None

    negative, open_loop, has_hook = await classify_signals_llm(personal_msgs, summary)
    try:
        logger.info("PP SIGNALS uid=%s negative=%s open_loop=%s has_hook=%s model=%s",
                    user_id, negative, open_loop, has_hook, settings.REASONING_MODEL)
    except Exception:
        pass

    if negative:
        try:
            await schedule_at(user_id, time_module.time() + _NEGATIVE_COOLDOWN)
            try:
                await redis.set(PING_STREAK_KEY.format(user_id), 0,
                                ex=settings.PERSONAL_PING_RETENTION_SECONDS)
            except Exception:
                pass
        except Exception:
            pass
        logger.info("PP SKIP negative_signal user=%s (DND/negative signal detected in history)", user_id)
        return False

    now_ts = time_module.time()
    try:
        tz = await user_zoneinfo(user_id)
        local_dt = datetime.fromtimestamp(now_ts, tz)
        local_hour = local_dt.hour + local_dt.minute / 60.0
        raw_last = await redis.get(LAST_PRIVATE_TS_KEY.format(user_id))
        last_ts = float(raw_last.decode() if isinstance(raw_last, (bytes, bytearray)) else raw_last) if raw_last else 0.0
    except Exception:
        try:
            _utc = datetime.utcfromtimestamp(now_ts)
            local_hour = _utc.hour + _utc.minute / 60.0
        except Exception:
            local_hour = 12.0
        local_dt = datetime.utcfromtimestamp(now_ts)
        last_ts = 0.0
    idle_hours = (now_ts - last_ts) / 3600.0 if last_ts > 0 else float("inf")

    ec = await classify_emotion_context_llm(personal_msgs, summary)
    care_ok = bool(ec.care_needed or (ec.motive in CARE_MOTIVES))
    anchor_hint = ec.anchor

    if not (open_loop or has_hook or care_ok):
        if boredom < settings.PERSONAL_PING_MIN_BOREDOM:
            try:
                now_ts = time_module.time()
                await schedule_random_ping(user_id, now_ts)
                try:
                    logger.info(
                        "PP SKIP boredom→random(user=%s) [%dh..%dh] b=%.2f<thr=%.2f (no hooks/care)",
                        user_id, int(_RANDOM_MIN_HOURS), int(_RANDOM_MAX_HOURS),
                        boredom, settings.PERSONAL_PING_MIN_BOREDOM
                    )
                except Exception:
                    pass
                return False
            except Exception:
                logger.exception("PP random schedule failed for user %s; fallback to default reschedule", user_id)
                return True

    if not open_loop and not has_hook and not care_ok:
        try:
            raw_re = await redis.get(REANIMATE_LAST_TS_KEY.format(user_id))
            re_ts = float(raw_re.decode() if isinstance(raw_re, (bytes, bytearray)) else raw_re) if raw_re else 0.0
        except Exception:
            re_ts = 0.0
        since_re_hours = (now_ts - re_ts) / 3600.0 if re_ts > 0 else float("inf")
        try:
            lp = await redis.hgetall(f"last_ping:pm:{user_id}") or {}
            raw_lp = lp.get("ts") if "ts" in lp else lp.get(b"ts")
            lp_ts = int(raw_lp) if raw_lp else 0
        except Exception:
            lp_ts = 0
        since_last_ping_h = (now_ts - lp_ts) / 3600.0 if lp_ts > 0 else float("inf")

        has_any_private_activity = (last_ts > 0)
        if has_any_private_activity and \
           idle_hours >= float(_REANIMATE_IDLE_HOURS) and \
           since_re_hours >= float(_REANIMATE_MIN_GAP_HOURS) and \
           since_last_ping_h >= float(_REANIMATE_MIN_GAP_HOURS):
            logger.info("PP REANIMATE gate PASSED user=%s idle=%.1fh since_re=%.1fh since_ping=%.1fh",
                        user_id, idle_hours, since_re_hours, since_last_ping_h)
            chosen_arm = "checkin"
        else:
            logger.info("PP SKIP no topical hook user=%s (idle=%.1fh, since_re=%.1fh, since_ping=%.1fh) — checking care_ok=%s",
                        user_id, idle_hours, since_re_hours, since_last_ping_h, care_ok)
            if not care_ok:
                return True
            chosen_arm = "checkin"

    novelty = (0.4 * mods["creativity_mod"] + 0.4 * mods["sarcasm_mod"] + 0.2 * mods["enthusiasm_mod"])
    coherence = (0.5 * mods["confidence_mod"] + 0.3 * mods["precision_mod"]
                 + 0.1 * (1 - mods["fatigue_mod"]) + 0.1 * (1 - mods["stress_mod"]))
    alpha = 1.8
    dynamic_temperature = MIN_TEMPERATURE + (MAX_TEMPERATURE - MIN_TEMPERATURE) * (novelty ** alpha)
    dynamic_top_p       = TOP_P_MIN + (TOP_P_MAX - TOP_P_MIN) * (1.0 - coherence)
    max_tokens = 120
    try:
        dynamic_temperature *= (1.0 + 0.10 * float(mods["valence_mod"]))
    except Exception:
        pass
    if dynamic_temperature < 0.55:
        dynamic_temperature = 0.55
    if dynamic_temperature > 0.70:
        dynamic_temperature = 0.70
    if dynamic_top_p < 0.85:
        dynamic_top_p = 0.85
    if dynamic_top_p > 0.98:
        dynamic_top_p = 0.98

    try:
        logger.info(
            "PP SAMPLING uid=%s novelty=%.3f coherence=%.3f temp=%.2f top_p=%.2f "
            "mods[c=%.2f sa=%.2f e=%.2f conf=%.2f prec=%.2f fat=%.2f str=%.2f cur=%.2f val=%.2f]",
            user_id, novelty, coherence, dynamic_temperature, dynamic_top_p,
            mods.get('creativity_mod',0.0), mods.get('sarcasm_mod',0.0), mods.get('enthusiasm_mod',0.0),
            mods.get('confidence_mod',0.0), mods.get('precision_mod',0.0), mods.get('fatigue_mod',0.0),
            mods.get('stress_mod',0.0), mods.get('curiosity_mod',0.0), mods.get('valence_mod',0.0)
        )
    except Exception:
        pass

    if _FORCE_CALLBACK_ON_OPEN and open_loop:
        chosen_arm = "callback"

    if open_loop:
        motive = "unfinished_thread"
        allow_question = True
    else:
        if ec.motive:
            motive = ec.motive
        elif has_hook:
            motive = "topic_interest"
        elif idle_hours >= float(_REANIMATE_IDLE_HOURS):
            motive = "missed_you"
        else:
            motive = "light_care"
        allow_question = bool(ec.care_needed)

    allow_generic_checkin = bool(ec.care_needed or motive in CARE_MOTIVES)

    if (not allow_generic_checkin) and _ALLOW_GENERIC_WHEN_BORED and (motive in {"light_care", "missed_you"}) \
       and (boredom >= settings.PERSONAL_PING_MIN_BOREDOM) and (random.random() < _GENERIC_HELLO_PROB):
        allow_generic_checkin = True
        motive = "bored_hello"
    if motive == "bored_hello" and not allow_question and random.random() < _GENERIC_HELLO_ASK_PROB:
        allow_question = True
    if chosen_arm == "question":
        allow_question = True

    last_assist = _last_assistant_text(personal_msgs)
    care_ok_final = bool(ec.care_needed or (ec.motive in CARE_MOTIVES) or (motive in CARE_MOTIVES))

    def _in_voice_window(h_start: int, h_end: int, lh: float) -> bool:
        return (h_start <= lh < h_end) if h_start < h_end else (lh >= h_start or lh < h_end)

    tts_win_ok = _in_voice_window(int(_TTS_VOICE_START_H), int(_TTS_VOICE_END_H), float(local_hour))

    ctx_key = build_bandit_ctx(
        local_dt,
        open_loop=open_loop,
        has_hook=has_hook,
        care_ok=care_ok_final,
        idle_hours=idle_hours,
        tts_window_ok=tts_win_ok,
    )

    if chosen_arm is None:
        chosen_arm = await bandit_choose_arm(user_id, ctx_key)
    
    prompt = build_prompt(
        chosen_arm,
        mem_ctx or "",
        motive=motive,
        allow_question=allow_question,
        anchor_hint=anchor_hint,
        allow_generic_checkin=allow_generic_checkin,
        lang_exemplar=last_assist
    )

    async def _gen_once(temp: float, top_p: float) -> str:
        time_sys = _build_ping_time_hint()
        resp = await asyncio.wait_for(
            _call_openai_with_retry(
                endpoint="responses.create",
                model=settings.RESPONSE_MODEL,
                input=[_msg("system", time_sys), _msg("system", system_msg), _msg("user", prompt)],
                max_output_tokens=max_tokens,
                temperature=temp,
                top_p=top_p,
            ),
            timeout=settings.RESPONSE_MODEL_TIMEOUT,
        )
        return (_get_output_text(resp) or "").strip()

    try:
        text = await _gen_once(dynamic_temperature, dynamic_top_p)
    except Exception:
        logger.exception("PP ERROR openai user=%s", user_id)
        return True

    if not text:
        logger.warning("PP SKIP empty_output user=%s", user_id)
        return True
    text = postprocess_ping(text, allow_question=allow_question, allow_generic_checkin=allow_generic_checkin)
    if not text:
        logger.warning("PP SKIP empty_after_postprocess user=%s", user_id)
        return True

    try:
        recent = await _get_recent_ping_texts(user_id, limit=10)
        if recent and max((_jaccard(text, t) for t in recent), default=0.0) >= 0.80:
            try:
                text2 = await _gen_once(min(0.85, dynamic_temperature + 0.08), min(0.98, dynamic_top_p + 0.02))
                text2 = postprocess_ping(text2, allow_question=allow_question, allow_generic_checkin=allow_generic_checkin)
                if text2 and max((_jaccard(text2, t) for t in recent), default=0.0) < 0.80:
                    text = text2
            except Exception:
                pass
    except Exception:
        logger.debug("recent dedupe check failed", exc_info=True)

    try:
        prev_txt = await redis.hget(f"last_ping:pm:{user_id}", "text")
        if isinstance(prev_txt, (bytes, bytearray)):
            prev_txt = prev_txt.decode("utf-8", "ignore")
        if prev_txt and norm_text(prev_txt) == norm_text(text):
            logger.info("PP SKIP duplicate(norm) pre-send user=%s", user_id)
            return True
    except RedisError:
        logger.debug("cannot read last_ping to dedupe (pre-send)", exc_info=True)

    sent_voice = False
    try:
        if _TTS_PING_ENABLED and is_tts_eligible_short(text):

            pref_raw = await redis.get(f"tts:pref:{user_id}") or b""
            pref = pref_raw.decode().strip().lower() if isinstance(pref_raw, (bytes, bytearray)) \
                   else str(pref_raw).strip().lower()

            voice_window_ok = tts_win_ok

            disable_until_raw = await redis.get(f"tts:cb:disable_until:{user_id}")
            cb_disabled = False
            if disable_until_raw is not None:
                try:
                    disable_until = float(disable_until_raw.decode() if isinstance(disable_until_raw, (bytes, bytearray)) else disable_until_raw)
                    cb_disabled = now_ts < disable_until
                except Exception:
                    cb_disabled = False

            p_dyn = max(0.0, min(1.0, _TTS_PING_PROB))
            if open_loop:
                p_dyn += 0.12
            if motive in CARE_MOTIVES:
                p_dyn += 0.10
            if motive == "bored_hello":
                p_dyn += 0.05
            if boredom >= settings.PERSONAL_PING_MIN_BOREDOM:
                p_dyn += 0.05

            p_dyn = max(0.0, min(0.66, p_dyn))

            chat_voice_disabled = bool(await redis.get(f"vmsg:disabled:chat:{user_id}"))

            try:
                stats_ctx = await bandit_get_stats_ctx(user_id, ctx_key)
            except Exception:
                stats_ctx = {}

            voice_bias: float | None = None
            try:
                a_v, b_v = stats_ctx.get(f"{chosen_arm}:voice", (0, 0))
                a_t, b_t = stats_ctx.get(f"{chosen_arm}:text",  (0, 0))
                if (a_v + b_v + a_t + b_t) > 0:
                    theta_v = (a_v + 1) / (a_v + b_v + 2)
                    theta_t = (a_t + 1) / (a_t + b_t + 2)
                    voice_bias = theta_v - theta_t
            except Exception:
                voice_bias = None

            want_voice = can_send_tts(
                user_id=user_id,
                pref=pref,
                chat_voice_disabled=chat_voice_disabled,
                voice_window_ok=voice_window_ok,
                cb_disabled=cb_disabled,
                p_dyn=p_dyn,
                voice_bias=voice_bias,
            )

            if want_voice and not chat_voice_disabled:
                sent_voice = await maybe_tts_and_send(
                    chat_id=user_id,
                    user_id=user_id,
                    reply_text=text,
                    voice_in=False,
                    force=True,
                    exclusive=not _TTS_PING_CAPTION_ENABLED,
                    caption_max=(_TTS_PING_CAPTION_LEN if _TTS_PING_CAPTION_ENABLED else 0),
                )
                if sent_voice:
                    try:
                        await redis.delete(f"tts:cb:failcount:{user_id}")
                    except Exception:
                        pass
    except Exception:
        logger.debug("TTS ping attempt failed", exc_info=True)

    if sent_voice:
        chosen_arm_mod = f"{chosen_arm}:voice"
        try:
            asyncio.create_task(record_ping_sent(int(user_id), chosen_arm_mod))
        except Exception:
            pass
        sent_ts = int(time_module.time())
        try:
            await redis.hset(f"last_ping:pm:{user_id}", mapping={
                "msg_id": 0,
                "ts": sent_ts,
                "text": text,
                "arm": chosen_arm_mod
            })
            await redis.expire(f"last_ping:pm:{user_id}", settings.PERSONAL_PING_RETENTION_SECONDS)
        except RedisError:
            logger.debug("failed to cache voice PM ping", exc_info=True)
        
        try:
            await push_message(chat_id, "assistant", text, user_id=user_id)
        except Exception:
            logger.exception("push_message failed for voice personal ping %s", user_id)
        await bandit_mark_pending(user_id, sent_ts, chosen_arm_mod, ctx=ctx_key)
        
        try:
            async with redis.pipeline(transaction=True) as pipe:
                pipe.incr(PING_STREAK_KEY.format(user_id))
                pipe.expire(PING_STREAK_KEY.format(user_id), settings.PERSONAL_PING_RETENTION_SECONDS)
                await pipe.execute()
        except RedisError:
            logger.exception("Failed to update ping streak for voice %s", user_id)
        
        if chosen_arm == "checkin" and (not open_loop and not has_hook):
            try:
                ttl_sec = int(max(
                    getattr(settings, "PERSONAL_PING_RETENTION_SECONDS", 7*24*3600),
                    float(_REANIMATE_MIN_GAP_HOURS) * 3600.0
                ))
                await redis.set(REANIMATE_LAST_TS_KEY.format(user_id), float(sent_ts), ex=ttl_sec)
            except Exception:
                logger.debug("Failed to store reanimate_last_ts (voice)", exc_info=True)
        try:
            await _push_recent_ping_text(user_id, text)
        except Exception:
            pass

        return True

    logger.info("Generated personal ping for %s (boredom=%.2f, arm=%s)", user_id, boredom, chosen_arm)

    try:
        chosen_arm_mod = f"{chosen_arm}:text"
        try:
            asyncio.create_task(record_ping_sent(int(user_id), chosen_arm_mod))
        except Exception:
            pass
        mid = await send_private_with_retry(user_id, text)
        if not mid:
            return True
        sent_ts = int(time_module.time())
        try:
            await redis.set(f"msg:{user_id}:{mid}", text, ex=settings.MEMORY_TTL_DAYS * 86_400)
            await redis.hset(f"last_ping:pm:{user_id}", mapping={
                "msg_id": int(mid),
                "ts": sent_ts,
                "text": text,
                "arm": chosen_arm_mod
            })
            await redis.expire(f"last_ping:pm:{user_id}", settings.PERSONAL_PING_RETENTION_SECONDS)
        except RedisError:
            logger.debug("failed to cache PM ping message_id/text", exc_info=True)
        try:
            await push_message(chat_id, "assistant", text, user_id=user_id)
        except Exception:
            logger.exception("push_message failed for personal ping %s", user_id)
        try:
            async with redis.pipeline(transaction=True) as pipe:
                pipe.incr(PING_STREAK_KEY.format(user_id))
                pipe.expire(PING_STREAK_KEY.format(user_id), settings.PERSONAL_PING_RETENTION_SECONDS)
                await pipe.execute()
        except RedisError:
            logger.exception("Failed to update ping streak for %s", user_id)

        await bandit_mark_pending(user_id, sent_ts, chosen_arm_mod, ctx=ctx_key)

        if chosen_arm == "checkin" and (not open_loop and not has_hook):
            try:
                ttl_sec = int(max(
                    getattr(settings, "PERSONAL_PING_RETENTION_SECONDS", 7*24*3600),
                    float(_REANIMATE_MIN_GAP_HOURS) * 3600.0
                ))
                await redis.set(
                    REANIMATE_LAST_TS_KEY.format(user_id),
                    float(sent_ts),
                    ex=ttl_sec
                )
            except Exception:
                logger.debug("Failed to store reanimate_last_ts", exc_info=True)
        
        try:
            await _push_recent_ping_text(user_id, text)
        except Exception:
            pass

        return True

    except TelegramForbiddenError:
        try:
            await redis.set(PING_STREAK_KEY.format(user_id), 0,
                            ex=settings.PERSONAL_PING_RETENTION_SECONDS)
        except Exception:
            pass
        await purge_user_state(user_id, "blocked bot")
        logger.info("Removed %s from personal ping (bot forbidden)", user_id)
        return False
    except RuntimeError as e:
        if str(e).startswith("HARD_BADREQUEST:"):
            try:
                await redis.set(PING_STREAK_KEY.format(user_id), 0,
                                ex=settings.PERSONAL_PING_RETENTION_SECONDS)
            except Exception:
                pass
            await purge_user_state(user_id, "HARD_BADREQUEST")
            logger.info("Removed %s from personal ping (hard badrequest)", user_id)
            return False
        raise
    except Exception:
        logger.exception("PP ERROR send user=%s", user_id)
        return True

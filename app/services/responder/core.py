cat >app/services/responder/core.py<< EOF
#app/services/responder/core.py
from __future__ import annotations

import logging
import asyncio
import time
import re
import unicodedata

from typing import Dict, List
from aiohttp import ClientError

from app.clients.openai_client import _call_openai_with_retry
from app.config import settings
from app.core.memory import (
    load_context, push_message,
    get_redis, get_cached_gender, cache_gender,
    _k_g_sum,
)
from app.emo_engine import get_persona
from app.core.db import AsyncSessionLocal
from app.core.models import User
from .prompt_builder import build_system_prompt
from .coref import needs_coref, resolve_coref
from .gender import detect_gender
from .rag import _KB_ENTRIES, _init_kb, is_relevant


logger = logging.getLogger(__name__)

ON_TOPIC_MAX_TOKENS = 800
OFF_TOPIC_MAX_TOKENS = 600
MAX_TEMPERATURE = 0.8
MIN_TEMPERATURE = 0.6
TOP_P_MIN = 0.8
TOP_P_MAX = 1.0
EMOJI_OR_SYMBOLS_ONLY = re.compile(r'^[\W_]+$', flags=re.UNICODE)
PERSONAL_WINDOW = 30
DEFAULT_MODS = {
    "creativity_mod": 0.5,
    "sarcasm_mod":    0.0,
    "enthusiasm_mod": 0.5,
    "technical_mod":  0.0,
    "confidence_mod": 0.5,
    "precision_mod":  0.5,
    "fatigue_mod":    0.0,
    "stress_mod":     0.0,
}
REPEAT_DETECT_WINDOW_SEC = getattr(settings, "REPEAT_DETECT_WINDOW_SEC", 25)
REPEAT_DETECT_TTL = getattr(settings, "REPEAT_DETECT_TTL", 120)


async def respond_to_user(
    text: str,
    chat_id: int,
    user_id: int,
    *,
    group_mode: bool = False,
    is_channel_post: bool = False,
    channel_title: str | None = None,
    reply_to: int | None = None,
    msg_id: int | None = None,
    voice_in: bool = False,
) -> str:

    redis = get_redis()

    t0 = time.time()
    logger.info("▶ respond_to_user START chat=%s user=%s len=%d",
                chat_id, user_id, len(text))

    try:
        persona = await get_persona(
            chat_id,
            user_id=user_id,
            group_mode=group_mode or is_channel_post,
        )
    except Exception:
        logger.exception("Failed to get_persona", exc_info=True)
        await push_message(chat_id, "assistant",
                           "I’m sorry, something went wrong.", user_id=user_id)
        return "I’m sorry, something went wrong."
    else:
        logger.info("   ↳ get_persona END (t=%.3fs)", time.time() - t0)

    try:
        logger.debug("▶ respond_to_user: waiting for persona._restored_evt")
        await persona._restored_evt.wait()
        logger.debug("▶ respond_to_user: persona._restored_evt is set, continue processing")
    except Exception:
        logger.exception("✖ Error while waiting for persona._restored_evt", exc_info=True)
    else:
        logger.info("   ↳ persona._restored_evt END (t=%.3fs)", time.time() - t0)

    for _m, _d in (
        ("valence", 0.0),
        ("arousal", 0.5),
        ("energy", 0.0),
        ("stress", 0.0),
        ("anxiety", 0.0),
    ):
        persona.state.setdefault(_m, _d)

    gender = None
    async with AsyncSessionLocal() as db:
        user = await db.get(User, user_id)
        if user and user.gender in ("male", "female"):
            gender = user.gender
        if gender is None:
            gender = await get_cached_gender(user_id)
        if gender is None:
            ui = await redis.hgetall(f"tg_user:{user_id}") or {}
            first = ui.get("first_name") or ui.get(b"first_name") or ""
            nick = ui.get("username") or ui.get(b"username") or ""
            raw_name = first or nick
            if isinstance(raw_name, (bytes, bytearray)):
                name = raw_name.decode(errors="ignore")
            else:
                name = str(raw_name)
            gender = await detect_gender(name, text) or "unknown"
            await cache_gender(user_id, gender)

    local_gender = gender if gender in ("male", "female") else "unknown"

    try:
        await persona.process_interaction(user_id, text, user_gender=local_gender)
    except Exception:
        logger.exception("Failed persona.process_interaction", exc_info=True)
    else:
        logger.info("   ↳ process_interaction END (t=%.3fs)", time.time() - t0)

    try:
        _ = await persona.summary()
    except Exception:
        logger.exception("Failed to get persona.summary", exc_info=True)
    else:
        logger.info("   ↳ persona.summary END (t=%.3fs)", time.time() - t0)

    guidelines: List[str] = []
    try:
        guidelines = await persona.style_guidelines(user_id)
    except Exception:
        logger.exception("Failed to get style_guidelines", exc_info=True)
    else:
        logger.info("   ↳ style_guidelines END (t=%.3fs)", time.time() - t0)

    try:
        style_mods = persona._mods_cache or await asyncio.wait_for(
            persona.style_modifiers(), 30
        )
    except Exception:
        logger.exception("style_modifiers acquisition failed")
        style_mods = {}

    mods = {k: style_mods.get(k, v) for k, v in DEFAULT_MODS.items()}

    try:
        reasoning_model = settings.REASONING_MODEL
        if mods.get("technical_mod", 0) > 0.6:
            reasoning_model = "gpt-4o-mini-functions"
        elif mods.get("curiosity_mod", 0) > 0.5:
            reasoning_model = settings.REASONING_MODEL

        draft_msg = None
        draft_resp = await asyncio.wait_for(
            _call_openai_with_retry(
                model=reasoning_model,
                messages=[
                    {"role": "system", "content": "You think out loud. Make a short plan of your answer in points."},
                    {"role": "user", "content": text},
                ],
                temperature=0.0,
                max_completion_tokens=300,
            ),
            timeout=15,
        )
        draft_msg = draft_resp.choices[0].message.content.strip()
    except Exception:
        draft_msg = None

    novelty = (
        0.4 * mods["creativity_mod"]
        + 0.4 * mods["sarcasm_mod"]
        + 0.2 * mods["enthusiasm_mod"]
    )
    coherence = (
        0.5 * mods["confidence_mod"]
        + 0.3 * mods["precision_mod"]
        + 0.1 * (1 - mods["fatigue_mod"])
        + 0.1 * (1 - mods["stress_mod"])
    )
    alpha = 1.8
    dynamic_temperature = MIN_TEMPERATURE + (MAX_TEMPERATURE - MIN_TEMPERATURE) * (novelty ** alpha)
    dynamic_temperature = min(MAX_TEMPERATURE, max(MIN_TEMPERATURE, dynamic_temperature))
    dynamic_top_p = TOP_P_MIN + (TOP_P_MAX - TOP_P_MIN) * (1.0 - coherence)
    dynamic_top_p = min(TOP_P_MAX, max(TOP_P_MIN, dynamic_top_p))

    query = re.sub(r"@\w+\b", "", text).strip()
    personal_msgs: List[Dict] = []
    try:
        summary = None
        if group_mode:
            g_sum = await redis.get(_k_g_sum(chat_id))
            if g_sum:
                if isinstance(g_sum, (bytes, bytearray)):
                    try:
                        g_sum = g_sum.decode("utf-8", "ignore")
                    except Exception:
                        g_sum = str(g_sum)
                summary = {"role": "system", "content": f"Summary: {g_sum}"}

        try:
            raw_personal = await load_context(chat_id, user_id)
        except asyncio.TimeoutError:
            logger.warning("load_context(chat,user) timeout for %s/%s", chat_id, user_id)
            raw_personal = []

        personal_msgs = [
            m for m in raw_personal
            if m.get("role") in ("system", "assistant")
            or m.get("user_id") == user_id
        ]
        personal_msgs = sorted(personal_msgs, key=lambda m: m.get("ts", 0))[-PERSONAL_WINDOW:]

        history: List[Dict] = [summary] if summary else []
        history.extend(personal_msgs)
    except Exception:
        logger.exception("Error building history for chat_id=%s user_id=%s", chat_id, user_id, exc_info=True)
        history = []

    if voice_in:
        history.insert(0, {
            "role": "system",
            "content": "The user's message was transcribed from a voice note they just sent. Do not ask them to send audio again; answer the transcribed content directly. You may briefly acknowledge that you received a voice note only if helpful."
        })

    if reply_to is not None:
        orig = await redis.get(f"msg:{chat_id}:{reply_to}")
        if orig:
            if isinstance(orig, (bytes, bytearray)):
                try:
                    orig = orig.decode("utf-8", "ignore")
                except Exception:
                    orig = str(orig)
            history.append({
                "role": "system",
                "content": f"[ReplyContext] The user replied to: «{orig}». Keep this in mind when replying to their current message."
            })

    is_emoji_only = bool(EMOJI_OR_SYMBOLS_ONLY.match(query))

    try:
        need_coref_flag = await needs_coref(query)
    except Exception as e:
        logger.warning("needs_coref failed: %s", e)
        need_coref_flag = False

    if not need_coref_flag or is_emoji_only:
        resolved = query
    else:
        try:
            resolved = await resolve_coref(query, history)
        except Exception as e:
            logger.warning("resolve_coref failed: %s", e)
            resolved = query

    safe_resolved = resolved

    push_allowed = True
    push_guard_key = None
    if msg_id is not None:
        try:
            ttl_days = getattr(settings, "MEMORY_TTL_DAYS", 3)
            push_guard_key = f"user_pushed:{chat_id}:{msg_id}"
            ok = await redis.set(
                push_guard_key,
                1,
                nx=True,
                ex=ttl_days * 86_400,
            )
            push_allowed = bool(ok)
        except Exception:
            logger.warning("user_pushed guard failed for chat_id=%s msg_id=%s", chat_id, msg_id, exc_info=True)

    try:
        if push_allowed:
            await push_message(chat_id, "user", safe_resolved, user_id=user_id)
    except Exception:
        logger.exception("push_message user failed for chat_id=%s", chat_id, exc_info=True)
        if push_guard_key:
            try:
                await redis.delete(push_guard_key)
            except Exception:
                pass
    finally:
        def _n(s: str) -> str:
            s = unicodedata.normalize("NFKC", s or "")
            s = re.sub(r"\s+", " ", s).strip()
            return s.casefold()

        _scan_source = personal_msgs if personal_msgs else history
        last_user_in_ctx = next(
            (m for m in reversed(_scan_source) if m.get("role") == "user"),
            None,
        )
        if not (
            last_user_in_ctx and _n(last_user_in_ctx.get("content", "")) == _n(resolved)
        ):
            history.append({"role": "user", "content": resolved})

    if is_channel_post:
        channel_desc = f"the {channel_title} channel" if channel_title else "the linked channel"
        header = {
            "role": "system",
            "content": (
                f"This message was forwarded from {channel_desc}.\n"
                "It is purely informational and not a direct user message.\n"
                "Write a brief, insightful, and concise comment.\n"
                "Do not introduce speculation or unrelated details; stay focused on the content provided."
            ),
        }
        history.insert(0, header)

    query_to_model = safe_resolved
    if draft_msg and is_channel_post:
        history.append({
            "role": "system",
            "content": f"INTERNAL_PLAN: {draft_msg}"
        })

    try:
        raw_mode = await redis.get(f"user_mode:{user_id}")
        if isinstance(raw_mode, bytes):
            raw_mode = raw_mode.decode()
        user_mode = raw_mode or "auto"
        mode_effective = "auto" if (group_mode or is_channel_post) else user_mode

        if mode_effective != "off_topic":
            on_topic_flag, on_topic_hits = await is_relevant(
                query_to_model, model=settings.EMBEDDING_MODEL,
                threshold=settings.RELEVANCE_THRESHOLD, return_hits=True
            )
        else:
            on_topic_flag, on_topic_hits = False, None
    except Exception:
        logger.exception("is_on_topic error for chat_id=%s", chat_id, exc_info=True)
        on_topic_flag, on_topic_hits = False, None

    logger.info("↳ build_system_prompt START chat=%s user=%s", chat_id, user_id)
    system_prompt = await build_system_prompt(
        persona,
        guidelines,
        user_gender=local_gender,
    )
    logger.info("↳ build_system_prompt END chat=%s user=%s (got %d chars)", chat_id, user_id, len(system_prompt.get("content", "")))

    if on_topic_flag:
        emb_model = settings.EMBEDDING_MODEL
        if emb_model not in _KB_ENTRIES:
            try:
                await _init_kb(emb_model)
            except Exception:
                logger.exception("Failed to init KB for %s", emb_model, exc_info=True)

        hits = on_topic_hits or []

        if not hits:
            messages = [
                {"role": "system", "content": system_prompt["content"]},
                *history,
            ]
            temperature = dynamic_temperature
            top_p = dynamic_top_p
            max_tokens = ON_TOPIC_MAX_TOKENS
        else:
            chunks = [h[2] for h in hits][:settings.KNOWLEDGE_TOP_K]
            snippets = "\n".join(f"{i+1}. {c}" for i, c in enumerate(chunks))

            user_prompt = (
                "Below are knowledge snippets relevant to the user's question.\n"
                "Respond to the user based on these knowledge snippets without adding false information.\n"
                "If knowledge snippets are written in the first-person style, use them in your responses in the first person as if they were your biography.\n"
                "______________\n"
                f"Snippets:\n{snippets}\n"
                "______________\n"
                "Your answer:"
            )
            messages = [
                {"role": "system", "content": system_prompt["content"]},
                *history,
                {"role": "system", "content": user_prompt},
            ]
            temperature = dynamic_temperature
            top_p = dynamic_top_p
            max_tokens = ON_TOPIC_MAX_TOKENS

    else:
        emb_model = settings.OFFTOPIC_EMBEDDING_MODEL
        if emb_model not in _KB_ENTRIES:
            try:
                await _init_kb(emb_model)
            except Exception:
                logger.exception("Failed to init OFFTOPIC KB for %s", emb_model, exc_info=True)

        logger.info("▶ Step: off-topic relevance gate")
        use_rag_off, hits = await is_relevant(
            query_to_model,
            model=settings.OFFTOPIC_EMBEDDING_MODEL,
            threshold=settings.OFFTOPIC_RELEVANCE_THRESHOLD,
            return_hits=True,
        )
        if not use_rag_off or not hits:
            messages = [
                {"role": "system", "content": system_prompt["content"]},
                *history,
            ]
            temperature = dynamic_temperature
            top_p = dynamic_top_p
            max_tokens = OFF_TOPIC_MAX_TOKENS
        else:
            chunks = [h[2] for h in hits][:settings.KNOWLEDGE_TOP_K]
            snippets = "\n".join(f"{i+1}. {c}" for i, c in enumerate(chunks))

            user_prompt = (
                "Below are knowledge snippets relevant to the user's question.\n"
                "Respond to the user based on these knowledge snippets without adding false information.\n"
                "If knowledge snippets are written in the first-person style, use them in your responses in the first person as if they were your biography.\n"
                "______________\n"
                f"Snippets:\n{snippets}\n\n"
                "______________\n"
                "Your answer:"
            )
            messages = [
                {"role": "system", "content": system_prompt["content"]},
                *history,
                {"role": "system", "content": user_prompt},
            ]
            temperature = dynamic_temperature
            top_p = dynamic_top_p
            max_tokens = OFF_TOPIC_MAX_TOKENS

    try:
        resp = await asyncio.wait_for(
            _call_openai_with_retry(
                model=settings.RESPONSE_MODEL,
                messages=messages,
                max_completion_tokens=max_tokens,
                temperature=temperature,
                top_p=top_p,
                frequency_penalty=0.4,
                presence_penalty=0.25,
            ),
            timeout=30.0,
        )
    except (ClientError, asyncio.TimeoutError, ValueError):
        logger.exception("OpenAI chat error", exc_info=True)
        reply = "I’m sorry, something went wrong."
    except Exception:
        logger.exception("Unexpected error in OpenAI chat", exc_info=True)
        reply = "I’m sorry, something went wrong."
    else:
        logger.info("   ↳ OpenAI chat END (t=%.3fs)", time.time() - t0)
        reply = resp.choices[0].message.content.strip() if getattr(resp, "choices", None) else ""
        reply = re.sub(
            r"(?m)^\s*\[(?:имя|name)\s*:[^\]]*\]\s*\n?",
            "", reply, flags=re.I).strip()

    assistant_allowed = True
    assistant_guard_key = None
    if msg_id is not None:
        try:
            ttl_days = getattr(settings, "MEMORY_TTL_DAYS", 3)
            assistant_guard_key = f"assistant_pushed:{chat_id}:{msg_id}"
            ok2 = await redis.set(
                assistant_guard_key,
                1,
                nx=True,
                ex=ttl_days * 86_400,
            )
            assistant_allowed = bool(ok2)
        except Exception:
            logger.warning("assistant_pushed guard failed for chat_id=%s msg_id=%s", chat_id, msg_id, exc_info=True)

    try:
        if assistant_allowed:
            await push_message(chat_id, "assistant", reply, user_id=user_id)
    except Exception:
        logger.exception("push_message assistant failed for chat_id=%s", chat_id, exc_info=True)
        if assistant_guard_key:
            try:
                await redis.delete(assistant_guard_key)
            except Exception:
                pass

    logger.info("✔ respond_to_user END   chat=%s user=%s dt=%.2fs",
                chat_id, user_id, time.time() - t0)
    return reply
EOF
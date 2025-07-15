cat >app/services/responder/core.py<< EOF
#app/services/responder/core.py
from __future__ import annotations

import logging, asyncio, hashlib, json, re, time

from typing import Dict, List
from aiohttp import ClientError

from app.clients.openai_client import _call_openai_with_retry
from app.config import settings
from app.core.memory import (
    load_context, push_message, 
    get_redis, get_cached_gender, cache_gender
)
from app.emo_engine import get_persona
from .prompt_builder import build_system_prompt
from .coref import needs_coref, resolve_coref
from .gender import detect_gender
from .rag import (
    get_relevant, _KB_ENTRIES, _init_kb, 
    is_on_topic, relevant_enough
)

logger = logging.getLogger(__name__)

ON_TOPIC_MAX_TOKENS = 800
OFF_TOPIC_MAX_TOKENS = 600
MAX_TEMPERATURE = 0.8
MIN_TEMPERATURE = 0.6
TOP_P_MIN = 0.8
TOP_P_MAX = 1.0
EMOJI_OR_SYMBOLS_ONLY = re.compile(r'^[\W_]+$', flags=re.UNICODE)

async def respond_to_user(text: str, chat_id: int, user_id: int) -> str:

    start_ts = time.time()
    redis = get_redis()

    gender = await get_cached_gender(user_id)

    if gender is None:
        ui = await redis.hgetall(f"tg_user:{user_id}")
        name = ui.get("first_name") or ui.get("username") or ""
        gender = await detect_gender(name, text)
        await cache_gender(user_id, gender)

    persona = get_persona(chat_id)
    if gender in ("male", "female"):
        persona.user_gender = gender

    await persona.process_interaction(user_id, text)
    guidelines = await persona.style_guidelines(user_id)
    #logger.debug("GUIDELINES → %r", guidelines)

    mods = getattr(persona, "_mods_cache", persona.style_modifiers())
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
    try:
        history = await load_context(chat_id)
    except Exception:
        logger.exception("load_context failed for chat_id=%s", chat_id)
        history = []

    is_emoji_only = bool(EMOJI_OR_SYMBOLS_ONLY.match(query))
    try:
        need_coref = await needs_coref(query)
    except Exception as e:
        logger.warning("needs_coref failed: %s", e)
        need_coref = False

    if not need_coref or is_emoji_only:
        resolved = query
    else:
        try:
            resolved = await resolve_coref(query, history)
        except Exception as e:
            logger.warning("resolve_coref failed: %s", e)
            resolved = query

    safe_resolved = resolved

    try:
        await push_message(chat_id, "user", safe_resolved, user_id)
        history.append({"role": "user", "content": resolved})
    except Exception:
        logger.exception("push_message user failed for chat_id=%s", chat_id)

    query_to_model = safe_resolved

    norm = lambda s: re.sub(r"[\W\s]+", "", s.lower())
    last_key = f"last_req:{chat_id}"
    prev_hash = await redis.get(last_key)
    cur_hash = hashlib.sha1(norm(query_to_model).encode()).hexdigest()
    is_repeat = (prev_hash == cur_hash)
    await redis.set(last_key, cur_hash, ex=300)

    try:
        raw_mode = await redis.get(f"user_mode:{user_id}")
        if isinstance(raw_mode, bytes):
            raw_mode = raw_mode.decode()
        user_mode = raw_mode or "auto"
        if user_mode != "off_topic":
            on_topic_flag, on_topic_hits = await is_on_topic(query_to_model)
        else:
            on_topic_flag, on_topic_hits = False, None
    except Exception:
        logger.exception("is_on_topic error for chat_id=%s", chat_id)
        on_topic_flag, on_topic_hits = False, None

    system_prompt = build_system_prompt(persona, guidelines)

    if on_topic_flag:
        emb_model = settings.EMBEDDING_MODEL
        if emb_model not in _KB_ENTRIES:
            try:
                await _init_kb(emb_model)
            except Exception:
                logger.exception("Failed to init KB for %s", emb_model)

        if on_topic_hits is not None:
            hits = on_topic_hits
        else:
            cache_key = "rag:" + hashlib.sha256(f"{emb_model}:{query_to_model}".encode()).hexdigest()
            raw = await redis.get(cache_key) if redis else None
            if raw:
                hits = json.loads(raw)
            else:
                hits = await get_relevant(query_to_model, model_name=emb_model)
                if redis:
                    await redis.set(cache_key, json.dumps(hits), ex=3600)

        top_score = hits[0][0] if hits else 0.0
        logger.debug("on_topic: %d hits, top_score=%0.4f", len(hits), top_score)

        meta_entries = _KB_ENTRIES.get(emb_model, [])
        meta_map = {e["id"]: e for e in meta_entries}
        filtered_snippets: List[str] = []
        q_lower = query_to_model.lower()
        for score, did, chunk in hits:
            entry = meta_map.get(did)
            if entry and (
                any(tag.lower() in q_lower for tag in entry.get("tags", []))
                or entry.get("category", "").lower() in q_lower
            ):
                filtered_snippets.append(chunk)

        chunks = (filtered_snippets or [h[2] for h in hits])[:settings.KNOWLEDGE_TOP_K]
        snippets = "\n".join(f"{i+1}. {c}" for i, c in enumerate(chunks))

        user_prompt = (
            "Below are knowledge snippets relevant to the user question. "
            "Respond to the user based on these knowledge snippets without adding false information. "
            "If knowledge snippets are written in the first-person style, use them in your responses in the first person as if they were your biography.\n\n"
            f"User question:\n{query_to_model}\n\n"
            f"Snippets:\n{snippets}\n\n"
            "Your answer:"
        )
        messages = [
            {"role": "system", "content": system_prompt["content"]},
            *history,
            {"role": "system", "content": user_prompt},
        ]
        if is_repeat:
            messages.append({
                "role": "system",
                "content": "Your interlocutor wrote the 100% same message as last time. Find a creative way to resolve the situation."
            })
        temperature = dynamic_temperature
        top_p = dynamic_top_p
        max_tokens = ON_TOPIC_MAX_TOKENS


    else:
        emb_model = settings.OFFTOPIC_EMBEDDING_MODEL
        if emb_model not in _KB_ENTRIES:
            await _init_kb(emb_model)

        cache_key = "rag_off:" + hashlib.sha256(f"{emb_model}:{query_to_model}".encode()).hexdigest()
        raw = await redis.get(cache_key) if redis else None
        if raw:
            hits = json.loads(raw)
        else:
            hits = await get_relevant(query_to_model, model_name=emb_model)
            if redis:
                await redis.set(cache_key, json.dumps(hits), ex=3600)

        use_rag_off = await relevant_enough(
            query_to_model,
            settings.OFFTOPIC_EMBEDDING_MODEL,
            settings.OFFTOPIC_RELEVANCE_THRESHOLD,
            hits=hits,
        )
        if not use_rag_off:
            messages = [
                {"role": "system", "content": system_prompt["content"]},
                *history,
                {"role": "user",   "content": query_to_model},
            ]
            temperature = dynamic_temperature
            top_p = dynamic_top_p
            max_tokens = OFF_TOPIC_MAX_TOKENS
        else:
            meta_entries = _KB_ENTRIES.get(emb_model, [])
            meta_map = {e["id"]: e for e in meta_entries}
            filtered_snippets: List[str] = []
            q_lower = query_to_model.lower()
            for score, did, chunk in hits:
                entry = meta_map.get(did)
                if entry and (
                    any(tag.lower() in q_lower for tag in entry.get("tags", []))
                    or entry.get("category", "").lower() in q_lower
                ):
                    filtered_snippets.append(chunk)

            chunks = (filtered_snippets or [h[2] for h in hits])[:settings.KNOWLEDGE_TOP_K]
            snippets = "\n".join(f"{i+1}. {c}" for i, c in enumerate(chunks))

            user_prompt = (
                "Below are knowledge snippets relevant to the user question. "
                "Respond to the user based on these knowledge snippets without adding false information. "
                "If knowledge snippets are written in the first-person style, use them in your responses in the first person as if they were your biography.\n\n"
                f"User question:\n{query_to_model}\n\n"
                f"Snippets:\n{snippets}\n\n"
                "Your answer:"
            )
            messages = [
                {"role": "system", "content": system_prompt["content"]},
                *history,
                {"role": "system", "content": user_prompt},
            ]
            if is_repeat:
                messages.append({
                    "role": "system",
                    "content": "Your interlocutor wrote the 100% same message as last time. Find a creative way to resolve the situation."
                })
            temperature = dynamic_temperature
            top_p = dynamic_top_p
            max_tokens = OFF_TOPIC_MAX_TOKENS

    try:
        resp = await asyncio.wait_for(
            _call_openai_with_retry(
                model=settings.RESPONSE_MODEL,
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature,
                top_p=top_p,
            ),
            timeout=60.0,
        )
        reply = resp.choices[0].message.content.strip() if getattr(resp, "choices", None) else ""
        reply = re.sub(r"\[[^\]\n]{0,60}(имя|name)[^\]]*\]", "", reply, flags=re.I).strip()
    except (ClientError, asyncio.TimeoutError, ValueError):
        logger.exception("OpenAI chat error")
        reply = "I’m sorry, something went wrong."
    except Exception:
        logger.exception("Unexpected error in OpenAI chat")
        reply = "I’m sorry, something went wrong."

    await push_message(chat_id, "assistant", reply)
    return reply
EOF
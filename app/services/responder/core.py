cat >app/services/responder/core.py<< 'EOF'
#app/services/responder/core.py
from __future__ import annotations

import logging
import asyncio
import time
import json
import hashlib
import math
import re
import unicodedata

from typing import Dict, List
from aiohttp import ClientError
from datetime import datetime, timedelta

from app.clients.openai_client import _call_openai_with_retry, _msg, _get_output_text
from app.config import settings
from app.core.memory import (
    load_context, push_message,
    get_redis, get_cached_gender, cache_gender,
    _k_g_sum, _k_p_sum, _k_g_sum_u,
    extract_summary_data, pack_summary_data,
    set_summary_if_newer, MEMORY_TTL,
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
}


def _b2s(x) -> str:
    if x is None:
        return ""
    if isinstance(x, (bytes, bytearray)):
        try:
            return x.decode("utf-8", "ignore")
        except Exception:
            return ""
    return x if isinstance(x, str) else str(x)

def _sha1_u(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", "ignore")).hexdigest()

def _cosine(u: List[float], v: List[float]) -> float:
    if not u or not v or len(u) != len(v):
        return 0.0
    dot = 0.0; nu = 0.0; nv = 0.0
    for a, b in zip(u, v):
        dot += a * b; nu += a * a; nv += b * b
    if nu <= 0 or nv <= 0:
        return 0.0
    return dot / (math.sqrt(nu) * math.sqrt(nv))

async def _embed_texts(texts: List[str], model: str) -> List[List[float]]:

    redis = get_redis()
    outs: List[List[float]] = []

    to_query: List[tuple[int, str]] = []
    cached: Dict[int, List[float]] = {}
    for i, t in enumerate(texts):
        t_norm = (t or "").strip()
        key = f"emb:{model}:{_sha1_u(t_norm)}"
        try:
            raw = await redis.get(key)
            if raw:
                cached[i] = json.loads(raw)
            else:
                to_query.append((i, t_norm))
        except Exception:
            to_query.append((i, t_norm))

    if to_query:
        try:
            ordered_idx = [idx for idx, _ in to_query]
            payload = [txt for _, txt in to_query]
            resp = await _call_openai_with_retry(
                model=model,
                endpoint="embeddings.create",
                input=payload,
            )
            vecs = [d.embedding for d in getattr(resp, "data", [])]
            for (i, t_norm), vec in zip(to_query, vecs):
                cached[i] = vec
                try:
                    key = f"emb:{model}:{_sha1_u(t_norm)}"
                    await redis.set(key, json.dumps(vec, separators=(",", ":")), ex=86_400)
                except Exception:
                    pass
        except Exception:
            for i, _ in to_query:
                cached[i] = []

    for i in range(len(texts)):
        outs.append(cached.get(i, []))
    return outs

def _hget(d: dict, key: str):
    if d is None:
        return None
    v = d.get(key)
    if v is None:
        try:
            v = d.get(key.encode())
        except Exception:
            v = None
    return v

def _compact_for_llm(msgs: List[Dict]) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    for m in msgs:
        try:
            role = (m.get("role") or "").strip()
            if role not in ("system", "user", "assistant"):
                continue
            c = m.get("content", "")
            if not isinstance(c, str):
                c = str(c) if c is not None else ""
            c = unicodedata.normalize("NFKC", c)
            c = re.sub(r"\s+", " ", c).strip()
            if not c:
                continue
            out.append({"role": role, "content": c})
        except Exception:
            continue
    return out

def _mk_kb_prompt(chunks: List[str]) -> str:
    snippets = "\n".join(f"{i+1}. {c}" for i, c in enumerate(chunks))
    return (
        "Below are knowledge snippets relevant to the user's query.\n"
        "Respond to the user based on these knowledge snippets without adding false information.\n"
        "If knowledge snippets are written in the first-person style, use them in your responses in the first person as if they were your biography.\n"
        "______________\n"
        f"Snippets:\n{snippets}\n"
        "______________\n"
        "Your answer:"
    )

def _get_last_assistant_text(history_msgs: List[Dict]) -> str | None:
    for m in reversed(history_msgs):
        if (m.get("role") == "assistant"):
            txt = (m.get("content") or "").strip()
            if txt:
                return txt
    return None

def _build_responses_messages(
    system_prompt_content: str,
    history_llm: List[Dict[str, str]],
    *,
    kb_chunks: List[str] | None = None,
    user_text: str | None = None,
    image_data_url: str | None = None,
    include_image_hint: bool = False,
) -> List[Dict]:

    sys_blocks = [{"type": "input_text", "text": system_prompt_content}]
    if include_image_hint:
        sys_blocks.append({
            "type": "input_text",
            "text": (
                "The user sent you an image. "
                "Analyze this image: it is part of your dialogue with this user. "
                "Respond naturally as a continuation of the conversation."
            ),
        })

    messages: List[Dict] = [{"role": "system", "content": sys_blocks}]

    for m in history_llm:
        role = m["role"]
        text = m["content"]
        part_type = "output_text" if role == "assistant" else "input_text"
        messages.append({
            "role": role,
            "content": [{"type": part_type, "text": text}],
        })

    if kb_chunks:
        kb_prompt = _mk_kb_prompt(kb_chunks)
        messages.append({
            "role": "system",
            "content": [{"type": "input_text", "text": kb_prompt}],
        })

    curr: List[Dict] = []
    if (user_text or "").strip():
        curr.append({"type": "input_text", "text": user_text})
    if image_data_url:
        curr.append({"type": "input_image", "image_url": image_data_url})
    if curr:
        messages.append({"role": "user", "content": curr})

    return messages

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
    image_b64: str | None = None,
    image_mime: str | None = None,
    allow_web: bool = False,
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
        logger.debug("▶ respond_to_user: waiting for persona._restored_evt (with timeout)")
        await asyncio.wait_for(persona._restored_evt.wait(), timeout=5.0)
        logger.debug("▶ respond_to_user: persona._restored_evt is set, continue processing")
    except asyncio.TimeoutError:
        logger.warning("persona._restored_evt.wait() timed out — continue without hard fail")
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
            if gender in ("male", "female"):
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

    draft_msg = None
    if getattr(settings, "ENABLE_INTERNAL_PLAN", False):
        try:
            reasoning_model = settings.REASONING_MODEL if mods.get("technical_mod", 0) > 0.6 else settings.BASE_MODEL
            resp = await asyncio.wait_for(
                _call_openai_with_retry(
                    endpoint="responses.create",
                    model=reasoning_model,
                    input=[
                        _msg("system", "You think out loud. Make a short plan of your answer in points."),
                        _msg("user", text),
                    ],
                    max_output_tokens=300,
                    temperature=0,
                ),
                timeout=60.0,
            )
            draft_msg = (_get_output_text(resp) or "").strip()
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

    query = re.sub(r"(?<!\S)@\w+\b", "", text).strip()
    if getattr(settings, "LLM_AUDIT", False):
        logger.info("[TRACE] raw=%r query=%r", text[:200], query[:200])
    personal_msgs: List[Dict] = []
    try:
        summary_group = None
        if group_mode:
            g_raw = await redis.get(_k_g_sum(chat_id))
            g_sum = extract_summary_data(g_raw) if g_raw else ""
            if g_sum:
                summary_group = {"role": "system", "content": f"Summary: {g_sum}"}

        try:
            raw_personal = await load_context(chat_id, user_id)
        except asyncio.TimeoutError:
            logger.warning("load_context(chat,user) timeout for %s/%s", chat_id, user_id)
            raw_personal = []

        personal_msgs = []
        for m in raw_personal:
            r = m.get("role")
            uid = m.get("user_id")
            if r == "system":
                personal_msgs.append(m)
            elif r == "assistant" and uid == user_id:
                personal_msgs.append(m)
            elif r == "user" and uid == user_id:
                personal_msgs.append(m)
        personal_msgs = sorted(personal_msgs, key=lambda m: m.get("ts", 0))

        history: List[Dict] = []
        if summary_group:
            history.append(summary_group)
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
        orig = _b2s(await redis.get(f"msg:{chat_id}:{reply_to}"))
        if orig:
            history.append({
                "role": "system",
                "content": f"[ReplyContext] The user replied to: «{orig}». Keep this in mind when replying to their current message."
            })
    elif chat_id != user_id:
        try:
            lp = await redis.hgetall(f"last_ping:{chat_id}:{user_id}") or {}
        except Exception:
            lp = {}
        if lp:
            ts_raw = _b2s(_hget(lp, "ts")) or "0"
            try:
                lp_ts = int(ts_raw or 0)
            except Exception:
                lp_ts = 0
            if lp_ts and (time.time() - lp_ts) < getattr(settings, "GROUP_PING_ACTIVE_TTL_SECONDS", 3600):
                txt = _b2s(_hget(lp, "text"))
                if txt:
                    history.append({
                        "role": "system",
                        "content": f"[ReplyContext] Your recent message was: «{txt}». The user may be answering that ping; keep continuity."
                    })

    pre_emoji_only = bool(EMOJI_OR_SYMBOLS_ONLY.match((query or "").strip()))
    try:
        need_coref_flag = await needs_coref(query)
    except Exception as e:
        logger.warning("needs_coref failed: %s", e)
        need_coref_flag = False

    if not need_coref_flag or pre_emoji_only:
        resolved = query
    else:
        try:
            resolved = await resolve_coref(query, history)
            if getattr(settings, "LLM_AUDIT", False) and resolved != query:
                logger.info("[TRACE] coref: %r -> %r", query[:200], resolved[:200])
        except Exception as e:
            logger.warning("resolve_coref failed: %s", e)
            resolved = query

    safe_resolved = resolved
    is_emoji_only = bool(EMOJI_OR_SYMBOLS_ONLY.match((safe_resolved or "").strip()))
    is_empty_after_strip = not (safe_resolved or "").strip()

    if is_empty_after_strip and not is_channel_post and not image_b64:
        logger.info("Empty message after stripping mentions/whitespace — skip responding.")
        logger.info("✔ respond_to_user END   chat=%s user=%s dt=%.2fs",
                    chat_id, user_id, time.time() - t0)
        return ""

    push_allowed = True
    push_guard_key = None

    if msg_id is not None:
        try:
            ttl_days = getattr(settings, "MEMORY_TTL_DAYS", 7)
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
        if push_allowed and not is_channel_post:
            if image_b64:
                marker = "[Image attached]" + (f" {safe_resolved}" if not is_empty_after_strip else "")
                await push_message(chat_id, "user", marker, user_id=user_id)
            elif not is_empty_after_strip:
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
        if not is_channel_post and not is_empty_after_strip and not image_b64:
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
        if safe_resolved:
            history.append({
                "role": "user", "content": safe_resolved
            })

    query_to_model = safe_resolved
    if draft_msg and is_channel_post:
        history.append({
            "role": "system",
            "content": f"INTERNAL_PLAN: {draft_msg}"
        })

    try:
        raw_mode = await redis.get(f"user_mode:{user_id}")
        if isinstance(raw_mode, (bytes, bytearray)):
            raw_mode = raw_mode.decode()
        user_mode = (raw_mode or "auto").strip().lower()
        mode_effective = "auto" if (group_mode or is_channel_post) else user_mode

        on_topic_flag, on_topic_hits = (False, None)
        off_topic_flag, off_topic_hits = (False, None)

        q_ok = bool((query_to_model or "").strip())
        if q_ok:
            if mode_effective == "on_topic":
                on_topic_flag, on_topic_hits = await is_relevant(
                    query_to_model,
                    model=settings.EMBEDDING_MODEL,
                    threshold=settings.RELEVANCE_THRESHOLD,
                    return_hits=True,
                )
            elif mode_effective == "off_topic":
                off_topic_flag, off_topic_hits = await is_relevant(
                    query_to_model,
                    model=settings.OFFTOPIC_EMBEDDING_MODEL,
                    threshold=settings.OFFTOPIC_RELEVANCE_THRESHOLD,
                    return_hits=True,
                )
            else:
                on_topic_flag, on_topic_hits = await is_relevant(
                    query_to_model,
                    model=settings.EMBEDDING_MODEL,
                    threshold=settings.RELEVANCE_THRESHOLD,
                    return_hits=True,
                )
                if not on_topic_flag:
                    off_topic_flag, off_topic_hits = await is_relevant(
                        query_to_model,
                        model=settings.OFFTOPIC_EMBEDDING_MODEL,
                        threshold=settings.OFFTOPIC_RELEVANCE_THRESHOLD,
                        return_hits=True,
                    )
    except Exception:
        logger.exception("is_on_topic error for chat_id=%s", chat_id, exc_info=True)
        on_topic_flag = off_topic_flag = False

    logger.info("↳ build_system_prompt START chat=%s user=%s", chat_id, user_id)
    system_prompt_content = await build_system_prompt(
        persona,
        guidelines,
        user_gender=local_gender,
    )
    if not (system_prompt_content or "").strip():
        system_prompt_content = "You are a helpful assistant. Keep replies concise and accurate."
    logger.info("↳ build_system_prompt END chat=%s user=%s (got %d chars)", chat_id, user_id, len(system_prompt_content))

    def _approx_tokens(s: str) -> int:
        cpt = getattr(settings, "APPROX_CHARS_PER_TOKEN", 3.5)
        try:
            cpt = float(cpt)
            if not (0.5 <= cpt <= 10):
                cpt = 3.5
        except Exception:
            cpt = 3.5
        return max(1, int(len(s) / max(1e-9, cpt)))

    def _count_tokens(msgs: List[Dict[str, str]]) -> int:
        tot = 0
        for m in msgs:
            tot += _approx_tokens(m.get("content", "")) + 6
        return tot

    async def _summarize_chunk(prev_summary: str, msgs_chunk: List[Dict[str, str]]) -> str:
        def _as_json_or_empty(s: str) -> str:
            if not s:
                return "{}"
            try:
                json.loads(s)
                return s
            except Exception:
                return "{}"
        lines = []
        for m in msgs_chunk:
            r = (m.get("role") or "").strip()
            content = (m.get("content") or "").strip()
            if r == "system" and (
                content.startswith("Summary:") or
                content.startswith("INTERNAL_PLAN:") or
                content.startswith("The user's message was transcribed from a voice note") or
                content.startswith("[ReplyContext]") or
                content.startswith("This message was forwarded from")
            ):
                continue
            speaker = "Assistant" if r == "assistant" else ("System" if r == "system" else "User")
            if content:
                lines.append(f"{speaker}: {content}")
        body = "\n".join(lines)
        system_prompt = (
            "You update a structured rolling conversation memory.\n"
            "Return ONLY minified JSON that EXACTLY matches the provided schema. "
            "No prose, no markdown, no code fences.\n"
            "Rules:\n"
            "- Preserve concrete numbers, dates, names, links, and identifiers.\n"
            "- Keep only durable information; drop chit-chat and ephemeral details.\n"
            "- Facts are short atomic strings; decisions are commitments with who/when; "
            "open_questions are unresolved questions; todos are explicit action items; "
            "preferences are stable user likes/constraints; entities are names/ids.\n"
            "- Merge with the previous summary and deduplicate.\n"
            "- If a field has nothing to add, keep it as an empty string/array per schema.\n"
        )
        user_prompt = (
            "PREVIOUS_SUMMARY_JSON:\n"
            f"{_as_json_or_empty(prev_summary)}\n\n"
            "NEW_MESSAGES:\n"
            f"{body}\n\n"
            "Return ONLY a single minified JSON object."
        )
        summary_schema = {
            "type": "object",
            "properties": {
                "topic": {"type": "string"},
                "facts": {"type": "array", "items": {"type": "string"}},
                "decisions": {"type": "array", "items": {"type": "string"}},
                "open_questions": {"type": "array", "items": {"type": "string"}},
                "todos": {"type": "array", "items": {"type": "string"}},
                "preferences": {"type": "array", "items": {"type": "string"}},
                "entities": {"type": "array", "items": {"type": "string"}}
            },
            "required": ["topic","facts","decisions","open_questions","todos","preferences","entities"],
            "additionalProperties": False,
        }
        try:
            resp = await asyncio.wait_for(
                _call_openai_with_retry(
                    endpoint="responses.create",
                    model=settings.REASONING_MODEL,
                    instructions=system_prompt,
                    input=user_prompt,
                    text={
                        "format": {
                            "type": "json_schema",
                            "name": "rolling_summary",
                            "schema": summary_schema,
                            "strict": True
                        }
                    },
                    temperature=0,
                    max_output_tokens=800,
                ),
                timeout=60.0,
            )
            return (_get_output_text(resp) or "{}").strip()
        except Exception:
            logger.exception("inline summarization failed")
            return prev_summary or "{}"

    async def _ensure_budget(
        history_msgs: List[Dict[str, str]],
        extra_overhead_tokens: int = 0,
        ref_text: str | None = None,
    ) -> List[Dict[str, str]]:

        sys_block = [{"role": "system", "content": system_prompt_content}]
        budget = int(getattr(settings, "CONTEXT_TOKEN_BUDGET", 8192))
        reserve = int(getattr(settings, "RESPONSES_TOKEN_RESERVE", 1024))
        reserve = max(1, min(reserve, max(1, budget - 1)))
        prompt_budget = max(1, budget - reserve)
        anchors = max(0, int(getattr(settings, "ANCHOR_TURNS", 8)))

        def _fits(msgs: List[Dict[str, str]]) -> bool:
            return (_count_tokens(sys_block + msgs) + extra_overhead_tokens) <= prompt_budget

        if _fits(history_msgs):
            return history_msgs

        ts_now = time.time()
        GAP_SEC = 8 * 60
        cand_indices = []
        for i in range(1, len(history_msgs) - 1):
            prev = history_msgs[i - 1]; cur = history_msgs[i]
            if prev.get("role") == "assistant":
                cand_indices.append(i)
                continue
            try:
                t0 = float(prev.get("ts", ts_now)); t1 = float(cur.get("ts", ts_now))
                if (t1 - t0) >= GAP_SEC:
                    cand_indices.append(i); continue
            except Exception:
                pass
        try:
            start = max(1, len(history_msgs) - (anchors + 12))
            for i in range(start, max(1, len(history_msgs) - anchors)):
                cand_indices.append(i)
        except Exception:
            pass
        cand_indices = sorted(set(cand_indices))
        if not cand_indices:
            cand_indices = [max(1, len(history_msgs) - anchors)]

        USE_EMBED_SEG = not bool(int(getattr(settings, "DISABLE_INLINE_BUDGET_EMBED", "1")))

        redis = get_redis()
        is_private = chat_id == user_id
        sum_key = _k_p_sum(user_id) if is_private else _k_g_sum_u(chat_id, user_id)
        prev_summary = extract_summary_data((await redis.get(sum_key)) or "")

        best = None
        if USE_EMBED_SEG and ref_text and not best:
            anchors_tail = max(1, anchors)
            protected = set([0])
            for j in range(max(0, len(history_msgs) - anchors_tail), len(history_msgs)):
                protected.add(j)

            candidates = [i for i in cand_indices if i not in protected and 0 <= i < len(history_msgs)-1]

            ref_vec = (await _embed_texts([ref_text[:1000]], settings.EMBEDDING_MODEL))[0]
            if ref_vec:
                cand_texts = []
                for i in candidates:
                    content = (history_msgs[i].get("content") or "").strip()
                    cand_texts.append(content[:1000])
                cand_vecs = await _embed_texts(cand_texts, settings.EMBEDDING_MODEL) if cand_texts else []

                scored = []
                for idx, vec in zip(candidates, cand_vecs):
                    sim = _cosine(ref_vec, vec) if vec else -1.0
                    scored.append((sim, idx))
                scored.sort(key=lambda x: x[0])

                kept = list(history_msgs)
                for _sim, rm_idx in sorted(scored, key=lambda x: x[1], reverse=True):
                    if 0 <= rm_idx < len(kept):
                        del kept[rm_idx]
                        if _fits(kept):
                            best = (0.9, rm_idx, prev_summary)
                            history_msgs = kept
                            break
        if not best:
            for idx in reversed(cand_indices):
                head = history_msgs[:idx]
                tail = history_msgs[idx:]
                summary_json = await _summarize_chunk(prev_summary, head)
                new_hist = [{"role": "system", "content": f"Summary: {summary_json}"}] + tail[-anchors:]
                if _fits(new_hist):
                    best = (1.0, idx, summary_json)
                    break

        if not best:
            idx = max(0, len(history_msgs) - anchors)
            head = history_msgs[:idx]; tail = history_msgs[idx:]
            summary_json = await _summarize_chunk(prev_summary, head)
            best = (1.0, idx, summary_json)

        _, cut_idx, final_summary = best
        try:
            await set_summary_if_newer(sum_key, pack_summary_data(final_summary), MEMORY_TTL)
        except Exception:
            logger.warning("Failed to persist rolling summary (set_if_newer)")

        new_hist = [{"role": "system", "content": f"Summary: {final_summary}"}] + history_msgs[cut_idx:][-anchors:]
        while not _fits(new_hist) and len(new_hist) > 2:
            head2 = new_hist[1: 1 + max(1, (len(new_hist) - 1) // 2)]
            tail2 = new_hist[1 + max(1, (len(new_hist) - 1) // 2):]
            final_summary = await _summarize_chunk(final_summary, head2)
            new_hist = [{"role": "system", "content": f"Summary: {final_summary}"}] + tail2
        if not _fits(new_hist):
            try:
                tiny = await _summarize_chunk("{}", [{"role": "system", "content": f"{final_summary}"}])
                new_hist = [{"role":"system","content": f"Summary: {tiny}"}]
            except Exception:
                new_hist = [{"role":"system","content": f"Summary: {final_summary}"}]
        return new_hist

    def _safe_max_tokens(suggested: int) -> int:
        try:
            reserve = int(getattr(settings, "RESPONSES_TOKEN_RESERVE", suggested))
        except Exception:
            reserve = suggested
        margin = max(16, min(64, reserve // 8))
        clamped = max(1, min(int(suggested), max(1, reserve - margin)))
        if clamped < int(suggested):
            logger.debug("max_output_tokens clamped: suggested=%s, result=%s, reserve=%s, margin=%s",
                         suggested, clamped, reserve, margin)
        return clamped

    top_k = int(getattr(settings, "KNOWLEDGE_TOP_K", 3))
    hits = []
    emb_model = None
    if on_topic_flag and on_topic_hits:
        emb_model = settings.EMBEDDING_MODEL
        hits = (on_topic_hits or [])[:top_k]
        logger.info("RAG: using ON-TOPIC KB (hits=%d)", len(hits))
    elif off_topic_flag and off_topic_hits:
        emb_model = settings.OFFTOPIC_EMBEDDING_MODEL
        hits = (off_topic_hits or [])[:top_k]
        logger.info("RAG: using OFF-TOPIC KB (hits=%d)", len(hits))
    else:
        logger.info("RAG: disabled for this turn (no similarity pass)")

    extra_overhead = 0
    if hits:
        rag_snippets_text = "\n".join(f"{i+1}. {h[2]}" for i, h in enumerate(hits))
        extra_overhead += _approx_tokens(
            "Below are knowledge snippets relevant to the user's question.\n"
            + rag_snippets_text + "\nYour answer:"
        ) + 12

    if emb_model:
        if emb_model not in _KB_ENTRIES:
            try:
                await _init_kb(emb_model)
            except Exception:
                logger.exception("Failed to init KB for %s", emb_model, exc_info=True)

    temperature = dynamic_temperature
    top_p = dynamic_top_p

    if emb_model is None:
        max_tokens = _safe_max_tokens(ON_TOPIC_MAX_TOKENS)
    else:
        max_tokens = _safe_max_tokens(
            ON_TOPIC_MAX_TOKENS if emb_model == settings.EMBEDDING_MODEL
            else OFF_TOPIC_MAX_TOKENS
        )

    resp = None
    reply = None
    try:
        if reply_to is None and chat_id == user_id:
            last_assist_txt = _get_last_assistant_text(history)
            if not last_assist_txt:
                try:
                    lp = await redis.hgetall(f"last_ping:pm:{user_id}") or {}
                except Exception:
                    lp = {}
                if lp:
                    ts_raw = _b2s(_hget(lp, "ts")) or "0"
                    try:
                        lp_ts = int(ts_raw or 0)
                    except Exception:
                        lp_ts = 0
                    if lp_ts and (time.time() - lp_ts) < getattr(settings, "PERSONAL_PING_RETENTION_SECONDS", 86_400):
                        txt = (_b2s(_hget(lp, "text")) or "").strip()
                        if txt:
                            last_assist_txt = txt
            if last_assist_txt:
                history.append({
                    "role": "system",
                    "content": f"[ReplyContext] Your recent message was: «{last_assist_txt}». The user may be answering it; keep continuity."
                })

        if image_b64:

            data_url = f"data:{(image_mime or 'image/jpeg')};base64,{image_b64}"

            kb_prompt = None; overhead = 0
            chunks = None
            if hits:
                chunks = [h[2] for h in hits]
                kb_prompt = _mk_kb_prompt(chunks)
                overhead = _approx_tokens(kb_prompt) + 12

            before = len(history)
            history = await _ensure_budget(history, extra_overhead_tokens=overhead, ref_text=query_to_model)
            if getattr(settings, "LLM_AUDIT", False) and len(history) < before:
                logger.info("[TRACE] history shrunk via Summary (ensure_budget)")
            history_llm = _compact_for_llm(history)

            messages = _build_responses_messages(
                system_prompt_content,
                history_llm,
                kb_chunks=(chunks or None),
                user_text=(query_to_model or "").strip() or None,
                image_data_url=data_url,
                include_image_hint=True,
            )

            browse_kwargs = {}
            if allow_web:
                browse_kwargs = {"tools": [{"type": "web_search"}], "tool_choice": "auto"}

            resp = await asyncio.wait_for(
                _call_openai_with_retry(
                    model=settings.RESPONSE_MODEL,
                    endpoint="responses.create",
                    input=messages,
                    max_output_tokens=max_tokens,
                    temperature=temperature,
                    top_p=top_p,
                    **browse_kwargs,
                ),
                timeout=30.0,
            )
        else:
            if not (is_emoji_only and not is_channel_post):
                before = len(history)
                history = await _ensure_budget(history, extra_overhead_tokens=extra_overhead, ref_text=query_to_model)
                if getattr(settings, "LLM_AUDIT", False) and len(history) < before:
                    logger.info("[TRACE] history shrunk via Summary (ensure_budget)")
            if is_emoji_only and not is_channel_post:
                reply = query_to_model
            else:
                history_llm = _compact_for_llm(history)
                chunks = [h[2] for h in hits] if hits else None
                messages = _build_responses_messages(
                    system_prompt_content,
                    history_llm,
                    kb_chunks=(chunks or None),
                    user_text=None,
                )

                browse_kwargs = {}
                if allow_web:
                    browse_kwargs = {"tools": [{"type": "web_search"}], "tool_choice": "auto"}

                resp = await asyncio.wait_for(
                    _call_openai_with_retry(
                        model=settings.RESPONSE_MODEL,
                        endpoint="responses.create",
                        input=messages,
                        max_output_tokens=max_tokens,
                        temperature=temperature,
                        top_p=top_p,
                        **browse_kwargs,
                    ),
                    timeout=30.0,
                )
    except (ClientError, asyncio.TimeoutError, ValueError):
        logger.exception("OpenAI chat error", exc_info=True)
        if reply is None:
            reply = "I’m sorry, something went wrong."
    except Exception:
        logger.exception("Unexpected error in OpenAI chat", exc_info=True)
        if reply is None:
            reply = "I’m sorry, something went wrong."
    else:
        if resp:
            logger.info("   ↳ OpenAI chat END (t=%.3fs)", time.time() - t0)
            reply = (_get_output_text(resp) or "").strip()
            reply = re.sub(
                r"(?m)^\s*\[(?:имя|name)\s*:[^\]]*\]\s*\n?",
                "", reply, flags=re.I).strip()
        if not (reply or "").strip():
            reply = "…"

    assistant_allowed = True
    assistant_guard_key = None
    if msg_id is not None:
        try:
            ttl_days = getattr(settings, "MEMORY_TTL_DAYS", 7)
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
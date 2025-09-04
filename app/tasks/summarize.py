#app/tasks/summarize.py
import asyncio
import json
import re
import logging

from typing import List, Dict
from asyncio import run_coroutine_threadsafe

from app.tasks.celery_app import celery
from app.tasks.utils.bg_loop import get_bg_loop
from app.clients.openai_client import _call_openai_with_retry,  _get_output_text
from app.emo_engine.persona.memory import PersonaMemory, get_embedding
from app.core.memory import (
    get_redis, _k_p_msgs, _k_p_sum,
    _k_g_msgs, _k_g_sum_u, _k_g_msgs_all,
    _k_g_sum, MEMORY_TTL, extract_summary_data,
    pack_summary_data, set_summary_if_newer,
)
from app.config import settings


logger = logging.getLogger(__name__)

REQUIRED_KEYS = ("topic", "facts", "decisions", "open_questions", "todos", "preferences", "entities")

def _build_summary_system_prompt() -> str:
    return (
        "You update a rolling conversation memory. "
        "Return ONLY minified JSON that conforms to the provided json_schema. "
        "Rules:\n"
        "- Merge PREVIOUS_SUMMARY_JSON with NEW_MESSAGES.\n"
        "- Keep durable, cross-turn information only; drop chit-chat, greetings, transient small talk.\n"
        "- No speculation or invention; do not add anything not grounded in the input.\n"
        "- Deduplicate items and normalize entity names consistently (same entity → same string).\n"
        "- topic: a short phrase of the main subject.\n"
        "- facts: atomic, verifiable statements (one idea per string).\n"
        "- decisions: explicit commitments/conclusions (who will do what/what was decided).\n"
        "- open_questions: unresolved questions that remain after NEW_MESSAGES.\n"
        "- todos: actionable tasks phrased as commands (\"verb + object\").\n"
        "- preferences: stable likes/dislikes or constraints that are likely to persist.\n"
        "- entities: proper names: people, products, orgs, places, usernames/handles.\n"
        "- If a section has nothing to add, keep it as an empty array; topic may be an empty string."
    )

def _build_summary_user_prompt(previous_json: str, messages: list[str]) -> str:
    prev = previous_json or "{}"
    new_block = "\n".join(messages) if messages else "(none)"
    return (
        "PREVIOUS_SUMMARY_JSON:\n" + prev + "\n\n"
        "NEW_MESSAGES (oldest→newest, one per line):\n" + new_block + "\n\n"
        "TASK: Update the summary by merging PREVIOUS_SUMMARY_JSON with NEW_MESSAGES according to the Rules. "
        "Output JSON only (no markdown, no code fences)."
    )

async def _summarize_worker(
    *,
    is_private: bool,
    chat_id: int,
    user_id: int,
    length: int,
) -> None:

    redis = get_redis()
    success = False

    MSG_TRIM = int(getattr(settings, "SUMMARY_MSG_TRIM", 1200))
    SHORT_FUSE_EX = int(getattr(settings, "SUMMARY_GUARD_EX", 120))

    if is_private:
        key_log = _k_p_msgs(user_id)
        key_sum = _k_p_sum(user_id)
    else:
        if user_id == 0:
            key_log = _k_g_msgs_all(chat_id)
            key_sum = _k_g_sum(chat_id)
        else:
            key_log = _k_g_msgs(chat_id, user_id)
            key_sum = _k_g_sum_u(chat_id, user_id)
    
    SKIP_PREFIXES = (
        "Summary:",
        "INTERNAL_PLAN:",
        "[ReplyContext]",
        "This message was forwarded from",
        "The user's message was transcribed",
    )

    flag_key = f"{key_log}:_summary_pending"

    try:
        got = await redis.set(flag_key, "1", ex=SHORT_FUSE_EX, nx=True)
        if not got:
            logger.info("Summarization already in progress for %s", key_log)
            return
        old_raw = await redis.get(key_sum) or ""
        _old_data = extract_summary_data(old_raw)
        try:
            json.loads(_old_data)
            old = _old_data
        except Exception:
            old = "{}"
        sml = int(getattr(settings, "SHORT_MEMORY_LIMIT", 0))
        if length < sml:
            logger.info("Summarization skipped: length (%s) < SHORT_MEMORY_LIMIT (%s)", length, sml)
            success = True
            return

        pct = float(getattr(settings, "SUMMARY_OLD_PCT", 0.25))
        if not (0.0 < pct < 1.0):
            pct = 0.25
        cut = max(1, min(int(length * pct), length - 1))

        try:
            rows: List[bytes | str] = await asyncio.wait_for(redis.lrange(key_log, 0, cut - 1), 2.0)
        except Exception:
            logger.exception("lrange failed chat=%s user=%s", chat_id, user_id)
            success = False
            return

        msgs = []
        for r in reversed(rows):
            try:
                raw = r.decode() if isinstance(r, (bytes, bytearray)) else r
                m = json.loads(raw)
            except Exception:
                continue
            body = (m.get("content") or "").strip()
            if MSG_TRIM and len(body) > MSG_TRIM:
                body = body[:MSG_TRIM] + "…"
            body = re.sub(r"\s+", " ", body)
            speaker = (
                "Assistant" if m.get("role") == "assistant"
                else ("User" if m.get("role") == "user" else "System")
            )
            if body.startswith(tuple(SKIP_PREFIXES)):
                continue
            msgs.append(f"{speaker}: {body}")

        if not msgs and not old:
            success = True
            return
        if not msgs:
            success = True
            return

        system_prompt  = _build_summary_system_prompt()
        user_prompt = _build_summary_user_prompt(old, msgs)

        summary_schema = {
            "type": "object",
            "properties": {
                "topic": {"type": "string"},
                "facts": {"type": "array", "items": {"type": "string"}},
                "decisions": {"type": "array", "items": {"type": "string"}},
                "open_questions": {"type": "array", "items": {"type": "string"}},
                "todos": {"type": "array", "items": {"type": "string"}},
                "preferences": {"type": "array", "items": {"type": "string"}},
                "entities": {"type": "array", "items": {"type": "string"}},
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
                            "name": "history_merge",
                            "schema": summary_schema,
                            "strict": True
                        }
                    },
                    temperature=0,
                    max_output_tokens=1000,
                ),
                timeout=20.0,
            )
            content = (_get_output_text(resp) or "{}").strip()
        except Exception:
            logger.exception("OpenAI summarization failed chat=%s user=%s", chat_id, user_id)
            return

        content = re.sub(r"^```[a-zA-Z]*\s*", "", content).rstrip("`").strip() if content.startswith("```") else content
        if "{" in content and "}" in content:
            content = content[content.find("{"): content.rfind("}") + 1]
        try:
            obj = json.loads(content)
            if not isinstance(obj, dict):
                obj = {}
        except Exception:
            logger.warning("Summarizer returned non-JSON; skipping update chat=%s user=%s", chat_id, user_id)
            return
        for k in REQUIRED_KEYS:
            if k not in obj:
                obj[k] = "" if k == "topic" else []
        new_summary = json.dumps(obj, ensure_ascii=False, separators=(',', ':'))

        try:
            payload = pack_summary_data(new_summary)
            await set_summary_if_newer(key_sum, payload, MEMORY_TTL)
            try:
                cur_len = await redis.llen(key_log)
            except Exception:
                cur_len = None
            trim_from = cut
            if isinstance(cur_len, int) and cur_len >= 0:
                trim_from = min(cut, cur_len)
            await redis.ltrim(key_log, trim_from, -1)
            success = True
        except Exception:
            logger.exception("Redis pipeline failed chat=%s user=%s", chat_id, user_id)
            return
    finally:
        try:
            if success:
                await redis.delete(flag_key)
            else:
                await redis.expire(flag_key, 60)
        except Exception:
            logger.warning("Failed to update summarization guard flag for %s", key_log)

    logger.info("Summary updated chat=%s user=%s", chat_id, user_id)


@celery.task(name="summarize_private_old", acks_late=True, time_limit=120)
def summarize_private_old(user_id: int, length: int) -> None:
    loop = get_bg_loop()
    fut = run_coroutine_threadsafe(
        _summarize_worker(
            is_private=True,
            chat_id=user_id,
            user_id=user_id,
            length=length,
        ),
        loop,
    )
    def _cb(f):
        try:
            f.result()
        except Exception as e:
            logger.error("summarize_private_old failed: %s", e, exc_info=True)
    fut.add_done_callback(_cb)


@celery.task(name="summarize_group_old", acks_late=True, time_limit=120)
def summarize_group_old(chat_id: int, user_id: int, length: int) -> None:
    loop = get_bg_loop()
    fut = run_coroutine_threadsafe(
        _summarize_worker(
            is_private=False,
            chat_id=chat_id,
            user_id=user_id,
            length=length,
        ),
        loop,
    )
    def _cb(f):
        try:
            f.result()
        except Exception as e:
            logger.error("summarize_group_old failed: %s", e, exc_info=True)
    fut.add_done_callback(_cb)


@celery.task(name="persona.summarize_memory", acks_late=True, time_limit=300)
def summarize_memory(chat_id: int, texts: list, old_ids: list) -> None:
    asyncio.run(_summarize_memory_worker(chat_id, texts, old_ids))


async def _summarize_memory_worker(chat_id: int, texts: list, old_ids: list) -> None:

    if not texts:
        logger.info("summarize_memory: no texts for chat=%s, skipping", chat_id)
        return
    CHUNK_TIMEOUT = float(getattr(settings, "MEMORY_SUMMARY_CHUNK_TIMEOUT", 45.0))
    CONS_TIMEOUT = float(getattr(settings, "MEMORY_SUMMARY_FINAL_TIMEOUT", 45.0))

    def _mk_prompt(block: list[str]) -> str:
        snippet = " ||| ".join(block)
        return (
            "You compress related autobiographical events into a single memory entry.\n\n"
            f"EVENTS (delimiter = '|||'):\n{snippet}\n\n"
            "TASK: Produce 1-2 short sentences (≤ 50 words total) in past tense, "
            "objective and free of speculation. Return ONLY the consolidated sentence."
        )
    try:
        MAX_CHUNK = 300
        partials = []
        for i in range(0, len(texts), MAX_CHUNK):
            block = texts[i:i+MAX_CHUNK]
            prompt = _mk_prompt(block)
            resp = await asyncio.wait_for(
                _call_openai_with_retry(
                    endpoint="responses.create",
                    model=settings.REASONING_MODEL,
                    instructions="You are a precise summarisation assistant.",
                    input=prompt,
                    max_output_tokens=192,
                    temperature=0,
                ),
                CHUNK_TIMEOUT,
            )
            partials.append((_get_output_text(resp) or "").strip())
        if len(partials) == 1:
            summary = partials[0]
        else:
            prompt = _mk_prompt(partials)
            resp = await asyncio.wait_for(
                _call_openai_with_retry(
                    endpoint="responses.create",
                    model=settings.REASONING_MODEL,
                    instructions="You consolidate summaries into one.",
                    input=prompt,
                    max_output_tokens=192,
                    temperature=0,
                ),
                CONS_TIMEOUT,
            )
            summary = (_get_output_text(resp) or "").strip()

        summary = (summary or "").strip()
        try:
            emb = await asyncio.wait_for(get_embedding(summary), timeout=15.0)
        except asyncio.TimeoutError:
            logger.warning("get_embedding timeout for summary, skipping record")
            return
        mem = PersonaMemory(chat_id=chat_id, start_maintenance=False)
        await mem.ready()
        eid, created = await mem.record(
            text=summary,
            embedding=emb,
            emotions={},
            state_metrics={},
            uid=None,
            salience=1.0,
            event_frame=False,
        )
        if eid:
            await mem._redis.hset(
                f"memory:{chat_id}:{eid}",
                mapping={"collapsed_from": ",".join(map(str, old_ids))}
            )
        zset_key = f"memory:ids:{chat_id}"
        pipe = mem._redis.pipeline(transaction=True)
        for old_eid in old_ids:
            pipe.delete(f"memory:{chat_id}:{old_eid}")
            pipe.zrem(zset_key, str(old_eid))
        await pipe.execute()
        logger.info("Collapsed %d entries into 1 summary and removed originals", len(old_ids))
    except Exception as e:
        logger.exception("summarize_memory failed: %s", e)

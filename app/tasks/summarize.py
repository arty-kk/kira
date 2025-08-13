cat >app/tasks/summarize.py<< 'EOF'
#app/tasks/summarize.py
import asyncio
import json
import logging

from typing import List
from asyncio import run_coroutine_threadsafe

from app.tasks.celery_app import celery
from app.tasks.utils.bg_loop import get_bg_loop
from app.clients.openai_client import _call_openai_with_retry
from app.emo_engine.persona.memory import PersonaMemory, get_embedding
from app.core.memory import (
    get_redis, _k_p_msgs, _k_p_sum,
    _k_g_msgs, _k_g_sum_u, MEMORY_TTL,
)
from app.config import settings


logger = logging.getLogger(__name__)


async def _summarize_worker(
    *,
    is_private: bool,
    chat_id: int,
    user_id: int,
    length: int,
) -> None:

    redis = get_redis()
    success = False

    if is_private:
        key_log = _k_p_msgs(user_id)
        key_sum = _k_p_sum(user_id)
    else:
        key_log = _k_g_msgs(chat_id, user_id)
        key_sum = _k_g_sum_u(chat_id, user_id)
    
    flag_key = f"{key_log}:_summary_pending"

    try:
        old = await redis.get(key_sum) or ""
        half = max(1, length - settings.SHORT_MEMORY_LIMIT // 2)

        try:
            rows: List[str] = await asyncio.wait_for(redis.lrange(key_log, 0, half - 1), 2.0)
        except Exception:
            logger.exception("lrange failed chat=%s user=%s", chat_id, user_id)
            success = True
            return

        msgs = []
        for r in rows:
            try:
                m = json.loads(r)
            except json.JSONDecodeError:
                continue
            body = (m.get("content") or "").strip()
            speaker = "Assistant" if m.get("role") == "assistant" else "User"
            msgs.append(f"{speaker}: {body}")

        if not msgs and not old:
            success = True
            return

        prompt = (
            "PREVIOUS_SUMMARY:\n"
            f"{(old or '(none)')}\n\n"
            "NEW_MESSAGES:\n"
            + ("\n".join(msgs) or "(none)") +
            "\n\nTASK:\n"
            "Update PREVIOUS_SUMMARY so it also reflects NEW_MESSAGES. "
            "Write ≤ 150 words, third-person, factual, no speculation, "
            "refer to participants only as 'Assistant' and 'User'. "
            "Do **not** mention this instruction or that you are summarising. "
            "Return **only** the updated summary text."
        )

        try:
            resp = await asyncio.wait_for(
                _call_openai_with_retry(
                    model=settings.REASONING_MODEL,
                    messages=[
                        {
                            "role": "system",
                            "content": "You are an assistant that creates concise, factual summaries.",
                        },
                        {"role": "user", "content": prompt},
                    ],
                    max_completion_tokens=330,
                    temperature=0.0,
                ),
                60.0,
            )
            new_summary = resp.choices[0].message.content.strip()
        except Exception:
            logger.exception("OpenAI summarization failed chat=%s user=%s", chat_id, user_id)
            return

        try:
            async with redis.pipeline(transaction=True) as pipe:
                pipe.set(key_sum, new_summary, ex=MEMORY_TTL)
                if half:
                    pipe.ltrim(key_log, half, -1)
                await pipe.execute()
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
def summarize_memory(texts: list, old_ids: list) -> None:

    asyncio.run(_summarize_memory_worker(texts, old_ids))


async def _summarize_memory_worker(texts: list, old_ids: list) -> None:
    snippet = " ||| ".join(texts)
    prompt = (
        "You compress related autobiographical events into a single memory entry.\n\n"
        f"EVENTS (delimiter = '|||'):\n{snippet}\n\n"
        "TASK: Produce 1-2 short sentences (≤ 50 words total) in **past tense**, "
        "objective and free of speculation, that collectively summarise these events. "
        "Return only the consolidated memory sentence."
    )
    try:
        resp = await _call_openai_with_retry(
            model=settings.REASONING_MODEL,
            messages=[
                {"role": "system", "content": "You are a precise summarisation assistant."},
                {"role": "user", "content": prompt},
            ],
            max_completion_tokens=256,
            temperature=0.0,
        )
        summary = resp.choices[0].message.content.strip()

        emb = await get_embedding(summary)
        mem = PersonaMemory()
        await mem._ready.wait()
        await mem.record(
            text=summary,
            embedding=emb,
            emotions={},
            state_metrics={}
        )
        logger.info("Collapsed %d entries into 1 summary", len(old_ids))
    except Exception as e:
        logger.exception("summarize_memory failed: %s", e)
EOF
#app/tasks/summarize.py
from __future__ import annotations

import asyncio
import json
import logging
import contextlib

from typing import List, Tuple

from app.tasks.celery_app import celery, run_coro_sync
from app.clients.openai_client import _call_openai_with_retry, _get_output_text
from app.core.memory import (
    USER_KEYS_REGISTRY_TTL,
    _register_user_key,
    get_redis, _k_mtm, _k_mtm_tokens,
    _k_ltm, MEMORY_TTL_LTM, pack_summary_data,
    set_summary_if_newer, _b2s, approx_tokens,
    extract_summary_data, push_ltm_slice,
)
from app.config import settings
from app.emo_engine.persona.memory import PersonaMemory, get_embedding
from app.prompts_base import (
    SUMMARIZE_COMPRESS_INSTRUCTIONS,
    SUMMARIZE_CONSOLIDATE_INSTRUCTIONS,
    summarize_compress_prompt,
    summarize_events_prompt,
    summarize_merge_prompt,
)

logger = logging.getLogger(__name__)


def _cap_lines(lines: List[str], max_tokens: int) -> str:
    acc = 0
    out: List[str] = []
    for ln in lines:
        t = approx_tokens(ln)
        if out and acc + t > max_tokens:
            break
        out.append(ln)
        acc += t
    return "\n".join(out)


def _is_private(chat_id: int, user_id: int) -> bool:
    return chat_id == user_id


def _limits(is_private: bool) -> Tuple[int, int, int, int]:
    if is_private:
        TRIM = settings.MTM_TRIM_CHUNK_TOKENS_PRIVATE
        MTM  = settings.MTM_BUDGET_TOKENS_PRIVATE
        LTM  = settings.LTM_MAX_TOKENS_PRIVATE
    else:
        TRIM = settings.MTM_TRIM_CHUNK_TOKENS_GROUP
        MTM  = settings.MTM_BUDGET_TOKENS_GROUP
        LTM  = settings.LTM_MAX_TOKENS_GROUP
    SAFE_CONF = settings.LTM_SUMMARY_MAX_OUTPUT_TOKENS
    SAFE = min(SAFE_CONF, LTM + 512)
    return (TRIM, MTM, LTM, SAFE)


def _compress_prompt(lines: List[str]) -> str:

    block = "\n".join(lines)
    return summarize_compress_prompt(block)


def _merge_prompt(old: str, new: str, max_tokens: int) -> str:

    return summarize_merge_prompt(old, new, max_tokens)


async def _rollup(chat_id: int, user_id: int, namespace: str = "default") -> None:

    redis = get_redis()

    ns_prefix = "api:" if namespace == "api" else ""
    guard_key = f"{ns_prefix}ltm_rollup_worker:{chat_id}:{user_id}"

    try:
        lock_ttl = int(getattr(settings, "LTM_ROLLUP_WORKER_EX", 300))
    except Exception:
        lock_ttl = 300
    try:
        got_lock = await redis.set(guard_key, "1", ex=lock_ttl, nx=True)
    except Exception:
        logger.warning(
            "LTM rollup: lock SET failed, skipping rollup chat=%s user=%s",
            chat_id,
            user_id,
            exc_info=True,
        )
        return
    if not got_lock:
        logger.info("LTM rollup: skipped (already running) chat=%s user=%s", chat_id, user_id)
        return

    try:
        is_priv = _is_private(chat_id, user_id)
        TRIM_CHUNK, MTM_BUDGET, LTM_MAX, SAFE_MAX_OUT = _limits(is_priv)

        key_mtm = _k_mtm(chat_id, user_id, namespace)
        key_tok = _k_mtm_tokens(chat_id, user_id, namespace)
        key_ltm = _k_ltm(chat_id, user_id, namespace)

        acc_tokens = 0
        picked: List[str] = []
        raw_rows: List[bytes] = []

        try:
            while acc_tokens < TRIM_CHUNK:
                row = await redis.lpop(key_mtm)
                if not row:
                    break
                raw_rows.append(row if isinstance(row, (bytes, bytearray)) else str(row).encode("utf-8"))
                try:
                    obj = json.loads(_b2s(row))
                    ln = f"{obj.get('role','')}: {obj.get('content','')}"
                except Exception:
                    ln = _b2s(row)
                t = approx_tokens(ln)
                acc_tokens += t
                picked.append(ln)
        except Exception:
            logger.exception("LTM rollup: MTM pop failed chat=%s user=%s", chat_id, user_id)

        if not picked:
            logger.info("LTM rollup: nothing to compress (MTM empty) chat=%s user=%s", chat_id, user_id)
            return

        def _token_sum(lines: list[str]) -> int:
            return sum(approx_tokens(x) for x in lines)
        SAFE_IN_CHUNK = 8000
        try:
            partial_trigger = settings.LTM_SUMMARY_PARTIAL_TRIGGER_TOKENS
            if _token_sum(picked) > partial_trigger:
                partials: list[str] = []
                buf: list[str] = []
                acc = 0
                for ln in picked:
                    t = approx_tokens(ln)
                    if acc + t > SAFE_IN_CHUNK and buf:
                        r = await asyncio.wait_for(
                            _call_openai_with_retry(
                                endpoint="responses.create",
                                model=settings.REASONING_MODEL,
                                model_role="regular",
                                input=_compress_prompt(buf),
                                max_output_tokens=SAFE_MAX_OUT,
                                temperature=0,
                            ),
                            timeout=settings.REASONING_MODEL_TIMEOUT
                        )
                        partials.append((_get_output_text(r) or "").strip())
                        buf, acc = [], 0
                    buf.append(ln)
                    acc += t
                if buf:
                    r = await asyncio.wait_for(
                        _call_openai_with_retry(
                            endpoint="responses.create",
                            model=settings.REASONING_MODEL,
                            model_role="regular",
                            input=_compress_prompt(buf),
                            max_output_tokens=SAFE_MAX_OUT,
                            temperature=0,
                        ),
                        timeout=settings.REASONING_MODEL_TIMEOUT
                    )
                    partials.append((_get_output_text(r) or "").strip())
                merged_partials = "\n".join(x for x in partials if x.strip())
                r2 = await asyncio.wait_for(
                    _call_openai_with_retry(
                        endpoint="responses.create",
                        model=settings.REASONING_MODEL,
                        model_role="regular",
                        input=_merge_prompt("", merged_partials, LTM_MAX),
                        max_output_tokens=SAFE_MAX_OUT,
                        temperature=0,
                    ),
                    timeout=settings.REASONING_MODEL_TIMEOUT
                )
                new_slice = (_get_output_text(r2) or "").strip()
                if not new_slice:
                    raise RuntimeError("empty new_slice after partials merge")
            else:
                r = await asyncio.wait_for(
                    _call_openai_with_retry(
                        endpoint="responses.create",
                        model=settings.REASONING_MODEL,
                        model_role="regular",
                        input=_compress_prompt(picked),
                        max_output_tokens=SAFE_MAX_OUT,
                        temperature=0,
                    ),
                    timeout=settings.REASONING_MODEL_TIMEOUT
                )
                new_slice = (_get_output_text(r) or "").strip()
                if not new_slice:
                    raise RuntimeError("empty new_slice")
        except Exception:
            logger.exception("LTM rollup: compression failed chat=%s user=%s — restoring MTM", chat_id, user_id)
            try:
                if raw_rows:
                    await redis.lpush(key_mtm, *reversed(raw_rows))
            except Exception:
                logger.warning("LTM rollup: MTM restore failed chat=%s user=%s", chat_id, user_id)
            return

        try:
            max_raw = int(getattr(settings, "LTM_SLICES_MAX_TOKENS", 4000))
            cap_items = int(getattr(settings, "LTM_SLICES_CAP", 40))
            ttl_sec = int(getattr(settings, "LTM_SLICES_TTL_SEC", 30 * 86400))
            raw_for_slice = _cap_lines(picked, max_raw)
            if raw_for_slice.strip():
                await push_ltm_slice(
                    chat_id,
                    user_id,
                    raw_for_slice,
                    cap_items=cap_items,
                    ttl_override=ttl_sec,
                )
        except Exception:
            logger.warning("LTM rollup: push_ltm_slice failed chat=%s user=%s", chat_id, user_id)

        try:
            old_raw = await redis.get(key_ltm)
            old_ltm = extract_summary_data(old_raw) if old_raw else ""
        except Exception:
            old_ltm = ""

        try:
            merge_resp = await asyncio.wait_for(
                _call_openai_with_retry(
                    endpoint="responses.create",
                    model=settings.REASONING_MODEL,
                    model_role="regular",
                    input=_merge_prompt(old_ltm, new_slice, LTM_MAX),
                    max_output_tokens=SAFE_MAX_OUT,
                    temperature=0,
                ),
                timeout=settings.REASONING_MODEL_TIMEOUT
            )
            merged = (_get_output_text(merge_resp) or "").strip()
        except Exception:
            logger.exception("LTM rollup: merge failed chat=%s user=%s — restoring MTM", chat_id, user_id)
            try:
                if raw_rows:
                    await redis.lpush(key_mtm, *reversed(raw_rows))
            except Exception:
                logger.warning("LTM rollup: MTM restore failed chat=%s user=%s", chat_id, user_id)
            return

        try:
            payload = pack_summary_data(merged)
            await set_summary_if_newer(key_ltm, payload, MEMORY_TTL_LTM)
            try:
                await _register_user_key(redis, user_id, key_ltm, USER_KEYS_REGISTRY_TTL)
            except Exception:
                pass
            try:
                try:
                    cpt = settings.APPROX_CHARS_PER_TOKEN
                except Exception:
                    cpt = 3.8
                byte_len_sum = 0
                for r in raw_rows:
                    if isinstance(r, (bytes, bytearray)):
                        byte_len_sum += len(r)
                    else:
                        byte_len_sum += len(str(r).encode("utf-8", "ignore"))
                decr = max(1, int(byte_len_sum / max(0.1, cpt)))
                script = (
                    "local v=redis.call('DECRBY', KEYS[1], ARGV[1]); "
                    "if v<0 then redis.call('SET', KEYS[1], 0); v=0 end; return v"
                )
                await redis.eval(script, 1, key_tok, str(int(decr)))
            except Exception:
                logger.warning("LTM rollup: tokens decrement failed chat=%s user=%s", chat_id, user_id)
            logger.info(
                "LTM updated chat=%s user=%s (popped≈%d tokens, MTM budget=%d, LTM max=%d)",
                chat_id, user_id, acc_tokens, MTM_BUDGET, LTM_MAX
            )
        except Exception:
            logger.exception("LTM rollup: save failed chat=%s user=%s", chat_id, user_id)
    finally:
        with contextlib.suppress(Exception):
            await redis.delete(guard_key)


@celery.task(name="ltm.rollup_private", acks_late=True, time_limit=240)
def rollup_private(user_id: int, namespace: str = "default") -> None:
    run_coro_sync(_rollup(chat_id=user_id, user_id=user_id, namespace=namespace))


@celery.task(name="ltm.rollup_group", acks_late=True, time_limit=240)
def rollup_group(chat_id: int, user_id: int, namespace: str = "default") -> None:
    run_coro_sync(_rollup(chat_id=chat_id, user_id=user_id, namespace=namespace))


@celery.task(name="persona.summarize_memory", acks_late=True, time_limit=300)
def summarize_memory(chat_id: int, texts: list, old_ids: list) -> None:
    run_coro_sync(_summarize_memory_worker(chat_id, texts, old_ids))


async def _summarize_memory_worker(chat_id: int, texts: list, old_ids: list) -> None:
    logger_local = logging.getLogger(__name__)

    mem = PersonaMemory(chat_id=chat_id, start_maintenance=False)
    await mem.ready()

    async def _unschedule(ids: list[str] | list):
        if not ids:
            return
        try:
            pipe = mem._redis.pipeline(transaction=True)
            for oid in ids:
                pipe.hdel(f"memory:{chat_id}:{oid}", "consolidation_scheduled_ts")
            await pipe.execute()
        except Exception:
            logger_local.debug("summarize_memory: unschedule cleanup skipped", exc_info=True)

    if not texts:
        logger_local.info("summarize_memory: no texts for chat=%s, skipping", chat_id)
        await _unschedule(old_ids)
        return

    def _mk_prompt(block: list[str]) -> str:
        snippet = " ||| ".join(block)
        return summarize_events_prompt(snippet)

    try:
        try:
            MAX_IN_TOKENS = int(getattr(settings, "MEMORY_SUMMARY_CHUNK_TOKENS", 6000))
        except Exception:
            MAX_IN_TOKENS = 6000

        partials: list[str] = []
        block: list[str] = []
        acc_tokens = 0

        for t in texts:
            tok = approx_tokens(t)
            if block and acc_tokens + tok > MAX_IN_TOKENS:
                prompt = _mk_prompt(block)
                resp = await asyncio.wait_for(
                    _call_openai_with_retry(
                        endpoint="responses.create",
                        model=settings.REASONING_MODEL,
                        model_role="regular",
                        instructions=SUMMARIZE_COMPRESS_INSTRUCTIONS,
                        input=prompt,
                        max_output_tokens=192,
                        temperature=0,
                    ),
                    timeout=settings.REASONING_MODEL_TIMEOUT
                )
                partials.append((_get_output_text(resp) or "").strip())
                block, acc_tokens = [], 0

            block.append(t)
            acc_tokens += tok

        if block:
            prompt = _mk_prompt(block)
            resp = await asyncio.wait_for(
                _call_openai_with_retry(
                    endpoint="responses.create",
                    model=settings.REASONING_MODEL,
                    model_role="regular",
                    instructions=SUMMARIZE_COMPRESS_INSTRUCTIONS,
                    input=prompt,
                    max_output_tokens=192,
                    temperature=0,
                ),
                timeout=settings.REASONING_MODEL_TIMEOUT,
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
                    model_role="regular",
                    instructions=SUMMARIZE_CONSOLIDATE_INSTRUCTIONS,
                    input=prompt,
                    max_output_tokens=192,
                    temperature=0,
                ),
                timeout=settings.REASONING_MODEL_TIMEOUT,
            )
            summary = (_get_output_text(resp) or "").strip()

        summary = (summary or "").strip()
        if not summary:
            logger_local.warning("summarize_memory: empty summary, keeping originals (chat=%s)", chat_id)
            await _unschedule(old_ids)
            return

        try:
            emb = await asyncio.wait_for(get_embedding(summary), timeout=15.0)
        except asyncio.TimeoutError:
            logger_local.warning("get_embedding timeout for summary, skipping record")
            await _unschedule(old_ids)
            return

        eid, created = await mem.record(
            text=summary,
            embedding=emb,
            emotions={},
            state_metrics={},
            uid=None,
            salience=1.0,
            event_frame=False,
        )
        if eid and created:
            await mem._redis.hset(
                f"memory:{chat_id}:{eid}",
                mapping={"collapsed_from": ",".join(map(str, old_ids))}
            )

        if eid and created:
            zset_key = f"memory:ids:{chat_id}"
            pipe = mem._redis.pipeline(transaction=True)
            for old_eid in old_ids:
                pipe.delete(f"memory:{chat_id}:{old_eid}")
                pipe.zrem(zset_key, str(old_eid))
            await pipe.execute()
        else:
            logger_local.warning("summarize_memory: no eid created, keeping originals (chat=%s)", chat_id)
            await _unschedule(old_ids)
            return

        try:
            uidsets_key = f"memory:uidsets:{chat_id}"
            uid_zsets = await mem._redis.smembers(uidsets_key)
            if uid_zsets:
                old_ids_str = [str(x) for x in old_ids]
                pipe2 = mem._redis.pipeline(transaction=True)
                for z in uid_zsets:
                    zname = z.decode() if isinstance(z, (bytes, bytearray)) else str(z)
                    for oid in old_ids_str:
                        pipe2.zrem(zname, oid)
                await pipe2.execute()
        except Exception:
            logger_local.debug("summarize_memory: per-uid zsets cleanup skipped", exc_info=True)

        logger_local.info(
            "Collapsed %d entries into 1 summary and removed originals (chat=%s)",
            len(old_ids), chat_id
        )
    except Exception as e:
        logger_local.exception("summarize_memory failed: %s", e)
        await _unschedule(old_ids)

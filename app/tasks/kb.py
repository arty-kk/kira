#app/tasks/kb.py
from __future__ import annotations

import asyncio
import logging
import time
import html

from typing import Any, Dict, List

from sqlalchemy import select, delete

from app.config import settings
from app.core.embedding_utils import normalize_embedding_row
from app.core.db import session_scope
from app.core.models import ApiKey, ApiKeyKnowledge, RagTagVector
from app.clients.openai_client import _call_openai_with_retry
from app.tasks.celery_app import celery, _run
from app.services.responder.rag.api_kb_proc import invalidate_api_kb_cache
from app.services.responder.rag.keyword_filter import invalidate_tags_index
from app.clients.telegram_client import get_bot
from app.bot.utils.telegram_safe import send_message_safe
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


logger = logging.getLogger(__name__)


def _normalize_embedding_row(raw: Any, *, expected_dim: int) -> List[float]:
    return normalize_embedding_row(raw, expected_dim=expected_dim, error_prefix="kb: ")


async def _embed_texts(texts: List[str], model: str) -> List[List[float]]:
    if not texts:
        return []

    api_model = str(model)
    try:
        bs = int(getattr(settings, "EMBED_BATCH_SIZE", 128) or 128)
    except Exception:
        bs = 128
    if bs <= 0:
        bs = 128

    expected_dim = int(getattr(settings, "RAG_VECTOR_DIM", 3072) or 3072)
    result: List[List[float]] = []
    overall_start = time.perf_counter()

    for batch_idx, start in enumerate(range(0, len(texts), bs)):
        chunk = texts[start : start + bs]
        t0 = time.perf_counter()
        try:
            resp = await asyncio.wait_for(
                _call_openai_with_retry(
                    endpoint="embeddings.create",
                    model=api_model,
                    input=chunk,
                    encoding_format="float",
                ),
                timeout=settings.EMBEDDING_TIMEOUT,
            )
            elapsed = time.perf_counter() - t0
            logger.info(
                "kb: embeddings.create ok model=%s batch_index=%d batch_size=%d elapsed=%.3fs",
                api_model,
                batch_idx,
                len(chunk),
                elapsed,
            )
        except Exception:
            elapsed = time.perf_counter() - t0
            logger.exception(
                "kb: embeddings.create FAILED model=%s batch_index=%d batch_size=%d elapsed=%.3fs",
                api_model,
                batch_idx,
                len(chunk),
                elapsed,
            )
            raise

        data = getattr(resp, "data", None) or (
            resp.get("data") if isinstance(resp, dict) else None
        )
        if not isinstance(data, list) or len(data) != len(chunk):
            raise RuntimeError("kb: embeddings response size mismatch")

        for row in data:
            emb = getattr(row, "embedding", None)
            if emb is None and isinstance(row, dict):
                emb = row.get("embedding")
            if emb is None:
                raise RuntimeError("kb: invalid embedding row")
            result.append(_normalize_embedding_row(emb, expected_dim=expected_dim))

    total_elapsed = time.perf_counter() - overall_start
    logger.info(
        "kb: embeddings total model=%s texts=%d batches=%d elapsed=%.3fs",
        api_model,
        len(texts),
        (len(texts) + bs - 1) // bs,
        total_elapsed,
    )
    return result


async def _notify_kb_status(api_key_id: int, kb_id: int, status: str, error: str | None = None) -> None:

    api_key_id = int(api_key_id)
    kb_id = int(kb_id)

    async with session_scope(read_only=True, stmt_timeout_ms=2000) as db:
        key = await db.get(ApiKey, api_key_id)
        kb = await db.get(ApiKeyKnowledge, kb_id)
        if not key or not kb:
            return
        user_id = key.user_id

    bot = get_bot()

    if status == "ready":
        text = "✅ KB is Ready."
    elif status == "failed":
        err = html.escape(error or kb.error or "unknown error", quote=True)
        text = f"⚠️ KB Error.\n<code>{err}</code>"
    else:
        return

    kb_back = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⬅️", callback_data="api:panel")]
        ]
    )

    try:
        await send_message_safe(bot, user_id, text, parse_mode="HTML", reply_markup=kb_back)
    except Exception:
        logger.exception(
            "kb: failed to notify user about kb status api_key_id=%s kb_id=%s",
            api_key_id,
            kb_id,
        )


@celery.task(name="kb.rebuild_for_api_key")
def rebuild_for_api_key(api_key_id: int, kb_id: int) -> None:
    _run(_rebuild_for_api_key_async(api_key_id, kb_id))
    invalidate_api_kb_cache(api_key_id)
    invalidate_tags_index(api_key_id)
    try:
        from app.services.responder.core import invalidate_active_kb_cache
        invalidate_active_kb_cache(api_key_id)
    except Exception:
        logger.exception("kb: invalidate_active_kb_cache failed for owner=%s", api_key_id)


async def _rebuild_for_api_key_async(api_key_id: int, kb_id: int) -> None:
    api_key_id = int(api_key_id)
    kb_id = int(kb_id)

    async with session_scope() as db:
        kb: ApiKeyKnowledge | None = await db.get(ApiKeyKnowledge, kb_id)
        if not kb:
            logger.warning(
                "kb: ApiKeyKnowledge id=%s not found (api_key_id=%s)",
                kb_id,
                api_key_id,
            )
            return
        if kb.api_key_id != api_key_id:
            logger.warning(
                "kb: ApiKeyKnowledge id=%s api_key_id mismatch (%s != %s)",
                kb_id,
                kb.api_key_id,
                api_key_id,
            )
            return

        raw_items = kb.items or []
        model = (
            kb.embedding_model
            or getattr(settings, "EMBEDDING_MODEL", "text-embedding-3-large")
        ).strip() or "text-embedding-3-large"

        kb.status = "building"
        kb.error = None
        kb.chunks_count = 0
        await db.flush()

    entries: List[Dict[str, Any]] = []
    for it in raw_items:
        try:
            text = (it.get("text") or "").strip()
        except Exception:
            text = ""
        if not text:
            continue
        entry = {
            "id": str(it.get("id", "") or ""),
            "category": it.get("category", "default") or "default",
            "tags": it.get("tags") or [],
            "text": text,
        }
        entries.append(entry)

    if not entries:
        logger.warning(
            "kb: no non-empty entries for api_key_id=%s kb_id=%s",
            api_key_id,
            kb_id,
        )
        async with session_scope() as db:
            kb = await db.get(ApiKeyKnowledge, kb_id)
            if kb:
                kb.status = "failed"
                kb.error = "No non-empty items to embed"
                kb.chunks_count = 0
            await db.flush()
        await _notify_kb_status(api_key_id, kb_id, status="failed", error="No non-empty items to embed")
        return

    expected_dim = int(getattr(settings, "RAG_VECTOR_DIM", 3072) or 3072)

    try:
        tag_rows: List[Dict[str, Any]] = []
        tag_strings: List[str] = []
        tag_seen: set[str] = set()
        for e in entries:
            for t in (e.get("tags") or []):
                if isinstance(t, str):
                    ts = t.strip()
                    if ts and ts not in tag_seen:
                        tag_seen.add(ts)
                        tag_strings.append(ts)

        tag_emb_map: Dict[str, List[float]] = {}
        if tag_strings:
            tag_embs = await _embed_texts(tag_strings, model=model)
            if len(tag_embs) != len(tag_strings):
                raise RuntimeError(f"Tag embedding size mismatch: tags={len(tag_strings)} embs={len(tag_embs)}")
            for ts, vec in zip(tag_strings, tag_embs):
                if len(vec) != expected_dim:
                    raise RuntimeError(f"Invalid tag embedding dim: got={len(vec)} expected={expected_dim}")
                tag_emb_map[ts] = [float(x) for x in vec]

        for e in entries:
            eid = str(e.get("id", "") or "")
            etext = str(e.get("text", "") or "")
            seen_item_tags: set[str] = set()
            for t in (e.get("tags") or []):
                if not isinstance(t, str):
                    continue
                ts = t.strip()
                if not ts:
                    continue
                if ts in seen_item_tags:
                    continue
                seen_item_tags.add(ts)
                if ts not in tag_emb_map:
                    continue
                tag_rows.append({"external_id": eid, "text": etext, "tag": ts, "embedding": tag_emb_map[ts]})

        async with session_scope() as db:
            kb = await db.get(ApiKeyKnowledge, kb_id)
            if not kb:
                return

            await db.execute(delete(RagTagVector).where(RagTagVector.kb_id == kb_id))

            if tag_rows:
                db.add_all([
                    RagTagVector(
                        scope="owner",
                        owner_id=api_key_id,
                        kb_id=kb_id,
                        embedding_model=model,
                        external_id=row["external_id"],
                        text=row["text"],
                        tag=row["tag"],
                        embedding=row["embedding"],
                    )
                    for row in tag_rows
                ])

            kb.status = "ready"
            kb.error = None
            kb.chunks_count = len(entries)

            try:
                max_versions = int(getattr(settings, "MAX_KB_VERSIONS_PER_KEY", 3) or 0)
            except Exception:
                max_versions = 0
            if max_versions > 0:
                res = await db.execute(
                    select(ApiKeyKnowledge.id)
                    .where(ApiKeyKnowledge.api_key_id == api_key_id)
                    .order_by(ApiKeyKnowledge.version.desc())
                )
                all_ids = [row[0] for row in res.fetchall()]
                stale_ids = all_ids[max_versions:]
                if stale_ids:
                    await db.execute(delete(ApiKeyKnowledge).where(ApiKeyKnowledge.id.in_(stale_ids)))
            await db.flush()
    except Exception as e:
        logger.exception("kb: failed DB persistence for api_key_id=%s kb_id=%s", api_key_id, kb_id)
        async with session_scope() as db:
            kb = await db.get(ApiKeyKnowledge, kb_id)
            if kb:
                kb.status = "failed"
                kb.error = str(e)
                kb.chunks_count = 0
            await db.flush()
        await _notify_kb_status(api_key_id, kb_id, status="failed", error=str(e))
        return

    await _notify_kb_status(api_key_id, kb_id, status="ready")


    try:
        invalidate_api_kb_cache(api_key_id)
    except Exception:
        logger.exception(
            "kb: invalidate_api_kb_cache failed for owner=%s",
            api_key_id,
        )

    try:
        invalidate_tags_index(api_key_id)
    except Exception:
        logger.exception(
            "kb: invalidate_tags_index failed for owner=%s",
            api_key_id,
        )

def _remove_owner_dir(owner_id: int) -> None:
    logger.info("kb: filesystem owner dir cleanup is disabled (db-backed RAG), owner_id=%s", owner_id)


@celery.task(name="kb.clear_for_api_key")
def clear_for_api_key(api_key_id: int) -> None:
    _remove_owner_dir(int(api_key_id))
    invalidate_api_kb_cache(api_key_id)
    invalidate_tags_index(api_key_id)
    try:
        from app.services.responder.core import invalidate_active_kb_cache
        invalidate_active_kb_cache(api_key_id)
    except Exception:
        logger.exception("kb: invalidate_active_kb_cache failed for owner=%s", api_key_id)


async def _gc_orphan_api_key_dirs_async() -> None:
    logger.info("kb: orphan filesystem GC skipped (db-backed RAG)")


@celery.task(name="kb.gc_orphan_api_key_dirs", ignore_result=True)
def gc_orphan_api_key_dirs() -> None:

    try:
        _run(_gc_orphan_api_key_dirs_async())
    except Exception:
        logger.exception("kb: gc_orphan_api_key_dirs failed")

#app/tasks/kb.py
from __future__ import annotations

import asyncio
import logging
import time
import json
import shutil
import html

from pathlib import Path
from typing import Any, Dict, List

import numpy as np

from sqlalchemy import select, delete

from app.config import settings
from app.core.db import session_scope
from app.core.models import ApiKey, ApiKeyKnowledge
from app.clients.openai_client import _call_openai_with_retry
from app.tasks.celery_app import celery, _run
from app.services.responder.rag.knowledge_proc import _build_state, EMBED_DIR
from app.services.responder.rag.api_kb_proc import invalidate_api_kb_cache
from app.services.responder.rag.keyword_filter import invalidate_tags_index
from app.clients.telegram_client import get_bot
from app.bot.utils.telegram_safe import send_message_safe
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


logger = logging.getLogger(__name__)


def _owner_dir(owner_id: int) -> Path:
    return EMBED_DIR / "api_keys" / str(int(owner_id))


def _out_json_path(owner_id: int, model: str) -> Path:
    return _owner_dir(owner_id) / f"knowledge_embedded_{model}.json"


def _out_npz_path(owner_id: int, model: str) -> Path:
    return _owner_dir(owner_id) / f"knowledge_embedded_{model}.npz"


def _out_tags_npz_path(owner_id: int, model: str) -> Path:
    return _owner_dir(owner_id) / f"tags_embedded_{model}.npz"


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
            if not isinstance(emb, list):
                raise RuntimeError("kb: invalid embedding row")
            result.append([float(x) for x in emb])

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

    owner_dir = _owner_dir(api_key_id)
    owner_dir.mkdir(parents=True, exist_ok=True)

    # ---- Main TEXT embeddings for KB ----
    try:
        embs = await _embed_texts([e["text"] for e in entries], model=model)
    except Exception as e:
        logger.exception(
            "kb: failed to embed KB for api_key_id=%s kb_id=%s",
            api_key_id,
            kb_id,
        )
        async with session_scope() as db:
            kb = await db.get(ApiKeyKnowledge, kb_id)
            if kb:
                kb.status = "failed"
                kb.error = str(e)
                kb.chunks_count = 0
            await db.flush()
        await _notify_kb_status(api_key_id, kb_id, status="failed", error=str(e))
        return

    if len(embs) != len(entries):
        err_msg = f"Embedding size mismatch: entries={len(entries)} embs={len(embs)}"
        logger.error("kb: %s", err_msg)
        async with session_scope() as db:
            kb = await db.get(ApiKeyKnowledge, kb_id)
            if kb:
                kb.status = "failed"
                kb.error = err_msg
                kb.chunks_count = 0
            await db.flush()
        await _notify_kb_status(api_key_id, kb_id, status="failed", error=err_msg)
        return

    for entry, vec in zip(entries, embs):
        entry["emb"] = vec

    # ---- Persist JSON snapshot ----
    out_json = _out_json_path(api_key_id, model)
    tmp_json = out_json.with_suffix(out_json.suffix + ".tmp")

    try:
        with open(tmp_json, "w", encoding="utf-8") as f:
            json.dump(entries, f, ensure_ascii=False, indent=2)
        tmp_json.replace(out_json)
        logger.info(
            "kb: saved JSON embeddings for api_key_id=%s kb_id=%s -> %s (entries=%d)",
            api_key_id,
            kb_id,
            out_json,
            len(entries),
        )
    except Exception:
        logger.exception(
            "kb: failed to write JSON embeddings for api_key_id=%s kb_id=%s",
            api_key_id,
            kb_id,
        )

    # ---- Build NPZ for TEXT embeddings ----
    try:
        state = await asyncio.to_thread(_build_state, entries)
    except Exception:
        logger.exception(
            "kb: _build_state raised for api_key_id=%s kb_id=%s",
            api_key_id,
            kb_id,
        )
        state = None

    if not state:
        async with session_scope() as db:
            kb = await db.get(ApiKeyKnowledge, kb_id)
            if kb:
                kb.status = "failed"
                kb.error = "Failed to build NPZ state"
                kb.chunks_count = 0
            await db.flush()
        await _notify_kb_status(api_key_id, kb_id, status="failed", error="Failed to build NPZ state")
        return

    try:
        E = state["E"]
        mean = state["mean"]
        ids = np.asarray(state["ids"], dtype=object)
        texts = np.asarray(state["texts"], dtype=object)
        meta = np.asarray(
            {
                "model": model,
                "api_model": model,
                "dim": int(E.shape[1]),
                "created": int(time.time()),
            },
            dtype=object,
        )

        out_npz = _out_npz_path(api_key_id, model)
        tmp_npz = out_npz.with_suffix(out_npz.suffix + ".tmp")

        with open(tmp_npz, "wb") as f:
            np.savez_compressed(
                f,
                E=E,
                mean=mean,
                ids=ids,
                texts=texts,
                meta=meta,
            )

        tmp_npz.replace(out_npz)

        logger.info(
            "kb: NPZ snapshot saved for api_key_id=%s kb_id=%s -> %s (E=%s)",
            api_key_id,
            kb_id,
            out_npz,
            E.shape,
        )
    except Exception as e:
        logger.exception(
            "kb: failed to write NPZ for api_key_id=%s kb_id=%s",
            api_key_id,
            kb_id,
        )
        async with session_scope() as db:
            kb = await db.get(ApiKeyKnowledge, kb_id)
            if kb:
                kb.status = "failed"
                kb.error = f"Failed to write NPZ: {e}"
                kb.chunks_count = 0
            await db.flush()
        await _notify_kb_status(api_key_id, kb_id, status="failed", error=f"Failed to write NPZ: {e}")
        return

    try:
        tag_strings: List[str] = []
        tag_seen: set[str] = set()
        for e in entries:
            tags = e.get("tags") or []
            if not isinstance(tags, list):
                continue
            for t in tags:
                if not isinstance(t, str):
                    continue
                ts = t.strip()
                if not ts:
                    continue
                if ts not in tag_seen:
                    tag_seen.add(ts)
                    tag_strings.append(ts)

        tag_emb_map: Dict[str, List[float]] = {}
        if tag_strings:
            tag_embs = await _embed_texts(tag_strings, model=model)
            if len(tag_embs) != len(tag_strings):
                raise RuntimeError(
                    f"Tag embedding size mismatch: tags={len(tag_strings)} embs={len(tag_embs)}"
                )
            for ts, vec in zip(tag_strings, tag_embs):
                tag_emb_map[ts] = [float(x) for x in vec]

        tag_rows: List[List[float]] = []
        tag_item_ids: List[str] = []
        tag_item_texts: List[str] = []
        tag_texts: List[str] = []

        for e in entries:
            tags = e.get("tags") or []
            if not isinstance(tags, list):
                continue

            eid = str(e.get("id", "") or "")
            if not eid:
                continue
            text = str(e.get("text", "") or "")

            for t in tags:
                if not isinstance(t, str):
                    continue
                ts = t.strip()
                if not ts:
                    continue
                v = tag_emb_map.get(ts)
                if v is None:
                    continue
                norm = float(np.linalg.norm(v))
                if not np.isfinite(norm) or norm <= 0.0:
                    continue
                vv = [float(x / norm) for x in v]
                tag_rows.append(vv)
                tag_item_ids.append(eid)
                tag_item_texts.append(text)
                tag_texts.append(ts)

        if tag_rows:
            TE_tags = np.asarray(tag_rows, dtype=np.float32)
            ids_tags = np.asarray(tag_item_ids, dtype=object)
            texts_tags = np.asarray(tag_item_texts, dtype=object)
            tags_names = np.asarray(tag_texts, dtype=object)
            meta_tags = np.asarray(
                {
                    "model": model,
                    "api_model": model,
                    "dim": int(TE_tags.shape[1]),
                    "format_version": 2,
                    "created": int(time.time()),
                },
                dtype=object,
            )

            out_tags = _out_tags_npz_path(api_key_id, model)
            tmp_tags = out_tags.with_suffix(out_tags.suffix + ".tmp")
            with open(tmp_tags, "wb") as f:
                np.savez_compressed(
                    f,
                    TE=TE_tags,
                    tag_item_ids=ids_tags,
                    tag_item_texts=texts_tags,
                    tag_texts=tags_names,
                    meta=meta_tags,
                )

            tmp_tags.replace(out_tags)

            logger.info(
                "kb: TAGS NPZ snapshot saved for api_key_id=%s kb_id=%s -> %s (TE=%s)",
                api_key_id,
                kb_id,
                out_tags,
                TE_tags.shape,
            )
        else:
            logger.info(
                "kb: TAGS NPZ not created (no entries with usable tags) for api_key_id=%s kb_id=%s",
                api_key_id,
                kb_id,
            )
    except Exception:
        logger.exception(
            "kb: failed to build TAGS NPZ for api_key_id=%s kb_id=%s",
            api_key_id,
            kb_id,
        )

    async with session_scope() as db:
        kb = await db.get(ApiKeyKnowledge, kb_id)
        if kb:
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
                    await db.execute(
                        delete(ApiKeyKnowledge).where(ApiKeyKnowledge.id.in_(stale_ids))
                    )
                    logger.info(
                        "kb: GC old ApiKeyKnowledge versions for api_key_id=%s: kept=%d, removed=%d",
                        api_key_id,
                        max_versions,
                        len(stale_ids),
                    )

        await db.flush()
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

    d = _owner_dir(owner_id)
    try:
        if d.exists():
            shutil.rmtree(d)
            logger.info(
                "kb: removed owner_dir for api_key_id=%s path=%s",
                owner_id,
                d,
            )
        else:
            logger.info(
                "kb: owner_dir does not exist for api_key_id=%s path=%s",
                owner_id,
                d,
            )
    except Exception:
        logger.exception(
            "kb: failed to remove owner_dir for api_key_id=%s path=%s",
            owner_id,
            d,
        )


@celery.task(name="kb.clear_for_api_key")
def clear_for_api_key(api_key_id: int) -> None:

    _remove_owner_dir(int(api_key_id))
    invalidate_api_kb_cache(api_key_id)
    invalidate_tags_index(api_key_id)


async def _gc_orphan_api_key_dirs_async() -> None:

    base = EMBED_DIR / "api_keys"
    if not base.exists():
        logger.info("kb: api_keys dir %s does not exist; nothing to GC", base)
        return

    subdirs = [d for d in base.iterdir() if d.is_dir()]
    if not subdirs:
        logger.info("kb: api_keys dir %s has no subdirs; nothing to GC", base)
        return

    candidate_ids: List[int] = []
    for d in subdirs:
        name = d.name
        try:
            kid = int(name)
        except (TypeError, ValueError):
            logger.warning(
                "kb: skipping non-numeric api_keys subdir: %s",
                d,
            )
            continue
        candidate_ids.append(kid)

    if not candidate_ids:
        logger.info(
            "kb: GC - no numeric api_key dirs found under %s",
            base,
        )
        return

    async with session_scope(read_only=True) as db:
        res = await db.execute(
            select(ApiKeyKnowledge.api_key_id)
            .where(ApiKeyKnowledge.api_key_id.in_(candidate_ids))
            .distinct()
        )
        used_ids = {int(x) for x in res.scalars().all()}

    orphan_ids = [kid for kid in candidate_ids if kid not in used_ids]

    if not orphan_ids:
        logger.info(
            "kb: GC - no orphan api_key embedding dirs found (checked=%d)",
            len(candidate_ids),
        )
        return

    for oid in orphan_ids:
        _remove_owner_dir(oid)

    logger.info(
        "kb: GC - removed %d orphan api_key embedding dirs out of %d candidates",
        len(orphan_ids),
        len(candidate_ids),
    )


@celery.task(name="kb.gc_orphan_api_key_dirs", ignore_result=True)
def gc_orphan_api_key_dirs() -> None:

    try:
        _run(_gc_orphan_api_key_dirs_async())
    except Exception:
        logger.exception("kb: gc_orphan_api_key_dirs failed")
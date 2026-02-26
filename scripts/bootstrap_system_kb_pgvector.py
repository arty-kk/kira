from __future__ import annotations

import argparse
import asyncio
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Tuple

from sqlalchemy import delete, select, text

from app.clients.openai_client import _call_openai_with_retry
from app.config import settings
from app.core.db import session_scope
from app.core.embedding_utils import normalize_embedding_row
from app.core.models import RagTagVector

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def _load_items(path: Path) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("knowledge file must contain JSON array")
    result: List[Dict[str, Any]] = []
    items_with_duplicate_tags = 0
    duplicates_removed_total = 0
    for row in payload:
        if not isinstance(row, dict):
            continue
        text = str(row.get("text") or "").strip()
        if not text:
            continue

        normalized_tags = [str(t).strip() for t in (row.get("tags") or []) if str(t).strip()]
        deduplicated_tags: List[str] = []
        seen_tags = set()
        for tag in normalized_tags:
            if tag in seen_tags:
                duplicates_removed_total += 1
                continue
            seen_tags.add(tag)
            deduplicated_tags.append(tag)

        if len(deduplicated_tags) < len(normalized_tags):
            items_with_duplicate_tags += 1

        result.append(
            {
                "id": str(row.get("id") or ""),
                "text": text,
                "tags": deduplicated_tags,
            }
        )

    if duplicates_removed_total > 0:
        logger.warning(
            "KB tag duplicates removed during load: items_with_duplicate_tags=%d duplicates_removed_total=%d",
            items_with_duplicate_tags,
            duplicates_removed_total,
        )

    return result, {
        "items_with_duplicate_tags": items_with_duplicate_tags,
        "duplicates_removed_total": duplicates_removed_total,
    }


async def _embed_texts_batched(texts: List[str], model: str, batch_size: int) -> List[List[float]]:
    if not texts:
        return []

    expected_dim = int(getattr(settings, "RAG_VECTOR_DIM", 3072) or 3072)
    vectors: List[List[float]] = []
    total = len(texts)
    for start in range(0, total, batch_size):
        end = min(start + batch_size, total)
        logger.info("embedding batch %d/%d: items=%d..%d model=%s", (start // batch_size) + 1, (total + batch_size - 1) // batch_size, start + 1, end, model)
        batch = texts[start:end]
        resp = await _call_openai_with_retry(endpoint="embeddings.create", model=model, input=batch, encoding_format="float")
        batch_vectors: List[List[float]] = []
        for d in resp.data:
            try:
                batch_vectors.append(normalize_embedding_row(d.embedding, expected_dim=expected_dim))
            except RuntimeError as exc:
                msg = str(exc)
                if "invalid embedding row shape=" in msg or "invalid embedding dim" in msg:
                    raise RuntimeError(f"invalid embedding row shape/dim: {msg}") from exc
                raise
        if len(batch_vectors) != len(batch):
            raise RuntimeError(f"embedding response size mismatch: requested={len(batch)} got={len(batch_vectors)}")
        vectors.extend(batch_vectors)

    return vectors


async def _run(args: argparse.Namespace) -> None:
    kb_path = Path(args.kb_file)
    kb_items, load_stats = _load_items(kb_path)
    model = args.model
    expected_dim = int(getattr(settings, "RAG_VECTOR_DIM", 3072) or 3072)
    batch_size = max(1, int(args.batch_size))

    logger.info("bootstrap start: kb_file=%s items=%d model=%s batch_size=%d", kb_path, len(kb_items), model, batch_size)

    if not kb_items:
        raise RuntimeError(f"no valid KB items loaded from {kb_path}")

    if load_stats["duplicates_removed_total"] > 0:
        logger.info(
            "kb load cleanup summary: items_with_duplicate_tags=%d duplicates_removed_total=%d",
            load_stats["items_with_duplicate_tags"],
            load_stats["duplicates_removed_total"],
        )

    async with session_scope(read_only=True) as db:
        current_schema = (await db.execute(text("select current_schema()"))).scalar_one_or_none()
        if not current_schema:
            raise RuntimeError("schema mismatch: current_schema() returned empty value")

        rag_table_exists = (
            await db.execute(
                text("select to_regclass(current_schema() || '.rag_tag_vectors') is not null")
            )
        ).scalar_one()
        if not rag_table_exists:
            raise RuntimeError(
                "missing table: "
                f"{current_schema}.rag_tag_vectors is not available in current schema; "
                "run migrate + DB smoke-check before bootstrap-rag"
            )

        cnt = await db.execute(
            select(RagTagVector.id).where(
                RagTagVector.scope == "global",
                RagTagVector.embedding_model == model,
            ).limit(1)
        )
        exists = cnt.scalar_one_or_none() is not None
    if exists and not args.force:
        logger.info("system kb already prepared for model=%s, skip (use --force to rebuild)", model)
        return

    tag_vocab = sorted({t for it in kb_items for t in it["tags"]})
    logger.info("tag vocabulary built: unique_tags=%d", len(tag_vocab))
    if not tag_vocab:
        raise RuntimeError(f"no tags found in KB payload: {kb_path}")

    tag_embs = await _embed_texts_batched(tag_vocab, model, batch_size=batch_size) if tag_vocab else []
    if len(tag_embs) != len(tag_vocab):
        raise RuntimeError(f"tag embedding size mismatch: tags={len(tag_vocab)} embs={len(tag_embs)}")

    for vec in tag_embs:
        if len(vec) != expected_dim:
            raise RuntimeError(f"invalid tag embedding dim: got={len(vec)} expected={expected_dim}")
    tag_map = {tag: emb for tag, emb in zip(tag_vocab, tag_embs)}

    async with session_scope() as db:
        await db.execute(delete(RagTagVector).where(RagTagVector.scope == "global", RagTagVector.embedding_model == model))

        rows_added = 0
        flush_every = max(1, int(args.flush_every))
        pending: List[RagTagVector] = []
        for item in kb_items:
            for tag in item["tags"]:
                emb = tag_map.get(tag)
                if emb is None:
                    continue
                pending.append(
                    RagTagVector(
                        scope="global",
                        owner_id=None,
                        kb_id=None,
                        embedding_model=model,
                        external_id=item["id"],
                        text=item["text"],
                        tag=tag,
                        embedding=emb,
                    )
                )
                if len(pending) >= flush_every:
                    db.add_all(pending)
                    await db.flush()
                    rows_added += len(pending)
                    logger.info("db flush complete: total_rows=%d", rows_added)
                    pending.clear()

        if pending:
            db.add_all(pending)
            await db.flush()
            rows_added += len(pending)
            logger.info("db flush complete: total_rows=%d", rows_added)

    logger.info("system tag-index prepared in pgvector: rows=%d unique_tags=%d model=%s", rows_added, len(tag_vocab), model)


def main() -> None:
    parser = argparse.ArgumentParser(description="Build system KB vectors directly into PostgreSQL/pgvector")
    parser.add_argument("--kb-file", default="app/services/responder/rag/knowledge_on.json")
    parser.add_argument("--model", default=settings.EMBEDDING_MODEL)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--batch-size", type=int, default=128)
    parser.add_argument("--flush-every", type=int, default=500)
    args = parser.parse_args()
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from pathlib import Path
from typing import Any, Dict, List

from sqlalchemy import delete, select

from app.clients.openai_client import _call_openai_with_retry
from app.config import settings
from app.core.db import session_scope
from app.core.models import RagTagVector

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def _load_items(path: Path) -> List[Dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("knowledge file must contain JSON array")
    result: List[Dict[str, Any]] = []
    for row in payload:
        if not isinstance(row, dict):
            continue
        text = str(row.get("text") or "").strip()
        if not text:
            continue
        result.append(
            {
                "id": str(row.get("id") or ""),
                "text": text,
                "tags": [str(t).strip() for t in (row.get("tags") or []) if str(t).strip()],
            }
        )
    return result


async def _embed_texts_batched(texts: List[str], model: str, batch_size: int) -> List[List[float]]:
    if not texts:
        return []

    vectors: List[List[float]] = []
    total = len(texts)
    for start in range(0, total, batch_size):
        end = min(start + batch_size, total)
        logger.info("embedding batch %d/%d: items=%d..%d model=%s", (start // batch_size) + 1, (total + batch_size - 1) // batch_size, start + 1, end, model)
        batch = texts[start:end]
        resp = await _call_openai_with_retry(endpoint="embeddings.create", model=model, input=batch, encoding_format="float")
        batch_vectors = [[float(x) for x in d.embedding] for d in resp.data]
        if len(batch_vectors) != len(batch):
            raise RuntimeError(f"embedding response size mismatch: requested={len(batch)} got={len(batch_vectors)}")
        vectors.extend(batch_vectors)

    return vectors


async def _run(args: argparse.Namespace) -> None:
    kb_path = Path(args.kb_file)
    kb_items = _load_items(kb_path)
    model = args.model
    expected_dim = int(getattr(settings, "RAG_VECTOR_DIM", 3072) or 3072)
    batch_size = max(1, int(args.batch_size))

    logger.info("bootstrap start: kb_file=%s items=%d model=%s batch_size=%d", kb_path, len(kb_items), model, batch_size)

    if not kb_items:
        raise RuntimeError(f"no valid KB items loaded from {kb_path}")

    async with session_scope(read_only=True) as db:
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

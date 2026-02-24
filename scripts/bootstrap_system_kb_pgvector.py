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


async def _embed_texts(texts: List[str], model: str) -> List[List[float]]:
    if not texts:
        return []
    resp = await _call_openai_with_retry(endpoint="embeddings.create", model=model, input=texts, encoding_format="float")
    return [[float(x) for x in d.embedding] for d in resp.data]


async def _run(args: argparse.Namespace) -> None:
    kb_items = _load_items(Path(args.kb_file))
    model = args.model
    expected_dim = int(getattr(settings, "RAG_VECTOR_DIM", 3072) or 3072)

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
    tag_embs = await _embed_texts(tag_vocab, model) if tag_vocab else []
    for vec in tag_embs:
        if len(vec) != expected_dim:
            raise RuntimeError(f"invalid tag embedding dim: got={len(vec)} expected={expected_dim}")
    tag_map = {tag: emb for tag, emb in zip(tag_vocab, tag_embs)}

    async with session_scope() as db:
        await db.execute(delete(RagTagVector).where(RagTagVector.scope == "global", RagTagVector.embedding_model == model))

        tag_rows = []
        for item in kb_items:
            for tag in item["tags"]:
                emb = tag_map.get(tag)
                if emb is None:
                    continue
                tag_rows.append(
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
        if tag_rows:
            db.add_all(tag_rows)
        await db.flush()

    logger.info("system tag-index prepared in pgvector: rows=%d unique_tags=%d model=%s", len(tag_rows), len(tag_vocab), model)


def main() -> None:
    parser = argparse.ArgumentParser(description="Build system KB vectors directly into PostgreSQL/pgvector")
    parser.add_argument("--kb-file", default="app/services/responder/rag/knowledge_on.json")
    parser.add_argument("--model", default=settings.EMBEDDING_MODEL)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()

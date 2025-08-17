cat >app/services/responder/rag/relevance.py<< 'EOF'
# app/services/responder/rag/relevance.py
import logging
import asyncio
import re
from typing import List, Tuple, Optional

from app.config import settings
from app.clients.openai_client import _call_openai_with_retry
from .knowledge_proc import get_relevant
from .keyword_filter import get_keyword_processor

logger = logging.getLogger(__name__)

_CLEAN = re.compile(r"[^\w\s]")


async def _llm_yesno(query: str, snippets: List[str]) -> bool:
    if not snippets:
        return False

    joined = "\n".join(f"{i+1}) {s}" for i, s in enumerate(snippets))

    try:
        resp = await asyncio.wait_for(
            _call_openai_with_retry(
                model=settings.BASE_MODEL,
                temperature=0.0,
                max_completion_tokens=4,
                messages=[
                    {"role": "system", "content": "Answer ONLY 'Yes' or 'No'."},
                    {
                        "role": "user",
                        "content": (
                            "Decide if the user's message explicitly requests information present in the snippets.\n"
                            "Say 'Yes' ONLY if at least one snippet clearly matches the user's intent/content;\n"
                            "Say 'No' for greetings, small talk, generic chit-chat, role-play hooks, or unrelated text.\n\n"
                            f"User message:\n{query}\n\nSnippets:\n{joined}"
                        ),
                    },
                ],
            ),
            timeout=10.0,
        )
        ans = (resp.choices[0].message.content or "").strip().lower()
        ok = ans.startswith("y")
        logger.debug("gate: llm_yesno verdict=%s", ok)
        return ok
    except Exception:
        logger.exception("gate: LLM yes/no failed")
        return False


async def is_relevant(
    text: str, *, model: str, threshold: float, return_hits: bool
) -> Tuple[bool, Optional[List[Tuple[float, str, str]]]]:

    clean = _CLEAN.sub(" ", text).lower()
    hits: List[Tuple[float, str, str]] = []

    kw_proc = get_keyword_processor(model=model)
    kws = [kw for kw in kw_proc.extract_keywords(clean) if len(kw) >= 4]

    if len(kws) >= 1:
        try:
            hits = await get_relevant(text, model_name=model)
        except Exception:
            logger.exception("gate: get_relevant failed on keyword path")
            hits = []

        if not hits:
            logger.info(
                "gate: model=%s ok=%s kws=%d hits=%d (keyword-path no hits)",
                model, False, len(kws), 0
            )
            return False, None

        snippets = [h[2] for h in hits[: min(3, settings.KNOWLEDGE_TOP_K)]]
        ok = await _llm_yesno(text, snippets)
        logger.info(
            "gate: model=%s ok=%s kws=%d top=%.3f hits=%d (keyword-path)",
            model, ok, len(kws), (hits[0][0] if hits else -1.0), len(hits)
        )
        return (ok, hits if (ok and return_hits) else None)

    try:
        hits = await get_relevant(text, model_name=model)
    except Exception:
        logger.exception("gate: get_relevant failed")
        return False, None

    if not hits:
        logger.info(
            "gate: model=%s ok=%s kws=%d top=%.3f hits=%d (no-hits)",
            model, False, len(kws), -1.0, 0
        )
        return False, (hits if return_hits else None)

    top = hits[0][0]
    margin = settings.RELEVANCE_MARGIN
    if top < threshold - margin:
        logger.info(
            "gate: model=%s ok=%s kws=%d top=%.3f thr=%.3f hits=%d (below-thr)",
            model, False, len(kws), top, threshold, len(hits)
        )
        return False, (hits if return_hits else None)

    snippets = [h[2] for h in hits[: min(3, settings.KNOWLEDGE_TOP_K)]]
    ok = await _llm_yesno(text, snippets)
    logger.info(
        "gate: model=%s ok=%s kws=%d top=%.3f thr=%.3f hits=%d",
        model, ok, len(kws), top, threshold, len(hits)
    )
    return (ok, hits if (ok and return_hits) else None)
EOF
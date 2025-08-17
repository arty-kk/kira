cat >app/services/responder/coref/resolve_coref.py<< 'EOF'
#app/services/responder/coref/resolve_coref.py
import logging
import asyncio

from typing import List, Dict

from app.clients.openai_client import _call_openai_with_retry
from app.config import settings

logger = logging.getLogger(__name__)

_COT_PROMPT = """You are a coreference-/deixis-resolution assistant.

Instructions (CoT Phase):
1. Identify all pronouns in the user query that refer to entities, events, persons, or objects NOT to the assistant itself and/or the user who wrote the query.
2. Ignore in coreference-/deixis-resolution any pronouns that refer to the assistant itself and/or the user who wrote the query (I, we, us, you, your, my, me).
3. For each pronoun (not related to the assistant and/or user) locate its antecedent in the provided conversation snippet.
4. If any pronouns that do NOT refer to the assistant and/or user do not have a clear logical antecedent, do not consider those pronouns in coreference resolution.
5. Return **only** a numbered list like:
   1. pronoun → antecedent
   2. pronoun → antecedent
6. If no pronouns require resolution, reply exactly: No pronouns to resolve.

Example 1:
Snippet:
  Alice went home. She was tired.
Query:
  "Can you tell me if she locked the door?"
Reply:
  1. she → Alice

Example 2:
Snippet:
  Sam has read an interesting book.
Query:
  "What was the title of the book he read?"
Reply:
  1. he → Sam

Example 2:
Snippet:
  Alice has read an interesting book.
Query:
  "What was the title of this book?"
Reply: No pronouns to resolve.
"""

_FINAL_PROMPT = """Now rewrite the user's query using the resolved antecedents.

Rules (Rewrite Phase):
1. Replace each pronoun (not related to the assistant itself and/or the user who wrote the query) with its specific antecedent.
2. Preserve the original meaning and conversational context.
3. **Return exactly** the rewritten query string, with no numbering, quotes, or extra text.

Example:
Original: "Can you tell me if she locked the door?"
Rewritten: "Can you tell me if Alice locked the door?"
"""

COT_TIMEOUT = 15
FINAL_TIMEOUT = 15

async def resolve_coref(text: str, history: List[Dict[str, str]]) -> str:

    snippet = [m for m in (history or []) if m.get("role") in ("user", "assistant")][-10:]

    def _clean_rewrite(s: str, original: str) -> str:
        s = (s or "").strip()
        s = re.sub(r"^```(?:\w+)?\s*|\s*```$", "", s, flags=re.IGNORECASE).strip()
        s = re.sub(r"^(rewritten|rewrite|final)\s*:\s*", "", s, flags=re.IGNORECASE).strip()
        s = s.strip('"\u00ab\u00bb“”‘’')
        s = " ".join(s.split())
        if not s or s.lower() in {"no pronouns to resolve", "none", "n/a"}:
            return original
        if len(s) > 4 * max(10, len(original)):
            return original
        return s

    cot_messages = [
        {
            "role": "system",
            "content": _COT_PROMPT
        },
        *snippet,
        {
            "role": "user",
            "content": f"Identify the antecedents for each referring expression in this user query:\n\"{text}\""
        },
    ]
    try:
        start = asyncio.get_running_loop().time()
        cot_resp = await asyncio.wait_for(
            _call_openai_with_retry(
                model=settings.BASE_MODEL,
                messages=cot_messages,
                temperature=0.0,
                top_p=1.0,
                max_completion_tokens=1000,
            ),
            timeout=COT_TIMEOUT,
        )
        if not cot_resp.choices:
            raise ValueError("empty choices in CoT response")
        reasoning = cot_resp.choices[0].message.content.strip()
        logger.debug("CoT completed in %.2fs", asyncio.get_running_loop().time() - start)
    except asyncio.TimeoutError:
        logger.warning("resolve_coref CoT step timed out after %.1fs", COT_TIMEOUT)
        reasoning = None
    except Exception as e:
        logger.exception("resolve_coref CoT step failed", exc_info=True)
        reasoning = None

    prompt_content = _FINAL_PROMPT
    if reasoning:
        prompt_content += (
            "\n\n# Context (do not output this):\n"
            f"{reasoning.strip()}"
        )
    prompt_content += (
        "\n\nIMPORTANT: Only output the rewritten query. NO explanations, NO chain-of-thought."
    )

    final_messages = [
        {"role": "system", "content": prompt_content},
        {"role": "user",   "content": text},
    ]

    try:
        start = asyncio.get_running_loop().time()
        final_resp = await asyncio.wait_for(
            _call_openai_with_retry(
                model=settings.BASE_MODEL,
                messages=final_messages,
                temperature=0.0,
                top_p=1.0,
                max_completion_tokens=1000,
            ),
            timeout=FINAL_TIMEOUT,
        )
        if not final_resp.choices:
            raise ValueError("empty choices in final response")
        rewritten_raw = final_resp.choices[0].message.content
        rewritten = _clean_rewrite(rewritten_raw, text)
        logger.debug("Final rewrite completed in %.2fs", asyncio.get_running_loop().time() - start)
        return rewritten
    except asyncio.TimeoutError:
        logger.warning("resolve_coref final rewrite timed out after %.1fs", FINAL_TIMEOUT)
    except Exception:
        logger.exception("resolve_coref final step failed", exc_info=True)

    return text
EOF
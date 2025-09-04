#app/services/responder/coref/resolve_coref.py
import logging
import asyncio
import re

from typing import List, Dict

from app.clients.openai_client import _call_openai_with_retry, _msg, _get_output_text
from app.config import settings

logger = logging.getLogger(__name__)

_COT_PROMPT = """You are a **multilingual** coreference/deixis-resolution assistant.

Instructions (CoT Phase):
1. Identify pronouns that may require coreference: third-person pronouns (he, she, it, they) and **demonstrative pronouns used pronominally** (this/that/these/those when they stand alone or with "one/ones").
2. EXCLUDE from consideration:
   - first- and second-person forms (I, me, my, you, your) in any language;
   - demonstratives used as **determiners** before a noun (e.g., "that book", "this idea");
   - "that" used as a **conjunction/complementizer** introducing a clause (e.g., "I think that we should go");
   - relative "that" inside noun clauses (do not try to replace it).
3. For each eligible pronoun, locate its antecedent in the provided conversation snippet.
4. If a pronoun has no clear antecedent, ignore it.
5. Return **only** a numbered list like:
   1. pronoun → antecedent
   2. pronoun → antecedent
6. If no pronouns require resolution, reply exactly: No pronouns to resolve

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

Example 3:
Snippet:
  Alice has read an interesting book.
Query:
  "What was the title of this book?"
Reply: No pronouns to resolve.

Example 4:
Snippet:
  Alice has read an interesting book: The Great Gatsby.
Query:
  "What was the title of this"
Reply: 
  1. this → The Great Gatsby

Example 5:
Snippet:
  I put the red folder on your desk.
Query:
  "Could you hand me that?"
Reply:
  1. that → the red folder

Example 6:
Query:
  "I think that we should go."
Reply: No pronouns to resolve
"""

_FINAL_PROMPT = """Rewrite the user's query using the resolved antecedents.

Rules (Rewrite Phase):
1. Replace only second- or third-person pronouns (except *I'm*, *my*, *me*, *you*, *your* in any language) and **pronominal** demonstratives (this/that/these/those standing alone or with "one/ones") with their specific antecedents.
2. Do **not** modify first- or second-person forms, demonstratives used as determiners before nouns, or "that" used as a conjunction/complementizer or as a relative pronoun.
3. Preserve the original meaning and conversational context.
4. **Return exactly** the rewritten query string, with no numbering, quotes, or extra text.

Example:
Original: "Can you tell me if she locked the door?"
Rewritten: "Can you tell me if Alice locked the door?"

Example:
Snippet: I put the red folder on your desk.
Original: "Could you hand me that?"
Rewritten: "Could you hand me the red folder?"
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

    _snippet_input = [
        _msg(m["role"], str(m.get("content", "")))
        for m in snippet
    ]
    cot_input = [
        _msg("system", _COT_PROMPT),
        *_snippet_input,
        _msg("user", f'Identify the antecedents (if they have one) in this user query:\n"{text}"'),
    ]
    try:
        cot_resp = await asyncio.wait_for(
            _call_openai_with_retry(
                endpoint="responses.create",
                model=settings.BASE_MODEL,
                input=cot_input,
                max_output_tokens=1000,
                temperature=0,
            ),
            timeout=COT_TIMEOUT,
        )
        reasoning = (_get_output_text(cot_resp) or "").strip()
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

    final_input = [
        _msg("system", prompt_content),
        _msg("user", text),
    ]

    try:
        final_resp = await asyncio.wait_for(
            _call_openai_with_retry(
                endpoint="responses.create",
                model=settings.BASE_MODEL,
                input=final_input,
                max_output_tokens=1000,
                temperature=0,
            ),
            timeout=FINAL_TIMEOUT,
        )
        rewritten_raw = _get_output_text(final_resp)
        rewritten = _clean_rewrite(rewritten_raw, text)
        return rewritten
    except asyncio.TimeoutError:
        logger.warning("resolve_coref final rewrite timed out after %.1fs", FINAL_TIMEOUT)
    except Exception:
        logger.exception("resolve_coref final step failed", exc_info=True)

    return text

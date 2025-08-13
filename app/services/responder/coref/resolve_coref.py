cat >app/services/responder/coref/resolve_coref.py<< 'EOF'
#app/services/responder/coref/resolve_coref.py
import logging
import asyncio

from typing import List, Dict

from app.clients.openai_client import _call_openai_with_retry
from app.config import settings

logger = logging.getLogger(__name__)

_COT_PROMPT = """You are a coreference-resolution assistant.

Instructions (CoT Phase):
1. Identify all pronouns in the user's new query that refer to entities not related to the assistant and/or user.
2. Ignore any pronoun (first- or second-person) whose antecedent is related to the assistant and/or user (I, me, we, us, you, your, твой, 您, etc.).
3. For each pronoun (not related to the assistant and/or user) locate its antecedent in the provided conversation snippet.
4. Return **only** a numbered list like:
   1. pronoun → antecedent
   2. pronoun → antecedent
5. If no pronouns require resolution, reply exactly: No pronouns to resolve.

Example:
Snippet:
  Alice went home. She was tired.
Query:
  "Can you tell me if she locked the door?"
Reply:
  1. she → Alice
"""

_FINAL_PROMPT = """Now rewrite the user's query using the resolved antecedents.

Rules (Rewrite Phase):
1. Replace each pronoun (not related to the assistant and/or user) with its specific antecedent.
2. Preserve the original meaning and conversational context.
3. **Return exactly** the rewritten query string, with no numbering, quotes, or extra text.

Example:
Original: "Can you tell me if she locked the door?"
Rewritten: "Can you tell me if Alice locked the door?"
"""

COT_TIMEOUT = 15
FINAL_TIMEOUT = 15

async def resolve_coref(text: str, history: List[Dict[str, str]]) -> str:

    snippet = history[-10:]

    cot_messages = [
        {
            "role": "system",
            "content": _COT_PROMPT
        },
        *snippet,
        {
            "role": "user",
            "content": f"Identify the antecedents for each pronoun in this user query:\n\"{text}\""
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
        rewritten = final_resp.choices[0].message.content.strip()
        logger.debug("Final rewrite completed in %.2fs", asyncio.get_running_loop().time() - start)
        return rewritten
    except asyncio.TimeoutError:
        logger.warning("resolve_coref final rewrite timed out after %.1fs", FINAL_TIMEOUT)
    except Exception:
        logger.exception("resolve_coref final step failed", exc_info=True)

    return text
EOF
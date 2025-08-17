cat >app/services/responder/coref/needs_coref.py<< 'EOF'
#app/services/responder/coref/needs_coref.py
import logging
import asyncio

from app.clients.openai_client import _call_openai_with_retry
from app.config import settings

logger = logging.getLogger(__name__)


_COREF_TIMEOUT = getattr(settings, "COREF_TIMEOUT", 10.0)

_COREF_PROMPT = """You are a multilingual classifier that decides whether a user message contains any second-person, third-person, or demonstrative pronouns.

Rules:
- Return exactly one uppercase word: YES or NO (no punctuation, quotes, extra text, or newlines).
- Consider in classification only those pronouns that refer to entities, events, persons, or objects NOT related to the assistant itself and/or the user who wrote the query.
- Ignore any pronouns that refer to the assistant itself and/or the user who wrote the query (I, we, us, you, your, my, me).
- If any pronouns are contained in the user query that refer to some entities, events, persons, or objects from the snippet, return YES. Otherwise, return NO.
- Output final reply only in English.

Examples:
Text: "John went to the park, and then he sat on a bench."
Reply: YES

Text: "I am tired."
Reply: NO

Text: "Could you pass me that book?"
Reply: YES

Text: "Do you know who he is?"
Reply: YES

Your Reply:
\"\"\"{text}\"\"\""""

async def needs_coref(text: str) -> bool:

    prompt = _COREF_PROMPT.format(text=text)
    try:
        resp = await asyncio.wait_for(
            _call_openai_with_retry(
                model=settings.BASE_MODEL,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.0,
                max_completion_tokens=5,
            ),
            timeout=_COREF_TIMEOUT,
        )
        if not getattr(resp, "choices", None) or not resp.choices:
            logger.error("needs_coref: no choices returned from OpenAI")
            return False
            
        content = resp.choices[0].message.content or ""
        if not content:
            logger.error("needs_coref: empty content")
            return False

        normalized = content.strip().upper().rstrip(".")
        return normalized == "YES"
    except asyncio.TimeoutError:
        logger.warning("needs_coref timed out after %.1f sec", _COREF_TIMEOUT)
        return False
    except Exception as e:
        logger.warning("needs_coref error: %s", e)
        return False
EOF
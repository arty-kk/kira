cat >app/services/responder/coref/needs_coref.py<< EOF
#app/services/responder/coref/needs_coref.py
import logging
import asyncio

from app.clients.openai_client import _call_openai_with_retry
from app.config import settings

logger = logging.getLogger(__name__)


_COREF_TIMEOUT = getattr(settings, "COREF_TIMEOUT", 10.0)

_COREF_PROMPT = """You are a classifier that decides whether a user message contains any third-person or demonstrative pronouns referring to entities other than the assistant, thus requiring coreference resolution.

Rules:
- Return exactly one uppercase word: YES or NO (no punctuation, quotes, extra text, or newlines).
- Consider only pronouns for third-person entities (he, she, it, they, this, that, these, those, etc.) in any language.
- Ignore all second-person pronouns referring to the assistant (you, your, твой, 您, etc.).
- Ignore all first-person pronouns (I, we, us, мне, 我们, etc.).
- If any sentence in the text contains a pronoun that refers to a non-assistant entity, return YES.
- Otherwise, return NO.

Examples:
Text: "John went to the park, and then he sat on a bench."
Reply: YES

Text: "I am tired."
Reply: NO

Text: "Could you pass me that book?"
Reply: NO

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
                max_tokens=5,
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
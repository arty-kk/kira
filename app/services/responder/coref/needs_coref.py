cat >app/services/responder/coref/needs_coref.py<< EOF
#app/services/responder/coref/needs_coref.py
import logging
import asyncio

from app.clients.openai_client import _call_openai_with_retry
from app.config import settings

logger = logging.getLogger(__name__)

_COREF_TIMEOUT = 10.0

async def needs_coref(text: str) -> bool:

    prompt = (
        "You are a classifier tasked with identifying whether the following user message contains any personal or demonstrative pronouns that refer to entities other than the assistant and thus require coreference resolution.\n\n"
        "Rules:\n"
        "- Return YES if there are pronouns requiring resolution for non-assistant entities.\n"
        "- Ignore all second-person pronouns addressing the assistant in any language.\n"
        "- Do not consider first-person pronouns for coreference.\n\n"
        "If need coreference resolution — reply with exactly one uppercase word: YES or NO, with no additional text.\n\n"
        f"Your Reply:\n\"\"\"{text}\"\"\""
    )
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
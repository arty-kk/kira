cat >app/services/responder/coref/needs_coref.py<< 'EOF'
# app/services/responder/coref/needs_coref.py
import logging
import asyncio
import json

from app.clients.openai_client import _call_openai_with_retry, _get_output_text
from app.config import settings

logger = logging.getLogger(__name__)

_COREF_TIMEOUT =  15

_COREF_SYSTEM = """You are a multilingual classifier that decides whether a user message contains pronouns that may require coreference resolution.

Decision rules (language-agnostic):
- Return NO if the text contains only first- or second-person forms (e.g., first person singular/plural and second person).
- Return YES if the text contains any third-person pronouns, demonstrative pronouns used pronominally (equivalents of "this/that/these/those" when they stand alone or with "one/ones"), or interrogative/relative pronouns (e.g., equivalents of "who/whom/whose/which", or "that" used as a relative pronoun).
- Return NO when demonstratives are used as determiners before a noun (e.g., equivalent of "that book") or when "that" (or its equivalent) is used as a complementizer/conjunction introducing a clause.

Output format:
- Return a JSON object that validates the provided JSON schema with a single field: {"answer":"YES" | "NO"} in uppercase, no extra text."""

def _build_user_prompt(text: str, *, force_yesno: bool = False) -> str:
    suffix = " YES or NO" if force_yesno else ""
    return f'Text:\n"""' + (text or "") + f'"""\nReply:{suffix}'

async def _ask_model(text: str, *, force_yesno: bool = False) -> str:

    user_prompt = _build_user_prompt(text, force_yesno=force_yesno)

    resp = await asyncio.wait_for(
        _call_openai_with_retry(
            endpoint="responses.create",
            model=settings.BASE_MODEL,
            instructions=_COREF_SYSTEM,
            input=user_prompt,
            text={
                "format": {
                    "type": "json_schema",
                    "name": "yes_no",
                    "strict": True,
                    "schema": {
                        "type": "object",
                        "properties": {
                            "answer": {"type": "string", "enum": ["YES", "NO"]}
                        },
                        "required": ["answer"],
                        "additionalProperties": False
                    }
                }
            },
            temperature=0,
            max_output_tokens=16,
        ),
        timeout=10,
    )
    raw = (_get_output_text(resp) or "").strip()
    try:
        obj = json.loads(raw)
        ans = obj.get("answer")
        if isinstance(ans, str) and ans in ("YES", "NO"):
            return ans
    except Exception:
        pass

    up = raw.upper().strip().strip('."\'! `')
    if up in ("YES", "NO"):
        return up
    return ""

async def needs_coref(text: str) -> bool:

    if text is None or not str(text).strip():
        return False

    try:
        ans = await asyncio.wait_for(_ask_model(text), timeout=_COREF_TIMEOUT)
        if ans in ("YES", "NO"):
            return ans == "YES"

        ans2 = await asyncio.wait_for(_ask_model(text, force_yesno=True), timeout=_COREF_TIMEOUT)
        if ans2 in ("YES", "NO"):
            return ans2 == "YES"

        logger.warning("needs_coref: unexpected outputs (%r, %r); defaulting to NO", ans, ans2)
        return False

    except asyncio.CancelledError:
        raise
    except asyncio.TimeoutError:
        logger.warning("needs_coref timed out after %.1f sec; defaulting to NO", _COREF_TIMEOUT)
        return False
    except Exception as e:
        logger.warning("needs_coref error: %s; defaulting to NO", e)
        return False
EOF

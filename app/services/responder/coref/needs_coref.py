# app/services/responder/coref/needs_coref.py
import logging
import asyncio
import json
from typing import Any, Dict, List, Optional

from app.clients.openai_client import _call_openai_with_retry, _get_output_text
from app.config import settings
from app.prompts_base import COREF_SYSTEM_PROMPT

logger = logging.getLogger(__name__)


COREF_SYSTEM = COREF_SYSTEM_PROMPT

def _fast_no_coref(text: str) -> bool:
    """
    Safe fast-path:
    if there are no alphabetic characters at all, we can confidently return NO.
    """
    if not text:
        return True
    return not any(ch.isalpha() for ch in text)

def _build_user_prompt(text: str, *, force_yesno: bool = False) -> str:
    suffix = " YES or NO" if force_yesno else ""
    return 'Text:\n"""' + (text or "") + f'"""\nReply:{suffix}'

async def _ask_model(text: str, *, force_yesno: bool = False) -> str:
    user_prompt = _build_user_prompt(text, force_yesno=force_yesno)

    resp = await asyncio.wait_for(
        _call_openai_with_retry(
            endpoint="responses.create",
            model=settings.BASE_MODEL,
            instructions=COREF_SYSTEM,
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
        timeout=settings.BASE_MODEL_TIMEOUT,
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

async def needs_coref(text: str, history: Optional[List[Dict[str, Any]]] = None) -> bool:
    if text is None or not str(text).strip():
        return False

    text = str(text)

    if history is not None:
        snippet = [m for m in (history or []) if m.get("role") in ("user", "assistant")][-8:]
        if not any(str(m.get("content", "")).strip() for m in snippet):
            return False

    if _fast_no_coref(text):
        return False

    try:
        ans = await asyncio.wait_for(_ask_model(text), timeout=settings.BASE_MODEL_TIMEOUT)
        if ans in ("YES", "NO"):
            return ans == "YES"

        # Фоллбек на случай редких нарушений формата
        ans2 = await asyncio.wait_for(_ask_model(text, force_yesno=True), timeout=settings.BASE_MODEL_TIMEOUT)
        if ans2 in ("YES", "NO"):
            return ans2 == "YES"

        logger.warning("needs_coref: unexpected outputs (%r, %r); defaulting to NO", ans, ans2)
        return False

    except asyncio.CancelledError:
        raise
    except asyncio.TimeoutError:
        logger.warning("needs_coref timed out after %.1f sec; defaulting to NO", settings.BASE_MODEL_TIMEOUT)
        return False
    except Exception as e:
        logger.warning("needs_coref error: %s; defaulting to NO", e)
        return False

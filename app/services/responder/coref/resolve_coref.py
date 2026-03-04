# app/services/responder/coref/resolve_coref.py
import asyncio
import logging

from typing import List, Dict

from app.clients.openai_client import _call_openai_with_retry, _msg, _get_output_text
from app.config import settings
from app.prompts_base import COREF_REWRITE_PROMPT

logger = logging.getLogger(__name__)

MAX_OUTPUT_MULT = 4
SNIPPET_MAX_MESSAGES = min(6, int(getattr(settings, "COREF_SNIPPET_MAX_MESSAGES", 6) or 6))


def _build_rewrite_user_prompt(query: str, snippet: List[Dict[str, str]]) -> str:
    stm_lines: List[str] = []
    for m in snippet:
        role = str(m.get("role") or "").strip().lower()
        if role not in ("user", "assistant"):
            continue
        content = str(m.get("content") or "").strip()
        if not content:
            continue
        who = "User" if role == "user" else "Assistant"
        stm_lines.append(f"[{who}] {content}")

    stm_block = "\n".join(stm_lines) if stm_lines else "(empty)"

    return (
        "SHORT-TERM MEMORY CONTEXT\n"
        "- STM is your (Assistant) current conversation history with the user (User). "
        "Use STM to understand dialogue flow and resolve context-dependent references in the current user message.\n"
        "\n"
        "STM (oldest -> newest)\n"
        f"{stm_block}\n\n"
        "CURRENT USER MESSAGE\n"
        f"{query}\n"
    )


async def _rewrite_with_prompt(query: str, snippet: List[Dict[str, str]]) -> str:
    user_prompt = _build_rewrite_user_prompt(query, snippet)
    msgs = [
        _msg("system", COREF_REWRITE_PROMPT),
        _msg("user", user_prompt),
    ]
    try:
        resp = await asyncio.wait_for(
            _call_openai_with_retry(
                endpoint="responses.create",
                model="gpt-5-nano",
                input=msgs,
                reasoning={"effort": "low"},
                max_output_tokens=160,
            ),
            timeout=settings.REASONING_MODEL_TIMEOUT,
        )
        raw = (_get_output_text(resp) or "").strip()
        if not raw:
            return query
        rewritten = raw
        if not rewritten:
            return query
        if len(rewritten) > MAX_OUTPUT_MULT * max(10, len(query)):
            return query
        return rewritten
    except Exception:
        return query


async def resolve_coref(text: str, history: List[Dict[str, str]]) -> str:
    if text is None:
        return ""

    query = str(text)

    snippet = [m for m in (history or []) if m.get("role") in ("user", "assistant")][-SNIPPET_MAX_MESSAGES:]
    if not snippet:
        return query

    if not any(str(m.get("content", "")).strip() for m in snippet):
        return query

    rewritten = await _rewrite_with_prompt(query, snippet)
    if len(rewritten) > MAX_OUTPUT_MULT * max(10, len(query)):
        return query
    return rewritten

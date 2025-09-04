cat >app/clients/openai_client.py<< 'EOF'
# app/clients/openai_client.py
import asyncio
import logging
import json

from typing import Any
from asyncio import Semaphore

from openai import AsyncOpenAI, APIStatusError
RETRYABLE_EXC_TYPES = tuple()
try:
    from openai import RateLimitError, APIConnectionError, APITimeoutError
    RETRYABLE_EXC_TYPES = (RateLimitError, APIConnectionError, APITimeoutError)
except Exception:
    pass
from tenacity import (
    AsyncRetrying, stop_after_attempt, wait_exponential, retry_if_exception
)

from app.config import settings

logger = logging.getLogger(__name__)

_openai: AsyncOpenAI | None = None
OPENAI_SEMAPHORE = Semaphore(getattr(settings, "OPENAI_MAX_CONCURRENT_REQUESTS", 100))


def get_openai() -> AsyncOpenAI:
    global _openai
    if _openai is None:
        import openai as _lib
        logger.info("OpenAI SDK version: %s", getattr(_lib, "__version__", "?"))
        _openai = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
    return _openai


async def _call_openai_with_retry(**kwargs: Any) -> Any:

    client = get_openai()

    async with OPENAI_SEMAPHORE:
        base_kwargs = dict(kwargs)

        def _should_retry(exc: Exception) -> bool:
            if isinstance(exc, asyncio.CancelledError):
                return False
            if isinstance(exc, (ValueError, TypeError)):
                return False
            if RETRYABLE_EXC_TYPES and isinstance(exc, RETRYABLE_EXC_TYPES):
                return True
            if isinstance(exc, APIStatusError):
                try:
                    status = int(getattr(exc, "status_code", 500) or 500)
                except Exception:
                    status = 500
                return status >= 500 or status == 429
            status_maybe = getattr(exc, "status_code", None)
            if status_maybe is not None:
                try:
                    s = int(status_maybe)
                    return s >= 500 or s == 429
                except Exception:
                    pass
            return True

        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(3),
            wait=wait_exponential(min=1, max=10),
            retry=retry_if_exception(_should_retry),
            reraise=True,
        ):
            with attempt:
                try:
                    params = dict(base_kwargs)
                    endpoint = params.pop("endpoint", None) or "responses.create"

                    def _normalize_params(ep: str, p: dict) -> dict:
                        model = str(p.get("model") or "")
                        def _is_structured_json_schema(pp: dict) -> bool:
                            try:
                                fmt = (((pp.get("text") or {}).get("format")) or {})
                                return (fmt.get("type") == "json_schema")
                            except Exception:
                                return False
                        if ep == "responses.create":
                            mot = p.get("max_output_tokens")
                            if isinstance(mot, int) and mot < 16:
                                p["max_output_tokens"] = 16
                            if _is_structured_json_schema(p):
                                try:
                                    cap = 96
                                    cur = int(p.get("max_output_tokens") or cap)
                                    p["max_output_tokens"] = max(16, min(cur, cap))
                                except Exception:
                                    p["max_output_tokens"] = 96

                            if model.startswith(("gpt-5-nano", "gpt-5-mini")):
                                for k in ("temperature", "top_p", "presence_penalty", "frequency_penalty"):
                                    p.pop(k, None)
                        return p

                    params = _normalize_params(endpoint, params)

                    if endpoint not in ("responses.create", "embeddings.create", "chat.completions.create"):
                        raise ValueError(
                            f"Unsupported endpoint '{endpoint}'. "
                            "Use 'responses.create', 'chat.completions.create' or 'embeddings.create'."
                        )
                        
                    safe_params = {
                        k: v for k, v in params.items()
                        if k not in (
                            "input", "messages", "prompt",
                            "response_format", "extra_body", "text",
                            "tools", "tool_choice", "instructions"
                        )
                    }
                    logger.info("OpenAI request: endpoint=%s meta=%r", endpoint, safe_params)

                    target = client
                    for part in endpoint.split("."):
                        target = getattr(target, part)

                    try:
                        resp = await target(**params)
                        logger.info("OpenAI response: endpoint=%s usage=%r", endpoint, getattr(resp, "usage", None))
                        return resp
                    except TypeError:
                        raise

                    except APIStatusError as http_err:
                        status = getattr(http_err, "status_code", "?")
                        logger.error("OpenAI API error: endpoint=%s status=%s", endpoint, status)
                        raise

                except asyncio.CancelledError:
                    raise
                except Exception:
                    raise

def _msg(role: str, text: str) -> dict:
    ctype = "output_text" if role == "assistant" else "input_text"
    return {"role": role, "content": [{"type": ctype, "text": text}]}
    
def _get_output_text(resp) -> str:
    
    def _ga(obj, name, default=None):
        if isinstance(obj, dict):
            return obj.get(name, default)
        return getattr(obj, name, default)

    for attr in ("parsed", "output_parsed"):
        pv = _ga(resp, attr, None)
        if isinstance(pv, str) and pv.strip():
            return pv.strip()
        if isinstance(pv, (dict, list)):
            try:
                return json.dumps(pv, ensure_ascii=False, separators=(",", ":"))
            except Exception:
                pass

    out = _ga(resp, "output", None)
    if isinstance(out, list):
        parts: list[str] = []
        for item in out:
            content = _ga(item, "content", None)
            if not isinstance(content, list):
                continue
            for part in content:
                typ = _ga(part, "type", None)
                if typ == "output_json":
                    jv = _ga(part, "json", None)
                    if jv is not None:
                        try:
                            return json.dumps(jv, ensure_ascii=False, separators=(",", ":"))
                        except Exception:
                            pass
                pv = _ga(part, "parsed", None)
                if isinstance(pv, str) and pv.strip():
                    parts.append(pv.strip())
                    continue
                if isinstance(pv, (dict, list)):
                    try:
                        parts.append(json.dumps(pv, ensure_ascii=False, separators=(",", ":")))
                        continue
                    except Exception:
                        pass
                txt = _ga(part, "text", None)
                if isinstance(txt, str) and txt:
                    parts.append(txt)
                    continue
                if isinstance(txt, dict):
                    val = _ga(txt, "value", None)
                    if isinstance(val, str) and val:
                        parts.append(val)
        if parts:
            return "".join(parts)

    choices = _ga(resp, "choices", None)
    if isinstance(choices, list) and choices:
        msg = _ga(choices[0], "message", None)
        if msg is not None:
            pv = _ga(msg, "parsed", None)
            if isinstance(pv, (dict, list)):
                try:
                    return json.dumps(pv, ensure_ascii=False, separators=(",", ":"))
                except Exception:
                    pass
            if isinstance(pv, str) and pv.strip():
                return pv.strip()
            content = _ga(msg, "content", None)
            if isinstance(content, str) and content.strip():
                return content.strip()
            if isinstance(content, list):
                parts = []
                for p in content:
                    txt = _ga(p, "text", None)
                    if isinstance(txt, str) and txt:
                        parts.append(txt)
                if parts:
                    return "".join(parts)

    t = _ga(resp, "output_text", None)
    if isinstance(t, str) and t:
        return t

    return ""
EOF
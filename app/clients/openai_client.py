#app/clients/openai_client.py
import asyncio
import logging
import json
import httpx

from typing import Any, Optional
from asyncio import Semaphore
from openai import AsyncOpenAI, APIStatusError
from tenacity import (
    AsyncRetrying,
    stop_after_attempt,
    stop_after_delay,
    wait_exponential,
    retry_if_exception,
)

from app.config import settings

RETRYABLE_EXC_TYPES = tuple()
try:
    from openai import RateLimitError, APIConnectionError, APITimeoutError
    RETRYABLE_EXC_TYPES = (RateLimitError, APIConnectionError, APITimeoutError)
except Exception:
    pass

logger = logging.getLogger(__name__)


OPENAI_MAX_ATTEMPTS = int(getattr(settings, "OPENAI_MAX_ATTEMPTS", 3))
OPENAI_TOTAL_TIMEOUT_SECONDS = float(getattr(settings, "OPENAI_TOTAL_TIMEOUT_SECONDS", 55.0))
OPENAI_REQ_CONNECT_TIMEOUT_SECONDS = float(getattr(settings, "OPENAI_REQ_CONNECT_TIMEOUT_SECONDS", 5.0))
OPENAI_REQ_READ_TIMEOUT_SECONDS = float(getattr(settings, "OPENAI_REQ_READ_TIMEOUT_SECONDS", 30.0))
OPENAI_REQ_WRITE_TIMEOUT_SECONDS = float(getattr(settings, "OPENAI_REQ_WRITE_TIMEOUT_SECONDS", 20.0))
OPENAI_REQ_POOL_TIMEOUT_SECONDS = float(getattr(settings, "OPENAI_REQ_POOL_TIMEOUT_SECONDS", 10.0))

_openai: AsyncOpenAI | None = None
try:
    _max_conc = int(getattr(settings, "OPENAI_MAX_CONCURRENT_REQUESTS", 100) or 100)
except Exception:
    _max_conc = 100
OPENAI_SEMAPHORE = Semaphore(max(1, _max_conc))


def _build_httpx_timeout() -> httpx.Timeout:
    return httpx.Timeout(
        connect=OPENAI_REQ_CONNECT_TIMEOUT_SECONDS,
        read=OPENAI_REQ_READ_TIMEOUT_SECONDS,
        write=OPENAI_REQ_WRITE_TIMEOUT_SECONDS,
        pool=OPENAI_REQ_POOL_TIMEOUT_SECONDS,
    )


def get_openai() -> AsyncOpenAI:
    global _openai
    if _openai is None:
        import openai as _lib
        logger.info("OpenAI SDK version: %s", getattr(_lib, "__version__", "?"))
        _openai = AsyncOpenAI(
            api_key=settings.OPENAI_API_KEY,
            timeout=_build_httpx_timeout(),
        )
    return _openai


def _should_retry(exc: Exception) -> bool:

    if isinstance(exc, asyncio.CancelledError):
        return False

    if isinstance(exc, (ValueError, TypeError)):
        return False

    if RETRYABLE_EXC_TYPES and isinstance(exc, RETRYABLE_EXC_TYPES):
        return True

    if isinstance(
        exc,
        (
            httpx.ReadTimeout,
            httpx.ConnectTimeout,
            httpx.WriteTimeout,
            httpx.PoolTimeout,
            httpx.TransportError,
        ),
    ):
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

    return False


def classify_openai_error(exc: Exception) -> str:
    if RETRYABLE_EXC_TYPES:
        try:
            from openai import RateLimitError
            if isinstance(exc, RateLimitError):
                return "rate_limit"
        except Exception:
            pass

    if isinstance(exc, APIStatusError):
        try:
            status = int(getattr(exc, "status_code", 500) or 500)
        except Exception:
            status = 500
        if status == 429:
            return "rate_limit"
        if status >= 500:
            return "upstream_5xx_or_transport"
        return "other"

    if isinstance(
        exc,
        (
            httpx.ReadTimeout,
            httpx.ConnectTimeout,
            httpx.WriteTimeout,
            httpx.PoolTimeout,
            httpx.TransportError,
        ),
    ):
        return "upstream_5xx_or_transport"

    status_maybe = getattr(exc, "status_code", None)
    if status_maybe is not None:
        try:
            status = int(status_maybe)
            if status == 429:
                return "rate_limit"
            if status >= 500:
                return "upstream_5xx_or_transport"
        except Exception:
            pass

    return "other"


def _normalize_response_input(input_value: Any) -> list[dict[str, Any]]:
    def _normalize_message_part(part: Any, *, role: str) -> dict[str, Any]:
        if isinstance(part, dict):
            ptype = str(part.get("type") or "").strip()
            if ptype:
                out = dict(part)
                if ptype == "text":
                    out["type"] = "output_text" if role == "assistant" else "input_text"
                return out
            if "text" in part:
                return {
                    "type": "output_text" if role == "assistant" else "input_text",
                    "text": str(part.get("text") or ""),
                }
            return dict(part)
        if isinstance(part, str):
            return {"type": "output_text" if role == "assistant" else "input_text", "text": part}
        return {"type": "output_text" if role == "assistant" else "input_text", "text": str(part)}

    def _normalize_message(msg: Any) -> dict[str, Any]:
        if isinstance(msg, dict):
            role = str(msg.get("role") or "user").strip().lower() or "user"
            content = msg.get("content")
            if isinstance(content, list):
                norm_content = [_normalize_message_part(c, role=role) for c in content]
            else:
                norm_content = [_normalize_message_part(content if content is not None else "", role=role)]
            return {"role": role, "content": norm_content}
        return {
            "role": "user",
            "content": [{"type": "input_text", "text": str(msg)}],
        }

    if isinstance(input_value, list):
        return [_normalize_message(m) for m in input_value]
    if isinstance(input_value, dict):
        return [_normalize_message(input_value)]
    if isinstance(input_value, str):
        return [{"role": "user", "content": [{"type": "input_text", "text": input_value}]}]
    return [{"role": "user", "content": [{"type": "input_text", "text": str(input_value or "")}] }]


def _prepare_responses_payload(
    *,
    prompt_profile: str,
    static_prefix: Optional[str] = None,
    dynamic_suffix: Optional[str] = None,
    instructions: Optional[str] = None,
    input: Any = None,
    messages: Any = None,
    **kwargs: Any,
) -> dict[str, Any]:
    payload: dict[str, Any] = dict(kwargs)

    effective_static_prefix = static_prefix if static_prefix is not None else instructions
    effective_dynamic_suffix = dynamic_suffix if dynamic_suffix is not None else payload.pop("prompt_dynamic_suffix", None)

    if effective_static_prefix is not None or effective_dynamic_suffix:
        payload["instructions"] = f"{effective_static_prefix or ''}{effective_dynamic_suffix or ''}"

    canonical_input = input if input is not None else messages
    if canonical_input is None:
        canonical_input = ""
    payload["input"] = _normalize_response_input(canonical_input)
    payload.pop("messages", None)

    return payload


async def _call_openai_with_retry(**kwargs: Any) -> Any:

    total_timeout_override: Optional[float] = None
    if "total_timeout" in kwargs:
        try:
            total_timeout_override = float(kwargs.pop("total_timeout"))
        except Exception:
            total_timeout_override = None

    client = get_openai()

    async with OPENAI_SEMAPHORE:
        base_kwargs = dict(kwargs)

        total_timeout = total_timeout_override if total_timeout_override is not None else OPENAI_TOTAL_TIMEOUT_SECONDS
        
        async for attempt in AsyncRetrying(
            stop=(stop_after_attempt(OPENAI_MAX_ATTEMPTS) | stop_after_delay(total_timeout)),
            wait=wait_exponential(min=0.5, max=2),
            retry=retry_if_exception(_should_retry),
            reraise=True,
        ):
            with attempt:
                params = dict(base_kwargs)
                endpoint = params.pop("endpoint", None) or "responses.create"

                if endpoint == "responses.create":
                    prompt_profile = str(params.pop("prompt_profile", "default") or "default")
                    static_prefix = params.pop("static_prefix", None)
                    dynamic_suffix = params.pop("dynamic_suffix", None)
                    instructions = params.pop("instructions", None)
                    input_value = params.pop("input", None)
                    messages = params.pop("messages", None)
                    params = _prepare_responses_payload(
                        prompt_profile=prompt_profile,
                        static_prefix=static_prefix,
                        dynamic_suffix=dynamic_suffix,
                        instructions=instructions,
                        input=input_value,
                        messages=messages,
                        **params,
                    )
                else:
                    prompt_profile = str(params.pop("prompt_profile", "") or "")

                def _normalize_params(ep: str, p: dict) -> dict:
                    model = str(p.get("model") or "")
                    model_role = str(p.pop("model_role", "") or "").strip().lower()

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

                        is_gpt5_family = model.startswith("gpt-5")
                        is_gpt_52 = model.startswith("gpt-5.2")

                        if is_gpt5_family:
                            for k in ("presence_penalty", "frequency_penalty"):
                                p.pop(k, None)

                            if not is_gpt_52:
                                for k in ("temperature", "top_p"):
                                    p.pop(k, None)

                            rnode = p.get("reasoning") or {}
                            if not isinstance(rnode, dict):
                                rnode = {}
                            if is_gpt_52:
                                default_effort = getattr(settings, "RESPONSE_REASONING_EFFORT", "none") or "none"
                            elif model_role == "reasoning":
                                default_effort = "medium"
                            elif model_role == "regular":
                                default_effort = "low"
                            elif model_role == "base":
                                default_effort = "minimal"
                            else:
                                default_effort = getattr(settings, "RESPONSE_REASONING_EFFORT", "low")
                            rnode.setdefault("effort", default_effort)
                            p["reasoning"] = rnode

                            tnode = p.get("text") or {}
                            if not isinstance(tnode, dict):
                                tnode = {}
                            if not _is_structured_json_schema(p):
                                verbosity_fallback = "low" if is_gpt_52 else "medium"
                                tnode.setdefault("verbosity", getattr(settings, "RESPONSE_VERBOSITY", verbosity_fallback))
                            p["text"] = tnode

                    if "timeout" not in p:
                        p["timeout"] = _build_httpx_timeout()

                    return p

                params = _normalize_params(endpoint, params)

                if endpoint not in ("responses.create", "embeddings.create", "chat.completions.create", "images.generate"):
                    raise ValueError(
                        f"Unsupported endpoint '{endpoint}'. "
                        "Use 'responses.create', 'chat.completions.create', 'embeddings.create' or 'images.generate'."
                    )

                safe_params = {
                    k: v
                    for k, v in params.items()
                    if k
                    not in (
                        "input",
                        "messages",
                        "prompt",
                        "response_format",
                        "extra_body",
                        "text",
                        "tools",
                        "tool_choice",
                        "instructions",
                        "timeout",
                    )
                }
                if prompt_profile:
                    safe_params["prompt_profile"] = prompt_profile
                logger.info(
                    "OpenAI request: endpoint=%s meta=%r attempt=%d",
                    endpoint,
                    safe_params,
                    attempt.retry_state.attempt_number,
                )

                target: Any = client
                for part in endpoint.split("."):
                    target = getattr(target, part)

                try:
                    resp = await target(**params)
                    logger.info(
                        "OpenAI response: endpoint=%s usage=%r attempt=%d",
                        endpoint,
                        getattr(resp, "usage", None),
                        attempt.retry_state.attempt_number,
                    )
                    return resp

                except asyncio.CancelledError:
                    raise

                except APIStatusError as http_err:
                    status = getattr(http_err, "status_code", "?")
                    logger.error(
                        "OpenAI API error: endpoint=%s status=%s attempt=%d",
                        endpoint,
                        status,
                        attempt.retry_state.attempt_number,
                    )
                    raise


async def transcribe_audio_with_retry(
    *,
    model: str,
    file: Any,
    response_format: str = "text",
    total_timeout: Optional[float] = None,
    **kwargs: Any,
) -> Any:
    client = get_openai()
    params = {
        "model": model,
        "file": file,
        "response_format": response_format,
        **kwargs,
    }
    if "timeout" not in params:
        params["timeout"] = _build_httpx_timeout()

    retry_timeout = total_timeout if total_timeout is not None else OPENAI_TOTAL_TIMEOUT_SECONDS
    attempt_no = 0
    async with OPENAI_SEMAPHORE:
        try:
            async for attempt in AsyncRetrying(
                stop=(stop_after_attempt(OPENAI_MAX_ATTEMPTS) | stop_after_delay(retry_timeout)),
                wait=wait_exponential(min=0.5, max=2),
                retry=retry_if_exception(_should_retry),
                reraise=True,
            ):
                with attempt:
                    attempt_no = attempt.retry_state.attempt_number
                    return await client.audio.transcriptions.create(**params)
        except Exception as exc:
            try:
                setattr(exc, "_openai_retry_attempts", attempt_no)
                setattr(exc, "_openai_total_timeout", retry_timeout)
            except Exception:
                pass
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

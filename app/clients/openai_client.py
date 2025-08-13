cat >app/clients/openai_client.py<< 'EOF'
#app/clients/openai_client.py
import asyncio
import logging

from typing import Any
from asyncio import Semaphore
from openai import AsyncOpenAI
from httpx import HTTPStatusError
from app.config import settings
from tenacity import AsyncRetrying, stop_after_attempt, wait_exponential, retry_if_exception


logger = logging.getLogger(__name__)

_openai: AsyncOpenAI | None = None

OPENAI_SEMAPHORE = Semaphore(settings.OPENAI_MAX_CONCURRENT_REQUESTS)

def get_openai() -> AsyncOpenAI:
    global _openai
    if _openai is None:
        _openai = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
    return _openai

async def _call_openai_with_retry(**kwargs: Any) -> Any:
    client = get_openai()

    if "max_tokens" in kwargs and "max_completion_tokens" not in kwargs:
        kwargs["max_completion_tokens"] = kwargs.pop("max_tokens")

    async with OPENAI_SEMAPHORE:
        base_kwargs = dict(kwargs)
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(3),
            wait=wait_exponential(min=1, max=10),
            retry=retry_if_exception(lambda exc: not isinstance(exc, asyncio.CancelledError)),
            reraise=True,
        ):
            with attempt:
                try:
                    params = dict(base_kwargs)

                    if params.get("temperature") == 0.0:
                        params.pop("temperature", None)

                    endpoint = params.pop("endpoint", "chat.completions.create")

                    if isinstance(params.get("kwargs"), dict):
                        extra = params.pop("kwargs")
                        params.update(extra)

                    target = client
                    for part in endpoint.split('.'):
                        target = getattr(target, part)

                    try:
                        return await target(**params)
                    except HTTPStatusError as http_err:
                        logger.error("OpenAI HTTP error: %s", http_err)
                        logger.error("Request params: %s", params)
                        try:
                            logger.error("Response body: %s", http_err.response.text)
                        except Exception:
                            logger.exception("Could not read response body from HTTP error")
                        raise

                except asyncio.CancelledError:
                    raise
                except Exception as err:
                    if (
                        endpoint.startswith("chat.")
                        and "max_completion_tokens" in params
                        and "unknown" in str(err).lower()
                        and "max_completion_tokens" in str(err)
                    ):
                        legacy = dict(params)
                        legacy["max_tokens"] = legacy.pop("max_completion_tokens")
                        return await client.chat.completions.create(**legacy)
                    raise

EOF
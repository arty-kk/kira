#app/clients/openai_client.py

from openai import AsyncOpenAI
from app.config import settings
from typing import Any
from asyncio import Semaphore
from app.config import settings
from tenacity import AsyncRetrying, stop_after_attempt, wait_exponential

_openai: AsyncOpenAI | None = None

OPENAI_SEMAPHORE = Semaphore(settings.OPENAI_MAX_CONCURRENT_REQUESTS)

def get_openai() -> AsyncOpenAI:
    global _openai
    if _openai is None:
        _openai = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
    return _openai


async def _call_openai_with_retry(**kwargs: Any) -> Any:
    client = get_openai()
    async with OPENAI_SEMAPHORE:
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(3),
            wait=wait_exponential(min=1, max=10),
            reraise=True
        ):
            with attempt:
                return await client.chat.completions.create(**kwargs)
# app/services/http_client.py

import aiohttp, logging

from aiohttp import ClientTimeout

logger = logging.getLogger(__name__)

class HTTPClient:

    def __init__(self, timeout_sec: int = 10):
        self._timeout_sec = timeout_sec
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def get_json(self, url: str, params: dict | None = None) -> dict:
        session = await self._get_session()
        timeout = ClientTimeout(total=self._timeout_sec)
        try:
            async with session.get(url, params=params, timeout=timeout) as response:
                response.raise_for_status()
                return await response.json()
        except Exception as e:
            logger.error("Error fetching JSON from %s: %s", url, e)
            raise

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

http_client = HTTPClient()
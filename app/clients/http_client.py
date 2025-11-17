#app/clients/http_client.py
import asyncio
import aiohttp
import logging

from aiohttp import ClientTimeout

from app.config import settings

logger = logging.getLogger(__name__)

class HTTPClient:

    def __init__(self, timeout_sec: int = 10):
        self._timeout_sec = timeout_sec
        self._session: aiohttp.ClientSession | None = None
        self._session_lock = asyncio.Lock()

    async def _get_session(self) -> aiohttp.ClientSession:
        async with self._session_lock:
            if self._session is None or self._session.closed:
                connector = aiohttp.TCPConnector(
                    limit=getattr(settings, "HTTP_MAX_CONNECTIONS", 200)
                )
                timeout = ClientTimeout(
                    total=self._timeout_sec,
                    sock_connect=self._timeout_sec / 2,
                    sock_read=self._timeout_sec / 2,
                )
                self._session = aiohttp.ClientSession(
                    connector=connector,
                    timeout=timeout
                )
        return self._session

    async def get_json(self, url: str, params: dict | None = None) -> dict:
        session = await self._get_session()
        try:
            async with session.get(url, params=params) as response:
                response.raise_for_status()
                return await response.json()
        except Exception as e:
            logger.error("Error fetching JSON from %s: %s", url, e)
            raise

    async def close(self) -> None:
        async with self._session_lock:
            if self._session and not self._session.closed:
                await self._session.close()
                self._session = None

http_client = HTTPClient(timeout_sec=getattr(settings, "HTTP_TIMEOUT_SEC", 10))
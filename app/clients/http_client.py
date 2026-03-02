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
        self._request_semaphore = asyncio.Semaphore(
            int(getattr(settings, "HTTP_MAX_CONCURRENCY", 50) or 50)
        )

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

    def _build_timeout(self, timeout_sec: float | None) -> ClientTimeout | None:
        if timeout_sec is None:
            return None
        try:
            t = float(timeout_sec)
        except Exception:
            return None
        if t <= 0:
            return None
        return ClientTimeout(total=t, sock_connect=max(0.1, t / 2), sock_read=max(0.1, t / 2))

    async def _request_bytes(
        self,
        method: str,
        url: str,
        *,
        params: dict | None = None,
        json: dict | None = None,
        headers: dict | None = None,
        timeout_sec: float | None = None,
        retries: int = 0,
        retry_backoff_sec: float = 0.5,
        semaphore: asyncio.Semaphore | None = None,
    ) -> bytes:
        session = await self._get_session()
        timeout = self._build_timeout(timeout_sec)
        attempts = max(1, int(retries) + 1)
        request_semaphore = semaphore or self._request_semaphore

        for attempt in range(1, attempts + 1):
            try:
                async with request_semaphore:
                    async with session.request(
                        method,
                        url,
                        params=params,
                        json=json,
                        headers=headers,
                        timeout=timeout,
                    ) as response:
                        response.raise_for_status()
                        return await response.read()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if attempt >= attempts:
                    logger.error("HTTP %s %s failed: %s", method, url, e)
                    raise
                delay = float(retry_backoff_sec) * (2 ** (attempt - 1))
                logger.warning(
                    "HTTP %s %s attempt %s/%s failed: %s; retrying in %.2fs",
                    method,
                    url,
                    attempt,
                    attempts,
                    e,
                    delay,
                )
                await asyncio.sleep(delay)

    async def get_json(
        self,
        url: str,
        *,
        params: dict | None = None,
        timeout_sec: float | None = None,
        retries: int = 0,
        retry_backoff_sec: float = 0.5,
        semaphore: asyncio.Semaphore | None = None,
        headers: dict | None = None,
    ) -> dict:
        raw = await self._request_bytes(
            "GET",
            url,
            params=params,
            timeout_sec=timeout_sec,
            retries=retries,
            retry_backoff_sec=retry_backoff_sec,
            semaphore=semaphore,
            headers=headers,
        )
        import json as _json
        return _json.loads(raw.decode("utf-8"))

    async def get_bytes(
        self,
        url: str,
        *,
        timeout_sec: float | None = None,
        retries: int = 0,
        headers: dict | None = None,
    ) -> bytes:
        return await self._request_bytes(
            "GET",
            url,
            timeout_sec=timeout_sec,
            retries=retries,
            headers=headers,
        )

    async def post_json(
        self,
        url: str,
        payload: dict,
        *,
        headers: dict | None = None,
        timeout_sec: float | None = None,
        retries: int = 0,
    ) -> dict:
        raw = await self._request_bytes(
            "POST",
            url,
            json=payload,
            headers=headers,
            timeout_sec=timeout_sec,
            retries=retries,
        )
        import json as _json
        return _json.loads(raw.decode("utf-8"))

    async def close(self) -> None:
        async with self._session_lock:
            if self._session and not self._session.closed:
                await self._session.close()
                self._session = None


http_client = HTTPClient(timeout_sec=getattr(settings, "HTTP_TIMEOUT_SEC", 10))

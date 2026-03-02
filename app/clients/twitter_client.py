#app/clients/twitter_client.py
import asyncio
import base64
import hashlib
import hmac
import logging
import secrets
import time
from urllib.parse import quote

from app.clients.http_client import http_client
from app.config import settings

logger = logging.getLogger(__name__)

_REQUIRED_ENV_VARS = (
    "TWITTER_API_KEY",
    "TWITTER_API_SECRET",
    "TWITTER_ACCESS_TOKEN",
    "TWITTER_ACCESS_TOKEN_SECRET",
    "TWITTER_BEARER_TOKEN",
)

_TWITTER_POST_URL = "https://api.twitter.com/2/tweets"
_TWITTER_TIMEOUT_SEC = float(getattr(settings, "TWITTER_HTTP_TIMEOUT_SEC", 15))
_TWITTER_RETRIES = int(getattr(settings, "TWITTER_HTTP_RETRIES", 2))
_twitter_post_semaphore = asyncio.Semaphore(
    max(1, int(getattr(settings, "TWITTER_MAX_CONCURRENCY", 4) or 4))
)


def is_twitter_configured() -> bool:
    return all(bool(getattr(settings, var, None)) for var in _REQUIRED_ENV_VARS)


def _oauth_percent_encode(value: str) -> str:
    return quote(str(value), safe="~-._")


def _build_oauth1_header(*, method: str, url: str) -> str:
    oauth_params = {
        "oauth_consumer_key": str(settings.TWITTER_API_KEY),
        "oauth_nonce": secrets.token_hex(16),
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp": str(int(time.time())),
        "oauth_token": str(settings.TWITTER_ACCESS_TOKEN),
        "oauth_version": "1.0",
    }

    param_str = "&".join(
        f"{_oauth_percent_encode(k)}={_oauth_percent_encode(v)}"
        for k, v in sorted(oauth_params.items())
    )
    base_string = "&".join([
        method.upper(),
        _oauth_percent_encode(url),
        _oauth_percent_encode(param_str),
    ])
    signing_key = (
        f"{_oauth_percent_encode(str(settings.TWITTER_API_SECRET))}"
        f"&{_oauth_percent_encode(str(settings.TWITTER_ACCESS_TOKEN_SECRET))}"
    )
    digest = hmac.new(signing_key.encode("utf-8"), base_string.encode("utf-8"), hashlib.sha1).digest()
    oauth_params["oauth_signature"] = base64.b64encode(digest).decode("ascii")

    auth = ", ".join(
        f'{_oauth_percent_encode(k)}="{_oauth_percent_encode(v)}"'
        for k, v in sorted(oauth_params.items())
    )
    return f"OAuth {auth}"


async def post_tweet(text: str) -> None:
    if not is_twitter_configured():
        missing_vars = [var for var in _REQUIRED_ENV_VARS if not getattr(settings, var, None)]
        missing_list = ", ".join(missing_vars)
        raise RuntimeError(
            f"Twitter client (V2) is not configured. Missing env vars: {missing_list}"
        )

    headers = {
        "Authorization": _build_oauth1_header(method="POST", url=_TWITTER_POST_URL),
        "Content-Type": "application/json",
    }
    payload = {"text": text}

    async with _twitter_post_semaphore:
        try:
            await http_client.post_json(
                _TWITTER_POST_URL,
                payload,
                headers=headers,
                timeout_sec=_TWITTER_TIMEOUT_SEC,
                retries=_TWITTER_RETRIES,
            )
            logger.info("Twitter: tweet posted successfully (via V2 API)")
        except Exception:
            logger.exception("Unexpected error posting tweet")
            raise

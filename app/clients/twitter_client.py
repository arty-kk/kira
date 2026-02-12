#app/clients/twitter_client.py
import asyncio
import logging
import threading

import tweepy
from tweepy import TweepyException

from app.config import settings

logger = logging.getLogger(__name__)

_REQUIRED_ENV_VARS = (
    "TWITTER_API_KEY",
    "TWITTER_API_SECRET",
    "TWITTER_ACCESS_TOKEN",
    "TWITTER_ACCESS_TOKEN_SECRET",
    "TWITTER_BEARER_TOKEN",
)

_twitter_client: tweepy.Client | None = None
_twitter_client_lock = threading.Lock()


def is_twitter_configured() -> bool:
    return all(bool(getattr(settings, var, None)) for var in _REQUIRED_ENV_VARS)


def _get_twitter_client() -> tweepy.Client:
    global _twitter_client

    if _twitter_client is not None:
        return _twitter_client

    with _twitter_client_lock:
        if _twitter_client is not None:
            return _twitter_client

        if not is_twitter_configured():
            missing_vars = [var for var in _REQUIRED_ENV_VARS if not getattr(settings, var, None)]
            missing_list = ", ".join(missing_vars)
            raise RuntimeError(
                f"Twitter client (V2) is not configured. Missing env vars: {missing_list}"
            )

        _twitter_client = tweepy.Client(
            bearer_token=settings.TWITTER_BEARER_TOKEN,
            consumer_key=settings.TWITTER_API_KEY,
            consumer_secret=settings.TWITTER_API_SECRET,
            access_token=settings.TWITTER_ACCESS_TOKEN,
            access_token_secret=settings.TWITTER_ACCESS_TOKEN_SECRET,
            wait_on_rate_limit=True,
        )
        return _twitter_client


async def post_tweet(text: str) -> None:

    loop = asyncio.get_running_loop()
    
    try:
        await loop.run_in_executor(None, lambda: _get_twitter_client().create_tweet(text=text))
        logger.info("Twitter: tweet posted successfully (via V2 API)")
    except TweepyException as e:
        logger.error("Twitter API error (V2): %s", e)
        raise
    except Exception:
        logger.exception("Unexpected error posting tweet")
        raise

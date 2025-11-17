#app/clients/twitter_client.py
import asyncio
import logging

import tweepy
from tweepy import TweepyException

from app.config import settings

logger = logging.getLogger(__name__)

required = (
    "TWITTER_API_KEY",
    "TWITTER_API_SECRET",
    "TWITTER_ACCESS_TOKEN",
    "TWITTER_ACCESS_TOKEN_SECRET",
    "TWITTER_BEARER_TOKEN",
)
for var in required:
    if not getattr(settings, var, None):
        raise RuntimeError(f"Environment variable {var} is required for Twitter client (V2)")

_twitter_client = tweepy.Client(
    bearer_token=settings.TWITTER_BEARER_TOKEN,
    consumer_key=settings.TWITTER_API_KEY,
    consumer_secret=settings.TWITTER_API_SECRET,
    access_token=settings.TWITTER_ACCESS_TOKEN,
    access_token_secret=settings.TWITTER_ACCESS_TOKEN_SECRET,
    wait_on_rate_limit=True,
)


async def post_tweet(text: str) -> None:

    loop = asyncio.get_running_loop()
    
    try:
        await loop.run_in_executor(None, lambda: _twitter_client.create_tweet(text=text))
        logger.info("Twitter: tweet posted successfully (via V2 API)")
    except TweepyException as e:
        logger.error("Twitter API error (V2): %s", e)
        raise
    except Exception:
        logger.exception("Unexpected error posting tweet")
        raise
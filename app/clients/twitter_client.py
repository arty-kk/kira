# app/clients/twitter_client.py

import asyncio
import logging

import tweepy
from tweepy import TweepyException

from app.config import settings

logger = logging.getLogger(__name__)

for var in (
    "TWITTER_API_KEY",
    "TWITTER_API_SECRET",
    "TWITTER_ACCESS_TOKEN",
    "TWITTER_ACCESS_TOKEN_SECRET",
):
    if not getattr(settings, var, None):
        raise RuntimeError(f"Environment variable {var} is required for Twitter client")

_auth = tweepy.OAuth1UserHandler(
    settings.TWITTER_API_KEY,
    settings.TWITTER_API_SECRET,
    settings.TWITTER_ACCESS_TOKEN,
    settings.TWITTER_ACCESS_TOKEN_SECRET,
)
_twitter = tweepy.API(_auth, wait_on_rate_limit=True)


async def post_tweet(text: str) -> None:

    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(None, _twitter.update_status, text)
        logger.info("Twitter: tweet posted successfully")
    except TweepyException as e:
        logger.error("Twitter API error: %s", e)
        raise
    except Exception:
        logger.exception("Unexpected error posting tweet")
        raise

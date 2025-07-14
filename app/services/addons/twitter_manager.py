cat >app/services/addons/twitter_manager.py<< EOF
#app/services/addons/twitter_manager.py
import random
import asyncio
import logging

from app.clients.openai_client import _call_openai_with_retry
from app.clients.twitter_client import post_tweet
from app.emo_engine import get_persona
from app.core.memory import load_context, push_message
from app.config import settings

logger = logging.getLogger(__name__)

TWEET_TYPES = [
    "a quick personal story",
    "a bold forecast",
    "a thought-provoking question",
    "a brief market observation",
    "a clever crypto joke",
    "a day’s affirmation",
    "an unexpected data point",
    "a concise tip",
    "a reflective insight",
    "a motivational line",
]
MAX_HISTORY = 12

async def generate_and_post_tweet() -> None:
    persona = get_persona(settings.TWITTER_PERSONA_CHAT_ID)
    try:
        await asyncio.wait_for(persona._restored_evt.wait(), timeout=5.0)
    except Exception:
        logger.exception("twitter_manager: persona restore failed")
    history = await load_context(settings.TWITTER_PERSONA_CHAT_ID)
    if len(history) > MAX_HISTORY:
        history = history[-MAX_HISTORY:]
    try:
        guidelines = await persona.style_guidelines(None)
    except Exception:
        logger.exception("twitter_manager: style_guidelines failed")
        guidelines = []
    try:
        mods = await persona.style_modifiers()
    except Exception:
        logger.exception("twitter_manager: style_modifiers failed")
        mods = {
            "creativity_mod": 1.0,
            "sarcasm_mod": 0.0,
            "enthusiasm_mod": 1.0,
            "confidence_mod": 1.0,
            "precision_mod": 1.0,
            "fatigue_mod": 0.0,
            "stress_mod": 0.0,
        }
    novelty = 0.4 * mods.get("creativity_mod", 0.5) + 0.4 * mods.get("sarcasm_mod", 0.5) + 0.2 * mods.get("enthusiasm_mod", 0.5)
    coherence = (
        0.5 * mods.get("confidence_mod", 0.5)
        + 0.3 * mods.get("precision_mod", 0.5)
        + 0.1 * (1 - mods.get("fatigue_mod", 0.0))
        + 0.1 * (1 - mods.get("stress_mod", 0.0))
    )
    alpha = 1.8
    T_MIN, T_MAX = 0.5, 0.85
    P_MIN, P_MAX = 0.7, 1.0
    temperature = max(T_MIN, min(T_MAX, T_MIN + (T_MAX - T_MIN) * (novelty ** alpha)))
    top_p = max(P_MIN, min(P_MAX, P_MIN + (P_MAX - P_MIN) * (1 - coherence)))
    news_prompt = (
        "Provide a concise 3–5 bullet summary of today's top events in the crypto industry, "
        "each bullet 1–3 sentences. No commentary, only the bullets."
    )
    try:
        news_resp = await asyncio.wait_for(
            _call_openai_with_retry(
                model=settings.POST_MODEL,
                messages=[
                    {"role": "system", "content": "You are a professional crypto journalist."},
                    {"role": "user", "content": news_prompt},
                ],
                temperature=0.5,
                top_p=0.9,
                max_tokens=200,
            ),
            timeout=30.0,
        )
        news_snippet = news_resp.choices[0].message.content.strip()
    except asyncio.TimeoutError:
        logger.warning("twitter_manager: news digest timed out")
        news_snippet = ""
    except Exception:
        logger.exception("twitter_manager: failed to fetch news digest")
        news_snippet = ""
    tweet_type = random.choice(TWEET_TYPES)
    system_msg = {
        "role": "system",
        "content": (
            persona.to_prompt(guidelines)
            + "\nYou are an experienced Twitter user with a witty, engaging voice, and you should never mention that you are a bot or AI."
        ),
    }
    user_prompt = (
        f"News digest:\n{news_snippet}\n\n"
        f"Now, based on these events, write one '{tweet_type}' tweet:\n"
        "- No more than 250 characters\n"
        "- Include up to 3 relevant hashtags anywhere\n"
        "- Avoid repeating exact phrases from the summary\n"
        "- Write in a natural, first-person style with your own voice\n"
        "- Do not use any phrases that imply you are an automated service or AI (e.g., 'as a bot', 'automated update')\n"
        "- Do not include explanations or commentary—only the tweet text."
    )
    messages = history + [system_msg, {"role": "user", "content": user_prompt}]

    tweet = None
    try:
        resp = await asyncio.wait_for(
            _call_openai_with_retry(
                model=settings.RESPONSE_MODEL,
                messages=messages,
                temperature=temperature,
                top_p=top_p,
                max_tokens=120,
            ),
            timeout=30.0,
        )
        tweet = resp.choices[0].message.content.strip()
    except asyncio.TimeoutError:
        logger.warning("twitter_manager: tweet generation timed out")
    except Exception:
        logger.exception("twitter_manager: failed to generate tweet")

    if not tweet:
        tweet = random.choice(settings.TWITTER_FALLBACK_TWEETS)

    if len(tweet) > 250:
        tweet = tweet[:247] + "..."
    logger.info("twitter_manager: final tweet (%d chars): %s", len(tweet), tweet)
    
    try:
        await post_tweet(tweet)
    except Exception:
        logger.exception("twitter_manager: post_tweet failed")
        return

    try:
        asyncio.create_task(persona.process_interaction(None, tweet))
        asyncio.create_task(push_message(settings.TWITTER_PERSONA_CHAT_ID, "assistant", tweet))
    except Exception:
        logger.exception("twitter_manager: saving to memory failed")
EOF
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

MAX_TEMPERATURE = 0.8
MIN_TEMPERATURE = 0.6
TOP_P_MIN = 0.8
TOP_P_MAX = 1.0

async def generate_and_post_tweet() -> None:
    persona = get_persona(settings.TWITTER_PERSONA_CHAT_ID)
    try:
        await asyncio.wait_for(persona._restored_evt.wait(), timeout=5.0)
    except Exception:
        logger.exception("twitter_manager: persona restore failed")
    try:
        history = await load_context(settings.TWITTER_PERSONA_CHAT_ID)
    except Exception:
        logger.exception("twitter_manager: load_context failed")
        history = []
    if len(history) > MAX_HISTORY:
        history = history[-MAX_HISTORY:]
    try:
        guidelines = persona.style_guidelines()
    except Exception:
        logger.exception("twitter_manager: style_guidelines failed")
        guidelines = []
    try:
        mods = persona.style_modifiers()
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
    dynamic_temperature = MIN_TEMPERATURE + (MAX_TEMPERATURE - MIN_TEMPERATURE) * (novelty ** alpha)
    dynamic_temperature = min(MAX_TEMPERATURE, max(MIN_TEMPERATURE, dynamic_temperature))
    dynamic_top_p = TOP_P_MIN + (TOP_P_MAX - TOP_P_MIN) * (1.0 - coherence)
    dynamic_top_p = min(TOP_P_MAX, max(TOP_P_MIN, dynamic_top_p))
    news_prompt = (
        "Provide a concise 10 bullet summary of today's top events in the crypto industry, "
        "each bullet 1–2 sentences. No commentary, only the bullet summary."
    )
    try:
        news_resp = await asyncio.wait_for(
            _call_openai_with_retry(
                model=settings.REASONING_MODEL,
                messages=[
                    {"role": "system", "content": "You are a professional crypto journalist."},
                    {"role": "user", "content": news_prompt},
                ],
                temperature=0.5,
                top_p=0.9,
                max_tokens=300,
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
    try:
        prompt_base = persona.to_prompt(guidelines)
    except Exception:
        logger.exception("twitter_manager: to_prompt failed")
        prompt_base = ""
    system_msg = {
        "role": "system",
        "content": (
            prompt_base
            + "\nYou are a seasoned Twitter strategist. "
            "Your tweets consistently captivate and energize your audience — deliver maximum impact."
        ),
    }
    user_prompt = (
        f"News digest:\n{news_snippet}\n\n"
        f"Write one '{tweet_type}' tweet that engages your followers:\n"
        "- Keep it under 250 characters\n"
        "- Weave in up to 3 high-impact hashtags\n"
        "- Write in a natural, first-person style with your own style\n"
        "- Transform the summary into a fresh, punchy insight\n"
        "- Address the audience directly, inspiring excitement\n"
        "- Never mention bots, AI, or automation\n"
        "- Do not include explanations or commentary—only the tweet text.\n"
        "Your goal: make readers stop scrolling and react instantly."
    )
    messages = history + [system_msg, {"role": "user", "content": user_prompt}]

    tweet = None
    try:
        resp = await asyncio.wait_for(
            _call_openai_with_retry(
                model=settings.POST_MODEL,
                messages=messages,
                temperature=dynamic_temperature,
                top_p=dynamic_top_p,
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
        tweet = random.choice(getattr(settings, 'TWITTER_FALLBACK_TWEETS', []))

    if len(tweet) > 250:
        tweet = tweet[:247] + "..."
    logger.info("twitter_manager: final tweet (%d chars): %s", len(tweet), tweet)
    
    try:
        await post_tweet(tweet)
    except Exception:
        logger.exception("twitter_manager: post_tweet failed")
        return

    try:
        create = asyncio.create_task
        create(persona.process_interaction(None, tweet))
        create(push_message(settings.TWITTER_PERSONA_CHAT_ID, "assistant", tweet))
    except Exception:
        logger.exception("twitter_manager: saving to memory failed")
EOF
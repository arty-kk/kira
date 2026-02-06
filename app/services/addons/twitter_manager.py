#app/services/addons/twitter_manager.py
import random
import asyncio
import logging

from app.clients.openai_client import _call_openai_with_retry, _get_output_text
from app.clients.twitter_client import post_tweet
from app.services.responder.prompt_builder import build_system_prompt
from app.emo_engine import get_persona
from app.core.memory import load_context, push_message
from app.config import settings

logger = logging.getLogger(__name__)

TWEET_TYPES = [
    #"a quick personal story",
    "a bold forecast",
    #"a thought-provoking question",
    "a brief market observation",
    "a crypto joke",
    "a crypto meme",
    "a day’s affirmation",
    #"an unexpected data point",
    "a concise tip",
    "a reflective insight",
    "a motivational line",
]
MAX_HISTORY = 12

MAX_TEMPERATURE = 0.8
MIN_TEMPERATURE = 0.6
TOP_P_MIN = 0.8
TOP_P_MAX = 1.0

DEFAULT_MODS = {
    "creativity_mod": 0.5,
    "sarcasm_mod": 0.0,
    "enthusiasm_mod": 0.5,
    "confidence_mod": 0.5,
    "precision_mod": 0.5,
    "fatigue_mod": 0.0,
    "stress_mod": 0.0,
    "valence_mod": 0.0,
}

def _merge_and_clamp_mods(style_mods: dict | None) -> dict:
    mods = DEFAULT_MODS.copy()
    if not isinstance(style_mods, dict):
        return mods
    for k in mods.keys():
        try:
            if k == "valence_mod":
                x = float(style_mods.get("valence_mod", style_mods.get("valence", mods[k])))
                mods[k] = max(-1.0, min(1.0, x))
            else:
                x = float(style_mods.get(k, mods[k]))
                mods[k] = max(0.0, min(1.0, x))
        except Exception:
            pass
    return mods

def _to_responses_input(messages: list[dict]) -> list[dict]:

    out: list[dict] = []
    for m in messages or []:
        role = m.get("role", "user")
        content = m.get("content", "")
        if isinstance(content, str):
            out.append({"role": role, "content": [
                {"type": "output_text" if role == "assistant" else "input_text", "text": content}
            ]})
        elif isinstance(content, list):
            norm_parts: list[dict] = []
            for p in content:
                if isinstance(p, dict):
                    t = p.get("type")
                    if t == "text" or (t is None and "text" in p):
                        p = {
                            "type": ("output_text" if role == "assistant" else "input_text"),
                            "text": p.get("text")
                        }
                norm_parts.append(p)
            out.append({"role": role, "content": norm_parts})
        else:
            out.append({"role": role, "content": [{"type": "input_text", "text": str(content)}]})
    return out


async def generate_and_post_tweet() -> None:

    persona = await get_persona(settings.TWITTER_PERSONA_CHAT_ID)

    try:
        await asyncio.wait_for(persona._restored_evt.wait(), timeout=5.0)
    except Exception:
        logger.exception("twitter_manager: persona restore failed")
        
    try:
        history = await load_context(
            settings.TWITTER_PERSONA_CHAT_ID,
            settings.TWITTER_PERSONA_CHAT_ID,
        )
    except Exception:
        logger.exception("twitter_manager: load_context failed")
        history = []
    if len(history) > MAX_HISTORY:
        history = history[-MAX_HISTORY:]

    try:
        style_mods = persona._mods_cache or await asyncio.wait_for(persona.style_modifiers(), 30)
    except Exception:
        logger.exception("style_modifiers acquisition failed")
        style_mods = {}
    mods = _merge_and_clamp_mods(style_mods)
    guidelines = await persona.style_guidelines(settings.TWITTER_PERSONA_CHAT_ID)
    
    novelty = (
        0.4 * mods["creativity_mod"]
      + 0.4 * mods["sarcasm_mod"]
      + 0.2 * mods["enthusiasm_mod"]
    )
    coherence = (
        0.5 * mods["confidence_mod"]
      + 0.3 * mods["precision_mod"]
      + 0.1 * (1 - mods["fatigue_mod"])
      + 0.1 * (1 - mods["stress_mod"])
    )
    alpha = 1.8
    dynamic_temperature = MIN_TEMPERATURE + (MAX_TEMPERATURE - MIN_TEMPERATURE) * (novelty ** alpha)
    dynamic_top_p = TOP_P_MIN + (TOP_P_MAX - TOP_P_MIN) * (1.0 - coherence)
    try:
        dynamic_temperature *= (1.0 + 0.10 * float(mods["valence_mod"]))
    except Exception:
        pass
    if dynamic_temperature < 0.55:
        dynamic_temperature = 0.55
    if dynamic_temperature > 0.70:
        dynamic_temperature = 0.70
    if dynamic_top_p < 0.85:
        dynamic_top_p = 0.85
    if dynamic_top_p > 0.98:
        dynamic_top_p = 0.98

    try:
        logger.info(
            "TWITTER mods and sampling: novelty=%.3f coherence=%.3f temp=%.2f top_p=%.2f "
            "mods[c/sa/e/conf/prec/fat/str/val]=[%.2f,%.2f,%.2f,%.2f,%.2f,%.2f,%.2f,%.2f]",
            novelty, coherence, dynamic_temperature, dynamic_top_p,
            mods["creativity_mod"], mods["sarcasm_mod"], mods["enthusiasm_mod"],
            mods["confidence_mod"], mods["precision_mod"], mods["fatigue_mod"],
            mods["stress_mod"], mods["valence_mod"]
        )
    except Exception:
        pass

    news_prompt = (
        "Provide a concise 10-bullet summary of today's top crypto price movements and related industry events, "
        "each bullet up to 3 sentences. "
        "No commentary, only the bullet summary."
    )
    try:
        news_resp = await asyncio.wait_for(
            _call_openai_with_retry(
                endpoint="responses.create",
                model=settings.POST_MODEL,
                input=_to_responses_input([
                    {"role": "system", "content": "You are a professional crypto journalist."},
                    {"role": "user", "content": news_prompt},
                ]),
                tools=[{"type": "web_search"}],
                tool_choice={"type": "auto"},
                max_output_tokens=500,
                temperature=0.6,
            ),
            timeout=settings.POST_MODEL_TIMEOUT,
        )
        news_snippet = (_get_output_text(news_resp) or "").strip()
    except asyncio.TimeoutError:
        logger.warning("twitter_manager: news digest timed out")
        news_snippet = ""
    except Exception:
        logger.exception("twitter_manager: failed to fetch news digest")
        news_snippet = ""
    tweet_type = random.choice(TWEET_TYPES)

    try:
        system_base = await build_system_prompt(persona, guidelines, user_gender=None)
    except Exception:
        logger.exception("twitter_manager: build_system_prompt failed")
        system_base = "You are a helpful assistant."
        
    system_msg = {
        "role": "system",
        "content": (
            system_base
            + "\nYou are a seasoned Twitter strategist. "
            "Your tweets consistently captivate and energize your audience — deliver maximum impact."
        ),
    }
    user_prompt = (
        f"News digest:\n{news_snippet}\n\n"
        f"Write one '{tweet_type}' tweet that engages twitter users:\n"
        "- Transform the summary into a fresh, punchy insight\n"
        "- Weave in some high-impact and trending hashtags + #crypto\n"
        "- Write in a natural, first-person style\n"
        "- Do not include explanations or commentary—only the tweet text.\n"
        "- Keep it under 250 characters\n"
        "Your goal: make readers stop scrolling and react instantly to your tweet."
    )
    messages = [system_msg] + history + [{"role": "user", "content": user_prompt}]

    tweet = None
    try:
        resp = await asyncio.wait_for(
            _call_openai_with_retry(
                endpoint="responses.create",
                model=settings.POST_MODEL,
                input=_to_responses_input(messages),
                temperature=dynamic_temperature,
                top_p=dynamic_top_p,
                max_output_tokens=120,
            ),
            timeout=settings.POST_MODEL_TIMEOUT,
        )
        tweet = (_get_output_text(resp) or "").strip()
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
        await asyncio.gather(
            persona.process_interaction(settings.TWITTER_PERSONA_CHAT_ID, tweet),
            push_message(
                settings.TWITTER_PERSONA_CHAT_ID,
                "assistant",
                tweet,
                user_id=settings.TWITTER_PERSONA_CHAT_ID,
            ),
        )
    except Exception:
        logger.exception("twitter_manager: saving to memory failed")

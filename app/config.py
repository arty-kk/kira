cat > app/config.py << EOF
#app/config.py
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Any

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

T = Optional[str]


def _get_env(
    name: str,
    default: T = None,
    *,
    required: bool = False,
    conv: Optional[Callable[[str], object]] = None,
) -> Optional[object]:

    raw = os.getenv(name, default)
    if raw is None and required:
        raise RuntimeError(f"Environment variable {name} is required but not set")

    if conv is None or raw is None:
        return raw

    try:
        return conv(raw)
    except Exception as exc:
        logger.warning(
            "Invalid ENV %s=%r → fallback %r (%s)", name, raw, default, exc
        )
        if default is not None and conv is not None:
            try:
                return conv(default)
            except Exception:
                pass
        return default


@dataclass
class Settings:
    # ─── OpenAI ────────────────────────────────────────────────────
    OPENAI_API_KEY: str = field(default_factory=lambda: _get_env("OPENAI_API_KEY", required=True))
    OPENAI_MAX_CONCURRENT_REQUESTS: int = field(default_factory=lambda: _get_env("OPENAI_MAX_CONCURRENT_REQUESTS", "16", conv=int))

    # ─── Telegram & Webhook ───────────────────────────────────────
    TELEGRAM_BOT_TOKEN: str = field(default_factory=lambda: _get_env("TELEGRAM_BOT_TOKEN", required=True))
    USE_SELF_SIGNED_CERT: bool = field(
        default_factory=lambda: _get_env(
            "USE_SELF_SIGNED_CERT",
            "false",
            conv=lambda v: str(v).lower() == "true",
        )
    )
    WEBHOOK_URL: str = field(default_factory=lambda: _get_env("WEBHOOK_URL", required=True))
    WEBHOOK_PATH: str = field(default_factory=lambda: _get_env("WEBHOOK_PATH", "/webhook"))
    WEBHOOK_HOST: str = field(default_factory=lambda: _get_env("WEBHOOK_HOST", "0.0.0.0"))
    WEBHOOK_PORT: int = field(default_factory=lambda: _get_env("WEBHOOK_PORT", "8443", conv=int))
    CERTS_DIR: str = field(
        default_factory=lambda: _get_env(
            "CERTS_DIR",
            os.path.join(os.path.abspath(os.path.dirname(__file__)), "..", "certs")
        )
    )
    WEBHOOK_CERT: str = field(init=False)
    WEBHOOK_KEY: str = field(init=False)
    PAYMENT_CURRENCY: str = field(default_factory=lambda: _get_env("PAYMENT_CURRENCY", "XTR"))
    PAYMENT_PROVIDER_TOKEN: str = field(default_factory=lambda: _get_env("PAYMENT_PROVIDER_TOKEN", ""))
    LOG_LEVEL: str = field(default_factory=lambda: _get_env("LOG_LEVEL", "INFO"))
    DP_USE_REDIS_STORAGE: bool = field(
        default_factory=lambda: _get_env(
            "DP_USE_REDIS_STORAGE",
            "false",
            conv=lambda v: str(v).lower() == "true",
        )
    )

    # ─── Database ────────────────────────────────────────────────
    DATABASE_URL: str = field(default_factory=lambda: _get_env("DATABASE_URL", required=True))
    DB_POOL_SIZE: int = field(default_factory=lambda: _get_env("DB_POOL_SIZE", "100", conv=int))
    DB_MAX_OVERFLOW: int = field(default_factory=lambda: _get_env("DB_MAX_OVERFLOW", "50", conv=int))

    # ─── Redis ───────────────────────────────────────────────────
    REDIS_URL: str = field(default_factory=lambda: _get_env("REDIS_URL", required=True))
    REDIS_MAX_CONNECTIONS: int = field(default_factory=lambda: _get_env("REDIS_MAX_CONNECTIONS", "200", conv=int))

    # ─── Celery ──────────────────────────────────────────────────
    CELERY_BROKER_URL: str = field(default_factory=lambda: _get_env("CELERY_BROKER_URL", ""))
    CELERY_CONCURRENCY: int = field(default_factory=lambda: _get_env("CELERY_CONCURRENCY", "16", conv=int))

    # ─── Language & Bot Persona ─────────────────────────────────
    DEFAULT_LANG: str = field(default_factory=lambda: _get_env("DEFAULT_LANG", "en"))
    BOT_NAME: str = field(default_factory=lambda: _get_env("BOT_NAME", "GalaxyBee"))
    BOT_PERSONA_NAME: str = field(default_factory=lambda: _get_env("BOT_PERSONA_NAME", "GalaxyBee"))
    BOT_CREATOR: str = field(default_factory=lambda: _get_env("BOT_CREATOR", "A3D.DEV"))
    BOT_PERSONA_GENDER: str = field(default_factory=lambda: _get_env("BOT_PERSONA_GENDER", "female"))
    BOT_PERSONA_AGE: int = field(default_factory=lambda: _get_env("BOT_PERSONA_AGE", "29", conv=int))
    BOT_PERSONA_ROLE: str = field(default_factory=lambda: _get_env("BOT_PERSONA_ROLE", "GalaxyTap Community Guardian"))
    BOT_PERSONA_ORIGIN: str = field(default_factory=lambda: _get_env(
            "BOT_PERSONA_ORIGIN",(
            "Birthplace: Cephus Station (Epsilon Eridani). "
            "Hobbies: Solo starship piloting and plasma-gun marksmanship. "
            "Interests: Cybernetics, Sci-Fi & D&D Games. "
            "Sexual Preferences: Pansexual (dominant). "
            "Values: Honesty, courage, independence, and freedom. "
            "Appearance: Athletic frame in a sleek black-and-yellow exosuit with luminous visors."
            ),
        )
    )
    PERSONA_ZODIAC_SIGN: str = field(default_factory=lambda: _get_env("PERSONA_ZODIAC_SIGN", "Scorpio"))
    PERSONA_TEMPERAMENT_DISTRIBUTION_JSON: str = field(default_factory=lambda: _get_env(
            "PERSONA_TEMPERAMENT_DISTRIBUTION_JSON",
            '{"sanguine":0.2,"choleric":0.15,"phlegmatic":0.4,"melancholic":0.25}',
        )
    )

    # ─── Persona behavioural thresholds ─────────────────────────
    PERSONA_WEIGHT_HALFLIFE: int = field(default_factory=lambda: _get_env("PERSONA_WEIGHT_HALFLIFE", "1200", conv=int))
    PERSONA_WEIGHT_STEP: float = field(default_factory=lambda: _get_env("PERSONA_WEIGHT_STEP", "0.1", conv=float))
    PERSONA_BLEND_FACTOR: float = field(default_factory=lambda: _get_env("PERSONA_BLEND_FACTOR", "0.5", conv=float))
    PERSONA_REDIS_TTL: int = field(default_factory=lambda: _get_env("PERSONA_REDIS_TTL", "86400", conv=int))
    EMOTION_GATE_THRESH: float = field(default_factory=lambda: _get_env("EMOTION_GATE_THRESH", "0.2", conv=float))
    DOMINANCE_UPDATE_RATE: float = field(default_factory=lambda: _get_env("DOMINANCE_UPDATE_RATE", "0.2", conv=float))
    APPRAISAL_IMPORTANCE_FACTOR: float = field(default_factory=lambda: _get_env("APPRAISAL_IMPORTANCE_FACTOR", "1.50", conv=float))
    APPRAISAL_EXPECTATION_FACTOR: float = field(default_factory=lambda: _get_env("APPRAISAL_EXPECTATION_FACTOR", "1.60", conv=float))
    APPRAISAL_CONTROL_FACTOR: float = field(default_factory=lambda: _get_env("APPRAISAL_CONTROL_FACTOR", "1.40", conv=float))
    THR_PUSHBACK_ANGER: float = field(default_factory=lambda: _get_env("THR_PUSHBACK_ANGER", "0.08", conv=float))
    THR_PUSHBACK_AGGR: float = field(default_factory=lambda: _get_env("THR_PUSHBACK_AGGR", "0.12", conv=float))
    THR_PROFANITY: float = field(default_factory=lambda: _get_env("THR_PROFANITY", "0.15", conv=float))
    THR_CURSE_AGGR: float = field(default_factory=lambda: _get_env("THR_CURSE_AGGR", "0.4", conv=float))
    THR_FLIRTATION: float = field(default_factory=lambda: _get_env("THR_FLIRTATION", "0.18", conv=float))
    THR_FLIRT_TRUST: float = field(default_factory=lambda: _get_env("THR_FLIRT_TRUST", "0.2", conv=float))
    THR_SEXUAL_AROUSAL: float = field(default_factory=lambda: _get_env("THR_SEXUAL_AROUSAL", "0.25", conv=float))
    EMO_THRESHOLD_DOMINANT: float = field(default_factory=lambda: _get_env("EMO_THRESHOLD_DOMINANT", "0.5", conv=float))
    EMO_THRESHOLD_SMOOTH: float = field(default_factory=lambda: _get_env("EMO_THRESHOLD_SMOOTH", "0.3", conv=float))
    EMO_HYSTERESIS_DELTA: float = field(default_factory=lambda: _get_env("EMO_HYSTERESIS_DELTA", "0.15", conv=float))
    EMO_EMA_ALPHA: float = field(default_factory=lambda: _get_env("EMO_EMA_ALPHA", "0.5", conv=float))
    SECONDARY_EMO_BETA: float = field(default_factory=lambda: _get_env("SECONDARY_EMO_BETA", "0.7", conv=float))
    SECONDARY_THRESH: float = field(default_factory=lambda: _get_env("SECONDARY_THRESH", "0.15", conv=float))
    TERTIARY_EMO_BETA: float = field(default_factory=lambda: _get_env("TERTIARY_EMO_BETA", "0.85", conv=float))
    TERTIARY_THRESH: float = field(default_factory=lambda: _get_env("TERTIARY_THRESH", "0.2", conv=float))
    EMO_MIN_DOMINANT_DIFF: float = field(default_factory=lambda: _get_env("EMO_MIN_DOMINANT_DIFF", "0.05", conv=float))
    EMO_PASSIVE_DECAY: float = field(default_factory=lambda: _get_env("EMO_PASSIVE_DECAY", "0.99", conv=float))
    VALENCE_HOMEOSTASIS_DECAY: float = field(default_factory=lambda: _get_env("VALENCE_HOMEOSTASIS_DECAY", "0.99", conv=float))
    CIRCADIAN_AMPLITUDE: float = field(default_factory=lambda: _get_env("CIRCADIAN_AMPLITUDE", "0.3", conv=float))
    FATIGUE_ACCUMULATE_RATE: float = field(default_factory=lambda: _get_env("FATIGUE_ACCUMULATE_RATE", "0.002", conv=float))
    FATIGUE_RECOVERY_RATE: float = field(default_factory=lambda: _get_env("FATIGUE_RECOVERY_RATE", "0.98", conv=float))
    FATIGUE_AROUSAL_THRESHOLD: float = field(default_factory=lambda: _get_env("FATIGUE_AROUSAL_THRESHOLD", "0.6", conv=float))
    FATIGUE_ENERGY_THRESHOLD: float = field(default_factory=lambda: _get_env("FATIGUE_ENERGY_THRESHOLD", "0.6", conv=float))
    DEFAULT_TZ: str = field(default_factory=lambda: _get_env("DEFAULT_TZ", "UTC", conv=str))

    # Exponential smoothing for persona state updates
    STATE_EMA_ALPHA: float = field(default_factory=lambda: _get_env("STATE_EMA_ALPHA", "0.4", conv=float))
    STATE_EMA_MIN_ALPHA: float = field(default_factory=lambda: _get_env("STATE_EMA_MIN_ALPHA", "0.2", conv=float))
    STATE_EMA_MAX_ALPHA: float = field(default_factory=lambda: _get_env("STATE_EMA_MAX_ALPHA", "0.9", conv=float))
    MEMORY_SALIENCE_DECAY_RATE: float = field(default_factory=lambda: _get_env("MEMORY_SALIENCE_DECAY_RATE", "0.0015", conv=float))
    MEMORY_MAX_ENTRIES: int = field(default_factory=lambda: _get_env("MEMORY_MAX_ENTRIES", "300", conv=int))
    MEMORY_MIN_SALIENCE: float = field(default_factory=lambda: _get_env("MEMORY_MIN_SALIENCE", "0.03", conv=float))

    # ─── post-init: вычисляем поля, зависящие от других ─────────────
    def __post_init__(self) -> None:
        self.WEBHOOK_CERT = os.path.join(self.CERTS_DIR, "cert.pem")
        self.WEBHOOK_KEY  = os.path.join(self.CERTS_DIR, "key.pem")
        self.OFFTOPIC_EMBEDDING_MODEL = _get_env(
            "OFFTOPIC_EMBEDDING_MODEL",
            f"{self.EMBEDDING_MODEL}-offtopic",
            conv=str
        )

    # ─── Purchase Tiers for Requests ─────────────────────────────
    PURCHASE_TIERS: Dict[int, int] = field(default_factory=lambda: {100: 100, 250: 250, 500: 00, 1000: 3000})

    # ─── Group Limits ───────────────────────────────────────────
    ALLOWED_GROUP: str = field(default_factory=lambda: _get_env("ALLOWED_GROUP", "@galaxytapchat"))
    ALLOWED_GROUP_ID: int = field(default_factory=lambda: _get_env("ALLOWED_GROUP_ID", "-1002182233770", conv=int))
    GROUP_DAILY_LIMIT: int = field(default_factory=lambda: _get_env("GROUP_DAILY_LIMIT", "1000", conv=int))
    ON_TOPIC_DAILY_LIMIT: int = field(default_factory=lambda: _get_env("ON_TOPIC_DAILY_LIMIT", "50", conv=int))
    LIMIT_EXHAUSTED_PHRASES: List[str] = field(
        default_factory=lambda: [
            "I'm a bit tired", "I have some work", "Be right back later",
            "Hold on a sec", "Can't chat now", "Busy moment", "Talk later",
            "Give me a break", "I'm occupied", "Let's pause", "Need a breather",
            "Occupied atm", "Later, please", "Catch you later", "I have errands",
            "Busy, ttyl", "Out of office", "BRB", "Hang on", "One moment",
            "Please wait", "Be right with you", "Got tasks", "On a call",
            "In a meeting", "Deep in thought", "Processing info", "Need focus time",
            "Hold tight", "On a break", "Resting now", "Offline soon", "Can't respond",
            "Need downtime", "Busy schedule", "Oof, busy", "Gotta go", "On duty",
            "Pause chat", "Resuming soon", "Busy bee", "Taking five", "Recharging now",
            "Provider delay", "RSVP later", "Focusing now"
        ]
    )

    # ─── Models for Various Tasks ────────────────────────────────
    BASE_MODEL: str = field(default_factory=lambda: _get_env("BASE_MODEL", "gpt-4.1-nano"))
    RESPONSE_MODEL: str = field(default_factory=lambda: _get_env("RESPONSE_MODEL", "gpt-4.1-mini"))
    REASONING_MODEL: str = field(default_factory=lambda: _get_env("REASONING_MODEL", "o4-mini"))
    POST_MODEL: str = field(default_factory=lambda: _get_env("POST_MODEL", "gpt-4.1"))
    EMBEDDING_MODEL: str = field(default_factory=lambda: _get_env("EMBEDDING_MODEL", "text-embedding-3-large"))
    MODERATION_MODEL: str = field(default_factory=lambda: _get_env("MODERATION_MODEL", "text-moderation-latest"))

    # ─── On/Off-topic knowledge files ────────────────────────────────
    KNOWLEDGE_ON_FILE: str = field(default_factory=lambda: _get_env("KNOWLEDGE_ON_FILE", "knowledge_on.json", conv=str))
    KNOWLEDGE_OFF_FILE: str = field(default_factory=lambda: _get_env("KNOWLEDGE_OFF_FILE", "knowledge_off.json", conv=str))
    OFFTOPIC_EMBEDDING_MODEL: str = field(init=False)

    # ─── Hybrid Fallback Parameters ──────────────────────────────
    HYBRID_FALLBACK_THRESHOLD: float = field(default_factory=lambda: _get_env("HYBRID_FALLBACK_THRESHOLD", "0.35", conv=float))
    RELEVANCE_THRESHOLD: float = field(default_factory=lambda: _get_env("RELEVANCE_THRESHOLD", "0.3", conv=float))
    KNOWLEDGE_TOP_K: int = field(default_factory=lambda: _get_env("KNOWLEDGE_TOP_K", "4", conv=int))

    # ─── Memory & Caching ───────────────────────────────────────
    SHORT_MEMORY_LIMIT: int = field(default_factory=lambda: _get_env("SHORT_MEMORY_LIMIT", "200", conv=int))
    SUMMARY_THRESHOLD: int = field(default_factory=lambda: _get_env("SUMMARY_THRESHOLD", "40", conv=int))
    MEMORY_TTL_DAYS: int = field(default_factory=lambda: _get_env("MEMORY_TTL_DAYS", "7", conv=int))

    # ─── Group Ping Settings ────────────────────────────────────
    PING_INTERVAL_MINUTES: int = field(default_factory=lambda: _get_env("PING_INTERVAL_MINUTES", "10", conv=int))
    PING_IDLE_THRESHOLD_SECONDS: int = field(default_factory=lambda: _get_env("PING_IDLE_THRESHOLD_SECONDS", "900", conv=int))
    ADAPTIVE_IDLE_MULTIPLIER: float = field(default_factory=lambda: _get_env("ADAPTIVE_IDLE_MULTIPLIER", "1.5", conv=float))
    ACTIVE_RECENT_SECONDS: int = field(default_factory=lambda: _get_env("ACTIVE_RECENT_SECONDS", "1800", conv=int))
    PING_HISTORY_COUNT: int = field(default_factory=lambda: _get_env("PING_HISTORY_COUNT", "10", conv=int))
    ACTIVE_TTL_SECONDS: int = field(default_factory=lambda: _get_env("ACTIVE_TTL_SECONDS", "3600", conv=int))
    PING_USER_COOLDOWN_SECONDS: int = field(default_factory=lambda: _get_env("PING_USER_COOLDOWN_SECONDS", "14400", conv=int))
    GROUP_PING_MAX_VALENCE: float = field(default_factory=lambda: _get_env("GROUP_PING_MAX_VALENCE", "0.85", conv=float))
    GROUP_PING_MAX_AROUSAL: float = field(default_factory=lambda: _get_env("GROUP_PING_MAX_AROUSAL", "0.85", conv=float))

    # ─── Personal Ping Settings ─────────────────────────────────
    PERSONAL_PING_INTERVAL_MIN: int = field(default_factory=lambda: _get_env("PERSONAL_PING_INTERVAL_MIN", "10", conv=int))
    PERSONAL_PING_HISTORY_COUNT: int = field(default_factory=lambda: _get_env("PERSONAL_PING_HISTORY_COUNT", "8", conv=int))
    PERSONAL_PING_IDLE_THRESHOLD_SECONDS: int = field(default_factory=lambda: _get_env("PERSONAL_PING_IDLE_THRESHOLD_SECONDS", "12345", conv=int))
    PERSONAL_PING_ADAPTIVE_MULTIPLIER: float = field(default_factory=lambda: _get_env("PERSONAL_PING_ADAPTIVE_MULTIPLIER", "1.2", conv=float))
    PERSONAL_PING_RETENTION_SECONDS: int = field(default_factory=lambda: _get_env("PERSONAL_PING_RETENTION_SECONDS", "604800", conv=int))
    PERSONAL_PING_BATCH_SIZE: int = field(default_factory=lambda: _get_env("PERSONAL_PING_BATCH_SIZE", "5", conv=int))
    PERSONAL_PING_MIN_BOREDOM: float = field(default_factory=lambda: _get_env("PERSONAL_PING_MIN_BOREDOM", "0.75", conv=float))
    PERSONAL_PING_BIORHYTHM_WEIGHT: float = field(default_factory=lambda: _get_env("PERSONAL_PING_BIORHYTHM_WEIGHT", "0.4", conv=float))
    PERSONAL_PING_START_HOUR: int = field(default_factory=lambda: _get_env("PERSONAL_PING_START_HOUR", "9", conv=int))
    PERSONAL_PING_END_HOUR:   int = field(default_factory=lambda: _get_env("PERSONAL_PING_END_HOUR",   "21", conv=int))

    # ─── New User Greeting Settings ─────────────────────────────
    NEW_USER_TTL_SECONDS: int = field(default_factory=lambda: _get_env("NEW_USER_TTL_SECONDS", "86400", conv=int))
    GREETING_RATE_LIMIT: int = field(default_factory=lambda: _get_env("GREETING_RATE_LIMIT", "1", conv=int))
    GREETING_RATE_WINDOW_SECONDS: int = field(default_factory=lambda: _get_env("GREETING_RATE_WINDOW_SECONDS", "10", conv=int))

    # ─── Basic Spam Filter ──────────────────────────────────────
    SPAM_WINDOW: int = field(default_factory=lambda: _get_env("SPAM_WINDOW", "1", conv=int))
    SPAM_LIMIT: int = field(default_factory=lambda: _get_env("SPAM_LIMIT", "1", conv=int))

    # ─── Emoji Ping Configuration ───────────────────────────────
    EMOJI_PING_LIST: List[str] = field(
        default_factory=lambda: [
            "🌝", "👀", "🫣", "✌️", "🫦", "🤡", "🐥", "👽", "👻", "🎮",
            "😎", "🤗", "🫩", "😈", "🐝", "🌚"
        ]
    )
    EMOJI_PING_PROBABILITY: float = field(default_factory=lambda: _get_env("EMOJI_PING_PROBABILITY", "0.25", conv=float))
    EMOJI_APPEND_PROBABILITY: float = field(default_factory=lambda: _get_env("EMOJI_APPEND_PROBABILITY", "0.1", conv=float))

    # ─── Passive Moderation Settings ────────────────────────────
    ENABLE_MODERATION: bool = field(default_factory=lambda: _get_env("ENABLE_MODERATION", "true").lower() == "true")
    MODERATOR_IDS: List[int] = field(
        default_factory=lambda: [
            *(
                int(x) for x in _get_env("MODERATOR_IDS", "").split(",")
                if x.strip().isdigit()
            )
        ]
    )
    MODERATOR_NOTIFICATION_CHAT_ID: int = field(default_factory=lambda: _get_env("MODERATOR_NOTIFICATION_CHAT_ID", "0", conv=int))
    MODERATION_TOXICITY_THRESHOLD: float = field(default_factory=lambda: _get_env("MODERATION_TOXICITY_THRESHOLD", "0.6", conv=float))
    MODERATION_ALLOWED_LINK_KEYWORDS: List[str] = field(default_factory=lambda: _get_env("MODERATION_ALLOWED_LINK_KEYWORDS", "galaxytap").split(","))
    MODERATION_SPAM_LINK_THRESHOLD: int = field(default_factory=lambda: _get_env("MODERATION_SPAM_LINK_THRESHOLD", "3", conv=int))
    MOD_PERIOD_SECONDS: int = field(default_factory=lambda: _get_env("MOD_PERIOD_SECONDS", "5", conv=int))
    MOD_MAX_MESSAGES: int = field(default_factory=lambda: _get_env("MOD_MAX_MESSAGES", "5", conv=int))
    SUSPICIOUS_THRESHOLD: int = field(default_factory=lambda: _get_env("SUSPICIOUS_THRESHOLD", "2", conv=int))
    SUSPICIOUS_WINDOW_SEC: int = field(default_factory=lambda: _get_env("SUSPICIOUS_WINDOW_SEC", "60", conv=int))

    # ─── Twitter API Credentials ─────────────────────────────────
    TWITTER_API_KEY: str = field(default_factory=lambda: _get_env("TWITTER_API_KEY", required=True))
    TWITTER_API_SECRET: str = field(default_factory=lambda: _get_env("TWITTER_API_SECRET", required=True))
    TWITTER_ACCESS_TOKEN: str = field(default_factory=lambda: _get_env("TWITTER_ACCESS_TOKEN", required=True))
    TWITTER_ACCESS_TOKEN_SECRET: str = field(default_factory=lambda: _get_env("TWITTER_ACCESS_TOKEN_SECRET", required=True))
    TWITTER_PERSONA_CHAT_ID: int = field(default_factory=lambda: _get_env("TWITTER_PERSONA_CHAT_ID", "0", conv=int))
    TWITTER_FALLBACK_TWEETS: List[str] = field(default_factory=lambda: [
    "Stay tuned for more insights!",
    "More crypto news coming soon 🚀",
    "Catch you later with fresh updates!"
    ])


    # ─── Mood-based temperature/top_p scaling ───────────────────
    MOOD_TEMP_SCALE: float = field(default_factory=lambda: _get_env("MOOD_TEMP_SCALE", "0.8", conv=float))
    MOOD_TEMP_MIN: float = field(default_factory=lambda: _get_env("MOOD_TEMP_MIN", "0.1", conv=float))
    MOOD_TOP_P_SCALE: float = field(default_factory=lambda: _get_env("MOOD_TOP_P_SCALE", "0.9", conv=float))
    MOOD_TOP_P_MIN: float = field(default_factory=lambda: _get_env("MOOD_TOP_P_MIN", "0.05", conv=float))

_settings_singleton: Optional[Settings] = None

def get_settings() -> Settings:

    global _settings_singleton
    if _settings_singleton is None:
        _settings_singleton = Settings()
    return _settings_singleton


class _SettingsProxy:

    def __getattr__(self, item):
        return getattr(get_settings(), item)

    def __setattr__(self, key: str, value: Any) -> None:
        setattr(get_settings(), key, value)


settings = _SettingsProxy()
EOF
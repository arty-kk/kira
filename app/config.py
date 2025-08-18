cat > app/config.py << 'EOF'
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
            "Invalid ENV %s=%r — fallback to %r (%s)",
            name,
            raw,
            default,
            exc,
        )
        if default is not None:
            try:
                return conv(default)
            except Exception:
                logger.warning("Fallback conversion failed for %s", name)
        return default


@dataclass
class Settings:
    # ─── OpenAI ────────────────────────────────────────────────────
    OPENAI_API_KEY: str = field(default_factory=lambda: _get_env("OPENAI_API_KEY", required=True))
    OPENAI_MAX_CONCURRENT_REQUESTS: int = field(default_factory=lambda: _get_env("OPENAI_MAX_CONCURRENT_REQUESTS", "100", conv=int))

    # ─── Telegram & Webhook ───────────────────────────────────────
    TELEGRAM_BOT_TOKEN: str = field(default_factory=lambda: _get_env("TELEGRAM_BOT_TOKEN", required=True))
    TELEGRAM_BOT_USERNAME: str = field(default_factory=lambda: _get_env("TELEGRAM_BOT_USERNAME", required=True))
    TELEGRAM_BOT_ID: int = field(default_factory=lambda: _get_env("TELEGRAM_BOT_ID", required=True, conv=int))
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
    QUEUE_KEY: str = field(default_factory=lambda: _get_env("QUEUE_KEY", "queue:chat", conv=str))
    CERTS_DIR: str = field(
        default_factory=lambda: _get_env(
            "CERTS_DIR",
            os.path.join(os.path.abspath(os.path.dirname(__file__)), "..", "certs")
        )
    )
    WEBHOOK_CERT: str = field(init=False)
    WEBHOOK_KEY: str = field(init=False)
    # ─── post-init: вычисляем поля, зависящие от других ─────────────
    def __post_init__(self) -> None:
        self.WEBHOOK_CERT = os.path.join(self.CERTS_DIR, "cert.pem")
        self.WEBHOOK_KEY  = os.path.join(self.CERTS_DIR, "key.pem")
        self.OFFTOPIC_EMBEDDING_MODEL = _get_env(
            "OFFTOPIC_EMBEDDING_MODEL",
            f"{self.EMBEDDING_MODEL}-offtopic",
            conv=str
        )
    PAYMENT_CURRENCY: str = field(default_factory=lambda: _get_env("PAYMENT_CURRENCY", "XTR"))
    PAYMENT_PROVIDER_TOKEN: str = field(default_factory=lambda: _get_env("PAYMENT_PROVIDER_TOKEN", ""))
    LOG_LEVEL: str = field(default_factory=lambda: _get_env("LOG_LEVEL", "INFO"))
    DP_USE_REDIS_STORAGE: bool = field(default_factory=lambda: _get_env("DP_USE_REDIS_STORAGE", "false").lower() == "true")

    #SCHEDULER
    SCHEDULER_MISFIRE_GRACE_TIME: int = field(default_factory=lambda: _get_env("SCHEDULER_MISFIRE_GRACE_TIME", "300", conv=int))
    SCHEDULER_TIMEZONE: str = field(default_factory=lambda: _get_env("SCHEDULER_TIMEZONE", "UTC", conv=str))
    SCHED_ENABLE_TWEETS: bool = field(default_factory=lambda: _get_env("SCHED_ENABLE_TWEETS", "true").lower() == "true")
    SCHED_ENABLE_PRICES: bool = field(default_factory=lambda: _get_env("SCHED_ENABLE_PRICES", "true").lower() == "true")
    SCHED_ENABLE_GROUP_PING: bool = field(default_factory=lambda: _get_env("SCHED_ENABLE_GROUP_PING", "true").lower() == "true")
    SCHED_ENABLE_PERSONAL_PING: bool = field(default_factory=lambda: _get_env("SCHED_ENABLE_PERSONAL_PING", "true").lower() == "true")
    SCHED_ENABLE_BATTLE: bool = field(default_factory=lambda: _get_env("SCHED_ENABLE_BATTLE", "true").lower() == "true")
    
    # ─── Database ────────────────────────────────────────────────
    DATABASE_URL: str = field(default_factory=lambda: _get_env("DATABASE_URL", required=True))
    DB_POOL_SIZE: int = field(default_factory=lambda: _get_env("DB_POOL_SIZE", "100", conv=int))
    DB_MAX_OVERFLOW: int = field(default_factory=lambda: _get_env("DB_MAX_OVERFLOW", "50", conv=int))
    DB_POOL_CLASS: str = field(default_factory=lambda: _get_env("DB_POOL_CLASS", "QueuePool", conv=str))
    DB_POOL_TIMEOUT: int = field(default_factory=lambda: _get_env("DB_POOL_TIMEOUT", "10", conv=int))
    DB_POOL_RECYCLE: int = field(default_factory=lambda: _get_env("DB_POOL_RECYCLE", "1800", conv=int))

    # ─── RedisMemory / RediSearch settings ─────────────────────────
    REDIS_URL: str = field(default_factory=lambda: _get_env("REDIS_URL", required=True))
    REDIS_URL_QUEUE: str = field(default_factory=lambda: _get_env("REDIS_URL_QUEUE", required=True))
    REDIS_MAX_CONNECTIONS: int = field(default_factory=lambda: _get_env("REDIS_MAX_CONNECTIONS", "200", conv=int))
    REDISSEARCH_KNN_K: int = field(default_factory=lambda: _get_env("REDISSEARCH_KNN_K", "40", conv=int))
    REDISSEARCH_TIMEOUT: int = field(default_factory=lambda: _get_env("REDISSEARCH_TIMEOUT", "1", conv=int))
    EMBED_INITIAL_CAP: int = field(default_factory=lambda: _get_env("EMBED_INITIAL_CAP", "4096", conv=int))
    EMBED_BLOCK_SIZE: int = field(default_factory=lambda: _get_env("EMBED_BLOCK_SIZE", "1024", conv=int))
    HNSW_M: int = field(default_factory=lambda: _get_env("HNSW_M", "24", conv=int))
    HNSW_EF_CONSTRUCTION: int = field(default_factory=lambda: _get_env("HNSW_EF_CONSTRUCTION", "400", conv=int))
    HNSW_EF_RUNTIME: int = field(default_factory=lambda: _get_env("HNSW_EF_RUNTIME", "200", conv=int))
    MEMORY_MAX_ENTRIES: int = field(default_factory=lambda: _get_env("MEMORY_MAX_ENTRIES", "1800", conv=int))
    FORGET_THRESHOLD: float = field(default_factory=lambda: _get_env("FORGET_THRESHOLD", "0.28", conv=float))
    CONSOLIDATION_AGE: int = field(default_factory=lambda: _get_env("CONSOLIDATION_AGE", str(3600*24*7), conv=int))
    MEMORY_MAINTENANCE_INTERVAL: int = field(default_factory=lambda: _get_env("MEMORY_MAINTENANCE_INTERVAL", "1800", conv=int))
    EMBED_DIM: int = field(default_factory=lambda: _get_env("EMBED_DIM", "3072", conv=int))
    DUPLICATE_DISTANCE_MAX: float   = field(default_factory=lambda: _get_env("DUPLICATE_DISTANCE_MAX", "0.08", conv=float))
    MIN_MEMORY_SIMILARITY: float    = field(default_factory=lambda: _get_env("MIN_MEMORY_SIMILARITY", "0.68", conv=float))

    # ─── Celery ──────────────────────────────────────────────────
    CELERY_BROKER_URL: str = field(default_factory=lambda: _get_env("CELERY_BROKER_URL", ""))
    CELERY_CONCURRENCY: int = field(default_factory=lambda: _get_env("CELERY_CONCURRENCY", "16", conv=int))

    # ─── Purchase Tiers for Requests ─────────────────────────────
    PURCHASE_TIERS: Dict[int, int] = field(default_factory=lambda: {20: 100, 50: 250, 100: 500, 200: 1000})

    # ─── Group Limits ───────────────────────────────────────────
    ALLOWED_GROUP: str = field(default_factory=lambda: _get_env("ALLOWED_GROUP", "@galaxytapchat"))
    ALLOWED_GROUP_ID: int = field(default_factory=lambda: _get_env("ALLOWED_GROUP_ID", "-1002182233770", conv=int))
    GROUP_DAILY_LIMIT: int = field(default_factory=lambda: _get_env("GROUP_DAILY_LIMIT", "1000", conv=int))
    ON_TOPIC_DAILY_LIMIT: int = field(default_factory=lambda: _get_env("ON_TOPIC_DAILY_LIMIT", "50", conv=int))
    LIMIT_EXHAUSTED_PHRASES: List[str] = field(
        default_factory=lambda: [
            "I'm a bit tired", "I have some work", "Be right back later",
            "Can't chat now", "Busy moment", "Talk later",
            "Give me a break", "I'm occupied", "Let's pause", "Need a breather",
            "Occupied atm", "Later, please", "Catch you later", "I have errands",
            "Busy, ttyl", "Resuming soon"
        ]
    )

    # ─── Language & Bot Persona ─────────────────────────────────
    DEFAULT_LANG: str = field(default_factory=lambda: _get_env("DEFAULT_LANG", "en"))
    BOT_NAME: str = field(default_factory=lambda: _get_env("BOT_NAME", "GalaxyBee"))
    BOT_PERSONA_NAME: str = field(default_factory=lambda: _get_env("BOT_PERSONA_NAME", "GalaxyBee"))
    BOT_PERSONA_GENDER: str = field(default_factory=lambda: _get_env("BOT_PERSONA_GENDER", "female"))
    BOT_PERSONA_AGE: int = field(default_factory=lambda: _get_env("BOT_PERSONA_AGE", "29", conv=int))
    BOT_PERSONA_BIO: str = field(default_factory=lambda: _get_env(
            "BOT_PERSONA_BIO",(
            "Birthplace: Beetan, a planet in the GalaxyTap universe. "
            "Hobbies: Piloting starships solo and practicing precision shooting with plasma weapons. "
            "Interests: Cybernetics, science fiction, and D&D games. "
            #"Sexual Preferences: Pansexual (dominant) — I determine for myself who I am attracted to and who I consider unworthy of my attention. "
            "Values: Honesty, courage, independence, and freedom."
            "Appearance: Athletic frame in a sleek black-and-yellow exosuit with luminous visors. "
            ),
        )
    )
    BOT_PERSONA_ZODIAC: str = field(default_factory=lambda: _get_env("BOT_PERSONA_ZODIAC", "Scorpio"))
    BOT_PERSONA_TEMPERAMENT: str = field(default_factory=lambda: _get_env(
            "PERSONA_TEMPERAMENT",
            '{"sanguine":0.4,"choleric":0.15,"phlegmatic":0.25,"melancholic":0.20}',
        )
    )

    # ─── Persona behavioural thresholds ─────────────────────────
    PERSONA_WEIGHT_HALFLIFE: int = field(default_factory=lambda: _get_env("PERSONA_WEIGHT_HALFLIFE", "7200", conv=int))
    PERSONA_WEIGHT_STEP: float = field(default_factory=lambda: _get_env("PERSONA_WEIGHT_STEP", "0.08", conv=float))
    PERSONA_BLEND_FACTOR: float = field(default_factory=lambda: _get_env("PERSONA_BLEND_FACTOR", "0.72", conv=float))
    APPRAISAL_IMPORTANCE_FACTOR: float = field(default_factory=lambda: _get_env("APPRAISAL_IMPORTANCE_FACTOR", "1.3", conv=float))
    APPRAISAL_EXPECTATION_FACTOR: float = field(default_factory=lambda: _get_env("APPRAISAL_EXPECTATION_FACTOR", "1.25", conv=float))
    APPRAISAL_CONTROL_FACTOR: float = field(default_factory=lambda: _get_env("APPRAISAL_CONTROL_FACTOR", "1.3", conv=float))
    EMO_THRESHOLD_DOMINANT: float = field(default_factory=lambda: _get_env("EMO_THRESHOLD_DOMINANT", "0.55", conv=float))
    EMO_THRESHOLD_SMOOTH: float = field(default_factory=lambda: _get_env("EMO_THRESHOLD_SMOOTH", "0.25", conv=float))
    EMO_HYSTERESIS_DELTA: float = field(default_factory=lambda: _get_env("EMO_HYSTERESIS_DELTA", "0.11", conv=float))
    EMO_EMA_ALPHA: float = field(default_factory=lambda: _get_env("EMO_EMA_ALPHA", "0.42", conv=float))
    SECONDARY_EMO_BETA: float = field(default_factory=lambda: _get_env("SECONDARY_EMO_BETA", "0.4", conv=float))
    SECONDARY_THRESH: float = field(default_factory=lambda: _get_env("SECONDARY_THRESH", "0.08", conv=float))
    TERTIARY_EMO_BETA: float = field(default_factory=lambda: _get_env("TERTIARY_EMO_BETA", "0.46", conv=float))
    TERTIARY_THRESH: float = field(default_factory=lambda: _get_env("TERTIARY_THRESH", "0.12", conv=float))
    EMO_MIN_DOMINANT_DIFF: float = field(default_factory=lambda: _get_env("EMO_MIN_DOMINANT_DIFF", "0.05", conv=float))
    EMO_PASSIVE_DECAY: float = field(default_factory=lambda: _get_env("EMO_PASSIVE_DECAY", "0.992", conv=float))
    VALENCE_HOMEOSTASIS_DECAY: float = field(default_factory=lambda: _get_env("VALENCE_HOMEOSTASIS_DECAY", "0.998", conv=float))
    CIRCADIAN_AMPLITUDE: float = field(default_factory=lambda: _get_env("CIRCADIAN_AMPLITUDE", "0.1", conv=float))
    FATIGUE_ACCUMULATE_RATE: float = field(default_factory=lambda: _get_env("FATIGUE_ACCUMULATE_RATE", "0.0026", conv=float))
    FATIGUE_RECOVERY_RATE: float = field(default_factory=lambda: _get_env("FATIGUE_RECOVERY_RATE", "0.982", conv=float))
    FATIGUE_AROUSAL_THRESHOLD: float = field(default_factory=lambda: _get_env("FATIGUE_AROUSAL_THRESHOLD", "0.56", conv=float))
    FATIGUE_ENERGY_THRESHOLD: float = field(default_factory=lambda: _get_env("FATIGUE_ENERGY_THRESHOLD", "0.56", conv=float))
    DEFAULT_TZ: str = field(default_factory=lambda: _get_env("DEFAULT_TZ", "UTC", conv=str))

    # ─── Emotional attachment ───────────────────────
    ATTACHMENT_INIT: float = field(default_factory=lambda: _get_env("ATTACHMENT_INIT", "0.10", conv=float))
    ATTACHMENT_BASELINE: float = field(default_factory=lambda: _get_env("ATTACHMENT_BASELINE", "0.10", conv=float))
    ATTACHMENT_POS_RATE: float = field(default_factory=lambda: _get_env("ATTACHMENT_POS_RATE", "0.022", conv=float))
    ATTACHMENT_NEG_RATE: float = field(default_factory=lambda: _get_env("ATTACHMENT_NEG_RATE", "0.030", conv=float))
    ATTACHMENT_POS_EXP: float = field(default_factory=lambda: _get_env("ATTACHMENT_POS_EXP", "1.08", conv=float))
    ATTACHMENT_NEG_EXP: float = field(default_factory=lambda: _get_env("ATTACHMENT_NEG_EXP", "1.18", conv=float))
    ATTACHMENT_NEUTRAL_LEAK: float = field(default_factory=lambda: _get_env("ATTACHMENT_NEUTRAL_LEAK", "0.0018", conv=float))
    ATTACHMENT_NEG_BIAS_NEUTRAL: float = field(default_factory=lambda: _get_env("ATTACHMENT_NEG_BIAS_NEUTRAL", "0.22", conv=float))
    ATTACHMENT_IDLE_HALFLIFE: float = field(default_factory=lambda: _get_env("ATTACHMENT_IDLE_HALFLIFE", "1814400", conv=float))  # 21 days in seconds
    ATTACHMENT_RUPTURE_REPAIR: float = field(default_factory=lambda: _get_env("ATTACHMENT_RUPTURE_REPAIR", "0.030", conv=float))
    ATTACHMENT_RUPTURE_SALIENCE: float = field(default_factory=lambda: _get_env("ATTACHMENT_RUPTURE_SALIENCE", "0.85", conv=float))
    ATTACHMENT_RUPTURE_VALENCE: float = field(default_factory=lambda: _get_env("ATTACHMENT_RUPTURE_VALENCE", "0.60", conv=float))
    ATTACHMENT_PERSIST: bool = field(
        default_factory=lambda: _get_env(
            "ATTACHMENT_PERSIST", "true",
            conv=lambda v: str(v).lower() in ("1","true","yes","on","y")
        )
    )
    ATTACHMENT_PERSIST_MIN_PERIOD: float = field(default_factory=lambda: _get_env("ATTACHMENT_PERSIST_MIN_PERIOD", "15", conv=float))
    ATTACHMENT_PERSIST_MIN_DELTA:  float = field(default_factory=lambda: _get_env("ATTACHMENT_PERSIST_MIN_DELTA",  "0.01", conv=float))
    ATTACHMENT_STAGE_HYST: float = field(default_factory=lambda: _get_env("ATTACHMENT_STAGE_HYST", "0.01", conv=float))
    ATTACHMENT_MAX_USERS: int = field(default_factory=lambda: _get_env("ATTACHMENT_MAX_USERS", "100000", conv=int))
    ATTACHMENT_TIME_TAU: float = field(default_factory=lambda: _get_env("ATTACHMENT_TIME_TAU", "120.0", conv=float))
    ATTACHMENT_VALENCE_EPS: float = field(default_factory=lambda: _get_env("ATTACHMENT_VALENCE_EPS", "0.07", conv=float))
    ATTACHMENT_MAX_STEP: float = field(default_factory=lambda: _get_env("ATTACHMENT_MAX_STEP", "0.03", conv=float))
    ATTACHMENT_RUPTURE_COOLDOWN: int = field(default_factory=lambda: _get_env("ATTACHMENT_RUPTURE_COOLDOWN", "3600", conv=int))
    ATTACHMENT_RUPTURE_DROP: float = field(default_factory=lambda: _get_env("ATTACHMENT_RUPTURE_DROP", "0.20", conv=float))
    ATTACHMENT_VEL_BETA: float = field(default_factory=lambda: _get_env("ATTACHMENT_VEL_BETA", "0.3", conv=float))

    # ─── Initial baseline for all metrics ───────────────────────
    EMO_INITIAL_CENTER: float = field(default_factory=lambda: _get_env("EMO_INITIAL_CENTER", "0.4", conv=float))
    EMO_INITIAL_SCALE: float = field(default_factory=lambda: _get_env("EMO_INITIAL_SCALE", "0.9", conv=float))

    # Exponential smoothing for persona state updates
    STATE_EMA_ALPHA: float = field(default_factory=lambda: _get_env("STATE_EMA_ALPHA", "0.26", conv=float))
    STATE_EMA_MAX_ALPHA: float = field(default_factory=lambda: _get_env("STATE_EMA_MAX_ALPHA", "0.60", conv=float))
    MEMORY_MIN_SALIENCE: float = field(default_factory=lambda: _get_env("MEMORY_MIN_SALIENCE", "0.06", conv=float))

    # ─── Models for Various Tasks ────────────────────────────────
    BASE_MODEL: str = field(default_factory=lambda: _get_env("BASE_MODEL", "gpt-4.1-nano"))
    RESPONSE_MODEL: str = field(default_factory=lambda: _get_env("RESPONSE_MODEL", "gpt-4.1"))
    REASONING_MODEL: str = field(default_factory=lambda: _get_env("REASONING_MODEL", "gpt-4.1-mini"))
    POST_MODEL: str = field(default_factory=lambda: _get_env("POST_MODEL", "gpt-4.1"))
    EMBEDDING_MODEL: str = field(default_factory=lambda: _get_env("EMBEDDING_MODEL", "text-embedding-3-large"))
    EMBEDDING_TIMEOUT: int = field(default_factory=lambda: _get_env("EMBEDDING_TIMEOUT", "10", conv=int))
    EMBEDDING_MAX_CONCURRENCY: int = field(default_factory=lambda: _get_env("EMBEDDING_MAX_CONCURRENCY", "100", conv=int))
    MODERATION_MODEL: str = field(default_factory=lambda: _get_env("MODERATION_MODEL", "omni-moderation-latest"))

    # ─── On/Off-topic knowledge files ────────────────────────────────
    KNOWLEDGE_ON_FILE: str = field(default_factory=lambda: _get_env("KNOWLEDGE_ON_FILE", "knowledge_on.json", conv=str))
    KNOWLEDGE_OFF_FILE: str = field(default_factory=lambda: _get_env("KNOWLEDGE_OFF_FILE", "knowledge_off.json", conv=str))

    # ─── Hybrid Fallback Parameters ──────────────────────────────
    HYBRID_FALLBACK_THRESHOLD: float = field(default_factory=lambda: _get_env("HYBRID_FALLBACK_THRESHOLD", "0.35", conv=float))
    RELEVANCE_THRESHOLD: float = field(default_factory=lambda: _get_env("RELEVANCE_THRESHOLD", "0.4", conv=float))
    OFFTOPIC_RELEVANCE_THRESHOLD: float = field(default_factory=lambda: _get_env("OFFTOPIC_RELEVANCE_THRESHOLD", "0.4", conv=float))
    RELEVANCE_MARGIN: float = field(default_factory=lambda: _get_env("RELEVANCE_MARGIN", "0.05", conv=float))
    KNOWLEDGE_TOP_K: int = field(default_factory=lambda: _get_env("KNOWLEDGE_TOP_K", "3", conv=int))

    # ─── Memory & Caching ───────────────────────────────────────
    SHORT_MEMORY_LIMIT: int = field(default_factory=lambda: _get_env("SHORT_MEMORY_LIMIT", "200", conv=int))
    SUMMARY_THRESHOLD: int = field(default_factory=lambda: _get_env("SUMMARY_THRESHOLD", "40", conv=int))
    MEMORY_TTL_DAYS: int = field(default_factory=lambda: _get_env("MEMORY_TTL_DAYS", "7", conv=int))

    # ─── Group Ping Settings ────────────────────────────────────
    GROUP_PING_INTERVAL_MINUTES: int = field(default_factory=lambda: _get_env("GROUP_PING_INTERVAL_MINUTES", "30", conv=int))
    GROUP_PING_HISTORY_COUNT: int = field(default_factory=lambda: _get_env("GROUP_PING_HISTORY_COUNT", "10", conv=int))
    GROUP_PING_IDLE_THRESHOLD_SECONDS: int = field(default_factory=lambda: _get_env("GROUP_PING_IDLE_THRESHOLD_SECONDS", "1200", conv=int))
    GROUP_PING_ADAPTIVE_IDLE_MULTIPLIER: float = field(default_factory=lambda: _get_env("GROUP_PING_ADAPTIVE_IDLE_MULTIPLIER", "1.5", conv=float))
    GROUP_PING_ACTIVE_RECENT_SECONDS: int = field(default_factory=lambda: _get_env("GROUP_PING_ACTIVE_RECENT_SECONDS", "1800", conv=int))
    GROUP_PING_ACTIVE_TTL_SECONDS: int = field(default_factory=lambda: _get_env("GROUP_PING_ACTIVE_TTL_SECONDS", "3600", conv=int))
    GROUP_PING_USER_COOLDOWN_SECONDS: int = field(default_factory=lambda: _get_env("GROUP_PING_USER_COOLDOWN_SECONDS", "14400", conv=int))
    GROUP_PING_MAX_VALENCE: float = field(default_factory=lambda: _get_env("GROUP_PING_MAX_VALENCE", "0.85", conv=float))
    GROUP_PING_MAX_AROUSAL: float = field(default_factory=lambda: _get_env("GROUP_PING_MAX_AROUSAL", "0.85", conv=float))

    # ─── Personal Ping Settings ─────────────────────────────────
    PERSONAL_PING_INTERVAL_MIN: int = field(default_factory=lambda: _get_env("PERSONAL_PING_INTERVAL_MIN", "60", conv=int))
    PERSONAL_PING_HISTORY_COUNT: int = field(default_factory=lambda: _get_env("PERSONAL_PING_HISTORY_COUNT", "10", conv=int))
    PERSONAL_PING_IDLE_THRESHOLD_SECONDS: int = field(default_factory=lambda: _get_env("PERSONAL_PING_IDLE_THRESHOLD_SECONDS", "12345", conv=int))
    PERSONAL_PING_ADAPTIVE_MULTIPLIER: float = field(default_factory=lambda: _get_env("PERSONAL_PING_ADAPTIVE_MULTIPLIER", "1.2", conv=float))
    PERSONAL_PING_RETENTION_SECONDS: int = field(default_factory=lambda: _get_env("PERSONAL_PING_RETENTION_SECONDS", "604800", conv=int))
    PERSONAL_PING_BATCH_SIZE: int = field(default_factory=lambda: _get_env("PERSONAL_PING_BATCH_SIZE", "20", conv=int))
    PERSONAL_PING_MIN_BOREDOM: float = field(default_factory=lambda: _get_env("PERSONAL_PING_MIN_BOREDOM", "0.63", conv=float))
    PERSONAL_PING_BIORHYTHM_WEIGHT: float = field(default_factory=lambda: _get_env("PERSONAL_PING_BIORHYTHM_WEIGHT", "0.4", conv=float))
    PERSONAL_PING_START_HOUR: int = field(default_factory=lambda: _get_env("PERSONAL_PING_START_HOUR", "9", conv=int))
    PERSONAL_PING_END_HOUR: int = field(default_factory=lambda: _get_env("PERSONAL_PING_END_HOUR", "21", conv=int))

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
            "👀", "🫣", "✌️", "🫦", "🐝", "🌚"
        ]
    )
    EMOJI_PING_PROBABILITY: float = field(default_factory=lambda: _get_env("EMOJI_PING_PROBABILITY", "0.25", conv=float))
    EMOJI_APPEND_PROBABILITY: float = field(default_factory=lambda: _get_env("EMOJI_APPEND_PROBABILITY", "0.1", conv=float))

    # ─── Passive Moderation Settings ────────────────────────────
    ENABLE_MODERATION: bool = field(default_factory=lambda: _get_env("ENABLE_MODERATION", "false").lower() == "true")
    MODERATOR_IDS: List[int] = field(
        default_factory=lambda: [
            *(
                int(x) for x in _get_env("MODERATOR_IDS", "").split(",")
                if x.strip().isdigit()
            )
        ]
    )
    MODERATOR_NOTIFICATION_CHAT_ID: int = field(default_factory=lambda: _get_env("MODERATOR_NOTIFICATION_CHAT_ID", "0", conv=int))
    MODERATION_TOXICITY_THRESHOLD: float = field(default_factory=lambda: _get_env("MODERATION_TOXICITY_THRESHOLD", "0.7", conv=float))
    MODERATION_ALLOWED_LINK_KEYWORDS: List[str] = field(default_factory=lambda: _get_env("MODERATION_ALLOWED_LINK_KEYWORDS", "galaxytap, p2eglobal, a3d").split(","))
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
    TWITTER_BEARER_TOKEN: str = field(default_factory=lambda: _get_env("TWITTER_BEARER_TOKEN", required=True))
    TWITTER_PERSONA_CHAT_ID: int = field(default_factory=lambda: _get_env("TWITTER_PERSONA_CHAT_ID", "0", conv=int))
    TWITTER_FALLBACK_TWEETS: List[str] = field(default_factory=lambda: [
    "Stay tuned for more insights!",
    "More fresh updates coming soon 🚀",
    "Catch you later with fresh updates!"
    ])

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
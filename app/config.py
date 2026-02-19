#app/config.py
from __future__ import annotations

import logging
import os

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Any

from dotenv import load_dotenv

from app.prompts_base import PERSONA_ROLE_DEFAULT_PROMPT

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


def _parse_bool(value: str) -> bool:
    normalized = str(value).strip().lower()
    if normalized in ("1", "true", "yes", "on"):
        return True
    if normalized in ("0", "false", "no", "off"):
        return False
    raise ValueError(f"Unsupported boolean value: {value!r}")


def _parse_int_csv_env(name: str) -> tuple[List[int], List[str]]:
    raw = (_get_env(name, "", conv=str) or "")
    values: List[int] = []
    invalid_tokens: List[str] = []

    for token in str(raw).split(","):
        cleaned = token.strip()
        if not cleaned:
            continue
        try:
            values.append(int(cleaned))
        except Exception:
            invalid_tokens.append(cleaned)

    if invalid_tokens:
        logger.warning("Invalid integer tokens in %s: %s", name, ", ".join(repr(x) for x in invalid_tokens))

    return values, invalid_tokens




def _normalize_comment_link_policy(value: str) -> str:
    normalized = str(value or "").strip().lower()
    allowed = {"group_default", "relaxed"}
    if normalized in allowed:
        return normalized
    logger.warning(
        "Invalid COMMENT_MODERATION_LINK_POLICY=%r; fallback to 'group_default'",
        value,
    )
    return "group_default"

def validate_settings_config(cfg: "Settings") -> None:
    cfg.COMMENT_MODERATION_LINK_POLICY = _normalize_comment_link_policy(cfg.COMMENT_MODERATION_LINK_POLICY)

    if cfg.COMMENT_MODERATION_ENABLED and not cfg.COMMENT_TARGET_CHAT_IDS and not cfg.COMMENT_SOURCE_CHANNEL_IDS:
        logger.warning(
            "COMMENT_MODERATION_ENABLED=true but both COMMENT_TARGET_CHAT_IDS and COMMENT_SOURCE_CHANNEL_IDS are empty; comment/discussion path is effectively disabled"
        )

    if cfg._COMMENT_TARGET_CHAT_IDS_INVALID_TOKENS or cfg._COMMENT_SOURCE_CHANNEL_IDS_INVALID_TOKENS:
        logger.warning(
            "Comment moderation config contains invalid CSV tokens: COMMENT_TARGET_CHAT_IDS=%s COMMENT_SOURCE_CHANNEL_IDS=%s",
            cfg._COMMENT_TARGET_CHAT_IDS_INVALID_TOKENS,
            cfg._COMMENT_SOURCE_CHANNEL_IDS_INVALID_TOKENS,
        )

    if cfg._ALLOWED_GROUP_IDS_INVALID_TOKENS:
        logger.warning(
            "Group access config contains invalid CSV tokens: ALLOWED_GROUP_IDS=%s",
            cfg._ALLOWED_GROUP_IDS_INVALID_TOKENS,
        )

    if cfg._MODERATOR_IDS_INVALID_TOKENS:
        logger.warning(
            "Moderator config contains invalid CSV tokens: MODERATOR_IDS=%s",
            cfg._MODERATOR_IDS_INVALID_TOKENS,
        )

    overlap = set(cfg.COMMENT_TARGET_CHAT_IDS) & set(cfg.COMMENT_SOURCE_CHANNEL_IDS)
    if overlap:
        logger.warning(
            "Comment moderation anomaly: IDs %s appear in both COMMENT_TARGET_CHAT_IDS and COMMENT_SOURCE_CHANNEL_IDS; verify target chats vs source channels to avoid unexpected filtering",
            sorted(overlap),
        )


def _default_api_idempotency_inflight_ttl_sec() -> int:
    call_timeout = int(_get_env("API_CALL_TIMEOUT_SEC", "135", conv=int) or 135)
    return max(30, call_timeout + 20)


@dataclass
class Settings:

    # ─── OpenAI ────────────────────────────────────────────────────
    OPENAI_API_KEY: str = field(default_factory=lambda: _get_env("OPENAI_API_KEY", required=True))
    OPENAI_MAX_CONCURRENT_REQUESTS: int = field(default_factory=lambda: _get_env("OPENAI_MAX_CONCURRENT_REQUESTS", "300", conv=int))
    APPROX_CHARS_PER_TOKEN: float = field(default_factory=lambda: _get_env("APPROX_CHARS_PER_TOKEN", "3.8", conv=float))
    #Models for Various Tasks
    BASE_MODEL: str = field(default_factory=lambda: _get_env("BASE_MODEL", "gpt-4.1-nano"))
    REASONING_MODEL: str = field(default_factory=lambda: _get_env("REASONING_MODEL", "gpt-4.1-mini"))
    RESPONSE_FREE_MODEL: str = field(default_factory=lambda: _get_env("RESPONSE_FREE_MODEL", "gpt-4.1-mini"))
    RESPONSE_MODEL: str = field(default_factory=lambda: _get_env("RESPONSE_MODEL", "gpt-4.1"))
    RESPONSE_REASONING_EFFORT: str = field(default_factory=lambda: _get_env("RESPONSE_REASONING_EFFORT", "low", conv=str))
    RESPONSE_VERBOSITY: str = field(default_factory=lambda: _get_env("RESPONSE_VERBOSITY", "medium", conv=str))
    POST_MODEL: str = field(default_factory=lambda: _get_env("POST_MODEL", "gpt-4.1"))
    MODERATION_MODEL: str = field(default_factory=lambda: _get_env("MODERATION_MODEL", "omni-moderation-latest"))
    TRANSCRIPTION_MODEL: str = field(default_factory=lambda: _get_env("TRANSCRIPTION_MODEL", "whisper-1", conv=str))
    BASE_MODEL_TIMEOUT: float = field(default_factory=lambda: _get_env("BASE_MODEL_TIMEOUT", "30.0", conv=float))
    REASONING_MODEL_TIMEOUT: float = field(default_factory=lambda: _get_env("REASONING_MODEL_TIMEOUT", "60.0", conv=float))
    RESPONSE_FREE_MODEL_TIMEOUT: float = field(default_factory=lambda: _get_env("RESPONSE_FREE_MODEL_TIMEOUT", "90.0", conv=float))
    RESPONSE_MODEL_TIMEOUT: float = field(default_factory=lambda: _get_env("RESPONSE_MODEL_TIMEOUT", "180.0", conv=float))
    POST_MODEL_TIMEOUT: float = field(default_factory=lambda: _get_env("POST_MODEL_TIMEOUT", "300.0", conv=float))
    # Embedding
    EMBEDDING_MODEL: str = field(default_factory=lambda: _get_env("EMBEDDING_MODEL", "text-embedding-3-large"))
    EMBEDDING_MODEL_SMALL: str = field(default_factory=lambda: _get_env("EMBEDDING_MODEL_SMALL", "text-embedding-3-small"))
    EMBEDDING_TIMEOUT: int = field(default_factory=lambda: _get_env("EMBEDDING_TIMEOUT", "60", conv=int))
    EMBEDDING_MAX_CONCURRENCY: int = field(default_factory=lambda: _get_env("EMBEDDING_MAX_CONCURRENCY", "100", conv=int))
    EMBED_BATCH_SIZE: int = field(default_factory=lambda: _get_env("EMBED_BATCH_SIZE", "128", conv=int))

    # ─── Telegram ───────────────────────────────────────
    TELEGRAM_BOT_TOKEN: str = field(default_factory=lambda: _get_env("TELEGRAM_BOT_TOKEN"))
    TELEGRAM_BOT_USERNAME: str = field(default_factory=lambda: _get_env("TELEGRAM_BOT_USERNAME"))
    TELEGRAM_BOT_ID: int = field(default_factory=lambda: _get_env("TELEGRAM_BOT_ID", conv=int))
    BOT_NAME: str = field(default_factory=lambda: _get_env("BOT_NAME", "Kira"))
    DEFAULT_LANG: str = field(default_factory=lambda: _get_env("DEFAULT_LANG", "en"))
    DEFAULT_TZ: str = field(default_factory=lambda: _get_env("DEFAULT_TZ", "UTC", conv=str))
    DIALOGS_DIR: str = field(default_factory=lambda: _get_env("DIALOGS_DIR", "dialogs", conv=str))
    ENABLE_DIALOG_LOGGING: bool = field(
        default_factory=lambda: _get_env("ENABLE_DIALOG_LOGGING", "false", conv=_parse_bool)
    )
    #TG Posting
    TG_PERSONA_CHAT_ID: int = field(default_factory=lambda: _get_env("TG_PERSONA_CHAT_ID", "11", conv=int))
    TG_CHANNEL_ID: int = field(default_factory=lambda: _get_env("TG_CHANNEL_ID", "0", conv=int))
    SCHED_ENABLE_TG_POSTS: bool = field(default_factory=lambda: _get_env("SCHED_ENABLE_TG_POSTS", "false", conv=_parse_bool))
    SCHED_TG_MIN_POSTS: int = field(default_factory=lambda: _get_env("SCHED_TG_MIN_POSTS", "8", conv=int))
    SCHED_TG_MAX_POSTS: int = field(default_factory=lambda: _get_env("SCHED_TG_MAX_POSTS", "15", conv=int))
    SCHED_TG_START_HOUR: int = field(default_factory=lambda: _get_env("SCHED_TG_START_HOUR", "8", conv=int))
    SCHED_TG_END_HOUR: int = field(default_factory=lambda: _get_env("SCHED_TG_END_HOUR", "23", conv=int))
    #UI / Buttons
    ENABLE_PRIVATE_STATIC_WELCOME: bool = field(default_factory=lambda: _get_env("ENABLE_PRIVATE_STATIC_WELCOME", "false", conv=_parse_bool))
    ENABLE_PRIVATE_AI_WELCOME: bool = field(default_factory=lambda: _get_env("ENABLE_PRIVATE_AI_WELCOME", "true", conv=_parse_bool))
    ENABLE_PRIVATE_WELCOME_VIDEO: bool = field(default_factory=lambda: _get_env("ENABLE_PRIVATE_WELCOME_VIDEO", "false", conv=_parse_bool))
    ENABLE_GROUP_AI_WELCOME: bool = field(default_factory=lambda: _get_env("ENABLE_GROUP_AI_WELCOME", "false", conv=_parse_bool))
    CLEAR_SETUP_MESSAGES: bool = field(default_factory=lambda: _get_env("CLEAR_SETUP_MESSAGES", "false", conv=_parse_bool))
    SHOW_SHOP_BUTTON: bool = field(default_factory=lambda: _get_env("SHOW_SHOP_BUTTON", "true", conv=_parse_bool))
    SHOW_REQUESTS_BUTTON: bool = field(
        default_factory=lambda: _get_env(
            "SHOW_REQUESTS_BUTTON",
            _get_env("SHOW_SHOP_BUTTON", "true"),
            conv=_parse_bool,
        )
    )
    SHOW_MEMORY_CLEAR_BUTTON: bool = field(default_factory=lambda: _get_env("SHOW_MEMORY_CLEAR_BUTTON", "true", conv=_parse_bool))
    SHOW_CHANNEL_BUTTON: bool = field(default_factory=lambda: _get_env("SHOW_CHANNEL_BUTTON", "false", conv=_parse_bool))
    SHOW_PERSONA_BUTTON: bool = field(default_factory=lambda: _get_env("SHOW_PERSONA_BUTTON", "true", conv=_parse_bool))
    SHOW_API_BUTTON: bool = field(default_factory=lambda: _get_env("SHOW_API_BUTTON", "true", conv=_parse_bool))
    #Webhook 
    WEBHOOK_URL: str = field(default_factory=lambda: _get_env("WEBHOOK_URL"))
    WEBHOOK_PATH: str = field(default_factory=lambda: _get_env("WEBHOOK_PATH", "/webhook"))
    WEBHOOK_HOST: str = field(default_factory=lambda: _get_env("WEBHOOK_HOST", "0.0.0.0"))
    WEBHOOK_PORT: int = field(default_factory=lambda: _get_env("WEBHOOK_PORT", "8443", conv=int))
    WEBHOOK_FEED_UPDATE_TIMEOUT_SEC: float = field(
        default_factory=lambda: _get_env("WEBHOOK_FEED_UPDATE_TIMEOUT_SEC", "30", conv=float)
    )
    WEBHOOK_DROP_PENDING_UPDATES: bool = field(
        default_factory=lambda: _get_env("WEBHOOK_DROP_PENDING_UPDATES", "false", conv=_parse_bool)
    )
    WEBHOOK_CERT: str = field(init=False)
    WEBHOOK_KEY: str = field(init=False)
    USE_SELF_SIGNED_CERT: bool = field(default_factory=lambda: _get_env("USE_SELF_SIGNED_CERT", "false", conv=_parse_bool))
    CERTS_DIR: str = field(default_factory=lambda: _get_env("CERTS_DIR", os.path.join(os.path.abspath(os.path.dirname(__file__)), "..", "certs")))

    def __post_init__(self) -> None:
        self.WEBHOOK_CERT = os.path.join(self.CERTS_DIR, "cert.pem")
        self.WEBHOOK_KEY  = os.path.join(self.CERTS_DIR, "key.pem")
        os.makedirs(self.CERTS_DIR, exist_ok=True)
        self.ALLOWED_GROUP_IDS, self._ALLOWED_GROUP_IDS_INVALID_TOKENS = _parse_int_csv_env("ALLOWED_GROUP_IDS")
        self.COMMENT_TARGET_CHAT_IDS, self._COMMENT_TARGET_CHAT_IDS_INVALID_TOKENS = _parse_int_csv_env(
            "COMMENT_TARGET_CHAT_IDS"
        )
        self.COMMENT_SOURCE_CHANNEL_IDS, self._COMMENT_SOURCE_CHANNEL_IDS_INVALID_TOKENS = _parse_int_csv_env(
            "COMMENT_SOURCE_CHANNEL_IDS"
        )
        self.MODERATOR_IDS, self._MODERATOR_IDS_INVALID_TOKENS = _parse_int_csv_env("MODERATOR_IDS")
        validate_settings_config(self)

    # ─── Public HTTP API ─────────────────────────────────────
    API_HOST: str = field(default_factory=lambda: _get_env("API_HOST", "0.0.0.0"))
    API_PORT: int = field(default_factory=lambda: _get_env("API_PORT", "8000", conv=int))
    PUBLIC_API_BASE_URL: str = field(default_factory=lambda: _get_env("PUBLIC_API_BASE_URL", "", conv=str))
    API_CALL_TIMEOUT_SEC: int = field(default_factory=lambda: _get_env("API_CALL_TIMEOUT_SEC", "135", conv=int))
    API_RESPOND_TIMEOUT_SEC: int = field(default_factory=lambda: _get_env("API_RESPOND_TIMEOUT_SEC", "120", conv=int))
    API_MAX_CONCURRENCY: int = field(default_factory=lambda: _get_env("API_MAX_CONCURRENCY", "128", conv=int))
    API_QUEUE_TIMEOUT_SEC: float = field(default_factory=lambda: _get_env("API_QUEUE_TIMEOUT_SEC", "0.25", conv=float))
    API_DB_TIMEOUT_MS: int = field(default_factory=lambda: _get_env("API_DB_TIMEOUT_MS", "500", conv=int))
    API_DB_TIMEOUT_AUTH_MS: int = field(default_factory=lambda: _get_env("API_DB_TIMEOUT_AUTH_MS", "250", conv=int))
    API_RATELIMIT_PER_MIN: int = field(default_factory=lambda: _get_env("API_RATELIMIT_PER_MIN", "60", conv=int))
    API_RATELIMIT_BURST_FACTOR: int = field(default_factory=lambda: _get_env("API_RATELIMIT_BURST_FACTOR", "2", conv=int))
    API_RATELIMIT_PER_IP_PER_MIN: int = field(default_factory=lambda: _get_env("API_RATELIMIT_PER_IP_PER_MIN", "360", conv=int))
    API_IDEMPOTENCY_TTL_SEC: int = field(default_factory=lambda: _get_env("API_IDEMPOTENCY_TTL_SEC", "3600", conv=int))
    API_IDEMPOTENCY_INFLIGHT_TTL_SEC: int = field(
        default_factory=lambda: _get_env(
            "API_IDEMPOTENCY_INFLIGHT_TTL_SEC",
            str(_default_api_idempotency_inflight_ttl_sec()),
            conv=int,
        )
    )
    API_KEY_HASH_SECRET: str = field(default_factory=lambda: _get_env("API_KEY_HASH_SECRET", "", conv=str))
    API_KEY_CACHE_TTL_SEC: int = field(default_factory=lambda: _get_env("API_KEY_CACHE_TTL_SEC", "60", conv=int))
    API_KEY_CACHE_NEGATIVE_TTL_SEC: int = field(default_factory=lambda: _get_env("API_KEY_CACHE_NEGATIVE_TTL_SEC", "30", conv=int))
    API_PERSONA_PER_KEY: bool = field(default_factory=lambda: _get_env("API_PERSONA_PER_KEY", "true", conv=_parse_bool))
    API_QUEUE_MAX_PAYLOAD_BYTES: int = field(default_factory=lambda: _get_env("API_QUEUE_MAX_PAYLOAD_BYTES", "131072", conv=int))
    API_QUEUE_SNAPSHOT_SEC: int = field(default_factory=lambda: _get_env("API_QUEUE_SNAPSHOT_SEC", "60", conv=int))
    API_PROCESSING_SWEEP_BATCH: int = field(default_factory=lambda: _get_env("API_PROCESSING_SWEEP_BATCH", "200", conv=int))
    TRUSTED_PROXY_IPS: List[str] = field(
        default_factory=lambda: [
            x.strip()
            for x in (_get_env("TRUSTED_PROXY_IPS", "", conv=str) or "").split(",")
            if x.strip()
        ]
    )

    #Basic Spam Filter
    SPAM_WINDOW: int = field(default_factory=lambda: _get_env("SPAM_WINDOW", "10", conv=int))
    SPAM_LIMIT: int = field(default_factory=lambda: _get_env("SPAM_LIMIT", "6", conv=int))
    #Group Limits
    ALLOWED_GROUP_IDS: List[int] = field(default_factory=list)
    _ALLOWED_GROUP_IDS_INVALID_TOKENS: List[str] = field(default_factory=list, init=False, repr=False)
    _MODERATOR_IDS_INVALID_TOKENS: List[str] = field(default_factory=list, init=False, repr=False)
    COMMENT_MODERATION_ENABLED: bool = field(
        default_factory=lambda: _get_env("COMMENT_MODERATION_ENABLED", "false", conv=_parse_bool)
    )
    COMMENT_MODERATION_DELETE_EXTERNAL_REPLIES: bool = field(
        default_factory=lambda: _get_env("COMMENT_MODERATION_DELETE_EXTERNAL_REPLIES", "false", conv=_parse_bool)
    )
    COMMENT_MODERATION_LINK_POLICY: str = field(
        default_factory=lambda: _get_env("COMMENT_MODERATION_LINK_POLICY", "group_default", conv=str)
    )
    COMMENT_TARGET_CHAT_IDS: List[int] = field(default_factory=list)
    COMMENT_SOURCE_CHANNEL_IDS: List[int] = field(default_factory=list)
    _COMMENT_TARGET_CHAT_IDS_INVALID_TOKENS: List[str] = field(default_factory=list, init=False, repr=False)
    _COMMENT_SOURCE_CHANNEL_IDS_INVALID_TOKENS: List[str] = field(default_factory=list, init=False, repr=False)
    GROUP_DAILY_LIMIT: int = field(default_factory=lambda: _get_env("GROUP_DAILY_LIMIT", "300", conv=int))
    GROUP_AUTOREPLY_ON_TOPIC: bool = field(default_factory=lambda: _get_env("GROUP_AUTOREPLY_ON_TOPIC", "false", conv=_parse_bool))
    LIMIT_EXHAUSTED_PHRASES: List[str] = field(
        default_factory=lambda: [
            "I'm a bit tired", "I have some work", "Be right back later",
            "Can't chat now", "Busy moment", "Talk later",
            "Give me a break", "I'm occupied", "Let's pause", "Need a breather",
            "Occupied atm", "Later, please", "Catch you later", "I have errands",
            "Busy, ttyl", "Resuming soon"
        ]
    )
    #Purchase Tiers for Requests
    PURCHASE_TIERS: Dict[int, int] = field(default_factory=lambda: {50: 250, 105: 500, 220: 1000, 575: 2500})
    GIFT_TIERS: List[Dict[str, Any]] = field(
        default_factory=lambda: [
            {"code": "matcha",        "title": "Matcha Latte",        "emoji": "🍵", "price_stars": 30,   "requests": 6},
            {"code": "plushie",       "title": "Kawaii Plushie",      "emoji": "🧸", "price_stars": 60,   "requests": 12},
            {"code": "keyboard_skin", "title": "Keyboard Skin",       "emoji": "⌨️",  "price_stars": 120,  "requests": 24},
            {"code": "cat_headset",   "title": "Cat-Ear Headset",     "emoji": "🎧", "price_stars": 240,  "requests": 48},
            {"code": "game_pass",     "title": "Game Pass",           "emoji": "🎮", "price_stars": 480,  "requests": 96},
            {"code": "energy_drink",  "title": "Energy Drink Crate",  "emoji": "🥤", "price_stars": 960,  "requests": 192},
            {"code": "rgb_setup",     "title": "RGB Setup",           "emoji": "🌈", "price_stars": 1920, "requests": 384},
            {"code": "stream_throne", "title": "Streamer Throne",     "emoji": "🪑", "price_stars": 3840, "requests": 768},
        ]
    )
    PAYMENT_CURRENCY: str = field(default_factory=lambda: _get_env("PAYMENT_CURRENCY", "XTR"))
    PAYMENT_PROVIDER_TOKEN: str = field(default_factory=lambda: _get_env("PAYMENT_PROVIDER_TOKEN", ""))
    PENDING_INVOICE_TTL: int = field(default_factory=lambda: _get_env("PENDING_INVOICE_TTL", "1800", conv=int))
    SHOP_LAST_TAB_TTL: int = field(default_factory=lambda: _get_env("SHOP_LAST_TAB_TTL", str(7 * 86400), conv=int))
    PAYMENTS_TRANSIENT_NOTICE_TTL: int = field(default_factory=lambda: _get_env("PAYMENTS_TRANSIENT_NOTICE_TTL", "6", conv=int))
    BOT_QUEUE_MAX_PAYLOAD_BYTES: int = field(default_factory=lambda: _get_env("BOT_QUEUE_MAX_PAYLOAD_BYTES", "65536", conv=int))

    # ─── Group Moderation Settings ────────────────────────────────
    ENABLE_MODERATION: bool = field(default_factory=lambda: _get_env("ENABLE_MODERATION", "false", conv=_parse_bool))
    MODERATION_TIMEOUT: int = field(default_factory=lambda: _get_env("MODERATION_TIMEOUT", "30", conv=int))
    ENABLE_AI_MODERATION: bool = field(default_factory=lambda: _get_env("ENABLE_AI_MODERATION", "true", conv=_parse_bool))
    MODERATION_SANITIZE_CONTEXT_FOR_MODEL: bool = field(
        default_factory=lambda: _get_env(
            "MODERATION_SANITIZE_CONTEXT_FOR_MODEL",
            "false",
            conv=_parse_bool,
        )
    )
    MODERATOR_IDS: List[int] = field(default_factory=list)
    MODERATOR_ADMIN_CACHE_TTL_SECONDS: int = field(default_factory=lambda: _get_env("MODERATOR_ADMIN_CACHE_TTL_SECONDS", "86400", conv=int))
    MODERATOR_NOTIFICATION_CHAT_ID: int = field(default_factory=lambda: _get_env("MODERATOR_NOTIFICATION_CHAT_ID", "0", conv=int))
    MODERATION_TOXICITY_THRESHOLD: float = field(default_factory=lambda: _get_env("MODERATION_TOXICITY_THRESHOLD", "0.7", conv=float))
    MODERATION_DELETE_BLOCKED: bool = field(default_factory=lambda: _get_env("MODERATION_DELETE_BLOCKED", "true", conv=_parse_bool))
    MODERATION_ALLOWED_LINK_KEYWORDS: List[str] = field(default_factory=lambda: [x.strip() for x in _get_env("MODERATION_ALLOWED_LINK_KEYWORDS", "galaxytap,p2eglobal,a3d").split(",") if x.strip()])
    MODERATION_DELETE_TELEGRAM_LINKS: bool = field(default_factory=lambda: _get_env("MODERATION_DELETE_TELEGRAM_LINKS", "true", conv=_parse_bool))
    MODERATION_TELEGRAM_DOMAINS: List[str] = field(default_factory=lambda: [x.strip().lower() for x in _get_env("MODERATION_TELEGRAM_DOMAINS", "t.me,telegram.me,telegram.dog").split(",") if x.strip()])
    MODERATION_SPAM_LINK_THRESHOLD: int = field(default_factory=lambda: _get_env("MODERATION_SPAM_LINK_THRESHOLD", "3", conv=int))
    MODERATION_SPAM_MENTION_THRESHOLD: int = field(default_factory=lambda: _get_env("MODERATION_SPAM_MENTION_THRESHOLD", "5", conv=int))
    MOD_MENTION_RESOLVE_TIMEOUT: float = field(default_factory=lambda: _get_env("MOD_MENTION_RESOLVE_TIMEOUT", "1.5", conv=float))
    MOD_MENTION_RESOLVE_CONCURRENCY: int = field(default_factory=lambda: _get_env("MOD_MENTION_RESOLVE_CONCURRENCY", "3", conv=int))
    MOD_MENTION_RESOLVE_TTL_POS: int = field(default_factory=lambda: _get_env("MOD_MENTION_RESOLVE_TTL_POS", "3600", conv=int))
    MOD_MENTION_RESOLVE_TTL_NEG: int = field(default_factory=lambda: _get_env("MOD_MENTION_RESOLVE_TTL_NEG", "300", conv=int))
    MOD_PERIOD_SECONDS: int = field(default_factory=lambda: _get_env("MOD_PERIOD_SECONDS", "5", conv=int))
    MOD_MAX_MESSAGES: int = field(default_factory=lambda: _get_env("MOD_MAX_MESSAGES", "5", conv=int))
    MOD_ALERT_THROTTLE_SECONDS: int = field(default_factory=lambda: _get_env("MOD_ALERT_THROTTLE_SECONDS", "60", conv=int))
    SUSPICIOUS_THRESHOLD: int = field(default_factory=lambda: _get_env("SUSPICIOUS_THRESHOLD", "2", conv=int))
    SUSPICIOUS_WINDOW_SEC: int = field(default_factory=lambda: _get_env("SUSPICIOUS_WINDOW_SEC", "60", conv=int))
    # Combot-style moderation toggles
    MODERATION_ADMIN_EXEMPT: bool = field(default_factory=lambda: _get_env("MODERATION_ADMIN_EXEMPT", "true", conv=_parse_bool))
    # service messages
    MODERATION_DELETE_SERVICE_JOINS:   bool = field(default_factory=lambda: _get_env("MODERATION_DELETE_SERVICE_JOINS",   "true",  conv=_parse_bool))
    MODERATION_DELETE_SERVICE_LEAVES:  bool = field(default_factory=lambda: _get_env("MODERATION_DELETE_SERVICE_LEAVES",  "true",  conv=_parse_bool))
    MODERATION_DELETE_SERVICE_PINNED:  bool = field(default_factory=lambda: _get_env("MODERATION_DELETE_SERVICE_PINNED",  "true",  conv=_parse_bool))
    # spam / newcomers
    MODERATION_SPAM_BAN_FIRST_LINK_AFTER_JOIN: bool = field(default_factory=lambda: _get_env("MODERATION_SPAM_BAN_FIRST_LINK_AFTER_JOIN", "true", conv=_parse_bool))
    MODERATION_NEW_DELETE_LINKS_24H:   bool = field(default_factory=lambda: _get_env("MODERATION_NEW_DELETE_LINKS_24H",   "true",  conv=_parse_bool))
    MODERATION_NEW_DELETE_FORWARDS_24H:bool = field(default_factory=lambda: _get_env("MODERATION_NEW_DELETE_FORWARDS_24H","true",  conv=_parse_bool))
    MODERATION_BAN_REVOKE_MESSAGES:    bool = field(default_factory=lambda: _get_env("MODERATION_BAN_REVOKE_MESSAGES",    "true",  conv=_parse_bool))
    # filters
    MODERATION_EDITED_DELETE: bool = field(default_factory=lambda: _get_env("MODERATION_EDITED_DELETE",                   "true",  conv=_parse_bool))
    MODERATION_LINKS_DELETE_ALL:       bool = field(default_factory=lambda: _get_env("MODERATION_LINKS_DELETE_ALL",       "true",  conv=_parse_bool))
    MODERATION_COMMANDS_DELETE_ALL:    bool = field(default_factory=lambda: _get_env("MODERATION_COMMANDS_DELETE_ALL",    "true",  conv=_parse_bool))
    MODERATION_COMMAND_WHITELIST: List[str] = field(default_factory=lambda: [x.strip().lstrip("/") for x in _get_env("MODERATION_COMMAND_WHITELIST", "battle").split(",") if x.strip()])
    MODERATION_FILES_DELETE_ALL:       bool = field(default_factory=lambda: _get_env("MODERATION_FILES_DELETE_ALL",       "true",  conv=_parse_bool))
    # per-type media
    MODERATION_VOICE_DELETE:           bool = field(default_factory=lambda: _get_env("MODERATION_VOICE_DELETE",           "true",  conv=_parse_bool))
    MODERATION_VIDEO_NOTE_DELETE:      bool = field(default_factory=lambda: _get_env("MODERATION_VIDEO_NOTE_DELETE",      "true",  conv=_parse_bool))
    MODERATION_AUDIO_DELETE:           bool = field(default_factory=lambda: _get_env("MODERATION_AUDIO_DELETE",           "true",  conv=_parse_bool))
    MODERATION_IMAGES_DELETE:          bool = field(default_factory=lambda: _get_env("MODERATION_IMAGES_DELETE",          "false",  conv=_parse_bool))
    MODERATION_VIDEOS_DELETE:          bool = field(default_factory=lambda: _get_env("MODERATION_VIDEOS_DELETE",          "true",  conv=_parse_bool))
    MODERATION_GIFS_DELETE:            bool = field(default_factory=lambda: _get_env("MODERATION_GIFS_DELETE",            "false",  conv=_parse_bool))
    MODERATION_STORIES_DELETE:         bool = field(default_factory=lambda: _get_env("MODERATION_STORIES_DELETE",         "true",  conv=_parse_bool))
    # forwards / external
    MODERATION_DELETE_EXTERNAL_CHANNEL_MSGS: bool = field(default_factory=lambda: _get_env("MODERATION_DELETE_EXTERNAL_CHANNEL_MSGS", "true", conv=_parse_bool))
    MODERATION_EXTERNAL_REPLIES_DELETE: bool = field(default_factory=lambda: _get_env("MODERATION_EXTERNAL_REPLIES_DELETE", "true", conv=_parse_bool))
    MODERATION_INLINE_BOT_MSGS_DELETE: bool = field(default_factory=lambda: _get_env("MODERATION_INLINE_BOT_MSGS_DELETE", "true",  conv=_parse_bool))
    MODERATION_DELETE_BOT_CHANNEL_CHAT_FORWARDS: bool = field(default_factory=lambda: _get_env("MODERATION_DELETE_BOT_CHANNEL_CHAT_FORWARDS", "true", conv=_parse_bool))
    MODERATION_DELETE_BUTTON_MESSAGES: bool = field(default_factory=lambda: _get_env("MODERATION_DELETE_BUTTON_MESSAGES", "true", conv=_parse_bool))
    # allows
    MODERATION_ALLOW_STICKERS:         bool = field(default_factory=lambda: _get_env("MODERATION_ALLOW_STICKERS",         "true",  conv=_parse_bool))
    MODERATION_ALLOW_MENTIONS:         bool = field(default_factory=lambda: _get_env("MODERATION_ALLOW_MENTIONS",         "true",  conv=_parse_bool))
    MODERATION_ALLOW_GAMES:            bool = field(default_factory=lambda: _get_env("MODERATION_ALLOW_GAMES",            "true",  conv=_parse_bool))
    MODERATION_ALLOW_DICE:             bool = field(default_factory=lambda: _get_env("MODERATION_ALLOW_DICE",             "true",  conv=_parse_bool))
    MODERATION_ALLOW_CUSTOM_EMOJI:     bool = field(default_factory=lambda: _get_env("MODERATION_ALLOW_CUSTOM_EMOJI",     "true",  conv=_parse_bool))

    # ─── Database ────────────────────────────────────────────────
    DATABASE_URL: str = field(default_factory=lambda: _get_env("DATABASE_URL", required=True))
    DB_POOL_SIZE: int = field(default_factory=lambda: _get_env("DB_POOL_SIZE", "200", conv=int))
    DB_MAX_OVERFLOW: int = field(default_factory=lambda: _get_env("DB_MAX_OVERFLOW", "100", conv=int))
    DB_POOL_CLASS: str = field(default_factory=lambda: _get_env("DB_POOL_CLASS", "QueuePool", conv=str))
    DB_POOL_TIMEOUT: int = field(default_factory=lambda: _get_env("DB_POOL_TIMEOUT", "2", conv=int))
    DB_POOL_RECYCLE: int = field(default_factory=lambda: _get_env("DB_POOL_RECYCLE", "1800", conv=int))
    DB_POOL_USE_LIFO: bool = field(default_factory=lambda: _get_env("DB_POOL_USE_LIFO", "true", conv=_parse_bool))
    DB_APP_NAME: str = field(default_factory=lambda: _get_env("DB_APP_NAME", "", conv=str))
    DB_LOG_SLOW_MS: int = field(default_factory=lambda: _get_env("DB_LOG_SLOW_MS", "0", conv=int))
    DB_CONNECT_TIMEOUT: int = field(default_factory=lambda: _get_env("DB_CONNECT_TIMEOUT", "5", conv=int))
    DB_COMMAND_TIMEOUT: int = field(default_factory=lambda: _get_env("DB_COMMAND_TIMEOUT", "10", conv=int))
    DB_STATEMENT_TIMEOUT_MS: int = field(default_factory=lambda: _get_env("DB_STATEMENT_TIMEOUT_MS", "5000", conv=int))
    DB_LOCK_TIMEOUT_MS: int = field(default_factory=lambda: _get_env("DB_LOCK_TIMEOUT_MS", "1000", conv=int))      

    # ─── Celery ──────────────────────────────────────────────────
    CELERY_BROKER_URL: str = field(default_factory=lambda: _get_env("CELERY_BROKER_URL", ""))
    CELERY_CONCURRENCY: int = field(default_factory=lambda: _get_env("CELERY_CONCURRENCY", "10", conv=int))
    CELERY_DEFAULT_QUEUE: str = field(default_factory=lambda: _get_env("CELERY_DEFAULT_QUEUE", "celery", conv=str))
    CELERY_MEDIA_QUEUE: str = field(default_factory=lambda: _get_env("CELERY_MEDIA_QUEUE", "queue_media", conv=str))
    CELERY_MEDIA_CONCURRENCY: int = field(default_factory=lambda: _get_env("CELERY_MEDIA_CONCURRENCY", "2", conv=int))
    CELERY_MEDIA_PREFETCH: int = field(default_factory=lambda: _get_env("CELERY_MEDIA_PREFETCH", "1", conv=int))
    CELERY_MODERATION_QUEUE: str = field(default_factory=lambda: _get_env("CELERY_MODERATION_QUEUE", "queue_moderation", conv=str))
    CELERY_MODERATION_CONCURRENCY: int = field(default_factory=lambda: _get_env("CELERY_MODERATION_CONCURRENCY", "2", conv=int))
    CELERY_MODERATION_PREFETCH: int = field(default_factory=lambda: _get_env("CELERY_MODERATION_PREFETCH", "1", conv=int))
    CELERY_MODERATION_MAX_IMAGE_BYTES: int = field(default_factory=lambda: _get_env("CELERY_MODERATION_MAX_IMAGE_BYTES", str(5 * 1024 * 1024), conv=int))
    CELERY_MODERATION_MAX_PAYLOAD_BYTES: int = field(default_factory=lambda: _get_env("CELERY_MODERATION_MAX_PAYLOAD_BYTES", str(256 * 1024), conv=int))
    CELERY_RUN_TIMEOUT_SEC: float = field(default_factory=lambda: _get_env("CELERY_RUN_TIMEOUT_SEC", "120", conv=float))
    MEDIA_PREPROCESS_TIMEOUT_SEC: float = field(default_factory=lambda: _get_env("MEDIA_PREPROCESS_TIMEOUT_SEC", "20", conv=float))
    MEDIA_PREPROCESSED_TTL_SEC: int = field(default_factory=lambda: _get_env("MEDIA_PREPROCESSED_TTL_SEC", "300", conv=int))
    MEDIA_MAX_INPUT_BYTES: int = field(default_factory=lambda: _get_env("MEDIA_MAX_INPUT_BYTES", str(30 * 1024 * 1024), conv=int))

    # ─── Redis─────────────────────────
    REDIS_URL: str = field(default_factory=lambda: _get_env("REDIS_URL", required=True))
    REDIS_URL_QUEUE: str = field(default_factory=lambda: _get_env("REDIS_URL_QUEUE", required=True))
    REDIS_URL_VECTOR: str = field(default_factory=lambda: _get_env("REDIS_URL_VECTOR", required=True))
    QUEUE_LOG_LEVEL: str = field(default_factory=lambda: _get_env("QUEUE_LOG_LEVEL", "INFO"))
    REDIS_PASSWORD: str = field(default_factory=lambda: _get_env("REDIS_PASSWORD", ""))
    REDIS_USERNAME: str = field(default_factory=lambda: _get_env("REDIS_USERNAME", "default"))
    QUEUE_KEY: str = field(default_factory=lambda: _get_env("QUEUE_KEY", "queue:chat", conv=str))
    DP_USE_REDIS_STORAGE: bool = field(default_factory=lambda: _get_env("DP_USE_REDIS_STORAGE", "false", conv=_parse_bool))
    REDIS_MAX_CONNECTIONS: int = field(default_factory=lambda: _get_env("REDIS_MAX_CONNECTIONS", "2000", conv=int))
    REDIS_SOCKET_TIMEOUT: float = field(default_factory=lambda: _get_env("REDIS_SOCKET_TIMEOUT", "5.0", conv=float))
    REDIS_SOCKET_CONNECT_TIMEOUT: float = field(default_factory=lambda: _get_env("REDIS_SOCKET_CONNECT_TIMEOUT", "5.0", conv=float))
    REDIS_QUEUE_SOCKET_TIMEOUT: float = field(default_factory=lambda: _get_env("REDIS_QUEUE_SOCKET_TIMEOUT", "140", conv=float))
    # Memory / RediSearch settings 
    REDISSEARCH_KNN_K: int = field(default_factory=lambda: _get_env("REDISSEARCH_KNN_K", "24", conv=int))
    REDISSEARCH_TIMEOUT: int = field(default_factory=lambda: _get_env("REDISSEARCH_TIMEOUT", "3", conv=int))
    EMBED_INITIAL_CAP: int = field(default_factory=lambda: _get_env("EMBED_INITIAL_CAP", "20000", conv=int))
    EMBED_BLOCK_SIZE: int = field(default_factory=lambda: _get_env("EMBED_BLOCK_SIZE", "512", conv=int))
    HNSW_M: int = field(default_factory=lambda: _get_env("HNSW_M", "18", conv=int))
    HNSW_EF_CONSTRUCTION: int = field(default_factory=lambda: _get_env("HNSW_EF_CONSTRUCTION", "400", conv=int))
    HNSW_EF_RUNTIME: int = field(default_factory=lambda: _get_env("HNSW_EF_RUNTIME", "64", conv=int))
    MEMORY_MAX_ENTRIES: int = field(default_factory=lambda: _get_env("MEMORY_MAX_ENTRIES", "3000", conv=int))
    FORGET_THRESHOLD: float = field(default_factory=lambda: _get_env("FORGET_THRESHOLD", "0.35", conv=float))
    CONSOLIDATION_AGE: int = field(default_factory=lambda: _get_env("CONSOLIDATION_AGE", str(7*86400), conv=int))
    MEMORY_MAINTENANCE_INTERVAL: int = field(default_factory=lambda: _get_env("MEMORY_MAINTENANCE_INTERVAL", "600", conv=int))
    EMBED_DIM: int = field(default_factory=lambda: _get_env("EMBED_DIM", "3072", conv=int))
    DUPLICATE_DISTANCE_MAX: float = field(default_factory=lambda: _get_env("DUPLICATE_DISTANCE_MAX", "0.1", conv=float))
    MIN_MEMORY_SIMILARITY: float = field(default_factory=lambda: _get_env("MIN_MEMORY_SIMILARITY", "0.32", conv=float))
    # Per-category similarity thresholds
    MIN_MEMORY_SIMILARITY_PAST: float = field(default_factory=lambda: _get_env("MIN_MEMORY_SIMILARITY_PAST", "0.34", conv=float))
    MIN_MEMORY_SIMILARITY_PRESENT: float = field(default_factory=lambda: _get_env("MIN_MEMORY_SIMILARITY_PRESENT", "0.36", conv=float))
    MIN_MEMORY_SIMILARITY_FUTURE: float = field(default_factory=lambda: _get_env("MIN_MEMORY_SIMILARITY_FUTURE", "0.32", conv=float))
    # Salience gate for storing STM ───────────────────────────
    MEMORY_MIN_SALIENCE_TO_STORE: float = field(default_factory=lambda: _get_env("MEMORY_MIN_SALIENCE_TO_STORE", "0.10", conv=float))
    MEMORY_MIN_SALIENCE: float = field(default_factory=lambda: _get_env("MEMORY_MIN_SALIENCE", "0.08", conv=float))
    # Reinforcement weights for forgetting score
    FORGET_USE_COUNT_WEIGHT: float = field(default_factory=lambda: _get_env("FORGET_USE_COUNT_WEIGHT", "0.10", conv=float))
    FORGET_LAST_USED_WEIGHT: float = field(default_factory=lambda: _get_env("FORGET_LAST_USED_WEIGHT", "0.10", conv=float))
    FORGET_LAST_USED_TAU: int = field(default_factory=lambda: _get_env("FORGET_LAST_USED_TAU", str(7*86400), conv=int))
    # Hybrid memory / thresholds
    VEC_SALIENCE_MIN: float = field(default_factory=lambda: _get_env("VEC_SALIENCE_MIN", "0.0", conv=float))
    HYBRID_TOPK_TXT: int = field(default_factory=lambda: _get_env("HYBRID_TOPK_TXT", "2", conv=int))
    MEMTXT_MAX_PER_UID: int = field(default_factory=lambda: _get_env("MEMTXT_MAX_PER_UID", "150", conv=int))
    MEMTXT_BM25_CANDIDATES: int = field(default_factory=lambda: _get_env("MEMTXT_BM25_CANDIDATES", "40", conv=int))
    # LTVM (Long-Term Vector Memory) 
    LTM_COOLDOWN_SECS: int = field(default_factory=lambda: _get_env("LTM_COOLDOWN_SECS", "0", conv=int))
    LTM_COOLDOWN_TURNS: int = field(default_factory=lambda: _get_env("LTM_COOLDOWN_TURNS", "0", conv=int))
    LTM_MAX_PER_PROMPT: int = field(default_factory=lambda: _get_env("LTM_MAX_PER_PROMPT", "1", conv=int))
    LTM_MIN_SIM: float = field(default_factory=lambda: _get_env("LTM_MIN_SIM", "0.57", conv=float))
    FACTS_INITIAL_CAP: int = field(default_factory=lambda: _get_env("FACTS_INITIAL_CAP", "4096", conv=int))
    PLANS_INITIAL_CAP: int = field(default_factory=lambda: _get_env("PLANS_INITIAL_CAP", "2048", conv=int))
    BOUNDS_INITIAL_CAP: int = field(default_factory=lambda: _get_env("BOUNDS_INITIAL_CAP", "1024", conv=int))
    # Memory & Caching
    SHORT_MEMORY_LIMIT: int = field(default_factory=lambda: _get_env("SHORT_MEMORY_LIMIT", "300", conv=int))
    MEMORY_TTL_DAYS: int = field(default_factory=lambda: _get_env("MEMORY_TTL_DAYS", "30", conv=int))
    CONTEXT_TOKEN_BUDGET: int = field(default_factory=lambda: _get_env("CONTEXT_TOKEN_BUDGET", "9000", conv=int))
    RESPONSES_TOKEN_RESERVE: int = field(default_factory=lambda: _get_env("RESPONSES_TOKEN_RESERVE", "1024", conv=int))
    ANCHOR_TURNS: int = field(default_factory=lambda: _get_env("ANCHOR_TURNS", "24", conv=int))
    PAIRED_ANCHORS: bool = field(default_factory=lambda: _get_env("PAIRED_ANCHORS", "true", conv=_parse_bool))
    ANCHOR_TURN_PAIRS: int = field(default_factory=lambda: _get_env("ANCHOR_TURN_PAIRS", "6", conv=int))
    # BG worker batching
    BG_BATCH_MAX: int = field(default_factory=lambda: _get_env("BG_BATCH_MAX", "32", conv=int))
    BG_BATCH_WAIT_MS: int = field(default_factory=lambda: _get_env("BG_BATCH_WAIT_MS", "35", conv=int))
    BG_WORKER_CONCURRENCY: int = field(default_factory=lambda: _get_env("BG_WORKER_CONCURRENCY", "16", conv=int))
    USER_LOCK_MAX: int = field(default_factory=lambda: _get_env("USER_LOCK_MAX", "5000", conv=int))
    # Event frame extraction
    EVENT_FRAME_ENABLED: bool = field(default_factory=lambda: _get_env("EVENT_FRAME_ENABLED", "1", conv=_parse_bool))
    EVENT_FRAME_MIN_SALIENCE: float = field(default_factory=lambda: _get_env("EVENT_FRAME_MIN_SALIENCE", "0.7", conv=float))
    EVENT_FRAME_MAX_CONCURRENCY: int = field(default_factory=lambda: _get_env("EVENT_FRAME_MAX_CONCURRENCY", "2", conv=int))
    # Topic extraction guard
    TOPIC_MIN_SALIENCE: float = field(default_factory=lambda: _get_env("TOPIC_MIN_SALIENCE", "0.4", conv=float))
    TOPIC_MIN_LEN: int = field(default_factory=lambda: _get_env("TOPIC_MIN_LEN", "60", conv=int))

    # ─── Layered Context Memory (STM/MTM/LTM) ─────────────────────────
    LAYERED_MEMORY_ENABLED: bool = field(default_factory=lambda: _get_env("LAYERED_MEMORY_ENABLED", "true", conv=_parse_bool))
    MEMORY_TTL_DAYS_STM_MTM: int = field(default_factory=lambda: _get_env("MEMORY_TTL_DAYS_STM_MTM", "30", conv=int))
    MEMORY_TTL_DAYS_LTM: int = field(default_factory=lambda: _get_env("MEMORY_TTL_DAYS_LTM", "365", conv=int))
    RERANK_ITEM_CHAR_LIMIT: int = field(default_factory=lambda: _get_env("RERANK_ITEM_CHAR_LIMIT", "400", conv=int))
    LLM_TIMEOUT: float = field(default_factory=lambda: _get_env("LLM_TIMEOUT", "10.0", conv=float))
    MEMORY_PARALLEL_TOKENS_PER_REQUEST: int = field(default_factory=lambda: _get_env("MEMORY_PARALLEL_TOKENS_PER_REQUEST", "9000", conv=int))
    MEMORY_PARALLEL_MAX_REQUESTS: int = field(default_factory=lambda: _get_env("MEMORY_PARALLEL_MAX_REQUESTS", "8", conv=int))
    MEMORY_HINTS_MAX: int = field(default_factory=lambda: _get_env("MEMORY_HINTS_MAX", "6", conv=int))
    SNIPPETS_MAX_TOKENS_GROUP: int = field(default_factory=lambda: _get_env("SNIPPETS_MAX_TOKENS_GROUP", "200", conv=int))
    #STM (Short-Term Context Memory)
    STM_TOKEN_BUDGET: int = field(default_factory=lambda: _get_env("STM_TOKEN_BUDGET", "5000", conv=int))
    STM_PAIR_LIMIT_PRIVATE: int = field(default_factory=lambda: _get_env("STM_PAIR_LIMIT_PRIVATE", "32", conv=int))
    STM_PAIR_LIMIT_GROUP: int = field(default_factory=lambda: _get_env("STM_PAIR_LIMIT_GROUP", "10", conv=int))
    STM_PAIR_ALIGN: bool = field(default_factory=lambda: _get_env("STM_PAIR_ALIGN", "true", conv=_parse_bool))
    STM_MIN_KEEP_PAIRS: int = field(default_factory=lambda: _get_env("STM_MIN_KEEP_PAIRS", "2", conv=int))
    STM_TRIM_RATIO: float = field(default_factory=lambda: _get_env("STM_TRIM_RATIO", "0.30", conv=float))
    STM_PROMOTE_GUARD_EX: int = field(default_factory=lambda: _get_env("STM_PROMOTE_GUARD_EX", "5", conv=int))
    GROUP_STM_TOKEN_BUDGET: int = field(default_factory=lambda: _get_env("GROUP_STM_TOKEN_BUDGET", "4000", conv=int))
    GROUP_STM_TRANSCRIPT_ENABLED: bool = field(default_factory=lambda: _get_env("GROUP_STM_TRANSCRIPT_ENABLED", "false", conv=_parse_bool))
    GROUP_RECENT_TOKENS_BUDGET: int = field(default_factory=lambda: _get_env("GROUP_RECENT_TOKENS_BUDGET", "12000", conv=int))
    GROUP_STM_TAIL_TOKENS: int = field(default_factory=lambda: _get_env("GROUP_STM_TAIL_TOKENS", "8000", conv=int))
    GROUP_STM_TAIL_MAX_LINES: int = field(default_factory=lambda: _get_env("GROUP_STM_TAIL_MAX_LINES", "60", conv=int))
    #MTM (Mid-Term Context Memory)
    MTM_BUDGET_TOKENS_PRIVATE: int = field(default_factory=lambda: _get_env("MTM_BUDGET_TOKENS_PRIVATE", "50000", conv=int))
    MTM_BUDGET_TOKENS_GROUP: int = field(default_factory=lambda: _get_env("MTM_BUDGET_TOKENS_GROUP", "14000", conv=int))
    MTM_TRIM_CHUNK_TOKENS_PRIVATE: int = field(default_factory=lambda: _get_env("MTM_TRIM_CHUNK_TOKENS_PRIVATE", "8000", conv=int))
    MTM_TRIM_CHUNK_TOKENS_GROUP: int = field(default_factory=lambda: _get_env("MTM_TRIM_CHUNK_TOKENS_GROUP", "3000", conv=int))
    MTM_TOPIC_CACHE_TTL_SEC: int = field(default_factory=lambda: _get_env("MTM_TOPIC_CACHE_TTL_SEC", "30", conv=int))
    MTM_STAGE1_TOPN: int = field(default_factory=lambda: _get_env("MTM_STAGE1_TOPN", "25", conv=int))
    MTM_STAGE2_TOPN: int = field(default_factory=lambda: _get_env("MTM_STAGE2_TOPN", "4", conv=int))
    MTM_RECENT_TAIL_TOKENS: int = field(default_factory=lambda: _get_env("MTM_RECENT_TAIL_TOKENS", "30000", conv=int))
    MTM_SNIPPETS_MAX_TOKENS: int = field(default_factory=lambda: _get_env("MTM_SNIPPETS_MAX_TOKENS", "500", conv=int))
    MTM_PYRAMID_ENABLED: bool = field(default_factory=lambda: _get_env("MTM_PYRAMID_ENABLED", "false", conv=_parse_bool))
    MTM_PYRAMID_TRIGGER_TOKENS: int = field(default_factory=lambda: _get_env("MTM_PYRAMID_TRIGGER_TOKENS", "20000", conv=int))
    MTM_BATCH_TOKENS: int = field(default_factory=lambda: _get_env("MTM_BATCH_TOKENS", "10000", conv=int))
    MTM_PARALLEL: int = field(default_factory=lambda: _get_env("MTM_PARALLEL", "6", conv=int))
    MTM_PARALLEL_JITTER_MS: int = field(default_factory=lambda: _get_env("MTM_PARALLEL_JITTER_MS", "50", conv=int))
    MTM_STAGE1_MAX_TOKENS: int = field(default_factory=lambda: _get_env("MTM_STAGE1_MAX_TOKENS", "400", conv=int))
    MTM_MAX_BATCHES: int = field(default_factory=lambda: _get_env("MTM_MAX_BATCHES", "64", conv=int))
    MTM_LLM_BATCH: int = field(default_factory=lambda: _get_env("MTM_LLM_BATCH", "20", conv=int))
    MTM_RECENT_WINDOW_TOKENS: int = field(default_factory=lambda: _get_env("MTM_RECENT_WINDOW_TOKENS", "35000", conv=int))
    MTM_COMPOSE_TIMEOUT_SEC: float = field(default_factory=lambda: _get_env("MTM_COMPOSE_TIMEOUT_SEC", "30.0", conv=float))
    MTM_S1_CAND_MAX: int = field(default_factory=lambda: _get_env("MTM_S1_CAND_MAX", "40", conv=int))
    MTM_S1_RECENT_TAKE: int = field(default_factory=lambda: _get_env("MTM_S1_RECENT_TAKE", "24", conv=int))
    #LTM (Long-Term Context Memory)
    LTM_MAX_TOKENS_PRIVATE: int = field(default_factory=lambda: _get_env("LTM_MAX_TOKENS_PRIVATE", "9000", conv=int))
    LTM_MAX_TOKENS_GROUP: int = field(default_factory=lambda: _get_env("LTM_MAX_TOKENS_GROUP", "2500", conv=int))
    LTM_STAGE1_TOPN: int = field(default_factory=lambda: _get_env("LTM_STAGE1_TOPN", "25", conv=int))
    LTM_STAGE2_TOPN: int = field(default_factory=lambda: _get_env("LTM_STAGE2_TOPN", "6", conv=int))
    LTM_SUMMARY_PARTIAL_TRIGGER_TOKENS: int = field(default_factory=lambda: _get_env("LTM_SUMMARY_PARTIAL_TRIGGER_TOKENS", "12000", conv=int))
    LTM_SNIPPETS_MAX_TOKENS: int = field(default_factory=lambda: _get_env("LTM_SNIPPETS_MAX_TOKENS", "300", conv=int))
    LTM_COMPOSE_TIMEOUT_SEC: float = field(default_factory=lambda: _get_env("LTM_COMPOSE_TIMEOUT_SEC", "30.0", conv=float))
    LTM_LLM_ITEM_CHAR_LIMIT: int = field(default_factory=lambda: _get_env("LTM_LLM_ITEM_CHAR_LIMIT", "500", conv=int))
    LTM_LLM_POOL_MAX: int = field(default_factory=lambda: _get_env("LTM_LLM_POOL_MAX", "50", conv=int))
    LTM_LLM_TOPN: int = field(default_factory=lambda: _get_env("LTM_LLM_TOPN", "10", conv=int))
    LTM_LLM_BATCH: int = field(default_factory=lambda: _get_env("LTM_LLM_BATCH", "30", conv=int))
    LTM_ROLLUP_GUARD_EX_SEC: int = field(default_factory=lambda: _get_env("LTM_ROLLUP_GUARD_EX_SEC", "240", conv=int))
    LTM_SUMMARY_MAX_OUTPUT_TOKENS: int = field(default_factory=lambda: _get_env("LTM_SUMMARY_MAX_OUTPUT_TOKENS", "4096", conv=int))

    # ─── Persona ─────────────────────────────────
    PERSONA_NAME: str = field(default_factory=lambda: _get_env("PERSONA_NAME", "Kira"))
    PERSONA_AGE: int = field(default_factory=lambda: _get_env("PERSONA_AGE", "29", conv=int))
    PERSONA_GENDER: str = field(default_factory=lambda: _get_env("PERSONA_GENDER", "female"))
    PERSONA_ZODIAC: str = field(default_factory=lambda: _get_env("PERSONA_ZODIAC", "Scorpio"))
    PERSONA_TEMPERAMENT: str = field(default_factory=lambda: _get_env(
            "PERSONA_TEMPERAMENT",
            '{"sanguine":0.45,"choleric":0.12,"phlegmatic":0.25,"melancholic":0.18}',
        )
    )
    PERSONA_ROLE: str = field(default_factory=lambda: _get_env(
            "PERSONA_ROLE",
            PERSONA_ROLE_DEFAULT_PROMPT,
        )
    )
    PERSONA_ARCHETYPES: str = field(default_factory=lambda: _get_env(
        "PERSONA_ARCHETYPES",
        '["Rebel","Jester","Sage"]',
    ))
    PERSONA_WIZARD_TTL_SEC: int = field(default_factory=lambda: _get_env("PERSONA_WIZARD_TTL_SEC", "604800", conv=int))
    
    #Initial baseline for all metrics
    EMO_INITIAL_CENTER: float = field(default_factory=lambda: _get_env("EMO_INITIAL_CENTER", "0.5", conv=float))
    VALENCE_BASELINE: float = field(default_factory=lambda: _get_env("VALENCE_BASELINE", "0.1", conv=float))
    MOOD_NEUTRAL_RADIUS: float = field(default_factory=lambda: _get_env("MOOD_NEUTRAL_RADIUS", "0.1", conv=float))
    #Exponential smoothing for persona state updates
    STATE_EMA_ALPHA: float = field(default_factory=lambda: _get_env("STATE_EMA_ALPHA", "0.26", conv=float))
    STATE_EMA_MAX_ALPHA: float = field(default_factory=lambda: _get_env("STATE_EMA_MAX_ALPHA", "0.60", conv=float))
    MEMORYFOLLOWUP_SIM_THRESHOLD: float = field(default_factory=lambda: _get_env("MEMORYFOLLOWUP_SIM_THRESHOLD", "0.45", conv=float))
    BG_QUEUE_MAX: int = field(default_factory=lambda: _get_env("BG_QUEUE_MAX", "2000", conv=int))
    #Behavioural thresholds
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
    AROUSAL_HOMEOSTASIS_DECAY: float = field(default_factory=lambda: _get_env("AROUSAL_HOMEOSTASIS_DECAY", "0.97", conv=float))
    ENERGY_HOMEOSTASIS_DECAY: float  = field(default_factory=lambda: _get_env("ENERGY_HOMEOSTASIS_DECAY",  "0.98", conv=float))
    CIRCADIAN_AMPLITUDE: float = field(default_factory=lambda: _get_env("CIRCADIAN_AMPLITUDE", "0.1", conv=float))
    FATIGUE_ACCUMULATE_RATE: float = field(default_factory=lambda: _get_env("FATIGUE_ACCUMULATE_RATE", "0.0026", conv=float))
    FATIGUE_RECOVERY_RATE: float = field(default_factory=lambda: _get_env("FATIGUE_RECOVERY_RATE", "0.982", conv=float))
    FATIGUE_AROUSAL_THRESHOLD: float = field(default_factory=lambda: _get_env("FATIGUE_AROUSAL_THRESHOLD", "0.56", conv=float))
    FATIGUE_ENERGY_THRESHOLD: float = field(default_factory=lambda: _get_env("FATIGUE_ENERGY_THRESHOLD", "0.56", conv=float))
    #Emotional attachment
    ATTACHMENT_INIT: float = field(default_factory=lambda: _get_env("ATTACHMENT_INIT", "0.10", conv=float))
    ATTACHMENT_BASELINE: float = field(default_factory=lambda: _get_env("ATTACHMENT_BASELINE", "0.10", conv=float))
    ATTACHMENT_POS_RATE: float = field(default_factory=lambda: _get_env("ATTACHMENT_POS_RATE", "0.01", conv=float))
    ATTACHMENT_NEG_RATE: float = field(default_factory=lambda: _get_env("ATTACHMENT_NEG_RATE", "0.019", conv=float))
    ATTACHMENT_POS_EXP: float = field(default_factory=lambda: _get_env("ATTACHMENT_POS_EXP", "1.08", conv=float))
    ATTACHMENT_NEG_EXP: float = field(default_factory=lambda: _get_env("ATTACHMENT_NEG_EXP", "1.18", conv=float))
    ATTACHMENT_NEUTRAL_LEAK: float = field(default_factory=lambda: _get_env("ATTACHMENT_NEUTRAL_LEAK", "0.0018", conv=float))
    ATTACHMENT_NEG_BIAS_NEUTRAL: float = field(default_factory=lambda: _get_env("ATTACHMENT_NEG_BIAS_NEUTRAL", "0.22", conv=float))
    ATTACHMENT_IDLE_HALFLIFE: float = field(default_factory=lambda: _get_env("ATTACHMENT_IDLE_HALFLIFE", "1814400", conv=float))
    ATTACHMENT_RUPTURE_REPAIR: float = field(default_factory=lambda: _get_env("ATTACHMENT_RUPTURE_REPAIR", "0.030", conv=float))
    ATTACHMENT_RUPTURE_SALIENCE: float = field(default_factory=lambda: _get_env("ATTACHMENT_RUPTURE_SALIENCE", "0.85", conv=float))
    ATTACHMENT_RUPTURE_VALENCE: float = field(default_factory=lambda: _get_env("ATTACHMENT_RUPTURE_VALENCE", "0.60", conv=float))
    ATTACHMENT_PERSIST: bool = field(
        default_factory=lambda: _get_env(
            "ATTACHMENT_PERSIST", "true",
            conv=_parse_bool
        )
    )
    ATTACHMENT_PERSIST_MIN_PERIOD: float = field(default_factory=lambda: _get_env("ATTACHMENT_PERSIST_MIN_PERIOD", "15", conv=float))
    ATTACHMENT_PERSIST_MIN_DELTA:  float = field(default_factory=lambda: _get_env("ATTACHMENT_PERSIST_MIN_DELTA",  "0.01", conv=float))
    ATTACHMENT_STAGE_HYST: float = field(default_factory=lambda: _get_env("ATTACHMENT_STAGE_HYST", "0.01", conv=float))
    ATTACHMENT_MAX_USERS: int = field(default_factory=lambda: _get_env("ATTACHMENT_MAX_USERS", "20000", conv=int))
    ATTACHMENT_TIME_TAU: float = field(default_factory=lambda: _get_env("ATTACHMENT_TIME_TAU", "240.0", conv=float))
    ATTACHMENT_VALENCE_EPS: float = field(default_factory=lambda: _get_env("ATTACHMENT_VALENCE_EPS", "0.07", conv=float))
    ATTACHMENT_MAX_STEP: float = field(default_factory=lambda: _get_env("ATTACHMENT_MAX_STEP", "0.015", conv=float))
    ATTACHMENT_RUPTURE_COOLDOWN: int = field(default_factory=lambda: _get_env("ATTACHMENT_RUPTURE_COOLDOWN", "3600", conv=int))
    ATTACHMENT_RUPTURE_DROP: float = field(default_factory=lambda: _get_env("ATTACHMENT_RUPTURE_DROP", "0.20", conv=float))
    ATTACHMENT_VEL_BETA: float = field(default_factory=lambda: _get_env("ATTACHMENT_VEL_BETA", "0.3", conv=float))
    ATTACHMENT_POS_CAP_PER_HOUR: float = field(default_factory=lambda: _get_env("ATTACHMENT_POS_CAP_PER_HOUR", "0.06", conv=float))
    ATTACHMENT_POS_ACCUM_TAU: float = field(default_factory=lambda: _get_env("ATTACHMENT_POS_ACCUM_TAU", "3600", conv=float))
    ATTACHMENT_WEIGHT_GAIN: float    = field(default_factory=lambda: _get_env("ATTACHMENT_WEIGHT_GAIN",    "1.0",  conv=float))
    ATTACHMENT_BEHAVIOR_GAIN: float  = field(default_factory=lambda: _get_env("ATTACHMENT_BEHAVIOR_GAIN",  "1.0",  conv=float))

    # ─── Responder: On/Off-topic knowledge files ────────────────────────────────
    KNOWLEDGE_ON_FILE: str = field(default_factory=lambda: _get_env("KNOWLEDGE_ON_FILE", "knowledge_on.json", conv=str))
    #Hybrid Fallback Parameters
    HYBRID_FALLBACK_THRESHOLD: float = field(default_factory=lambda: _get_env("HYBRID_FALLBACK_THRESHOLD", "0.35", conv=float))
    RELEVANCE_THRESHOLD: float = field(default_factory=lambda: _get_env("RELEVANCE_THRESHOLD", "0.28", conv=float))
    KEYWORD_RELEVANCE_THRESHOLD: float = field(default_factory=lambda: _get_env("KEYWORD_RELEVANCE_THRESHOLD", "0.28", conv=float))
    RELEVANCE_MARGIN: float = field(default_factory=lambda: _get_env("RELEVANCE_MARGIN", "0.05", conv=float))
    RELEVANCE_GAP_MIN: float = field(default_factory=lambda: _get_env("RELEVANCE_GAP_MIN", "0.05", conv=float))
    MMR_LAMBDA: float = field(default_factory=lambda: _get_env("MMR_LAMBDA", "0.2", conv=float))
    KNOWLEDGE_TOP_K: int = field(default_factory=lambda: _get_env("KNOWLEDGE_TOP_K", "3", conv=int))

    # ─── SCHEDULER ────────────────────────────────────────────────
    SCHED_ENABLE_KB_GC: bool = field(default_factory=lambda: _get_env("SCHED_ENABLE_KB_GC", "true", conv=_parse_bool))
    SCHED_ENABLE_TWEETS: bool = field(default_factory=lambda: _get_env("SCHED_ENABLE_TWEETS", "true", conv=_parse_bool))
    SCHED_ENABLE_PRICES: bool = field(default_factory=lambda: _get_env("SCHED_ENABLE_PRICES", "true", conv=_parse_bool))
    SCHED_ENABLE_GROUP_PING: bool = field(default_factory=lambda: _get_env("SCHED_ENABLE_GROUP_PING", "true", conv=_parse_bool))
    SCHED_ENABLE_PERSONAL_PING: bool = field(default_factory=lambda: _get_env("SCHED_ENABLE_PERSONAL_PING", "true", conv=_parse_bool))
    SCHED_ENABLE_BATTLE: bool = field(default_factory=lambda: _get_env("SCHED_ENABLE_BATTLE", "true", conv=_parse_bool))
    SCHED_ENABLE_ANALYTICS: bool = field(default_factory=lambda: _get_env("SCHED_ENABLE_ANALYTICS", "false", conv=_parse_bool))
    SCHEDULER_MISFIRE_GRACE_TIME: int = field(default_factory=lambda: _get_env("SCHEDULER_MISFIRE_GRACE_TIME", "300", conv=int))
    #Group Ping Settings
    GROUP_PING_INTERVAL_MINUTES: int = field(default_factory=lambda: _get_env("GROUP_PING_INTERVAL_MINUTES", "60", conv=int))
    GROUP_PING_HISTORY_COUNT: int = field(default_factory=lambda: _get_env("GROUP_PING_HISTORY_COUNT", "10", conv=int))
    GROUP_PING_IDLE_THRESHOLD_SECONDS: int = field(default_factory=lambda: _get_env("GROUP_PING_IDLE_THRESHOLD_SECONDS", "1200", conv=int))
    GROUP_PING_ADAPTIVE_IDLE_MULTIPLIER: float = field(default_factory=lambda: _get_env("GROUP_PING_ADAPTIVE_IDLE_MULTIPLIER", "1.5", conv=float))
    GROUP_PING_ACTIVE_RECENT_SECONDS: int = field(default_factory=lambda: _get_env("GROUP_PING_ACTIVE_RECENT_SECONDS", "1800", conv=int))
    GROUP_PING_ACTIVE_TTL_SECONDS: int = field(default_factory=lambda: _get_env("GROUP_PING_ACTIVE_TTL_SECONDS", "86400", conv=int))
    GROUP_PING_USER_COOLDOWN_SECONDS: int = field(default_factory=lambda: _get_env("GROUP_PING_USER_COOLDOWN_SECONDS", "14400", conv=int))
    GROUP_PING_MAX_VALENCE: float = field(default_factory=lambda: _get_env("GROUP_PING_MAX_VALENCE", "0.85", conv=float))
    GROUP_PING_MAX_AROUSAL: float = field(default_factory=lambda: _get_env("GROUP_PING_MAX_AROUSAL", "0.85", conv=float))
    GROUP_PING_SCAN_LIMIT: int = field(default_factory=lambda: _get_env("GROUP_PING_SCAN_LIMIT", "100", conv=int))
    GROUP_PING_LOCK_TTL_SEC: int = field(default_factory=lambda: _get_env("GROUP_PING_LOCK_TTL_SEC", "20", conv=int))
    #Personal Ping Settings
    PERSONAL_PING_INTERVAL_SEC: int = field(default_factory=lambda: _get_env("PERSONAL_PING_INTERVAL_SEC", "300", conv=int))
    PERSONAL_PING_HISTORY_COUNT: int = field(default_factory=lambda: _get_env("PERSONAL_PING_HISTORY_COUNT", "10", conv=int))
    PERSONAL_PING_IDLE_THRESHOLD_SECONDS: int = field(default_factory=lambda: _get_env("PERSONAL_PING_IDLE_THRESHOLD_SECONDS", "12345", conv=int))
    PERSONAL_PING_ADAPTIVE_MULTIPLIER: float = field(default_factory=lambda: _get_env("PERSONAL_PING_ADAPTIVE_MULTIPLIER", "1.2", conv=float))
    PERSONAL_PING_RETENTION_SECONDS: int = field(default_factory=lambda: _get_env("PERSONAL_PING_RETENTION_SECONDS", "604800", conv=int))
    PERSONAL_PING_BATCH_SIZE: int = field(default_factory=lambda: _get_env("PERSONAL_PING_BATCH_SIZE", "100", conv=int))
    PERSONAL_PING_MIN_BOREDOM: float = field(default_factory=lambda: _get_env("PERSONAL_PING_MIN_BOREDOM", "0.5", conv=float))
    PERSONAL_PING_BIORHYTHM_WEIGHT: float = field(default_factory=lambda: _get_env("PERSONAL_PING_BIORHYTHM_WEIGHT", "0.4", conv=float))
    PERSONAL_PING_START_HOUR: int = field(default_factory=lambda: _get_env("PERSONAL_PING_START_HOUR", "9", conv=int))
    PERSONAL_PING_END_HOUR: int = field(default_factory=lambda: _get_env("PERSONAL_PING_END_HOUR", "21", conv=int))
    PM_SEND_CONCURRENCY: int = field(default_factory=lambda: _get_env("PM_SEND_CONCURRENCY", "8", conv=int))
    PERSONAL_PING_MAX_CONSECUTIVE: int = field(default_factory=lambda: _get_env("PERSONAL_PING_MAX_CONSECUTIVE", "3", conv=int))
    PERSONAL_PING_BACKOFF_MULT: float = field(default_factory=lambda: _get_env("PERSONAL_PING_BACKOFF_MULT", "3.0", conv=float))
    PERSONAL_PING_BACKOFF_MAX_HOURS: int = field(default_factory=lambda: _get_env("PERSONAL_PING_BACKOFF_MAX_HOURS", "48", conv=int))
    PERSONAL_PING_BACKOFF_JITTER_PCT: float = field(default_factory=lambda: _get_env("PERSONAL_PING_BACKOFF_JITTER_PCT", "0.10", conv=float))
    PERSONAL_PING_ALLOW_GENERIC_WHEN_BORED: bool = field(default_factory=lambda: _get_env("PERSONAL_PING_ALLOW_GENERIC_WHEN_BORED", "true", conv=_parse_bool))
    PERSONAL_PING_GENERIC_HELLO_PROB: float = field(default_factory=lambda: _get_env("PERSONAL_PING_GENERIC_HELLO_PROB", "0.25", conv=float))
    PERSONAL_PING_GENERIC_HELLO_ASK_PROB: float = field(default_factory=lambda: _get_env("PERSONAL_PING_GENERIC_HELLO_ASK_PROB", "0.5", conv=float))
    #New User Greeting Settings
    NEW_USER_TTL_SECONDS: int = field(default_factory=lambda: _get_env("NEW_USER_TTL_SECONDS", "86400", conv=int))
    GREETING_RATE_LIMIT: int = field(default_factory=lambda: _get_env("GREETING_RATE_LIMIT", "1", conv=int))
    GREETING_RATE_WINDOW_SECONDS: int = field(default_factory=lambda: _get_env("GREETING_RATE_WINDOW_SECONDS", "10", conv=int))
    #Emoji Ping Configuration
    EMOJI_PING_LIST: List[str] = field(default_factory=lambda: ["👀", "🫣", "✌️", "🫦", "🐝", "🌚"])
    EMOJI_PING_PROBABILITY: float = field(default_factory=lambda: _get_env("EMOJI_PING_PROBABILITY", "0.25", conv=float))
    EMOJI_APPEND_PROBABILITY: float = field(default_factory=lambda: _get_env("EMOJI_APPEND_PROBABILITY", "0.1", conv=float))
    #Twitter API Credentials
    TWITTER_API_KEY: str = field(default_factory=lambda: _get_env("TWITTER_API_KEY", "", conv=str))
    TWITTER_API_SECRET: str = field(default_factory=lambda: _get_env("TWITTER_API_SECRET", "", conv=str))
    TWITTER_ACCESS_TOKEN: str = field(default_factory=lambda: _get_env("TWITTER_ACCESS_TOKEN", "", conv=str))
    TWITTER_ACCESS_TOKEN_SECRET: str = field(default_factory=lambda: _get_env("TWITTER_ACCESS_TOKEN_SECRET", "", conv=str))
    TWITTER_BEARER_TOKEN: str = field(default_factory=lambda: _get_env("TWITTER_BEARER_TOKEN", "", conv=str))
    TWITTER_PERSONA_CHAT_ID: int = field(default_factory=lambda: _get_env("TWITTER_PERSONA_CHAT_ID", "0", conv=int))
    TWITTER_FALLBACK_TWEETS: List[str] = field(default_factory=lambda: [
    "Stay tuned for more insights!",
    "More fresh updates coming soon 🚀",
    "Catch you later with fresh updates!"
    ])

    # ─── ElevenLabs / TTS ─────────────────────────────────
    TTS_ENABLED: bool = field(default_factory=lambda: _get_env("TTS_ENABLED", "true", conv=_parse_bool))
    TTS_PROBABILITY_TEXT: float = field(default_factory=lambda: _get_env("TTS_PROBABILITY_TEXT", "0.07", conv=float))
    TTS_PROBABILITY_VOICEIN: float = field(default_factory=lambda: _get_env("TTS_PROBABILITY_VOICEIN", "0.33", conv=float))
    PERSONAL_PING_TTS_ENABLED: bool = field(default_factory=lambda: _get_env("PERSONAL_PING_TTS_ENABLED", "false", conv=_parse_bool))
    PERSONAL_PING_TTS_PROBABILITY: float = field(default_factory=lambda: _get_env("PERSONAL_PING_TTS_PROBABILITY", "0.66", conv=float))
    PERSONAL_PING_TTS_START_HOUR: int = field(default_factory=lambda: _get_env("PERSONAL_PING_TTS_START_HOUR", "10", conv=int))
    PERSONAL_PING_TTS_END_HOUR: int = field(default_factory=lambda: _get_env("PERSONAL_PING_TTS_END_HOUR", "21", conv=int))
    PERSONAL_PING_TTS_CB_FAILS: int = field(default_factory=lambda: _get_env("PERSONAL_PING_TTS_CB_FAILS", "3", conv=int))
    PERSONAL_PING_TTS_CB_COOLDOWN_SEC: int = field(default_factory=lambda: _get_env("PERSONAL_PING_TTS_CB_COOLDOWN_SEC", "3600", conv=int))
    PERSONAL_PING_TTS_CAPTION_ENABLED: bool = field(default_factory=lambda: _get_env("PERSONAL_PING_TTS_CAPTION_ENABLED", "false", conv=_parse_bool))
    PERSONAL_PING_TTS_CAPTION_LEN: int = field(default_factory=lambda: _get_env("PERSONAL_PING_TTS_CAPTION_LEN", "160", conv=int))
    ELEVENLABS_API_KEY: str = field(default_factory=lambda: _get_env("ELEVENLABS_API_KEY", "", conv=str))
    ELEVENLABS_MODEL_ID: str = field(default_factory=lambda: _get_env("ELEVENLABS_MODEL_ID", "eleven_multilingual_v2", conv=str))
    ELEVENLABS_TIMEOUT: int = field(default_factory=lambda: _get_env("ELEVENLABS_TIMEOUT", "20", conv=int))
    ELEVENLABS_DEFAULT_VOICE_ID: str = field(default_factory=lambda: _get_env("ELEVENLABS_DEFAULT_VOICE_ID", "", conv=str))
    ELEVENLABS_VOICE_MAP: str = field(default_factory=lambda: _get_env("ELEVENLABS_VOICE_MAP", "{}", conv=str))  # JSON: {"en":"...","ru":"..."}
    ELEVENLABS_MAX_TTS_CHARS: int = field(default_factory=lambda: _get_env("ELEVENLABS_MAX_TTS_CHARS", "900", conv=int))
    TTS_SKIP_BACKLOG: int = field(default_factory=lambda: _get_env("TTS_SKIP_BACKLOG", "100", conv=int))
    ELEVENLABS_OUTPUT_FORMAT: str = field(default_factory=lambda: _get_env("ELEVENLABS_OUTPUT_FORMAT", "ogg_48000", conv=str))
    ELEVENLABS_TTS_NORMALIZATION: str = field(default_factory=lambda: _get_env("ELEVENLABS_TTS_NORMALIZATION", "on", conv=str)) # auto|on|off (text normalization)
    ELEVENLABS_SEED: T = field(default_factory=lambda: _get_env("ELEVENLABS_SEED", None, conv=str))
    # SSML / Prosody controls
    ELEVENLABS_ENABLE_SSML_BREAKS: bool = field(default_factory=lambda: _get_env("ELEVENLABS_ENABLE_SSML_BREAKS", "true", conv=_parse_bool))
    ELEVENLABS_SSML_MODE: str = field(default_factory=lambda: _get_env("ELEVENLABS_SSML_MODE", "off", conv=str))
    SSML_BREAK_MAX_MID_MS: int = field(default_factory=lambda: _get_env("SSML_BREAK_MAX_MID_MS", "1200", conv=int))
    SSML_BREAK_MAX_ANY_MS: int  = field(default_factory=lambda: _get_env("SSML_BREAK_MAX_ANY_MS",  "3000", conv=int))
    SSML_BREAKS_PER_100CH: int  = field(default_factory=lambda: _get_env("SSML_BREAKS_PER_100CH",  "1",   conv=int))
    # Phonemes (EN only, optional)
    ELEVENLABS_ENABLE_PHONEMES_EN: bool = field(default_factory=lambda: _get_env("ELEVENLABS_ENABLE_PHONEMES_EN", "false", conv=_parse_bool))
    PHONEME_REDIS_DICT_KEY: str = field(default_factory=lambda: _get_env("PHONEME_REDIS_DICT_KEY", "pron:en", conv=str))
    PHONEME_MAX_PER_MESSAGE: int = field(default_factory=lambda: _get_env("PHONEME_MAX_PER_MESSAGE", "6", conv=int))

_settings_singleton: Optional[Settings] = None

def get_settings() -> Settings:

    global _settings_singleton
    if _settings_singleton is None:
        _settings_singleton = Settings()
    return _settings_singleton

class _SettingsProxy:

    def __getattr__(self, item):
        try:
            return getattr(get_settings(), item)
        except AttributeError:
            global _settings_singleton
            _settings_singleton = None
            return getattr(get_settings(), item)

    def __setattr__(self, key: str, value: Any) -> None:
        setattr(get_settings(), key, value)

settings = _SettingsProxy()

import os
import asyncio
import types
import unittest
from unittest.mock import AsyncMock, patch

os.environ.setdefault("OPENAI_API_KEY", "test")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "12345:abcde")
os.environ.setdefault("TELEGRAM_BOT_USERNAME", "kiragame_aibot")
os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://u:p@localhost/db")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("REDIS_URL_QUEUE", "redis://localhost:6379/1")
os.environ.setdefault("REDIS_URL_VECTOR", "redis://localhost:6379/2")

from app.bot.handlers import moderation
from app.services.addons import passive_moderation


class _FakeRedisMentions:
    def __init__(self) -> None:
        self.cache: dict[str, str] = {}

    async def hget(self, key: str, field: str):
        return None

    async def get(self, key: str):
        return self.cache.get(key)

    async def set(self, key: str, value: str, ex: int | None = None):
        self.cache[key] = value
        return True


class _FakePipe:
    def set(self, *args, **kwargs):
        return None

    async def execute(self):
        return [True]

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeRedisHandler:
    def __init__(self) -> None:
        self.hset = AsyncMock()
        self.sadd = AsyncMock()
        self.set = AsyncMock()
        self.zrem = AsyncMock()

    def pipeline(self, transaction=True):
        return _FakePipe()


class PassiveModerationMentionTests(unittest.IsolatedAsyncioTestCase):
    async def test_check_light_returns_spam_mentions_before_resolve(self) -> None:
        text = "@a @b @c @d"
        entities = [
            {"type": "mention", "offset": 0, "length": 2},
            {"type": "mention", "offset": 3, "length": 2},
            {"type": "mention", "offset": 6, "length": 2},
            {"type": "mention", "offset": 9, "length": 2},
        ]
        with (
            patch.object(passive_moderation, "settings", types.SimpleNamespace(ENABLE_MODERATION=True, MODERATION_SPAM_MENTION_THRESHOLD=2)),
            patch.object(passive_moderation, "is_flooding", AsyncMock(return_value=False)),
            patch.object(passive_moderation, "extract_urls", return_value=[]),
            patch.object(passive_moderation, "extract_external_mentions", AsyncMock(return_value=([], False))) as mentions_mock,
            patch.object(passive_moderation, "contains_telegram_obfuscated", return_value=False),
            patch.object(passive_moderation, "url_is_unwanted", return_value=False),
            patch.object(passive_moderation, "moderate_with_openai", AsyncMock(return_value=False)),
        ):
            status = await passive_moderation.check_light(1, 2, text, entities, source="user")

        self.assertEqual(status, "spam_mentions")
        mentions_mock.assert_not_awaited()

    async def test_extract_external_mentions_caches_resolution(self) -> None:
        redis = _FakeRedisMentions()
        bot = types.SimpleNamespace(get_chat=AsyncMock(return_value=types.SimpleNamespace(type="private", is_bot=False)))
        text = "hello @Known and @Known"
        entities = [
            {"type": "mention", "offset": 6, "length": 6},
            {"type": "mention", "offset": 17, "length": 6},
        ]

        with (
            patch.object(passive_moderation, "get_redis", return_value=redis),
            patch.object(passive_moderation, "get_bot", return_value=bot),
            patch.object(
                passive_moderation,
                "settings",
                types.SimpleNamespace(
                    MOD_MENTION_RESOLVE_TIMEOUT=0.2,
                    MOD_MENTION_RESOLVE_CONCURRENCY=2,
                    MOD_MENTION_RESOLVE_TTL_POS=60,
                    MOD_MENTION_RESOLVE_TTL_NEG=60,
                ),
            ),
        ):
            first_external, first_unresolved = await passive_moderation.extract_external_mentions(1, text, entities)
            second_external, second_unresolved = await passive_moderation.extract_external_mentions(1, text, entities)

        self.assertEqual(first_external, [])
        self.assertFalse(first_unresolved)
        self.assertEqual(second_external, [])
        self.assertFalse(second_unresolved)
        bot.get_chat.assert_awaited_once_with("@known")


    async def test_extract_external_mentions_skips_own_bot_username(self) -> None:
        redis = _FakeRedisMentions()
        bot = types.SimpleNamespace(get_chat=AsyncMock())
        text = "ping @kiragame_aibot"
        entities = [{"type": "mention", "offset": 5, "length": 15}]

        with (
            patch.object(passive_moderation, "get_redis", return_value=redis),
            patch.object(passive_moderation, "get_bot", return_value=bot),
            patch.object(
                passive_moderation,
                "settings",
                types.SimpleNamespace(
                    TELEGRAM_BOT_USERNAME="kiragame_aibot",
                    MOD_MENTION_RESOLVE_TIMEOUT=0.2,
                    MOD_MENTION_RESOLVE_CONCURRENCY=2,
                    MOD_MENTION_RESOLVE_TTL_POS=60,
                    MOD_MENTION_RESOLVE_TTL_NEG=60,
                ),
            ),
        ):
            external, unresolved = await passive_moderation.extract_external_mentions(1, text, entities)

        self.assertEqual(external, [])
        self.assertFalse(unresolved)
        bot.get_chat.assert_not_awaited()

    async def test_check_light_does_not_flag_own_bot_mention_as_link_violation(self) -> None:
        text = "@kiragame_aibot ."
        entities = [{"type": "mention", "offset": 0, "length": 15}]

        with (
            patch.object(
                passive_moderation,
                "settings",
                types.SimpleNamespace(
                    ENABLE_MODERATION=True,
                    MODERATION_SPAM_MENTION_THRESHOLD=5,
                    TELEGRAM_BOT_USERNAME="kiragame_aibot",
                    MOD_MENTION_RESOLVE_TIMEOUT=0.2,
                    MOD_MENTION_RESOLVE_CONCURRENCY=2,
                    MOD_MENTION_RESOLVE_TTL_POS=60,
                    MOD_MENTION_RESOLVE_TTL_NEG=60,
                ),
            ),
            patch.object(passive_moderation, "is_flooding", AsyncMock(return_value=False)),
            patch.object(passive_moderation, "extract_urls", return_value=[]),
            patch.object(passive_moderation, "get_redis", return_value=_FakeRedisMentions()),
            patch.object(passive_moderation, "get_bot", return_value=types.SimpleNamespace(get_chat=AsyncMock())),
            patch.object(passive_moderation, "contains_telegram_obfuscated", return_value=False),
            patch.object(passive_moderation, "url_is_unwanted", return_value=False),
            patch.object(passive_moderation, "moderate_with_openai", AsyncMock(return_value=False)),
        ):
            status = await passive_moderation.check_light(1, 2, text, entities, source="user")

        self.assertEqual(status, "clean")

    async def test_extract_external_mentions_does_not_mark_unknown_error_as_external(self) -> None:
        redis = _FakeRedisMentions()
        bot = types.SimpleNamespace(get_chat=AsyncMock(side_effect=RuntimeError("telegram api timeout")))
        text = "hello @Broken"
        entities = [{"type": "mention", "offset": 6, "length": 7}]

        with (
            patch.object(passive_moderation, "get_redis", return_value=redis),
            patch.object(passive_moderation, "get_bot", return_value=bot),
            patch.object(
                passive_moderation,
                "settings",
                types.SimpleNamespace(
                    MOD_MENTION_RESOLVE_TIMEOUT=0.2,
                    MOD_MENTION_RESOLVE_CONCURRENCY=2,
                    MOD_MENTION_RESOLVE_TTL_POS=60,
                    MOD_MENTION_RESOLVE_TTL_NEG=60,
                ),
            ),
        ):
            external, unresolved = await passive_moderation.extract_external_mentions(1, text, entities)

        self.assertEqual(external, [])
        self.assertTrue(unresolved)

    async def test_check_light_flags_link_violation_on_mention_resolve_error_when_flag_enabled(self) -> None:
        text = "hello @Broken"
        entities = [{"type": "mention", "offset": 6, "length": 7}]

        with (
            patch.object(
                passive_moderation,
                "settings",
                types.SimpleNamespace(
                    ENABLE_MODERATION=True,
                    MODERATION_SPAM_MENTION_THRESHOLD=5,
                    MODERATION_DELETE_UNRESOLVED_MENTIONS=True,
                    TELEGRAM_BOT_USERNAME="kiragame_aibot",
                    MOD_MENTION_RESOLVE_TIMEOUT=0.2,
                    MOD_MENTION_RESOLVE_CONCURRENCY=2,
                    MOD_MENTION_RESOLVE_TTL_POS=60,
                    MOD_MENTION_RESOLVE_TTL_NEG=60,
                ),
            ),
            patch.object(passive_moderation, "is_flooding", AsyncMock(return_value=False)),
            patch.object(passive_moderation, "extract_urls", return_value=[]),
            patch.object(passive_moderation, "get_redis", return_value=_FakeRedisMentions()),
            patch.object(
                passive_moderation,
                "get_bot",
                return_value=types.SimpleNamespace(get_chat=AsyncMock(side_effect=RuntimeError("telegram api timeout"))),
            ),
            patch.object(passive_moderation, "contains_telegram_obfuscated", return_value=False),
            patch.object(passive_moderation, "url_is_unwanted", return_value=False),
            patch.object(passive_moderation, "moderate_with_openai", AsyncMock(return_value=False)),
        ):
            status = await passive_moderation.check_light(1, 2, text, entities, source="user")

        self.assertEqual(status, "link_violation")

    async def test_check_light_keeps_clean_on_mention_resolve_error_when_flag_disabled(self) -> None:
        text = "hello @Broken"
        entities = [{"type": "mention", "offset": 6, "length": 7}]

        with (
            patch.object(
                passive_moderation,
                "settings",
                types.SimpleNamespace(
                    ENABLE_MODERATION=True,
                    MODERATION_SPAM_MENTION_THRESHOLD=5,
                    MODERATION_DELETE_UNRESOLVED_MENTIONS=False,
                    TELEGRAM_BOT_USERNAME="kiragame_aibot",
                    MOD_MENTION_RESOLVE_TIMEOUT=0.2,
                    MOD_MENTION_RESOLVE_CONCURRENCY=2,
                    MOD_MENTION_RESOLVE_TTL_POS=60,
                    MOD_MENTION_RESOLVE_TTL_NEG=60,
                ),
            ),
            patch.object(passive_moderation, "is_flooding", AsyncMock(return_value=False)),
            patch.object(passive_moderation, "extract_urls", return_value=[]),
            patch.object(passive_moderation, "get_redis", return_value=_FakeRedisMentions()),
            patch.object(
                passive_moderation,
                "get_bot",
                return_value=types.SimpleNamespace(get_chat=AsyncMock(side_effect=RuntimeError("telegram api timeout"))),
            ),
            patch.object(passive_moderation, "contains_telegram_obfuscated", return_value=False),
            patch.object(passive_moderation, "url_is_unwanted", return_value=False),
            patch.object(passive_moderation, "moderate_with_openai", AsyncMock(return_value=False)),
        ):
            status = await passive_moderation.check_light(1, 2, text, entities, source="user")

        self.assertEqual(status, "clean")

    async def test_check_light_flags_link_violation_for_external_channel_mention(self) -> None:
        text = "hello @NewsChannel"
        entities = [{"type": "mention", "offset": 6, "length": 12}]

        with (
            patch.object(
                passive_moderation,
                "settings",
                types.SimpleNamespace(
                    ENABLE_MODERATION=True,
                    MODERATION_SPAM_MENTION_THRESHOLD=5,
                    MOD_MENTION_RESOLVE_TIMEOUT=0.2,
                    MOD_MENTION_RESOLVE_CONCURRENCY=2,
                    MOD_MENTION_RESOLVE_TTL_POS=60,
                    MOD_MENTION_RESOLVE_TTL_NEG=60,
                ),
            ),
            patch.object(passive_moderation, "is_flooding", AsyncMock(return_value=False)),
            patch.object(passive_moderation, "extract_urls", return_value=[]),
            patch.object(passive_moderation, "get_redis", return_value=_FakeRedisMentions()),
            patch.object(
                passive_moderation,
                "get_bot",
                return_value=types.SimpleNamespace(get_chat=AsyncMock(return_value=types.SimpleNamespace(type="channel", is_bot=False))),
            ),
            patch.object(passive_moderation, "contains_telegram_obfuscated", return_value=False),
            patch.object(passive_moderation, "url_is_unwanted", return_value=False),
            patch.object(passive_moderation, "moderate_with_openai", AsyncMock(return_value=False)),
        ):
            status = await passive_moderation.check_light(1, 2, text, entities, source="user")

        self.assertEqual(status, "link_violation")

    async def test_check_light_flags_link_violation_for_external_bot_mention(self) -> None:
        text = "hello @SomeBot"
        entities = [{"type": "mention", "offset": 6, "length": 8}]

        with (
            patch.object(
                passive_moderation,
                "settings",
                types.SimpleNamespace(
                    ENABLE_MODERATION=True,
                    MODERATION_SPAM_MENTION_THRESHOLD=5,
                    MOD_MENTION_RESOLVE_TIMEOUT=0.2,
                    MOD_MENTION_RESOLVE_CONCURRENCY=2,
                    MOD_MENTION_RESOLVE_TTL_POS=60,
                    MOD_MENTION_RESOLVE_TTL_NEG=60,
                ),
            ),
            patch.object(passive_moderation, "is_flooding", AsyncMock(return_value=False)),
            patch.object(passive_moderation, "extract_urls", return_value=[]),
            patch.object(passive_moderation, "get_redis", return_value=_FakeRedisMentions()),
            patch.object(
                passive_moderation,
                "get_bot",
                return_value=types.SimpleNamespace(get_chat=AsyncMock(return_value=types.SimpleNamespace(type="private", is_bot=True))),
            ),
            patch.object(passive_moderation, "contains_telegram_obfuscated", return_value=False),
            patch.object(passive_moderation, "url_is_unwanted", return_value=False),
            patch.object(passive_moderation, "moderate_with_openai", AsyncMock(return_value=False)),
        ):
            status = await passive_moderation.check_light(1, 2, text, entities, source="user")

        self.assertEqual(status, "link_violation")


    async def test_check_light_flags_profile_bio_cta_without_urls(self) -> None:
        text = "Все у меня в био, загляни в профиль"
        entities = []

        with (
            patch.object(passive_moderation, "settings", types.SimpleNamespace(ENABLE_MODERATION=True, MODERATION_SPAM_MENTION_THRESHOLD=5)),
            patch.object(passive_moderation, "is_flooding", AsyncMock(return_value=False)),
            patch.object(passive_moderation, "extract_urls", return_value=[]),
            patch.object(passive_moderation, "extract_external_mentions", AsyncMock(return_value=([], False))),
            patch.object(passive_moderation, "contains_telegram_obfuscated", return_value=False),
            patch.object(passive_moderation, "url_is_unwanted", return_value=False),
            patch.object(passive_moderation, "moderate_with_openai", AsyncMock(return_value=False)),
        ):
            status = await passive_moderation.check_light(1, 2, text, entities, source="user")

        self.assertEqual(status, "promo_profile_cta")

    async def test_check_light_flags_channel_chat_cta_without_urls(self) -> None:
        text = "На моем канале все подробности, пиши мне"
        entities = []

        with (
            patch.object(passive_moderation, "settings", types.SimpleNamespace(ENABLE_MODERATION=True, MODERATION_SPAM_MENTION_THRESHOLD=5)),
            patch.object(passive_moderation, "is_flooding", AsyncMock(return_value=False)),
            patch.object(passive_moderation, "extract_urls", return_value=[]),
            patch.object(passive_moderation, "extract_external_mentions", AsyncMock(return_value=([], False))),
            patch.object(passive_moderation, "contains_telegram_obfuscated", return_value=False),
            patch.object(passive_moderation, "url_is_unwanted", return_value=False),
            patch.object(passive_moderation, "moderate_with_openai", AsyncMock(return_value=False)),
        ):
            status = await passive_moderation.check_light(1, 2, text, entities, source="user")

        self.assertEqual(status, "promo_profile_cta")


    async def test_check_light_flags_combat_promo_cta_without_urls(self) -> None:
        text = "Я ветеран, много контента с фронта, заходи ко мне в профиль"
        entities = []

        with (
            patch.object(passive_moderation, "settings", types.SimpleNamespace(ENABLE_MODERATION=True, MODERATION_SPAM_MENTION_THRESHOLD=5)),
            patch.object(passive_moderation, "is_flooding", AsyncMock(return_value=False)),
            patch.object(passive_moderation, "extract_urls", return_value=[]),
            patch.object(passive_moderation, "extract_external_mentions", AsyncMock(return_value=([], False))),
            patch.object(passive_moderation, "contains_telegram_obfuscated", return_value=False),
            patch.object(passive_moderation, "url_is_unwanted", return_value=False),
            patch.object(passive_moderation, "moderate_with_openai", AsyncMock(return_value=False)),
        ):
            status = await passive_moderation.check_light(1, 2, text, entities, source="user")

        self.assertEqual(status, "promo_profile_cta")

    async def test_check_light_keeps_clean_for_combat_discussion_without_cta(self) -> None:
        text = "Сегодня обсуждали историю про фронт и дроны в новостях"
        entities = []

        with (
            patch.object(passive_moderation, "settings", types.SimpleNamespace(ENABLE_MODERATION=True, MODERATION_SPAM_MENTION_THRESHOLD=5)),
            patch.object(passive_moderation, "is_flooding", AsyncMock(return_value=False)),
            patch.object(passive_moderation, "extract_urls", return_value=[]),
            patch.object(passive_moderation, "extract_external_mentions", AsyncMock(return_value=([], False))),
            patch.object(passive_moderation, "contains_telegram_obfuscated", return_value=False),
            patch.object(passive_moderation, "url_is_unwanted", return_value=False),
            patch.object(passive_moderation, "moderate_with_openai", AsyncMock(return_value=False)),
        ):
            status = await passive_moderation.check_light(1, 2, text, entities, source="user")

        self.assertEqual(status, "clean")

    async def test_check_light_keeps_clean_for_regular_text_without_cta(self) -> None:
        text = "Просто обсуждаем патч и багрепорты"
        entities = []

        with (
            patch.object(passive_moderation, "settings", types.SimpleNamespace(ENABLE_MODERATION=True, MODERATION_SPAM_MENTION_THRESHOLD=5)),
            patch.object(passive_moderation, "is_flooding", AsyncMock(return_value=False)),
            patch.object(passive_moderation, "extract_urls", return_value=[]),
            patch.object(passive_moderation, "extract_external_mentions", AsyncMock(return_value=([], False))),
            patch.object(passive_moderation, "contains_telegram_obfuscated", return_value=False),
            patch.object(passive_moderation, "url_is_unwanted", return_value=False),
            patch.object(passive_moderation, "moderate_with_openai", AsyncMock(return_value=False)),
        ):
            status = await passive_moderation.check_light(1, 2, text, entities, source="user")

        self.assertEqual(status, "clean")


    async def test_check_light_flags_job_promo_without_links(self) -> None:
        text = "Есть работа удаленно, хороший доход, пиши в личку"
        entities = []

        with (
            patch.object(passive_moderation, "settings", types.SimpleNamespace(ENABLE_MODERATION=True, MODERATION_SPAM_MENTION_THRESHOLD=5)),
            patch.object(passive_moderation, "is_flooding", AsyncMock(return_value=False)),
            patch.object(passive_moderation, "extract_urls", return_value=[]),
            patch.object(passive_moderation, "extract_external_mentions", AsyncMock(return_value=([], False))),
            patch.object(passive_moderation, "contains_telegram_obfuscated", return_value=False),
            patch.object(passive_moderation, "url_is_unwanted", return_value=False),
            patch.object(passive_moderation, "moderate_with_openai", AsyncMock(return_value=False)),
        ):
            status = await passive_moderation.check_light(1, 2, text, entities, source="user")

        self.assertEqual(status, "promo")

    async def test_check_light_flags_explicit_nsfw_without_ai(self) -> None:
        text = "скину porn видео"
        entities = []

        with (
            patch.object(passive_moderation, "settings", types.SimpleNamespace(ENABLE_MODERATION=True, MODERATION_SPAM_MENTION_THRESHOLD=5)),
            patch.object(passive_moderation, "is_flooding", AsyncMock(return_value=False)),
            patch.object(passive_moderation, "extract_urls", return_value=[]),
            patch.object(passive_moderation, "extract_external_mentions", AsyncMock(return_value=([], False))),
            patch.object(passive_moderation, "contains_telegram_obfuscated", return_value=False),
            patch.object(passive_moderation, "url_is_unwanted", return_value=False),
            patch.object(passive_moderation, "moderate_with_openai", AsyncMock(return_value=False)),
        ):
            status = await passive_moderation.check_light(1, 2, text, entities, source="user")

        self.assertEqual(status, "sexual_content")

    async def test_comment_context_promo_profile_cta_forces_deep_check(self) -> None:
        fake_redis = _FakeRedisHandler()
        deep_mock = AsyncMock(return_value=False)

        with (
            patch.object(moderation, "redis_client", fake_redis),
            patch.object(moderation, "settings", types.SimpleNamespace(MODERATION_ADMIN_EXEMPT=False, MOD_ALERT_THROTTLE_SECONDS=60, MOD_LIGHT_TIMEOUT=0.5, MOD_DEEP_TIMEOUT=5.0, MOD_DEEP_TEXT_THRESHOLD=400)),
            patch.object(moderation, "get_targets", return_value=[]),
            patch.object(moderation, "check_light", AsyncMock(return_value="promo_profile_cta")),
            patch.object(moderation, "check_deep", deep_mock),
            patch.object(moderation, "extract_urls", return_value=[]),
            patch.object(moderation, "contains_telegram_obfuscated", return_value=False),
            patch.object(moderation, "contains_any_link_obfuscated", return_value=False),
            patch.object(moderation, "_is_new_user", AsyncMock(return_value=False)),
            patch.object(moderation, "analytics_record_moderation", AsyncMock()),
            patch.object(moderation, "_resolve_chat_display_name", AsyncMock(return_value="")),
        ):
            await moderation.handle_passive_moderation(
                chat_id=100,
                message=None,
                text="смотри в профиле",
                entities=[],
                source="user",
                user_id=42,
                message_id=79,
                is_comment_context=True,
            )

        deep_mock.assert_awaited_once()

    async def test_group_context_promo_profile_cta_forces_deep_check(self) -> None:
        fake_redis = _FakeRedisHandler()
        deep_mock = AsyncMock(return_value=False)

        with (
            patch.object(moderation, "redis_client", fake_redis),
            patch.object(moderation, "settings", types.SimpleNamespace(MODERATION_ADMIN_EXEMPT=False, MOD_ALERT_THROTTLE_SECONDS=60, MOD_LIGHT_TIMEOUT=0.5, MOD_DEEP_TIMEOUT=5.0, MOD_DEEP_TEXT_THRESHOLD=400)),
            patch.object(moderation, "get_targets", return_value=[]),
            patch.object(moderation, "check_light", AsyncMock(return_value="promo_profile_cta")),
            patch.object(moderation, "check_deep", deep_mock),
            patch.object(moderation, "extract_urls", return_value=[]),
            patch.object(moderation, "contains_telegram_obfuscated", return_value=False),
            patch.object(moderation, "contains_any_link_obfuscated", return_value=False),
            patch.object(moderation, "_is_new_user", AsyncMock(return_value=False)),
            patch.object(moderation, "analytics_record_moderation", AsyncMock()),
            patch.object(moderation, "_resolve_chat_display_name", AsyncMock(return_value="")),
        ):
            await moderation.handle_passive_moderation(
                chat_id=100,
                message=None,
                text="смотри в профиле",
                entities=[],
                source="user",
                user_id=42,
                message_id=81,
                is_comment_context=False,
            )

        deep_mock.assert_awaited_once()

    async def test_comment_context_clean_without_base_risk_skips_deep_check(self) -> None:
        fake_redis = _FakeRedisHandler()
        deep_mock = AsyncMock(return_value=False)

        with (
            patch.object(moderation, "redis_client", fake_redis),
            patch.object(moderation, "settings", types.SimpleNamespace(MODERATION_ADMIN_EXEMPT=False, MOD_ALERT_THROTTLE_SECONDS=60, MOD_LIGHT_TIMEOUT=0.5, MOD_DEEP_TIMEOUT=5.0, MOD_DEEP_TEXT_THRESHOLD=400)),
            patch.object(moderation, "get_targets", return_value=[]),
            patch.object(moderation, "check_light", AsyncMock(return_value="clean")),
            patch.object(moderation, "check_deep", deep_mock),
            patch.object(moderation, "extract_urls", return_value=[]),
            patch.object(moderation, "contains_telegram_obfuscated", return_value=False),
            patch.object(moderation, "contains_any_link_obfuscated", return_value=False),
            patch.object(moderation, "_is_new_user", AsyncMock(return_value=False)),
            patch.object(moderation, "analytics_record_moderation", AsyncMock()),
            patch.object(moderation, "_resolve_chat_display_name", AsyncMock(return_value="")),
        ):
            await moderation.handle_passive_moderation(
                chat_id=100,
                message=None,
                text="обычный комментарий",
                entities=[],
                source="user",
                user_id=42,
                message_id=80,
                is_comment_context=True,
            )

        deep_mock.assert_not_awaited()

    async def test_handle_passive_moderation_maps_profile_cta_reason(self) -> None:
        fake_redis = _FakeRedisHandler()

        with (
            patch.object(moderation, "redis_client", fake_redis),
            patch.object(moderation, "settings", types.SimpleNamespace(MODERATION_ADMIN_EXEMPT=False, MOD_ALERT_THROTTLE_SECONDS=60, MOD_LIGHT_TIMEOUT=0.5, MOD_DEEP_TIMEOUT=5.0, MOD_DEEP_TEXT_THRESHOLD=400)),
            patch.object(moderation, "get_targets", return_value=[]),
            patch.object(moderation, "check_light", AsyncMock(return_value="promo_profile_cta")),
            patch.object(moderation, "check_deep", AsyncMock(return_value=False)),
            patch.object(moderation, "extract_urls", return_value=[]),
            patch.object(moderation, "contains_telegram_obfuscated", return_value=False),
            patch.object(moderation, "contains_any_link_obfuscated", return_value=False),
            patch.object(moderation, "_is_new_user", AsyncMock(return_value=False)),
            patch.object(moderation, "analytics_record_moderation", AsyncMock()),
            patch.object(moderation, "_resolve_chat_display_name", AsyncMock(return_value="")),
        ):
            status = await moderation.handle_passive_moderation(
                chat_id=100,
                message=None,
                text="смотри в профиле",
                entities=[],
                source="user",
                user_id=42,
                message_id=78,
            )

        self.assertEqual(status, "flagged")
        self.assertEqual(
            fake_redis.hset.await_args.kwargs["mapping"]["reason"],
            "Promotional CTA to profile/bio/channel",
        )

    async def test_handle_passive_moderation_timeout_is_risky_not_clean(self) -> None:
        fake_redis = _FakeRedisHandler()

        async def _slow_light(*args, **kwargs):
            await asyncio.sleep(0.05)
            return "clean"

        with (
            patch.object(moderation, "redis_client", fake_redis),
            patch.object(moderation, "settings", types.SimpleNamespace(MODERATION_ADMIN_EXEMPT=False, MOD_ALERT_THROTTLE_SECONDS=60, MOD_LIGHT_TIMEOUT=0.01, MOD_DEEP_TIMEOUT=5.0, MOD_DEEP_TEXT_THRESHOLD=400)),
            patch.object(moderation, "get_targets", return_value=[]),
            patch.object(moderation, "check_light", _slow_light),
            patch.object(moderation, "check_deep", AsyncMock(return_value=False)),
            patch.object(moderation, "extract_urls", return_value=[]),
            patch.object(moderation, "contains_telegram_obfuscated", return_value=False),
            patch.object(moderation, "contains_any_link_obfuscated", return_value=False),
            patch.object(moderation, "_is_new_user", AsyncMock(return_value=False)),
            patch.object(moderation, "analytics_record_moderation", AsyncMock()),
        ):
            status = await moderation.handle_passive_moderation(
                chat_id=100,
                message=None,
                text="hello",
                entities=[],
                source="user",
                user_id=42,
                message_id=77,
            )

        self.assertEqual(status, "flagged")
        self.assertNotEqual(status, "clean")
        self.assertEqual(fake_redis.hset.await_args.kwargs["mapping"]["reason"], "Light moderation timeout (risk fallback)")


    async def test_check_deep_without_history_uses_only_current_message(self) -> None:
        moderate_mock = AsyncMock(return_value=False)

        with (
            patch.object(passive_moderation, "settings", types.SimpleNamespace(ENABLE_AI_MODERATION=True, MODERATION_DEEP_INCLUDE_HISTORY=False)),
            patch.object(passive_moderation, "load_context", AsyncMock(return_value=[{"role": "user", "content": "you are awful"}])),
            patch.object(passive_moderation, "moderate_with_openai", moderate_mock),
        ):
            blocked = await passive_moderation.check_deep(
                chat_id=100,
                user_id=42,
                text="hello there",
                source="user",
            )

        self.assertFalse(blocked)
        moderate_mock.assert_awaited_once_with("hello there", image_b64=None, image_mime=None)



    async def test_moderate_with_openai_image_only_payload_has_no_synthetic_text(self) -> None:
        response = types.SimpleNamespace(output_text='{"regular_promo":false,"income_promo":false,"insult_abuse":false,"threat_abuse":false}')
        call_mock = AsyncMock(return_value=response)

        with (
            patch.object(passive_moderation, "settings", types.SimpleNamespace(ENABLE_AI_MODERATION=True, MODERATION_MODEL="gpt-5-nano", MODERATION_AI_REASONING_EFFORT="low")),
            patch.object(passive_moderation, "_call_openai_with_retry", call_mock),
        ):
            flagged = await passive_moderation.moderate_with_openai("", image_b64="Zm9v", image_mime="image/png")

        self.assertFalse(flagged)
        sent_input = call_mock.await_args.kwargs["input"]
        self.assertEqual(sent_input[1]["role"], "user")
        content = sent_input[1]["content"]
        self.assertEqual(len(content), 1)
        self.assertEqual(content[0]["type"], "input_image")

    async def test_moderate_with_openai_honors_enabled_category_boolean_flag(self) -> None:
        response = types.SimpleNamespace(output_text='{"regular_promo":false,"income_promo":true,"insult_abuse":false,"threat_abuse":false}')

        with (
            patch.object(passive_moderation, "settings", types.SimpleNamespace(ENABLE_AI_MODERATION=True, MODERATION_MODEL="gpt-5-nano", MODERATION_AI_REASONING_EFFORT="low")),
            patch.object(passive_moderation, "_call_openai_with_retry", AsyncMock(return_value=response)),
        ):
            flagged = await passive_moderation.moderate_with_openai("income text")

        self.assertTrue(flagged)
        self.assertEqual(passive_moderation.get_last_ai_moderation_category(), "income_promo")

    async def test_moderate_with_openai_flagged_false_with_high_score_stays_false(self) -> None:
        response = types.SimpleNamespace(output_text='{"regular_promo":false,"income_promo":false,"insult_abuse":false,"threat_abuse":false}')

        with (
            patch.object(passive_moderation, "settings", types.SimpleNamespace(ENABLE_AI_MODERATION=True, MODERATION_MODEL="gpt-5-nano", MODERATION_AI_REASONING_EFFORT="low")),
            patch.object(passive_moderation, "_call_openai_with_retry", AsyncMock(return_value=response)),
        ):
            flagged = await passive_moderation.moderate_with_openai("neutral text")

        self.assertFalse(flagged)
        self.assertEqual(passive_moderation.get_last_ai_moderation_category(), "")

    async def test_moderate_with_openai_mixed_payload_keeps_text_and_image(self) -> None:
        response = types.SimpleNamespace(output_text='{"regular_promo":false,"income_promo":false,"insult_abuse":false,"threat_abuse":false}')
        call_mock = AsyncMock(return_value=response)

        with (
            patch.object(passive_moderation, "settings", types.SimpleNamespace(ENABLE_AI_MODERATION=True, MODERATION_MODEL="gpt-5-nano", MODERATION_AI_REASONING_EFFORT="low")),
            patch.object(passive_moderation, "_call_openai_with_retry", call_mock),
        ):
            flagged = await passive_moderation.moderate_with_openai("hello", image_b64="Zm9v", image_mime="image/png")

        self.assertFalse(flagged)
        sent_input = call_mock.await_args.kwargs["input"]
        content = sent_input[1]["content"]
        self.assertEqual(len(content), 2)
        self.assertEqual(content[0], {"type": "input_text", "text": "hello"})
        self.assertEqual(content[1]["type"], "input_image")

    async def test_check_deep_with_history_includes_context_and_new_message(self) -> None:
        moderate_mock = AsyncMock(return_value=True)
        history = [
            {"role": "user", "content": "toxic history"},
            {"role": "assistant", "content": "ack"},
        ]

        with (
            patch.object(passive_moderation, "settings", types.SimpleNamespace(ENABLE_AI_MODERATION=True, MODERATION_DEEP_INCLUDE_HISTORY=True)),
            patch.object(passive_moderation, "load_context", AsyncMock(return_value=history)),
            patch.object(passive_moderation, "moderate_with_openai", moderate_mock),
        ):
            blocked = await passive_moderation.check_deep(
                chat_id=100,
                user_id=42,
                text="neutral message",
                source="user",
            )

        self.assertTrue(blocked)
        combined_text = moderate_mock.await_args.args[0]
        self.assertIn("USER: toxic history", combined_text)
        self.assertIn("ASSISTANT: ack", combined_text)
        self.assertIn("NEW MESSAGE:\nneutral message", combined_text)


if __name__ == "__main__":
    unittest.main()

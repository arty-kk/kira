import os
import asyncio
import types
import unittest
from unittest.mock import AsyncMock, patch

from aiogram.enums import ChatType

os.environ.setdefault("OPENAI_API_KEY", "test")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "12345:abcde")
os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://u:p@localhost/db")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("REDIS_URL_QUEUE", "redis://localhost:6379/1")
os.environ.setdefault("REDIS_URL_VECTOR", "redis://localhost:6379/2")

from app.bot.handlers import moderation
from app.services.addons import passive_moderation


class PassiveModerationSourceBehaviorTests(unittest.IsolatedAsyncioTestCase):
    async def test_check_light_channel_trusted_runs_ai_and_keeps_flood_user_only(self) -> None:
        with (
            patch.object(passive_moderation, "settings", types.SimpleNamespace(ENABLE_MODERATION=True, ENABLE_AI_MODERATION=True, MODERATION_SPAM_LINK_THRESHOLD=5)),
            patch.object(passive_moderation, "is_flooding", AsyncMock(return_value=True)) as flooding_mock,
            patch.object(passive_moderation, "extract_urls", return_value=[]),
            patch.object(passive_moderation, "extract_external_mentions", AsyncMock(return_value=([], False))),
            patch.object(passive_moderation, "contains_telegram_obfuscated", return_value=False),
            patch.object(passive_moderation, "url_is_unwanted", return_value=False),
            patch.object(passive_moderation, "moderate_with_openai", AsyncMock(return_value=True)) as toxic_mock,
        ):
            status = await passive_moderation.check_light(1, 2, "hello", [], source="channel", allow_ai_for_source=True)

        self.assertEqual(status, "toxic")
        flooding_mock.assert_not_awaited()
        toxic_mock.assert_awaited_once()


    async def test_check_light_user_keeps_flood_behavior(self) -> None:
        with (
            patch.object(passive_moderation, "settings", types.SimpleNamespace(ENABLE_MODERATION=True, ENABLE_AI_MODERATION=True, MODERATION_SPAM_LINK_THRESHOLD=5)),
            patch.object(passive_moderation, "is_flooding", AsyncMock(return_value=True)) as flooding_mock,
        ):
            status = await passive_moderation.check_light(1, 2, "hello", [], source="user")

        self.assertEqual(status, "flood")
        flooding_mock.assert_awaited_once()

    async def test_check_deep_channel_trusted_runs_ai_without_history(self) -> None:
        with (
            patch.object(passive_moderation, "settings", types.SimpleNamespace(ENABLE_MODERATION=True, ENABLE_AI_MODERATION=True, MODERATION_SPAM_LINK_THRESHOLD=5)),
            patch.object(passive_moderation, "load_context", AsyncMock(return_value=[{"role": "user", "content": "x"}])) as load_mock,
            patch.object(passive_moderation, "moderate_with_openai", AsyncMock(return_value=True)) as ai_mock,
        ):
            blocked = await passive_moderation.check_deep(1, 2, "hello", source="channel", allow_ai_for_source=True)

        self.assertTrue(blocked)
        load_mock.assert_not_awaited()
        ai_mock.assert_awaited_once_with("hello", image_b64=None, image_mime=None)

    async def test_check_deep_bot_trusted_runs_ai_without_history(self) -> None:
        with (
            patch.object(passive_moderation, "settings", types.SimpleNamespace(ENABLE_MODERATION=True, ENABLE_AI_MODERATION=True, MODERATION_SPAM_LINK_THRESHOLD=5)),
            patch.object(passive_moderation, "load_context", AsyncMock(return_value=[{"role": "user", "content": "x"}])) as load_mock,
            patch.object(passive_moderation, "moderate_with_openai", AsyncMock(return_value=True)) as ai_mock,
        ):
            blocked = await passive_moderation.check_deep(1, 2, "hello", source="bot", allow_ai_for_source=True)

        self.assertTrue(blocked)
        load_mock.assert_not_awaited()
        ai_mock.assert_awaited_once_with("hello", image_b64=None, image_mime=None)


    async def test_handle_passive_moderation_linked_channel_full_trust_skips_ai(self) -> None:
        fake_redis = _FakeRedis()

        message = types.SimpleNamespace(
            chat=types.SimpleNamespace(id=-1001, type=ChatType.SUPERGROUP, linked_chat_id=None),
            from_user=None,
            message_id=88,
            entities=[],
            caption_entities=[],
        )

        with (
            patch.object(moderation, "redis_client", fake_redis),
            patch.object(moderation, "settings", types.SimpleNamespace(
                MODERATION_ADMIN_EXEMPT=False,
                MOD_ALERT_THROTTLE_SECONDS=60,
                MOD_LIGHT_TIMEOUT=2.0,
                MOD_DEEP_TIMEOUT=5.0,
                MOD_DEEP_TEXT_THRESHOLD=10,
                ALLOWED_GROUP_IDS=[],
                COMMENT_SOURCE_CHANNEL_IDS=[],
            )),
            patch.object(moderation, "is_from_linked_channel", AsyncMock(return_value=True)),
            patch.object(moderation, "get_targets", return_value=[]),
            patch.object(moderation, "check_light", AsyncMock(return_value="toxic")) as check_light_mock,
            patch.object(moderation, "check_deep", AsyncMock(return_value=False)) as check_deep_mock,
            patch.object(moderation, "extract_urls", return_value=[]),
            patch.object(moderation, "contains_telegram_obfuscated", return_value=False),
            patch.object(moderation, "contains_any_link_obfuscated", return_value=False),
            patch.object(moderation, "_is_new_user", AsyncMock(return_value=False)),
            patch.object(moderation, "analytics_record_moderation", AsyncMock()),
        ):
            status = await moderation.handle_passive_moderation(
                chat_id=-1001,
                message=message,
                text="hello",
                entities=[],
                image_b64=None,
                source="channel",
                user_id=42,
                message_id=88,
            )

        self.assertEqual(status, "clean")
        check_light_mock.assert_not_awaited()
        check_deep_mock.assert_not_awaited()

    async def test_handle_passive_moderation_trusted_admin_repost_from_untrusted_source_is_exempt(self) -> None:
        fake_redis = _FakeRedis()
        message = types.SimpleNamespace(
            chat=types.SimpleNamespace(id=-10055, type=ChatType.SUPERGROUP, linked_chat_id=None),
            from_user=types.SimpleNamespace(id=777),
            sender_chat=None,
            forward_from_chat=types.SimpleNamespace(id=-100999, type=ChatType.CHANNEL),
            message_id=99,
            entities=[],
            caption_entities=[],
            is_automatic_forward=False,
        )

        with (
            patch.object(moderation, "redis_client", fake_redis),
            patch.object(moderation, "settings", types.SimpleNamespace(
                MODERATION_ADMIN_EXEMPT=False,
                MOD_ALERT_THROTTLE_SECONDS=60,
                MOD_LIGHT_TIMEOUT=2.0,
                MOD_DEEP_TIMEOUT=5.0,
                MOD_DEEP_TEXT_THRESHOLD=10,
                ALLOWED_GROUP_IDS=[-10055],
                COMMENT_SOURCE_CHANNEL_IDS=[],
            )),
            patch.object(moderation, "_is_admin", AsyncMock(return_value=True)),
            patch.object(moderation, "is_from_linked_channel", AsyncMock(return_value=False)),
            patch.object(moderation, "check_light", AsyncMock(return_value="toxic")) as check_light_mock,
            patch.object(moderation, "check_deep", AsyncMock(return_value=True)) as check_deep_mock,
            patch.object(moderation, "analytics_record_moderation", AsyncMock()),
        ):
            status = await moderation.handle_passive_moderation(
                chat_id=-10055,
                message=message,
                text="forwarded text",
                entities=[],
                source="user",
                user_id=777,
                message_id=99,
            )

        self.assertEqual(status, "clean")
        check_light_mock.assert_not_awaited()
        check_deep_mock.assert_not_awaited()

    async def test_handle_passive_moderation_comment_target_admin_is_exempt(self) -> None:
        fake_redis = _FakeRedis()
        message = types.SimpleNamespace(
            chat=types.SimpleNamespace(id=-100777, type=ChatType.SUPERGROUP, linked_chat_id=-10042),
            from_user=types.SimpleNamespace(id=555),
            sender_chat=None,
            forward_from_chat=None,
            message_id=17,
            entities=[],
            caption_entities=[],
            is_automatic_forward=False,
        )

        with (
            patch.object(moderation, "redis_client", fake_redis),
            patch.object(moderation, "settings", types.SimpleNamespace(
                MODERATION_ADMIN_EXEMPT=False,
                MOD_ALERT_THROTTLE_SECONDS=60,
                MOD_LIGHT_TIMEOUT=2.0,
                MOD_DEEP_TIMEOUT=5.0,
                MOD_DEEP_TEXT_THRESHOLD=10,
                ALLOWED_GROUP_IDS=[],
                COMMENT_TARGET_CHAT_IDS=[-100777],
                COMMENT_SOURCE_CHANNEL_IDS=[-10042],
            )),
            patch.object(moderation, "_is_admin", AsyncMock(return_value=True)),
            patch.object(moderation, "is_from_linked_channel", AsyncMock(return_value=False)),
            patch.object(moderation, "check_light", AsyncMock(return_value="toxic")) as check_light_mock,
            patch.object(moderation, "check_deep", AsyncMock(return_value=True)) as check_deep_mock,
            patch.object(moderation, "analytics_record_moderation", AsyncMock()),
        ):
            status = await moderation.handle_passive_moderation(
                chat_id=-100777,
                message=message,
                text="comment",
                entities=[],
                source="user",
                user_id=555,
                message_id=17,
            )

        self.assertEqual(status, "clean")
        check_light_mock.assert_not_awaited()
        check_deep_mock.assert_not_awaited()

class ProfileNsfwClassifierTests(unittest.IsolatedAsyncioTestCase):
    async def test_classify_profile_nsfw_fast_flags_when_sexual_category_present(self) -> None:
        resp = types.SimpleNamespace(
            results=[
                types.SimpleNamespace(
                    categories={"sexual": True},
                    category_scores={"sexual": 0.2},
                )
            ]
        )
        with (
            patch.object(passive_moderation, "get_openai", return_value=types.SimpleNamespace(moderations=types.SimpleNamespace(create=AsyncMock(return_value=resp)))),
            patch.object(passive_moderation, "settings", types.SimpleNamespace(MODERATION_MODEL="omni-moderation-latest", MODERATION_AI_NSFW_THRESHOLD=0.6)),
        ):
            flagged = await passive_moderation.classify_profile_nsfw_fast(image_b64="abcd", image_mime="image/jpeg")

        self.assertTrue(flagged)

    async def test_classify_profile_nsfw_fast_flags_by_sexual_score_threshold(self) -> None:
        resp = types.SimpleNamespace(
            results=[
                types.SimpleNamespace(
                    categories={"sexual": False},
                    category_scores={"sexual": 0.95},
                )
            ]
        )
        with (
            patch.object(passive_moderation, "get_openai", return_value=types.SimpleNamespace(moderations=types.SimpleNamespace(create=AsyncMock(return_value=resp)))),
            patch.object(passive_moderation, "settings", types.SimpleNamespace(MODERATION_MODEL="omni-moderation-latest", MODERATION_AI_NSFW_THRESHOLD=0.6)),
        ):
            flagged = await passive_moderation.classify_profile_nsfw_fast(image_b64="abcd", image_mime="image/jpeg")

        self.assertTrue(flagged)

    async def test_classify_profile_nsfw_fast_returns_false_for_non_sexual_flags(self) -> None:
        resp = types.SimpleNamespace(
            results=[
                types.SimpleNamespace(
                    categories={"violence": True, "sexual": False},
                    category_scores={"violence": 0.99, "sexual": 0.01},
                )
            ]
        )
        with (
            patch.object(passive_moderation, "get_openai", return_value=types.SimpleNamespace(moderations=types.SimpleNamespace(create=AsyncMock(return_value=resp)))),
            patch.object(passive_moderation, "settings", types.SimpleNamespace(MODERATION_MODEL="omni-moderation-latest", MODERATION_AI_NSFW_THRESHOLD=0.6)),
        ):
            flagged = await passive_moderation.classify_profile_nsfw_fast(image_b64="abcd", image_mime="image/jpeg")

        self.assertFalse(flagged)


class _FakePipe:
    def __init__(self, set_calls):
        self._set_calls = set_calls

    def set(self, *args, **kwargs):
        self._set_calls.append((args, kwargs))
        return None

    async def execute(self):
        return [True]

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeRedis:
    def __init__(self) -> None:
        self.hset = AsyncMock()
        self.sadd = AsyncMock()
        self.set = AsyncMock()
        self.zrem = AsyncMock()
        self.pipeline_set_calls = []

    def pipeline(self, transaction=True):
        return _FakePipe(self.pipeline_set_calls)


class ModerationHandlerSourceRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_handle_passive_moderation_empty_payload_skips_light_throttle_set(self) -> None:
        fake_redis = _FakeRedis()

        with (
            patch.object(moderation, "redis_client", fake_redis),
            patch.object(moderation, "settings", types.SimpleNamespace(MODERATION_ADMIN_EXEMPT=False, MOD_ALERT_THROTTLE_SECONDS=60, MOD_LIGHT_TIMEOUT=2.0, MOD_DEEP_TIMEOUT=5.0, MOD_DEEP_TEXT_THRESHOLD=400)),
            patch.object(moderation, "check_light", AsyncMock(return_value="clean")) as check_light_mock,
            patch.object(moderation, "check_deep", AsyncMock(return_value=False)) as check_deep_mock,
        ):
            status = await moderation.handle_passive_moderation(
                chat_id=100,
                message=None,
                text="   ",
                entities=[],
                image_b64=None,
                source="user",
                user_id=42,
                message_id=77,
            )

        self.assertEqual(status, "clean")
        self.assertEqual(fake_redis.pipeline_set_calls, [])
        check_light_mock.assert_not_awaited()
        check_deep_mock.assert_not_awaited()

    async def test_handle_passive_moderation_non_empty_payload_sets_light_throttle(self) -> None:
        fake_redis = _FakeRedis()

        with (
            patch.object(moderation, "redis_client", fake_redis),
            patch.object(moderation, "settings", types.SimpleNamespace(MODERATION_ADMIN_EXEMPT=False, MOD_ALERT_THROTTLE_SECONDS=60, MOD_LIGHT_TIMEOUT=2.0, MOD_DEEP_TIMEOUT=5.0, MOD_DEEP_TEXT_THRESHOLD=400)),
            patch.object(moderation, "get_targets", return_value=[]),
            patch.object(moderation, "check_light", AsyncMock(return_value="clean")),
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
                image_b64=None,
                source="user",
                user_id=42,
                message_id=77,
                is_comment_context=True,
            )

        self.assertEqual(status, "clean")
        self.assertEqual(len(fake_redis.pipeline_set_calls), 1)
        set_args, set_kwargs = fake_redis.pipeline_set_calls[0]
        self.assertEqual(set_args, ("mod_alert:light:100:42", 1))
        self.assertEqual(set_kwargs, {"ex": 60, "nx": True})

    async def test_handle_passive_moderation_toxic_skips_deep_and_deletes_when_flagged_delete_enabled(self) -> None:
        fake_redis = _FakeRedis()
        check_deep_mock = AsyncMock(return_value=True)

        with (
            patch.object(moderation, "redis_client", fake_redis),
            patch.object(moderation, "settings", types.SimpleNamespace(
                MODERATION_ADMIN_EXEMPT=False,
                MOD_ALERT_THROTTLE_SECONDS=60,
                MOD_LIGHT_TIMEOUT=2.0,
                MOD_DEEP_TIMEOUT=5.0,
                MOD_DEEP_TEXT_THRESHOLD=400,
                MODERATION_DELETE_BLOCKED=True,
                MODERATION_DELETE_FLAGGED=True,
            )),
            patch.object(moderation, "get_targets", return_value=[]),
            patch.object(moderation, "check_light", AsyncMock(return_value="toxic")),
            patch.object(moderation, "check_deep", check_deep_mock),
            patch.object(moderation, "extract_urls", return_value=[]),
            patch.object(moderation, "contains_telegram_obfuscated", return_value=False),
            patch.object(moderation, "contains_any_link_obfuscated", return_value=False),
            patch.object(moderation, "_is_new_user", AsyncMock(return_value=False)),
            patch.object(moderation, "_delete_message_safe", AsyncMock(return_value=True)) as delete_mock,
            patch.object(moderation, "analytics_record_moderation", AsyncMock()),
        ):
            status = await moderation.handle_passive_moderation(
                chat_id=100,
                message=None,
                text="hello",
                entities=[],
                image_b64=None,
                source="user",
                user_id=42,
                message_id=77,
            )

        self.assertEqual(status, "flagged")
        check_deep_mock.assert_not_awaited()
        delete_mock.assert_awaited_once_with(100, 77)

    async def test_handle_passive_moderation_blocks_previously_flagged_profile_in_worker(self) -> None:
        fake_redis = _FakeRedis()
        fake_redis.exists = AsyncMock(return_value=True)
        check_light_mock = AsyncMock(return_value="clean")

        with (
            patch.object(moderation, "redis_client", fake_redis),
            patch.object(moderation, "settings", types.SimpleNamespace(MODERATION_ADMIN_EXEMPT=False, MODERATION_PROFILE_NSFW_ENFORCE=True, MOD_ALERT_THROTTLE_SECONDS=60, MOD_LIGHT_TIMEOUT=2.0, MOD_DEEP_TIMEOUT=5.0, MOD_DEEP_TEXT_THRESHOLD=400)),
            patch.object(moderation, "_restrict_user_write_safe", AsyncMock(return_value=True)) as restrict_mock,
            patch.object(moderation, "_delete_message_safe", AsyncMock(return_value=True)) as delete_mock,
            patch.object(moderation, "_flag", AsyncMock()) as flag_mock,
            patch.object(moderation, "_is_profile_nsfw", AsyncMock(return_value=False)) as nsfw_mock,
            patch.object(moderation, "check_light", check_light_mock),
        ):
            status = await moderation.handle_passive_moderation(
                chat_id=-1001,
                message=None,
                text="hello",
                entities=[],
                source="user",
                user_id=42,
                message_id=77,
            )

        self.assertEqual(status, "blocked")
        nsfw_mock.assert_not_awaited()
        check_light_mock.assert_not_awaited()
        delete_mock.assert_awaited_once_with(-1001, 77)
        restrict_mock.assert_awaited_once_with(-1001, 42)
        self.assertIn("profile_nsfw_blocked", flag_mock.await_args.kwargs["reason"])

    async def test_handle_passive_moderation_trusted_destination_enforces_profile_nsfw(self) -> None:
        fake_redis = _FakeRedis()
        fake_redis.exists = AsyncMock(return_value=False)
        check_light_mock = AsyncMock(return_value="clean")

        with (
            patch.object(moderation, "redis_client", fake_redis),
            patch.object(moderation, "settings", types.SimpleNamespace(MODERATION_ADMIN_EXEMPT=False, MODERATION_PROFILE_NSFW_ENFORCE=True, MOD_ALERT_THROTTLE_SECONDS=60, MOD_LIGHT_TIMEOUT=2.0, MOD_DEEP_TIMEOUT=5.0, MOD_DEEP_TEXT_THRESHOLD=400, ALLOWED_GROUP_IDS=[-1001], COMMENT_SOURCE_CHANNEL_IDS=[])),
            patch.object(moderation, "_is_profile_nsfw", AsyncMock(return_value=True)) as nsfw_mock,
            patch.object(moderation, "_cleanup_user_history_and_mute", AsyncMock()) as cleanup_mock,
            patch.object(moderation, "_delete_message_safe", AsyncMock(return_value=True)) as delete_mock,
            patch.object(moderation, "_flag", AsyncMock()) as flag_mock,
            patch.object(moderation, "check_light", check_light_mock),
            patch.object(moderation, "check_deep", AsyncMock(return_value=False)),
            patch.object(moderation, "get_targets", return_value=[]),
            patch.object(moderation, "extract_urls", return_value=[]),
            patch.object(moderation, "contains_telegram_obfuscated", return_value=False),
            patch.object(moderation, "contains_any_link_obfuscated", return_value=False),
            patch.object(moderation, "_is_new_user", AsyncMock(return_value=False)),
            patch.object(moderation, "analytics_record_moderation", AsyncMock()),
        ):
            status = await moderation.handle_passive_moderation(
                chat_id=-1001,
                message=None,
                text="hello",
                entities=[],
                source="user",
                user_id=42,
                message_id=77,
            )

        self.assertEqual(status, "blocked")
        nsfw_mock.assert_awaited_once_with(42)
        check_light_mock.assert_not_awaited()
        delete_mock.assert_awaited_once_with(-1001, 77)
        cleanup_mock.assert_awaited_once_with(-1001, 42)
        fake_redis.set.assert_any_await("mod:profile_nsfw_blocked:-1001:42", 1)
        self.assertIn("profile_nsfw", flag_mock.await_args.kwargs["reason"])

    async def test_handle_passive_moderation_profile_nsfw_check_runs_in_worker(self) -> None:
        fake_redis = _FakeRedis()
        fake_redis.exists = AsyncMock(return_value=False)
        check_light_mock = AsyncMock(return_value="clean")

        with (
            patch.object(moderation, "redis_client", fake_redis),
            patch.object(moderation, "settings", types.SimpleNamespace(MODERATION_ADMIN_EXEMPT=False, MODERATION_PROFILE_NSFW_ENFORCE=True, MOD_ALERT_THROTTLE_SECONDS=60, MOD_LIGHT_TIMEOUT=2.0, MOD_DEEP_TIMEOUT=5.0, MOD_DEEP_TEXT_THRESHOLD=400)),
            patch.object(moderation, "_is_profile_nsfw", AsyncMock(return_value=True)) as nsfw_mock,
            patch.object(moderation, "_cleanup_user_history_and_mute", AsyncMock()) as cleanup_mock,
            patch.object(moderation, "_delete_message_safe", AsyncMock(return_value=True)) as delete_mock,
            patch.object(moderation, "_flag", AsyncMock()) as flag_mock,
            patch.object(moderation, "check_light", check_light_mock),
        ):
            status = await moderation.handle_passive_moderation(
                chat_id=-1001,
                message=None,
                text="hello",
                entities=[],
                source="user",
                user_id=42,
                message_id=77,
            )

        self.assertEqual(status, "blocked")
        nsfw_mock.assert_awaited_once_with(42)
        check_light_mock.assert_not_awaited()
        delete_mock.assert_awaited_once_with(-1001, 77)
        cleanup_mock.assert_awaited_once_with(-1001, 42)
        fake_redis.set.assert_any_await("mod:profile_nsfw_blocked:-1001:42", 1)
        self.assertIn("profile_nsfw", flag_mock.await_args.kwargs["reason"])

    async def test_handle_passive_moderation_passes_channel_source_as_is(self) -> None:
        fake_redis = _FakeRedis()
        check_light_mock = AsyncMock(return_value="clean")
        check_deep_mock = AsyncMock(return_value=False)

        with (
            patch.object(moderation, "redis_client", fake_redis),
            patch.object(moderation, "settings", types.SimpleNamespace(MODERATION_ADMIN_EXEMPT=False, MOD_ALERT_THROTTLE_SECONDS=60, MOD_LIGHT_TIMEOUT=2.0, MOD_DEEP_TIMEOUT=5.0, MOD_DEEP_TEXT_THRESHOLD=400)),
            patch.object(moderation, "get_targets", return_value=[]),
            patch.object(moderation, "check_light", check_light_mock),
            patch.object(moderation, "check_deep", check_deep_mock),
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
                image_b64="aGVsbG8=",
                image_mime="image/jpeg",
                source="channel",
                user_id=42,
                message_id=77,
            )

        self.assertEqual(status, "clean")
        self.assertEqual(check_light_mock.await_args.kwargs["source"], "channel")
        self.assertEqual(check_deep_mock.await_args.kwargs["source"], "channel")


    async def test_handle_passive_moderation_comment_context_skips_deep_when_light_clean(self) -> None:
        fake_redis = _FakeRedis()
        check_light_mock = AsyncMock(return_value="clean")
        check_deep_mock = AsyncMock(return_value=False)

        with (
            patch.object(moderation, "redis_client", fake_redis),
            patch.object(moderation, "settings", types.SimpleNamespace(MODERATION_ADMIN_EXEMPT=False, MOD_ALERT_THROTTLE_SECONDS=60, MOD_LIGHT_TIMEOUT=2.0, MOD_DEEP_TIMEOUT=5.0, MOD_DEEP_TEXT_THRESHOLD=400)),
            patch.object(moderation, "get_targets", return_value=[]),
            patch.object(moderation, "check_light", check_light_mock),
            patch.object(moderation, "check_deep", check_deep_mock),
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
                image_b64="aGVsbG8=",
                image_mime="image/jpeg",
                source="user",
                user_id=42,
                message_id=77,
                is_comment_context=True,
            )

        self.assertEqual(status, "clean")
        check_light_mock.assert_awaited_once()
        check_deep_mock.assert_not_awaited()

    async def test_handle_passive_moderation_group_context_runs_deep_when_risky(self) -> None:
        fake_redis = _FakeRedis()
        check_light_mock = AsyncMock(return_value="clean")
        check_deep_mock = AsyncMock(return_value=False)

        with (
            patch.object(moderation, "redis_client", fake_redis),
            patch.object(moderation, "settings", types.SimpleNamespace(MODERATION_ADMIN_EXEMPT=False, MOD_ALERT_THROTTLE_SECONDS=60, MOD_LIGHT_TIMEOUT=2.0, MOD_DEEP_TIMEOUT=5.0, MOD_DEEP_TEXT_THRESHOLD=400)),
            patch.object(moderation, "get_targets", return_value=[]),
            patch.object(moderation, "check_light", check_light_mock),
            patch.object(moderation, "check_deep", check_deep_mock),
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
                image_b64="aGVsbG8=",
                image_mime="image/jpeg",
                source="user",
                user_id=42,
                message_id=77,
                is_comment_context=False,
            )

        self.assertEqual(status, "clean")
        check_light_mock.assert_awaited_once()
        check_deep_mock.assert_awaited_once()


    async def test_handle_passive_moderation_skips_private_chat_by_chat_id_user_id(self) -> None:
        fake_redis = _FakeRedis()
        check_light_mock = AsyncMock(return_value="flood")
        check_deep_mock = AsyncMock(return_value=True)

        with (
            patch.object(moderation, "redis_client", fake_redis),
            patch.object(moderation, "settings", types.SimpleNamespace(MODERATION_ADMIN_EXEMPT=False, MOD_ALERT_THROTTLE_SECONDS=60, MOD_LIGHT_TIMEOUT=2.0, MOD_DEEP_TIMEOUT=5.0, MOD_DEEP_TEXT_THRESHOLD=400)),
            patch.object(moderation, "check_light", check_light_mock),
            patch.object(moderation, "check_deep", check_deep_mock),
        ):
            status = await moderation.handle_passive_moderation(
                chat_id=42,
                message=None,
                text="hello",
                entities=[],
                source="user",
                user_id=42,
                message_id=77,
            )

        self.assertEqual(status, "clean")
        check_light_mock.assert_not_awaited()
        check_deep_mock.assert_not_awaited()


    async def test_handle_passive_moderation_skips_private_chat_by_message_type(self) -> None:
        fake_redis = _FakeRedis()
        check_light_mock = AsyncMock(return_value="flood")
        check_deep_mock = AsyncMock(return_value=True)
        message = types.SimpleNamespace(
            chat=types.SimpleNamespace(id=42, type=moderation.ChatType.PRIVATE),
            from_user=types.SimpleNamespace(id=42),
            message_id=77,
        )

        with (
            patch.object(moderation, "redis_client", fake_redis),
            patch.object(moderation, "settings", types.SimpleNamespace(MODERATION_ADMIN_EXEMPT=False, MOD_ALERT_THROTTLE_SECONDS=60, MOD_LIGHT_TIMEOUT=2.0, MOD_DEEP_TIMEOUT=5.0, MOD_DEEP_TEXT_THRESHOLD=400)),
            patch.object(moderation, "check_light", check_light_mock),
            patch.object(moderation, "check_deep", check_deep_mock),
        ):
            status = await moderation.handle_passive_moderation(
                chat_id=42,
                message=message,
                text="hello",
                entities=[],
                source="user",
                user_id=42,
                message_id=77,
            )

        self.assertEqual(status, "clean")
        check_light_mock.assert_not_awaited()
        check_deep_mock.assert_not_awaited()


    async def test_handle_passive_moderation_alert_includes_ai_category_for_toxic(self) -> None:
        fake_redis = _FakeRedis()
        send_alert_mock = AsyncMock()

        with (
            patch.object(moderation, "redis_client", fake_redis),
            patch.object(moderation, "settings", types.SimpleNamespace(MODERATION_ADMIN_EXEMPT=False, MOD_ALERT_THROTTLE_SECONDS=60, MOD_LIGHT_TIMEOUT=2.0, MOD_DEEP_TIMEOUT=5.0, MOD_DEEP_TEXT_THRESHOLD=400, MODERATOR_ADMIN_CACHE_TTL_SECONDS=86400, MODERATION_NOTIFY_ADMINS_ON_AI_FLAGS=True)),
            patch.object(moderation, "get_targets", return_value=[999]),
            patch.object(moderation, "check_light", AsyncMock(return_value="toxic")),
            patch.object(moderation, "get_last_ai_moderation_category", return_value="hate"),
            patch.object(moderation, "check_deep", AsyncMock(return_value=False)),
            patch.object(moderation, "extract_urls", return_value=[]),
            patch.object(moderation, "contains_telegram_obfuscated", return_value=False),
            patch.object(moderation, "contains_any_link_obfuscated", return_value=False),
            patch.object(moderation, "_is_new_user", AsyncMock(return_value=False)),
            patch.object(moderation, "analytics_record_moderation", AsyncMock()),
            patch.object(moderation, "_send_alert_with_actions", send_alert_mock),
        ):
            status = await moderation.handle_passive_moderation(
                chat_id=-100200,
                message=None,
                text="hello",
                entities=[],
                image_b64=None,
                source="user",
                user_id=42,
                message_id=77,
                is_comment_context=False,
            )
            await asyncio.sleep(0)

        self.assertEqual(status, "flagged")
        sent_text = send_alert_mock.await_args.kwargs["text"]
        self.assertIn("AI moderation policy violation (hate)", sent_text)


    async def test_handle_passive_moderation_alert_includes_chat_name_and_generic_ai_reason(self) -> None:
        fake_redis = _FakeRedis()
        send_alert_mock = AsyncMock()

        with (
            patch.object(moderation, "redis_client", fake_redis),
            patch.object(moderation, "settings", types.SimpleNamespace(MODERATION_ADMIN_EXEMPT=False, MOD_ALERT_THROTTLE_SECONDS=60, MOD_LIGHT_TIMEOUT=2.0, MOD_DEEP_TIMEOUT=5.0, MOD_DEEP_TEXT_THRESHOLD=400, MODERATOR_ADMIN_CACHE_TTL_SECONDS=86400, MODERATION_NOTIFY_ADMINS_ON_AI_FLAGS=True)),
            patch.object(moderation, "get_targets", return_value=[999]),
            patch.object(moderation, "check_light", AsyncMock(return_value="toxic")),
            patch.object(moderation, "check_deep", AsyncMock(return_value=False)),
            patch.object(moderation, "extract_urls", return_value=[]),
            patch.object(moderation, "contains_telegram_obfuscated", return_value=False),
            patch.object(moderation, "contains_any_link_obfuscated", return_value=False),
            patch.object(moderation, "_is_new_user", AsyncMock(return_value=False)),
            patch.object(moderation, "analytics_record_moderation", AsyncMock()),
            patch.object(moderation, "_send_alert_with_actions", send_alert_mock),
            patch.object(moderation.bot, "get_chat", AsyncMock(return_value=types.SimpleNamespace(title="Main Discussion", username=None, full_name=None))),
        ):
            status = await moderation.handle_passive_moderation(
                chat_id=-100200,
                message=None,
                text="hello",
                entities=[],
                image_b64=None,
                source="user",
                user_id=42,
                message_id=77,
                is_comment_context=False,
            )
            await asyncio.sleep(0)

        self.assertEqual(status, "flagged")
        self.assertTrue(send_alert_mock.await_count >= 1)
        sent_text = send_alert_mock.await_args.kwargs["text"]
        self.assertIn("Main Discussion", sent_text)
        self.assertIn("AI moderation policy violation", sent_text)


    async def test_handle_passive_moderation_uses_payload_chat_title_without_lookup(self) -> None:
        fake_redis = _FakeRedis()
        send_alert_mock = AsyncMock()
        get_chat_mock = AsyncMock(return_value=types.SimpleNamespace(title="ShouldNotBeUsed", username=None, full_name=None))

        with (
            patch.object(moderation, "redis_client", fake_redis),
            patch.object(moderation, "settings", types.SimpleNamespace(MODERATION_ADMIN_EXEMPT=False, MOD_ALERT_THROTTLE_SECONDS=60, MOD_LIGHT_TIMEOUT=2.0, MOD_DEEP_TIMEOUT=5.0, MOD_DEEP_TEXT_THRESHOLD=400, MODERATOR_ADMIN_CACHE_TTL_SECONDS=86400, MODERATION_NOTIFY_ADMINS_ON_AI_FLAGS=True)),
            patch.object(moderation, "get_targets", return_value=[999]),
            patch.object(moderation, "check_light", AsyncMock(return_value="toxic")),
            patch.object(moderation, "check_deep", AsyncMock(return_value=False)),
            patch.object(moderation, "extract_urls", return_value=[]),
            patch.object(moderation, "contains_telegram_obfuscated", return_value=False),
            patch.object(moderation, "contains_any_link_obfuscated", return_value=False),
            patch.object(moderation, "_is_new_user", AsyncMock(return_value=False)),
            patch.object(moderation, "analytics_record_moderation", AsyncMock()),
            patch.object(moderation, "_send_alert_with_actions", send_alert_mock),
            patch.object(moderation.bot, "get_chat", get_chat_mock),
        ):
            status = await moderation.handle_passive_moderation(
                chat_id=-100200,
                message=None,
                text="hello",
                entities=[],
                image_b64=None,
                source="user",
                user_id=42,
                message_id=77,
                is_comment_context=False,
                chat_title="Team Chat",
            )
            await asyncio.sleep(0)

        self.assertEqual(status, "flagged")
        sent_text = send_alert_mock.await_args.kwargs["text"]
        self.assertIn("Team Chat", sent_text)
        get_chat_mock.assert_not_awaited()



    async def test_handle_passive_moderation_toxic_does_not_delete_flagged_when_disabled(self) -> None:
        fake_redis = _FakeRedis()

        with (
            patch.object(moderation, "redis_client", fake_redis),
            patch.object(moderation, "settings", types.SimpleNamespace(MODERATION_ADMIN_EXEMPT=False, MOD_ALERT_THROTTLE_SECONDS=60, MOD_LIGHT_TIMEOUT=2.0, MOD_DEEP_TIMEOUT=5.0, MOD_DEEP_TEXT_THRESHOLD=400, MODERATION_NOTIFY_ADMINS_ON_AI_FLAGS=False, MODERATION_DELETE_FLAGGED=False)),
            patch.object(moderation, "get_targets", return_value=[999]),
            patch.object(moderation, "check_light", AsyncMock(return_value="toxic")),
            patch.object(moderation, "check_deep", AsyncMock(return_value=False)),
            patch.object(moderation, "extract_urls", return_value=[]),
            patch.object(moderation, "contains_telegram_obfuscated", return_value=False),
            patch.object(moderation, "contains_any_link_obfuscated", return_value=False),
            patch.object(moderation, "_is_new_user", AsyncMock(return_value=False)),
            patch.object(moderation, "analytics_record_moderation", AsyncMock()),
            patch.object(moderation, "_delete_message_safe", AsyncMock(return_value=True)) as delete_mock,
            patch.object(moderation, "_send_alert_with_actions", AsyncMock()) as send_alert_mock,
        ):
            status = await moderation.handle_passive_moderation(
                chat_id=100,
                message=None,
                text="toxic text",
                entities=[],
                source="user",
                user_id=42,
                message_id=77,
            )

        self.assertEqual(status, "flagged")
        delete_mock.assert_not_awaited()
        send_alert_mock.assert_not_called()

    async def test_handle_passive_moderation_toxic_deletes_flagged_when_enabled(self) -> None:
        fake_redis = _FakeRedis()

        with (
            patch.object(moderation, "redis_client", fake_redis),
            patch.object(moderation, "settings", types.SimpleNamespace(MODERATION_ADMIN_EXEMPT=False, MOD_ALERT_THROTTLE_SECONDS=60, MOD_LIGHT_TIMEOUT=2.0, MOD_DEEP_TIMEOUT=5.0, MOD_DEEP_TEXT_THRESHOLD=400, MODERATION_NOTIFY_ADMINS_ON_AI_FLAGS=False, MODERATION_DELETE_FLAGGED=True)),
            patch.object(moderation, "get_targets", return_value=[999]),
            patch.object(moderation, "check_light", AsyncMock(return_value="toxic")),
            patch.object(moderation, "check_deep", AsyncMock(return_value=False)),
            patch.object(moderation, "extract_urls", return_value=[]),
            patch.object(moderation, "contains_telegram_obfuscated", return_value=False),
            patch.object(moderation, "contains_any_link_obfuscated", return_value=False),
            patch.object(moderation, "_is_new_user", AsyncMock(return_value=False)),
            patch.object(moderation, "analytics_record_moderation", AsyncMock()),
            patch.object(moderation, "_delete_message_safe", AsyncMock(return_value=True)) as delete_mock,
            patch.object(moderation, "_send_alert_with_actions", AsyncMock()) as send_alert_mock,
        ):
            status = await moderation.handle_passive_moderation(
                chat_id=100,
                message=None,
                text="toxic text",
                entities=[],
                source="user",
                user_id=42,
                message_id=77,
            )

        self.assertEqual(status, "flagged")
        delete_mock.assert_awaited_once_with(100, 77)
        send_alert_mock.assert_not_called()

    async def test_handle_passive_moderation_blocked_delete_unchanged(self) -> None:
        fake_redis = _FakeRedis()

        with (
            patch.object(moderation, "redis_client", fake_redis),
            patch.object(moderation, "settings", types.SimpleNamespace(MODERATION_ADMIN_EXEMPT=False, MOD_ALERT_THROTTLE_SECONDS=60, MOD_LIGHT_TIMEOUT=2.0, MOD_DEEP_TIMEOUT=5.0, MOD_DEEP_TEXT_THRESHOLD=400, MODERATION_NOTIFY_ADMINS_ON_AI_FLAGS=False, MODERATION_DELETE_FLAGGED=False, MODERATION_DELETE_BLOCKED=True)),
            patch.object(moderation, "get_targets", return_value=[999]),
            patch.object(moderation, "check_light", AsyncMock(return_value="toxic")),
            patch.object(moderation, "check_deep", AsyncMock(return_value=True)),
            patch.object(moderation, "extract_urls", return_value=[]),
            patch.object(moderation, "contains_telegram_obfuscated", return_value=False),
            patch.object(moderation, "contains_any_link_obfuscated", return_value=False),
            patch.object(moderation, "_is_new_user", AsyncMock(return_value=False)),
            patch.object(moderation, "analytics_record_moderation", AsyncMock()),
            patch.object(moderation, "_delete_message_safe", AsyncMock(return_value=True)) as delete_mock,
            patch.object(moderation, "_send_alert_with_actions", AsyncMock()) as send_alert_mock,
        ):
            status = await moderation.handle_passive_moderation(
                chat_id=100,
                message=None,
                text="toxic text",
                entities=[],
                image_b64="dGVzdA==",
                source="user",
                user_id=42,
                message_id=77,
            )

        self.assertEqual(status, "blocked")
        delete_mock.assert_awaited_once_with(100, 77)
        send_alert_mock.assert_not_called()

    async def test_handle_passive_moderation_returns_error_and_records_error_state_on_exception(self) -> None:
        fake_redis = _FakeRedis()
        analytics_mock = AsyncMock()

        with (
            patch.object(moderation, "redis_client", fake_redis),
            patch.object(moderation, "settings", types.SimpleNamespace(MODERATION_ADMIN_EXEMPT=False, MOD_ALERT_THROTTLE_SECONDS=60, MOD_LIGHT_TIMEOUT=2.0, MOD_DEEP_TIMEOUT=5.0, MOD_DEEP_TEXT_THRESHOLD=400)),
            patch.object(moderation, "get_targets", return_value=[]),
            patch.object(moderation, "check_light", AsyncMock(side_effect=Exception("boom"))),
            patch.object(moderation, "analytics_record_moderation", analytics_mock),
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

        self.assertEqual(status, "error")
        fake_redis.hset.assert_any_await(
            "mod:msg:100:77",
            mapping={
                "status": "error",
                "reason": "internal_error",
                "ts": unittest.mock.ANY,
                "user_id": 42,
            },
        )
        analytics_mock.assert_any_await(100, "error", "internal_error")


class ModerationAlertDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_cleanup_user_history_and_mute_only_restricts_user(self) -> None:
        with (
            patch.object(moderation, "_restrict_user_write_safe", AsyncMock(return_value=True)) as restrict_mock,
        ):
            await moderation._cleanup_user_history_and_mute(-1001, 42)

        restrict_mock.assert_awaited_once_with(-1001, 42)

    async def test_send_alert_with_actions_ignores_forbidden_targets_without_error_log(self) -> None:
        class _Forbidden(Exception):
            pass

        send_message = AsyncMock(side_effect=[_Forbidden("forbidden")])
        fake_bot = types.SimpleNamespace(send_message=send_message)

        with (
            patch.object(moderation, "bot", fake_bot),
            patch.object(moderation, "TelegramForbiddenError", _Forbidden),
            patch.object(moderation, "logger") as logger_mock,
        ):
            await moderation._send_alert_with_actions(
                [777],
                text="alert",
                chat_id=100,
                offender_id=42,
                msg_id=10,
            )

        logger_mock.info.assert_called_once()
        logger_mock.error.assert_not_called()

    async def test_send_alert_with_actions_logs_non_forbidden_failures_as_error(self) -> None:
        send_message = AsyncMock(side_effect=[RuntimeError("boom")])
        fake_bot = types.SimpleNamespace(send_message=send_message)

        with (
            patch.object(moderation, "bot", fake_bot),
            patch.object(moderation, "logger") as logger_mock,
        ):
            await moderation._send_alert_with_actions(
                [778],
                text="alert",
                chat_id=100,
                offender_id=42,
                msg_id=10,
            )

        logger_mock.error.assert_called_once()



if __name__ == "__main__":
    unittest.main()

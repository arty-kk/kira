import os
import asyncio
import unittest
from unittest.mock import patch

os.environ.setdefault("OPENAI_API_KEY", "test")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "12345:abcde")
os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://u:p@localhost/db")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("REDIS_URL_QUEUE", "redis://localhost:6379/1")
os.environ.setdefault("REDIS_URL_VECTOR", "redis://localhost:6379/2")
os.environ.setdefault("CELERY_BROKER_URL", "redis://localhost:6379/3")
os.environ.setdefault("TWITTER_API_KEY", "x")
os.environ.setdefault("TWITTER_API_SECRET", "x")
os.environ.setdefault("TWITTER_ACCESS_TOKEN", "x")
os.environ.setdefault("TWITTER_ACCESS_TOKEN_SECRET", "x")
os.environ.setdefault("TWITTER_BEARER_TOKEN", "x")

from app.config import settings
from app.tasks.celery_app import celery
from app.tasks.moderation import (
    _clear_moderation_inflight,
    _set_moderation_inflight,
    passive_moderate,
    profile_nsfw_scan,
    prepare_moderation_payload,
)


class ModerationCeleryConfigTests(unittest.TestCase):
    def test_task_routes_include_moderation_queue(self) -> None:
        routes = celery.conf.task_routes or {}
        self.assertEqual(celery.conf.task_default_queue, settings.CELERY_DEFAULT_QUEUE)
        self.assertIn("moderation.*", routes)
        self.assertEqual(routes["moderation.*"]["queue"], settings.CELERY_MODERATION_QUEUE)
        self.assertEqual(routes["media.preprocess_group_image"]["queue"], settings.CELERY_MEDIA_QUEUE)

    def test_passive_moderate_retry_and_limits(self) -> None:
        self.assertEqual(passive_moderate.max_retries, 3)
        self.assertEqual(passive_moderate.soft_time_limit, settings.MODERATION_TIMEOUT)
        self.assertEqual(passive_moderate.time_limit, settings.MODERATION_TIMEOUT + 5)
        self.assertTrue(passive_moderate.retry_backoff)
        self.assertTrue(passive_moderate.retry_jitter)


    def test_profile_nsfw_scan_writes_flagged_result(self) -> None:
        payload = {
            "result_key": "mod:profile_nsfw:result:test",
            "result_ttl": 42,
            "image_b64": "Zm9v",
            "image_mime": "image/jpeg",
        }

        with (
            patch("app.services.addons.passive_moderation.classify_profile_nsfw_fast", unittest.mock.AsyncMock(return_value=True)),
            patch.object(profile_nsfw_scan.__wrapped__.__globals__["consts"], "redis_client", type("RedisStub", (), {
                "set": unittest.mock.AsyncMock(return_value=True),
            })()),
        ):
            result = profile_nsfw_scan.run(payload)

        self.assertEqual(result, "flagged")

    def test_profile_nsfw_scan_invalid_payload_without_key(self) -> None:
        with self.assertLogs("app.tasks.moderation", level="WARNING") as logs:
            result = profile_nsfw_scan.run({"image_b64": "Zm9v"})

        self.assertEqual(result, "invalid_payload")
        self.assertTrue(any("missing result_key" in entry for entry in logs.output))

    def test_passive_moderate_forwards_comment_context_only(self) -> None:
        payload = {
            "chat_id": 1,
            "user_id": 2,
            "message_id": 3,
            "text": "hi",
            "entities": [],
            "source": "user",
            "is_channel_post": True,
            "is_comment_context": True,
        }

        async def _fake_handle(**kwargs):
            return "clean"

        with (
            patch("app.bot.handlers.moderation.handle_passive_moderation", side_effect=_fake_handle) as handle_mock,
        ):
            result = passive_moderate.run(payload)

        self.assertEqual(result, "clean")
        _, kwargs = handle_mock.call_args
        self.assertTrue(kwargs["is_comment_context"])
        self.assertNotIn("is_channel_post", kwargs)

    def test_passive_moderate_invalid_payload_returns_terminal_status(self) -> None:
        payload = {
            "chat_id": 1,
            "user_id": 2,
            "text": "hi",
        }

        with (
            patch("app.bot.handlers.moderation.handle_passive_moderation") as handle_mock,
            self.assertLogs("app.tasks.moderation", level="WARNING") as logs,
        ):
            result = passive_moderate.run(payload)

        self.assertEqual(result, "invalid_payload")
        handle_mock.assert_not_called()
        self.assertTrue(any("invalid payload" in entry for entry in logs.output))

    def test_passive_moderate_non_dict_payload_returns_terminal_status(self) -> None:
        with (
            patch("app.bot.handlers.moderation.handle_passive_moderation") as handle_mock,
            self.assertLogs("app.tasks.moderation", level="WARNING") as logs,
        ):
            result = passive_moderate.run("bad-payload")

        self.assertEqual(result, "invalid_payload")
        handle_mock.assert_not_called()
        self.assertTrue(any("payload must be dict" in entry for entry in logs.output))

    def test_passive_moderate_skips_when_prefilter_marker_exists(self) -> None:
        payload = {
            "chat_id": 100,
            "user_id": 200,
            "message_id": 300,
            "text": "hi",
            "entities": [],
            "source": "user",
        }

        def _fake_run_coro_sync(coro, timeout=None):
            close = getattr(coro, "close", None)
            if callable(close):
                close()
            return True

        with (
            patch("app.bot.handlers.moderation.handle_passive_moderation") as handle_mock,
            patch.dict(passive_moderate._orig_run.__globals__, {"run_coro_sync": _fake_run_coro_sync}),
        ):
            result = passive_moderate._orig_run(payload)

        self.assertEqual(result, "blocked")
        handle_mock.assert_not_called()

    def test_passive_moderate_valid_payload_keeps_contract(self) -> None:
        payload = {
            "chat_id": "100",
            "user_id": "200",
            "message_id": "300",
            "text": "hi",
            "entities": [],
            "source": "user",
        }

        async def _fake_handle(**kwargs):
            return "clean"

        with (
            patch("app.bot.handlers.moderation.handle_passive_moderation", side_effect=_fake_handle) as handle_mock,
        ):
            result = passive_moderate.run(payload)

        self.assertEqual(result, "clean")
        _, kwargs = handle_mock.call_args
        self.assertEqual(kwargs["chat_id"], 100)
        self.assertEqual(kwargs["user_id"], 200)
        self.assertEqual(kwargs["message_id"], 300)


    def test_passive_moderate_valid_channel_user_id_keeps_contract(self) -> None:
        payload = {
            "chat_id": -100123,
            "user_id": -100456,
            "message_id": 300,
            "text": "hi",
            "entities": [],
            "source": "channel",
        }

        async def _fake_handle(**kwargs):
            return "clean"

        with (
            patch("app.bot.handlers.moderation.handle_passive_moderation", side_effect=_fake_handle) as handle_mock,
        ):
            result = passive_moderate.run(payload)

        self.assertEqual(result, "clean")
        _, kwargs = handle_mock.call_args
        self.assertEqual(kwargs["chat_id"], -100123)
        self.assertEqual(kwargs["user_id"], -100456)
        self.assertEqual(kwargs["message_id"], 300)


    def test_passive_moderate_sets_and_clears_inflight_marker(self) -> None:
        payload = {
            "chat_id": 100,
            "user_id": 200,
            "message_id": 300,
            "text": "hi",
            "entities": [],
            "source": "user",
        }

        async def _fake_handle(**kwargs):
            return "clean"

        def _fake_run(coro):
            return asyncio.run(coro)

        redis_set_mock = unittest.mock.AsyncMock(return_value=True)
        redis_eval_mock = unittest.mock.AsyncMock(return_value=1)

        with (
            patch("app.bot.handlers.moderation.handle_passive_moderation", side_effect=_fake_handle),
                        patch.object(passive_moderate.__wrapped__.__globals__["consts"], "redis_client", type("RedisStub", (), {
                "set": redis_set_mock,
                "eval": redis_eval_mock,
                "incrby": unittest.mock.AsyncMock(return_value=1),
                "incr": unittest.mock.AsyncMock(return_value=1),
            })()),
        ):
            result = passive_moderate.run(payload)

        self.assertEqual(result, "clean")
        redis_set_mock.assert_awaited_once_with(
            "mod:inflight:100:300",
            unittest.mock.ANY,
            ex=unittest.mock.ANY,
            nx=True,
        )
        redis_eval_mock.assert_awaited_once_with(
            unittest.mock.ANY,
            1,
            "mod:inflight:100:300",
            unittest.mock.ANY,
        )

    def test_passive_moderate_raises_when_handler_fails(self) -> None:
        payload = {
            "chat_id": 100,
            "user_id": 200,
            "message_id": 300,
            "text": "hi",
            "entities": [],
            "source": "user",
        }

        async def _raise_handle(**kwargs):
            raise RuntimeError("mod:msg persistence failed")

        def _fake_run(coro):
            return asyncio.run(coro)

        with (
            patch("app.bot.handlers.moderation.handle_passive_moderation", side_effect=_raise_handle),
                    ):
            with self.assertRaises(RuntimeError):
                passive_moderate.run(payload)

    def test_prepare_moderation_payload_drops_oversized_json(self) -> None:
        oversized = {"text": "x", "image_b64": "a" * (settings.CELERY_MODERATION_MAX_PAYLOAD_BYTES + 128)}
        prepared = prepare_moderation_payload(oversized, context="test")
        self.assertNotIn("image_b64", prepared)

    def test_prepare_moderation_payload_drops_invalid_base64_and_logs_reason(self) -> None:
        payload = {
            "text": "x",
            "image_b64": "aGVs*bG8=",
            "image_mime": "image/png",
        }
        with self.assertLogs("app.tasks.moderation", level="WARNING") as logs:
            prepared = prepare_moderation_payload(payload, context="api")

        self.assertNotIn("image_b64", prepared)
        self.assertNotIn("image_mime", prepared)
        self.assertTrue(any("invalid base64" in entry and "api" in entry for entry in logs.output))

    def test_set_moderation_inflight_suppresses_traceback_for_closed_transport_runtime_error(self) -> None:
        redis_stub = type(
            "RedisStub",
            (),
            {
                "set": unittest.mock.AsyncMock(
                    side_effect=RuntimeError("unable to perform operation on <TCPTransport closed=True>; the handler is closed")
                )
            },
        )()

        with (
            patch("app.tasks.moderation.consts.redis_client", redis_stub),
            self.assertLogs("app.tasks.moderation", level="WARNING") as logs,
        ):
            result = asyncio.run(_set_moderation_inflight("mod:inflight:1:2", "token"))

        self.assertFalse(result)
        self.assertTrue(any("failed to set moderation inflight" in entry for entry in logs.output))
        self.assertFalse(any("Traceback" in entry for entry in logs.output))

    def test_clear_moderation_inflight_suppresses_traceback_for_closed_transport_runtime_error(self) -> None:
        redis_stub = type(
            "RedisStub",
            (),
            {
                "eval": unittest.mock.AsyncMock(
                    side_effect=RuntimeError("unable to perform operation on <TCPTransport closed=True>; the handler is closed")
                )
            },
        )()

        with (
            patch("app.tasks.moderation.consts.redis_client", redis_stub),
            self.assertLogs("app.tasks.moderation", level="WARNING") as logs,
        ):
            asyncio.run(_clear_moderation_inflight("mod:inflight:1:2", "token"))

        self.assertTrue(any("failed to clear moderation inflight" in entry for entry in logs.output))
        self.assertFalse(any("Traceback" in entry for entry in logs.output))

    def test_set_moderation_inflight_retries_once_after_closed_transport_error(self) -> None:
        redis_stub = type(
            "RedisStub",
            (),
            {
                "set": unittest.mock.AsyncMock(
                    side_effect=[
                        RuntimeError("unable to perform operation on <TCPTransport closed=True>; the handler is closed"),
                        True,
                    ]
                ),
                "aclose": unittest.mock.AsyncMock(return_value=None),
            },
        )()

        with patch("app.tasks.moderation.consts.redis_client", redis_stub):
            result = asyncio.run(_set_moderation_inflight("mod:inflight:1:2", "token"))

        self.assertTrue(result)
        self.assertEqual(redis_stub.set.await_count, 2)
        redis_stub.aclose.assert_awaited_once()

    def test_clear_moderation_inflight_retries_once_after_closed_transport_error(self) -> None:
        redis_stub = type(
            "RedisStub",
            (),
            {
                "eval": unittest.mock.AsyncMock(
                    side_effect=[
                        RuntimeError("unable to perform operation on <TCPTransport closed=True>; the handler is closed"),
                        1,
                    ]
                ),
                "aclose": unittest.mock.AsyncMock(return_value=None),
            },
        )()

        with patch("app.tasks.moderation.consts.redis_client", redis_stub):
            asyncio.run(_clear_moderation_inflight("mod:inflight:1:2", "token"))

        self.assertEqual(redis_stub.eval.await_count, 2)
        redis_stub.aclose.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()

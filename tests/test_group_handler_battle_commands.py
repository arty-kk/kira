import types
import unittest
from unittest.mock import AsyncMock, Mock, patch

from aiogram.enums import MessageEntityType

from app.bot.handlers import group


class GroupBattleCommandRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_battle_command_addressing_and_side_effects(self) -> None:
        scenarios = [
            ("/battle", "MyBot", True),
            ("/battle@MyBot", "MyBot", True),
            ("/battle@other_bot", "MyBot", False),
        ]

        for text, bot_username, should_launch in scenarios:
            with self.subTest(text=text):
                redis_mock = types.SimpleNamespace(
                    set=AsyncMock(return_value=True),
                    sismember=AsyncMock(return_value=False),
                    hget=AsyncMock(return_value=None),
                    exists=AsyncMock(return_value=False),
                )
                delay_mock = Mock()
                delete_mock = AsyncMock()
                send_mock = AsyncMock()

                message = types.SimpleNamespace(
                    chat=types.SimpleNamespace(id=101),
                    from_user=types.SimpleNamespace(id=202, is_bot=False),
                    text=text,
                    caption=None,
                    entities=[types.SimpleNamespace(type=MessageEntityType.BOT_COMMAND, offset=0, length=len(text))],
                    caption_entities=[],
                    reply_to_message=None,
                    message_id=303,
                )

                with (
                    patch.object(group, "redis_client", redis_mock),
                    patch.object(group.consts, "BOT_ID", 999),
                    patch.object(group.consts, "BOT_USERNAME", bot_username),
                    patch.object(group.battle_launch_task, "delay", delay_mock),
                    patch.object(group, "delete_message_safe", delete_mock),
                    patch.object(group, "send_message_safe", send_mock),
                    patch.object(group.bot, "get_chat_member", AsyncMock(return_value=types.SimpleNamespace(status="member"))),
                ):
                    handled = await group._maybe_handle_battle(
                        message,
                        trigger="mention",
                    )

                self.assertEqual(handled, should_launch)
                if should_launch:
                    redis_mock.set.assert_awaited_once_with(
                        "battle:req:101:202:999",
                        1,
                        nx=True,
                        ex=group.BATTLE_ENQUEUE_DEDUP_TTL_SECONDS,
                    )
                    delay_mock.assert_called_once_with("202", "999", 101)
                    delete_mock.assert_awaited_once_with(group.bot, 101, 303)
                    send_mock.assert_not_called()
                else:
                    redis_mock.set.assert_not_awaited()
                    delay_mock.assert_not_called()
                    delete_mock.assert_not_awaited()
                    send_mock.assert_not_called()


    async def test_battle_blocks_banned_opponent(self) -> None:
        redis_mock = types.SimpleNamespace(
            set=AsyncMock(return_value=True),
            sismember=AsyncMock(return_value=False),
            hget=AsyncMock(return_value="303"),
            exists=AsyncMock(return_value=False),
        )
        delay_mock = Mock()
        send_mock = AsyncMock()

        message = types.SimpleNamespace(
            chat=types.SimpleNamespace(id=101),
            from_user=types.SimpleNamespace(id=202, is_bot=False),
            text="/battle @target",
            caption=None,
            entities=[
                types.SimpleNamespace(type=MessageEntityType.BOT_COMMAND, offset=0, length=7),
                types.SimpleNamespace(type=MessageEntityType.MENTION, offset=8, length=7),
            ],
            caption_entities=[],
            reply_to_message=None,
            message_id=303,
        )

        with (
            patch.object(group, "redis_client", redis_mock),
            patch.object(group.consts, "BOT_ID", 999),
            patch.object(group.consts, "BOT_USERNAME", "MyBot"),
            patch.object(group.battle_launch_task, "delay", delay_mock),
            patch.object(group, "delete_message_safe", AsyncMock()),
            patch.object(group, "send_message_safe", send_mock),
            patch.object(group.bot, "get_chat_member", AsyncMock(return_value=types.SimpleNamespace(status="kicked"))),
        ):
            handled = await group._maybe_handle_battle(message, trigger="mention")

        self.assertTrue(handled)
        redis_mock.set.assert_not_awaited()
        delay_mock.assert_not_called()
        send_mock.assert_awaited_once()

    async def test_battle_dedup_blocks_double_enqueue(self) -> None:
        redis_mock = types.SimpleNamespace(
            set=AsyncMock(side_effect=[True, False]),
            sismember=AsyncMock(return_value=False),
            hget=AsyncMock(return_value=None),
            exists=AsyncMock(return_value=False),
        )
        delay_mock = Mock()

        with (
            patch.object(group, "redis_client", redis_mock),
            patch.object(group.consts, "BOT_ID", 999),
            patch.object(group.consts, "BOT_USERNAME", "MyBot"),
            patch.object(group.battle_launch_task, "delay", delay_mock),
            patch.object(group, "delete_message_safe", AsyncMock()),
            patch.object(group, "send_message_safe", AsyncMock()),
            patch.object(group.bot, "get_chat_member", AsyncMock(return_value=types.SimpleNamespace(status="member"))),
        ):
            for msg_id in (1, 2):
                message = types.SimpleNamespace(
                    chat=types.SimpleNamespace(id=101),
                    from_user=types.SimpleNamespace(id=202, is_bot=False),
                    text="/battle",
                    caption=None,
                    entities=[types.SimpleNamespace(type=MessageEntityType.BOT_COMMAND, offset=0, length=7)],
                    caption_entities=[],
                    reply_to_message=None,
                    message_id=msg_id,
                )
                handled = await group._maybe_handle_battle(
                    message,
                    trigger="mention",
                )
                self.assertTrue(handled)

        self.assertEqual(delay_mock.call_count, 1)


class GroupBattleOtherBotCommandIgnoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_on_group_message_ignores_battle_addressed_to_other_bot(self) -> None:
        message = types.SimpleNamespace(
            chat=types.SimpleNamespace(id=777, username="allowed-group"),
            message_id=101,
            text="/battle@other_bot",
            caption=None,
            entities=[types.SimpleNamespace(type=MessageEntityType.BOT_COMMAND, offset=0, length=17)],
            caption_entities=[],
            reply_to_message=None,
            from_user=types.SimpleNamespace(id=42, is_bot=False),
            sender_chat=None,
        )

        with (
            patch.object(group, "_is_chat_allowed", return_value=True),
            patch.object(group, "_first_delivery", AsyncMock(return_value=True)),
            patch.object(group, "_update_presence", AsyncMock()),
            patch.object(group, "record_activity", AsyncMock()),
            patch.object(group, "apply_moderation_filters", AsyncMock(return_value=False)),
            patch.object(group, "_is_channel_post", return_value=False),
            patch.object(group, "_reply_gate_requires_mention", return_value=False),
            patch.object(group, "_extract_entities", return_value=[]),
            patch.object(group, "split_context_text", return_value=("/battle@other_bot", "/battle@other_bot")),
            patch.object(group.consts, "BOT_USERNAME", "MyBot"),
            patch.object(group, "_ensure_daily_limit", AsyncMock()) as daily_limit_mock,
            patch.object(group, "_store_context", AsyncMock()) as store_context_mock,
            patch.object(group, "_maybe_handle_battle", AsyncMock()) as maybe_battle_mock,
            patch.object(group.asyncio, "create_task", lambda _coro: _coro.close()),
        ):
            await group.on_group_message(message)

        maybe_battle_mock.assert_not_awaited()
        daily_limit_mock.assert_not_awaited()
        store_context_mock.assert_not_awaited()


class GroupModerationGateTests(unittest.IsolatedAsyncioTestCase):
    async def test_on_group_message_stops_pipeline_when_moderation_handles_message(self) -> None:
        message = types.SimpleNamespace(
            chat=types.SimpleNamespace(id=777, username="allowed-group"),
            message_id=101,
            text="/battle",
            caption=None,
            entities=[types.SimpleNamespace(type=MessageEntityType.BOT_COMMAND, offset=0, length=7)],
            caption_entities=[],
            reply_to_message=None,
            from_user=types.SimpleNamespace(id=42, is_bot=False),
            sender_chat=None,
        )

        with (
            patch.object(group, "_is_chat_allowed", return_value=True),
            patch.object(group, "_first_delivery", AsyncMock(return_value=True)),
            patch.object(group, "_update_presence", AsyncMock()),
            patch.object(group, "record_activity", AsyncMock()),
            patch.object(group, "apply_moderation_filters", AsyncMock(return_value=True)),
            patch.object(group, "_is_channel_post", return_value=False),
            patch.object(group, "_reply_gate_requires_mention", return_value=False),
            patch.object(group, "_extract_entities", return_value=[]),
            patch.object(group, "split_context_text", return_value=("/battle", "/battle")),
            patch.object(group, "_ensure_daily_limit", AsyncMock()) as daily_limit_mock,
            patch.object(group, "_store_context", AsyncMock()) as store_context_mock,
            patch.object(group, "_maybe_handle_battle", AsyncMock()) as maybe_battle_mock,
            patch.object(group.asyncio, "create_task", lambda _coro: _coro.close()),
        ):
            await group.on_group_message(message)

        maybe_battle_mock.assert_not_awaited()
        daily_limit_mock.assert_not_awaited()
        store_context_mock.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()

import types
import unittest
from unittest.mock import AsyncMock, Mock, patch

from aiogram.enums import ChatType, MessageEntityType

from app.bot.handlers import battle


class BattleHandlersAllowlistTests(unittest.IsolatedAsyncioTestCase):
    async def test_cmd_group_battle_ignores_unauthorized_chat_without_side_effects(self) -> None:
        message = types.SimpleNamespace(
            chat=types.SimpleNamespace(id=999, username="forbidden_group"),
            from_user=types.SimpleNamespace(id=42),
            text="/battle",
            caption=None,
            entities=[],
            caption_entities=[],
            reply_to_message=None,
            reply=AsyncMock(),
            delete=AsyncMock(),
        )
        redis_mock = types.SimpleNamespace(
            set=AsyncMock(),
            hget=AsyncMock(),
            sismember=AsyncMock(),
        )

        with (
            patch.object(battle, "settings", types.SimpleNamespace(ALLOWED_GROUP_IDS=[123])),
            patch.object(battle, "redis_client", redis_mock),
            patch.object(battle.battle_launch_task, "delay", Mock()) as delay_mock,
        ):
            await battle.cmd_group_battle(message)

        delay_mock.assert_not_called()
        redis_mock.set.assert_not_called()
        redis_mock.hget.assert_not_called()
        redis_mock.sismember.assert_not_called()
        message.reply.assert_not_called()
        message.delete.assert_not_called()


class BattleHandlersCachedUserMapTests(unittest.IsolatedAsyncioTestCase):
    async def test_cmd_group_battle_resolves_cached_mention_from_string_and_bytes(self) -> None:
        for cached_value in ("123", b"123"):
            with self.subTest(cached_value=cached_value):
                message = types.SimpleNamespace(
                    chat=types.SimpleNamespace(id=123, username="allowed_group", type=ChatType.GROUP),
                    from_user=types.SimpleNamespace(id=42),
                    text="/battle @enemy",
                    caption=None,
                    entities=[types.SimpleNamespace(type=MessageEntityType.MENTION, offset=8, length=6)],
                    caption_entities=[],
                    reply_to_message=None,
                    reply=AsyncMock(),
                    delete=AsyncMock(),
                )
                redis_mock = types.SimpleNamespace(
                    set=AsyncMock(return_value=True),
                    hget=AsyncMock(return_value=cached_value),
                    sismember=AsyncMock(return_value=False),
                )

                with (
                    patch.object(battle, "settings", types.SimpleNamespace(ALLOWED_GROUP_IDS=[123])),
                    patch.object(battle, "redis_client", redis_mock),
                    patch.object(battle.bot, "get_chat", AsyncMock()) as get_chat_mock,
                    patch.object(battle.battle_launch_task, "delay", Mock()) as delay_mock,
                ):
                    await battle.cmd_group_battle(message)

                delay_mock.assert_called_once_with("42", "123", 123)
                get_chat_mock.assert_not_called()

    async def test_resolve_stats_target_user_id_supports_cached_string_and_bytes(self) -> None:
        for cached_value in ("123", b"123"):
            with self.subTest(cached_value=cached_value):
                message = types.SimpleNamespace(
                    chat=types.SimpleNamespace(id=123, username="allowed_group", type=ChatType.GROUP),
                    from_user=types.SimpleNamespace(id=42),
                    text="/battle_stats @enemy",
                    caption=None,
                    entities=[types.SimpleNamespace(type=MessageEntityType.MENTION, offset=14, length=6)],
                    caption_entities=[],
                    reply_to_message=None,
                    reply=AsyncMock(),
                )
                redis_mock = types.SimpleNamespace(
                    hmget=AsyncMock(side_effect=[("4", "2", "1"), ("1", "0", "0")]),
                    hget=AsyncMock(return_value=cached_value),
                )
                chat_member = types.SimpleNamespace(
                    user=types.SimpleNamespace(username="botname", full_name="Bot Name")
                )

                with (
                    patch.object(battle, "settings", types.SimpleNamespace(ALLOWED_GROUP_IDS=[123])),
                    patch.object(battle, "redis_client", redis_mock),
                    patch.object(battle.bot, "get_chat", AsyncMock()) as get_chat_mock,
                    patch.object(battle.bot, "get_chat_member", AsyncMock(return_value=chat_member)),
                ):
                    await battle.cmd_battle_stats(message)

                redis_mock.hmget.assert_any_call("battle:bot_stats:123", "win", "loss", "tie")
                redis_mock.hmget.assert_any_call("battle:bot_vs:123:123", "win", "loss", "tie")
                get_chat_mock.assert_not_called()

    async def test_cmd_battle_stats_ignores_unauthorized_chat_without_side_effects(self) -> None:
        message = types.SimpleNamespace(
            chat=types.SimpleNamespace(id=999, username="forbidden_group"),
            text="/battle_stats",
            caption=None,
            entities=[],
            caption_entities=[],
            reply_to_message=None,
            reply=AsyncMock(),
        )
        redis_mock = types.SimpleNamespace(
            hmget=AsyncMock(),
            hget=AsyncMock(),
        )

        with (
            patch.object(battle, "settings", types.SimpleNamespace(ALLOWED_GROUP_IDS=[123])),
            patch.object(battle, "redis_client", redis_mock),
            patch.object(battle, "_resolve_stats_target_user_id", AsyncMock()) as resolve_mock,
            patch.object(battle.bot, "get_chat_member", AsyncMock()) as get_chat_member_mock,
            patch.object(battle.bot, "get_me", AsyncMock()) as get_me_mock,
        ):
            await battle.cmd_battle_stats(message)

        redis_mock.hmget.assert_not_called()
        redis_mock.hget.assert_not_called()
        resolve_mock.assert_not_called()
        get_chat_member_mock.assert_not_called()
        get_me_mock.assert_not_called()
        message.reply.assert_not_called()


if __name__ == "__main__":
    unittest.main()

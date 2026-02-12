import types
import unittest
from unittest.mock import AsyncMock, Mock, patch

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

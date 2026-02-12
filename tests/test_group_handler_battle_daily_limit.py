import types
import unittest
from contextlib import ExitStack
from unittest.mock import AsyncMock, Mock, patch

from app.bot.handlers import group


class GroupBattleDailyLimitBypassTests(unittest.IsolatedAsyncioTestCase):
    async def test_battle_branch_skips_daily_limit_and_context_on_repeated_commands(self) -> None:
        call_order: list[str] = []

        async def _first_delivery(*_args, **_kwargs):
            call_order.append("first_delivery")
            return True

        async def _moderation(*_args, **_kwargs):
            call_order.append("moderation")
            return False

        async def _sismember(*_args, **_kwargs):
            call_order.append("battle")
            return False

        redis_mock = types.SimpleNamespace(
            incr=AsyncMock(),
            expireat=AsyncMock(),
            sadd=AsyncMock(),
            sismember=AsyncMock(side_effect=_sismember),
            set=AsyncMock(side_effect=[True, False, False]),
        )

        ensure_daily_limit = AsyncMock(return_value=True)
        store_context = AsyncMock()
        battle_delay = Mock()

        with ExitStack() as stack:
            stack.enter_context(patch.object(group, "redis_client", redis_mock))
            stack.enter_context(patch.object(group, "_is_chat_allowed", return_value=True))
            stack.enter_context(patch.object(group, "_first_delivery", side_effect=_first_delivery))
            stack.enter_context(patch.object(group, "_update_presence", AsyncMock()))
            stack.enter_context(patch.object(group, "apply_moderation_filters", side_effect=_moderation))
            stack.enter_context(patch.object(group, "_is_channel_post", return_value=False))
            stack.enter_context(patch.object(group, "_reply_gate_requires_mention", return_value=False))
            stack.enter_context(patch.object(group, "_extract_entities", return_value=[]))
            stack.enter_context(patch.object(group, "split_context_text", return_value=("/battle", "/battle")))
            stack.enter_context(patch.object(group, "_is_bot_command_to_us", return_value=True))
            stack.enter_context(patch.object(group, "_is_mention", return_value=False))
            stack.enter_context(patch.object(group, "_mentions_other_user", return_value=False))
            maybe_handle_battle = stack.enter_context(patch.object(group, "_maybe_handle_battle", wraps=group._maybe_handle_battle))
            stack.enter_context(patch.object(group, "_ensure_daily_limit", ensure_daily_limit))
            stack.enter_context(patch.object(group, "_store_context", store_context))
            stack.enter_context(patch.object(group, "_store_quote_context", AsyncMock()))
            stack.enter_context(patch.object(group, "_replied_to_our_bot", return_value=False))
            stack.enter_context(patch.object(group, "_user_id_val", return_value=42))
            stack.enter_context(patch.object(group, "record_activity", AsyncMock()))
            stack.enter_context(patch.object(group, "delete_message_safe", AsyncMock()))
            stack.enter_context(patch.object(group, "send_message_safe", AsyncMock()))
            stack.enter_context(patch.object(group.battle_launch_task, "delay", battle_delay))
            stack.enter_context(patch.object(group.asyncio, "create_task", lambda _coro: _coro.close()))

            for msg_id in (101, 102, 103):
                message = types.SimpleNamespace(
                    chat=types.SimpleNamespace(id=777, username="allowed-group"),
                    message_id=msg_id,
                    text="/battle",
                    caption=None,
                    entities=[],
                    caption_entities=[],
                    reply_to_message=None,
                    from_user=types.SimpleNamespace(id=42, is_bot=False),
                    sender_chat=None,
                )
                await group.on_group_message(message)

        self.assertEqual(
            call_order,
            [
                "first_delivery",
                "moderation",
                "battle",
                "first_delivery",
                "moderation",
                "battle",
                "first_delivery",
                "moderation",
                "battle",
            ],
        )
        self.assertEqual(maybe_handle_battle.call_count, 3)
        ensure_daily_limit.assert_not_called()
        store_context.assert_not_called()
        redis_mock.incr.assert_not_called()
        self.assertEqual(battle_delay.call_count, 1)


if __name__ == "__main__":
    unittest.main()

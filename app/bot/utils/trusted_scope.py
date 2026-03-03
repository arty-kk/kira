from __future__ import annotations

import contextlib
from typing import Any, Awaitable, Callable

from aiogram import types
from aiogram.enums import ChatType


def trusted_scope_ids(settings_like: Any) -> tuple[set[int], set[int], set[int]]:
    trusted_chat_ids = {
        int(x)
        for x in (
            *(getattr(settings_like, "ALLOWED_GROUP_IDS", []) or []),
            *(getattr(settings_like, "COMMENT_TARGET_CHAT_IDS", []) or []),
        )
    }
    trusted_source_channel_ids = {
        int(x) for x in (getattr(settings_like, "COMMENT_SOURCE_CHANNEL_IDS", []) or [])
    }
    trusted_scope = trusted_chat_ids | trusted_source_channel_ids
    return trusted_chat_ids, trusted_source_channel_ids, trusted_scope


def is_trusted_destination(chat_id: int, chat_obj: Any, settings_like: Any) -> bool:
    _, trusted_source_channel_ids, trusted_scope = trusted_scope_ids(settings_like)
    if int(chat_id) in trusted_scope:
        return True
    with contextlib.suppress(Exception):
        linked_chat_id = int(getattr(chat_obj, "linked_chat_id", 0) or 0)
        if linked_chat_id and linked_chat_id in trusted_source_channel_ids:
            return True
    return False


def extract_source_scope_ids(message: types.Message | Any) -> set[int]:
    source_scope_ids: set[int] = set()
    for field in ("sender_chat", "forward_from_chat"):
        src = getattr(message, field, None)
        if src and getattr(src, "type", None) in (ChatType.CHANNEL, ChatType.GROUP, ChatType.SUPERGROUP):
            with contextlib.suppress(Exception):
                source_scope_ids.add(int(src.id))
    return source_scope_ids


def is_trusted_repost(message: types.Message | Any, trusted_scope: set[int], destination_trusted: bool) -> bool:
    if not destination_trusted:
        return False
    try:
        sc = getattr(message, "sender_chat", None)
        if sc and int(sc.id) == int(message.chat.id) and getattr(sc, "type", None) in (ChatType.GROUP, ChatType.SUPERGROUP):
            return False
    except Exception:
        pass
    source_scope_ids = extract_source_scope_ids(message)
    if not source_scope_ids:
        return False
    return bool(source_scope_ids & trusted_scope)


async def is_trusted_actor(
    *,
    message: types.Message | None,
    user_id: int | None,
    chat_id: int,
    from_linked: bool,
    trusted_scope_ids: set[int],
    trusted_source_channel_ids: set[int],
    is_admin_cb: Callable[[int, int], Awaitable[bool]],
) -> bool:
    if from_linked:
        return True

    is_destination_trusted = int(chat_id) in trusted_scope_ids

    source_channel_id: int | None = None
    source_scope: set[int] = set()
    if message is not None:
        source_scope = extract_source_scope_ids(message)
        with contextlib.suppress(Exception):
            linked_chat_id = int(getattr(getattr(message, "chat", None), "linked_chat_id", 0) or 0)
            if linked_chat_id and linked_chat_id in trusted_source_channel_ids:
                is_destination_trusted = True
                source_channel_id = linked_chat_id

        if source_channel_id is None:
            sender_chat = getattr(message, "sender_chat", None)
            if sender_chat and getattr(sender_chat, "type", None) == ChatType.CHANNEL:
                with contextlib.suppress(Exception):
                    source_channel_id = int(sender_chat.id)
        if source_channel_id is None:
            forward_from_chat = getattr(message, "forward_from_chat", None)
            if forward_from_chat and getattr(forward_from_chat, "type", None) == ChatType.CHANNEL:
                with contextlib.suppress(Exception):
                    source_channel_id = int(forward_from_chat.id)

    if is_destination_trusted and (source_scope & trusted_scope_ids):
        return True

    uid = int(getattr(getattr(message, "from_user", None), "id", user_id or 0) or 0)
    if uid and is_destination_trusted:
        with contextlib.suppress(Exception):
            if await is_admin_cb(chat_id, uid):
                return True

    if uid and source_channel_id and source_channel_id in trusted_source_channel_ids:
        with contextlib.suppress(Exception):
            if await is_admin_cb(source_channel_id, uid):
                return True

    return False

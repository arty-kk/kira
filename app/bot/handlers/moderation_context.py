"""Shared moderation context resolution helpers for sync/async handler paths."""

from __future__ import annotations

import contextlib

from aiogram import types
from aiogram.enums import ChatType

from app.bot.components.constants import redis_client
from app.config import settings

_ROOT_OF_PREFIX = "comment:root_of"
_THREAD_ROOT_PREFIX = "comment:thread_root"


def _ctx_ttl_seconds() -> int:
    try:
        return int(getattr(settings, "REPLY_CONTEXT_TTL_SEC", 86400) or 86400)
    except Exception:
        return 86400


def _root_of_key(chat_id: int, msg_id: int) -> str:
    return f"{_ROOT_OF_PREFIX}:{int(chat_id)}:{int(msg_id)}"


def _thread_root_key(chat_id: int, thread_id: int) -> str:
    return f"{_THREAD_ROOT_PREFIX}:{int(chat_id)}:{int(thread_id)}"


def _message_thread_id(message: types.Message) -> int | None:
    try:
        thread_id = getattr(message, "message_thread_id", None)
        if thread_id:
            return int(thread_id)
    except Exception:
        return None
    return None


def _trusted_source_channel_ids() -> set[int]:
    try:
        return {int(x) for x in (getattr(settings, "COMMENT_SOURCE_CHANNEL_IDS", []) or [])}
    except Exception:
        return set()


def _source_channel_id(msg: types.Message) -> int | None:
    for field in ("sender_chat", "forward_from_chat"):
        src = getattr(msg, field, None)
        if src and getattr(src, "type", None) == ChatType.CHANNEL:
            with contextlib.suppress(Exception):
                return int(src.id)
    return None


def _linked_channel_id(message: types.Message) -> int | None:
    try:
        linked_chat_id = getattr(getattr(message, "chat", None), "linked_chat_id", None)
        if linked_chat_id:
            return int(linked_chat_id)
    except Exception:
        return None
    return None


def _is_linked_channel_post(msg: types.Message, linked_chat_id: int) -> bool:
    try:
        if bool(getattr(msg, "is_automatic_forward", False)):
            sender_chat = getattr(msg, "sender_chat", None)
            if sender_chat and getattr(sender_chat, "type", None) == ChatType.CHANNEL:
                return int(getattr(sender_chat, "id", 0) or 0) == int(linked_chat_id)
            forward_from_chat = getattr(msg, "forward_from_chat", None)
            if forward_from_chat and getattr(forward_from_chat, "type", None) == ChatType.CHANNEL:
                return int(getattr(forward_from_chat, "id", 0) or 0) == int(linked_chat_id)
    except Exception:
        pass

    try:
        sender_chat = getattr(msg, "sender_chat", None)
        if sender_chat and getattr(sender_chat, "type", None) == ChatType.CHANNEL:
            return int(getattr(sender_chat, "id", 0) or 0) == int(linked_chat_id)
    except Exception:
        pass

    try:
        forward_from_chat = getattr(msg, "forward_from_chat", None)
        if forward_from_chat and getattr(forward_from_chat, "type", None) == ChatType.CHANNEL:
            return int(getattr(forward_from_chat, "id", 0) or 0) == int(linked_chat_id)
    except Exception:
        pass

    return False


def _is_channel_origin_message(msg: types.Message) -> bool:
    try:
        if bool(getattr(msg, "is_automatic_forward", False)):
            return True
    except Exception:
        pass

    try:
        sender_chat = getattr(msg, "sender_chat", None)
        if sender_chat and getattr(sender_chat, "type", None) == ChatType.CHANNEL:
            return True
    except Exception:
        pass

    try:
        forward_from_chat = getattr(msg, "forward_from_chat", None)
        if forward_from_chat and getattr(forward_from_chat, "type", None) == ChatType.CHANNEL:
            return True
    except Exception:
        pass

    return False


async def update_comment_thread_root_cache(message: types.Message) -> int | None:
    try:
        chat_id = int(getattr(getattr(message, "chat", None), "id", 0) or 0)
        msg_id = int(getattr(message, "message_id", 0) or 0)
        if not chat_id or not msg_id:
            return None
    except Exception:
        return None

    ttl = _ctx_ttl_seconds()
    thread_id = _message_thread_id(message)

    linked_id = _linked_channel_id(message)
    source_channel_id = _source_channel_id(message)
    trusted_source_ids = _trusted_source_channel_ids()
    trusted_source_hit = bool(source_channel_id and source_channel_id in trusted_source_ids)

    is_channel_root = False
    if linked_id and _is_linked_channel_post(message, linked_id):
        is_channel_root = True
    elif trusted_source_hit and _is_channel_origin_message(message):
        is_channel_root = True

    if is_channel_root:
        with contextlib.suppress(Exception):
            await redis_client.set(_root_of_key(chat_id, msg_id), int(msg_id), ex=ttl)
        if thread_id and thread_id > 0:
            with contextlib.suppress(Exception):
                await redis_client.set(_thread_root_key(chat_id, thread_id), int(msg_id), ex=ttl)
        return int(msg_id)

    if thread_id and thread_id > 0:
        with contextlib.suppress(Exception):
            cached_thread_root = await redis_client.get(_thread_root_key(chat_id, thread_id))
            if cached_thread_root:
                if isinstance(cached_thread_root, (bytes, bytearray)):
                    cached_thread_root = cached_thread_root.decode("utf-8", "ignore")
                root_from_thread = int(str(cached_thread_root).strip())
                if root_from_thread > 0:
                    with contextlib.suppress(Exception):
                        await redis_client.set(_root_of_key(chat_id, msg_id), int(root_from_thread), ex=ttl)
                    return int(root_from_thread)

    parent = getattr(message, "reply_to_message", None)
    if not parent:
        return None

    try:
        parent_id = int(getattr(parent, "message_id", 0) or 0)
    except Exception:
        return None
    if not parent_id:
        return None

    if linked_id and _is_linked_channel_post(parent, linked_id):
        with contextlib.suppress(Exception):
            await redis_client.set(_root_of_key(chat_id, msg_id), int(parent_id), ex=ttl)
        if thread_id and thread_id > 0:
            with contextlib.suppress(Exception):
                await redis_client.set(_thread_root_key(chat_id, thread_id), int(parent_id), ex=ttl)
        return int(parent_id)

    parent_source_channel_id = _source_channel_id(parent)
    if parent_source_channel_id and parent_source_channel_id in trusted_source_ids and _is_channel_origin_message(parent):
        with contextlib.suppress(Exception):
            await redis_client.set(_root_of_key(chat_id, msg_id), int(parent_id), ex=ttl)
        if thread_id and thread_id > 0:
            with contextlib.suppress(Exception):
                await redis_client.set(_thread_root_key(chat_id, thread_id), int(parent_id), ex=ttl)
        return int(parent_id)

    root_id = None
    with contextlib.suppress(Exception):
        cached = await redis_client.get(_root_of_key(chat_id, parent_id))
        if cached:
            if isinstance(cached, (bytes, bytearray)):
                cached = cached.decode("utf-8", "ignore")
            root_id = int(str(cached).strip())

    if root_id and root_id > 0:
        with contextlib.suppress(Exception):
            await redis_client.set(_root_of_key(chat_id, msg_id), int(root_id), ex=ttl)
        if thread_id and thread_id > 0:
            with contextlib.suppress(Exception):
                await redis_client.set(_thread_root_key(chat_id, thread_id), int(root_id), ex=ttl)
        return int(root_id)

    return None


async def resolve_message_moderation_context_async(message: types.Message, *, from_linked: bool = False) -> str:
    root_id = await update_comment_thread_root_cache(message)
    if root_id:
        return "comment"
    if from_linked:
        return "comment"
    if _linked_channel_id(message):
        return "comment"
    # Backward-compatible fallback when linked_chat_id is absent in update,
    # but message still carries explicit channel-origin markers.
    if _is_channel_origin_message(message):
        return "comment"
    return "group"


def resolve_message_moderation_context(message: types.Message, *, from_linked: bool = False) -> str:
    if from_linked:
        return "comment"

    sender_chat = getattr(message, "sender_chat", None)
    forward_from_chat = getattr(message, "forward_from_chat", None)

    if getattr(message, "is_automatic_forward", False):
        return "comment"
    if sender_chat and getattr(sender_chat, "type", None) == ChatType.CHANNEL:
        return "comment"
    if forward_from_chat and getattr(forward_from_chat, "type", None) == ChatType.CHANNEL:
        return "comment"

    linked_chat_id = getattr(getattr(message, "chat", None), "linked_chat_id", None)
    if linked_chat_id:
        parent = getattr(message, "reply_to_message", None)
        if parent:
            parent_sender_chat = getattr(parent, "sender_chat", None)
            parent_forward_from_chat = getattr(parent, "forward_from_chat", None)
            if (
                parent_sender_chat
                and getattr(parent_sender_chat, "type", None) == ChatType.CHANNEL
                and int(getattr(parent_sender_chat, "id", 0) or 0) == int(linked_chat_id)
            ):
                return "comment"
            if (
                parent_forward_from_chat
                and getattr(parent_forward_from_chat, "type", None) == ChatType.CHANNEL
                and int(getattr(parent_forward_from_chat, "id", 0) or 0) == int(linked_chat_id)
            ):
                return "comment"
    return "group"


def is_comment_moderation_context(message: types.Message, *, from_linked: bool = False) -> bool:
    return resolve_message_moderation_context(message, from_linked=from_linked) == "comment"

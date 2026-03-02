"""Shared moderation context resolution helpers for sync/async handler paths."""

from __future__ import annotations

import contextlib

from aiogram import types
from aiogram.enums import ChatType

from app.bot.components.constants import redis_client
from app.config import settings

_ROOT_OF_PREFIX = "comment:root_of"


def _ctx_ttl_seconds() -> int:
    try:
        return int(getattr(settings, "REPLY_CONTEXT_TTL_SEC", 86400) or 86400)
    except Exception:
        return 86400


def _root_of_key(chat_id: int, msg_id: int) -> str:
    return f"{_ROOT_OF_PREFIX}:{int(chat_id)}:{int(msg_id)}"


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
    linked_id = _linked_channel_id(message)
    if not linked_id:
        return None

    try:
        chat_id = int(getattr(getattr(message, "chat", None), "id", 0) or 0)
        msg_id = int(getattr(message, "message_id", 0) or 0)
        if not chat_id or not msg_id:
            return None
    except Exception:
        return None

    ttl = _ctx_ttl_seconds()

    if _is_linked_channel_post(message, linked_id):
        with contextlib.suppress(Exception):
            await redis_client.set(_root_of_key(chat_id, msg_id), int(msg_id), ex=ttl)
        return int(msg_id)

    parent = getattr(message, "reply_to_message", None)
    if not parent:
        return None

    try:
        parent_id = int(getattr(parent, "message_id", 0) or 0)
    except Exception:
        return None
    if not parent_id:
        return None

    if _is_linked_channel_post(parent, linked_id):
        with contextlib.suppress(Exception):
            await redis_client.set(_root_of_key(chat_id, msg_id), int(parent_id), ex=ttl)
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

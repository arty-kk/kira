from __future__ import annotations

"""Shared moderation context resolution helpers for sync/async handler paths."""

from aiogram import types
from aiogram.enums import ChatType


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
    return "group"


def is_comment_moderation_context(message: types.Message, *, from_linked: bool = False) -> bool:
    return resolve_message_moderation_context(message, from_linked=from_linked) == "comment"

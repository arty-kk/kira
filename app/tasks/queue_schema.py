from __future__ import annotations

"""Queue payload schemas used to validate job payloads before enqueueing."""

from typing import Any, Optional

from pydantic import BaseModel, Field, ValidationError


class BotQueueJob(BaseModel):
    chat_id: int
    user_id: int
    text: Optional[str] = ""
    msg_id: Optional[int] = None
    reply_to: Optional[int] = None
    tg_reply_to: Optional[int] = None
    reservation_id: Optional[int] = None
    is_group: bool = False
    is_channel_post: bool = False
    channel_title: Optional[str] = None
    voice_in: bool = False
    voice_file_id: Optional[str] = None
    merged_msg_ids: Optional[list[int]] = None
    image_b64: Optional[str] = None
    image_mime: Optional[str] = None
    trigger: Optional[str] = None
    enforce_on_topic: bool = False
    allow_web: bool = False
    billing_tier: Optional[str] = None
    entities: list[Any] = Field(default_factory=list)

    model_config = {"extra": "allow"}


class ApiQueueJob(BaseModel):
    request_id: str
    text: str = ""
    image_b64: Optional[str] = None
    image_mime: Optional[str] = None
    voice_b64: Optional[str] = None
    voice_mime: Optional[str] = None
    chat_id: int
    memory_uid: int
    persona_owner_id: int
    persona_profile_id: Optional[str] = None
    api_key_id: int
    billing_tier: Optional[str] = None
    result_key: str
    msg_id: Optional[int] = None
    allow_web: bool = False
    enqueued_at: float

    model_config = {"extra": "allow"}


def validate_bot_job(payload: dict[str, Any]) -> Optional[str]:
    try:
        BotQueueJob.model_validate(payload)
    except ValidationError as exc:
        return str(exc)
    return None


def validate_api_job(payload: dict[str, Any]) -> Optional[str]:
    try:
        ApiQueueJob.model_validate(payload)
    except ValidationError as exc:
        return str(exc)
    return None

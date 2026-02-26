#app/tasks/queue_schema.py
from __future__ import annotations

from typing import Any, Optional, Annotated

from pydantic import BaseModel, Field, ValidationError


class BotQueueJob(BaseModel):
    chat_id: int
    user_id: int
    text: Optional[str] = ""
    msg_id: int
    reply_to: Optional[int] = None
    tg_reply_to: Optional[int] = None
    reservation_id: Optional[int] = None
    reservation_ids: Optional[list[Annotated[int, Field(strict=True, gt=0)]]] = None
    is_group: bool = False
    is_channel_post: bool = False
    is_comment_context: bool = False
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
    precomputed_rag_hits: Optional[list[Any]] = None
    query_embedding: Optional[list[float]] = None
    embedding_model: Optional[str] = None
    rag_precheck_source: Optional[str] = None
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
    knowledge_owner_id: int
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
        job = BotQueueJob.model_validate(payload)
    except ValidationError as exc:
        return str(exc)
    if job.msg_id <= 0:
        return "msg_id must be > 0"
    return None


def validate_api_job(payload: dict[str, Any]) -> Optional[str]:
    try:
        ApiQueueJob.model_validate(payload)
    except ValidationError as exc:
        return str(exc)
    return None

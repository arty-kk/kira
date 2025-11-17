#app/core/models.py
from sqlalchemy import (
    Column, BigInteger, Integer,
    String, DateTime, Boolean,
    ForeignKey, UniqueConstraint, text,
)
from sqlalchemy.sql import func
from sqlalchemy.dialects.postgresql import JSONB

from app.core.db import Base


class User(Base):

    __tablename__ = 'users'

    id                  = Column(BigInteger, primary_key=True)
    username            = Column(String, nullable=True)
    full_name           = Column(String, nullable=False)
    registered_at       = Column(DateTime(timezone=True), server_default=func.now())
    free_requests       = Column(Integer, default=0, nullable=False)
    paid_requests       = Column(Integer, default=0, nullable=False)
    used_requests       = Column(Integer, default=0, nullable=False)
    gender              = Column(String(6), nullable=True)
    total_paid_cents    = Column(Integer, default=0, nullable=False)
    pm_welcome_sent     = Column(DateTime(timezone=True), nullable=True)
    persona_prefs       = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict)


class ApiKey(Base):

    __tablename__ = 'api_keys'

    id           = Column(BigInteger, primary_key=True)
    user_id      = Column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    key_hash     = Column(String(128), nullable=False, unique=True)
    label        = Column(String(128), nullable=True)
    persona_prefs = Column(
        JSONB,
        nullable=False,
        server_default=text("'{}'::jsonb"),
        default=dict,
    )
    created_at   = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    last_used_at = Column(DateTime(timezone=True), nullable=True)
    active       = Column(Boolean, nullable=False, server_default=text("true"))


class ApiKeyStats(Base):

    __tablename__ = 'api_key_stats'

    api_key_id       = Column(
        BigInteger,
        ForeignKey("api_keys.id", ondelete="CASCADE"),
        primary_key=True,
    )
    messages_in      = Column(BigInteger, nullable=False, server_default=text("0"))
    messages_out     = Column(BigInteger, nullable=False, server_default=text("0"))
    total_latency_ms  = Column(BigInteger, nullable=False, server_default=text("0"))
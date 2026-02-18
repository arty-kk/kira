#app/core/models.py
from sqlalchemy import (
    Column, BigInteger, Integer, Identity,
    String, DateTime, Boolean,
    ForeignKey, UniqueConstraint, CheckConstraint, text,
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
    free_requests       = Column(Integer, default=5, nullable=False)
    paid_requests       = Column(Integer, default=0, nullable=False)
    used_requests       = Column(Integer, default=0, nullable=False)
    gender              = Column(String(6), nullable=True)
    total_paid_cents    = Column(Integer, default=0, nullable=False)
    pm_welcome_sent     = Column(DateTime(timezone=True), nullable=True)
    persona_prefs       = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict)


class RequestReservation(Base):

    __tablename__ = "request_reservations"

    id            = Column(BigInteger, Identity(always=False), primary_key=True)
    user_id       = Column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    status        = Column(String(16), nullable=False, server_default=text("'reserved'"))
    used_paid     = Column(Boolean, nullable=False, server_default=text("false"))
    chat_id       = Column(BigInteger, nullable=True)
    message_id    = Column(BigInteger, nullable=True)
    created_at    = Column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)
    updated_at    = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    __table_args__ = (
        CheckConstraint("status IN ('reserved','consumed','refunded')", name="ck_request_reservations_status"),
    )

class PaymentReceipt(Base):

    __tablename__ = "payment_receipts"

    id                         = Column(BigInteger, Identity(always=False), primary_key=True)
    user_id                    = Column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    kind                       = Column(String(16), nullable=False)  # buy|gift
    requests_amount            = Column(Integer, nullable=False, server_default=text("0"))
    stars_amount               = Column(Integer, nullable=False, server_default=text("0"))
    invoice_payload            = Column(String(128), nullable=True)
    telegram_payment_charge_id = Column(String(128), nullable=False, unique=True)
    provider_payment_charge_id = Column(String(128), nullable=True, index=True)
    created_at                 = Column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)

    __table_args__ = (
        CheckConstraint("kind IN ('buy','gift')", name="ck_payment_receipts_kind"),
        CheckConstraint("stars_amount >= 0", name="ck_payment_receipts_stars_nonneg"),
        CheckConstraint("requests_amount >= 0", name="ck_payment_receipts_requests_nonneg"),
        CheckConstraint("telegram_payment_charge_id <> ''", name="ck_payment_receipts_charge_id_nonempty"),
        CheckConstraint(
            "(provider_payment_charge_id IS NULL) OR (provider_payment_charge_id <> '')",
            name="ck_payment_receipts_provider_charge_id_nonempty",
        ),
    )


class PaymentOutbox(Base):

    __tablename__ = "payment_outbox"

    id                         = Column(BigInteger, Identity(always=False), primary_key=True)
    user_id                    = Column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    kind                       = Column(String(16), nullable=False)  # buy|gift
    status                     = Column(String(16), nullable=False, server_default=text("'pending'"))
    requests_amount            = Column(Integer, nullable=False, server_default=text("0"))
    stars_amount               = Column(Integer, nullable=False, server_default=text("0"))
    invoice_payload            = Column(String(128), nullable=True)
    telegram_payment_charge_id = Column(String(128), nullable=False, unique=True)
    provider_payment_charge_id = Column(String(128), nullable=True, index=True)
    gift_code                  = Column(String(64), nullable=True)
    gift_title                 = Column(String(128), nullable=True)
    gift_emoji                 = Column(String(32), nullable=True)
    attempts                   = Column(Integer, nullable=False, server_default=text("0"))
    lease_attempts             = Column(Integer, nullable=False, server_default=text("0"))
    last_error                 = Column(String, nullable=True)
    leased_at                  = Column(DateTime(timezone=True), nullable=True, index=True)
    lease_token                = Column(String(64), nullable=True)
    created_at                 = Column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)
    updated_at                 = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    applied_at                 = Column(DateTime(timezone=True), nullable=True, index=True)
    notified_at                = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint("kind IN ('buy','gift')", name="ck_payment_outbox_kind"),
        CheckConstraint("status IN ('pending','applied','failed')", name="ck_payment_outbox_status"),
        CheckConstraint("stars_amount >= 0", name="ck_payment_outbox_stars_nonneg"),
        CheckConstraint("requests_amount > 0", name="ck_payment_outbox_requests_positive"),
        CheckConstraint("lease_attempts >= 0", name="ck_payment_outbox_lease_attempts_nonneg"),
        CheckConstraint("telegram_payment_charge_id <> ''", name="ck_payment_outbox_charge_id_nonempty"),
    )


class RefundOutbox(Base):

    __tablename__ = "refund_outbox"

    id           = Column(BigInteger, Identity(always=False), primary_key=True)
    owner_id     = Column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    billing_tier = Column(String(16), nullable=True)
    request_id   = Column(String(128), nullable=False, index=True)
    reason       = Column(String(128), nullable=False)
    status       = Column(String(16), nullable=False, server_default=text("'pending'"))
    attempts     = Column(Integer, nullable=False, server_default=text("0"))
    lease_attempts = Column(Integer, nullable=False, server_default=text("0"))
    last_error   = Column(String, nullable=True)
    leased_at    = Column(DateTime(timezone=True), nullable=True, index=True)
    lease_token  = Column(String(64), nullable=True)
    created_at   = Column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)
    updated_at   = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    processed_at = Column(DateTime(timezone=True), nullable=True, index=True)

    __table_args__ = (
        CheckConstraint("status IN ('pending','applied','failed')", name="ck_refund_outbox_status"),
        CheckConstraint("billing_tier IN ('free','paid')", name="ck_refund_outbox_billing_tier"),
        CheckConstraint("attempts >= 0", name="ck_refund_outbox_attempts_nonneg"),
        CheckConstraint("lease_attempts >= 0", name="ck_refund_outbox_lease_attempts_nonneg"),
        CheckConstraint("request_id <> ''", name="ck_refund_outbox_request_id_nonempty"),
        CheckConstraint("reason <> ''", name="ck_refund_outbox_reason_nonempty"),
        UniqueConstraint("request_id", name="uq_refund_outbox_request_id"),
    )

class ApiKey(Base):

    __tablename__ = 'api_keys'

    id            = Column(BigInteger, Identity(always=False), primary_key=True)
    user_id       = Column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    key_hash      = Column(String(128), nullable=False, unique=True)
    label         = Column(String(128), nullable=True)
    persona_prefs = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict)
    created_at    = Column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)
    last_used_at  = Column(DateTime(timezone=True), nullable=True)
    active        = Column(Boolean, nullable=False, server_default=text("true"))


class ApiKeyStats(Base):

    __tablename__ = 'api_key_stats'

    api_key_id       = Column(BigInteger, ForeignKey("api_keys.id", ondelete="CASCADE"), primary_key=True)
    messages_in      = Column(BigInteger, nullable=False, server_default=text("0"))
    messages_out     = Column(BigInteger, nullable=False, server_default=text("0"))
    total_latency_ms = Column(BigInteger, nullable=False, server_default=text("0"))

class ApiKeyKnowledge(Base):

    __tablename__ = "api_key_knowledge"

    id              = Column(BigInteger, Identity(always=False), primary_key=True)
    api_key_id      = Column(BigInteger, ForeignKey("api_keys.id", ondelete="CASCADE"), nullable=False, index=True)
    version         = Column(Integer, nullable=False, server_default=text("1"))
    label           = Column(String(255), nullable=True)
    items           = Column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    embedding_model = Column(String(128), nullable=False, server_default=text("'text-embedding-3-large'"))
    status          = Column(String(32), nullable=False, server_default=text("'pending'")) # 'pending' | 'building' | 'ready' | 'failed'
    error           = Column(String, nullable=True)
    chunks_count    = Column(Integer, nullable=False, server_default=text("0"))
    created_at      = Column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)
    updated_at      = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "api_key_id",
            "version",
            name="uq_api_key_knowledge_version",
        ),
    )

class GiftPurchase(Base):

    __tablename__ = "gift_purchases"

    id                         = Column(BigInteger, Identity(always=False), primary_key=True)
    user_id                    = Column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    gift_code                  = Column(String(64), nullable=False)
    gift_title                 = Column(String(128), nullable=True)
    gift_emoji                 = Column(String(32), nullable=True)
    stars_amount               = Column(Integer, nullable=False, server_default=text("0"))
    requests_amount            = Column(Integer, nullable=False, server_default=text("0"))
    invoice_payload            = Column(String(128), nullable=True)
    telegram_payment_charge_id = Column(String(128), nullable=False, unique=True)
    created_at                 = Column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)

    __table_args__ = (
        CheckConstraint("gift_code <> ''", name="ck_gift_purchases_gift_code_nonempty"),
        CheckConstraint("stars_amount >= 0", name="ck_gift_purchases_stars_nonneg"),
        CheckConstraint("requests_amount >= 0", name="ck_gift_purchases_requests_nonneg"),
        CheckConstraint("telegram_payment_charge_id <> ''", name="ck_gift_purchases_charge_id_nonempty"),
    )

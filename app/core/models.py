#app/core/models.py

from sqlalchemy import Column, BigInteger, Integer, String, DateTime
from sqlalchemy.sql import func

from app.core.db import Base

class User(Base):

    __tablename__ = 'users'

    id                  = Column(BigInteger, primary_key=True)
    username            = Column(String, nullable=True)
    full_name           = Column(String, nullable=False)
    registered_at       = Column(DateTime(timezone=True), server_default=func.now())

    free_requests_left  = Column(Integer, default=30, nullable=False)
    paid_requests       = Column(Integer, default=0,  nullable=False)
    used_requests       = Column(Integer, default=0,  nullable=False)
    gender              = Column(String(6), nullable=True)
    total_paid_cents    = Column(Integer, default=0, nullable=False)

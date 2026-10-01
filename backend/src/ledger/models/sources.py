from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Identity,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from ledger.db.base import Base

SOURCE_KINDS = ("tiller", "simplefin")


class Source(Base):
    """An automated feed of transactions/balances; `secret` holds encrypted credentials (e.g. SimpleFIN access URL)."""

    __tablename__ = "source"
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(20), unique=True)
    name: Mapped[str] = mapped_column(String(100))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    config: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}")
    secret: Mapped[str | None] = mapped_column(Text)
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_status: Mapped[str | None] = mapped_column(String(20))
    last_error: Mapped[str | None] = mapped_column(Text)
    last_result: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class AccountBalance(Base):
    __tablename__ = "account_balance"
    __table_args__ = (UniqueConstraint("account_id", "as_of", "source"),)
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("account.id", ondelete="CASCADE"), index=True)
    as_of: Mapped[date] = mapped_column(Date)
    balance: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    available: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    source: Mapped[str] = mapped_column(String(20))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Holding(Base):
    """Investment position snapshot per account and date."""

    __tablename__ = "holding"
    __table_args__ = (UniqueConstraint("account_id", "as_of", "external_id"),)
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("account.id", ondelete="CASCADE"), index=True)
    as_of: Mapped[date] = mapped_column(Date)
    external_id: Mapped[str] = mapped_column(String(120))
    symbol: Mapped[str | None] = mapped_column(String(40))
    description: Mapped[str | None] = mapped_column(String(300))
    shares: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))
    market_value: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    cost_basis: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    currency: Mapped[str | None] = mapped_column(String(10))
    source: Mapped[str] = mapped_column(String(20))

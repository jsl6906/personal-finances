from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import BigInteger, Date, DateTime, ForeignKey, Identity, Numeric, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from ledger.db.base import Base

ANOMALY_KINDS = (
    "category_spike",
    "large_for_merchant",
    "large_transaction",
    "new_merchant",
    "bill_increase",
    "unmatched_transfer",
)
ANOMALY_STATUSES = ("open", "reviewed", "dismissed")


class Anomaly(Base):
    """Out-of-norm spending detected by the nightly job; subject_key makes detection idempotent."""

    __tablename__ = "anomaly"
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    kind: Mapped[str] = mapped_column(String(30), index=True)
    subject_key: Mapped[str] = mapped_column(String(120), unique=True)
    period: Mapped[date] = mapped_column(Date, index=True)
    transaction_id: Mapped[int | None] = mapped_column(ForeignKey("transaction.id", ondelete="CASCADE"))
    category_id: Mapped[int | None] = mapped_column(ForeignKey("category.id", ondelete="SET NULL"))
    series_id: Mapped[int | None] = mapped_column(ForeignKey("statement_series.id", ondelete="CASCADE"))
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    baseline: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    score: Mapped[Decimal] = mapped_column(Numeric(8, 3))
    title: Mapped[str] = mapped_column(String(200))
    detail: Mapped[str] = mapped_column(Text)
    ai_note: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="open", server_default="open", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

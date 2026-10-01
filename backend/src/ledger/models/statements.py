from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Identity,
    Numeric,
    String,
    Table,
    Text,
    func,
    select,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, column_property, mapped_column, relationship

from ledger.db.base import Base, TimestampMixin
from ledger.models.imports import Attachment
from ledger.models.transactions import Transaction

STATEMENT_STATUSES = ("processing", "suggested", "approved", "dismissed", "failed")

statement_transaction = Table(
    "statement_transaction",
    Base.metadata,
    Column("statement_id", BigInteger, ForeignKey("statement.id", ondelete="CASCADE"), primary_key=True),
    Column("transaction_id", BigInteger, ForeignKey("transaction.id", ondelete="CASCADE"), primary_key=True),
)


class StatementSeries(TimestampMixin, Base):
    """A recurring bill/statement stream (e.g. Water · City of Springfield) whose usage is tracked over time."""

    __tablename__ = "statement_series"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    vendor: Mapped[str | None] = mapped_column(String(200))
    service_type: Mapped[str | None] = mapped_column(String(60))
    category_id: Mapped[int | None] = mapped_column(ForeignKey("category.id", ondelete="SET NULL"))
    tag_id: Mapped[int | None] = mapped_column(ForeignKey("tag.id", ondelete="SET NULL"))
    primary_metric: Mapped[str | None] = mapped_column(String(60))
    unit: Mapped[str | None] = mapped_column(String(30))
    notes: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")


class Statement(Base):
    __tablename__ = "statement"
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    attachment_id: Mapped[int] = mapped_column(ForeignKey("attachment.id", ondelete="RESTRICT"), index=True)
    series_id: Mapped[int | None] = mapped_column(ForeignKey("statement_series.id", ondelete="SET NULL"), index=True)
    status: Mapped[str] = mapped_column(String(20), default="processing", server_default="processing", index=True)
    document_type: Mapped[str | None] = mapped_column(String(40))
    vendor: Mapped[str | None] = mapped_column(String(200))
    account_ref: Mapped[str | None] = mapped_column(String(40))
    statement_date: Mapped[date | None] = mapped_column(Date)
    period_start: Mapped[date | None] = mapped_column(Date)
    period_end: Mapped[date | None] = mapped_column(Date)
    due_date: Mapped[date | None] = mapped_column(Date)
    amount_due: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    summary: Mapped[str | None] = mapped_column(Text)
    extracted: Mapped[dict | None] = mapped_column(JSONB)
    suggestion: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}")
    error: Mapped[str | None] = mapped_column(Text)
    job_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    attachment: Mapped[Attachment] = relationship(lazy="joined")
    series: Mapped[StatementSeries | None] = relationship(lazy="joined")
    usage: Mapped[list["StatementUsage"]] = relationship(
        lazy="selectin", cascade="all, delete-orphan", order_by="StatementUsage.id"
    )


class StatementUsage(Base):
    __tablename__ = "statement_usage"
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    statement_id: Mapped[int] = mapped_column(ForeignKey("statement.id", ondelete="CASCADE"), index=True)
    metric: Mapped[str] = mapped_column(String(60))
    value: Mapped[Decimal] = mapped_column(Numeric(16, 3))
    unit: Mapped[str | None] = mapped_column(String(30))
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")


# Declared here (not on Transaction) to keep models/transactions.py free of a circular import.
Transaction.has_statement = column_property(
    select(statement_transaction.c.transaction_id)
    .where(statement_transaction.c.transaction_id == Transaction.id)
    .correlate_except(statement_transaction)
    .exists()
)

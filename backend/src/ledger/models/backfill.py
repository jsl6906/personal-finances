from datetime import date, datetime

from sqlalchemy import BigInteger, Date, DateTime, ForeignKey, Identity, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from ledger.db.base import Base

# pending -> classified -> done | review | skipped | failed
BACKFILL_STATUSES = ("pending", "classified", "done", "review", "skipped", "failed")
STATEMENT_KINDS = ("bank_statement", "credit_card_statement", "loan_statement", "investment_statement")
BILL_KINDS = ("utility_bill", "other_bill")


class BackfillFile(Base):
    """One file in the historical archive being backfilled (Google Drive folder or mounted inbox)."""

    __tablename__ = "backfill_file"
    __table_args__ = (UniqueConstraint("provider", "external_id"),)
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    provider: Mapped[str] = mapped_column(String(10))
    external_id: Mapped[str] = mapped_column(String(500))
    path: Mapped[str] = mapped_column(String(1000), default="")
    name: Mapped[str] = mapped_column(String(300))
    mime_type: Mapped[str | None] = mapped_column(String(120))
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    modified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(20), default="pending", server_default="pending", index=True)
    kind: Mapped[str | None] = mapped_column(String(30), index=True)
    period_start: Mapped[date | None] = mapped_column(Date)
    detail: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}")
    message: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    attachment_id: Mapped[int | None] = mapped_column(ForeignKey("attachment.id", ondelete="SET NULL"))
    import_batch_id: Mapped[int | None] = mapped_column(ForeignKey("import_batch.id", ondelete="SET NULL"))
    statement_id: Mapped[int | None] = mapped_column(ForeignKey("statement.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

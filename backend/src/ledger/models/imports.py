from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Date,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, deferred, mapped_column, relationship

from ledger.db.base import Base, TimestampMixin

IMPORT_STATUSES = ("extracting", "mapping", "preparing", "review", "committed", "rolled_back", "failed")
ROW_DECISIONS = ("pending", "insert", "skip_duplicate", "keep", "invalid")
PAIR_STATUSES = ("pending", "confirmed_duplicate", "confirmed_separate")


class Attachment(Base):
    """A stored file (statement, bill, spreadsheet); content lives in Postgres as bytea."""

    __tablename__ = "attachment"
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    filename: Mapped[str] = mapped_column(String(300))
    mime_type: Mapped[str] = mapped_column(String(120))
    size_bytes: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64), unique=True)
    content: Mapped[bytes] = deferred(mapped_column(LargeBinary))
    source: Mapped[str] = mapped_column(String(20), default="upload", server_default="upload")
    extracted_text: Mapped[str | None] = deferred(mapped_column(Text))
    ai_summary: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ImportMappingTemplate(TimestampMixin, Base):
    __tablename__ = "import_mapping_template"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    header_signature: Mapped[str] = mapped_column(String(64), unique=True)
    mapping: Mapped[dict] = mapped_column(JSONB)
    options: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}")
    defaults: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}")


class ImportBatch(Base):
    __tablename__ = "import_batch"
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    attachment_id: Mapped[int | None] = mapped_column(ForeignKey("attachment.id", ondelete="SET NULL"))
    source_type: Mapped[str] = mapped_column(String(20))
    origin: Mapped[str] = mapped_column(String(20), default="upload", server_default="upload")
    status: Mapped[str] = mapped_column(String(20), default="mapping", server_default="mapping", index=True)
    sheet_name: Mapped[str | None] = mapped_column(String(120))
    sheets: Mapped[list] = mapped_column(JSONB, default=list, server_default="[]")
    columns: Mapped[list] = mapped_column(JSONB, default=list, server_default="[]")
    header_signature: Mapped[str | None] = mapped_column(String(64))
    template_id: Mapped[int | None] = mapped_column(ForeignKey("import_mapping_template.id", ondelete="SET NULL"))
    mapping: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}")
    mapping_source: Mapped[str | None] = mapped_column(String(20))
    options: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}")
    defaults: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}")
    doc_meta: Mapped[dict | None] = mapped_column(JSONB)
    stats: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}")
    row_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    error: Mapped[str | None] = mapped_column(Text)
    job_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    committed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    attachment: Mapped[Attachment | None] = relationship(lazy="joined")


class ImportRow(Base):
    __tablename__ = "import_row"
    __table_args__ = (Index("ix_import_row_batch_idx", "batch_id", "row_index"),)
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("import_batch.id", ondelete="CASCADE"))
    row_index: Mapped[int] = mapped_column(Integer)
    raw: Mapped[dict] = mapped_column(JSONB)
    txn_date: Mapped[date | None] = mapped_column(Date)
    posted_date: Mapped[date | None] = mapped_column(Date)
    description: Mapped[str | None] = mapped_column(Text)
    merchant: Mapped[str | None] = mapped_column(String(200))
    amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    account_id: Mapped[int | None] = mapped_column(ForeignKey("account.id", ondelete="SET NULL"))
    category_id: Mapped[int | None] = mapped_column(ForeignKey("category.id", ondelete="SET NULL"))
    category_hint: Mapped[str | None] = mapped_column(String(200))
    account_hint: Mapped[str | None] = mapped_column(String(200))
    notes: Mapped[str | None] = mapped_column(Text)
    check_number: Mapped[str | None] = mapped_column(String(30))
    external_id: Mapped[str | None] = mapped_column(String(200))
    fingerprint: Mapped[str | None] = mapped_column(String(64))
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(4, 3))
    errors: Mapped[list] = mapped_column(JSONB, default=list, server_default="[]")
    decision: Mapped[str] = mapped_column(String(20), default="pending", server_default="pending")
    transaction_id: Mapped[int | None] = mapped_column(ForeignKey("transaction.id", ondelete="SET NULL"))


class CategoryAlias(Base):
    """Maps an external category label (e.g. a bank's 'Bills & Utilities') to one of ours."""

    __tablename__ = "category_alias"
    id: Mapped[int] = mapped_column(primary_key=True)
    alias: Mapped[str] = mapped_column(String(200), unique=True)
    category_id: Mapped[int] = mapped_column(ForeignKey("category.id", ondelete="CASCADE"))


class DuplicatePair(Base):
    """A candidate duplicate: an existing transaction vs. another transaction or an incoming import row."""

    __tablename__ = "duplicate_pair"
    __table_args__ = (
        Index(
            "uq_duplicate_pair_txns",
            "txn_a_id",
            "txn_b_id",
            unique=True,
            postgresql_where=text("txn_b_id IS NOT NULL"),
        ),
        Index(
            "uq_duplicate_pair_row",
            "import_row_id",
            "txn_a_id",
            unique=True,
            postgresql_where=text("import_row_id IS NOT NULL"),
        ),
    )
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    txn_a_id: Mapped[int] = mapped_column(ForeignKey("transaction.id", ondelete="CASCADE"), index=True)
    txn_b_id: Mapped[int | None] = mapped_column(ForeignKey("transaction.id", ondelete="CASCADE"), index=True)
    import_row_id: Mapped[int | None] = mapped_column(ForeignKey("import_row.id", ondelete="CASCADE"))
    score: Mapped[Decimal] = mapped_column(Numeric(4, 3))
    reasons: Mapped[list] = mapped_column(JSONB, default=list, server_default="[]")
    ai_probability: Mapped[Decimal | None] = mapped_column(Numeric(4, 3))
    ai_reason: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(25), default="pending", server_default="pending", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

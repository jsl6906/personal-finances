from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Numeric,
    SmallInteger,
    String,
    Table,
    Text,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ledger.db.base import Base, TimestampMixin
from ledger.models.reference import Account, Category, CategoryRule, HouseholdMember, MerchantProfile, Tag

SOURCE_TYPES = ("manual", "spreadsheet", "document", "tiller", "simplefin", "backfill")

transaction_tag = Table(
    "transaction_tag",
    Base.metadata,
    Column("transaction_id", BigInteger, ForeignKey("transaction.id", ondelete="CASCADE"), primary_key=True),
    Column("tag_id", ForeignKey("tag.id", ondelete="CASCADE"), primary_key=True),
)


class Transaction(TimestampMixin, Base):
    __tablename__ = "transaction"
    __table_args__ = (
        Index("ix_transaction_account_date", "account_id", "txn_date"),
        Index("ix_transaction_live_date", "txn_date", postgresql_where=text("deleted_at IS NULL")),
        Index("ix_transaction_amount_date", "amount", "txn_date", postgresql_where=text("deleted_at IS NULL")),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    account_id: Mapped[int | None] = mapped_column(ForeignKey("account.id", ondelete="RESTRICT"))
    txn_date: Mapped[date] = mapped_column(Date)
    posted_date: Mapped[date | None] = mapped_column(Date)
    description: Mapped[str] = mapped_column(Text)
    original_description: Mapped[str | None] = mapped_column(Text)
    merchant: Mapped[str | None] = mapped_column(String(200), index=True)
    # 'user' = merchant set by hand; never recomputed from the description.
    merchant_source: Mapped[str | None] = mapped_column(String(10))
    # Negative = money out, positive = money in.
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    category_id: Mapped[int | None] = mapped_column(ForeignKey("category.id", ondelete="SET NULL"), index=True)
    category_source: Mapped[str | None] = mapped_column(String(20))
    # The rule that set category_id (category_source='rule').
    category_rule_id: Mapped[int | None] = mapped_column(
        ForeignKey("category_rule.id", ondelete="SET NULL"), index=True
    )
    suggested_category_id: Mapped[int | None] = mapped_column(ForeignKey("category.id", ondelete="SET NULL"))
    suggestion_confidence: Mapped[Decimal | None] = mapped_column(Numeric(4, 3))
    suggestion_reason: Mapped[str | None] = mapped_column(Text)
    member_id: Mapped[int | None] = mapped_column(ForeignKey("household_member.id", ondelete="SET NULL"))
    notes: Mapped[str | None] = mapped_column(Text)
    check_number: Mapped[str | None] = mapped_column(String(30))
    source_type: Mapped[str] = mapped_column(String(20), default="manual", server_default="manual")
    external_id: Mapped[str | None] = mapped_column(String(200), index=True)
    fingerprint: Mapped[str | None] = mapped_column(String(64), index=True)
    budget_spread_months: Mapped[int | None] = mapped_column(SmallInteger)
    import_batch_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("import_batch.id", ondelete="SET NULL"), index=True
    )
    # The opposite leg of a transfer between two household accounts (set on both rows by transfer matching).
    transfer_match_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("transaction.id", ondelete="SET NULL"), index=True
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    account: Mapped[Account | None] = relationship(lazy="joined")
    category: Mapped[Category | None] = relationship(foreign_keys=[category_id], lazy="joined")
    suggested_category: Mapped[Category | None] = relationship(foreign_keys=[suggested_category_id], lazy="joined")
    category_rule: Mapped[CategoryRule | None] = relationship(lazy="joined")
    member: Mapped[HouseholdMember | None] = relationship(lazy="joined")
    tags: Mapped[list[Tag]] = relationship(secondary=transaction_tag, lazy="selectin", order_by=Tag.name)
    merchant_profile: Mapped[MerchantProfile | None] = relationship(
        primaryjoin="foreign(Transaction.merchant) == MerchantProfile.key", viewonly=True, lazy="joined"
    )


SOURCE_ROLES = ("created", "matched")


class TransactionSource(Base):
    """A record backing up a transaction: the import row that created it, or a later import row that matched it."""

    __tablename__ = "transaction_source"
    __table_args__ = (
        Index(
            "uq_transaction_source_row",
            "transaction_id",
            "import_row_id",
            unique=True,
            postgresql_where=text("import_row_id IS NOT NULL"),
        ),
    )
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    transaction_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("transaction.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(String(10))
    import_batch_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("import_batch.id", ondelete="SET NULL"), index=True
    )
    import_row_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("import_row.id", ondelete="SET NULL"))
    attachment_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("attachment.id", ondelete="SET NULL"), index=True
    )
    # How this source recorded the transaction (can differ from the ledger row it was matched to).
    txn_date: Mapped[date | None] = mapped_column(Date)
    description: Mapped[str | None] = mapped_column(Text)
    amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    match_score: Mapped[Decimal | None] = mapped_column(Numeric(4, 3))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class TransactionNote(TimestampMixin, Base):
    """One of any number of notes on a transaction, typed by hand or captured from an imported document."""

    __tablename__ = "transaction_note"
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    transaction_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("transaction.id", ondelete="CASCADE"), index=True
    )
    body: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(20), default="user", server_default="user")
    import_batch_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("import_batch.id", ondelete="SET NULL"), index=True
    )
    attachment_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("attachment.id", ondelete="SET NULL"))

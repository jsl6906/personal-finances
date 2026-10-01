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
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ledger.db.base import Base, TimestampMixin
from ledger.models.reference import Account, Category, HouseholdMember, MerchantProfile, Tag

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
    member: Mapped[HouseholdMember | None] = relationship(lazy="joined")
    tags: Mapped[list[Tag]] = relationship(secondary=transaction_tag, lazy="selectin", order_by=Tag.name)
    merchant_profile: Mapped[MerchantProfile | None] = relationship(
        primaryjoin="foreign(Transaction.merchant) == MerchantProfile.key", viewonly=True, lazy="joined"
    )

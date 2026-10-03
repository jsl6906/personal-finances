from datetime import datetime
from decimal import Decimal

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Index, Integer, Numeric, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ledger.db.base import Base, TimestampMixin

CATEGORY_TYPES = ("expense", "income", "transfer")
ACCOUNT_TYPES = (
    "checking",
    "savings",
    "credit_card",
    "loan",
    "mortgage",
    "investment",
    "retirement",
    "cash",
    "property",
    "vehicle",
    "other",
)


class Institution(TimestampMixin, Base):
    __tablename__ = "institution"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    website: Mapped[str | None] = mapped_column(String(300))
    notes: Mapped[str | None] = mapped_column(Text)

    accounts: Mapped[list["Account"]] = relationship(back_populates="institution", passive_deletes=True)


class Account(TimestampMixin, Base):
    __tablename__ = "account"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    institution_id: Mapped[int | None] = mapped_column(ForeignKey("institution.id", ondelete="SET NULL"))
    account_type: Mapped[str] = mapped_column(String(30), default="checking")
    mask: Mapped[str | None] = mapped_column(String(10))
    is_hidden: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    is_closed: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    notes: Mapped[str | None] = mapped_column(Text)
    external_refs: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}")

    institution: Mapped[Institution | None] = relationship(back_populates="accounts", lazy="joined")


class HouseholdMember(TimestampMixin, Base):
    __tablename__ = "household_member"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    initials: Mapped[str] = mapped_column(String(4))
    email: Mapped[str | None] = mapped_column(String(254))


class CategoryGroup(TimestampMixin, Base):
    __tablename__ = "category_group"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    type: Mapped[str] = mapped_column(String(20), default="expense")
    sort_order: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    hide_from_reports: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")

    categories: Mapped[list["Category"]] = relationship(
        back_populates="group", order_by="Category.name", passive_deletes="all"
    )


class Category(TimestampMixin, Base):
    __tablename__ = "category"
    id: Mapped[int] = mapped_column(primary_key=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("category_group.id", ondelete="RESTRICT"))
    name: Mapped[str] = mapped_column(String(100), unique=True)
    type: Mapped[str] = mapped_column(String(20), default="expense")
    hide_from_reports: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    description: Mapped[str | None] = mapped_column(Text)

    group: Mapped[CategoryGroup] = relationship(back_populates="categories", lazy="joined")


class Tag(TimestampMixin, Base):
    __tablename__ = "tag"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80), unique=True)
    color: Mapped[str | None] = mapped_column(String(20))


RULE_MATCH_TYPES = ("merchant", "contains", "regex")
RULE_SOURCES = ("user", "learned", "ai")


class CategoryRule(TimestampMixin, Base):
    """Auto-categorization: the first active match (priority, then newest) categorizes an uncategorized transaction.

    match_type: merchant = exact merchant key; contains = case-insensitive substring of the description;
    regex = case-insensitive Postgres regex on the description. Account and |amount| range narrow the match.
    """

    __tablename__ = "category_rule"
    __table_args__ = (Index("ix_category_rule_match", "match_type", "pattern"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    match_type: Mapped[str] = mapped_column(String(20), default="merchant", server_default="merchant")
    pattern: Mapped[str] = mapped_column(String(300))
    category_id: Mapped[int] = mapped_column(ForeignKey("category.id", ondelete="CASCADE"), index=True)
    account_id: Mapped[int | None] = mapped_column(ForeignKey("account.id", ondelete="CASCADE"))
    amount_min: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    amount_max: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    priority: Mapped[int] = mapped_column(Integer, default=100, server_default="100")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    source: Mapped[str] = mapped_column(String(20), default="user", server_default="user")
    note: Mapped[str | None] = mapped_column(Text)


class MerchantProfile(TimestampMixin, Base):
    """User curation of a merchant key: a display name, or `alias_of` to merge it into another (canonical) key."""

    __tablename__ = "merchant_profile"
    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(200), unique=True)
    display_name: Mapped[str | None] = mapped_column(String(200))
    alias_of: Mapped[str | None] = mapped_column(String(200), index=True)
    # Set once an AI merchant review has looked at the key and the user acted (or nothing was suggested).
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


MERCHANT_SUGGESTION_KINDS = ("merge", "rename")
MERCHANT_SUGGESTION_STATUSES = ("pending", "accepted", "dismissed")


class MerchantSuggestion(TimestampMixin, Base):
    """AI merchant-review proposal: merge `source_keys` into `target_key` and/or give it `display_name`."""

    __tablename__ = "merchant_suggestion"
    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int | None] = mapped_column(BigInteger)
    kind: Mapped[str] = mapped_column(String(20))
    target_key: Mapped[str] = mapped_column(String(200))
    source_keys: Mapped[list[str]] = mapped_column(JSONB, default=list, server_default="[]")
    display_name: Mapped[str | None] = mapped_column(String(200))
    reason: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="pending", server_default="pending", index=True)

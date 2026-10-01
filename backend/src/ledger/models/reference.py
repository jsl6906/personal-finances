from sqlalchemy import Boolean, ForeignKey, Integer, String, Text, UniqueConstraint
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


class MerchantRule(TimestampMixin, Base):
    """Learned or user-defined mapping from a normalized merchant to a category."""

    __tablename__ = "merchant_rule"
    __table_args__ = (UniqueConstraint("merchant"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    merchant: Mapped[str] = mapped_column(String(200))
    category_id: Mapped[int] = mapped_column(ForeignKey("category.id", ondelete="CASCADE"))
    source: Mapped[str] = mapped_column(String(20), default="learned")
    hits: Mapped[int] = mapped_column(Integer, default=1, server_default="1")


class MerchantProfile(TimestampMixin, Base):
    """User curation of a merchant key: a display name, or `alias_of` to merge it into another (canonical) key."""

    __tablename__ = "merchant_profile"
    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(200), unique=True)
    display_name: Mapped[str | None] = mapped_column(String(200))
    alias_of: Mapped[str | None] = mapped_column(String(200), index=True)

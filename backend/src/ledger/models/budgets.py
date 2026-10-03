from decimal import Decimal

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, Index, Numeric, SmallInteger, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from ledger.db.base import Base, TimestampMixin

PERIOD_TYPES = ("month", "quarter", "year")
PERIOD_MONTHS = {"month": 1, "quarter": 3, "year": 12}


class Budget(TimestampMixin, Base):
    """Budget for one category, one category group, or all spending (both targets NULL), per month/quarter/year.

    A group or overall budget is shown net of the narrower budgets inside it ("everything else").
    """

    __tablename__ = "budget"
    __table_args__ = (
        CheckConstraint("NOT (category_id IS NOT NULL AND group_id IS NOT NULL)", name="one_target"),
        CheckConstraint("period_type IN ('month', 'quarter', 'year')", name="period_type"),
        Index(
            "uq_budget_overall",
            text("(true)"),
            unique=True,
            postgresql_where=text("category_id IS NULL AND group_id IS NULL"),
        ),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    category_id: Mapped[int | None] = mapped_column(ForeignKey("category.id", ondelete="CASCADE"), unique=True)
    group_id: Mapped[int | None] = mapped_column(ForeignKey("category_group.id", ondelete="CASCADE"), unique=True)
    period_type: Mapped[str] = mapped_column(String(10), default="month")
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    notes: Mapped[str | None] = mapped_column(Text)


class SpreadRule(TimestampMixin, Base):
    """Spreads matching transactions evenly over N months for budgeting (e.g. an annual premium over 12)."""

    __tablename__ = "spread_rule"
    __table_args__ = (CheckConstraint("months BETWEEN 1 AND 60", name="months"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    category_id: Mapped[int | None] = mapped_column(ForeignKey("category.id", ondelete="CASCADE"))
    merchant_pattern: Mapped[str | None] = mapped_column(String(200))
    min_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    months: Mapped[int] = mapped_column(SmallInteger, default=12)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")

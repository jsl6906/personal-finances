from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Date, DateTime, ForeignKey, Numeric, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from ledger.db.base import Base, TimestampMixin


class Asset(TimestampMixin, Base):
    """Valuation settings for a property/vehicle account; the values themselves are account_balance rows."""

    __tablename__ = "asset"
    account_id: Mapped[int] = mapped_column(ForeignKey("account.id", ondelete="CASCADE"), primary_key=True)
    method: Mapped[str] = mapped_column(String(20), default="manual", server_default="manual")
    loan_account_id: Mapped[int | None] = mapped_column(ForeignKey("account.id", ondelete="SET NULL"))
    purchase_date: Mapped[date | None] = mapped_column(Date)
    purchase_price: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    address: Mapped[str | None] = mapped_column(String(300))
    depreciation_rate: Mapped[Decimal | None] = mapped_column(Numeric(5, 4))
    last_valued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    last_result: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}")

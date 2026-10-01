"""seed categories from the household's Tiller taxonomy

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-28

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# (group, type, group_hidden, [(category, hidden), ...])
TAXONOMY = [
    ("Business Services", "expense", False, [
        "Advertising", "Business Services", "Cleaning Service", "Financial", "Legal", "Office Supplies",
        "Private Practice Expenses", "Professional Develop", "Shipping", "Synoptic Expense"]),
    ("Cable / Phone / Internet", "expense", False, ["Cable/Internet", "Mobile Phone", "Television"]),
    ("Food & Dining", "expense", False, [
        "Alcohol & Bars", "Coffee Shops", "Fast Food", "Food & Dining", "Food Delivery", "Groceries", "Restaurants"]),
    ("Health & Medical", "expense", False, ["Dentist", "Doctor", "Doctor - Mental Health", "Eyecare", "Pharmacy"]),
    ("Kids", "expense", False, ["Babysitter & Daycare", "Education", "Kids Activities", "Misc Kids"]),
    ("Mortgage & Rent", "expense", False, ["Mortgage & Rent"]),
    ("Other Expenses", "expense", False, [
        "Charity", "Finance Charge", "Hair", "Laundry", "Personal Care", "Service Fee", "Unclassified"]),
    ("Other Loan Payment", "expense", False, ["Loan Payment", "Loan Transaction", "Loans"]),
    ("Pets", "expense", False, ["Pet Food & Supplies", "Veterinary"]),
    ("Shopping", "expense", False, [
        "Amazon", "Books, Amusement, & Entertainment", "Clothing", "Convenience Stores", "Costco", "Furnishings",
        "Gifts & Donations", "Macy's", "Shopping", "Target"]),
    ("Student Loan", "expense", False, ["Student Loan"]),
    ("Taxes", "expense", False, ["Federal Tax", "Property Tax", "State Tax"]),
    ("Transportation & Travel", "expense", False, [
        "Air Travel", "Auto & Transport", "Auto Insurance", "Auto Payment", "Gas & Fuel", "Hotel", "Parking",
        "Public Transportation", "Rental Car & Taxi", "Service & Parts", "Travel", "Vacation"]),
    ("Utilities & Home Services", "expense", False, ["Electric", "Home Center & Tools", "Home Services", "Water"]),
    ("Hide", "expense", True, ["Buy", "Investments"]),
    ("Paycheck", "income", False, ["Paycheck Income"]),
    ("Private Practice Income", "income", False, ["PRIV PRACTICE INCOME"]),
    ("Data Viz Income", "income", False, ["SYNOPTIC INCOME"]),
    ("Home Care Income", "income", False, ["Home Care Paycheck"]),
    ("Other Income", "income", False, ["Income", "Interest Income", "Reimbursement", "TSP Deposit",
                                       ("Change in Investment", True)]),
    ("Transfer", "transfer", False, ["Credit Card Payment", "Transfer"]),
]


def upgrade() -> None:
    conn = op.get_bind()
    for order, (group, gtype, ghide, cats) in enumerate(TAXONOMY):
        gid = conn.execute(
            sa.text(
                """--sql
                INSERT INTO personal_finances.category_group (name, type, sort_order, hide_from_reports)
                VALUES (:n, :t, :o, :h)
                ON CONFLICT (name) DO UPDATE SET sort_order = EXCLUDED.sort_order
                RETURNING id
                """
            ),
            {"n": group, "t": gtype, "o": order * 10, "h": ghide},
        ).scalar_one()
        for cat in cats:
            name, hidden = cat if isinstance(cat, tuple) else (cat, ghide)
            conn.execute(
                sa.text(
                    """--sql
                    INSERT INTO personal_finances.category (group_id, name, type, hide_from_reports)
                    VALUES (:g, :n, :t, :h)
                    ON CONFLICT (name) DO NOTHING
                    """
                ),
                {"g": gid, "n": name, "t": gtype, "h": hidden},
            )


def downgrade() -> None:
    # Seed rows may be referenced by transactions; leave them in place.
    pass

"""Generate realistic sample statement/bill PDFs for exercising the backfill end to end.

uv run --with reportlab python scripts/make_sample_docs.py <output-folder>
"""

import sys
from pathlib import Path

from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas


def bank_statement(path: Path) -> None:
    c = canvas.Canvas(str(path), pagesize=letter)
    y = 740
    for line in (
        "MAPLE CREDIT UNION",
        "Member Checking Statement   Account ending 3817",
        "Statement period: 03/01/2017 - 03/31/2017",
        "Beginning balance: $2,410.55      Ending balance: $2,883.24",
    ):
        c.drawString(60, y, line)
        y -= 18
    y -= 12
    c.drawString(60, y, "Date      Description                                   Amount        Balance")
    y -= 16
    bal = 2410.55
    for d, desc, amt in (
        ("03/01", "PAYROLL DIRECT DEP ACME CORP", 1850.00),
        ("03/04", "KROGER #412 SPRINGFIELD IL", -132.18),
        ("03/09", "SPRINGFIELD WATER DEPT AUTOPAY", -47.63),
        ("03/15", "AMEREN ILLINOIS ONLINE PMT", -98.40),
        ("03/21", "SHELL OIL 5744", -38.10),
        ("03/28", "TRANSFER TO SAVINGS", -1061.00),
    ):
        bal += amt
        c.drawString(60, y, f"{d}     {desc:<44} {amt:>10,.2f}   {bal:>10,.2f}")
        y -= 16
    c.save()


def water_bill(path: Path) -> None:
    c = canvas.Canvas(str(path), pagesize=letter)
    y = 740
    for line in (
        "CITY OF SPRINGFIELD WATER DEPARTMENT",
        "Account number: 000-44210-07",
        "Bill date: 02/20/2017        Due date: 03/10/2017",
        "Service period: 01/18/2017 - 02/17/2017",
        "",
        "Water usage: 4,120 gallons",
        "Water charge                               $31.80",
        "Sewer charge                               $15.83",
        "TOTAL AMOUNT DUE                           $47.63",
        "",
        "Paid by AutoPay on the due date.",
    ):
        c.drawString(60, y, line)
        y -= 18
    c.save()


if __name__ == "__main__":
    out = Path(sys.argv[1])
    (out / "2017" / "bank").mkdir(parents=True, exist_ok=True)
    (out / "2017" / "utilities").mkdir(parents=True, exist_ok=True)
    bank_statement(out / "2017" / "bank" / "maple_checking_2017-03.pdf")
    water_bill(out / "2017" / "utilities" / "water_2017-02.pdf")
    (out / "2017" / "old_budget_export.csv").write_text(
        "Date,Payee,Amount,Account\n02/11/2017,COSTCO WHOLESALE,-184.22,Maple Checking\n"
        "02/19/2017,NETFLIX.COM,-9.99,Maple Checking\n",
        encoding="utf-8",
    )
    print("wrote samples to", out)

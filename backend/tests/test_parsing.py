from datetime import date
from decimal import Decimal

from ledger.imports.parsing import (
    heuristic_mapping,
    mapping_complete,
    parse_amount,
    parse_date,
    read_table,
)


def test_parse_amount_variants():
    assert parse_amount("$1,234.56") == Decimal("1234.56")
    assert parse_amount("(42.10)") == Decimal("-42.10")
    assert parse_amount("42.10-") == Decimal("-42.10")
    assert parse_amount("−61.40") == Decimal("-61.40")
    assert parse_amount("12.00 CR") == Decimal("12.00")
    assert parse_amount("12.00 DR") == Decimal("-12.00")
    assert parse_amount("abc") is None
    assert parse_amount("") is None


def test_parse_date_variants():
    assert parse_date("2026-09-26") == date(2026, 9, 26)
    assert parse_date("09/26/2026") == date(2026, 9, 26)
    assert parse_date("9/6/26") == date(2026, 9, 6)
    assert parse_date("26/09/2026", dayfirst=True) == date(2026, 9, 26)
    assert parse_date("Sep 26, 2026") == date(2026, 9, 26)
    assert parse_date("46291") == date(2026, 9, 26)  # Excel serial
    assert parse_date("26.09.2026", fmt="%d.%m.%Y") == date(2026, 9, 26)
    assert parse_date("Kroger") is None
    assert parse_date("142.18") is None


def test_csv_with_preamble_and_heuristic_mapping():
    data = (
        b"Account Activity for CHASE SAPPHIRE ...4471\n"
        b"Generated 09/28/2026\n"
        b"\n"
        b"Transaction Date,Post Date,Description,Category,Type,Amount,Memo\n"
        b"09/26/2026,09/27/2026,KROGER #412 SPRINGFIELD IL,Groceries,Sale,-142.18,\n"
        b"09/24/2026,09/25/2026,COSTCO GAS #1157,Gas,Sale,-61.40,\n"
    )
    t = read_table(data, "chase.csv")
    assert t.headers == ["Transaction Date", "Post Date", "Description", "Category", "Type", "Amount", "Memo"]
    assert len(t.rows) == 2
    assert t.rows[0]["Description"] == "KROGER #412 SPRINGFIELD IL"
    m = heuristic_mapping(t.headers)
    assert m["Transaction Date"] == "txn_date"
    assert m["Post Date"] == "posted_date"
    assert m["Description"] == "description"
    assert m["Amount"] == "amount"
    assert m["Category"] == "category"
    assert m["Memo"] == "description" or m["Memo"] == "notes" or m["Memo"] == "ignore"
    assert mapping_complete(m)


def test_tiller_headers_mapping():
    headers = [
        "Date",
        "Description",
        "Category",
        "Amount",
        "Account",
        "Account #",
        "Institution",
        "Month",
        "Week",
        "Transaction ID",
        "Account ID",
        "Check Number",
        "Full Description",
        "Date Added",
    ]
    m = heuristic_mapping(headers)
    assert m["Date"] == "txn_date"
    assert m["Transaction ID"] == "external_id"
    assert m["Full Description"] == "original_description"
    assert m["Account"] == "account"
    assert m["Account #"] == "ignore"
    assert m["Date Added"] == "ignore"


def test_xlsx_roundtrip(tmp_path):
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "Transactions"
    ws.append(["Date", "Payee", "Debit", "Credit"])
    ws.append([date(2026, 9, 1), "PAYROLL", None, 3412.55])
    ws.append([date(2026, 9, 2), "AMEREN ILLINOIS", 163.9, None])
    wb.create_sheet("Other")
    path = tmp_path / "t.xlsx"
    wb.save(path)
    t = read_table(path.read_bytes(), "t.xlsx")
    assert t.sheets == ["Transactions", "Other"]
    assert t.rows[0] == {"Date": "2026-09-01", "Payee": "PAYROLL", "Debit": "", "Credit": "3412.55"}
    m = heuristic_mapping(t.headers)
    assert (m["Payee"], m["Debit"], m["Credit"]) == ("description", "debit", "credit")

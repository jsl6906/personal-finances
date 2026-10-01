from datetime import date
from decimal import Decimal

from ledger.services.normalize import fingerprint, normalize_merchant


def test_normalize_merchant_strips_store_numbers_and_noise():
    assert normalize_merchant("KROGER #412 SPRINGFIELD IL") == "kroger springfield"
    assert normalize_merchant("AMAZON.COM*2K4LM1 AMZN.COM/BILL WA") == normalize_merchant("AMAZON.COM*9ZZ AMZN.COM/BILL WA")
    assert normalize_merchant("POS DEBIT TRADER JOE'S #702") == "trader joe's"
    assert normalize_merchant("") is None
    a = "Verizon Wireless Payments~ Future Amount: 212.73 ~ Tran: Achdw"
    b = "Verizon Wireless Payments~ Future Amount: 1,216.37 ~ Tran: Achdw"
    assert normalize_merchant(a) == normalize_merchant(b) == "verizon wireless payments future amount tran achdw"


def test_fingerprint_stable_across_description_noise():
    a = fingerprint(1, date(2026, 9, 26), Decimal("-142.18"), "KROGER #412 SPRINGFIELD IL")
    b = fingerprint(1, date(2026, 9, 26), Decimal("-142.180"), "KROGER #413 SPRINGFIELD IL")
    c = fingerprint(2, date(2026, 9, 26), Decimal("-142.18"), "KROGER #412 SPRINGFIELD IL")
    assert a == b
    assert a != c

"""Venmo review (2026-10-05): categories, counterparty merchants and memos from the Venmo app history.

Only existing ledger rows are touched (Venmo entries funded from unconnected accounts are not added).
Memo goes into notes when notes are empty, otherwise into a "Venmo: ..." transaction note.

Dry run by default; --apply commits. Run: scripts/with_env.ps1 .env.azure python scripts/venmo_cleanup.py
"""

import asyncio
import json
import sys
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select

from ledger.db.engine import dispose_engine, get_sessionmaker
from ledger.models import Category, Transaction, TransactionNote
from ledger.services import merchants

ROOT = Path(__file__).resolve().parents[2]
KEEP = None

# (txn id, expected date, expected amount, merchant or KEEP, category or KEEP, Venmo memo)
CHANGES = [
    (2419, "2025-10-14", "-200.00", "Shannon Matheny", KEEP, "Emmett Latimore (wrestling registration)"),
    (2512, "2025-09-29", "-15.00", "Ava Melendrez", "Travel & Vacation", "Thanks 🙏😻 (cat sitting)"),
    (2636, "2025-09-08", "-10.00", "Amanda Wisley", KEEP, "Face paint"),
    (2637, "2025-09-08", "-10.00", "Amanda Wisley", KEEP, "Facepaint"),
    (2914, "2025-07-30", "-27.00", "Laura Miskimins", KEEP, "🎂🎉 dinner"),
    (2921, "2025-07-28", "-30.00", "Ava Melendrez", KEEP, "Babysitting 😊🤩🙏"),
    (3013, "2025-07-18", "-30.00", "Kelly Hugunin", "Gifts & Donations", "BKS gift"),
    (3235, "2025-06-17", "-6.00", "Tina Nguyen", KEEP, "Nails thank you!"),
    (3249, "2025-06-16", "-50.00", "Ava Melendrez", "Travel & Vacation", "Thank you! 🐱 💕 (cat sitting)"),
    (3693, "2025-03-31", "-30.00", "Laura Miskimins", KEEP, "🌻 🙏 😍"),
    (3761, "2025-03-17", "-50.00", "Becky Kassman", KEEP, "Wegmans 🍕💕"),
    (3817, "2025-03-03", "-12.00", "Becky Kassman", KEEP, "Wegmans 🛒❤️"),
    (4134, "2025-01-06", "-22.00", "Becky Kassman", KEEP, "Groceries 🤗"),
    (4592, "2024-11-04", "-20.00", "Meghean Melendrez", KEEP, "🍕 🎃"),
    (4815, "2024-10-04", "-200.00", "Shannon Matheny", KEEP, "Wrestling registration for Emmett Latimore"),
    (5116, "2024-08-19", "-8.00", "Becky Kassman", KEEP, "🙏 💗"),
    (5859, "2024-05-22", "-30.00", "Nancy Hall", "Restaurants", "Clare's BD dinner 🍕 🎉"),
    (6501, "2024-02-27", "110.00", "Dawn Smith", "Income", "Cash-out of Dawn Smith's payment: Furniture. Thank you!"),
    (6642, "2024-02-07", "-39.00", "Nancy Hall", "Private Practice Expenses", "Dominion ⛽"),
    (6666, "2024-02-05", "-4.00", "Becky Kassman", "Groceries", "Wegmans 🙏 😀"),
    (
        6898, "2024-01-05", "125.00", KEEP, "Income",
        "Cash-out of Venmo balance: Anam Ahmad $100 'Thanks!!' (2022-02-05), Xavier Medrano $10 'Discount' "
        "on the bouncy (2022-01-28), Catherine Torgersen $15 'Storage bins' (2024-01-02)",
    ),
    (9352, "2023-06-21", "-121.00", "Mojgan Jamali", "Events & Attractions", "Tix"),
    (10964, "2023-01-30", "-12.00", "Andrew Kasman", "Restaurants", "🍗 🐔 🙏"),
    (11905, "2022-11-07", "-270.00", "Mohammad Tehrani", "Restaurants", "Kabob 🙏👍😋😋"),
    (12461, "2022-09-19", "-600.00", "Mohammad Tehrani", "Gifts & Donations", "Happy birthday 🎉🎂"),
    (12690, "2022-08-29", "-30.00", "Akram Moteepour", "Gifts & Donations", "Sima"),
    (13267, "2022-06-27", "-20.00", "Anna Smith", KEEP, "Stroller"),
    (13385, "2022-06-14", "-25.00", "Scott Smith", KEEP, "Batman"),
    (13432, "2022-06-06", "-25.00", "JJ Mahoney", KEEP, "25"),
    (13431, "2022-06-06", "-1500.00", "Akram Moteepour", KEEP, "❤"),
    (13430, "2022-06-06", "-252.00", "Mohammad Tehrani", KEEP, "Catering! Thank you so much!"),
    (13832, "2022-03-22", "-260.00", "Mohammad Tehrani", "Restaurants", "Shamshiri and ⛽ 🙏"),
    (14050, "2022-02-22", "-15.00", "Frederic Barasse", KEEP, "Lego"),
    (14314, "2022-01-27", "-20.00", "Xavier Medrano", KEEP, "Bouncy ($10 refunded 2022-01-28 as 'Discount')"),
    (14711, "2021-12-20", "-5.00", "Paula Cordero Salas", KEEP, "Carry on"),
    (15369, "2021-10-04", "-800.00", "Akram Moteepour", KEEP, "🙏"),
    (16771, "2021-05-10", "-50.00", "Akram Moteepour", "Gifts & Donations", "Happy mother's day Shima joon!"),
    (17140, "2021-03-22", "-50.00", "Akram Moteepour", "Gifts & Donations", "دشت سال نو"),
    (17665, "2020-12-28", "-100.00", "Akram Moteepour", "Gifts & Donations", "🎅🎄🎁 merry Christmas!"),
    (17772, "2020-12-14", "-500.00", "Akram Moteepour", "Gifts & Donations", "Happy birthday🎂🎉"),
    (18378, "2020-09-18", "-500.00", "Mohammad Tehrani", "Gifts & Donations", "Happy birthday!!!"),
    (18796, "2020-06-22", "-500.00", "Mohammad Tehrani", "Gifts & Donations", "Happy Father's day to you!"),
]


async def run(apply: bool) -> None:
    log: list[str] = []
    async with get_sessionmaker()() as s:
        cats = {c.name: c for c in (await s.scalars(select(Category))).all()}
        if missing := sorted({c for *_, c, _ in CHANGES if c} - set(cats)):
            raise SystemExit(f"Missing categories {missing}; aborting")
        ids = [c[0] for c in CHANGES]
        txns = {t.id: t for t in (await s.scalars(select(Transaction).where(Transaction.id.in_(ids)))).unique()}
        notes = {
            (n.transaction_id, n.body)
            for n in (await s.scalars(select(TransactionNote).where(TransactionNote.transaction_id.in_(ids)))).all()
        }
        backup = []
        for tid, d, amt, merchant, cat, memo in CHANGES:
            t = txns.get(tid)
            if t is None or t.deleted_at or t.txn_date != date.fromisoformat(d) or t.amount != Decimal(amt):
                raise SystemExit(f"#{tid} not found or changed ({t and (t.txn_date, t.amount)}); aborting")
            backup.append({"id": tid, "merchant": t.merchant, "merchant_source": t.merchant_source,
                           "category_id": t.category_id, "category_source": t.category_source,
                           "category_rule_id": t.category_rule_id, "notes": t.notes})
            parts = [f"#{tid:<6} {d} {amt:>9}"]
            if merchant:
                parts.append(f"merchant {t.merchant!r} -> {merchant!r}")
                await merchants.set_transaction_merchant(s, t, merchant)
            if cat and cats[cat].id != t.category_id:
                parts.append(f"category {t.category.name if t.category else None!r} -> {cat!r}")
                t.category_id, t.category_source, t.category_rule_id = cats[cat].id, "user", None
                t.suggested_category_id = t.suggestion_confidence = t.suggestion_reason = None
            if not t.notes:
                parts.append(f"notes = {memo!r}")
                t.notes = memo
            elif (tid, body := f"Venmo: {memo}") not in notes:
                parts.append(f"note + {body!r} (notes {t.notes!r})")
                s.add(TransactionNote(transaction_id=tid, body=body, source="user"))
            log.append("  ".join(parts))
        if apply:
            out = ROOT / "logs" / f"venmo_cleanup_backup_{datetime.now(UTC):%Y%m%d%H%M%S}.json"
            out.write_text(json.dumps(backup, default=str), encoding="utf-8")
            log.append(f"backup: {out}")
            await s.commit()
        else:
            await s.rollback()
    await dispose_engine()
    print(("APPLIED" if apply else "DRY RUN") + "\n" + "\n".join(log))


if __name__ == "__main__":
    asyncio.run(run("--apply" in sys.argv))

"""Retire the legacy merchant categories (Amazon, Costco, Target, Macy's) in favor of merchants + Shopping > Retail.

- Store/URL merchant keys merge into one merchant each (distinct services like Costco Gas/Travel, Amazon Prime/Kindle/
  Video/Fresh, card transfers and payments stay separate).
- Legacy-category rows and generic 'Shopping' rows for those merchants -> Retail (Macy's -> Clothing); card
  transfers/payments -> Credit Card Payment; Costco Cash Reward -> Reimbursement.
- User merchant rules send future uncategorized rows to the same categories; legacy names become category aliases so
  Tiller labels keep resolving; the legacy categories are deleted.

Dry run by default; pass --apply to commit. Run: scripts/with_env.ps1 .env.azure python scripts/retail_categories.py
"""

import asyncio
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ledger.analytics.transfers import match_transfers
from ledger.db.engine import dispose_engine, get_engine
from ledger.services import merchants
from ledger.services.categorize import apply_rules

RETAIL, CLOTHING, CC_PAYMENT, REIMBURSEMENT = "Retail", "Clothing", "Credit Card Payment", "Reimbursement"
RETAIL_DESCRIPTION = "General merchandise from big-box and online retailers (Amazon, Costco, Target)."

# canonical key: (display name, include pattern, exclude pattern, category for its rows)
MERCHANTS = {
    "amazon": (
        "Amazon",
        r"^(& )?(amazon|amzn|amz)\b|^amazonstores\b",
        r"prime|kindle|video|digit|fresh|grocery|kids|pharma|audible|transfer|web services|aws",
        RETAIL,
    ),
    "costco": ("Costco", r"^(www )?costco\b", r"gas|travel|cash reward|renewal|membership|instacart", RETAIL),
    "target": ("Target", r"^target\b", None, RETAIL),
    "macy's": ("Macy's", r"^macys\b", r"^macys$|^macys online", CLOTHING),
}
# legacy category: default destination
LEGACY = {"Amazon": RETAIL, "Costco": RETAIL, "Target": RETAIL, "Macy's": CLOTHING}
# Non-purchase merchant keys inside the legacy categories.
SPECIAL = [
    (re.compile(r"^transfer |^macys$|^macys online"), CC_PAYMENT),
    (re.compile(r"^costco cash reward"), REIMBURSEMENT),
]
ROOT = Path(__file__).resolve().parents[2]


async def q(s: AsyncSession, sql: str, **params):
    return await s.execute(text(sql), params)


def canonical_for(key: str) -> str | None:
    for canon, (_, inc, exc, _) in MERCHANTS.items():
        if key != canon and re.search(inc, key) and not (exc and re.search(exc, key)):
            return canon
    return None


def legacy_target(key: str | None, legacy_name: str) -> str:
    for pat, cat in SPECIAL:
        if key and pat.search(key):
            return cat
    return LEGACY[legacy_name]


async def run(apply: bool) -> None:
    now = datetime.now(UTC)
    log: list[str] = []
    async with get_engine().connect() as conn:
        txn = await conn.begin()
        async with AsyncSession(bind=conn, join_transaction_mode="create_savepoint") as s:
            cats = {r.name: r.id for r in await q(s, "SELECT id, name FROM category")}
            missing = [n for n in [*LEGACY, CLOTHING, CC_PAYMENT, REIMBURSEMENT, "Shopping"] if n not in cats]
            if missing or RETAIL in cats:
                raise SystemExit(f"Taxonomy changed since the review, aborting (missing {missing}, Retail exists?)")
            legacy_ids = {cats[n]: n for n in LEGACY}

            if apply:
                tx = await q(
                    s,
                    'SELECT id, category_id, category_source, merchant FROM "transaction" '
                    "WHERE category_id = ANY(:c) OR merchant ~ '(amazon|amzn|amz|costco|target|macys)'",
                    c=[*legacy_ids, cats["Shopping"]],
                )
                backup = {
                    "category": [
                        dict(r._mapping) for r in await q(s, "SELECT * FROM category WHERE id = ANY(:c)", c=list(legacy_ids))
                    ],
                    "transaction": [dict(r._mapping) for r in tx],
                    "import_row": [
                        dict(r._mapping)
                        for r in await q(
                            s, "SELECT id, category_id FROM import_row WHERE category_id = ANY(:c)", c=list(legacy_ids)
                        )
                    ],
                    "anomaly": [
                        dict(r._mapping)
                        for r in await q(
                            s, "SELECT id, category_id FROM anomaly WHERE category_id = ANY(:c)", c=list(legacy_ids)
                        )
                    ],
                }
                out = ROOT / "logs" / f"retail_categories_backup_{now:%Y%m%d%H%M%S}.json"
                out.write_text(json.dumps(backup, default=str), encoding="utf-8")
                log.append(f"backup: {out}")

            shopping_group = (await q(s, "SELECT group_id FROM category WHERE id = :i", i=cats["Shopping"])).scalar_one()
            cats[RETAIL] = (
                await q(
                    s,
                    "INSERT INTO category (group_id, name, type, description) VALUES (:g, :n, 'expense', :d) RETURNING id",
                    g=shopping_group,
                    n=RETAIL,
                    d=RETAIL_DESCRIPTION,
                )
            ).scalar_one()
            log.append(f"created category {RETAIL} (id {cats[RETAIL]}) in group {shopping_group}")

            # 1. Merchant merges.
            keys = Counter(
                dict(
                    (await q(s, 'SELECT merchant, count(*) FROM "transaction" WHERE merchant IS NOT NULL GROUP BY 1')).all()
                )
            )
            for k in (await q(s, "SELECT key FROM merchant_profile WHERE alias_of IS NULL")).scalars():
                keys.setdefault(k, 0)
            plan: dict[str, list[str]] = defaultdict(list)
            for k in sorted(keys):
                if canon := canonical_for(k):
                    plan[canon].append(k)
            for canon, (display, *_rest) in MERCHANTS.items():
                await merchants.rename(s, canon, display)
                log.append(f"merchant {display!r} <- {sum(keys[k] for k in plan[canon])} rows from {len(plan[canon])} keys")
                for k in plan[canon]:
                    await merchants.merge(s, k, canon)
                    log.append(f"    {keys[k]:>5}  {k}")
            await s.flush()

            # 2. Legacy-category rows (all, including soft-deleted, so the categories can be deleted).
            rows = (
                await q(
                    s, 'SELECT id, category_id, merchant FROM "transaction" WHERE category_id = ANY(:c)', c=list(legacy_ids)
                )
            ).all()
            moves: dict[str, list[int]] = defaultdict(list)
            summary: Counter = Counter()
            for r in rows:
                dest = legacy_target(r.merchant, legacy_ids[r.category_id])
                moves[dest].append(r.id)
                summary[(legacy_ids[r.category_id], dest, r.merchant if dest not in (RETAIL, CLOTHING) else "*")] += 1
            for (src, dest, m), n in sorted(summary.items()):
                log.append(f"legacy {src} -> {dest}: {n}" + ("" if m == "*" else f"  ({m})"))

            # 3. Generic 'Shopping' rows for the canonical merchants.
            for canon, (display, _, _, dest) in MERCHANTS.items():
                ids = (
                    (
                        await q(
                            s,
                            'SELECT id FROM "transaction" WHERE category_id = :c AND merchant = :m',
                            c=cats["Shopping"],
                            m=canon,
                        )
                    )
                    .scalars()
                    .all()
                )
                moves[dest].extend(ids)
                log.append(f"Shopping -> {dest} ({display}): {len(ids)}")

            for dest, ids in moves.items():
                await q(
                    s,
                    'UPDATE "transaction" SET category_id = :c, suggested_category_id = NULL, suggestion_confidence = NULL, '
                    "suggestion_reason = NULL WHERE id = ANY(:ids)",
                    c=cats[dest],
                    ids=ids,
                )

            # 4. Rules for future rows + currently uncategorized ones.
            for canon, (_, _, _, dest) in MERCHANTS.items():
                await q(
                    s,
                    "INSERT INTO merchant_rule (merchant, category_id, source, hits) VALUES (:m, :c, 'user', 1) "
                    "ON CONFLICT (merchant) DO UPDATE SET category_id = EXCLUDED.category_id, source = 'user'",
                    m=canon,
                    c=cats[dest],
                )
            uncat = (
                (
                    await q(
                        s,
                        'SELECT id FROM "transaction" WHERE category_id IS NULL AND deleted_at IS NULL '
                        "AND merchant = ANY(:m)",
                        m=list(MERCHANTS),
                    )
                )
                .scalars()
                .all()
            )
            log.append(
                f"uncategorized rows categorized by the new rules: {await apply_rules(s, list(uncat)) if uncat else 0}"
            )

            # 5. Other references, aliases, then drop the legacy categories.
            for table in ("import_row", "anomaly"):
                for cid, name in legacy_ids.items():
                    res = await q(
                        s, f"UPDATE {table} SET category_id = :d WHERE category_id = :c", d=cats[LEGACY[name]], c=cid
                    )
                    if res.rowcount:
                        log.append(f"{table}: {name} -> {LEGACY[name]}: {res.rowcount}")
            left = (
                await q(s, 'SELECT count(*) FROM "transaction" WHERE category_id = ANY(:c)', c=list(legacy_ids))
            ).scalar_one()
            if left:
                raise SystemExit(f"{left} transactions still reference legacy categories, aborting")
            await q(s, "DELETE FROM category WHERE id = ANY(:c)", c=list(legacy_ids))
            for name, dest in LEGACY.items():
                await q(
                    s,
                    "INSERT INTO category_alias (alias, category_id) VALUES (:a, :c) "
                    "ON CONFLICT (alias) DO UPDATE SET category_id = EXCLUDED.category_id",
                    a=name.lower(),
                    c=cats[dest],
                )
            log.append(f"deleted categories {sorted(legacy_ids.values())}; aliases -> {LEGACY}")

            log.append(f"transfer matching: {await match_transfers(s)}")
            await s.commit()

        if apply:
            await txn.commit()
        else:
            await txn.rollback()
    await dispose_engine()
    print(("APPLIED" if apply else "DRY RUN") + "\n" + "\n".join(log))


if __name__ == "__main__":
    asyncio.run(run("--apply" in sys.argv))

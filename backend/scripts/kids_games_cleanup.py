"""Follow-up cleanup (2026-10-05): Kids Activities split, and Video Games fixes.

- Kids Activities keeps classes/camps/scouts; kids' sports and parks/rec centers -> Sports & Recreation; play places
  and outings -> Events & Attractions; school lunches/photos -> Education; plus a few one-off merchants.
- Azure bills ("MICROSOFT#G..." / "Microsoft-G...") were keyed to the Minecraft Realms merchant and filed as Video
  Games: they get their own merchant (Microsoft Azure) and Synoptic Expense, with a regex rule ahead of the merchant
  rule. Windows/billing one-offs -> Software & Apps; uncategorized Steam rows -> Video Games.

Dry run by default; --apply commits. Run: scripts/with_env.ps1 .env.azure python scripts/kids_games_cleanup.py
"""

import asyncio
import json
import sys
from collections import Counter, defaultdict
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

from sqlalchemy.ext.asyncio import AsyncSession
from taxonomy_cleanup import ROOT, classify, generic, q, rows_of

from ledger.db.engine import dispose_engine, get_engine

NOTE = "kids/games cleanup 2026-10-05"
KIDS = [
    (r"burke stati|citizens assoc", "HOA Dues"),
    (r"myschoolbucks|strawbrid|starbridge|preschool smiles|lifetouch|yearbook|school sports pics", "Education"),
    (r"great wolf|water country|arcade mania", "Travel & Vacation"),
    (r"nintendo", "Video Games"),
    (r"amazon kids", "Software & Apps"),
    (r"scholastic", "Books & Audiobooks"),
    (r"highlights|hcs x6433", "News & Magazines"),
    (r"mybooster|booster club", "Gifts & Donations"),
    (r"woodcraft", "Home Improvement"),
    (r"(^|\|)usps(\||$)", "Shipping"),
    (r"swim|wrestling|\bbwc\b|\bbks\b|bryc|road yout|little league|ice arena|trek |braddock lakers", "Sports & Recreation"),
    (
        r"burke lake|clemyjontri|fryingpan|frying pan|nova parks|northern virginia reg prk|prince william for|"
        r"recreation\.?gov|south run|lee distt|mt vernon rc|audreymoore|woodley|recreation as",
        "Sports & Recreation",
    ),
    (
        r"chuck e|luv 2 play|billy beez|sky zone|lego discovery|scramble|let'?s play|play indoor|child science|"
        r"children'?s science|\bzoo\b|zoofari|capital wheel|disney on ice|dave & buster|bull run fest|festival|"
        r"dreamland|country farms|conservanc",
        "Events & Attractions",
    ),
]
AZURE = r"microsoft ?[#*-] ?g[0-9x]"
AZURE_RULE = r"MICROSOFT ?[#*-] ?G[0-9]"
AZURE_KEY, AZURE_NAME = "microsoft azure", "Microsoft Azure"
GAMES = [
    (AZURE, "Synoptic Expense"),
    (r"msft (windows|billing)", "Software & Apps"),
    (r"fireworks", "Shopping"),
]
SOURCES = {"Kids Activities": (KIDS, "Kids Activities"), "Video Games": (GAMES, "Video Games")}


async def run(apply: bool) -> None:
    now = datetime.now(UTC)
    log: list[str] = []
    async with get_engine().connect() as conn:
        txn = await conn.begin()
        async with AsyncSession(bind=conn, join_transaction_mode="create_savepoint") as s:
            cats = {r.name: r.id for r in await q(s, "SELECT id, name FROM category")}
            dests = {d for rules, default in SOURCES.values() for d in [default, *(d for _, d in rules)]}
            if missing := sorted(dests - set(cats)):
                raise SystemExit(f"Missing categories {missing}; aborting")
            source_ids = {cats[n]: n for n in SOURCES}

            rows = (
                await q(
                    s,
                    """--sql
                    SELECT t.id, t.category_id, t.merchant, mp.display_name AS display, t.description, t.notes,
                           t.amount, t.txn_date, NULL AS account_type
                    FROM "transaction" t LEFT JOIN merchant_profile mp ON mp.key = t.merchant
                    WHERE t.category_id = ANY(:c)
                    """,
                    c=list(source_ids),
                )
            ).all()
            steam = (
                (
                    await q(
                        s,
                        'SELECT id FROM "transaction" WHERE category_id IS NULL AND deleted_at IS NULL '
                        "AND merchant = 'steam games'",
                    )
                )
                .scalars()
                .all()
            )
            if apply:
                backup = {
                    "transaction": await rows_of(
                        s,
                        'SELECT id, category_id, merchant, merchant_source FROM "transaction" '
                        "WHERE category_id = ANY(:c) OR id = ANY(:ids)",
                        c=list(source_ids),
                        ids=list(steam),
                    ),
                    "category_rule": await rows_of(s, "SELECT * FROM category_rule"),
                    "merchant_profile": await rows_of(s, "SELECT * FROM merchant_profile WHERE key = :k", k=AZURE_KEY),
                }
                out = ROOT / "logs" / f"kids_games_cleanup_backup_{now:%Y%m%d%H%M%S}.json"
                out.write_text(json.dumps(backup, default=str), encoding="utf-8")
                log.append(f"backup: {out}")

            moves: dict[int, list[int]] = defaultdict(list)
            stats: dict[tuple[str, str], list] = defaultdict(lambda: [0, Decimal(0), Counter()])
            key_dest: dict[str, Counter] = defaultdict(Counter)
            azure_ids = []
            for r in rows:
                src = source_ids[r.category_id]
                rules, default = SOURCES[src]
                dest = classify(r, rules, default)
                st = stats[(src, dest)]
                st[0] += 1
                st[1] += r.amount
                st[2][r.display or r.merchant or r.description[:30]] += 1
                if cats[dest] != r.category_id:
                    moves[cats[dest]].append(r.id)
                    if src == "Kids Activities":
                        key_dest[r.merchant][dest] += 1
                    elif classify(r, [(AZURE, "azure")], "") == "azure":
                        azure_ids.append(r.id)
            for (src, dest), (n, total, top) in sorted(stats.items()):
                shown = ", ".join(f"{m} {c}" for m, c in top.most_common(25))
                log.append(f"{src} -> {dest}: {n} rows, {total:,.2f}  [{shown}]")
            for cid, ids in moves.items():
                await q(s, 'UPDATE "transaction" SET category_id = :c WHERE id = ANY(:ids)', c=cid, ids=ids)
            log.append(f"transactions moved: {sum(len(v) for v in moves.values())}")

            # Azure gets its own merchant identity so it no longer rides on the Minecraft Realms key.
            await q(
                s,
                "INSERT INTO merchant_profile (key, display_name) VALUES (:k, :n) "
                "ON CONFLICT (key) DO UPDATE SET display_name = EXCLUDED.display_name, alias_of = NULL",
                k=AZURE_KEY,
                n=AZURE_NAME,
            )
            await q(
                s,
                "UPDATE \"transaction\" SET merchant = :k, merchant_source = 'user' WHERE id = ANY(:ids)",
                k=AZURE_KEY,
                ids=azure_ids,
            )
            log.append(f"Azure rows re-keyed to {AZURE_KEY!r}: {len(azure_ids)}")
            await q(
                s,
                'UPDATE "transaction" SET category_id = :c WHERE id = ANY(:ids)',
                c=cats["Video Games"],
                ids=list(steam),
            )
            log.append(f"uncategorized Steam rows -> Video Games: {len(steam)}")

            # Rules: repoint Kids Activities rules, add an Azure regex ahead of the Realms merchant rule, add kid rules.
            rules = (await q(s, "SELECT id, match_type, pattern, category_id, source FROM category_rule")).all()
            displays = dict((await q(s, "SELECT key, display_name FROM merchant_profile")).all())
            existing = {r.pattern for r in rules}
            for r in rules:
                if r.category_id != cats["Kids Activities"]:
                    continue
                probe = SimpleNamespace(
                    display=displays.get(r.pattern),
                    merchant=r.pattern,
                    description=r.pattern,
                    notes=None,
                    amount=Decimal(0),
                    txn_date=None,
                    account_type=None,
                )
                dest = classify(probe, KIDS, "Kids Activities")
                if dest != "Kids Activities":
                    await q(s, "UPDATE category_rule SET category_id = :c WHERE id = :i", c=cats[dest], i=r.id)
                    log.append(f"rule {r.id} {r.match_type}:{r.pattern!r} ({r.source}) Kids Activities -> {dest}")
            if AZURE_RULE not in existing:
                await q(
                    s,
                    "INSERT INTO category_rule (match_type, pattern, category_id, priority, source, note) "
                    "VALUES ('regex', :p, :c, 50, 'user', :n)",
                    p=AZURE_RULE,
                    c=cats["Synoptic Expense"],
                    n=f"{NOTE}: Azure bills share the Minecraft Realms merchant key",
                )
                log.append(f"rule added: regex {AZURE_RULE!r} -> Synoptic Expense (priority 50)")
            added = []
            for key, dests in sorted(key_dest.items(), key=lambda kv: kv[0] or ""):
                if generic(key) or key in existing or len(dests) != 1 or sum(dests.values()) < 2:
                    continue
                dest = next(iter(dests))
                await q(
                    s,
                    "INSERT INTO category_rule (match_type, pattern, category_id, source, note) "
                    "VALUES ('merchant', :p, :c, 'user', :n)",
                    p=key,
                    c=cats[dest],
                    n=NOTE,
                )
                added.append(f"{key} -> {dest}")
            log.append(f"rules added ({len(added)}):" + "".join(f"\n    {a}" for a in added))
            await s.commit()

        if apply:
            await txn.commit()
        else:
            await txn.rollback()
    await dispose_engine()
    print(("APPLIED" if apply else "DRY RUN") + "\n" + "\n".join(log))


if __name__ == "__main__":
    asyncio.run(run("--apply" in sys.argv))

"""Taxonomy follow-up (2026-10-05): utilities, the Hide group, the Shopping catch-all, and regrouping.

- Cable / Phone / Internet group retired: Cable/Internet -> "TV / Internet" and Mobile Phone, both under Utilities.
  Merchant "verizon" (Fios) -> TV / Internet, "verizon wireless" -> Mobile Phone (incl. uncategorized rows).
  Television retired: live TV (Cox, YouTube TV, cable) -> TV / Internet, streaming -> Movies & TV.
  The group's budget is split into Mobile Phone / TV / Internet budgets by trailing-12-month spend.
- Hide group retired: Investments (TSP/529/brokerage legs) folds into Transfer.
- Shopping category retired: rows routed to specific categories (new "Crafts & Party Supplies"), rest -> Retail.
- Entertainment group's categories and Kids Activities move into the Shopping group; Entertainment is deleted.

Dry run by default; --apply commits.
Run: scripts/with_env.ps1 .env.azure python scripts/utilities_shopping_cleanup.py [--apply]
"""

import asyncio
import json
import sys
from collections import Counter, defaultdict
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

from sqlalchemy.ext.asyncio import AsyncSession
from taxonomy_cleanup import ROOT, classify, generic, q, rows_of

from ledger.analytics.transfers import match_transfers
from ledger.db.engine import dispose_engine, get_engine

NOTE = "utilities/shopping cleanup 2026-10-05"
RENAMES = {"Cable/Internet": "TV / Internet"}
NEW_CATEGORIES = {"Crafts & Party Supplies": "Shopping"}
# category -> new group
MOVES = {
    "TV / Internet": "Utilities",
    "Mobile Phone": "Utilities",
    "Kids Activities": "Shopping",
    "Books & Audiobooks": "Shopping",
    "News & Magazines": "Shopping",
    "Music & Audio": "Shopping",
    "Movies & TV": "Shopping",
    "Video Games": "Shopping",
    "Software & Apps": "Shopping",
    "Events & Attractions": "Shopping",
    "Sports & Recreation": "Shopping",
}
# retired category -> where leftover references and import labels go
DELETED = {"Television": "Movies & TV", "Shopping": "Retail", "Investments": "Transfer"}
DELETE_GROUPS = ["Cable / Phone / Internet", "Hide", "Entertainment"]
SPLIT_BUDGET = ("Cable / Phone / Internet", ["Mobile Phone", "TV / Internet"])


def fios(r):
    return r.merchant == "verizon"


def station(r):
    hay = f"{r.display}|{r.merchant}|{r.description}".lower()
    return any(s in hay for s in ("exxon", "kwik fill", "shell oil", "sunoco"))


CABLE = [
    (r"mediamall", "Software & Apps"),
    (r"(^|\|)metro(\||$)", "Public Transportation"),
    (r"gogo", "Travel & Vacation"),
]
MOBILE = [(fios, "TV / Internet"), (r"adelphia", "TV / Internet"), (r"zoom", "Software & Apps")]
TELEVISION = [
    (r"cox comm|cable|satellite|youtube ?tv", "TV / Internet"),
    (r"google voice|voice g", "Mobile Phone"),
    (r"(^|\|)king(\||$)", "Video Games"),
    (r"zogox", "Software & Apps"),
    (r"vooks|read alo", "Books & Audiobooks"),
    (r"amazon prime\*|(^|\|)amazon(\||$)", "Retail"),
]
KIDS = [(r"journeys kidz", "Clothing")]
SHOPPING = [
    (r"commercial ?payment ?withdrawal|commercial card pmt|creditcardwithdrawal", "Credit Card Payment"),
    (
        r"michaels|(^|\|)a\.?c\.? moore|ac moore|jo-?ann|cricut|wholesale arts|craftybundl|party (depot|city)|"
        r"christmas attic|fireworks",
        "Crafts & Party Supplies",
    ),
    (r"spencer", "Retail"),
    (
        r"hallmark|papyrus|floral|gift|spoonful of comfort|norooz|curio|chaney chicks|purple papaya|crochet",
        "Gifts & Donations",
    ),
    (r"paradies|hudson news|newslink|(^|\|)relay(\||$)|(^|\|)airport(\||$)|sundries|super lama", "Travel & Vacation"),
    (r"jose cuer|moose.?s? tooth|hard rock|hrc orlando|(^|\|)subway(\||$)", "Restaurants & Fast Food"),
    (r"godiva|candies|candymaker|cndy|peanut man", "Ice Cream & Desserts"),
    (r"restaurant depot|amazon prime now|(^|\|)market(\||$)", "Groceries"),
    (r"montgomery county dlc", "Alcohol & Bars"),
    (lambda r: station(r) and r.amount <= -25, "Gas & Fuel"),
    (lambda r: station(r), "Convenience Stores"),
    (r"vending|travel plaza|trvl", "Convenience Stores"),
    (r"eckerd|otc brands|legendairy|pharmacy", "Pharmacy"),
    (r"saratoga spa|smithsonian|freer sackler|foxwoods|athletic bo|athletiburke", "Events & Attractions"),
    (r"bath & body|(^|\|)lush|mac cosmetics|ellamila|dame products|spa ?finder", "Personal Care"),
    (r"marine|live rock|super pets|catladybox|mattypup|aquarium", "Pet Food & Supplies"),
    (r"helix|west elm|hayneedle|wicked cushions|overstock|behnam art", "Furnishings"),
    (r"bulbs com|tooltopia|acme ?tools|tool nut|greenworks|grill ?parts|kidco|backyard bird|hamama", "Home Improvement"),
    (r"advance (auto|stores)|weathertech", "Service & Parts"),
    (r"keyme", "Home Repairs & Maintenance"),
    (
        r"francesca|nordstrom|jcpenney|burlington|t shirt factor|parks ?project|beautiful bags|(^|\|)chaps|"
        r"sabres store",
        "Clothing",
    ),
    (r"comic|books|sounds true", "Books & Audiobooks"),
    (r"louis ?c\.?k|prime video|(^|\|)nhl", "Movies & TV"),
    (r"digital goods.? games", "Video Games"),
    (
        r"usenet|chompsms|driver cleaner|unifiedintents|haunted studios|digital goods|(^|\|)honey(\||$)|"
        r"(^|\|)google(\||$)",
        "Software & Apps",
    ),
    (r"kiddie mall|mightyme", "Kids Activities"),
    (r"lifetouch", "Education"),
    (r"wspsychr", "Private Practice Expenses"),
    (r"cell doc", "Mobile Phone"),
    (r"(^|\|)sports(\||$)", "Sports & Recreation"),
]
# category -> (rules, default destination); first matching rule wins
SOURCES = {
    "Cable/Internet": (CABLE, "TV / Internet"),
    "Mobile Phone": (MOBILE, "Mobile Phone"),
    "Television": (TELEVISION, "Movies & TV"),
    "Kids Activities": (KIDS, "Kids Activities"),
    "Shopping": (SHOPPING, "Retail"),
    "Investments": ([], "Transfer"),
}
# new merchant/contains rules: (match_type, pattern, category, priority)
NEW_RULES = [
    ("merchant", "verizon", "TV / Internet", 100),
    ("merchant", "verizon wireless", "Mobile Phone", 100),
    ("contains", "youtube tv", "TV / Internet", 50),
]


async def run(apply: bool) -> None:
    now = datetime.now(UTC)
    log: list[str] = []
    async with get_engine().connect() as conn:
        txn = await conn.begin()
        async with AsyncSession(bind=conn, join_transaction_mode="create_savepoint") as s:
            groups = {r.name: r.id for r in await q(s, "SELECT id, name FROM category_group")}
            cats = {r.name: r.id for r in await q(s, "SELECT id, name FROM category")}
            final_names = {RENAMES.get(n, n) for n in cats} | set(NEW_CATEGORIES)
            dests = {d for rules, default in SOURCES.values() for d in [default, *(d for _, d in rules)]}
            dests |= {*DELETED.values(), *MOVES, *(d for _, _, d, _ in NEW_RULES), *SPLIT_BUDGET[1]}
            missing = sorted(n for n in {*RENAMES, *DELETED, *SOURCES} if n not in cats)
            missing += sorted(dests - final_names)
            missing += sorted(g for g in {*DELETE_GROUPS, *MOVES.values(), *NEW_CATEGORIES.values()} if g not in groups)
            clash = sorted(n for n in [*NEW_CATEGORIES, *RENAMES.values()] if n in cats)
            if missing or clash:
                raise SystemExit(f"Taxonomy changed since the review (missing {missing}, already exist {clash}); aborting")
            source_ids = {cats[n]: n for n in SOURCES}
            deleted_ids = {cats[n]: n for n in DELETED}
            verizon_uncat = (
                (
                    await q(
                        s,
                        'SELECT id FROM "transaction" WHERE category_id IS NULL AND deleted_at IS NULL '
                        "AND merchant = 'verizon wireless'",
                    )
                )
                .scalars()
                .all()
            )

            if apply:
                backup = {
                    "category_group": await rows_of(s, "SELECT * FROM category_group"),
                    "category": await rows_of(s, "SELECT * FROM category"),
                    "category_rule": await rows_of(s, "SELECT * FROM category_rule"),
                    "category_alias": await rows_of(s, "SELECT * FROM category_alias"),
                    "budget": await rows_of(s, "SELECT * FROM budget"),
                    "import_row": await rows_of(
                        s, "SELECT id, category_id FROM import_row WHERE category_id = ANY(:c)", c=list(deleted_ids)
                    ),
                    "anomaly": await rows_of(
                        s, "SELECT id, category_id FROM anomaly WHERE category_id = ANY(:c)", c=list(deleted_ids)
                    ),
                    "transaction": await rows_of(
                        s,
                        "SELECT id, category_id, suggested_category_id, suggestion_confidence, suggestion_reason "
                        'FROM "transaction" WHERE category_id = ANY(:c) OR suggested_category_id = ANY(:d) '
                        "OR id = ANY(:ids)",
                        c=list(source_ids),
                        d=list(deleted_ids),
                        ids=list(verizon_uncat),
                    ),
                }
                out = ROOT / "logs" / f"utilities_shopping_backup_{now:%Y%m%d%H%M%S}.json"
                out.write_text(json.dumps(backup, default=str), encoding="utf-8")
                log.append(f"backup: {out}")

            # 1. Categories: rename, create, regroup.
            for old, new in RENAMES.items():
                await q(s, "UPDATE category SET name = :n WHERE id = :i", n=new, i=cats[old])
            final = {RENAMES.get(n, n): i for n, i in cats.items()}
            for name, grp in NEW_CATEGORIES.items():
                final[name] = (
                    await q(
                        s,
                        "INSERT INTO category (group_id, name, type) VALUES (:g, :n, 'expense') RETURNING id",
                        g=groups[grp],
                        n=name,
                    )
                ).scalar_one()
            for name, grp in MOVES.items():
                await q(s, "UPDATE category SET group_id = :g WHERE id = :i", g=groups[grp], i=final[name])
            log.append(f"renamed {RENAMES}; created {NEW_CATEGORIES}")
            log.append(f"regrouped: {', '.join(f'{n} -> {g}' for n, g in MOVES.items())}")

            # 2. Transactions of the source categories (soft-deleted too, so retired categories end up empty).
            txns = (
                await q(
                    s,
                    """--sql
                    SELECT t.id, t.category_id, t.merchant, mp.display_name AS display, t.description, t.notes,
                           t.amount, t.txn_date, t.deleted_at, NULL AS account_type
                    FROM "transaction" t LEFT JOIN merchant_profile mp ON mp.key = t.merchant
                    WHERE t.category_id = ANY(:c)
                    """,
                    c=list(source_ids),
                )
            ).all()
            moves: dict[int, list[int]] = defaultdict(list)
            stats: dict[tuple[str, str], list] = defaultdict(lambda: [0, Decimal(0), Counter()])
            key_dest: dict[str, Counter] = defaultdict(Counter)
            unsure = []
            for r in txns:
                src = source_ids[r.category_id]
                rules, default = SOURCES[src]
                dest = classify(r, rules, default)
                st = stats[(src, dest)]
                st[0] += 1
                st[1] += r.amount
                st[2][r.display or r.merchant or r.description[:30]] += 1
                if final[dest] != r.category_id:
                    moves[final[dest]].append(r.id)
                    if dest != default:
                        key_dest[r.merchant][dest] += 1
                if src == "Shopping" and dest == default and abs(r.amount) >= 200 and r.deleted_at is None:
                    unsure.append(f"    #{r.id} {r.txn_date} {r.amount:>10} {r.description[:60]} | {r.notes or ''}")
            for (src, dest), (n, total, top) in sorted(stats.items()):
                shown = ", ".join(f"{m} {c}" for m, c in top.most_common(40 if src == "Shopping" else 12))
                log.append(f"{src} -> {dest}: {n} rows, {total:,.2f}  [{shown}]")
            log.append("Shopping -> Retail rows >= $200 (check):\n" + "\n".join(unsure))
            for cid, ids in moves.items():
                await q(
                    s,
                    'UPDATE "transaction" SET category_id = :c, suggested_category_id = NULL, '
                    "suggestion_confidence = NULL, suggestion_reason = NULL WHERE id = ANY(:ids)",
                    c=cid,
                    ids=ids,
                )
            await q(
                s,
                'UPDATE "transaction" SET category_id = :c WHERE id = ANY(:ids)',
                c=final["Mobile Phone"],
                ids=list(verizon_uncat),
            )
            log.append(f"transactions moved: {sum(len(v) for v in moves.values())}")
            log.append(f"uncategorized Verizon Wireless rows -> Mobile Phone: {len(verizon_uncat)}")

            # 3. Rules: repoint rules on source categories, add Verizon/YouTube TV rules and routed-merchant rules.
            displays = dict((await q(s, "SELECT key, display_name FROM merchant_profile")).all())
            rules = (await q(s, "SELECT id, match_type, pattern, category_id, source FROM category_rule")).all()
            existing = {(r.match_type, r.pattern.lower()) for r in rules}
            for r in rules:
                if r.category_id not in source_ids:
                    continue
                src = source_ids[r.category_id]
                probe = SimpleNamespace(
                    display=displays.get(r.pattern),
                    merchant=r.pattern,
                    description=r.pattern,
                    notes=None,
                    amount=Decimal(0),
                    txn_date=None,
                    account_type=None,
                )
                dest = classify(probe, *SOURCES[src])
                if final[dest] != r.category_id:
                    await q(s, "UPDATE category_rule SET category_id = :c WHERE id = :i", c=final[dest], i=r.id)
                    log.append(f"rule {r.id} {r.match_type}:{r.pattern!r} ({r.source}) {src} -> {dest}")
            added = []
            for match_type, pattern, dest, priority in NEW_RULES:
                if (match_type, pattern) in existing:
                    log.append(f"rule {match_type}:{pattern!r} already exists; left as is")
                    continue
                await q(
                    s,
                    "INSERT INTO category_rule (match_type, pattern, category_id, priority, source, note) "
                    "VALUES (:m, :p, :c, :o, 'user', :n)",
                    m=match_type,
                    p=pattern,
                    c=final[dest],
                    o=priority,
                    n=NOTE,
                )
                existing.add((match_type, pattern))
                added.append(f"{match_type}:{pattern} -> {dest} (priority {priority})")
            for key, ds in sorted(key_dest.items(), key=lambda kv: kv[0] or ""):
                if generic(key) or ("merchant", key) in existing or len(ds) != 1 or sum(ds.values()) < 3:
                    continue
                dest = next(iter(ds))
                await q(
                    s,
                    "INSERT INTO category_rule (match_type, pattern, category_id, source, note) "
                    "VALUES ('merchant', :p, :c, 'user', :n)",
                    p=key,
                    c=final[dest],
                    n=NOTE,
                )
                added.append(f"merchant:{key} -> {dest}")
            log.append(f"rules added ({len(added)}):" + "".join(f"\n    {a}" for a in added))

            # 4. Budget: the retired group's budget becomes per-category budgets split by recent spend.
            grp_name, split_cats = SPLIT_BUDGET
            budget = (
                await q(s, "SELECT id, amount, period_type FROM budget WHERE group_id = :g", g=groups[grp_name])
            ).first()
            if budget:
                since = date.today() - timedelta(days=365)
                spend = dict(
                    (
                        await q(
                            s,
                            'SELECT category_id, -sum(amount) FROM "transaction" WHERE deleted_at IS NULL '
                            "AND category_id = ANY(:c) AND txn_date >= :d GROUP BY 1",
                            c=[final[n] for n in split_cats],
                            d=since,
                        )
                    ).all()
                )
                total = sum(spend.values()) or Decimal(1)
                parts = {
                    n: (budget.amount * spend.get(final[n], 0) / total / 5).quantize(Decimal(1)) * 5 for n in split_cats
                }
                parts[split_cats[0]] += budget.amount - sum(parts.values())
                await q(s, "DELETE FROM budget WHERE id = :i", i=budget.id)
                for name, amount in parts.items():
                    await q(
                        s,
                        "INSERT INTO budget (category_id, period_type, amount, notes) VALUES (:c, :p, :a, :n)",
                        c=final[name],
                        p=budget.period_type,
                        a=amount,
                        n=f"Split from the {grp_name} budget ({budget.amount}) by 12-month spend; {NOTE}",
                    )
                log.append(
                    f"budget {budget.id} ({grp_name} {budget.amount}/{budget.period_type}) -> "
                    + ", ".join(f"{n} {a} (12-mo spend {spend.get(final[n], 0)})" for n, a in parts.items())
                )

            # 5. Other references, aliases, then drop retired categories and groups.
            for table in ("import_row", "anomaly", "statement_series", "spread_rule", "budget"):
                for cid, name in deleted_ids.items():
                    res = await q(
                        s, f"UPDATE {table} SET category_id = :d WHERE category_id = :c", d=final[DELETED[name]], c=cid
                    )
                    if res.rowcount:
                        log.append(f"{table}: {name} -> {DELETED[name]}: {res.rowcount}")
            for cid, name in deleted_ids.items():
                await q(
                    s, "UPDATE category_alias SET category_id = :d WHERE category_id = :c", d=final[DELETED[name]], c=cid
                )
            aliases = {name.lower(): dest for name, dest in DELETED.items()}
            aliases |= {old.lower(): new for old, new in RENAMES.items()}
            for alias, dest in aliases.items():
                await q(
                    s,
                    "INSERT INTO category_alias (alias, category_id) VALUES (:a, :c) "
                    "ON CONFLICT (alias) DO UPDATE SET category_id = EXCLUDED.category_id",
                    a=alias,
                    c=final[dest],
                )
            log.append(f"aliases: {aliases}")
            left = (
                await q(s, 'SELECT count(*) FROM "transaction" WHERE category_id = ANY(:c)', c=list(deleted_ids))
            ).scalar_one()
            if left:
                raise SystemExit(f"{left} transactions still reference retired categories; aborting")
            await q(s, "DELETE FROM category WHERE id = ANY(:c)", c=list(deleted_ids))
            left = (
                await q(
                    s,
                    "SELECT string_agg(name, ', ') FROM category WHERE group_id = ANY(:g)",
                    g=[groups[g] for g in DELETE_GROUPS],
                )
            ).scalar_one()
            if left:
                raise SystemExit(f"Categories still in retired groups: {left}; aborting")
            await q(s, "DELETE FROM category_group WHERE id = ANY(:g)", g=[groups[g] for g in DELETE_GROUPS])
            log.append(f"deleted categories {sorted(deleted_ids.values())}; groups {DELETE_GROUPS}")

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

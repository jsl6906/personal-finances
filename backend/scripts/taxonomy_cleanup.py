"""Taxonomy cleanup (reviewed 2026-10-05): split, merge and regroup categories.

- Food: Coffee / Bakery / Ice Cream -> Coffee Shops + Bakeries + Ice Cream & Desserts; Fast Food and Other Food &
  Dining fold into Restaurants (convenience stores, groceries, desserts, etc. routed by merchant).
- Entertainment group replaces Books, Amusement, & Entertainment (books, news, music, movies, games, software,
  events, sports). Home Services group (housecleaning, pest, lawn, security, moving, repairs) + Utilities (trash).
- Loans group (Loan Payment, Student Loan); loan/mortgage/retirement-account legs -> Transfer. Taxes and Fees group
  (Fees & Charges, Government Fees & Fines). Work Income group. Gifts & Donations absorbs Charity.
- Income rows that are merchant/tax refunds move to the merchant's usual expense category.

Rules are repointed (and added for merchants routed to new categories), retired names become category aliases so
imports keep resolving, budgets/spread rules/series follow their category. Dry run by default; --apply commits.
Run: scripts/with_env.ps1 .env.azure python scripts/taxonomy_cleanup.py [--apply]
"""

import asyncio
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ledger.analytics.transfers import match_transfers
from ledger.db.engine import dispose_engine, get_engine

ROOT = Path(__file__).resolve().parents[2]
NOTE = "taxonomy cleanup 2026-10-05"

GROUP_RENAMES = {
    "Other Loan Payment": "Loans",
    "Taxes": "Taxes and Fees",
    "Utilities & Home Services": "Utilities",
    "Paycheck": "Work Income",
}
# new group -> existing (final-named) group it sorts with
NEW_GROUPS = {"Home Services": "Utilities", "Entertainment": "Shopping"}
DELETE_GROUPS = ["Student Loan", "Private Practice Income", "Data Viz Income", "Home Care Income"]

# original name -> (new name, new group or None)
RENAMES = {
    "Coffee / Bakery / Ice Cream": ("Coffee Shops", None),
    "Service Fee": ("Fees & Charges", "Taxes and Fees"),
    "Laundry": ("Laundry & Dry Cleaning", None),
    "Parking": ("Parking & Tolls", None),
    "Vacation": ("Travel & Vacation", None),
    "Home Services": ("Home Repairs & Maintenance", "Home Services"),
    "Gifts & Donations": ("Gifts & Donations", "Other Expenses"),
    "Student Loan": ("Student Loan", "Loans"),
    "Paycheck Income": ("Federal Salary", None),
    "PRIV PRACTICE INCOME": ("Mona's Private Practice", "Work Income"),
    "SYNOPTIC INCOME": ("Data Viz Income", "Work Income"),
    "Home Care Paycheck": ("Home Healthcare Paycheck", "Work Income"),
}
NEW_CATEGORIES = {
    "Bakeries": "Food & Dining",
    "Ice Cream & Desserts": "Food & Dining",
    "Government Fees & Fines": "Taxes and Fees",
    "Vehicle Purchase": "Transportation & Travel",
    "Trash & Recycling": "Utilities",
    "Housecleaning": "Home Services",
    "Pest Control": "Home Services",
    "Lawn & Irrigation": "Home Services",
    "Home Security": "Home Services",
    "Moving & Storage": "Home Services",
    "Home Improvement": "Shopping",
    "Books & Audiobooks": "Entertainment",
    "News & Magazines": "Entertainment",
    "Music & Audio": "Entertainment",
    "Movies & TV": "Entertainment",
    "Video Games": "Entertainment",
    "Software & Apps": "Entertainment",
    "Events & Attractions": "Entertainment",
    "Sports & Recreation": "Entertainment",
    "Cash & ATM": "Other Expenses",
    "HOA Dues": "Mortgage & Rent",
}
# retired category -> where leftover references and import labels go
DELETED = {
    "Fast Food": "Restaurants",
    "Other Food & Dining": "Restaurants",
    "Books, Amusement, & Entertainment": "Events & Attractions",
    "Charity": "Gifts & Donations",
    "Finance Charge": "Fees & Charges",
    "Auto & Transport": "Service & Parts",
    "Auto Payment": "Loan Payment",
    "Home Center / Tools / Garden": "Home Improvement",
    "Cleaning Service": "Laundry & Dry Cleaning",
    "Travel": "Travel & Vacation",
    "Buy": "Investments",
    "Advertising": "Business Services",
    "Professional Develop": "Business Services",
    "Misc Kids": "Kids Activities",
    "Loans": "Loan Payment",
    "Loan Transaction": "Loan Payment",
}
ALIAS_OVERRIDES = {"games movies & software": "Video Games"}


def on_account(*types):
    return lambda r: r.account_type in types


def cleaning_check(r):
    return r.merchant == "check" and r.txn_date is not None and r.txn_date.year == 2021 and 150 <= -r.amount <= 200


COFFEE_CHAINS = r"starbucks|dunkin|tim horton|peet'?s|caribou|misha"
DESSERT = (
    r"ice cream|icecream|creamery|coldstone|cold stone|gelato|dolcezza|frozen yogurt|froyo|sweet ?frog|baskin|"
    r"dairy queen|haagen|häagen|dippin|ben & jerry|pleasant pops|popsy pop|moo thru|sweet berry|skycream|sweet spot|"
    r"laderach|läderach|candy|pitango|\brita'?s\b|kilwins"
)
BAKERY = (
    r"bakery|bakeshop|bagel|doughnut|donut|krispy kreme|tous les jours|parsa|einstein|firehook|buzz b\b|auntie anne|"
    r"castro'?s|bullfro|patisserie|pastry|cinnabon"
)
FOOD = [
    (COFFEE_CHAINS, "Coffee Shops"),
    (r"astro doughnuts", "Restaurants"),
    (DESSERT, "Ice Cream & Desserts"),
    (BAKERY, "Bakeries"),
    (r"coffee|espresso", "Coffee Shops"),
    (r"sweetgreen|tropical smoothie|dolce vita|ciccheti", "Restaurants"),
    (r"7[- ]?eleven|sheetz|food mart|convenience", "Convenience Stores"),
    (r"lego discovery", "Events & Attractions"),
    (r"mason u ffax books", "Books & Audiobooks"),
]
OTHER_FOOD = [
    (r"ikea", "Furnishings"),
    (r"nuts\.?com|carnivore club|snacksack|nespresso|butcher|seafood market|teavana|universal yums|beef jerky", "Groceries"),
    (r"vineyard|winery|abc store", "Alcohol & Bars"),
    (r"drafthouse|cinema", "Movies & TV"),
]
ENT = [
    (r"dc ticket", "Government Fees & Fines"),
    (r"burke stati|citizens assoc", "HOA Dues"),
    (r"reston shirt", "Clothing"),
    (r"universal yums", "Groceries"),
    (r"party city|cricut", "Shopping"),
    (r"angie'?s", "Home Repairs & Maintenance"),
    (
        r"consumer reports|consumers.? checkbook|nytimes|new york times|new ?yorker|(^|\|)cnp|cond[eé] nast|bon ?app|"
        r"washington post|wapo|highlights|hcs x6433|magazine|mdc |taste of home|rda |better ?homes|this old house|"
        r"the onion|buffalo news|hockey news|hearst|direct marketing|hudson news|main street news|smithsonian'?s nation",
        "News & Magazines",
    ),
    (
        r"audible|kindle|barnes|borders|dalton|books?\b|bookseller|powell|comic|library|fcpl|t f books|dba t f|ketabsara",
        "Books & Audiobooks",
    ),
    (r"spotify|third man|itunes|modlife|music|sirius|\bfye\b|kung fu|warner bros|nonesuc|patreon|tuneup", "Music & Audio"),
    (
        r"hulu|amazon video|youtube|moviepass|regal|\bamc\b|cinema|drafthouse|silver theatre|bethesda row|dipson|"
        r"joylan|e street|fandango|vudu|playon|film|flixtools|fs x x8914|louis c|\bnhl\b|netflix",
        "Movies & TV",
    ),
    (
        r"steam|nintendo|playstation|\bking\b|humble ?bundle|green ?man|gamestop|ncsoft|plaync|square enix|gameloft|"
        r"zeptolab|2d boy|numinous|steel crate|june.?s journey|clan ?servers|servercraft|realm|(^|\|)interactive(\||$)|"
        r"bubblesoft|(^|\|)sony(\||$)",
        "Video Games",
    ),
    (
        r"microsoft|apple|google|chatgpt|openai|\bcalm\b|walk at home|zoom|webroot|sublime|swiftkey|jthink|teslacoil|"
        r"shifty jelly|tolriq|doubletwist|jfdp|drilly|es global|magiscan|lose it|endomondo|avangate|tagrename|"
        r"moneybookers|mind your brain|avaiya|baby pics|zogox|inreach|better route|eharmony|swstudios|computergeeks",
        "Software & Apps",
    ),
    (
        r"dick'?s sporting|dick clothing|sports authority|performance bike|(^|\|)rei(\||$)|hudson trail|spokes|trek |"
        r"go outdoors|kettler|iceplex|golf links",
        "Sports & Recreation",
    ),
    (r"wal-?mart|wal mart|(^|\|)amazon(\||$)|newegg|radioshack", "Retail"),
]
GIFTS = [(r"wantable", "Clothing"), (r"golf", "Travel & Vacation"), (r"medical faculty", "Doctor / Specialist")]
AUTO = [
    (r"motor veh|\bdmv\b", "Government Fees & Fines"),
    (r"e-?z ?pass|beltway express|\btoll|laz parking|parking|oxon hill|va19", "Parking & Tolls"),
    (r"exxon|sunoco|shell oil|\bbp\b", "Gas & Fuel"),
    (r"alamo|rent a car|car rental", "Rental Car & Taxi"),
    (r"better route", "Software & Apps"),
    (r"keyme|locksmith|total security", "Home Repairs & Maintenance"),
    (r"cardtronics|\batm\b", "Cash & ATM"),
    (r"fighting chance|check 1090", "Vehicle Purchase"),
]
LOAN = [
    (on_account("loan", "mortgage", "retirement", "investment"), "Transfer"),
    (r"transfer to cen[cs]us fcu|units purchased", "Transfer"),
]
AUTO_PAYMENT = [
    (r"fed salary|federal payroll", "Federal Salary"),
    (r"down payment|check paid #1152", "Vehicle Purchase"),
    (r"echeck deposit", "Transfer"),  # TSP loan for the F150
    (r"hyundai", "Service & Parts"),
]
FEES = [
    (r"car loan interest", "Loan Payment"),
    (
        r"citation|court|public safety|dc ticket|city of alexandria|fairfax co|motor veh|\bdmv\b|passport|"
        r"birth certificate|change of address|official payments|\bopay\b",
        "Government Fees & Fines",
    ),
    (r"beltway express|e-?z ?pass", "Parking & Tolls"),
    (r"towing", "Service & Parts"),
]
HOME_CENTER = [
    (r"balance transfer|synchrony|card payment|auto pymt", "Credit Card Payment"),
    (r"nursery|garden cent|lawn ?care|rachio", "Lawn & Irrigation"),
    (r"global construction", "Home Repairs & Maintenance"),
]
HOME_SERVICES = [
    (r"pennymac|the point at dun", "Mortgage & Rent"),
    (r"(^|\|)ups(\||$)|ups store", "Shipping"),
    (r"virginia abc|alcoholic bvrg", "Alcohol & Bars"),
    (r"integrators", "Business Services"),
    (r"magic clean|zips|dry clean", "Laundry & Dry Cleaning"),
    (r"gutter", "Home Repairs & Maintenance"),
    (r"pest|rat ?cloud|terminix|orkin", "Pest Control"),
    (r"disposal|trash|waste|recycl", "Trash & Recycling"),
    (r"hydro-?tech|irrig|lawn|landscap|garden", "Lawn & Irrigation"),
    (r"brinks|livewatch|safemart|nest|(^|\|)google(\||$)|\badt\b|simplisafe", "Home Security"),
    (r"u-?haul|hireahelper|moving|storage|\bpods\b", "Moving & Storage"),
    (r"o & t multi|tatiana|cleaning|maid|zelle mona d", "Housecleaning"),
    (cleaning_check, "Housecleaning"),
]
SHOPPING = [
    (r"card payment|payment to card|balance transfer|promotional balance", "Credit Card Payment"),
    (r"lowe'?s|home depot|harbor freight|ace hardware", "Home Improvement"),
    (r"rite aid|\bcvs\b|walgreens", "Pharmacy"),
    (r"pottery ?barn|crate (&|and) barrel|wayfair|world market|cost plus|bed bath|homegoods|ikea", "Furnishings"),
    (r"kay jewel|vera bradley|macy'?s|j\.? ?crew|old navy", "Clothing"),
    (r"persian ?basket|beef jerky", "Groceries"),
    (r"third man", "Music & Audio"),
    (r"gamestop", "Video Games"),
    (r"dick'?s sporting|dick clothing|nhl shop|frg shop", "Sports & Recreation"),
    (r"pristine aquarium", "Pet Food & Supplies"),
    (r"safemart", "Home Security"),
    (
        r"wal-?mart|wal mart|sam'?s club|sams club|best buy|newegg|dollar (tree|general)|five below|tuesday morning|"
        r"\bsears\b|\bqvc\b|ebay|etsy|wholesale club|kohl|tj ?maxx|buy ?buy ?baby|\bkmart\b|big lots",
        "Retail",
    ),
]
# original category name -> (rules, default destination); first matching rule wins
SOURCES = {
    "Coffee / Bakery / Ice Cream": (FOOD, "Coffee Shops"),
    "Fast Food": (FOOD, "Restaurants"),
    "Restaurants": (FOOD, "Restaurants"),
    "Other Food & Dining": (OTHER_FOOD + FOOD, "Restaurants"),
    "Books, Amusement, & Entertainment": (ENT, "Events & Attractions"),
    "Gifts & Donations": (GIFTS, "Gifts & Donations"),
    "Charity": (GIFTS, "Gifts & Donations"),
    "Auto & Transport": (AUTO, "Service & Parts"),
    "Auto Payment": (LOAN + AUTO_PAYMENT, "Loan Payment"),
    "Loan Payment": (LOAN, "Loan Payment"),
    "Student Loan": (LOAN, "Student Loan"),
    "Service Fee": (FEES, "Fees & Charges"),
    "Finance Charge": (FEES, "Fees & Charges"),
    "Home Center / Tools / Garden": (HOME_CENTER, "Home Improvement"),
    "Home Services": (HOME_SERVICES, "Home Repairs & Maintenance"),
    "Cleaning Service": ([], "Laundry & Dry Cleaning"),
    "Travel": ([], "Travel & Vacation"),
    "Buy": ([], "Investments"),
    "Unclassified": ([(r"\batm\b|teller|cash withdrawal|customer withdrawal", "Cash & ATM")], "Unclassified"),
    "Shopping": (SHOPPING, "Shopping"),
    "Retail": ([(r"home depot|lowe'?s|harbor freight|ace hardware|menards", "Home Improvement")], "Retail"),
}
# Income refunds keep their category when the merchant's usual category is one of these (or the key is generic).
REFUND_SKIP = {"Unclassified", "Loan Payment", "Student Loan", "Cash & ATM", "Investments", "Buy"}
GENERIC_KEYS = {"check", "echeck", "echeck deposit", "venmo", "google", "amazon", "costco", "apple", "paypal", "square"}
GENERIC_PREFIX = ("zelle", "check ", "transfer", "payment", "deposit", "google")


def generic(key: str | None) -> bool:
    return not key or key in GENERIC_KEYS or key.startswith(GENERIC_PREFIX)


def classify(row, rules, default: str) -> str:
    hay = "|".join(str(x or "") for x in (row.display, row.merchant, row.description, row.notes)).lower()
    for match, dest in rules:
        if match(row) if callable(match) else re.search(match, hay):
            return dest
    return default


async def q(s: AsyncSession, sql: str, **params):
    return await s.execute(text(sql), params)


async def rows_of(s: AsyncSession, sql: str, **params) -> list[dict]:
    return [dict(r._mapping) for r in await q(s, sql, **params)]


async def run(apply: bool) -> None:
    now = datetime.now(UTC)
    log: list[str] = []
    async with get_engine().connect() as conn:
        txn = await conn.begin()
        async with AsyncSession(bind=conn, join_transaction_mode="create_savepoint") as s:
            groups = {r.name: r for r in await q(s, "SELECT id, name, sort_order FROM category_group")}
            cats = {r.name: r.id for r in await q(s, "SELECT id, name FROM category")}
            final_names = {RENAMES.get(n, (n, None))[0] for n in cats} | set(NEW_CATEGORIES)
            final_groups = {GROUP_RENAMES.get(n, n) for n in groups} | set(NEW_GROUPS)
            dests = {d for rules, default in SOURCES.values() for d in [default, *(d for _, d in rules)]}
            dests |= {*DELETED.values(), *ALIAS_OVERRIDES.values(), "Income"}
            ref_groups = {*(g for _, g in RENAMES.values() if g), *NEW_CATEGORIES.values(), *NEW_GROUPS.values()}
            missing = sorted(n for n in {*RENAMES, *DELETED, *SOURCES} if n not in cats)
            missing += sorted(dests - final_names) + sorted(g for g in [*GROUP_RENAMES, *DELETE_GROUPS] if g not in groups)
            missing += sorted(ref_groups - final_groups)
            clash = sorted(n for n in NEW_CATEGORIES if n in cats) + sorted(g for g in NEW_GROUPS if g in groups)
            if missing or clash:
                raise SystemExit(f"Taxonomy changed since the review (missing {missing}, already exist {clash}); aborting")
            source_ids = {cats[n]: n for n in SOURCES}
            deleted_ids = {cats[n]: n for n in DELETED}

            if apply:
                touched = list({*source_ids, *deleted_ids, cats["Income"]})
                backup = {
                    "category_group": await rows_of(s, "SELECT * FROM category_group"),
                    "category": await rows_of(s, "SELECT * FROM category"),
                    "category_rule": await rows_of(s, "SELECT * FROM category_rule"),
                    "category_alias": await rows_of(s, "SELECT * FROM category_alias"),
                    "budget": await rows_of(s, "SELECT * FROM budget"),
                    "spread_rule": await rows_of(s, "SELECT * FROM spread_rule"),
                    "statement_series": await rows_of(s, "SELECT id, category_id FROM statement_series"),
                    "import_row": await rows_of(
                        s, "SELECT id, category_id FROM import_row WHERE category_id = ANY(:c)", c=touched
                    ),
                    "anomaly": await rows_of(
                        s, "SELECT id, category_id FROM anomaly WHERE category_id = ANY(:c)", c=touched
                    ),
                    "transaction": await rows_of(
                        s,
                        "SELECT id, category_id, suggested_category_id, suggestion_confidence, suggestion_reason "
                        'FROM "transaction" WHERE category_id = ANY(:c) OR suggested_category_id = ANY(:c)',
                        c=touched,
                    ),
                }
                out = ROOT / "logs" / f"taxonomy_cleanup_backup_{now:%Y%m%d%H%M%S}.json"
                out.write_text(json.dumps(backup, default=str), encoding="utf-8")
                log.append(f"backup: {out}")

            # 1. Groups.
            for old, new in GROUP_RENAMES.items():
                await q(s, "UPDATE category_group SET name = :n WHERE id = :i", n=new, i=groups[old].id)
            gid = {GROUP_RENAMES.get(n, n): r.id for n, r in groups.items()}
            gsort = {GROUP_RENAMES.get(n, n): r.sort_order for n, r in groups.items()}
            for name, near in NEW_GROUPS.items():
                gid[name] = (
                    await q(
                        s,
                        "INSERT INTO category_group (name, type, sort_order) VALUES (:n, 'expense', :o) RETURNING id",
                        n=name,
                        o=gsort[near],
                    )
                ).scalar_one()
            log.append(f"groups renamed {GROUP_RENAMES}; created {list(NEW_GROUPS)}")

            # 2. Categories: renames/moves, then new ones.
            for old, (new, grp) in RENAMES.items():
                await q(
                    s,
                    "UPDATE category SET name = :n, group_id = coalesce(:g, group_id) WHERE id = :i",
                    n=new,
                    g=gid[grp] if grp else None,
                    i=cats[old],
                )
            final = {RENAMES.get(n, (n, None))[0]: i for n, i in cats.items()}
            for name, grp in NEW_CATEGORIES.items():
                final[name] = (
                    await q(
                        s,
                        "INSERT INTO category (group_id, name, type) VALUES (:g, :n, 'expense') RETURNING id",
                        g=gid[grp],
                        n=name,
                    )
                ).scalar_one()
            log.append(f"categories renamed: {', '.join(f'{o} -> {n}' for o, (n, _) in RENAMES.items() if o != n)}")
            log.append(f"categories created: {', '.join(NEW_CATEGORIES)}")

            # 3. Transactions of the source categories (soft-deleted too, so retired categories end up empty).
            txns = (
                await q(
                    s,
                    """--sql
                    SELECT t.id, t.category_id, t.merchant, mp.display_name AS display, t.description, t.notes,
                           t.amount, t.txn_date, a.account_type
                    FROM "transaction" t
                    LEFT JOIN merchant_profile mp ON mp.key = t.merchant
                    LEFT JOIN account a ON a.id = t.account_id
                    WHERE t.category_id = ANY(:c)
                    """,
                    c=list(source_ids),
                )
            ).all()
            moves: dict[int, list[int]] = defaultdict(list)
            stats: dict[tuple[str, str], list] = defaultdict(lambda: [0, Decimal(0), Counter()])
            key_dest: dict[str, Counter] = defaultdict(Counter)
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
                    if dest != default and dest in NEW_CATEGORIES:
                        key_dest[r.merchant][dest] += 1
            for (src, dest), (n, total, top) in sorted(stats.items()):
                default = SOURCES[src][1]
                shown = ", ".join(f"{m} {c}" for m, c in top.most_common(5 if dest == default else 14))
                log.append(f"{src} -> {dest}: {n} rows, {total:,.2f}  [{shown}]")
            for cid, ids in moves.items():
                await q(
                    s,
                    'UPDATE "transaction" SET category_id = :c, suggested_category_id = NULL, '
                    "suggestion_confidence = NULL, suggestion_reason = NULL WHERE id = ANY(:ids)",
                    c=cid,
                    ids=ids,
                )
            log.append(f"transactions moved: {sum(len(v) for v in moves.values())}")

            # 4. Income refunds -> the merchant's usual expense category.
            refunds = (
                await q(
                    s,
                    """--sql
                    WITH usual AS (
                        SELECT DISTINCT ON (t.merchant) t.merchant, t.category_id
                        FROM "transaction" t
                        JOIN category c ON c.id = t.category_id
                        JOIN category_group g ON g.id = c.group_id
                        WHERE c.type = 'expense' AND NOT c.hide_from_reports AND NOT g.hide_from_reports
                          AND c.name <> ALL(:skip) AND t.deleted_at IS NULL AND t.merchant IS NOT NULL
                        GROUP BY t.merchant, t.category_id
                        ORDER BY t.merchant, count(*) DESC
                    )
                    SELECT t.id, t.merchant, t.amount, c.name AS dest, u.category_id
                    FROM "transaction" t
                    JOIN usual u ON u.merchant = t.merchant
                    JOIN category c ON c.id = u.category_id
                    WHERE t.category_id = :income AND t.amount > 0
                    """,
                    skip=sorted(REFUND_SKIP),
                    income=final["Income"],
                )
            ).all()
            refunds = [r for r in refunds if not generic(r.merchant)]
            by_dest: dict[str, list] = defaultdict(list)
            for r in refunds:
                by_dest[r.dest].append(r)
            for dest, rs in sorted(by_dest.items(), key=lambda kv: -len(kv[1])):
                top = Counter(r.merchant for r in rs).most_common(8)
                log.append(
                    f"Income refund -> {dest}: {len(rs)} rows, {sum(r.amount for r in rs):,.2f}  "
                    f"[{', '.join(f'{m} {c}' for m, c in top)}]"
                )
                await q(
                    s,
                    'UPDATE "transaction" SET category_id = :c WHERE id = ANY(:ids)',
                    c=rs[0].category_id,
                    ids=[r.id for r in rs],
                )

            # 5. Rules: repoint, retire the catch-all venmo rule, add rules for merchants routed to new categories.
            acct_types = dict((await q(s, "SELECT id, account_type FROM account")).all())
            displays = dict((await q(s, "SELECT key, display_name FROM merchant_profile")).all())
            rules = (await q(s, "SELECT id, match_type, pattern, category_id, account_id, source FROM category_rule")).all()
            existing = {r.pattern for r in rules if r.match_type == "merchant"}
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
                    account_type=acct_types.get(r.account_id),
                )
                dest = classify(probe, *SOURCES[src])
                if final[dest] != r.category_id:
                    await q(s, "UPDATE category_rule SET category_id = :c WHERE id = :i", c=final[dest], i=r.id)
                    log.append(f"rule {r.id} {r.match_type}:{r.pattern!r} ({r.source}) {src} -> {dest}")
            res = await q(
                s,
                "UPDATE category_rule SET is_active = false, note = :n "
                "WHERE match_type = 'merchant' AND pattern = 'venmo' AND source = 'learned'",
                n=f"deactivated: {NOTE} (sent every Venmo payment to Gifts)",
            )
            log.append(f"deactivated learned venmo rule: {res.rowcount}")
            added = []
            for key, dests in sorted(key_dest.items(), key=lambda kv: kv[0] or ""):
                if generic(key) or key in existing or len(dests) != 1 or sum(dests.values()) < 3:
                    continue
                dest = next(iter(dests))
                await q(
                    s,
                    "INSERT INTO category_rule (match_type, pattern, category_id, source, note) "
                    "VALUES ('merchant', :p, :c, 'user', :n)",
                    p=key,
                    c=final[dest],
                    n=NOTE,
                )
                added.append(f"{key} -> {dest}")
            log.append(f"rules added ({len(added)}):" + "".join(f"\n    {a}" for a in added))

            # 6. Other references, aliases, then drop retired categories and groups.
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
            aliases |= {old.lower(): new for old, (new, _) in RENAMES.items() if old != new}
            aliases |= ALIAS_OVERRIDES
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
            for g in DELETE_GROUPS:
                await q(s, "DELETE FROM category_group WHERE id = :i", i=groups[g].id)
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

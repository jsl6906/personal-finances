"""Automated sources (Tiller sheet, SimpleFIN): normalize a feed, link accounts, and route new transactions
through the regular import pipeline (duplicate detection, category mapping), auto-committing clean batches."""

import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from ledger.imports.parsing import column_profile
from ledger.imports.service import _insert_rows, _Refs, commit_batch, prepare_batch
from ledger.models import (
    Account,
    AccountBalance,
    Category,
    CategoryAlias,
    DuplicatePair,
    Holding,
    ImportBatch,
    ImportRow,
    Institution,
    Source,
    Transaction,
)

log = logging.getLogger(__name__)

FEED_COLUMNS = [
    "Date",
    "Posted",
    "Description",
    "Full Description",
    "Amount",
    "Account",
    "Category",
    "Check Number",
    "Transaction ID",
    "Note",
]
FEED_MAPPING = {
    "Date": "txn_date",
    "Posted": "posted_date",
    "Description": "description",
    "Full Description": "original_description",
    "Amount": "amount",
    "Account": "account",
    "Category": "category",
    "Check Number": "check_number",
    "Transaction ID": "external_id",
    "Note": "notes",
}
OPEN_BATCH_STATUSES = ("extracting", "mapping", "preparing", "review")
AI_SAME, AI_DIFFERENT = Decimal("0.9"), Decimal("0.1")


@dataclass
class FeedAccount:
    key: str
    name: str
    institution: str | None = None
    mask: str | None = None
    type_hint: str | None = None


@dataclass
class FeedTxn:
    external_id: str
    account_key: str
    txn_date: date
    amount: Decimal
    description: str
    posted_date: date | None = None
    full_description: str | None = None
    category: str | None = None
    check_number: str | None = None
    note: str | None = None


@dataclass
class FeedBalance:
    account_key: str
    as_of: date
    balance: Decimal
    available: Decimal | None = None


@dataclass
class FeedHolding:
    account_key: str
    as_of: date
    external_id: str
    symbol: str | None
    description: str | None
    shares: Decimal | None
    market_value: Decimal | None
    cost_basis: Decimal | None
    currency: str | None


@dataclass
class Feed:
    accounts: dict[str, FeedAccount] = field(default_factory=dict)
    transactions: list[FeedTxn] = field(default_factory=list)
    balances: list[FeedBalance] = field(default_factory=list)
    holdings: list[FeedHolding] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    synthetic_ids: int = 0


def last4(value: str | None) -> str | None:
    digits = re.findall(r"\d", value or "")
    return "".join(digits[-4:]) if len(digits) >= 4 else None


def guess_account_type(name: str, hint: str | None = None) -> str:
    s = f"{hint or ''} {name}".lower()
    for words, kind in (
        (("credit", "card", "visa", "mastercard", "amex", "discover"), "credit_card"),
        (("mortgage",), "mortgage"),
        (("loan", "auto", "student"), "loan"),
        (("401k", "401(k)", "ira", "roth", "tsp", "retire"), "retirement"),
        (("brokerage", "invest", "stock"), "investment"),
        (("saving", "money market"), "savings"),
    ):
        if any(w in s for w in words):
            return kind
    return "checking"


async def ensure_account(session: AsyncSession, kind: str, fa: FeedAccount) -> Account:
    """Find the account linked to this feed account (external_refs[kind]), else match by mask or name, else create."""
    acct = await session.scalar(select(Account).where(Account.external_refs[kind].astext == fa.key))
    if acct is None:
        tail = last4(fa.mask) or last4(fa.name)
        if tail:
            by_mask = (
                (await session.scalars(select(Account).where(Account.mask.is_not(None), Account.is_closed.is_(False))))
                .unique()
                .all()
            )
            hits = [a for a in by_mask if last4(a.mask) == tail]
            acct = hits[0] if len(hits) == 1 else None
        if acct is None:
            acct = await session.scalar(select(Account).where(Account.name.ilike(fa.name)))
    if acct is None:
        inst_id = None
        if fa.institution:
            await session.execute(insert(Institution).values(name=fa.institution[:200]).on_conflict_do_nothing())
            inst_id = await session.scalar(select(Institution.id).where(Institution.name == fa.institution[:200]))
        name = fa.name[:200]
        if await session.scalar(select(Account.id).where(Account.name == name)):
            name = f"{fa.name} ···{last4(fa.mask) or fa.key[-4:]}"[:200]
        acct = Account(
            name=name,
            institution_id=inst_id,
            account_type=guess_account_type(fa.name, fa.type_hint),
            mask=last4(fa.mask) or last4(fa.name),
            external_refs={},
        )
        session.add(acct)
    if (acct.external_refs or {}).get(kind) != fa.key:
        acct.external_refs = {**(acct.external_refs or {}), kind: fa.key}
    await session.flush()
    return acct


async def _known_external_ids(session: AsyncSession, kind: str, ids: list[str]) -> set[str]:
    known: set[str] = set()
    for start in range(0, len(ids), 1000):
        chunk = ids[start : start + 1000]
        known.update(
            (await session.scalars(select(Transaction.external_id).where(Transaction.external_id.in_(chunk)))).all()
        )
        known.update(
            (
                await session.scalars(
                    select(ImportRow.external_id)
                    .join(ImportBatch, ImportBatch.id == ImportRow.batch_id)
                    .where(
                        ImportRow.external_id.in_(chunk),
                        ImportBatch.origin == kind,
                        ImportBatch.status.in_(OPEN_BATCH_STATUSES),
                    )
                )
            ).all()
        )
    return known


async def _fill_categories(session: AsyncSession, feed: Feed) -> int:
    """Existing uncategorized transactions pick up a category assigned in the source since the last sync."""
    labelled = {t.external_id: t.category for t in feed.transactions if t.category}
    if not labelled:
        return 0
    refs = _Refs(
        [], (await session.scalars(select(Category))).unique().all(), (await session.scalars(select(CategoryAlias))).all()
    )
    filled = 0
    for start in range(0, len(labelled), 1000):
        chunk = list(labelled)[start : start + 1000]
        rows = (
            (
                await session.scalars(
                    select(Transaction).where(
                        Transaction.external_id.in_(chunk),
                        Transaction.category_id.is_(None),
                        Transaction.deleted_at.is_(None),
                    )
                )
            )
            .unique()
            .all()
        )
        for t in rows:
            cid = refs.category(labelled[t.external_id])
            if cid:
                t.category_id, t.category_source = cid, "source"
                filled += 1
    return filled


async def auto_resolve(session: AsyncSession, batch: ImportBatch) -> int:
    """Apply confident AI verdicts to possible duplicates; returns how many remain ambiguous."""
    pairs = (
        await session.scalars(
            select(DuplicatePair)
            .join(ImportRow, DuplicatePair.import_row_id == ImportRow.id)
            .where(ImportRow.batch_id == batch.id, DuplicatePair.status == "pending")
        )
    ).all()
    ambiguous = 0
    for p in pairs:
        if p.ai_probability is not None and p.ai_probability >= AI_SAME:
            await session.execute(update(ImportRow).where(ImportRow.id == p.import_row_id).values(decision="skip_duplicate"))
        elif p.ai_probability is not None and p.ai_probability <= AI_DIFFERENT:
            await session.execute(update(ImportRow).where(ImportRow.id == p.import_row_id).values(decision="keep"))
        else:
            ambiguous += 1
    await session.flush()
    return ambiguous


async def apply_feed(session: AsyncSession, source: Source, feed: Feed) -> dict:
    from ledger.jobs.worker import enqueue

    kind = source.kind
    accounts = {key: await ensure_account(session, kind, fa) for key, fa in feed.accounts.items()}

    for b in feed.balances:
        if b.account_key not in accounts:
            continue
        stmt = insert(AccountBalance).values(
            account_id=accounts[b.account_key].id, as_of=b.as_of, balance=b.balance, available=b.available, source=kind
        )
        await session.execute(
            stmt.on_conflict_do_update(
                index_elements=["account_id", "as_of", "source"],
                set_={"balance": stmt.excluded.balance, "available": stmt.excluded.available},
            )
        )
    for h in feed.holdings:
        if h.account_key not in accounts:
            continue
        values = {k: v for k, v in h.__dict__.items() if k != "account_key"}
        stmt = insert(Holding).values(account_id=accounts[h.account_key].id, source=kind, **values)
        await session.execute(
            stmt.on_conflict_do_update(
                index_elements=["account_id", "as_of", "external_id"],
                set_={k: stmt.excluded[k] for k in ("symbol", "description", "shares", "market_value", "cost_basis")},
            )
        )

    known = await _known_external_ids(session, kind, [t.external_id for t in feed.transactions])
    fresh = [t for t in feed.transactions if t.external_id not in known and t.account_key in accounts]
    filled = await _fill_categories(session, feed)
    result = {
        "accounts": len(accounts),
        "fetched": len(feed.transactions),
        "new": len(fresh),
        "categories_filled": filled,
        "balances": len(feed.balances),
        "holdings": len(feed.holdings),
        "warnings": feed.warnings[:20],
    }
    if not fresh:
        await session.commit()
    else:
        rows = [
            {
                "Date": t.txn_date.isoformat(),
                "Posted": t.posted_date.isoformat() if t.posted_date else "",
                "Description": t.description,
                "Full Description": t.full_description or "",
                "Amount": f"{t.amount:.2f}",
                "Account": accounts[t.account_key].name,
                "Category": t.category or "",
                "Check Number": t.check_number or "",
                "Transaction ID": t.external_id,
                "Note": t.note or "",
            }
            for t in fresh
        ]
        batch = ImportBatch(
            source_type="spreadsheet",
            origin=kind,
            columns=column_profile(FEED_COLUMNS, rows),
            mapping=dict(FEED_MAPPING),
            mapping_source="preset",
            options={"date_format": "%Y-%m-%d", "dayfirst": False, "invert_sign": False},
            defaults={},
            status="preparing",
        )
        session.add(batch)
        await session.flush()
        await _insert_rows(session, batch, rows)
        await prepare_batch(session, batch)

    # Commit this sync's batch and any earlier one still waiting (e.g. its duplicates were since resolved).
    waiting = (
        (
            await session.scalars(
                select(ImportBatch)
                .where(ImportBatch.origin == kind, ImportBatch.status == "review")
                .order_by(ImportBatch.id)
            )
        )
        .unique()
        .all()
    )
    inserted = skipped = needs_review = 0
    uncategorized: list[int] = []
    for b in waiting:
        ambiguous = await auto_resolve(session, b)
        if ambiguous:
            needs_review += ambiguous
            result["batch_id"] = b.id
            continue
        committed = await commit_batch(session, b)
        inserted += committed["inserted"]
        skipped += committed["skipped_duplicates"]
        uncategorized += committed["uncategorized_ids"]
    if uncategorized:
        await enqueue(session, "categorize", {"ids": uncategorized})
    if inserted:
        await enqueue(session, "detect_anomalies", {})
    await session.commit()
    result.update(inserted=inserted, skipped_duplicates=skipped)
    if needs_review:
        result["needs_review"] = needs_review
    return result


async def record_run(session: AsyncSession, source: Source, result: dict | None, error: str | None) -> None:
    source.last_sync_at = datetime.now(UTC)
    source.last_status = "failed" if error else ("needs_review" if (result or {}).get("needs_review") else "ok")
    source.last_error = error
    if result is not None:
        source.last_result = result
    await session.commit()

"""Merchant identity: automatic keys from descriptions, user merges (aliases), display names, per-transaction overrides."""

import re
from string import capwords

from sqlalchemy import delete, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from ledger.db.filters import id_in
from ledger.models import CategoryRule, MerchantProfile, MerchantSuggestion, Transaction
from ledger.services.normalize import normalize_merchant

_USER_NON_WORD = re.compile(r"[^a-z0-9&' ]+")


class MerchantError(ValueError):
    pass


def default_name(key: str | None) -> str | None:
    return capwords(key) if key else None


def user_key(name: str) -> str | None:
    """Key for a merchant name typed by the user: light cleanup only (digits are kept)."""
    s = " ".join(_USER_NON_WORD.sub(" ", name.lower()).split())
    return s[:200] or None


async def alias_map(session: AsyncSession) -> dict[str, str]:
    rows = await session.execute(
        select(MerchantProfile.key, MerchantProfile.alias_of).where(MerchantProfile.alias_of.is_not(None))
    )
    return dict(rows.all())


def canonical(key: str | None, aliases: dict[str, str]) -> str | None:
    return aliases.get(key, key) if key else None


async def resolve(session: AsyncSession, key: str | None) -> str | None:
    if not key:
        return None
    return await session.scalar(select(MerchantProfile.alias_of).where(MerchantProfile.key == key)) or key


async def auto_key(session: AsyncSession, description: str | None) -> str | None:
    return await resolve(session, normalize_merchant(description))


async def _profile(session: AsyncSession, key: str) -> MerchantProfile:
    p = await session.scalar(select(MerchantProfile).where(MerchantProfile.key == key))
    if p is None:
        p = MerchantProfile(key=key)
        session.add(p)
    return p


async def display_names(session: AsyncSession, keys: list[str]) -> dict[str, str]:
    if not keys:
        return {}
    rows = await session.execute(
        select(MerchantProfile.key, MerchantProfile.display_name).where(
            MerchantProfile.key.in_(keys), MerchantProfile.display_name.is_not(None)
        )
    )
    return dict(rows.all())


async def rename(session: AsyncSession, key: str, name: str | None) -> None:
    p = await _profile(session, key)
    if p.alias_of:
        raise MerchantError("This merchant is merged into another; rename that one instead")
    p.display_name = (name or "").strip()[:200] or None


async def set_transaction_merchant(session: AsyncSession, t: Transaction, name: str | None) -> None:
    """Override one transaction's merchant (blank = back to automatic)."""
    if not name or not name.strip():
        t.merchant_source = None
        t.merchant = await auto_key(session, t.description)
        return
    named = await session.scalar(
        select(MerchantProfile.key).where(
            func.lower(MerchantProfile.display_name) == name.strip().lower(), MerchantProfile.alias_of.is_(None)
        )
    )
    key = named or user_key(name)
    if key is None:
        raise MerchantError("Merchant name needs letters or digits")
    key = await resolve(session, key)
    p = await _profile(session, key)
    if not p.display_name and not named:
        p.display_name = name.strip()[:200]
    t.merchant, t.merchant_source = key, "user"


async def merge(session: AsyncSession, source: str, target: str) -> int:
    """Fold `source` into `target`: past and future transactions, aliases and category rule follow. Returns rows moved."""
    target = await resolve(session, target)
    if not source or not target or source == target:
        raise MerchantError("Pick two different merchants")
    src = await _profile(session, source)
    if src.alias_of:
        raise MerchantError("That merchant is already merged into another")
    tgt = await _profile(session, target)
    if src.display_name and not tgt.display_name:
        tgt.display_name = src.display_name
    src.alias_of = target
    await session.execute(update(MerchantProfile).where(MerchantProfile.alias_of == source).values(alias_of=target))
    moved = await session.execute(update(Transaction).where(Transaction.merchant == source).values(merchant=target))
    rules = (
        await session.scalars(
            select(CategoryRule).where(CategoryRule.match_type == "merchant", CategoryRule.pattern.in_([source, target]))
        )
    ).all()
    taken = {(r.account_id, r.amount_min, r.amount_max) for r in rules if r.pattern == target}
    for r in rules:
        if r.pattern != source:
            continue
        if (r.account_id, r.amount_min, r.amount_max) in taken:
            await session.delete(r)
        else:
            r.pattern = target
    return moved.rowcount or 0


async def unmerge(session: AsyncSession, source: str) -> int:
    """Undo a merge: transactions whose description still produces `source` get it back. Returns rows moved."""
    p = await session.scalar(select(MerchantProfile).where(MerchantProfile.key == source))
    if p is None or not p.alias_of:
        raise MerchantError("That merchant is not merged")
    target, p.alias_of = p.alias_of, None
    if not p.display_name:
        await session.execute(delete(MerchantProfile).where(MerchantProfile.id == p.id))
    rows = await session.execute(
        select(Transaction.id, Transaction.description).where(
            Transaction.merchant == target, Transaction.merchant_source.is_(None)
        )
    )
    ids = [i for i, desc in rows if normalize_merchant(desc) == source]
    if ids:
        await session.execute(update(Transaction).where(id_in(Transaction.id, ids)).values(merchant=source))
    return len(ids)


async def mark_reviewed(session: AsyncSession, keys: list[str]) -> None:
    """Keep these keys out of future AI merchant reviews (unless a re-review of everything is asked for)."""
    if not keys:
        return
    await session.flush()
    await session.execute(
        text(
            """--sql
            INSERT INTO merchant_profile (key, reviewed_at) SELECT DISTINCT unnest(CAST(:keys AS text[])), now()
            ON CONFLICT (key) DO UPDATE SET reviewed_at = now(), updated_at = now()
            """
        ),
        {"keys": list(keys)},
    )


async def accept_suggestion(
    session: AsyncSession,
    s: MerchantSuggestion,
    target: str | None = None,
    keys: list[str] | None = None,
    name: str | None = None,
) -> int:
    """Apply an AI merchant suggestion, optionally edited: keep `target`, merge only `keys`, use `name`.

    Returns transactions moved.
    """
    members = [s.target_key, *s.source_keys]
    target = target or s.target_key
    if target not in members:
        raise MerchantError("Pick the merchant to keep from this suggestion")
    chosen = members if keys is None else [k for k in members if k in set(keys)]
    target = await resolve(session, target)
    moved = 0
    for k in chosen:
        # Skip the target itself and keys merged elsewhere since the review ran.
        if k != target and await resolve(session, k) == k:
            moved += await merge(session, k, target)
    name = ((s.display_name if name is None else name) or "").strip()
    if name:
        await rename(session, target, name)
    await mark_reviewed(session, members)
    s.status = "accepted"
    return moved

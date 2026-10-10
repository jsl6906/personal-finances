"""Home and vehicle valuations, stored as account_balance rows (source = manual | rentcast | depreciation) so they flow
into balances, net worth and history like any synced account."""

import logging
from datetime import UTC, date, datetime
from decimal import Decimal

import httpx
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from ledger.assets import rentcast
from ledger.db.engine import get_sessionmaker
from ledger.jobs.worker import JobContext, job_handler
from ledger.models import Account, AccountBalance, Asset

log = logging.getLogger(__name__)

DEFAULT_DEPRECIATION = Decimal("0.15")
# RentCast's free tier is 50 lookups/month, so scheduled runs skip homes valued recently.
RENTCAST_REFRESH_DAYS = 25


async def record_value(session: AsyncSession, account_id: int, as_of: date, value: Decimal, source: str) -> None:
    stmt = insert(AccountBalance).values(account_id=account_id, as_of=as_of, balance=value, source=source)
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=["account_id", "as_of", "source"],
            set_={"balance": stmt.excluded.balance, "created_at": func.now()},
        )
    )


async def _latest(session: AsyncSession, account_id: int, source: str) -> AccountBalance | None:
    return await session.scalar(
        select(AccountBalance)
        .where(AccountBalance.account_id == account_id, AccountBalance.source == source)
        .order_by(AccountBalance.as_of.desc(), AccountBalance.created_at.desc())
        .limit(1)
    )


def depreciate(value: Decimal, since: date, on: date, rate: Decimal) -> Decimal:
    years = Decimal((on - since).days) / Decimal("365.25")
    return (value * (1 - rate) ** years).quantize(Decimal("0.01"))


async def value_asset(session: AsyncSession, asset: Asset, force: bool = False) -> dict:
    today = date.today()
    if asset.method == "rentcast":
        last = await _latest(session, asset.account_id, "rentcast")
        if last and not force and (today - last.as_of).days < RENTCAST_REFRESH_DAYS:
            return {"skipped": f"valued {last.as_of.isoformat()}"}
        data = await rentcast.estimate(asset.address or "")
        value = Decimal(str(data["price"])).quantize(Decimal("0.01"))
        result = {
            "value": float(value),
            "low": data.get("priceRangeLow"),
            "high": data.get("priceRangeHigh"),
            "comparables": len(data.get("comparables") or []),
        }
    elif asset.method == "depreciation":
        # A recorded value (e.g. a KBB check) re-anchors the curve; otherwise start from the purchase.
        anchor = await _latest(session, asset.account_id, "manual")
        if anchor and (asset.purchase_date is None or anchor.as_of >= asset.purchase_date):
            base, since, basis = anchor.balance, anchor.as_of, "recorded value"
        elif asset.purchase_price is not None and asset.purchase_date is not None:
            base, since, basis = asset.purchase_price, asset.purchase_date, "purchase price"
        else:
            raise ValueError("Depreciation needs a purchase price and date, or a recorded value to start from")
        if since >= today:
            return {"skipped": f"{basis} is current"}
        rate = asset.depreciation_rate if asset.depreciation_rate is not None else DEFAULT_DEPRECIATION
        value = depreciate(base, since, today, rate)
        result = {"value": float(value), "basis": basis, "since": since.isoformat(), "rate": float(rate)}
    else:
        return {"skipped": "manual"}
    await record_value(session, asset.account_id, today, value, asset.method)
    asset.last_valued_at = datetime.now(UTC)
    asset.last_error = None
    asset.last_result = result
    return result


async def run_valuation(session: AsyncSession, asset: Asset, force: bool = False) -> dict:
    """Value one asset and commit, recording (rather than raising) expected failures on the asset."""
    account_id = asset.account_id
    try:
        result = await value_asset(session, asset, force)
    except (rentcast.RentCastError, ValueError, httpx.HTTPError) as exc:
        await session.rollback()
        asset = await session.get(Asset, account_id, populate_existing=True)
        asset.last_error = str(exc)[:1000] or type(exc).__name__
        await session.commit()
        return {"error": asset.last_error}
    await session.commit()
    return result


@job_handler("value_assets")
async def value_assets_job(ctx: JobContext) -> dict:
    async with get_sessionmaker()() as session:
        ids = (
            await session.scalars(
                select(Asset.account_id)
                .join(Account, Account.id == Asset.account_id)
                .where(Asset.method != "manual", ~Account.is_closed)
                .order_by(Asset.account_id)
            )
        ).all()
    results = {}
    for i, aid in enumerate(ids):
        await ctx.progress(i / max(len(ids), 1), f"Valuing asset {aid}")
        async with get_sessionmaker()() as session:
            results[str(aid)] = await run_valuation(session, await session.get(Asset, aid), bool(ctx.payload.get("force")))
    return results

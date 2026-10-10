from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ledger.assets.service import DEFAULT_DEPRECIATION, record_value, run_valuation
from ledger.config import get_settings
from ledger.db.engine import get_session
from ledger.models import Account, Asset
from ledger.schemas import AssetCreate, AssetIn, ValuationIn

router = APIRouter(tags=["assets"])

ASSET_TYPES = ("property", "vehicle")
LOAN_TYPES = ("loan", "mortgage")

_LIST = text(
    """--sql
    SELECT a.id AS account_id, a.name, a.account_type, a.is_hidden,
           coalesce(s.method, 'manual') AS method, s.loan_account_id, s.purchase_date, s.purchase_price, s.address,
           s.depreciation_rate, s.last_valued_at, s.last_error, coalesce(s.last_result, '{}') AS last_result,
           v.balance AS value, v.as_of AS value_as_of, v.source AS value_source,
           l.name AS loan_name, lb.balance AS loan_balance, lb.as_of AS loan_as_of
    FROM account a
    LEFT JOIN asset s ON s.account_id = a.id
    LEFT JOIN LATERAL (
        SELECT balance, as_of, source FROM account_balance WHERE account_id = a.id
        ORDER BY as_of DESC, created_at DESC LIMIT 1
    ) v ON true
    LEFT JOIN account l ON l.id = s.loan_account_id
    LEFT JOIN LATERAL (
        SELECT balance, as_of FROM account_balance WHERE account_id = l.id
        ORDER BY as_of DESC, created_at DESC LIMIT 1
    ) lb ON true
    WHERE a.account_type IN ('property', 'vehicle') AND NOT a.is_closed
    ORDER BY a.account_type, a.name
    """
)


def _f(v):
    return float(v) if v is not None else None


@router.get("/assets")
async def list_assets(session: AsyncSession = Depends(get_session)):
    rows = []
    for r in (await session.execute(_LIST)).mappings():
        d = dict(r)
        for k in ("purchase_price", "depreciation_rate", "value", "loan_balance"):
            d[k] = _f(d[k])
        # Lenders report loan balances with either sign; equity always subtracts the amount owed.
        owed = abs(d["loan_balance"]) if d["loan_balance"] is not None else None
        d["equity"] = d["value"] - owed if d["value"] is not None and owed is not None else None
        rows.append(d)
    return {
        "rentcast_configured": get_settings().rentcast_api_key is not None,
        "default_depreciation_rate": float(DEFAULT_DEPRECIATION),
        "assets": rows,
    }


async def _asset_account(session: AsyncSession, account_id: int) -> Account:
    acct = await session.get(Account, account_id)
    if acct is None or acct.account_type not in ASSET_TYPES:
        raise HTTPException(404, f"Asset account {account_id} not found")
    return acct


async def _check_loan(session: AsyncSession, loan_id: int | None) -> None:
    if loan_id is None:
        return
    loan = await session.get(Account, loan_id)
    if loan is None or loan.account_type not in LOAN_TYPES:
        raise HTTPException(422, "Linked loan must be a loan or mortgage account")


def _settings(body: AssetIn) -> dict:
    d = body.model_dump(include=set(AssetIn.model_fields))
    d["address"] = (d["address"] or "").strip() or None
    return d


@router.post("/assets", status_code=201)
async def create_asset(body: AssetCreate, session: AsyncSession = Depends(get_session)):
    await _check_loan(session, body.loan_account_id)
    if await session.scalar(select(Account.id).where(Account.name == body.name)):
        raise HTTPException(409, "Name already exists")
    acct = Account(name=body.name, account_type=body.account_type)
    session.add(acct)
    await session.flush()
    asset = Asset(account_id=acct.id, **_settings(body))
    session.add(asset)
    if body.value is not None:
        await record_value(session, acct.id, date.today(), body.value, "manual")
    await session.commit()
    return {"account_id": acct.id, "valuation": await run_valuation(session, asset)}


@router.put("/assets/{account_id}")
async def update_asset(account_id: int, body: AssetIn, session: AsyncSession = Depends(get_session)):
    await _asset_account(session, account_id)
    await _check_loan(session, body.loan_account_id)
    asset = await session.get(Asset, account_id)
    if asset is None:
        asset = Asset(account_id=account_id)
        session.add(asset)
    new = _settings(body)
    changed = asset.method != new["method"] or asset.address != new["address"]
    for k, v in new.items():
        setattr(asset, k, v)
    if new["method"] == "manual":
        asset.last_error = None
    await session.commit()
    return {"account_id": account_id, "valuation": await run_valuation(session, asset, force=changed)}


@router.post("/assets/{account_id}/valuations")
async def add_valuation(account_id: int, body: ValuationIn, session: AsyncSession = Depends(get_session)):
    await _asset_account(session, account_id)
    as_of = body.as_of or date.today()
    if as_of > date.today():
        raise HTTPException(422, "Valuation date can't be in the future")
    await record_value(session, account_id, as_of, body.value, "manual")
    await session.commit()
    asset = await session.get(Asset, account_id)
    # A back-dated value re-anchors the depreciation curve, so bring today's estimate in line with it.
    if asset is not None and asset.method == "depreciation" and as_of < date.today():
        return {"account_id": account_id, "valuation": await run_valuation(session, asset)}
    return {"account_id": account_id, "valuation": {"value": float(body.value), "as_of": as_of.isoformat()}}


@router.post("/assets/{account_id}/refresh")
async def refresh_asset(account_id: int, session: AsyncSession = Depends(get_session)):
    await _asset_account(session, account_id)
    asset = await session.get(Asset, account_id)
    if asset is None or asset.method == "manual":
        raise HTTPException(422, "This asset is valued manually; record a value instead")
    result = await run_valuation(session, asset, force=True)
    if "error" in result:
        raise HTTPException(502, result["error"])
    return {"account_id": account_id, "valuation": result}

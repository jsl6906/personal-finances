"""RentCast AVM (https://developers.rentcast.io/reference/value-estimate): one API call per home per refresh."""

import httpx

from ledger.config import get_settings

URL = "https://api.rentcast.io/v1/avm/value"


class RentCastError(RuntimeError):
    pass


async def estimate(address: str) -> dict:
    key = get_settings().rentcast_api_key
    if key is None:
        raise RentCastError("RENTCAST_API_KEY is not configured")
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(
            URL, params={"address": address}, headers={"X-Api-Key": key.get_secret_value(), "Accept": "application/json"}
        )
    if r.status_code in (401, 403):
        raise RentCastError("RentCast rejected the API key (or the plan doesn't include value estimates)")
    if r.status_code == 404:
        raise RentCastError("RentCast couldn't find that address; use 'Street, City, State, Zip'")
    if r.status_code == 429:
        raise RentCastError("RentCast usage limit reached; try again next month or upgrade the plan")
    if r.status_code != 200:
        raise RentCastError(f"RentCast request failed (HTTP {r.status_code})")
    data = r.json()
    if not data.get("price"):
        raise RentCastError("RentCast returned no estimate for that address")
    return data

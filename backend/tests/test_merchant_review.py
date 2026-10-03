import re

from ledger.ai import merchant_review
from ledger.jobs.worker import JobContext

NAMES = {
    "mrv burger fairfax": "MRV Burger",
    "mrv burger reston": "MRV Burger",
    "mrv tacohut catering": "MRV Taco Hut",
    "mrv dangerouslytastypies": "MRV Dangerously Tasty Pies",
    "mrv plain shop": "Mrv Plain Shop",
    "mrv donut den": "MRV Donut Den",
}


def test_group_norm():
    assert merchant_review.group_norm("The McDonald’s, Inc.") == merchant_review.group_norm("mcdonalds")
    assert merchant_review.group_norm("Café Rio") == "caferio"
    assert merchant_review.group_norm("AT&T") != merchant_review.group_norm("ATT")


async def test_review_flow(client, monkeypatch):
    for i, desc in enumerate(
        [
            "MRV BURGER #12 FAIRFAX VA",
            "MRV BURGER #12 FAIRFAX VA",
            "MRV BURGER 0099 RESTON",
            "MRV TACO HUT",
            "MRV TACOHUT CATERING",
            "MRV DANGEROUSLYTASTYPIES",
            "MRV PLAIN SHOP",
            "MRV DONUT DEN",
        ]
    ):
        r = await client.post(
            "/api/transactions", json={"txn_date": f"2025-04-{i + 1:02d}", "description": desc, "amount": "-7.00"}
        )
        assert r.status_code == 201, r.text
    await client.put("/api/merchants/name", json={"key": "mrv taco hut", "display_name": "MRV Taco Hut"})

    async def fake_generate(prompt, **kwargs):
        assert "MRV Taco Hut" in prompt.split("Rows")[0]
        assert "mrv taco hut |" not in prompt
        rows = re.findall(r"^(\d+) \| ([^|]+) \|", prompt, re.M)
        return merchant_review.Names(
            items=[merchant_review.Named(i=int(i), name=NAMES[k.strip()]) for i, k in rows if k.strip() in NAMES]
        )

    monkeypatch.setattr(merchant_review, "generate", fake_generate)
    job = (await client.post("/api/merchants/review/run", json={})).json()
    assert (await client.post("/api/merchants/review/run", json={})).status_code == 409
    result = await merchant_review.merchant_review_job(JobContext(job["id"], "merchant_review", {}))
    assert result["merges"] >= 2 and result["renames"] >= 2

    state = (await client.get("/api/merchants/review")).json()
    mine = {s["target"]["key"]: s for s in state["suggestions"] if s["target"]["key"].startswith("mrv")}
    burger = mine["mrv burger fairfax"]
    assert burger["kind"] == "merge" and burger["display_name"] == "MRV Burger"
    assert [x["key"] for x in burger["sources"]] == ["mrv burger reston"] and burger["target"]["count"] == 2
    taco = mine["mrv taco hut"]
    assert taco["kind"] == "merge" and [x["key"] for x in taco["sources"]] == ["mrv tacohut catering"]
    assert mine["mrv dangerouslytastypies"]["kind"] == "rename" and not mine["mrv dangerouslytastypies"]["minor"]
    assert mine["mrv donut den"]["minor"]
    assert "mrv plain shop" not in mine

    # Accept the burger merge keeping Reston with an edited name; accept taco as suggested; dismiss the donut rename.
    body = {
        "items": [
            {"id": burger["id"], "target_key": "mrv burger reston", "display_name": "MRV Burgers"},
            {"id": taco["id"]},
        ]
    }
    r = (await client.post("/api/merchants/review/accept", json=body)).json()
    assert r == {"accepted": 2, "moved": 3, "errors": []}
    d = (await client.get("/api/merchants/detail", params={"key": "mrv burger fairfax"})).json()
    assert (d["key"], d["name"], d["stats"]["count"]) == ("mrv burger reston", "MRV Burgers", 3)
    d = (await client.get("/api/merchants/detail", params={"key": "mrv tacohut catering"})).json()
    assert (d["key"], d["name"]) == ("mrv taco hut", "MRV Taco Hut")

    donut = mine["mrv donut den"]["id"]
    assert (await client.post("/api/merchants/review/dismiss", json={"ids": [donut]})).json() == {"dismissed": 1}

    state = (await client.get("/api/merchants/review")).json()
    left = {s["target"]["key"] for s in state["suggestions"]}
    assert left & set(NAMES) == {"mrv dangerouslytastypies"}

    # Reviewed, dismissed and curated keys are skipped next time; the pending rename is replaced.
    keys = {c["key"] for c in await _candidates()}
    assert keys & set(NAMES) == {"mrv dangerouslytastypies"}


async def _candidates():
    from ledger.db.engine import get_sessionmaker

    async with get_sessionmaker()() as session:
        return await merchant_review.candidates(session)

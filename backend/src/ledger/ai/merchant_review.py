"""AI merchant review: Gemini names every not-yet-curated merchant key; keys given the same name become merge
suggestions, and a single key whose name differs from its automatic one becomes a rename suggestion."""

import asyncio
import re
import unicodedata
from collections import defaultdict

from pydantic import BaseModel
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ledger.ai.client import generate
from ledger.db.engine import get_sessionmaker
from ledger.jobs.worker import JobCancelled, JobContext, job_handler
from ledger.models import MerchantSuggestion
from ledger.services.merchants import default_name, mark_reviewed

CHUNK = 150
PARALLEL = 4

SYSTEM = """You clean up merchant names in a household's bank and credit card history.
Each row is a merchant key derived automatically from bank descriptions. For every row, return the name a person would use
for that business or payee.
- Use the brand's usual spelling and capitalization: McDonald's, Trader Joe's, USPS, Amazon, 7-Eleven.
- Drop store numbers, cities, states, branch/location words, processor or card prefixes (SQ *, TST*, PAYPAL *, CHECKCARD,
  POS, DEBIT), domains (.com) and corporate suffixes (Inc, LLC) unless they are part of the brand.
- A payment processor in front of a merchant: name the merchant (PAYPAL *NETFLIX -> Netflix).
- All rows for the same business must get exactly the same name, including different locations of a chain. When a row is
  one of the existing merchants listed, reuse that exact name.
- Different businesses must get different names. For small local businesses with generic names keep the distinguishing
  word from the key.
- Non-merchant rows get a short descriptive name that keeps who/where: "<Employer> Payroll", "Interest",
  "Transfer to Savings", "Zelle to <Person>", "<Issuer> Card Payment", "ATM Withdrawal", "Check".
- If you can't tell what it is, return the key in Title Case without the noise.
Return one item per row, with the row's i."""


class Named(BaseModel):
    i: int
    name: str


class Names(BaseModel):
    items: list[Named]


def _words(name: str | None) -> str:
    s = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode().lower()
    return " ".join(re.sub(r"[^a-z0-9&]+", " ", re.sub(r"['\u2019]", "", s)).split())


def same_words(a: str | None, b: str | None) -> bool:
    """Names differ only in capitalization / punctuation ('Mcdonalds' vs "McDonald's")."""
    return _words(a) == _words(b)


def group_norm(name: str | None) -> str:
    """Comparison form of a merchant name: 'The McDonald's, Inc.' == 'mcdonalds'."""
    s = re.sub(r"^the ", "", _words(name))
    s = re.sub(r" (inc|llc|co|corp|ltd)$", "", s)
    return s.replace(" ", "")


async def candidates(session: AsyncSession, include_reviewed: bool = False) -> list[dict]:
    """Canonical merchant keys nobody has curated: no display name, nothing merged in, (by default) not reviewed."""
    sql = text(
        """--sql
        SELECT t.merchant AS key, count(*) AS n,
               (array_agg(t.description ORDER BY t.txn_date DESC, t.id DESC))[1] AS sample,
               mode() WITHIN GROUP (ORDER BY c.name) AS category
        FROM "transaction" t
        LEFT JOIN category c ON c.id = t.category_id
        LEFT JOIN merchant_profile mp ON mp.key = t.merchant
        WHERE t.deleted_at IS NULL AND t.merchant IS NOT NULL AND mp.display_name IS NULL
          AND (CAST(:all AS boolean) OR mp.reviewed_at IS NULL)
          AND NOT EXISTS (SELECT 1 FROM merchant_profile a WHERE a.alias_of = t.merchant)
        GROUP BY t.merchant
        ORDER BY t.merchant
        """
    )
    return [dict(r) for r in (await session.execute(sql, {"all": include_reviewed})).mappings()]


async def curated(session: AsyncSession) -> dict[str, str | None]:
    """Merchants the user already cleaned up (renamed, or with others merged in): key -> display name."""
    sql = text(
        """--sql
        SELECT key, display_name FROM merchant_profile WHERE alias_of IS NULL AND display_name IS NOT NULL
        UNION
        SELECT a.alias_of, p.display_name FROM merchant_profile a LEFT JOIN merchant_profile p ON p.key = a.alias_of
        WHERE a.alias_of IS NOT NULL
        """
    )
    return dict((await session.execute(sql)).all())


async def lock_review(session: AsyncSession) -> None:
    """Serialize writers of merchant suggestions (accept/dismiss requests, the review job) until commit."""
    await session.execute(text("SELECT pg_advisory_xact_lock(hashtext('merchant_review'))"))


async def pending_keys(session: AsyncSession) -> set[str]:
    rows = await session.execute(
        select(MerchantSuggestion.target_key, MerchantSuggestion.source_keys).where(MerchantSuggestion.status == "pending")
    )
    return {k for target, sources in rows for k in (target, *sources)}


def build_suggestions(cands: list[dict], names: dict[str, str], known: dict[str, str | None]) -> list[dict]:
    known_by_norm: dict[str, str] = {}
    for key, display in sorted(known.items()):
        known_by_norm.setdefault(group_norm(display or default_name(key)), key)
    groups: dict[str, list[dict]] = defaultdict(list)
    for c in cands:
        if c["key"] in names:
            groups[group_norm(names[c["key"]])].append(c)
    out = []
    for norm, members in groups.items():
        if not norm:
            continue
        members.sort(key=lambda c: (-c["n"], c["key"]))
        name = names[members[0]["key"]]
        keys = [c["key"] for c in members]
        if existing := known_by_norm.get(norm):
            out.append(
                {
                    "kind": "merge",
                    "target_key": existing,
                    "source_keys": keys,
                    "display_name": known[existing] or name,
                    "reason": f"Identified as “{name}”, which you already cleaned up",
                }
            )
        elif len(keys) > 1:
            out.append(
                {
                    "kind": "merge",
                    "target_key": keys[0],
                    "source_keys": keys[1:],
                    "display_name": name,
                    "reason": f"{len(keys)} merchants identified as “{name}”",
                }
            )
        elif name != default_name(keys[0]):
            out.append({"kind": "rename", "target_key": keys[0], "source_keys": [], "display_name": name, "reason": None})
    return out


@job_handler("merchant_review")
async def merchant_review_job(ctx: JobContext) -> dict:
    sm = get_sessionmaker()
    include_reviewed = bool(ctx.payload.get("all"))
    async with sm() as session:
        if include_reviewed:
            # A full re-review supersedes undecided suggestions; otherwise they stay for the user to finish.
            await lock_review(session)
            await session.execute(delete(MerchantSuggestion).where(MerchantSuggestion.status == "pending"))
            await session.commit()
        waiting = {
            s.target_key: s.display_name
            for s in await session.scalars(select(MerchantSuggestion).where(MerchantSuggestion.status == "pending"))
        }
        skip = await pending_keys(session)
        cands = [c for c in await candidates(session, include_reviewed) if c["key"] not in skip]
        known = await curated(session)
    if not cands:
        return {"considered": 0, "named": 0, "merges": 0, "renames": 0, "failed_chunks": 0}

    known_list = "\n".join(sorted({d or default_name(k) for k, d in (waiting | known).items()})) or "(none yet)"
    chunks = [cands[i : i + CHUNK] for i in range(0, len(cands), CHUNK)]
    names: dict[str, str] = {}
    slots = asyncio.Semaphore(PARALLEL)
    done = 0
    cancelled = False

    async def run(chunk: list[dict]) -> None:
        nonlocal done, cancelled
        async with slots:
            if cancelled:
                return
            rows = "\n".join(
                f"{i} | {c['key']} | {c['n']} | {' '.join((c['sample'] or '').split())} | {c['category'] or '-'}"
                for i, c in enumerate(chunk)
            )
            prompt = (
                f"Existing merchants:\n{known_list}\n\n"
                f"Rows (i | merchant key | transactions | latest bank description | usual category):\n{rows}"
            )
            result: Names = await generate(
                prompt, purpose="merchant_review", tier="main", system=SYSTEM, schema=Names, temperature=0
            )
            for item in result.items:
                name = " ".join(item.name.split())[:200]
                if 0 <= item.i < len(chunk) and name:
                    names[chunk[item.i]["key"]] = name
            done += 1
            try:
                await ctx.progress(0.95 * done / len(chunks), f"Named {len(names):,} of {len(cands):,} merchants")
            except JobCancelled:
                cancelled = True
                raise

    outcomes = await asyncio.gather(*(run(c) for c in chunks), return_exceptions=True)
    errors = [e for e in outcomes if isinstance(e, BaseException)]
    if cancelled := next((e for e in errors if isinstance(e, JobCancelled)), None):
        raise cancelled
    if errors and not names:
        raise errors[0]

    suggestions = build_suggestions(cands, names, waiting | known)
    involved = {k for s in suggestions for k in (s["target_key"], *s["source_keys"])}
    added = 0
    async with sm() as session:
        await lock_review(session)
        open_by_target = {
            s.target_key: s
            for s in await session.scalars(select(MerchantSuggestion).where(MerchantSuggestion.status == "pending"))
        }
        for s in suggestions:
            if (p := open_by_target.get(s["target_key"])) is None:
                session.add(MerchantSuggestion(job_id=ctx.job_id, **s))
                added += 1
                continue
            # New keys for a business that already has a waiting suggestion join it.
            p.source_keys = [*p.source_keys, *s["source_keys"]]
            if p.target_key not in known:
                p.reason = f"{len(p.source_keys) + 1} merchants identified as “{p.display_name}”"
            p.kind = "merge"
        # Keys that came back fine as they are count as reviewed; unanswered keys are retried next run.
        await mark_reviewed(session, [k for k in names if k not in involved])
        await session.commit()
    return {
        "considered": len(cands),
        "named": len(names),
        "added": added,
        "extended": len(suggestions) - added,
        "merges": sum(s["kind"] == "merge" for s in suggestions),
        "renames": sum(s["kind"] == "rename" for s in suggestions),
        "failed_chunks": len(errors),
    }

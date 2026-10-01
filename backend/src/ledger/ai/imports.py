"""Gemini helpers for imports: column mapping, statement extraction, duplicate adjudication."""

import logging
from decimal import Decimal

from google.genai import types
from pydantic import BaseModel, Field

from ledger.ai.client import file_part, generate, get_client
from ledger.imports.parsing import FIELDS

log = logging.getLogger(__name__)
INLINE_LIMIT = 18 * 1024 * 1024


# ---- column mapping ----
class ColumnChoice(BaseModel):
    column: str
    field: str = Field(description=f"One of: {', '.join(FIELDS)}")


class MappingSuggestion(BaseModel):
    columns: list[ColumnChoice]
    invert_sign: bool = Field(description="True if expenses appear as positive numbers in a single amount column")


MAPPING_SYSTEM = f"""You map columns of a bank/credit-card transaction export to a ledger schema.
Fields: {", ".join(FIELDS)}.
- txn_date: transaction date; posted_date: posting date if separate.
- description: the payee/merchant text; original_description: a longer raw description if a cleaner one exists.
- amount: a single signed amount column. Use debit/credit when money out and money in are separate columns.
- Map every column exactly once; use ignore for balances, running totals, types, and anything else."""


async def suggest_mapping(profile: list[dict]) -> MappingSuggestion:
    lines = "\n".join(f"- {c['name']!r}: samples {c['samples']}" for c in profile)
    return await generate(
        f"Columns:\n{lines}",
        purpose="import_mapping",
        tier="lite",
        system=MAPPING_SYSTEM,
        schema=MappingSuggestion,
        temperature=0,
    )


# ---- document extraction ----
class ExtractedTxn(BaseModel):
    date: str = Field(description="Transaction date YYYY-MM-DD (infer the year from the statement period)")
    posted_date: str | None = Field(default=None, description="Posting date YYYY-MM-DD if shown")
    description: str
    amount: float = Field(description="Signed from the account holder's view: money out negative, money in positive")
    balance: float | None = None
    confidence: float = Field(ge=0, le=1, description="Legibility/extraction confidence for this row")


class ExtractedStatement(BaseModel):
    document_type: str = Field(description="bank_statement, credit_card_statement, loan_statement, receipt, bill, other")
    institution: str | None = None
    account_name: str | None = None
    account_last4: str | None = None
    account_type: str | None = Field(default=None, description="checking, savings, credit_card, loan, investment, other")
    period_start: str | None = Field(default=None, description="YYYY-MM-DD")
    period_end: str | None = Field(default=None, description="YYYY-MM-DD")
    opening_balance: float | None = None
    closing_balance: float | None = None
    sign_note: str = Field(description="How the source shows charges vs credits and how you converted them")
    summary: str = Field(description="One sentence describing the document")
    transactions: list[ExtractedTxn]


EXTRACT_SYSTEM = """You extract every transaction line from a financial statement or receipt.
Rules:
- Include every posted transaction exactly once; skip running balances, subtotals, summaries, and marketing text.
- Output amounts signed from the account holder's perspective: purchases, fees, withdrawals, payments sent = negative;
  deposits, refunds, credits, payments received = positive. For credit card statements, purchases are negative
  and payments to the card are positive.
- Dates as YYYY-MM-DD; when the statement omits the year, infer it from the statement period (watch Dec/Jan spans).
- Keep the description text as printed (merchant + location), without the amount.
- confidence < 0.8 when a value is smudged, cut off, or ambiguous."""


async def document_part(data: bytes, mime_type: str):
    if len(data) <= INLINE_LIMIT:
        return file_part(data, mime_type)
    import io

    uploaded = await get_client().aio.files.upload(file=io.BytesIO(data), config=types.UploadFileConfig(mime_type=mime_type))
    return uploaded


async def extract_statement(data: bytes, mime_type: str, filename: str) -> ExtractedStatement:
    part = await document_part(data, mime_type)
    return await generate(
        [part, f"File name: {filename}. Extract the statement header and all transactions."],
        purpose="extract_statement",
        tier="main",
        system=EXTRACT_SYSTEM,
        schema=ExtractedStatement,
        temperature=0,
    )


# ---- external category labels ----
class LabelMatch(BaseModel):
    label: str
    category_id: int | None = Field(description="Best matching category id, or null if none fits")
    confidence: float = Field(ge=0, le=1)


class LabelMatches(BaseModel):
    items: list[LabelMatch]


LABEL_SYSTEM = """Map category labels used by a bank or another budgeting tool onto the household's own category
taxonomy. Only map when the meaning clearly corresponds to exactly one category; broad labels that could be several
categories (e.g. "Bills & Utilities" when Electric, Water and Internet exist separately) get confidence below 0.6."""


async def map_category_labels(labels: list[str], taxonomy: str) -> list[LabelMatch]:
    listing = "\n".join(f"- {label}" for label in labels)
    result: LabelMatches = await generate(
        f"Taxonomy (id: group > category):\n{taxonomy}\n\nLabels:\n{listing}",
        purpose="category_alias",
        tier="lite",
        system=LABEL_SYSTEM,
        schema=LabelMatches,
        temperature=0,
    )
    return result.items


# ---- duplicate adjudication ----
class PairVerdict(BaseModel):
    pair_id: int
    probability_same: float = Field(ge=0, le=1)
    reason: str


class PairVerdicts(BaseModel):
    items: list[PairVerdict]


DUP_SYSTEM = """You decide whether two bank transactions are the same real-world transaction recorded twice
(e.g. the same charge from two feeds, a pending vs posted copy, or a re-imported statement) or two genuinely separate
transactions (e.g. repeat purchases at the same store, recurring bills). Consider date gaps (transaction vs posted
dates differ by 1-3 days), description variants of the same merchant, and whether the accounts differ.
reason: one short sentence a person can act on."""


def _side(d: dict) -> str:
    return f"{d['date']} | {Decimal(str(d['amount'])):.2f} | {d['description']} | account: {d.get('account') or 'unknown'}"


async def adjudicate_pairs(pairs: list[dict]) -> dict[int, PairVerdict]:
    """pairs: [{id, a: {date, amount, description, account}, b: {...}}] -> verdict per pair id."""
    lines = "\n".join(f"pair {p['id']}:\n  A: {_side(p['a'])}\n  B: {_side(p['b'])}" for p in pairs)
    result: PairVerdicts = await generate(
        lines, purpose="duplicate_adjudication", tier="lite", system=DUP_SYSTEM, schema=PairVerdicts, temperature=0
    )
    return {v.pair_id: v for v in result.items}

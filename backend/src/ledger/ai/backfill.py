"""Gemini classification of archive documents so the backfill can route them to imports or bills."""

from typing import Literal

from pydantic import BaseModel, Field

from ledger.ai.client import generate
from ledger.ai.imports import document_part

DocumentType = Literal[
    "bank_statement",
    "credit_card_statement",
    "loan_statement",
    "investment_statement",
    "utility_bill",
    "other_bill",
    "receipt",
    "tax_document",
    "annual_summary",
    "other",
]


class DocClass(BaseModel):
    document_type: DocumentType
    institution: str | None = Field(default=None, description="Bank, card issuer or biller name")
    account_last4: str | None = None
    period_start: str | None = Field(default=None, description="YYYY-MM-DD")
    period_end: str | None = Field(default=None, description="YYYY-MM-DD")
    confidence: float = Field(ge=0, le=1)
    reason: str = Field(description="A few words on what identified the document")


SYSTEM = """Classify a household financial document from an archive.
- bank_statement: checking/savings statement listing transactions. credit_card_statement: card statement listing
  purchases. loan_statement / investment_statement: mortgage, auto, student loan, brokerage, retirement.
- utility_bill: electric, gas, water, sewer, trash, internet, phone. other_bill: insurance, subscriptions, medical,
  HOA and other invoices that are paid from an account.
- receipt: a single purchase receipt. tax_document: W-2, 1099, 1098, tax returns.
- annual_summary: year-end / annual account summaries that recap a year of activity (not a periodic statement).
- other: anything else (credit reports, letters, notices, agreements).
Use the file name and folder only as hints; the content decides. confidence < 0.6 if the scan is unreadable."""


async def classify_document(data: bytes, mime_type: str, filename: str, folder: str) -> DocClass:
    part = await document_part(data, mime_type)
    return await generate(
        [part, f"File: {folder + '/' if folder else ''}{filename}"],
        purpose="backfill_classify",
        tier="lite",
        system=SYSTEM,
        schema=DocClass,
        temperature=0,
    )

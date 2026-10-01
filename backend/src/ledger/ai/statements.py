"""Gemini extraction of bills/statements: vendor, period, amount and usage metrics."""

from pydantic import BaseModel, Field

from ledger.ai.client import generate
from ledger.ai.imports import document_part


class UsageMetric(BaseModel):
    metric: str = Field(description="snake_case name, e.g. water_usage, electricity_usage, gas_usage, data_usage")
    value: float
    unit: str = Field(description="Unit as printed, normalized: gal, kWh, therms, ccf, GB, Mbps, miles, ...")
    is_primary: bool = Field(description="True for the single metric that best tracks consumption for this bill")


class BillExtraction(BaseModel):
    document_type: str = Field(
        description="utility_bill, insurance, subscription, telecom, medical, tax, loan, receipt, bank_statement, other"
    )
    vendor: str | None = Field(default=None, description="Company that issued the bill")
    service_type: str | None = Field(
        default=None,
        description="Short service label: Water, Electric, Natural gas, Internet, Mobile, Trash, "
        "Auto insurance, Home insurance, Property tax, ... (null if not a recurring service)",
    )
    account_last4: str | None = Field(default=None, description="Last 4 characters of the customer account number")
    statement_date: str | None = Field(default=None, description="YYYY-MM-DD")
    period_start: str | None = Field(default=None, description="Service period start YYYY-MM-DD")
    period_end: str | None = Field(default=None, description="Service period end YYYY-MM-DD")
    due_date: str | None = Field(default=None, description="YYYY-MM-DD")
    amount_due: float | None = Field(default=None, description="Total amount due / paid for this statement, positive")
    usage: list[UsageMetric] = Field(default_factory=list)
    summary: str = Field(description="One sentence a person would use to recognize this document")


SYSTEM = """You read household bills and statements (utilities, insurance, subscriptions, telecom, taxes, receipts).
Extract the header facts and every consumption/usage figure for the billing period (not year-to-date or historical
graph values). Dates as YYYY-MM-DD. amount_due is the current charges total the household pays for this statement
(use the new charges if a previous balance was already paid). Mark exactly one usage metric as primary when any exist."""


async def extract_bill(data: bytes, mime_type: str, filename: str) -> BillExtraction:
    part = await document_part(data, mime_type)
    return await generate(
        [part, f"File name: {filename}."],
        purpose="extract_bill",
        tier="main",
        system=SYSTEM,
        schema=BillExtraction,
        temperature=0,
    )

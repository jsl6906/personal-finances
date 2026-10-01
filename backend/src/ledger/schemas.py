from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

CategoryType = Literal["expense", "income", "transfer"]
AccountType = Literal[
    "checking",
    "savings",
    "credit_card",
    "loan",
    "mortgage",
    "investment",
    "retirement",
    "cash",
    "property",
    "vehicle",
    "other",
]


class ORM(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class InstitutionIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    website: str | None = None
    notes: str | None = None


class InstitutionOut(InstitutionIn, ORM):
    id: int


class AccountIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    institution_id: int | None = None
    account_type: AccountType = "checking"
    mask: str | None = Field(default=None, max_length=10)
    is_hidden: bool = False
    is_closed: bool = False
    notes: str | None = None


class AccountOut(AccountIn, ORM):
    id: int
    institution_name: str | None = None


class MemberIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    initials: str = Field(min_length=1, max_length=4)
    email: str | None = None


class MemberOut(MemberIn, ORM):
    id: int


class CategoryGroupIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    type: CategoryType = "expense"
    sort_order: int = 0
    hide_from_reports: bool = False


class CategoryGroupOut(CategoryGroupIn, ORM):
    id: int


class CategoryIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    group_id: int
    type: CategoryType = "expense"
    hide_from_reports: bool = False
    is_active: bool = True
    description: str | None = None


class CategoryOut(CategoryIn, ORM):
    id: int
    group_name: str


class TagIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    color: str | None = None


class TagOut(TagIn, ORM):
    id: int


class TransactionBase(BaseModel):
    txn_date: date
    posted_date: date | None = None
    description: str = Field(min_length=1)
    amount: Decimal = Field(max_digits=14, decimal_places=2)
    account_id: int | None = None
    category_id: int | None = None
    member_id: int | None = None
    notes: str | None = None
    check_number: str | None = None
    budget_spread_months: int | None = Field(default=None, ge=1, le=60)


class TransactionCreate(TransactionBase):
    tag_ids: list[int] = []
    merchant_name: str | None = Field(default=None, max_length=200)


class TransactionUpdate(BaseModel):
    txn_date: date | None = None
    posted_date: date | None = None
    description: str | None = Field(default=None, min_length=1)
    amount: Decimal | None = Field(default=None, max_digits=14, decimal_places=2)
    account_id: int | None = None
    category_id: int | None = None
    member_id: int | None = None
    notes: str | None = None
    check_number: str | None = None
    budget_spread_months: int | None = Field(default=None, ge=1, le=60)
    tag_ids: list[int] | None = None
    # Blank resets the merchant to the automatic one derived from the description.
    merchant_name: str | None = Field(default=None, max_length=200)


class TransactionOut(TransactionBase):
    id: int
    original_description: str | None
    merchant: str | None
    merchant_name: str | None = None
    merchant_source: str | None = None
    account_name: str | None
    institution_name: str | None
    category_name: str | None
    category_group_id: int | None = None
    category_group_name: str | None
    category_type: str | None
    category_source: str | None
    category_rule_id: int | None = None
    category_rule: str | None = None
    suggested_category_id: int | None
    suggested_category_name: str | None
    suggestion_confidence: Decimal | None
    suggestion_reason: str | None
    member_initials: str | None
    source_type: str
    external_id: str | None
    import_batch_id: int | None
    transfer_match_id: int | None = None
    has_statement: bool = False
    tags: list[TagOut]
    created_at: datetime
    updated_at: datetime


class TransactionPage(BaseModel):
    items: list[TransactionOut]
    total: int
    total_in: Decimal
    total_out: Decimal


class TxnSourceOut(BaseModel):
    id: int
    role: str
    origin: str | None
    source_type: str | None
    import_batch_id: int | None
    attachment_id: int | None
    filename: str | None
    mime_type: str | None
    txn_date: date | None
    description: str | None
    amount: Decimal | None
    match_score: Decimal | None
    created_at: datetime


class NoteIn(BaseModel):
    body: str = Field(min_length=1, max_length=5000)


class TxnNoteOut(ORM):
    id: int
    body: str
    source: str
    import_batch_id: int | None
    attachment_id: int | None
    filename: str | None = None
    created_at: datetime
    updated_at: datetime


class BulkUpdate(BaseModel):
    ids: list[int] = Field(min_length=1, max_length=5000)
    category_id: int | None = None
    member_id: int | None = None
    account_id: int | None = None
    add_tag_ids: list[int] = []
    remove_tag_ids: list[int] = []
    delete: bool = False


class IdList(BaseModel):
    ids: list[int] = Field(min_length=1, max_length=5000)


class SuggestRequest(BaseModel):
    ids: list[int] | None = None


class JobOut(ORM):
    id: int
    type: str
    status: str
    payload: dict
    result: dict | None
    progress: Decimal
    message: str | None
    error: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class LoginIn(BaseModel):
    password: str = Field(min_length=1, max_length=500)


# ---- imports ----
ImportField = Literal[
    "txn_date",
    "posted_date",
    "description",
    "original_description",
    "amount",
    "debit",
    "credit",
    "category",
    "account",
    "notes",
    "check_number",
    "external_id",
    "ignore",
]


class ImportOptions(BaseModel):
    date_format: str = "auto"
    dayfirst: bool = False
    invert_sign: bool = False


class ImportDefaults(BaseModel):
    account_id: int | None = None
    account_map: dict[str, int | None] = {}
    category_id: int | None = None
    member_id: int | None = None
    notes: str | None = None
    tag_ids: list[int] = []


class PrepareIn(BaseModel):
    mapping: dict[str, ImportField]
    options: ImportOptions = ImportOptions()
    defaults: ImportDefaults = ImportDefaults()
    save_template: bool = False
    template_name: str | None = Field(default=None, max_length=120)


class BatchSummary(ORM):
    id: int
    filename: str | None
    source_type: str
    origin: str
    status: str
    row_count: int
    stats: dict
    error: str | None
    job_id: int | None
    created_at: datetime
    committed_at: datetime | None


class BatchSource(BaseModel):
    mime_type: str | None = None
    size_bytes: int | None = None
    sha256: str | None = None
    uploaded_at: datetime | None = None
    archive_provider: str | None = None
    archive_path: str | None = None
    archive_kind: str | None = None
    archive_modified_at: datetime | None = None


class BatchDetail(BatchSummary):
    source: BatchSource = BatchSource()
    attachment_id: int | None
    sheet_name: str | None
    sheets: list
    columns: list
    mapping: dict
    mapping_source: str | None
    options: dict
    defaults: dict
    doc_meta: dict | None
    template_name: str | None
    decisions: dict[str, int]
    preview: list[dict]


class ImportRowOut(ORM):
    id: int
    row_index: int
    raw: dict
    txn_date: date | None
    posted_date: date | None
    description: str | None
    amount: Decimal | None
    account_id: int | None
    account_name: str | None = None
    category_id: int | None
    category_name: str | None = None
    account_hint: str | None
    category_hint: str | None
    notes: str | None
    confidence: Decimal | None
    errors: list
    decision: str
    transaction_id: int | None


class TxnBrief(BaseModel):
    id: int
    txn_date: date
    description: str
    amount: Decimal
    account_name: str | None
    category_name: str | None
    notes: str | None
    source_type: str
    created_at: datetime


class ImportPairOut(BaseModel):
    id: int
    status: str
    score: Decimal
    reasons: list
    ai_probability: Decimal | None
    ai_reason: str | None
    row: ImportRowOut
    existing: TxnBrief


class DecisionIn(BaseModel):
    decision: Literal["skip_duplicate", "keep", "insert", "pending"]


class CommitIn(BaseModel):
    pending_as: Literal["skip", "keep"] = "skip"


class SheetIn(BaseModel):
    sheet: str


class TxnPairOut(BaseModel):
    id: int
    status: str
    score: Decimal
    reasons: list
    ai_probability: Decimal | None
    ai_reason: str | None
    a: TxnBrief
    b: TxnBrief


class PairDecisionIn(BaseModel):
    decision: Literal["duplicate", "separate"]
    keep_id: int | None = None


class ScanIn(BaseModel):
    since: date | None = None


# ---- statements ----
class UsageIn(BaseModel):
    metric: str = Field(min_length=1, max_length=60)
    value: Decimal
    unit: str | None = Field(default=None, max_length=30)
    is_primary: bool = False


class UsageOut(UsageIn, ORM):
    id: int


class StatementOut(BaseModel):
    id: int
    attachment_id: int
    filename: str
    series_id: int | None
    series_name: str | None
    status: str
    document_type: str | None
    vendor: str | None
    account_ref: str | None
    statement_date: date | None
    period_start: date | None
    period_end: date | None
    due_date: date | None
    amount_due: Decimal | None
    summary: str | None
    suggestion: dict
    error: str | None
    job_id: int | None
    usage: list[UsageOut]
    transactions: list[TxnBrief]
    created_at: datetime
    approved_at: datetime | None


class StatementApprove(BaseModel):
    series_id: int | None = None
    new_series_name: str | None = Field(default=None, max_length=120)
    new_series_category_id: int | None = None
    transaction_ids: list[int] = []
    vendor: str | None = None
    statement_date: date | None = None
    period_start: date | None = None
    period_end: date | None = None
    due_date: date | None = None
    amount_due: Decimal | None = None
    usage: list[UsageIn] | None = None


class SeriesIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    vendor: str | None = None
    service_type: str | None = None
    category_id: int | None = None
    primary_metric: str | None = None
    unit: str | None = None
    notes: str | None = None
    is_active: bool = True


class SeriesOut(SeriesIn, ORM):
    id: int
    tag_id: int | None
    category_name: str | None = None
    statement_count: int = 0
    latest_period_end: date | None = None


class SeriesPoint(BaseModel):
    statement_id: int
    filename: str
    attachment_id: int
    period_start: date | None
    period_end: date | None
    statement_date: date | None
    amount_due: Decimal | None
    usage_value: Decimal | None
    usage_unit: str | None
    cost_per_unit: Decimal | None
    transactions: list[TxnBrief]


# ---- chat ----
class ChatSessionOut(ORM):
    id: int
    title: str
    created_at: datetime
    updated_at: datetime


class ChatSessionIn(BaseModel):
    title: str | None = Field(None, max_length=200)


class ChatMessageOut(BaseModel):
    id: int
    session_id: int
    role: str
    content: str
    attachment_id: int | None
    attachment_name: str | None = None
    attachment_kind: str | None = None
    queries: list
    created_at: datetime


# ---- alerts ----
class AlertRuleOut(ORM):
    kind: str
    enabled: bool
    params: dict
    updated_at: datetime


class AlertRuleIn(BaseModel):
    enabled: bool | None = None
    pace: bool | None = None
    threshold: Decimal | None = Field(None, gt=0, le=1_000_000)


class RecipientIn(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    name: str | None = Field(None, max_length=100)
    enabled: bool = True


class RecipientOut(RecipientIn, ORM):
    id: int
    created_at: datetime


class AlertEventOut(ORM):
    id: int
    kind: str
    title: str
    body: str
    link: str | None
    status: str
    recipients: list
    error: str | None
    created_at: datetime
    sent_at: datetime | None

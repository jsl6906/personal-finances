"""Import pipeline: upload -> (extract) -> map -> prepare (+ duplicate detection) -> review -> commit/rollback."""

import hashlib
import logging
from collections import defaultdict
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from ledger.config import get_settings
from ledger.db.filters import id_in
from ledger.imports.parsing import (
    column_profile,
    detect_kind,
    header_signature,
    heuristic_mapping,
    mapping_complete,
    mime_for,
    parse_amount,
    parse_date,
    read_table,
)
from ledger.models import (
    Account,
    Attachment,
    Category,
    CategoryAlias,
    DuplicatePair,
    ImportBatch,
    ImportMappingTemplate,
    ImportRow,
    Institution,
    StatementCheck,
    Tag,
    Transaction,
    TransactionNote,
    TransactionSource,
)
from ledger.services.categorize import apply_rules
from ledger.services.dedupe import other_description
from ledger.services.merchants import alias_map, canonical
from ledger.services.normalize import fingerprint, normalize_merchant

log = logging.getLogger(__name__)

DOC_COLUMNS = ["Date", "Posted", "Description", "Details", "Amount", "Account", "Balance", "Confidence"]
DOC_MAPPING = {
    "Date": "txn_date",
    "Posted": "posted_date",
    "Description": "description",
    "Details": "notes",
    "Amount": "amount",
    "Account": "account",
    "Balance": "ignore",
    "Confidence": "ignore",
}


class ImportError_(ValueError):
    """User-facing import problem (bad file, nothing to import)."""


async def store_attachment(
    session: AsyncSession, data: bytes, filename: str, content_type: str | None, source: str = "upload"
) -> Attachment:
    sha = hashlib.sha256(data).hexdigest()
    existing = await session.scalar(select(Attachment).where(Attachment.sha256 == sha))
    if existing:
        return existing
    att = Attachment(
        filename=filename[:300],
        mime_type=mime_for(filename, content_type),
        size_bytes=len(data),
        sha256=sha,
        content=data,
        source=source,
    )
    session.add(att)
    await session.flush()
    return att


async def create_batch(
    session: AsyncSession,
    data: bytes,
    filename: str,
    content_type: str | None,
    sheet: str | None = None,
    origin: str = "upload",
) -> ImportBatch:
    from ledger.jobs.worker import enqueue

    kind = detect_kind(filename)
    if kind is None:
        raise ImportError_("Unsupported file type; upload a CSV/Excel sheet or a PDF/image statement")
    if len(data) > get_settings().max_upload_mb * 1024 * 1024:
        raise ImportError_(f"File is larger than {get_settings().max_upload_mb} MB")
    att = await store_attachment(session, data, filename, content_type, source=origin)
    batch = ImportBatch(attachment_id=att.id, source_type=kind, origin=origin)
    session.add(batch)
    await session.flush()
    if kind == "spreadsheet":
        await load_spreadsheet(session, batch, data, filename, sheet)
    else:
        batch.status = "extracting"
        job = await enqueue(session, "extract_document", {"batch_id": batch.id})
        batch.job_id = job.id
    return batch


async def _insert_rows(session: AsyncSession, batch: ImportBatch, rows: list[dict]) -> None:
    await session.execute(
        delete(DuplicatePair).where(
            DuplicatePair.import_row_id.in_(select(ImportRow.id).where(ImportRow.batch_id == batch.id))
        )
    )
    await session.execute(delete(ImportRow).where(ImportRow.batch_id == batch.id))
    values = [{"batch_id": batch.id, "row_index": i, "raw": r} for i, r in enumerate(rows)]
    for start in range(0, len(values), 1000):
        await session.execute(insert(ImportRow).values(values[start : start + 1000]))
    batch.row_count = len(rows)


async def load_spreadsheet(session: AsyncSession, batch: ImportBatch, data: bytes, filename: str, sheet: str | None) -> None:
    table = read_table(data, filename, sheet)
    if not table.headers or not table.rows:
        raise ImportError_("No header row or data rows were found in this sheet")
    batch.sheets, batch.sheet_name = table.sheets, table.sheet
    batch.columns = column_profile(table.headers, table.rows)
    batch.header_signature = header_signature(table.headers)
    await _insert_rows(session, batch, table.rows)

    template = await session.scalar(
        select(ImportMappingTemplate).where(ImportMappingTemplate.header_signature == batch.header_signature)
    )
    if template:
        batch.template_id = template.id
        batch.mapping = {h: template.mapping.get(h, "ignore") for h in table.headers}
        batch.options, batch.defaults, batch.mapping_source = template.options, template.defaults, "template"
    else:
        batch.mapping, batch.mapping_source = heuristic_mapping(table.headers), "heuristic"
        batch.options = {"date_format": "auto", "dayfirst": False, "invert_sign": False}
        if not mapping_complete(batch.mapping) and get_settings().gemini_key:
            try:
                from ledger.ai.imports import suggest_mapping

                suggestion = await suggest_mapping(batch.columns)
                ai_map = {c.column: c.field for c in suggestion.columns if c.column in batch.mapping}
                batch.mapping = {h: ai_map.get(h, "ignore") for h in table.headers}
                batch.options["invert_sign"] = suggestion.invert_sign
                batch.mapping_source = "ai"
            except Exception:
                log.exception("AI column mapping failed; keeping heuristic mapping")
    batch.status = "mapping"


# ---------- prepare ----------
class _Refs:
    def __init__(self, accounts, categories, aliases):
        self.acct_by_name = {a.name.lower(): a.id for a in accounts}
        self.acct_by_mask = defaultdict(list)
        for a in accounts:
            if a.mask:
                self.acct_by_mask[a.mask[-4:]].append(a.id)
        self.cat_by_name = {c.name.lower(): c.id for c in categories}
        self.cat_by_alias = {a.alias: a.category_id for a in aliases}

    def account(self, value: str | None) -> int | None:
        if not value:
            return None
        v = value.strip().lower()
        if v in self.acct_by_name:
            return self.acct_by_name[v]
        digits = "".join(ch for ch in v if ch.isdigit())
        if len(digits) >= 4 and len(self.acct_by_mask.get(digits[-4:], [])) == 1:
            return self.acct_by_mask[digits[-4:]][0]
        return None

    def category(self, value: str | None) -> int | None:
        if not value:
            return None
        v = value.strip().lower()
        return self.cat_by_name.get(v) or self.cat_by_alias.get(v)


def _collect(raw: dict, mapping: dict[str, str]) -> dict[str, str]:
    vals: dict[str, list[str]] = defaultdict(list)
    for col, fld in mapping.items():
        if fld and fld != "ignore":
            v = str(raw.get(col, "") or "").strip()
            if v:
                vals[fld].append(v)
    return {k: " ".join(v) for k, v in vals.items()}


def parse_row(raw: dict, mapping: dict[str, str], options: dict, defaults: dict, refs: _Refs) -> dict:
    v = _collect(raw, mapping)
    errors: list[str] = []
    fmt, dayfirst = options.get("date_format", "auto"), bool(options.get("dayfirst"))
    txn_date = parse_date(v.get("txn_date"), fmt, dayfirst)
    posted = parse_date(v.get("posted_date"), fmt, dayfirst)
    if txn_date is None and posted is not None:
        txn_date = posted
    if txn_date is None:
        errors.append(f"Unreadable date {v.get('txn_date')!r}" if v.get("txn_date") else "Missing date")

    amount: Decimal | None = None
    if "amount" in v:
        amount = parse_amount(v["amount"])
        if amount is None:
            errors.append(f"Unreadable amount {v['amount']!r}")
    elif "debit" in v or "credit" in v:
        debit, credit = parse_amount(v.get("debit")), parse_amount(v.get("credit"))
        if debit is None and credit is None:
            errors.append("Missing debit/credit amount")
        else:
            amount = abs(credit or Decimal(0)) - abs(debit or Decimal(0))
    else:
        errors.append("Missing amount")
    if amount is not None and options.get("invert_sign"):
        amount = -amount

    description = v.get("description") or v.get("original_description")
    if not description:
        errors.append("Missing description")

    account_id = (
        (defaults.get("account_map") or {}).get(v.get("account"))
        or refs.account(v.get("account"))
        or defaults.get("account_id")
    )
    category_id = refs.category(v.get("category")) or defaults.get("category_id")
    notes = v.get("notes") or defaults.get("notes") or None
    return {
        "txn_date": txn_date,
        "posted_date": posted,
        "description": description,
        "merchant": normalize_merchant(description),
        "amount": amount,
        "account_id": account_id,
        "account_hint": (v.get("account") or None),
        "category_id": category_id,
        "category_hint": (v.get("category") or None),
        "notes": notes,
        "check_number": (v.get("check_number") or None),
        "external_id": (v.get("external_id") or None),
        "original_description": v.get("original_description"),
        "fingerprint": fingerprint(account_id, txn_date, amount, description) if txn_date and amount is not None else None,
        "errors": errors,
        "decision": "invalid" if errors else "pending",
    }


async def learn_category_aliases(session: AsyncSession, batch: ImportBatch, refs: _Refs) -> int:
    """Ask Gemini to map unknown category labels in the file onto our taxonomy; confident matches become aliases."""
    from ledger.ai.categorize import _taxonomy
    from ledger.ai.imports import map_category_labels

    cat_cols = [c for c, f in batch.mapping.items() if f == "category"]
    if not cat_cols or not get_settings().gemini_key:
        return 0
    raws = (await session.scalars(select(ImportRow.raw).where(ImportRow.batch_id == batch.id))).all()
    labels = sorted({str(r.get(c, "")).strip() for r in raws for c in cat_cols} - {""})
    unknown = [lbl for lbl in labels if refs.category(lbl) is None][:200]
    if not unknown:
        return 0
    taxonomy, valid_ids = await _taxonomy(session)
    try:
        matches = await map_category_labels(unknown, taxonomy)
    except Exception:
        log.exception("Category label mapping failed; continuing without aliases")
        return 0
    added = 0
    for m in matches:
        if m.category_id in valid_ids and m.confidence >= 0.75 and m.label in unknown:
            await session.execute(
                insert(CategoryAlias).values(alias=m.label.lower(), category_id=m.category_id).on_conflict_do_nothing()
            )
            refs.cat_by_alias[m.label.lower()] = m.category_id
            added += 1
    return added


async def prepare_batch(session: AsyncSession, batch: ImportBatch, progress=None) -> dict:
    from ledger.services.dedupe import adjudicate_pending, detect_import_duplicates

    refs = _Refs(
        (await session.scalars(select(Account))).unique().all(),
        (await session.scalars(select(Category))).unique().all(),
        (await session.scalars(select(CategoryAlias))).all(),
    )
    aliases = await learn_category_aliases(session, batch, refs)
    await session.execute(
        delete(DuplicatePair).where(
            DuplicatePair.import_row_id.in_(select(ImportRow.id).where(ImportRow.batch_id == batch.id))
        )
    )
    rows = (
        await session.scalars(select(ImportRow).where(ImportRow.batch_id == batch.id).order_by(ImportRow.row_index))
    ).all()
    merchant_aliases = await alias_map(session)
    for row in rows:
        parsed = parse_row(row.raw, batch.mapping, batch.options, batch.defaults, refs)
        parsed.pop("original_description")
        parsed["merchant"] = canonical(parsed["merchant"], merchant_aliases)
        for k, val in parsed.items():
            setattr(row, k, val)
        if row.raw.get("Confidence") not in (None, "") and batch.source_type == "document":
            row.confidence = Decimal(str(row.raw["Confidence"]))
    await session.flush()
    stats = await detect_import_duplicates(session, batch.id)
    stats["category_aliases_learned"] = aliases
    stats["invalid"] = sum(1 for r in rows if r.decision == "invalid")
    stats["no_account"] = sum(1 for r in rows if r.decision != "invalid" and r.account_id is None)
    batch.stats = {**batch.stats, **stats}
    await session.commit()
    if progress:
        await progress(0.5, "Checking possible duplicates")

    pending = (
        await session.scalars(
            select(DuplicatePair.id)
            .join(ImportRow, DuplicatePair.import_row_id == ImportRow.id)
            .where(ImportRow.batch_id == batch.id, DuplicatePair.status == "pending")
        )
    ).all()
    if pending and get_settings().gemini_key:
        stats["ai_reviewed"] = await adjudicate_pending(session, list(pending))
    batch.stats = {**batch.stats, **stats}
    batch.status = "review"
    await session.commit()
    return stats


async def save_template(session: AsyncSession, batch: ImportBatch, name: str) -> None:
    if not batch.header_signature:
        return
    defaults = {k: v for k, v in batch.defaults.items() if k in ("account_id", "category_id", "member_id", "tag_ids")}
    stmt = insert(ImportMappingTemplate).values(
        name=name[:120],
        header_signature=batch.header_signature,
        mapping=batch.mapping,
        options=batch.options,
        defaults=defaults,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[ImportMappingTemplate.header_signature],
        set_={
            "name": stmt.excluded.name,
            "mapping": stmt.excluded.mapping,
            "options": stmt.excluded.options,
            "defaults": stmt.excluded.defaults,
            "updated_at": func.now(),
        },
    ).returning(ImportMappingTemplate.id)
    batch.template_id = await session.scalar(stmt)


# ---------- commit / rollback ----------
async def transaction_factory(session: AsyncSession, batch: ImportBatch):
    """A function turning one of the batch's rows into a new (unsaved) Transaction with the batch defaults."""
    d = batch.defaults
    tags = (await session.scalars(select(Tag).where(Tag.id.in_(d.get("tag_ids") or [])))).all()
    source = batch.origin if batch.origin in ("backfill", "tiller", "simplefin") else batch.source_type
    merchant_aliases = await alias_map(session)

    def make(r: ImportRow, account_id: int | None = None) -> Transaction:
        t = Transaction(
            account_id=account_id or r.account_id,
            txn_date=r.txn_date,
            posted_date=r.posted_date,
            description=r.description,
            original_description=_collect(r.raw, batch.mapping).get("original_description"),
            merchant=canonical(r.merchant, merchant_aliases),
            amount=r.amount,
            category_id=r.category_id,
            category_source="import" if r.category_id else None,
            member_id=d.get("member_id"),
            notes=r.notes,
            check_number=r.check_number,
            source_type=source,
            external_id=r.external_id,
            fingerprint=fingerprint(account_id, r.txn_date, r.amount, r.description) if account_id else r.fingerprint,
            import_batch_id=batch.id,
        )
        t.tags = list(tags)
        return t

    return make


async def commit_batch(session: AsyncSession, batch: ImportBatch, pending_as: str = "skip") -> dict:
    if batch.status != "review":
        raise ImportError_(f"Batch is {batch.status}; only reviewed batches can be committed")
    if batch.source_type == "document":
        from ledger.imports.coverage import same_file_imports

        if twins := await same_file_imports(session, batch):
            raise ImportError_(
                f"This file was already imported as {', '.join(f'#{i}' for i in twins)}; roll that import back first"
            )
    await session.execute(
        update(ImportRow)
        .where(ImportRow.batch_id == batch.id, ImportRow.decision == "pending")
        .values(decision="keep" if pending_as == "keep" else "skip_duplicate")
    )
    rows = (
        await session.scalars(
            select(ImportRow)
            .where(ImportRow.batch_id == batch.id, ImportRow.decision.in_(["insert", "keep"]))
            .order_by(ImportRow.row_index)
        )
    ).all()
    now = datetime.now(UTC)
    make = await transaction_factory(session, batch)
    new_txns: list[tuple[ImportRow, Transaction]] = []
    for r in rows:
        t = make(r)
        session.add(t)
        new_txns.append((r, t))
    await session.flush()

    for r, t in new_txns:
        r.transaction_id = t.id
        if r.decision == "keep":
            await session.execute(
                update(DuplicatePair)
                .where(DuplicatePair.import_row_id == r.id)
                .values(status="confirmed_separate", txn_b_id=t.id, decided_at=now)
            )
    await session.execute(
        update(DuplicatePair)
        .where(
            DuplicatePair.import_row_id.in_(
                select(ImportRow.id).where(ImportRow.batch_id == batch.id, ImportRow.decision == "skip_duplicate")
            ),
            DuplicatePair.status == "pending",
        )
        .values(status="confirmed_duplicate", decided_at=now)
    )
    linked = await _record_sources(session, batch, new_txns)
    new_ids = [t.id for _, t in new_txns]
    by_rule = await apply_rules(session, new_ids) if new_ids else 0

    counts = dict(
        (
            await session.execute(
                select(ImportRow.decision, func.count()).where(ImportRow.batch_id == batch.id).group_by(ImportRow.decision)
            )
        ).all()
    )
    batch.stats = {
        **batch.stats,
        "inserted": len(new_ids),
        "skipped_duplicates": counts.get("skip_duplicate", 0),
        "linked_to_existing": linked,
        "kept_separate": counts.get("keep", 0),
        "invalid": counts.get("invalid", 0),
        "categorized_by_rule": by_rule,
    }
    batch.status, batch.committed_at = "committed", now
    await session.flush()
    if batch.source_type == "document":
        from ledger.imports.coverage import auto_fix

        new_ids += (await auto_fix(session, batch))["new_ids"]
    uncategorized = (
        (
            await session.scalars(
                select(Transaction.id).where(id_in(Transaction.id, new_ids), Transaction.category_id.is_(None))
            )
        ).all()
        if new_ids
        else []
    )
    return {**batch.stats, "uncategorized_ids": list(uncategorized)}


async def _record_sources(session: AsyncSession, batch: ImportBatch, new_txns: list[tuple[ImportRow, Transaction]]) -> int:
    """Link each committed row to the transaction it created or duplicated, and keep row notes on duplicates.

    Returns how many duplicate rows were linked to an existing transaction."""
    base = {"import_batch_id": batch.id, "attachment_id": batch.attachment_id}
    values = [
        {**base, "transaction_id": t.id, "role": "created", "import_row_id": r.id, "txn_date": r.txn_date,
         "description": r.description, "amount": r.amount, "match_score": None}
        for r, t in new_txns
    ]
    matched = (
        await session.execute(
            select(ImportRow, DuplicatePair.txn_a_id, DuplicatePair.score, Transaction.description,
                   Transaction.original_description)
            .join(DuplicatePair, DuplicatePair.import_row_id == ImportRow.id)
            .join(Transaction, Transaction.id == DuplicatePair.txn_a_id)
            .where(
                ImportRow.batch_id == batch.id,
                ImportRow.decision == "skip_duplicate",
                DuplicatePair.status == "confirmed_duplicate",
                Transaction.deleted_at.is_(None),
            )
            .order_by(ImportRow.row_index)
        )
    ).all()
    notes: list[tuple[int, str]] = []
    for r, txn_id, score, existing_desc, existing_orig in matched:
        values.append(
            {**base, "transaction_id": txn_id, "role": "matched", "import_row_id": r.id, "txn_date": r.txn_date,
             "description": r.description, "amount": r.amount, "match_score": score}
        )
        # Only the row's own note column; batch-wide default notes add nothing to an existing transaction.
        body = (_collect(r.raw, batch.mapping).get("notes") or "").strip()
        if body:
            notes.append((txn_id, body))
        other = other_description(r.description, existing_desc, existing_orig)
        if other:
            notes.append((txn_id, f"Also described as: {other}"))
    for start in range(0, len(values), 1000):
        await session.execute(insert(TransactionSource).values(values[start : start + 1000]).on_conflict_do_nothing())
    if notes:
        await _add_import_notes(session, batch, notes)
    return len(matched)


async def _add_import_notes(session: AsyncSession, batch: ImportBatch, notes: list[tuple[int, str]]) -> None:
    ids = list({tid for tid, _ in notes})
    seen: dict[int, set[str]] = defaultdict(set)
    for tid, body in (
        await session.execute(
            select(TransactionNote.transaction_id, TransactionNote.body).where(id_in(TransactionNote.transaction_id, ids))
        )
    ).all():
        seen[tid].add(body.strip())
    main_notes = dict(
        (await session.execute(select(Transaction.id, Transaction.notes).where(id_in(Transaction.id, ids)))).all()
    )
    for tid, body in notes:
        if body in seen[tid] or body in (main_notes.get(tid) or ""):
            continue
        seen[tid].add(body)
        session.add(
            TransactionNote(
                transaction_id=tid,
                body=body,
                source="import",
                import_batch_id=batch.id,
                attachment_id=batch.attachment_id,
            )
        )
    await session.flush()


async def rollback_batch(session: AsyncSession, batch: ImportBatch) -> int:
    if batch.status != "committed":
        raise ImportError_("Only committed imports can be rolled back")
    await session.execute(
        delete(TransactionSource).where(TransactionSource.import_batch_id == batch.id, TransactionSource.role == "matched")
    )
    await session.execute(
        delete(TransactionNote).where(TransactionNote.import_batch_id == batch.id, TransactionNote.source == "import")
    )
    res = await session.execute(
        update(Transaction)
        .where(Transaction.import_batch_id == batch.id, Transaction.deleted_at.is_(None))
        .values(deleted_at=datetime.now(UTC))
    )
    batch.status = "rolled_back"
    batch.stats = {**batch.stats, "rolled_back": res.rowcount}
    await session.execute(delete(StatementCheck).where(StatementCheck.import_batch_id == batch.id))
    return res.rowcount


# ---------- document extraction ----------
async def _suggest_account(session: AsyncSession, institution: str | None, last4: str | None) -> int | None:
    if last4:
        ids = (
            await session.scalars(
                select(Account.id).where(Account.mask.is_not(None), func.right(Account.mask, 4) == last4[-4:])
            )
        ).all()
        if len(ids) == 1:
            return ids[0]
    if institution:
        ids = (
            await session.scalars(
                select(Account.id)
                .join(Institution, Account.institution_id == Institution.id, isouter=True)
                .where(
                    (Institution.name.ilike(f"%{institution}%")) | (Account.name.ilike(f"%{institution}%")),
                    Account.is_closed.is_(False),
                )
            )
        ).all()
        if len(ids) == 1:
            return ids[0]
    return None


def _reconcile(total: Decimal, opening: float | None, closing: float | None) -> dict | None:
    if opening is None or closing is None:
        return None
    change = Decimal(str(closing)) - Decimal(str(opening))
    return {
        "sum_of_rows": f"{total:.2f}",
        "balance_change": f"{change:.2f}",
        "reconciles": abs(abs(change) - abs(total)) < Decimal("0.02"),
    }


def _dec(v) -> Decimal | None:
    if v is None or v == "":
        return None
    return Decimal(str(v)).quantize(Decimal("0.01"))


def balance_fixes(
    amounts: list[Decimal], balances: list[Decimal | None], opening: Decimal | None, closing: Decimal | None
) -> dict[int, Decimal]:
    """One statement account's rows (document order) whose amount disagrees with the change in the printed running
    balance: {position: amount implied by the balances}. Only returned when the corrected rows add up to the
    statement's own balance change, so a misread balance is never mistaken for a misread amount."""
    if opening is None or closing is None or not amounts:
        return {}
    change = closing - opening

    def reconciles(total: Decimal) -> bool:
        return abs(abs(change) - abs(total)) < Decimal("0.02")

    total = sum(amounts, Decimal(0))
    if reconciles(total):
        return {}
    n = len(amounts)
    # Statements list rows oldest-first or newest-first; balances may be printed as owed (credit cards).
    for chrono in (list(range(n)), list(range(n - 1, -1, -1))):
        deltas: dict[int, Decimal] = {}
        prev = opening
        for i in chrono:
            if balances[i] is None:
                prev = None
                continue
            if prev is not None:
                deltas[i] = balances[i] - prev
            prev = balances[i]
        for sign in (1, -1):
            ok = {i for i, d in deltas.items() if amounts[i] == sign * d}
            if len(ok) < max(1, len(deltas) - len(ok)):
                continue
            fixes = {}
            for pos, i in enumerate(chrono):
                if i not in deltas or i in ok:
                    continue
                # The row's own balance must be confirmed by the next row (or the closing balance).
                nxt = chrono[pos + 1] if pos + 1 < n else None
                if (nxt is None and balances[i] == closing) or nxt in ok:
                    fixes[i] = sign * deltas[i]
            if fixes and reconciles(total + sum(fixes[i] - amounts[i] for i in fixes)):
                return fixes
    return {}


def _row_account(value: str | None, accounts: list[dict]) -> str:
    """Resolve a transaction's account reference to one of the statement accounts' refs."""
    if len(accounts) == 1:
        return accounts[0]["ref"]
    v = (value or "").strip()
    for a in accounts:
        if v.lower() == a["ref"].lower():
            return a["ref"]
    digits = "".join(ch for ch in v if ch.isdigit())[-4:]
    hits = [a for a in accounts if digits and a["last4"] and a["last4"][-4:] == digits]
    return hits[0]["ref"] if len(hits) == 1 else v


async def extract_into_batch(session: AsyncSession, batch: ImportBatch) -> dict:
    from ledger.ai.imports import StatementAccount, extract_statement

    att = batch.attachment
    content = await session.scalar(select(Attachment.content).where(Attachment.id == att.id))
    result = await extract_statement(content, att.mime_type, att.filename)
    if not result.transactions:
        raise ImportError_(f"No transactions found ({result.document_type}: {result.summary})")
    found = [a for a in result.accounts if a.last4 or a.name]
    if not found and (result.account_last4 or result.account_name):
        found = [
            StatementAccount(
                last4=result.account_last4,
                name=result.account_name,
                account_type=result.account_type,
                opening_balance=result.opening_balance,
                closing_balance=result.closing_balance,
            )
        ]
    accounts = [{**a.model_dump(), "ref": (a.last4 or a.name).strip()} for a in found]
    refs = [_row_account(t.account, accounts) if accounts else "" for t in result.transactions]
    amounts = [_dec(t.amount) for t in result.transactions]
    balances = [_dec(t.balance) for t in result.transactions]
    fixed: dict[int, Decimal] = {}
    for ref, opening, closing in [(a["ref"], a["opening_balance"], a["closing_balance"]) for a in accounts] or [
        ("", result.opening_balance, result.closing_balance)
    ]:
        mine = [i for i, r in enumerate(refs) if r == ref]
        found_fixes = balance_fixes([amounts[i] for i in mine], [balances[i] for i in mine], _dec(opening), _dec(closing))
        fixed.update({mine[p]: amt for p, amt in found_fixes.items()})
    balance_fixed = [{"row": i, "read": f"{amounts[i]:.2f}", "amount": f"{amt:.2f}"} for i, amt in sorted(fixed.items())]
    for i, amt in fixed.items():
        amounts[i] = amt
    rows = [
        {
            "Date": t.date,
            "Posted": t.posted_date or "",
            "Description": t.description,
            "Details": t.details or "",
            "Amount": f"{amt:.2f}",
            "Account": ref,
            "Balance": "" if bal is None else f"{bal:.2f}",
            "Confidence": f"{t.confidence:.2f}",
        }
        for t, ref, amt, bal in zip(result.transactions, refs, amounts, balances, strict=True)
    ]
    await _insert_rows(session, batch, rows)

    for a in accounts:
        mine = [amt for amt, ref in zip(amounts, refs, strict=True) if ref == a["ref"]]
        a["rows"] = len(mine)
        a["reconciliation"] = _reconcile(sum(mine, Decimal(0)), a["opening_balance"], a["closing_balance"])
    checked = [a["reconciliation"] for a in accounts if a["reconciliation"]]
    if checked:
        recon = {
            "sum_of_rows": f"{sum(Decimal(r['sum_of_rows']) for r in checked):.2f}",
            "balance_change": f"{sum(Decimal(r['balance_change']) for r in checked):.2f}",
            "reconciles": all(r["reconciles"] for r in checked),
        }
    else:
        recon = _reconcile(sum(amounts, Decimal(0)), result.opening_balance, result.closing_balance)
    meta = result.model_dump(exclude={"transactions", "accounts"})
    known = {a["ref"] for a in accounts}
    meta.update(
        {
            "accounts": accounts,
            "unassigned_rows": sum(1 for ref in refs if ref not in known),
            "low_confidence_rows": sum(1 for t in result.transactions if t.confidence < 0.8),
            "reconciliation": recon,
            # Amounts misread from the document, corrected from its running balance column.
            "balance_fixed": balance_fixed,
        }
    )
    batch.doc_meta = meta
    batch.columns = column_profile(DOC_COLUMNS, rows)
    batch.mapping, batch.mapping_source = dict(DOC_MAPPING), "document"
    batch.options = {"date_format": "auto", "dayfirst": False, "invert_sign": False}
    # The institution alone only identifies the account when the statement covers just one.
    institution = result.institution if len(accounts) <= 1 else None
    account_map = {a["ref"]: await _suggest_account(session, institution, a["last4"]) for a in accounts}
    if len(accounts) == 1:
        account_id = account_map[accounts[0]["ref"]]
    elif not accounts:
        account_id = await _suggest_account(session, result.institution, None)
    else:
        account_id = None
    batch.defaults = {
        "account_id": account_id,
        "account_map": account_map,
        "notes": f"Imported from {att.filename}",
    }
    batch.status = "mapping"
    return {"rows": len(rows), "document_type": result.document_type}

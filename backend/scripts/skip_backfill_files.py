"""Skip flagged backfill files with nothing to import (approved 2026-10-10).

- Every failed file: zero-activity statements, notices and loan papers ("No transactions found"), and the .txt the
  service account can't read.
- File #181: "March 21.pdf" is the same Costco statement as file #157 ("March 21 (2).pdf"); its uncommitted import
  is discarded. Retrying a skipped file re-imports it.

Dry run by default; pass --apply to commit.
Run: scripts/with_env.ps1 .env.azure python scripts/skip_backfill_files.py [--apply]
"""

import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select

from ledger.db.engine import dispose_engine, get_sessionmaker
from ledger.models import BackfillFile, ImportBatch

DUPLICATE_FILE, DUPLICATE_OF = 181, 157
ROOT = Path(__file__).resolve().parents[2]


async def run(apply: bool) -> None:
    now = datetime.now(UTC)
    log: list[str] = []
    async with get_sessionmaker()() as s:
        failed = (await s.scalars(select(BackfillFile).where(BackfillFile.status == "failed").order_by(BackfillFile.id))).all()
        dup = await s.get(BackfillFile, DUPLICATE_FILE)
        if dup.status != "review" or not dup.import_batch_id:
            raise SystemExit(f"File #{DUPLICATE_FILE} is {dup.status}; aborting")
        batch = await s.get(ImportBatch, dup.import_batch_id)
        if batch.status != "review":
            raise SystemExit(f"Import #{batch.id} is {batch.status}; aborting")
        files = [*failed, dup]
        if apply:
            backup = [
                {k: getattr(f, k) for k in ("id", "status", "message", "error", "import_batch_id")} for f in files
            ]
            out = ROOT / "logs" / f"skip_backfill_files_backup_{now:%Y%m%d%H%M%S}.json"
            out.write_text(json.dumps(backup, default=str), encoding="utf-8")
            log.append(f"backup: {out}")
        for f in failed:
            log.append(f"skip f{f.id} {f.path}/{f.name} | {(f.error or f.message or '')[:90]}")
            f.status, f.message = "skipped", f"Nothing to import: {(f.error or f.message or '')[:400]}"
            f.error = None
        log.append(f"skip f{dup.id} {dup.path}/{dup.name} and discard its import #{batch.id}")
        dup.status, dup.message, dup.import_batch_id = "skipped", f"Same statement as file #{DUPLICATE_OF}", None
        await s.delete(batch)
        log.append(f"{len(files)} files skipped")
        if apply:
            await s.commit()
        else:
            await s.rollback()
    await dispose_engine()
    print(("APPLIED" if apply else "DRY RUN") + "\n" + "\n".join(log))


if __name__ == "__main__":
    asyncio.run(run("--apply" in sys.argv))

"""Check Google access for the configured service account: uv run python scripts/google_check.py <sheet-url> <folder-url>"""

import asyncio
import sys
from collections import Counter

from ledger.sources import archive, tiller
from ledger.sources.google import service_account_email


async def main(sheet: str | None, folder: str | None) -> None:
    print("service account:", service_account_email())
    if sheet:
        sid = tiller.parse_sheet_id(sheet)
        info = await tiller.sheet_info(sid)
        print("sheet:", info["title"], "| tabs:", ", ".join(info["sheets"]))
        feed = await tiller.fetch({"sheet_id": sid}, full=False)
        print(
            f"tiller last 60 days: {len(feed.transactions)} txns, {len(feed.accounts)} accounts, "
            f"{len(feed.balances)} balances, warnings={feed.warnings}"
        )
    if folder:
        fid = archive.parse_folder_id(folder)
        print("folder:", await archive.drive_folder_name(fid))
        files = await archive.drive_list(fid)
        kinds = Counter((f.name.rsplit(".", 1)[-1].lower() if "." in f.name else "?") for f in files)
        years = Counter(f.path.split("/")[0] or "(root)" for f in files)
        print(f"archive: {len(files)} files, {sum(f.supported for f in files)} supported")
        print("  by extension:", dict(kinds.most_common(12)))
        print("  by top folder:", dict(years.most_common(15)))


asyncio.run(main(*(sys.argv[1:3] + [None, None])[:2])) if sys.argv[1:2] != ["subfolders"] else None


async def subfolders(folder: str) -> None:
    import httpx

    fid = archive.parse_folder_id(folder)
    async with httpx.AsyncClient(timeout=60) as client:
        data = (
            await archive._drive_get(
                client,
                archive.DRIVE,
                {
                    "q": f"'{fid}' in parents and trashed = false and mimeType = '{archive.FOLDER_MIME}'",
                    "fields": "files(id,name)",
                    "pageSize": 1000,
                },
            )
        ).json()
    for f in sorted(data.get("files", []), key=lambda f: f["name"]):
        print(f["id"], f["name"])


if sys.argv[1:2] == ["subfolders"]:
    asyncio.run(subfolders(sys.argv[2]))

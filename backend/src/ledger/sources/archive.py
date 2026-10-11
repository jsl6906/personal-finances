"""Archive providers for the backfill: Google Drive folders (shared with the service account) or a local folder."""

import asyncio
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import httpx

from ledger.config import get_settings
from ledger.imports.parsing import detect_kind
from ledger.sources.google import GoogleNotConfigured, auth_headers, service_account_email

DRIVE = "https://www.googleapis.com/drive/v3/files"
FOLDER_MIME = "application/vnd.google-apps.folder"
SHEET_MIME = "application/vnd.google-apps.spreadsheet"
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
ID_RE = re.compile(r"^[A-Za-z0-9_-]{10,}$")
MAX_FILES = 50_000


class ArchiveError(RuntimeError):
    pass


@dataclass
class ArchiveFile:
    external_id: str
    path: str
    name: str
    mime_type: str | None
    size_bytes: int | None
    modified_at: datetime | None

    @property
    def supported(self) -> bool:
        return self.mime_type == SHEET_MIME or detect_kind(self.name) is not None


# ---- Google Drive ----
def parse_folder_id(value: str) -> str:
    value = value.strip()
    m = re.search(r"/folders/([A-Za-z0-9_-]+)", value) or re.search(r"[?&]id=([A-Za-z0-9_-]+)", value)
    folder = m.group(1) if m else value
    if not ID_RE.match(folder):
        raise ArchiveError("That doesn't look like a Google Drive folder URL or ID")
    return folder


async def _drive_get(client: httpx.AsyncClient, url: str, params: dict | None = None) -> httpx.Response:
    try:
        headers = await auth_headers()
    except GoogleNotConfigured as exc:
        raise ArchiveError(str(exc)) from None
    r = await client.get(url, params=params, headers=headers)
    if r.status_code in (403, 404):
        raise ArchiveError(
            f"Google Drive returned {r.status_code}: share the folder (Viewer) with {service_account_email()}"
        )
    r.raise_for_status()
    return r


async def drive_folder_name(folder_id: str) -> str:
    async with httpx.AsyncClient(timeout=30) as client:
        r = await _drive_get(client, f"{DRIVE}/{folder_id}", {"fields": "name,mimeType", "supportsAllDrives": "true"})
    meta = r.json()
    if meta.get("mimeType") != FOLDER_MIME:
        raise ArchiveError(f"'{meta.get('name')}' is not a folder")
    return meta["name"]


async def drive_list(folder_id: str, root: str = "") -> list[ArchiveFile]:
    out: list[ArchiveFile] = []
    queue: list[tuple[str, str]] = [(folder_id, root)]
    async with httpx.AsyncClient(timeout=60) as client:
        while queue and len(out) < MAX_FILES:
            fid, path = queue.pop(0)
            token = None
            while True:
                params = {
                    "q": f"'{fid}' in parents and trashed = false",
                    "fields": "nextPageToken,files(id,name,mimeType,size,modifiedTime)",
                    "pageSize": 1000,
                    "supportsAllDrives": "true",
                    "includeItemsFromAllDrives": "true",
                }
                if token:
                    params["pageToken"] = token
                data = (await _drive_get(client, DRIVE, params)).json()
                for f in data.get("files", []):
                    if f["mimeType"] == FOLDER_MIME:
                        queue.append((f["id"], f"{path}/{f['name']}".lstrip("/")))
                        continue
                    name = f["name"] + (".xlsx" if f["mimeType"] == SHEET_MIME else "")
                    out.append(
                        ArchiveFile(
                            external_id=f["id"],
                            path=path,
                            name=name,
                            mime_type=f["mimeType"],
                            size_bytes=int(f["size"]) if f.get("size") else None,
                            modified_at=datetime.fromisoformat(f["modifiedTime"].replace("Z", "+00:00"))
                            if f.get("modifiedTime")
                            else None,
                        )
                    )
                token = data.get("nextPageToken")
                if not token:
                    break
    return out


async def drive_download(external_id: str, mime_type: str | None) -> bytes:
    async with httpx.AsyncClient(timeout=180, follow_redirects=True) as client:
        if mime_type == SHEET_MIME:
            r = await _drive_get(client, f"{DRIVE}/{external_id}/export", {"mimeType": XLSX_MIME})
        else:
            r = await _drive_get(client, f"{DRIVE}/{external_id}", {"alt": "media", "supportsAllDrives": "true"})
    return r.content


# ---- local folder (e.g. the /inbox volume) ----
def inbox_root() -> Path:
    root = get_settings().inbox_dir
    if root is None:
        raise ArchiveError("No local archive folder configured (INBOX_DIR)")
    return root.resolve()


def resolve_local(subpath: str) -> Path:
    root = inbox_root()
    target = (root / subpath.strip().lstrip("/\\")).resolve()
    if target != root and root not in target.parents:
        raise ArchiveError("The folder must be inside the configured inbox")
    if not target.is_dir():
        raise ArchiveError(f"Folder not found: {subpath or '/'}")
    return target


def _local_list_sync(subpath: str) -> list[ArchiveFile]:
    root, base = inbox_root(), resolve_local(subpath)
    out = []
    for p in sorted(base.rglob("*")):
        if not p.is_file() or p.name.startswith("."):
            continue
        rel = p.relative_to(root).as_posix()
        st = p.stat()
        out.append(
            ArchiveFile(
                external_id=rel,
                path="" if p.parent == root else p.parent.relative_to(root).as_posix(),
                name=p.name,
                mime_type=None,
                size_bytes=st.st_size,
                modified_at=datetime.fromtimestamp(st.st_mtime, UTC),
            )
        )
        if len(out) >= MAX_FILES:
            break
    return out


async def local_list(subpath: str) -> list[ArchiveFile]:
    return await asyncio.to_thread(_local_list_sync, subpath)


async def local_download(external_id: str) -> bytes:
    root = inbox_root()
    target = (root / external_id).resolve()
    if root not in target.parents:
        raise ArchiveError("File is outside the inbox")
    return await asyncio.to_thread(target.read_bytes)


async def list_files(settings: dict) -> list[ArchiveFile]:
    if settings.get("provider") == "local":
        return await local_list(settings.get("local_path", ""))
    folders = settings.get("folders") or []
    if not folders:
        raise ArchiveError("Add an archive folder first")
    out: list[ArchiveFile] = []
    seen: set[str] = set()
    for folder in folders:
        # Paths start with the root folder's name so files from different folders stay distinguishable.
        for f in await drive_list(folder["id"], folder["name"]):
            if f.external_id not in seen:
                seen.add(f.external_id)
                out.append(f)
    return out


async def download(provider: str, external_id: str, mime_type: str | None) -> bytes:
    if provider == "local":
        return await local_download(external_id)
    return await drive_download(external_id, mime_type)

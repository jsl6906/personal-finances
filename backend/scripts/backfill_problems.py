"""List failed/review backfill files: uv run python scripts/backfill_problems.py"""

import sys

sys.path.insert(0, __file__.rsplit("\\", 1)[0].rsplit("/", 1)[0])
from live import client  # noqa: E402

c = client()
for f in c.get("/backfill/files", params={"limit": 200, "status": ["failed", "review"]}).json():
    print(f"[{f['status']}] {f['kind']} | {f['path']}/{f['name']} | {(f['error'] or f['message'] or '')[:200]}")

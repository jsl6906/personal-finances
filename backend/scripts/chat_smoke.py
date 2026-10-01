"""Ask the running app a chat question: uv run python scripts/chat_smoke.py "question" [file]"""

import sys

import httpx

BASE = "http://127.0.0.1:8000/api"
question = sys.argv[1] if len(sys.argv) > 1 else "What were our top 5 spending categories over the last 12 months?"

with httpx.Client(base_url=BASE, timeout=180) as c:
    c.post("/auth/login", json={"password": sys.argv[3] if len(sys.argv) > 3 else "ledger-dev"}).raise_for_status()
    chat = c.post("/chat/sessions", json={}).json()
    files = {"file": open(sys.argv[2], "rb")} if len(sys.argv) > 2 and sys.argv[2] else None
    r = c.post(f"/chat/sessions/{chat['id']}/messages", data={"content": question}, files=files)
    r.raise_for_status()
    for m in r.json():
        print(f"--- {m['role']}\n{m['content']}")
        for q in m["queries"]:
            print(f"  [sql] {q.get('purpose')}: {q['sql']}")
            print(f"  [err] {q['error']}" if q.get("error") else f"  rows: {q.get('row_count')}")

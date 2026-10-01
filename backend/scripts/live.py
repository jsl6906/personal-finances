"""Drive the running app's API for live checks: uv run python scripts/live.py <command> [args]

Commands: email-setup <addr,...> | test-email | digest | tiller <sheet-url> [full] | job <id> | backfill <folder-url>
"""

import os
import sys
import time

import httpx

BASE = os.environ.get("LEDGER_URL", "http://127.0.0.1:8000") + "/api"


def client() -> httpx.Client:
    c = httpx.Client(base_url=BASE, timeout=300)
    c.post("/auth/login", json={"password": "ledger-dev"}).raise_for_status()
    return c


def wait(c: httpx.Client, job_id: int, every: float = 3) -> dict:
    while True:
        j = c.get(f"/jobs/{job_id}").json()
        if j["status"] in ("succeeded", "failed", "cancelled"):
            return j
        print(f"  … {j['status']} {j.get('message') or ''}", flush=True)
        time.sleep(every)


def main(cmd: str, *args: str) -> None:
    c = client()
    if cmd == "email-setup":
        wanted = {a.strip().lower() for a in args[0].split(",")}
        for r in c.get("/alerts/recipients").json():
            if r["email"] not in wanted:
                c.delete(f"/alerts/recipients/{r['id']}")
        have = {r["email"] for r in c.get("/alerts/recipients").json()}
        for addr in wanted - have:
            c.post("/alerts/recipients", json={"email": addr}).raise_for_status()
        print("recipients:", [r["email"] for r in c.get("/alerts/recipients").json()])
        print("status:", c.get("/alerts/status").json())
    elif cmd == "test-email":
        r = c.post("/alerts/test")
        print(r.status_code, r.json())
    elif cmd == "digest":
        j = wait(c, c.post("/alerts/digest/send").json()["id"])
        print(j["status"], j.get("result"), (j.get("error") or "")[:300])
        print("latest event:", c.get("/alerts/events", params={"limit": 1}).json())
    elif cmd == "tiller":
        r = c.put("/sources/tiller", json={"sheet": args[0], "lookback_days": 60})
        print("configure:", r.status_code, r.json() if r.status_code != 200 else r.json()["config"])
        sid = r.json()["id"]
        full = len(args) > 1 and args[1] == "full"
        started = time.time()
        j = wait(c, c.post(f"/sources/{sid}/sync", params={"full": str(full).lower()}).json()["id"], every=5)
        print(f"{j['status']} in {time.time() - started:.0f}s:", j.get("result"), (j.get("error") or "")[:500])
    elif cmd == "job":
        print(wait(c, int(args[0])))
    elif cmd == "last-error":
        j = c.get("/jobs", params={"type": args[0], "limit": 1}).json()[0]
        frames = [
            ln.strip() for ln in (j.get("error") or "").splitlines() if "ledger" in ln and ln.strip().startswith("File")
        ]
        print(j["id"], j["status"], *frames[-8:], sep="\n")
    elif cmd == "backfill":
        r = c.put("/backfill/settings", json={"provider": "drive", "folder": args[0], "auto_approve_bills": True})
        print("configure:", r.status_code, r.json().get("folder_name") if r.status_code == 200 else r.text)
        j = wait(c, c.post("/backfill/scan").json()["id"])
        print("scan:", j["status"], j.get("result"), (j.get("error") or "")[:300])
    elif cmd == "backfill-skip":
        for f in c.get("/backfill/files", params={"limit": 500, "status": ["pending", "classified"]}).json():
            if f["name"] == args[0]:
                print(f["id"], c.post(f"/backfill/files/{f['id']}", json={"action": "skip"}).json())
    elif cmd == "backfill-start":
        print(c.post("/backfill/start").json())
    elif cmd == "backfill-status":
        ov = c.get("/backfill").json()
        s = ov["summary"]
        print(
            f"{s['processed']}/{s['total']} {s['by_status']} added={s['transactions_added']} dups={s['duplicates_skipped']}"
        )
        print("paused:", ov["settings"]["paused"], "| error:", ov["settings"]["last_error"], "| job:", ov["active_job"])
        for f in c.get("/backfill/files", params={"limit": int(args[0]) if args else 10}).json():
            print(f"  [{f['status']}] {f['kind']} | {f['path']}/{f['name']} | {f['error'] or f['message']}")
    elif cmd == "backfill-run":
        c.post("/backfill/start").raise_for_status()
        while True:
            ov = c.get("/backfill").json()
            s = ov["summary"]
            print(f"  {s['processed']}/{s['total']} {s['by_status']}", flush=True)
            if ov["settings"]["paused"] and not ov["active_job"]:
                break
            time.sleep(10)
        print(
            "error:", ov["settings"]["last_error"], "| added:", s["transactions_added"], "| dups:", s["duplicates_skipped"]
        )
        for f in c.get("/backfill/files", params={"limit": 60}).json():
            print(f"  [{f['status']}] {f['kind']} | {f['name']} | {f['error'] or f['message']}")
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main(*sys.argv[1:])

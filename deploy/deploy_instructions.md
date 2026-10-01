# Deploying Ledger to MediaServer

The app runs as the `ledger` container in the main compose file on MediaServer (Tailscale alias `mediaserver`), at http://mediaserver:8470.
The share `\\MediaServer\Docker` is `/mnt/samsung_ssd/dockerconfig` on the server.

## Pushing an update

1. On the PC, run the tests:
   ```powershell
   cd c:\Projects\personal-finances\backend; uv run pytest -q
   ```
2. Build the image and copy it to the share. This writes `\\MediaServer\Docker\ledger\container_files\ledger-<timestamp>.tar` and refreshes `update_ledger.sh`:
   ```powershell
   cd c:\Projects\personal-finances; powershell -ExecutionPolicy Bypass -File deploy\publish.ps1
   ```
3. On the server, load the image and recreate the container:
   ```bash
   ssh josh@mediaserver
   bash /mnt/samsung_ssd/dockerconfig/ledger/update_ledger.sh
   ```
   The script loads the newest tar, tags it `ledger:latest`, deletes older tars, runs `docker compose up -d --force-recreate ledger`, and waits for the container to report healthy.
   Database migrations run automatically when the app starts, and a backfill that was interrupted resumes on its own.

Check the deployment with `docker logs -f ledger`, or open http://mediaserver:8470/api/health.

## Files on the server (`/mnt/samsung_ssd/dockerconfig/ledger/`)

| Path | Purpose |
|------|---------|
| `.env` | Production settings: Azure Postgres service principal, password hash, session secret, Gemini key, Gmail SMTP, `APP_BASE_URL`. Created once by `deploy/make_server_env.ps1`, then edited by hand. `chmod 600`. |
| `secrets/google-sa.json` | Google service-account key used for Tiller and Drive. Mounted read-only at `/secrets`. Owned by uid 10001 (the container user). |
| `container_files/` | Image tars from `publish.ps1`. |
| `update_ledger.sh` | The update script (source: `deploy/update_ledger.sh`). |

The `ledger` service is defined in `/mnt/samsung_ssd/dockerconfig/docker-compose.yaml`.
`ledger` is also listed in `RESTART_CONTAINERS` in `scripts/azure-pg-firewall.env`, so it restarts when the server's public IP changes, and it has an entry in `dashboard/services.yaml`.

## Notes

- Only one instance may run against the Azure database at a time. Two instances would both run scheduled syncs and emails, so stop any instance on the PC that uses `.env.azure`.
- To change the login password, run `uv run ledger hash-password` (in `backend/`), paste the printed line into the server `.env` with the single quotes kept, then run `docker compose up -d --force-recreate ledger` from `/mnt/samsung_ssd/dockerconfig`.
- `.env` changes take effect only after the container is recreated. A plain `docker restart` does not reload the env file.
- `publish.ps1` saves the image locally and then copies it, because `docker save` written straight to the SMB share produces an empty file.

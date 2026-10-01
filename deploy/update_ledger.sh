#!/usr/bin/env bash
# Load the newest image tar from container_files/ and recreate the ledger container.
# Run on the server: bash /mnt/samsung_ssd/dockerconfig/ledger/update_ledger.sh
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
TAR_DIR="$SCRIPT_DIR/container_files"

latest_tar="$(ls -1t "$TAR_DIR"/ledger-*.tar 2>/dev/null | head -n1 || true)"
if [[ -z "$latest_tar" ]]; then
  echo "No ledger-*.tar files found in $TAR_DIR" >&2
  exit 1
fi

echo "[INFO] Loading $latest_tar"
load_output="$(docker load -i "$latest_tar")"
echo "$load_output"
if [[ "$load_output" =~ Loaded\ image:\ ([^[:space:]]+) ]]; then
  docker tag "${BASH_REMATCH[1]}" ledger:latest
else
  echo "[ERROR] Could not find the loaded image name" >&2
  exit 1
fi

old_tars="$(ls -1t "$TAR_DIR"/ledger-*.tar | tail -n +2 || true)"
if [[ -n "$old_tars" ]]; then
  echo "[INFO] Removing old tars:"; echo "$old_tars"
  echo "$old_tars" | xargs rm -f
fi

(cd "$SCRIPT_DIR/.." && docker compose up -d --force-recreate ledger)

echo "[INFO] Waiting for health check..."
for _ in $(seq 1 30); do
  status="$(docker inspect -f '{{.State.Health.Status}}' ledger 2>/dev/null || echo missing)"
  [[ "$status" == "healthy" ]] && break
  sleep 5
done
docker ps --filter name=ledger --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}'
docker logs --tail 15 ledger

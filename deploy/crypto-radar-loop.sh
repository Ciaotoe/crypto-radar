#!/usr/bin/env bash
set -u

REPO_DIR="${REPO_DIR:-/opt/crypto-radar}"
UV_BIN="${UV_BIN:-/usr/local/bin/uv}"
SCAN_SECONDS="${SCAN_SECONDS:-900}"
OFFSET_SECONDS="${OFFSET_SECONDS:-15}"
SCAN_TIMEOUT_SECONDS="${SCAN_TIMEOUT_SECONDS:-1500}"

if [ ! -x "$UV_BIN" ]; then
  echo "uv not executable: $UV_BIN" >&2
  exit 1
fi
cd "$REPO_DIR" || exit 1

while true; do
  now="$(date +%s)"
  next=$(( (now / SCAN_SECONDS + 1) * SCAN_SECONDS + OFFSET_SECONDS ))
  wait_seconds=$(( next - now ))
  if [ "$wait_seconds" -lt 1 ]; then
    wait_seconds=1
  fi
  sleep "$wait_seconds"

  echo "=== scan $(date -u '+%Y-%m-%d %H:%M:%SZ') ==="
  timeout "$SCAN_TIMEOUT_SECONDS" "$UV_BIN" run --no-sync crypto_radar.py
  rc=$?
  if [ "$rc" -ne 0 ]; then
    echo "scan failed with exit code $rc; retrying at next boundary" >&2
  fi
done

#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="${REPO_DIR:-/opt/crypto-radar}"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/crypto-radar}"
KEEP_DAYS="${KEEP_DAYS:-30}"
STAMP="$(TZ=Asia/Shanghai date +%Y%m%dT%H%M%S)"
TMP=""

cleanup() {
  if [ -n "$TMP" ] && [ -f "$TMP" ]; then
    rm -f "$TMP"
  fi
}
trap cleanup EXIT

mkdir -p "$BACKUP_DIR"

files=()
for name in data/signals.json data/universe.json data/signals_archive.json data/klines_4h.json; do
  if [ -f "$REPO_DIR/$name" ]; then
    files+=("$name")
  fi
done

if [ "${#files[@]}" -eq 0 ]; then
  echo "crypto-radar: no data files to back up"
  exit 0
fi

TMP="$(mktemp "$BACKUP_DIR/.backup-XXXXXX")"
tar -czf "$TMP" -C "$REPO_DIR" "${files[@]}"
mv "$TMP" "$BACKUP_DIR/crypto-radar-$STAMP.tar.gz"
TMP=""
echo "crypto-radar: backup $BACKUP_DIR/crypto-radar-$STAMP.tar.gz"

find "$BACKUP_DIR" -type f -name 'crypto-radar-*.tar.gz' -mtime +"$KEEP_DAYS" -delete

#!/bin/bash
# Deletes the one-time setup token (file + .env line) as soon as the first administrator exists.
# Runs as a systemd service on the temporary test server. Prints/logs no secrets.
DIR_FILE="${POSTER_DIR_FILE:-/etc/poster-project-dir}"
TOKEN_FILE="${POSTER_TOKEN_FILE:-/root/setup-token.txt}"
LOG="${POSTER_LOG:-/var/log/poster-bootstrap.log}"
INTERVAL="${POSTER_POLL_SECONDS:-15}"

project_dir() { cat "$DIR_FILE"; }

setup_done() {
  ( cd "$(project_dir)" 2>/dev/null && docker compose -f docker-compose.yml -f docker-compose.https.yml exec -T db \
      psql -U poster -d poster -Atc "select 1 from app_state where key = 'setup_completed'" 2>/dev/null ) | grep -qx 1
}

remove_token() {  # remove_token <path of .env>
  if [[ -f "$TOKEN_FILE" ]]; then shred -u "$TOKEN_FILE" 2>/dev/null || rm -f "$TOKEN_FILE"; fi
  if [[ -f "$1" ]]; then
    python3 - "$1" <<'PY'
import sys
path = sys.argv[1]
lines = [l for l in open(path, encoding="utf-8").read().splitlines() if l.split("=", 1)[0].strip() != "SETUP_TOKEN"]
open(path, "w", encoding="utf-8").write("\n".join(lines) + "\n")   # keeps the file's 0600 mode
PY
  fi
}

run_once() {
  if setup_done; then
    remove_token "$(project_dir)/.env"
    echo "$(date -u +%FT%TZ) setup token deleted (first administrator exists)" >> "$LOG"
    return 0
  fi
  return 1
}

main() { until run_once; do sleep "$INTERVAL"; done; }

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then main "$@"; fi

#!/bin/bash
# =====================================================================================================
# TEMPORARY HTTPS TEST SERVER - paste this whole file, UNCHANGED, into the "user data" / "cloud-init" box
# when you create an Ubuntu 24.04 server. It runs once, as root, at first boot and:
#   installs Docker -> downloads the PUBLIC project (no token) -> generates every secret ON THE SERVER
#   -> starts PostgreSQL + migrations + web + worker + Caddy (automatic HTTPS) in development/test mode.
#
# THIS FILE CONTAINS NO SECRETS AND NEEDS NONE. The one-time setup token for the first-run page is
# generated here on the server (cryptographically random), kept in a root-only file (mode 0600), shown only
# on the provider's web console screen, and deleted automatically once the first administrator exists.
# Nothing secret is ever printed to stdout/logs; progress without secrets goes to /var/log/poster-bootstrap.log.
# =====================================================================================================
REPO_URL="https://github.com/kawsar98dd-lang/telegram-autopost-bot.git"   # public repository: no credentials involved

set -u
LOG="${POSTER_LOG:-/var/log/poster-bootstrap.log}"
PROJECT_DIR="${POSTER_PROJECT_DIR:-/opt/poster}"
TOKEN_FILE="${POSTER_TOKEN_FILE:-/root/setup-token.txt}"
STATE_FILE="${POSTER_DIR_FILE:-/etc/poster-project-dir}"

log() { echo "$(date -u +%FT%TZ) $*" >> "$LOG"; }

# Prints to the physical/virtual console screens only (never to files, logs or stdout).
announce_on_console() {
  local t
  for t in /dev/tty1 /dev/console; do
    if [[ -w "$t" ]]; then printf '\n%s\n' "$1" > "$t" 2>/dev/null || true; fi
  done
}

die() {
  log "FATAL: $*"
  announce_on_console "POSTER TEST SERVER: SETUP FAILED ($*). Log in as root on this console and read: tail -n 40 /var/log/poster-bootstrap.log"
  exit 1
}

check_inputs() {
  [[ "$REPO_URL" =~ ^https://[A-Za-z0-9./_-]+$ ]] || { log "REPO_URL must be a plain https URL"; return 1; }
  return 0
}

valid_ipv4() { [[ "$1" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]] && ! [[ "$1" =~ (^|\.)(25[6-9]|2[6-9][0-9]|[3-9][0-9]{2})(\.|$) ]]; }

derive_host() { valid_ipv4 "$1" || return 1; echo "${1//./-}.sslip.io"; }

detect_ip() {
  local ip url
  # provider metadata first (no third party involved), public echo services only as a fallback
  for url in http://169.254.169.254/hetzner/v1/metadata/public-ipv4 https://api.ipify.org https://ipv4.icanhazip.com; do
    ip=$(curl -4 -fsS --max-time 8 "$url" 2>/dev/null | tr -d '[:space:]')
    if valid_ipv4 "$ip"; then echo "$ip"; return 0; fi
  done
  return 1
}

set_env_value() {  # set_env_value <file> <KEY> <VALUE>; the value travels in the environment, never on a command line
  ENV_KEY="$2" ENV_VALUE="$3" python3 - "$1" <<'PY'
import os, sys
path, key, value = sys.argv[1], os.environ["ENV_KEY"], os.environ["ENV_VALUE"]
lines, done = open(path, encoding="utf-8").read().splitlines(), False
for i, line in enumerate(lines):
    if line.split("=", 1)[0].strip() == key and not line.lstrip().startswith("#"):
        lines[i], done = f"{key}={value}", True
if not done:
    lines.append(f"{key}={value}")
open(path, "w", encoding="utf-8").write("\n".join(lines) + "\n")
PY
}

create_setup_token() {  # writes a random one-time token to $TOKEN_FILE (mode 0600). Prints NOTHING.
  python3 - "$TOKEN_FILE" <<'PY' || return 1
import os, secrets, sys
alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"            # no look-alike characters, easy to type on a phone
body = "".join(secrets.choice(alphabet) for _ in range(20))  # 100 bits from the OS CSPRNG
token = "-".join(body[i:i + 5] for i in range(0, 20, 5))
fd = os.open(sys.argv[1], os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w") as fh:
    fh.write(token + "\n")
PY
  chmod 600 "$TOKEN_FILE"
}

write_env() {  # write_env <project dir> <public host>
  local dir="$1" host="$2"
  ( cd "$dir" && python3 scripts/generate_keys.py --init-env >/dev/null ) || return 1
  local f="$dir/.env" token
  token=$(<"$TOKEN_FILE")
  set_env_value "$f" APP_ENV development            # temporary test mode: allows LICENSE_ENFORCEMENT=false
  set_env_value "$f" APP_URL "https://$host"        # the browser origin must match exactly
  set_env_value "$f" APP_HOST "$host"
  set_env_value "$f" SETUP_TOKEN "$token"
  set_env_value "$f" LICENSE_ENFORCEMENT false      # there is no license server yet (test only)
  set_env_value "$f" TRUST_PROXY_HEADERS true       # Caddy is the only entrance and sets X-Forwarded-For
  set_env_value "$f" WEB_BIND_ADDR 127.0.0.1        # the plain-http port is not reachable from outside
  chmod 600 "$f"
}

install_prereqs() {
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -y >> "$LOG" 2>&1 || return 1
  # Ubuntu's own signed packages first (no "curl | sh"); Docker's convenience script only as a fallback.
  apt-get install -y git curl ca-certificates python3 python3-cryptography docker.io docker-compose-v2 >> "$LOG" 2>&1 || true
  apt-get install -y git curl ca-certificates python3 python3-cryptography >> "$LOG" 2>&1 || return 1
  if ! docker compose version >> "$LOG" 2>&1; then
    log "Ubuntu docker packages unavailable, using Docker's official install script"
    curl -fsSL https://get.docker.com -o /tmp/get-docker.sh && sh /tmp/get-docker.sh >> "$LOG" 2>&1 || return 1
    rm -f /tmp/get-docker.sh
  fi
  systemctl enable --now docker >> "$LOG" 2>&1 || true
  docker compose version >> "$LOG" 2>&1
}

clone_repo() {  # prints the project directory (the folder that contains docker-compose.yml)
  rm -rf "$PROJECT_DIR"
  git clone --depth 1 "$REPO_URL" "$PROJECT_DIR" >> "$LOG" 2>&1 || return 1
  local compose; compose=$(find "$PROJECT_DIR" -maxdepth 2 -name docker-compose.yml | head -n1)
  [[ -n "$compose" ]] || return 1
  dirname "$compose"
}

install_helpers() {  # install_helpers <project dir>: console commands + automatic removal of the setup token
  local dir="$1"
  echo "$dir" > "$STATE_FILE"
  install -m 0755 "$dir/deploy/diagnose.sh" /usr/local/bin/poster-diagnose || return 1
  install -m 0755 "$dir/deploy/poster-token-cleanup.sh" /usr/local/sbin/poster-token-cleanup.sh || return 1
  printf '#!/bin/bash\nif [ -r %s ]; then cat %s; else echo "No token file: it is deleted automatically once the first administrator exists."; fi\n' \
    "$TOKEN_FILE" "$TOKEN_FILE" > /usr/local/bin/poster-token
  chmod 0700 /usr/local/bin/poster-token
  cat > /etc/systemd/system/poster-token-cleanup.service <<'UNIT'
[Unit]
Description=Delete the one-time setup token once the first administrator exists
After=docker.service
Requires=docker.service

[Service]
ExecStart=/usr/local/sbin/poster-token-cleanup.sh
Restart=on-failure

[Install]
WantedBy=multi-user.target
UNIT
  systemctl daemon-reload >> "$LOG" 2>&1 && systemctl enable --now poster-token-cleanup.service >> "$LOG" 2>&1
}

main() {
  : > "$LOG"; chmod 600 "$LOG"
  check_inputs || die "input check failed"
  local ip host dir i
  ip=$(detect_ip) || die "could not detect the public IPv4 address"
  host=$(derive_host "$ip") || die "bad IP address"
  log "public host will be $host"
  install_prereqs || die "installing Docker and tools failed"
  dir=$(clone_repo) || die "downloading the project failed"
  create_setup_token || die "creating the setup token failed"
  write_env "$dir" "$host" || die "creating .env failed"
  cd "$dir" || die "cannot enter the project folder"
  [[ -f deploy/Caddyfile && -f docker-compose.https.yml && -f deploy/diagnose.sh && -f deploy/poster-token-cleanup.sh ]] \
    || die "this version of the project lacks the HTTPS test files"
  install_helpers "$dir" || die "installing helper commands failed"
  docker compose -f docker-compose.yml -f docker-compose.https.yml up -d --build >> "$LOG" 2>&1 || die "docker compose failed"
  for i in $(seq 1 60); do
    if curl -fsS --max-time 8 "https://$host/health/ready" >/dev/null 2>&1; then
      log "READY: https://$host"
      announce_on_console "POSTER TEST SERVER READY: open https://$host   One-time setup token (shown only on this screen): $(<"$TOKEN_FILE")   If you cannot read it: log in as root and run: poster-token"
      return 0
    fi
    sleep 10
  done
  log "stack started but https://$host did not answer within 10 minutes (certificate or firewall problem?)"
  announce_on_console "POSTER TEST SERVER NOT READY at https://$host. Log in as root on this console and run: poster-diagnose   (setup token, if needed: poster-token)"
}

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then main "$@"; fi

#!/bin/bash
# OPTIONAL evidence helper, run on the test server inside the project folder (where docker-compose.yml is).
# It prints counts only. Values you type are hidden, never printed, never put on any command line
# (patterns are handed to grep through a process substitution, so they do not appear in `ps`).
C="docker compose -f docker-compose.yml -f docker-compose.https.yml"
q() { $C exec -T db psql -U poster -d poster -c "$1"; }

echo "== accounts (status; are the API credential columns empty?)"
q "select status, api_id_enc is null as no_id, api_hash_enc is null as no_hash from telegram_accounts;"
echo "== stored sessions (must start with gAAAA = encrypted; 0 after Disconnect)"
q "select count(*) as sessions, min(left(session_enc,5)) as starts_with from telegram_sessions;"
echo "== logins still in progress (must be 0 when you are finished)"
q "select count(*) as pending_logins from telegram_login_attempts;"

echo "== is any secret stored or logged in clear text?  Expected: 0 and 0 for every item"
for label in "API hash" "login code" "2FA password" "phone number digits"; do
  read -rsp "Type your $label (hidden; press Enter to skip): " secret; echo
  [ -z "$secret" ] && continue
  in_db=$($C exec -T db pg_dump -U poster poster | grep -cF -f <(printf '%s\n' "$secret"))
  in_logs=$($C logs web worker 2>&1 | grep -cF -f <(printf '%s\n' "$secret"))
  echo "$label: matches in database = $in_db, in logs = $in_logs"
  secret=""
done

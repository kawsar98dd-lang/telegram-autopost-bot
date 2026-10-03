#!/bin/bash
# Non-secret diagnostics for the temporary test server. Log in as root on the provider's web console and run:
#     poster-diagnose
# Output contains no secrets (log lines are additionally masked).
DIR=$(cat /etc/poster-project-dir 2>/dev/null)
C="docker compose -f docker-compose.yml -f docker-compose.https.yml"
mask() { sed -E 's/gAAAA[A-Za-z0-9_=-]+/[MASKED]/g; s/[A-Za-z0-9+\/_-]{60,}/[MASKED]/g'; }

echo "== bootstrap log (last 25 lines)"; tail -n 25 /var/log/poster-bootstrap.log 2>/dev/null | mask
if [ -z "$DIR" ] || ! cd "$DIR" 2>/dev/null; then echo "Project folder not found: setup failed before the download. See the log above."; exit 1; fi
echo "== containers"; $C ps 2>&1 | head -n 20
echo "== listening sockets (expected: 0.0.0.0:80 and 0.0.0.0:443 from Docker, 127.0.0.1:8000; NOTHING on 5432)"
ss -ltn 2>/dev/null | awk 'NR==1 || /:(80|443|8000|5432|22) /'
echo "== local health (200 expected)"; curl -s -o /dev/null -w "%{http_code}\n" --max-time 8 http://127.0.0.1:8000/health/ready
echo "== certificate / proxy log (Caddy, last 30 lines)"; $C logs --tail 30 caddy 2>&1 | mask
echo "== web log (last 15 lines)"; $C logs --tail 15 web 2>&1 | mask
echo "== setup token file present? (it is deleted once the first administrator exists)"
if [ -e /root/setup-token.txt ]; then echo "yes (read it with: poster-token)"; else echo "no"; fi

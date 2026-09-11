#!/usr/bin/env bash
# Print an access token for the API, for pasting into Swagger UI or curl.
#
# Usage:
#   ./scripts/api-token.sh                             # default dev admin
#   ./scripts/api-token.sh viewer@dev.example.com      # a specific user
#   ./scripts/api-token.sh admin@acme.example.com      # the other tenant
#
# The password is read from API_PASSWORD if set, so it need not be typed or
# left in shell history.
set -euo pipefail

EMAIL="${1:-admin@dev.example.com}"
PASSWORD="${API_PASSWORD:-DevPassword123!}"
PORT="${API_PORT:-8200}"
BASE="http://localhost:${PORT}"

response=$(curl -sS --max-time 20 -X POST "${BASE}/api/v1/auth/login" \
  -H 'Content-Type: application/json' \
  -d "{\"email\":\"${EMAIL}\",\"password\":\"${PASSWORD}\"}")

token=$(printf '%s' "$response" | python3 -c '
import json, sys
try:
    payload = json.load(sys.stdin)
except ValueError:
    sys.exit("could not parse the API response")
if "access_token" not in payload:
    detail = payload.get("detail", payload)
    sys.exit("login failed: " + str(detail))
print(payload["access_token"])
')

cat <<SUMMARY >&2
signed in as ${EMAIL}

Swagger UI:  ${BASE}/docs
  Click "Authorize", paste the token below, then try any endpoint.

curl:
  curl -s ${BASE}/api/v1/pbxs -H "Authorization: Bearer \$(./scripts/api-token.sh ${EMAIL})"

SUMMARY

printf '%s\n' "$token"

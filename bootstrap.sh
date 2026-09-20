#!/usr/bin/env bash
# One-time credential setup for zmail. Run this yourself in a terminal.
#
# Everything secret is read with `read -s` (no echo, no shell history), exchanged
# over TLS, and written straight into the macOS Keychain. Nothing is printed and
# nothing is written to a file, so the values never reach an agent's context.
set -euo pipefail

SERVICE="zoho-mail-agent"
REGION="${ZOHO_REGION:-com}"
ACCOUNTS="https://accounts.zoho.${REGION}"

echo "Zoho Mail — agent credentials"
echo "Region: ${REGION}  (override with ZOHO_REGION=eu ./bootstrap.sh)"
echo
echo "Have api-console.zoho.com open with a Self Client created, and the"
echo "generated code in hand. The code expires — 10 minutes at the shortest"
echo "setting — so generate it immediately before running this."
echo
echo "The scope you pasted in the console is what this token can ever do."
echo "A refresh never widens a grant, so to add sending later you come back"
echo "here and redo it. For read plus send, the code must have been generated"
echo "with ZohoMail.messages.CREATE alongside the three READ scopes."
echo "Afterwards, 'zmail whoami' reports which one you ended up with."
echo

read -r  -p "Client ID:     " CLIENT_ID
read -rs -p "Client Secret: " CLIENT_SECRET; echo
read -rs -p "Generated code: " CODE; echo
echo

[ -n "$CLIENT_ID" ] && [ -n "$CLIENT_SECRET" ] && [ -n "$CODE" ] || {
  echo "All three values are required." >&2; exit 1; }

echo "Exchanging the code for a refresh token..."
RESP=$(curl -sS -X POST "${ACCOUNTS}/oauth/v2/token" \
  --data-urlencode "grant_type=authorization_code" \
  --data-urlencode "client_id=${CLIENT_ID}" \
  --data-urlencode "client_secret=${CLIENT_SECRET}" \
  --data-urlencode "code=${CODE}")

REFRESH=$(printf '%s' "$RESP" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("refresh_token",""))' 2>/dev/null || true)

if [ -z "$REFRESH" ]; then
  # Print the error but never the request: the response carries no secret of ours,
  # and without it "it failed" is unactionable.
  echo "No refresh_token returned. Zoho said:" >&2
  printf '%s\n' "$RESP" >&2
  echo >&2
  echo "Most common causes, in order:" >&2
  echo "  invalid_code       the code expired or was already redeemed — generate a new one" >&2
  echo "  invalid_client     wrong region; retry with ZOHO_REGION=eu (or in / com.au / jp)" >&2
  echo "  invalid_scope      a scope was mistyped in the console" >&2
  exit 1
fi

store_secret() {  # $1=account $2=value -- never echoed
  case "$(uname -s)" in
    Darwin) security add-generic-password -U -s "$SERVICE" -a "$1" -w "$2" \
              -D "Zoho Mail read-only agent access" -j "Created by agent-mail-mcp/bootstrap.sh" ;;
    *)      command -v secret-tool >/dev/null 2>&1 || {
              echo "secret-tool not found. Install libsecret-tools (Debian/Ubuntu:" >&2
              echo "  sudo apt install libsecret-tools) or export the three ZOHO_* vars." >&2
              exit 1; }
            printf '%s' "$2" | secret-tool store --label="Zoho Mail read-only agent access" \
              service "$SERVICE" account "$1" ;;
  esac
}

for pair in "client_id:${CLIENT_ID}" "client_secret:${CLIENT_SECRET}" "refresh_token:${REFRESH}"; do
  store_secret "${pair%%:*}" "${pair#*:}"
done

unset CLIENT_SECRET CODE REFRESH RESP

echo
echo "Stored in the Keychain under service '${SERVICE}'."
echo "Refresh tokens do not expire on their own — revoke at api-console.zoho.com"
echo "if this machine is ever lost."
echo
echo "Verify:  zmail whoami"

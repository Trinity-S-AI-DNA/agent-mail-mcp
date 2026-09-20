#!/usr/bin/env bash
# One-time setup for the Telegram notification bridge. Run this yourself.
#
# The bot token is read with `read -s` and written straight into the keyring:
# no echo, no shell history, no file in this repo. The chat id is discovered by
# asking Telegram, not by making you hunt for it.
#
# Stored under a SEPARATE keychain service from the mailbox credentials, so
# revoking the bot never touches mail access and vice versa.
set -euo pipefail

SERVICE="mail-telegram-bridge"

echo "Telegram bridge for agent-mail"
echo
echo "1. Open Telegram and talk to @BotFather"
echo "2. /newbot  ->  give it a name and a username"
echo "3. BotFather replies with a token like 1234567890:AAE...  -- copy it"
echo
echo "Use a NEW bot dedicated to this. A token pasted here is a token that can"
echo "post as that bot everywhere it is a member."
echo

read -rs -p "Bot token: " BOT_TOKEN; echo
echo

[ -n "${BOT_TOKEN}" ] || { echo "The token is required." >&2; exit 1; }

# Validate before storing. Storing first means a typo is discovered later, by a
# bridge that silently never delivers.
echo "Checking the token with Telegram..."
ME="$(curl -fsS "https://api.telegram.org/bot${BOT_TOKEN}/getMe" || true)"
if ! printf '%s' "${ME}" | grep -q '"ok":true'; then
  echo >&2
  echo "Telegram rejected that token. Nothing was stored." >&2
  echo "Most likely a stray space, or the token was revoked in BotFather." >&2
  exit 1
fi
BOT_NAME="$(printf '%s' "${ME}" | sed -n 's/.*"username":"\([^"]*\)".*/\1/p')"
echo "Token is valid: @${BOT_NAME}"
echo

echo "Now open a chat with @${BOT_NAME} in Telegram and send it any message"
echo "(just 'hi' is enough). A bot cannot start a conversation with you --"
echo "Telegram only lets it reply, so this step is not optional."
echo
read -r -p "Press Enter once you have sent it..."

echo "Looking for your chat..."
CHAT_ID=""
for _ in 1 2 3 4 5; do
  UPDATES="$(curl -fsS "https://api.telegram.org/bot${BOT_TOKEN}/getUpdates" || true)"
  CHAT_ID="$(printf '%s' "${UPDATES}" | sed -n 's/.*"chat":{"id":\(-\{0,1\}[0-9]\{1,\}\).*/\1/p' | head -1)"
  [ -n "${CHAT_ID}" ] && break
  sleep 2
done

if [ -z "${CHAT_ID}" ]; then
  echo >&2
  echo "No message found. Nothing was stored." >&2
  echo "Send the bot a message first, then run this again. If you are sure you" >&2
  echo "did: another process may have consumed the updates (a webhook, or a" >&2
  echo "second copy of this script)." >&2
  exit 1
fi
echo "Found chat id: ${CHAT_ID}"
echo

store_secret() {  # $1=account $2=value -- never echoed
  case "$(uname -s)" in
    Darwin) security add-generic-password -U -s "${SERVICE}" -a "$1" -w "$2" ;;
    *)      command -v secret-tool >/dev/null 2>&1 || {
              echo "secret-tool not found. Install libsecret-tools (Debian/Ubuntu:" >&2
              echo "  sudo apt install libsecret-tools) or export TELEGRAM_BOT_TOKEN" >&2
              echo "  and TELEGRAM_CHAT_ID instead." >&2
              exit 1; }
            printf '%s' "$2" | secret-tool store --label="${SERVICE} $1" \
              service "${SERVICE}" account "$1" ;;
  esac
}

store_secret telegram_bot_token "${BOT_TOKEN}"
store_secret telegram_chat_id "${CHAT_ID}"
unset BOT_TOKEN

echo "Stored in the keyring under service '${SERVICE}'."
echo
echo "Test it:        ./mailbot.py --test"
echo "One sweep:      ./mailbot.py --once"
echo "Keep watching:  ./mailbot.py --watch"
echo
echo "The first sweep announces nothing: it records where the mailbox is now and"
echo "notifies only about what arrives after."

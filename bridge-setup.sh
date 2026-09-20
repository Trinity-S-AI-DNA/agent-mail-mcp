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

read_secret() {  # $1=account -- prints the stored value, empty if absent
  case "$(uname -s)" in
    Darwin) security find-generic-password -s "${SERVICE}" -a "$1" -w 2>/dev/null || true ;;
    *)      secret-tool lookup service "${SERVICE}" account "$1" 2>/dev/null || true ;;
  esac
}

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

# One mailbox can feed several agents. Adding one afterwards must not require
# re-entering the bot token, so it is its own mode.
#
#   ./bridge-setup.sh --add-chat 229866794
#   ./bridge-setup.sh --list-chats
#   ./bridge-setup.sh --remove-chat 229866794
if [ "${1:-}" = "--add-chat" ] || [ "${1:-}" = "--remove-chat" ]; then
  TARGET="${2:-}"
  [ -n "${TARGET}" ] || { echo "Usage: $0 $1 <chat-id>" >&2; exit 1; }
  case "${TARGET}" in
    ''|*[!0-9-]*) echo "A chat id is digits, optionally leading '-' for a group." >&2; exit 1 ;;
  esac
  CURRENT="$(read_secret telegram_chat_id)"
  NEW=""
  for c in ${CURRENT//,/ }; do
    [ "$c" = "${TARGET}" ] && continue          # drop it; re-added below if adding
    NEW="${NEW:+${NEW},}$c"
  done
  if [ "${1}" = "--add-chat" ]; then
    NEW="${NEW:+${NEW},}${TARGET}"
  fi
  [ -n "${NEW}" ] || { echo "That would leave no recipients. Refusing." >&2; exit 1; }
  store_secret telegram_chat_id "${NEW}"
  echo "Recipients are now: ${NEW}"
  echo
  if [ "${1}" = "--add-chat" ]; then
    echo "A bot cannot open a conversation on its own. Chat ${TARGET} must send"
    echo "the bot a message first, or delivery fails with 'chat not found'."
    echo "Check with: ./mailbot.py --test"
  fi
  exit 0
fi

if [ "${1:-}" = "--list-chats" ]; then
  CURRENT="$(read_secret telegram_chat_id)"
  OWNER="$(read_secret telegram_owner_id)"
  [ -n "${CURRENT}" ] && echo "feed:  ${CURRENT}" || echo "feed:  (no recipients configured)"
  if [ -n "${OWNER}" ]; then
    echo "owner: ${OWNER}"
  elif [ -n "${CURRENT}" ] && [ "${CURRENT#*,}" = "${CURRENT}" ]; then
    echo "owner: ${CURRENT}  (the only recipient, by default)"
  else
    echo "owner: (none -- nobody can reply or send; $0 --set-owner <id>)"
  fi
  exit 0
fi

# Receiving the feed and commanding the mailbox are different powers. An owner
# can reply, send, and drive the job loop; everyone else on the feed can only
# read. With one recipient the distinction is empty and the default covers it;
# the moment a second agent is added, the choice has to be made explicitly,
# because the alternative is guessing which chat may write as the mailbox.
if [ "${1:-}" = "--set-owner" ]; then
  TARGET="${2:-}"
  [ -n "${TARGET}" ] || { echo "Usage: $0 --set-owner <chat-id>[,<chat-id>...]" >&2; exit 1; }
  case "${TARGET}" in
    ''|*[!0-9,-]*) echo "A chat id is digits, optionally leading '-' for a group." >&2; exit 1 ;;
  esac
  FEED="$(read_secret telegram_chat_id)"
  for c in ${TARGET//,/ }; do
    case ",${FEED}," in
      *",$c,"*) ;;
      *) echo "Chat $c is not on the feed. Add it first: $0 --add-chat $c" >&2; exit 1 ;;
    esac
  done
  store_secret telegram_owner_id "${TARGET}"
  echo "Owner is now: ${TARGET}"
  echo "Everyone else on the feed keeps read-only access."
  exit 0
fi

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
echo "Feeding several agents? Have EACH of them message the bot now. Every chat"
echo "that has written to it will be picked up in one pass."
echo
read -r -p "Press Enter once they have sent it..."

echo "Looking for chats..."
CHAT_ID=""
for _ in 1 2 3 4 5; do
  UPDATES="$(curl -fsS "https://api.telegram.org/bot${BOT_TOKEN}/getUpdates" || true)"
  # Every distinct chat, not just the first: one mailbox can feed several agents,
  # and picking head -1 silently ignored everyone who wrote in second.
  CHAT_ID="$(printf '%s' "${UPDATES}" \
    | grep -o '"chat":{"id":-\{0,1\}[0-9]\{1,\}' \
    | sed 's/.*://' \
    | awk '!seen[$0]++' \
    | paste -sd, -)"
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
echo "Found chat id(s): ${CHAT_ID}"
echo

store_secret telegram_bot_token "${BOT_TOKEN}"
store_secret telegram_chat_id "${CHAT_ID}"
unset BOT_TOKEN

# With one chat there is nothing to choose. With several, the script must not
# pick for you: the owner is the chat allowed to send mail as this mailbox, and
# the discovery order above is the order people happened to press Send in.
if [ "${CHAT_ID#*,}" = "${CHAT_ID}" ]; then
  store_secret telegram_owner_id "${CHAT_ID}"
  echo "Owner (may reply and send): ${CHAT_ID}"
else
  echo "Several chats are on the feed: ${CHAT_ID}"
  echo
  echo "Which of them is YOURS? That chat may reply, send mail and drive the"
  echo "job campaign. The others receive the feed and can only read."
  read -r -p "Owner chat id (blank = decide later): " OWNER_ID
  if [ -n "${OWNER_ID}" ]; then
    store_secret telegram_owner_id "${OWNER_ID}"
    echo "Owner: ${OWNER_ID}"
  else
    echo "No owner set. Until you run '$0 --set-owner <id>' the bridge only"
    echo "delivers mail -- no chat can reply or send."
  fi
fi

echo
echo "Stored in the keyring under service '${SERVICE}'."
echo
echo "Test it:        ./mailbot.py --test"
echo "One sweep:      ./mailbot.py --once"
echo "Two-way:        ./mailbot.py --watch     (mail out, commands back)"
echo
echo "Add a recipient later:  $0 --add-chat <id>"
echo "Choose the owner:       $0 --set-owner <id>"
echo "List them:              $0 --list-chats"
echo
echo "The first sweep announces nothing: it records where the mailbox is now and"
echo "notifies only about what arrives after."

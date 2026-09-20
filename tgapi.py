#!/usr/bin/env python3
"""Telegram transport, bridge state, and who is allowed to act.

Split out of mailbot.py when the bridge stopped being one-way. Two modules now
talk to Telegram -- mailbot pushes mail out, botops takes instructions back --
and both need the same token handling, the same state file and the same answer
to "is this chat allowed to send mail as the owner". One copy of each.

Nothing here knows what a mail message is. That is deliberate: this is the
layer that must never grow a reason to interpret somebody else's text.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import zmail  # noqa: E402

STATE_PATH = os.path.expanduser("~/.cache/zoho-mail-agent/bridge-state.json")

# Telegram rejects a sendMessage over 4096 characters outright. Leave room for
# the header lines and the HTML entities a subject can expand into.
TELEGRAM_LIMIT = 3500


def log(msg: str):
    """Timestamped, on stderr, so --watch output can be tailed and stdout stays JSON."""
    sys.stderr.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
    sys.stderr.flush()


def load_state() -> dict:
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(state: dict):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    # Atomic replace: a bridge killed mid-write would otherwise come back to a
    # truncated state file, read it as "first run", and re-announce the inbox.
    os.replace(tmp, STATE_PATH)


def telegram(method: str, payload: dict, timeout: int = 30) -> dict:
    token = zmail.keychain("telegram_bot_token")
    url = f"https://api.telegram.org/bot{token}/{method}"
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        detail = e.read().decode()[:400]
        # The token is in the URL, so the URL must never reach a log or an error.
        if e.code in (401, 404):
            zmail.die(
                f"Telegram rejected the bot token (HTTP {e.code}). The token is wrong, "
                f"revoked, or was stored with a stray space. Re-run ./bridge-setup.sh.\n"
                f"Telegram said: {detail}")
        if e.code == 400 and "chat not found" in detail.lower():
            # Recoverable and PER CHAT, so it must not be fatal. One recipient
            # who has not yet written to the bot is the normal state of a
            # freshly added agent; killing the process over it turns a missing
            # handshake into a crash loop for everybody else on the feed.
            raise RuntimeError(
                "chat not found -- the bot cannot open a conversation on its own. "
                "That chat must send the bot any message first.")
        raise RuntimeError(f"Telegram HTTP {e.code}: {detail}")
    except OSError as e:
        raise RuntimeError(f"Telegram unreachable: {e}") from e


def send(chat_id: str, text: str, buttons: list[list[tuple[str, str]]] | None = None,
         reply_to: int | None = None) -> dict:
    """One outbound Telegram message. `buttons` is rows of (label, callback_data)."""
    payload: dict = {
        "chat_id": chat_id,
        "text": text[:TELEGRAM_LIMIT],
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if buttons:
        payload["reply_markup"] = {"inline_keyboard": [
            [{"text": label, "callback_data": data} for label, data in row] for row in buttons]}
    if reply_to is not None:
        payload["reply_to_message_id"] = reply_to
        # The quoted message can be deleted between our reading it and our reply.
        # Without this the whole send fails rather than arriving unquoted.
        payload["allow_sending_without_reply"] = True
    return telegram("sendMessage", payload)


def _ids(raw: str | None) -> list[str]:
    ids = [c.strip() for c in (raw or "").replace(",", " ").split()]
    # Deduplicate while keeping order: the same chat listed twice would receive
    # every mail twice, and the second copy looks like a bridge malfunction.
    return list(dict.fromkeys(c for c in ids if c))


def recipients(required: bool = True) -> list[str]:
    """The chat ids the mail feed is delivered to, in configured order."""
    ids = _ids(zmail.keychain("telegram_chat_id", required=False))
    if not ids and required:
        zmail.die("no telegram_chat_id configured; run ./bridge-setup.sh")
    return ids


def owners() -> list[str]:
    """The chat ids allowed to act -- to reply, send, and drive the job loop.

    Receiving the feed and commanding the mailbox are different powers, and the
    bridge was built to feed more than one agent. An observer that could also
    send would be able to write as the mailbox owner to anyone, which is not
    what "add this chat to the feed" ever meant.

    With exactly one recipient the distinction is empty, so that recipient is
    the owner -- it is the chat of whoever ran bridge-setup.sh. With several
    recipients and no explicit choice, nobody is an owner: guessing which of
    them may send mail is the one thing this function must not do.
    """
    explicit = _ids(zmail.keychain("telegram_owner_id", required=False))
    if explicit:
        return explicit
    feed = recipients(required=False)
    return feed if len(feed) == 1 else []


def is_owner(chat_id: str) -> bool:
    return str(chat_id) in owners()

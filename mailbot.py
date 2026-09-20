#!/usr/bin/env python3
"""Run the mailbox from a Telegram chat: new mail out, instructions back.

Outbound, this file: every new message in the Inbox arrives in the chat with a
preview and a keyboard. Inbound, botops.py: the buttons and commands come back
as replies, searches and sends. `--watch` drives both in one process, so the
bridge is a conversation rather than a feed.

Nothing is ever sent on this bridge without the owner pressing send. The draft
is written for them, they may rewrite it, and only then does it leave. See the
header of botops.py for why that ordering is the whole defence.

Why polling and not IMAP IDLE: Zoho disables IMAP/POP for Free-plan accounts
created after 2024, so on the mailbox this was built against IDLE is not merely
slower to set up -- it is unavailable. Polling the REST API reuses the OAuth
token that is already here, which means this bridge adds no new mailbox
credential to the machine. The cost is latency: new mail lands in the chat within
one poll interval (default 30s) rather than instantly.

State lives in a small JSON file, so a restart does not re-announce mail you have
already seen, and the FIRST run announces nothing at all -- it records where the
mailbox is and starts watching from there. An alert bridge whose debut is 200
notifications for old mail gets muted within the minute, and a muted bridge is
worse than no bridge: it looks like it is working.

    ./mailbot.py --once      one sweep, then exit      (for cron / launchd)
    ./mailbot.py --watch     stay running: mail out, commands back
    ./mailbot.py --feed-only with --watch, push mail but ignore every command
    ./mailbot.py --test      send one probe message and exit
    ./mailbot.py --status    what is configured, without secrets

Message bodies are other people's text. This bridge forwards a PREVIEW and never
acts on what it reads; the agent on the other side must treat it the same way.
"""
from __future__ import annotations

import argparse
import html
import json
import os
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import botops  # noqa: E402  -- the inbound half
import tgapi  # noqa: E402   -- transport, state, and who may act
import zmail  # noqa: E402  -- same directory, one implementation of the mail API
from tgapi import load_state, log, recipients, save_state, telegram  # noqa: E402

INBOX_TYPE = "Inbox"
DEFAULT_INTERVAL = 30
PREVIEW_CHARS = 400
TELEGRAM_LIMIT = tgapi.TELEGRAM_LIMIT


def inbox_folder_id(aid: str) -> str:
    for d in (zmail.api(f"/accounts/{aid}/folders").get("data") or []):
        if d.get("folderType") == INBOX_TYPE:
            return d["folderId"]
    zmail.die("no Inbox folder found on this account")


def render(m: dict) -> str:
    """One notification. HTML-escaped, because a subject line is a stranger's text.

    Telegram parses <b>/<i>/<a> in HTML mode, so an unescaped subject containing a
    tag would break the message or, worse, inject a link. Escape first, add markup
    after -- never the other way round.
    """
    sender = html.escape(zmail.strip_html(m.get("from") or "(no sender)"))
    subject = html.escape(zmail.strip_html(m.get("subject") or "(no subject)"))
    when = html.escape(m.get("received") or "")

    body = ""
    try:
        raw = (zmail.api(
            f"/accounts/{zmail.account_id()}/folders/{m['folderId']}"
            f"/messages/{m['messageId']}/content").get("data") or {}).get("content") or ""
        text = zmail.strip_html(raw).strip()
        if text:
            clipped = text[:PREVIEW_CHARS]
            body = html.escape(clipped) + ("…" if len(text) > PREVIEW_CHARS else "")
    except SystemExit:
        # zmail.die() raises SystemExit. A body that will not fetch is not a reason
        # to drop the whole notification -- the headers are the part that matters.
        body = "<i>(body unavailable)</i>"

    parts = [f"📬 <b>{subject}</b>", f"от {sender}"]
    if when:
        parts.append(f"<i>{when}</i>")
    if body:
        parts += ["", body]
    parts += ["", f"<code>{html.escape(str(m['messageId']))}</code>"]

    out = "\n".join(parts)
    return out[:TELEGRAM_LIMIT] if len(out) > TELEGRAM_LIMIT else out


def notify(chat_id: str, text: str, mid: str | None = None):
    """Deliver one notification, with the keyboard that makes it actionable.

    The buttons are attached for every recipient, not only owners: an observer
    who presses one is told the feed is read-only, which is a clearer answer
    than a message that silently has no buttons on one phone and buttons on
    another. The authority check lives in botops, at the point of action.
    """
    payload: dict = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if mid is not None:
        payload["reply_markup"] = {"inline_keyboard": [[
            {"text": "✍️ Ответить", "callback_data": f"r|{mid}"},
            {"text": "📄 Полностью", "callback_data": f"b|{mid}"},
        ]]}
    telegram("sendMessage", payload)


def migrate_seen(state: dict, chats: list[str]) -> dict:
    """Return the per-recipient seen map, upgrading the old single-chat format.

    Deliveries are tracked per chat rather than per message. With one recipient
    the two are the same thing; with several they are not, and a shared set
    makes a partial failure unrecoverable -- retrying the message would deliver
    a second copy to every chat that already had it.
    """
    seen = state.get("seen")
    if isinstance(seen, list):
        # The old format recorded one flat set for the single configured chat.
        # Every currently configured chat inherits it as its horizon, which is
        # right for the original recipient and safe for any added alongside it.
        return {c: list(seen) for c in chats}
    return dict(seen or {})


def sweep(aid: str, folder: str, state: dict, limit: int = 25) -> int:
    """One poll. Returns how many notifications went out, across all recipients."""
    data = zmail.api(f"/accounts/{aid}/messages/view",
                     {"limit": limit, "start": 1, "folderId": folder}).get("data") or []
    msgs = [zmail.summary(m) | {"folderId": folder} for m in data]
    ids_now = [str(m["messageId"]) for m in msgs]

    chats = recipients()
    seen = migrate_seen(state, chats)

    # A recipient with no horizon has never been watched -- either this is the
    # first run, or a chat was added to an already-running bridge. Both get the
    # same treatment: record where the mailbox is, announce nothing. Flooding a
    # newly connected agent with the existing inbox is how a bridge gets muted.
    fresh_chats = [c for c in chats if c not in seen]
    for c in fresh_chats:
        seen[c] = list(ids_now)
        log(f"chat {c}: {len(ids_now)} existing messages marked as seen, watching from here")

    state["initialised"] = True

    # render() fetches the message body, so it is memoised for the sweep: two
    # recipients are two deliveries of one notification, not two fetches of the
    # same mail.
    rendered: dict[str, str] = {}

    def text_for(m: dict) -> str:
        key = str(m["messageId"])
        if key not in rendered:
            rendered[key] = render(m)
        return rendered[key]

    sent = 0
    for chat in chats:
        if chat in fresh_chats:
            continue
        known = set(seen[chat])
        # Oldest first, so a burst arrives in the chat in the order it arrived in
        # the mailbox rather than reversed.
        fresh = [m for m in reversed(msgs) if str(m["messageId"]) not in known]
        for m in fresh:
            try:
                notify(chat, text_for(m), str(m["messageId"]))
            except RuntimeError as e:
                # Network blip: leave it unseen so the next sweep retries it for
                # THIS chat only. Marking it seen here would silently drop the
                # one mail the user needed.
                log(f"deferred {m['messageId']} for chat {chat}: {e}")
                continue
            known.add(str(m["messageId"]))
            sent += 1
            log(f"notified chat {chat}: {m.get('subject')!r} from {m.get('from')}")
        # Bounded: the set only needs to cover what a `view` call can return, and
        # an unbounded list would grow until the state file is the slowest part
        # of a sweep.
        seen[chat] = list(known)[-500:]

    # A chat removed from the configuration keeps no state: leaving it would
    # silently resume mid-stream if it were ever re-added, skipping everything
    # that arrived while it was disconnected.
    state["seen"] = {c: seen[c] for c in chats}
    state["last_sweep"] = time.strftime("%Y-%m-%d %H:%M:%S")
    save_state(state)
    return sent


def main():
    ap = argparse.ArgumentParser(prog="mailbot", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--once", action="store_true", help="one sweep, then exit")
    g.add_argument("--watch", action="store_true", help="poll until stopped")
    g.add_argument("--test", action="store_true", help="send one probe message and exit")
    g.add_argument("--status", action="store_true", help="show configuration, no secrets")
    g.add_argument("--reset", action="store_true", help="forget state; next run re-baselines")
    ap.add_argument("-i", "--interval", type=int, default=DEFAULT_INTERVAL,
                    help=f"seconds between mail polls with --watch (default {DEFAULT_INTERVAL})")
    ap.add_argument("--feed-only", action="store_true",
                    help="with --watch: push mail, ignore every incoming command")
    a = ap.parse_args()

    if a.status:
        state = load_state()
        chats = tgapi.recipients(required=False)
        seen = state.get("seen")
        # A chat id is not a secret -- it is the address of a conversation this
        # machine already delivers to -- and with several recipients the whole
        # point of --status is telling which of them are actually wired up.
        per_chat = (
            {c: len(seen.get(c) or []) for c in chats} if isinstance(seen, dict)
            else {c: len(seen or []) for c in chats}
        )
        json.dump({
            "bot_token_configured": zmail.keychain("telegram_bot_token", required=False) is not None,
            "recipients": chats,
            "recipient_count": len(chats),
            "keychain_service": zmail.BRIDGE_SERVICE,
            "state_path": tgapi.STATE_PATH,
            "initialised": bool(state.get("initialised")),
            "seen_count_per_chat": per_chat,
            "state_format": "per-chat" if isinstance(seen, dict) else "legacy-single-chat",
            "last_sweep": state.get("last_sweep"),
            "poll_interval_sec": a.interval,
            # The two-way half. `owners` is who may reply and send; everyone
            # else on the feed can only read. Empty with several recipients and
            # no explicit choice means nobody can act -- which is a setup step
            # left undone, not a malfunction, and worth seeing here.
            "owners": tgapi.owners(),
            "commands_enabled": not a.feed_only,
            "draft_engine": botops.draft_engine(),
            "job_cli": shutil.which("tri"),
            "drafts_pending": len(state.get("drafts") or {}),
            "telegram_offset": state.get("tg_offset", 0),
        }, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
        return

    if a.reset:
        try:
            os.remove(tgapi.STATE_PATH)
            log("state cleared; the next run re-baselines and announces nothing")
        except FileNotFoundError:
            log("no state to clear")
        return

    if a.test:
        # Probe every recipient, and do not stop at the first failure: with
        # several chats configured, "the probe failed" is useless unless it says
        # which chat failed. A chat that has not started the bot yet is the
        # normal cause, and it is per-chat.
        ok, bad = [], []
        for chat in recipients():
            try:
                role = "вы владелец: можно отвечать и отправлять" if tgapi.is_owner(chat) \
                    else "лента только для чтения"
                telegram("sendMessage", {
                    "chat_id": chat,
                    "text": ("✅ <b>Мост почты подключён.</b>\n"
                             "Новые письма на admin@t27.ai будут приходить сюда.\n"
                             f"<i>{role}</i>\n\n/help — что умеет мост."),
                    "parse_mode": "HTML",
                })
                ok.append(chat)
            except (RuntimeError, SystemExit) as e:
                bad.append((chat, str(e).strip().splitlines()[0] if str(e).strip() else "failed"))
        for chat in ok:
            log(f"probe delivered to chat {chat}")
        for chat, why in bad:
            log(f"probe FAILED for chat {chat}: {why}")
        if bad:
            sys.exit(1)
        return

    aid = zmail.account_id()
    folder = inbox_folder_id(aid)

    if a.once:
        sweep(aid, folder, load_state())
        return

    if a.feed_only:
        log(f"watching Inbox every {a.interval}s, commands disabled; Ctrl-C to stop")
    else:
        owners = tgapi.owners()
        who = ", ".join(owners) if owners else \
            "NOBODY -- run ./bridge-setup.sh --set-owner <id>"
        log(f"watching Inbox every {a.interval}s; commands accepted from {who}")

    # One loop, two clocks. Telegram is long-polled, which costs nothing while
    # idle and makes a button press feel immediate; the mailbox is polled on the
    # slower interval because Zoho gives no push. Sharing one thread keeps a
    # single state file writer, so a draft and a sweep can never race.
    last = 0.0
    while True:
        state = load_state()
        if time.monotonic() - last >= a.interval:
            try:
                sweep(aid, folder, state)
            except SystemExit:
                raise
            except Exception as e:  # noqa: BLE001 -- a watcher that dies on a blip is useless
                log(f"sweep failed, retrying next interval: {e}")
            last = time.monotonic()

        if a.feed_only:
            time.sleep(min(a.interval, 5))
            continue
        try:
            botops.poll(aid, folder, state, timeout=max(5, min(25, a.interval)))
        except SystemExit:
            raise
        except Exception as e:  # noqa: BLE001
            log(f"command poll failed, continuing: {e}")
            time.sleep(5)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("stopped")

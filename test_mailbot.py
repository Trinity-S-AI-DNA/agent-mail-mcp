#!/usr/bin/env python3
"""Offline tests for the bridge fan-out. Standard library only: `python3 test_mailbot.py`.

Everything here runs against stubs -- no mailbox, no Telegram, no keychain. The
cases that matter are the ones a single-recipient bridge could not have: a chat
added to a running bridge, and a delivery that fails for one recipient while
succeeding for another.
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import mailbot  # noqa: E402
import zmail  # noqa: E402

mailbot.STATE_PATH = os.path.join(tempfile.mkdtemp(), "state.json")

CHATS = "111"
MSGS: list[dict] = []
BODY_FETCHES: list[str] = []
SENT: list[str] = []
FAIL: set[str] = set()

zmail.keychain = lambda k, required=True: {
    "telegram_chat_id": CHATS, "telegram_bot_token": "T"}.get(k)
zmail.account_id = lambda: "acct"
zmail.summary = lambda m: dict(m)
zmail.strip_html = lambda s: s


def _api(path, query=None):
    if "/content" in path:
        BODY_FETCHES.append(path)
        return {"data": {"content": "body text"}}
    return {"data": list(MSGS)}


def _telegram(method, payload):
    chat = str(payload["chat_id"])
    if chat in FAIL:
        raise RuntimeError("simulated outage")
    SENT.append(chat)
    return {"ok": True}


zmail.api = _api
mailbot.telegram = _telegram

results: list[bool] = []


def check(label, cond, detail=""):
    results.append(bool(cond))
    print(f"{' ok ' if cond else 'FAIL'}  {label}{'  — ' + str(detail) if detail else ''}")


def mail(i):
    return {"messageId": i, "subject": f"s{i}", "from": "x@y", "received": "", "folderId": "f"}


def sweep():
    SENT.clear()
    BODY_FETCHES.clear()
    n = mailbot.sweep("acct", "f", mailbot.load_state())
    return n, sorted(SENT)


def main():
    global CHATS, MSGS, FAIL

    CHATS, MSGS = "111 222", [mail(1), mail(2)]
    n, to = sweep()
    check("first run announces nothing", n == 0 and to == [], f"sent={n}")
    check("both chats got a horizon", set(mailbot.load_state()["seen"]) == {"111", "222"})

    MSGS = [mail(3)] + MSGS
    n, to = sweep()
    check("new mail reaches both chats once", n == 2 and to == ["111", "222"], to)
    check("body is fetched once for two recipients", len(BODY_FETCHES) == 1, f"{len(BODY_FETCHES)} fetches")

    CHATS = "111 222 333"
    n, to = sweep()
    check("a chat added later is baselined, not flooded", n == 0 and to == [], f"sent={n}")
    MSGS = [mail(4)] + MSGS
    n, to = sweep()
    check("after baselining the added chat receives fresh mail", to == ["111", "222", "333"], to)

    FAIL, MSGS = {"222"}, [mail(5)] + MSGS
    n, to = sweep()
    check("a failing chat does not block the others", to == ["111", "333"], to)
    FAIL = set()
    n, to = sweep()
    check("the retry goes only to the chat that failed", to == ["222"], to)

    mailbot.save_state({"initialised": True, "seen": ["9", "8"], "last_sweep": "x"})
    CHATS, MSGS = "111 222", [mail(9), mail(8)]
    n, to = sweep()
    check("a legacy flat list migrates to a per-chat horizon",
          n == 0 and set(mailbot.load_state()["seen"]) == {"111", "222"}, f"sent={n}")

    CHATS, MSGS = "111,111, 222", [mail(10)] + MSGS
    n, to = sweep()
    check("a duplicated chat id delivers once", to == ["111", "222"], to)

    CHATS, MSGS = "111", [mail(11)] + MSGS
    sweep()
    check("removing a chat drops its state",
          set(mailbot.load_state()["seen"]) == {"111"}, list(mailbot.load_state()["seen"]))

    print()
    print("all passed" if all(results) else f"{results.count(False)} failed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())

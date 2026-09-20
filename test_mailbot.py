#!/usr/bin/env python3
"""Offline tests for the bridge: `python3 test_mailbot.py`.

Everything here runs against stubs -- no mailbox, no Telegram, no keychain, no
drafting model. Two halves:

  fan-out     what a single-recipient bridge could not get wrong: a chat added
              to a running bridge, and a delivery that fails for one recipient
              while succeeding for another.

  two-way     the path that can put a letter in a stranger's inbox. The cases
              that matter are the refusals -- an observer pressing send, a send
              without the scope, an empty draft -- because the happy path is
              the one anybody would test.
"""
from __future__ import annotations

import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import botops  # noqa: E402
import mailbot  # noqa: E402
import tgapi  # noqa: E402
import zmail  # noqa: E402

tgapi.STATE_PATH = os.path.join(tempfile.mkdtemp(), "state.json")

KEYS = {"telegram_chat_id": "111", "telegram_bot_token": "T", "telegram_owner_id": None}
MSGS: list[dict] = []
BODY_FETCHES: list[str] = []
SENT: list[str] = []          # telegram deliveries, by chat id
FAIL: set[str] = set()        # chats whose delivery raises
OUT: list[tuple[str, str]] = []   # (chat, text) from botops.send
MAILED: list[dict] = []       # what reached zmail.send_mail
SCOPE = ["ZohoMail.messages.READ " + zmail.SEND_SCOPE]

zmail.keychain = lambda k, required=True: KEYS.get(k)
zmail.account_id = lambda: "acct"
zmail.summary = lambda m: dict(m)
zmail.strip_html = lambda s: s
zmail.cached_scope = lambda: SCOPE[0]
zmail.normalise_search = lambda q: q


def _api(path, query=None):
    if "/content" in path:
        BODY_FETCHES.append(path)
        return {"data": {"content": "body text"}}
    if path == "/accounts":
        return {"data": [{"primaryEmailAddress": "admin@t27.ai"}]}
    return {"data": list(MSGS)}


def _telegram(method, payload, timeout=30):
    if method != "sendMessage":
        return {"ok": True}
    chat = str(payload["chat_id"])
    if chat in FAIL:
        raise RuntimeError("simulated outage")
    SENT.append(chat)
    return {"ok": True}


def _send(chat, text, buttons=None, reply_to=None):
    OUT.append((str(chat), text))
    return {"ok": True}


def _send_mail(aid, **kw):
    MAILED.append(kw)
    return {"sent": True, "messageId": "999"}


zmail.api = _api
zmail.send_mail = _send_mail
tgapi.telegram = _telegram
mailbot.telegram = _telegram       # mailbot holds its own reference
botops.send = _send
botops.compose_draft = lambda m, body, frm: ("предложенный ответ", "stub")

results: list[bool] = []


def check(label, cond, detail=""):
    results.append(bool(cond))
    print(f"{' ok ' if cond else 'FAIL'}  {label}{'  — ' + str(detail) if detail else ''}")


def mail(i):
    return {"messageId": i, "subject": f"s{i}", "from": f"Someone <s{i}@ex.com>",
            "received": "", "folderId": "f"}


def sweep():
    SENT.clear()
    BODY_FETCHES.clear()
    n = mailbot.sweep("acct", "f", tgapi.load_state())
    return n, sorted(SENT)


def msg(chat, text):
    OUT.clear()
    st = tgapi.load_state()
    botops.handle_message({"chat": {"id": chat}, "text": text}, "acct", "f", st)
    return OUT[:]


def press(chat, data):
    OUT.clear()
    st = tgapi.load_state()
    botops.handle_callback({"id": "cb", "data": data, "message": {"chat": {"id": chat}}},
                           "acct", "f", st)
    return OUT[:]


def only_draft() -> str:
    drafts = tgapi.load_state().get("drafts") or {}
    return next(iter(drafts))


# ------------------------------------------------------------------ fan-out

def test_fanout():
    global MSGS, FAIL

    KEYS["telegram_chat_id"], MSGS = "111 222", [mail(1), mail(2)]
    n, to = sweep()
    check("first run announces nothing", n == 0 and to == [], f"sent={n}")
    check("both chats got a horizon", set(tgapi.load_state()["seen"]) == {"111", "222"})

    MSGS = [mail(3)] + MSGS
    n, to = sweep()
    check("new mail reaches both chats once", n == 2 and to == ["111", "222"], to)
    check("body is fetched once for two recipients", len(BODY_FETCHES) == 1,
          f"{len(BODY_FETCHES)} fetches")

    KEYS["telegram_chat_id"] = "111 222 333"
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

    tgapi.save_state({"initialised": True, "seen": ["9", "8"], "last_sweep": "x"})
    KEYS["telegram_chat_id"], MSGS = "111 222", [mail(9), mail(8)]
    n, to = sweep()
    check("a legacy flat list migrates to a per-chat horizon",
          n == 0 and set(tgapi.load_state()["seen"]) == {"111", "222"}, f"sent={n}")

    KEYS["telegram_chat_id"], MSGS = "111,111, 222", [mail(10)] + MSGS
    n, to = sweep()
    check("a duplicated chat id delivers once", to == ["111", "222"], to)

    KEYS["telegram_chat_id"], MSGS = "111", [mail(11)] + MSGS
    sweep()
    check("removing a chat drops its state",
          set(tgapi.load_state()["seen"]) == {"111"}, list(tgapi.load_state()["seen"]))


# ------------------------------------------------------------------ authority

def test_authority():
    KEYS["telegram_chat_id"], KEYS["telegram_owner_id"] = "111", None
    check("a lone recipient is the owner", tgapi.owners() == ["111"], tgapi.owners())

    KEYS["telegram_chat_id"] = "111 222"
    check("with two recipients and no choice, nobody may send", tgapi.owners() == [],
          tgapi.owners())

    KEYS["telegram_owner_id"] = "111"
    check("an explicit owner is honoured", tgapi.owners() == ["111"] and
          tgapi.is_owner("111") and not tgapi.is_owner("222"))

    out = msg("222", "/inbox")
    check("an observer's command is refused, not executed",
          len(out) == 1 and "только для чтения" in out[0][1], out)

    out = msg("999", "/inbox")
    check("an unknown chat gets no answer at all", out == [], out)

    out = press("222", "r|3")
    check("an observer pressing Reply is refused",
          len(out) == 1 and "только для чтения" in out[0][1], out)


# ------------------------------------------------------------------ reply flow

def test_reply_flow():
    global MSGS
    KEYS["telegram_chat_id"], KEYS["telegram_owner_id"] = "111 222", "111"
    MSGS = [mail(7)]
    MAILED.clear()

    press("111", "r|7")
    st = tgapi.load_state()
    check("pressing Reply creates exactly one draft", len(st.get("drafts") or {}) == 1,
          st.get("drafts"))
    tok = only_draft()
    d = st["drafts"][tok]
    check("the draft is addressed from the letter, not guessed",
          d["to"] == "s7@ex.com" and d["subject"] == "Re: s7" and d["reply_to"] == "7", d)
    check("nothing was mailed by drafting", MAILED == [], MAILED)

    out = press("111", f"e|{tok}")
    check("Correct asks for replacement text", "Пришлите текст" in out[0][1], out)
    msg("111", "мой собственный текст")
    d = tgapi.load_state()["drafts"][tok]
    check("the owner's text replaces the draft body",
          d["body"] == "мой собственный текст" and d["engine"] == "ваш текст", d)

    press("111", f"s|{tok}")
    check("send goes out once, as a reply to the original",
          len(MAILED) == 1 and MAILED[0]["reply_to"] == "7"
          and MAILED[0]["content"] == "мой собственный текст", MAILED)
    check("a sent draft is gone", not (tgapi.load_state().get("drafts") or {}),
          tgapi.load_state().get("drafts"))

    out = press("111", f"s|{tok}")
    check("pressing send twice does not send twice",
          len(MAILED) == 1 and "устарел" in out[0][1], (len(MAILED), out))


def test_send_refusals():
    global MSGS
    MSGS = [mail(7)]
    MAILED.clear()

    press("111", "r|7")
    tok = only_draft()
    st = tgapi.load_state()
    st["drafts"][tok]["body"] = "   "
    tgapi.save_state(st)
    out = press("111", f"s|{tok}")
    check("an empty draft is not sent", MAILED == [] and "пуст" in out[0][1], out)

    st = tgapi.load_state()
    st["drafts"][tok]["body"] = "текст"
    tgapi.save_state(st)
    SCOPE[0] = "ZohoMail.messages.READ"
    out = press("111", f"s|{tok}")
    check("without the send scope nothing is transmitted",
          MAILED == [] and zmail.SEND_SCOPE in out[0][1], out)
    check("and the draft survives the refusal", tok in (tgapi.load_state().get("drafts") or {}))
    SCOPE[0] = "ZohoMail.messages.READ " + zmail.SEND_SCOPE

    press("111", f"x|{tok}")
    check("Cancel discards the draft and sends nothing",
          MAILED == [] and tok not in (tgapi.load_state().get("drafts") or {}))

    press("111", "r|404")
    check("replying to a message that is not there creates no draft",
          not (tgapi.load_state().get("drafts") or {}), tgapi.load_state().get("drafts"))


def test_draft_hygiene():
    st = tgapi.load_state()
    st["drafts"] = {"old": {"created": time.time() - botops.DRAFT_TTL - 60, "body": "x"}}
    tok = botops.new_draft(st, to="a@b", subject="s", body="")
    check("a stale draft is pruned when a new one is made",
          "old" not in st["drafts"] and tok in st["drafts"], list(st["drafts"]))


def test_poll_offset():
    """A handler that throws must not wedge the loop on the same update."""
    st = tgapi.load_state()
    st["tg_offset"] = 0
    tgapi.save_state(st)
    boom = botops.handle_message

    def explode(*_a, **_k):
        raise ValueError("handler blew up")

    botops.handle_message = explode
    tgapi.telegram = lambda m, p, timeout=30: (
        {"result": [{"update_id": 41, "message": {"chat": {"id": "111"}, "text": "/help"}}]}
        if m == "getUpdates" else {"ok": True})
    try:
        n = botops.poll("acct", "f", st)
    finally:
        botops.handle_message = boom
        tgapi.telegram = _telegram
    check("a failing update is counted and stepped past",
          n == 1 and tgapi.load_state()["tg_offset"] == 42,
          tgapi.load_state().get("tg_offset"))


def main():
    test_fanout()
    print()
    test_authority()
    print()
    test_reply_flow()
    test_send_refusals()
    test_draft_hygiene()
    test_poll_offset()
    print()
    print("all passed" if all(results) else f"{results.count(False)} of {len(results)} failed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())

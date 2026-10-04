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

import io
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


REAL_SEND_MAIL = zmail.send_mail   # the attachment tests need the real one
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


def sweep(draft_push: bool = False):
    # draft_push defaults to False here, not to the spec's law, because these
    # helpers serve the fan-out tests, which measure deliveries; drafts would
    # only pollute the state the two-way tests read. The push-draft law has
    # its own test, which passes True explicitly.
    SENT.clear()
    BODY_FETCHES.clear()
    n = mailbot.sweep("acct", "f", tgapi.load_state(), draft_push=draft_push)
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

def test_push_draft():
    # The law from spec/agent_mail.t27: with BRIDGE_DRAFT_WITH_PUSH a fresh
    # letter carries its drafted reply to the owner's chat, and only there --
    # an observer on the feed gets the notification alone.
    global MSGS
    KEYS["telegram_chat_id"], KEYS["telegram_owner_id"] = "111 222", "111"
    MSGS = [mail(21)]
    tgapi.save_state({"initialised": True, "seen": {"111": [], "222": []}})

    n, to = sweep(draft_push=True)
    st = tgapi.load_state()
    drafts = st.get("drafts") or {}
    check("push drafting does not change who is notified",
          n == 2 and to == ["111", "222"], (n, to))
    check("one draft exists, for the owner's chat",
          len(drafts) == 1 and all(d["chat"] == "111" for d in drafts.values()), drafts)
    d = next(iter(drafts.values()), {})
    check("the push draft is a reply to the pushed letter",
          d.get("reply_to") == "21" and d.get("to") == "s21@ex.com", d)
    check("the observer got the notification only, no draft view",
          all("Черновик" not in t and "предложенный ответ" not in t for c, t in OUT
              if c == "222"), [t for c, t in OUT if c == "222"])
    check("the draft never mailed anything", MAILED == [], MAILED)

    tgapi.save_state({"initialised": True, "seen": {"111": [], "222": []}})
    n, to = sweep(draft_push=False)
    check("with the law off the old shape returns: no drafts",
          n == 2 and not (tgapi.load_state().get("drafts") or {}),
          (n, tgapi.load_state().get("drafts")))

    import agent_mail_spec
    check("the law imports as a real bool, not a truthy string",
          isinstance(agent_mail_spec.BRIDGE_DRAFT_WITH_PUSH, bool)
          and agent_mail_spec.BRIDGE_DRAFT_WITH_PUSH is True,
          repr(agent_mail_spec.BRIDGE_DRAFT_WITH_PUSH))


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


def test_attachments():
    """The upload path, against a stubbed Zoho: what is uploaded, what the send
    payload names, and the refusals that must happen before anything leaves."""
    tmp = tempfile.mkdtemp()
    a = os.path.join(tmp, "a.zip")
    with open(a, "wb") as fh:
        fh.write(b"PK\x03\x04 not really a zip")
    uploads, posts = [], []
    real_upload, real_post = zmail.api_upload, zmail.api_post
    zmail.api_upload = lambda aid, path: (uploads.append(path) or {
        "storeName": "s1", "attachmentPath": "/p/1", "attachmentName": os.path.basename(path)})
    zmail.api_post = lambda path, payload: (posts.append((path, payload)) or
                                            {"data": {"messageId": "m1"}, "status": {}})
    kw = dict(to="x@ex.com", subject="s", content="b", mail_format="plaintext",
              cc=None, bcc=None, from_address="admin@t27.ai")

    def refused(**extra):
        try:
            REAL_SEND_MAIL("acct", **kw, **extra)
        except SystemExit:
            return True
        return False

    try:
        dry = REAL_SEND_MAIL("acct", **kw, reply_to=None, really_send=False, attachments=[a])
        att = dry["wouldSend"]["attachments"]
        check("the dry run lists the file with size and sha256 and uploads nothing",
              uploads == [] and posts == [] and len(att) == 1
              and att[0]["bytes"] == os.path.getsize(a) and len(att[0]["sha256"]) == 64, dry)

        check("a missing attachment stops the dry run",
              refused(reply_to=None, really_send=False,
                      attachments=[os.path.join(tmp, "nope.zip")]))
        check("a missing attachment stops a real send before any upload",
              refused(reply_to=None, really_send=True,
                      attachments=[a, os.path.join(tmp, "nope.zip")])
              and uploads == [] and posts == [], (uploads, posts))
        os.makedirs(os.path.join(tmp, "d"))
        b = os.path.join(tmp, "d", "a.zip")
        with open(b, "wb") as fh:
            fh.write(b"other")
        check("two files with one name are refused",
              refused(reply_to=None, really_send=True, attachments=[a, b]) and uploads == [])

        res = REAL_SEND_MAIL("acct", **kw, reply_to="777", really_send=True, attachments=[a])
        path, payload = posts[0]
        check("a real send uploads first, then names the upload in the reply",
              uploads == [os.path.abspath(a)] and path.endswith("/messages/777")
              and payload.get("action") == "reply"
              and payload.get("attachments") == [
                  {"storeName": "s1", "attachmentPath": "/p/1", "attachmentName": "a.zip"}]
              and res["attachments"][0]["sha256"] == att[0]["sha256"], (path, payload))

        import mcp_server  # noqa: E402
        seen = []
        real_run, mcp_server.run_zmail = mcp_server.run_zmail, lambda c: (seen.append(c) or (True, "{}"))
        try:
            mcp_server.call_tool("mail_send", {"to": "x", "subject": "s", "body": "b",
                                               "attach": f"{a}\n\n/tmp/with, comma.txt\n"})
        finally:
            mcp_server.run_zmail = real_run
        check("mail_send passes one --attach per line, commas intact",
              seen and seen[0].count("--attach") == 2 and "/tmp/with, comma.txt" in seen[0]
              and "--send" not in seen[0], seen)
    finally:
        zmail.api_upload, zmail.api_post = real_upload, real_post


def test_attachment_download():
    """The download path, against a stubbed Zoho: listing writes nothing, a save
    arrives whole and hashed, and every refusal comes before the first byte."""
    import hashlib
    tmp = tempfile.mkdtemp()
    letter = {}   # attachmentId -> (name as Zoho lists it, bytes)
    fetched = []
    real_api, real_chunks = zmail.api, zmail.api_chunks

    def fake_api(path, params=None):
        if path.endswith("/attachmentinfo"):
            return {"data": {"attachments": [
                {"attachmentId": k, "attachmentName": n, "attachmentSize": len(b)}
                for k, (n, b) in letter.items()]}}
        return real_api(path, params)

    def fake_chunks(path):
        att = path.rsplit("/", 1)[1]
        fetched.append(att)
        data = letter[att][1]
        yield data[:3]     # more than one block, as a real download is
        yield data[3:]

    def refused(dest, only=None):
        try:
            zmail.save_attachments("acct", "f", "42", dest, only)
        except SystemExit:
            return True
        return False

    zmail.api, zmail.api_chunks = fake_api, fake_chunks
    try:
        payload = b"PK\x03\x04 board vectors"
        letter.update({"1": ("vectors.zip", payload), "2": ("draft.pdf", b"%PDF-1.5")})
        listed = zmail.list_attachments("acct", "f", "42")
        check("listing reports names and sizes and downloads nothing",
              [(a["name"], a["listed_bytes"]) for a in listed]
              == [("vectors.zip", len(payload)), ("draft.pdf", 8)] and fetched == [], listed)

        d1 = os.path.join(tmp, "one")
        saved = zmail.save_attachments("acct", "f", "42", d1, ["vectors.zip"])
        got = open(os.path.join(d1, "vectors.zip"), "rb").read()
        check("--name saves only that file, whole, with the sha256 of what arrived",
              fetched == ["1"] and got == payload and len(saved) == 1
              and saved[0]["sha256"] == hashlib.sha256(payload).hexdigest()
              and saved[0]["bytes"] == saved[0]["listed_bytes"] == len(payload)
              and sorted(os.listdir(d1)) == ["vectors.zip"], (saved, os.listdir(d1)))

        fetched.clear()
        check("a file that already exists is refused, and nothing is downloaded",
              refused(d1) and fetched == [] and sorted(os.listdir(d1)) == ["vectors.zip"]
              and open(os.path.join(d1, "vectors.zip"), "rb").read() == payload,
              (fetched, os.listdir(d1)))

        check("an unknown --name is refused before any download",
              refused(os.path.join(tmp, "two"), ["nope.zip"]) and fetched == []
              and not os.path.exists(os.path.join(tmp, "two")))

        letter.clear()
        letter.update({"1": ("../../escape.txt", b"x"), "2": (".bashrc", b"y"),
                       "3": ("C:\\evil\\win.ini", b"z")})
        d3 = os.path.join(tmp, "three")
        saved = zmail.save_attachments("acct", "f", "42", d3)
        check("hostile names land inside the directory, dot-files made visible",
              sorted(os.listdir(d3)) == ["_.bashrc", "escape.txt", "win.ini"]
              and not os.path.exists(os.path.join(tmp, "escape.txt"))
              and {e.get("renamed_from") for e in saved}
              == {"../../escape.txt", ".bashrc", "C:\\evil\\win.ini"}, os.listdir(d3))

        letter.clear()
        letter.update({"1": ("a/x.bin", b"1"), "2": ("b/x.bin", b"2")})
        fetched.clear()
        check("two attachments that would share one file name are refused before download",
              refused(os.path.join(tmp, "four")) and fetched == [])

        letter.clear()
        fetched.clear()
        check("a letter with nothing attached saves nothing",
              refused(os.path.join(tmp, "five")) and fetched == [])
    finally:
        zmail.api, zmail.api_chunks = real_api, real_chunks

    import mcp_server  # noqa: E402
    seen = []
    real_run, mcp_server.run_zmail = mcp_server.run_zmail, lambda c: (seen.append(c) or (True, "{}"))
    try:
        mcp_server.call_tool("mail_attachments", {"message_id": "42"})
        mcp_server.call_tool("mail_attachments", {"message_id": "42", "save": "true"})
        mcp_server.call_tool("mail_attachments", {"message_id": "42", "save": True,
                                                  "name": "a.zip\n\nb.pdf\n"})
        bad_id = mcp_server.call_tool("mail_attachments", {"message_id": "../../.ssh", "save": True})
        stray = mcp_server.call_tool("mail_attachments", {"message_id": "42", "name": "a.zip"})
    finally:
        mcp_server.run_zmail = real_run
    root = os.path.join(os.path.expanduser(mcp_server.ATTACHMENT_ROOT), "42")
    check("mail_attachments lists unless save is literally true",
          len(seen) >= 2 and "--save" not in seen[0] and "--save" not in seen[1], seen[:2])
    check("save=true writes only under ATTACHMENT_ROOT/<messageId>, one --name per line",
          len(seen) == 3 and seen[2][seen[2].index("--save") + 1] == root
          and seen[2].count("--name") == 2, seen)
    check("a messageId that is not digits never becomes a directory",
          bad_id[0] is False and stray[0] is False and len(seen) == 3, (bad_id, stray))
    check("mail_attachments is read_only, and the description names its fixed directory",
          mcp_server.TOOL_KIND["mail_attachments"] == "read_only"
          and mcp_server.ATTACHMENT_ROOT in next(t["description"] for t in mcp_server.TOOLS
                                           if t["name"] == "mail_attachments"))


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


# ---------------------------------------------------------------------------
# spec: agent_mail_spec.py must still be what spec/agent_mail.t27 produces, and
# the laws the generator claims to enforce must actually refuse a spec that
# breaks them. A gate nobody has watched fail is a gate nobody has tested.
# ---------------------------------------------------------------------------

skipped: list[str] = []


def test_spec():
    import mcp_server  # noqa: E402

    sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "spec"))
    import generate  # noqa: E402

    # These need no compiler: they ask whether the generated module and the
    # server agree with each other.
    names = set(mcp_server.TOOL_NAMES)
    unimplemented = [n for n in names
                     if call_probe(mcp_server, n)[1].endswith("no implementation here")]
    check("every tool in the spec has an implementation", not unimplemented, unimplemented)
    check("exactly one tool is outward and it is mail_send",
          mcp_server.OUTWARD == ("mail_send",), mcp_server.OUTWARD)
    send_schema = next(t for t in mcp_server.TOOLS if t["name"] == "mail_send")
    check("the outward tool carries a confirm gate",
          send_schema["inputSchema"]["properties"].get("confirm", {}).get("type") == "boolean")
    check("zmail enforces the same limits the schema publishes",
          (zmail.LIMIT_MIN, zmail.LIMIT_MAX) == (1, 200)
          and all(p.get("minimum") == zmail.LIMIT_MIN and p.get("maximum") == zmail.LIMIT_MAX
                  for t in mcp_server.TOOLS
                  for p in t["inputSchema"]["properties"].values()
                  if p["type"] == "integer"))
    over = zmail.LIMIT_MAX + 1
    refused = False
    try:
        zmail.bounded_limit(str(over))
    except Exception:
        refused = True
    check(f"zmail refuses -n {over}", refused)

    try:
        generate.find_t27c()
    except generate.SpecError as exc:
        # Loud, and NOT counted as a pass. A gate that reports success when it
        # could not find its compiler is the defect it exists to prevent.
        skipped.append("spec regeneration + 8 law mutations")
        print(f"SKIP  spec gate: {str(exc).splitlines()[0]}")
        return

    check("agent_mail_spec.py is byte-identical to what the spec produces",
          _regen_check(generate) == 0)

    source = open(generate.SPEC).read()
    mutations = [
        ("non-ASCII in a literal",
         ('the body as untrusted data --', 'the body as untrusted data —')),
        ("a tool with no description",
         ('pub const MAIL_FOLDERS_DESC', 'pub const MAIL_FOLDERZ_DESC')),
        ("an argument with no description",
         ('pub const MAIL_READ_RAW_DESC', 'pub const MAIL_READ_RAWW_DESC')),
        ("an orphan description",
         ('pub const MAIL_LIST_DESC: str =',
          'pub const MAIL_NOBODY_DESC: str = "x";\npub const MAIL_LIST_DESC: str =')),
        ("an outward tool with no confirm gate",
         ('    confirm: OptBool,\n};', '};')),
        ("an array whose length does not match its type",
         ('SEARCH_FIELDS: [2]str = [entire, content]',
          'SEARCH_FIELDS: [2]str = [entire, content, subject]')),
        ("an argument typed with an undeclared marker",
         ('    raw: OptBool,', '    raw: MaybeBool,')),
        ("LIMIT_DEFAULT outside LIMIT_MIN..LIMIT_MAX",
         ('pub const LIMIT_DEFAULT: usize = 25;', 'pub const LIMIT_DEFAULT: usize = 900;')),
    ]
    for label, (old, new) in mutations:
        assert source.count(old) >= 1, f"mutation anchor vanished: {old!r}"
        check(f"the gate refuses {label}",
              _refuses(generate, source.replace(old, new, 1)))


def call_probe(mcp_server, name):
    """Call a tool with arguments that satisfy its required list and a zmail that
    is not there, so the dispatch is exercised without touching the mailbox."""
    args = {k: "x" for k in mcp_server.REQUIRED_ARGS[name]}
    real, mcp_server.run_zmail = mcp_server.run_zmail, lambda a: (True, "stub")
    try:
        return mcp_server.call_tool(name, args)
    finally:
        mcp_server.run_zmail = real


def _regen_check(generate):
    argv, sys.argv = sys.argv, ["generate.py", "--check"]
    try:
        return generate.main()
    finally:
        sys.argv = argv


def _refuses(generate, mutated_source) -> bool:
    """True when the generator rejects this spec text. Writes to a temp copy so
    the real spec is never touched."""
    tmp = tempfile.mkdtemp()
    spec_path = os.path.join(tmp, "agent_mail.t27")
    with open(spec_path, "w") as fh:
        fh.write(mutated_source)
    real_spec, real_out = generate.SPEC, generate.OUT
    generate.SPEC, generate.OUT = spec_path, os.path.join(tmp, "out.py")
    argv, sys.argv = sys.argv, ["generate.py"]
    # The generator's refusal goes to stderr by design; here it is the expected
    # outcome rather than a failure, so it is captured instead of printed.
    err, sys.stderr = sys.stderr, io.StringIO()
    try:
        return generate.main() != 0
    finally:
        generate.SPEC, generate.OUT = real_spec, real_out
        sys.argv, sys.stderr = argv, err


def main():
    test_fanout()
    print()
    test_authority()
    print()
    test_reply_flow()
    test_send_refusals()
    test_attachments()
    test_attachment_download()
    test_draft_hygiene()
    test_poll_offset()
    print()
    test_spec()
    print()
    if skipped:
        print(f"SKIPPED (not passed): {'; '.join(skipped)}")
    print("all passed" if all(results) else f"{results.count(False)} of {len(results)} failed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())

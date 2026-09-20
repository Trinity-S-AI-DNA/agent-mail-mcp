#!/usr/bin/env python3
"""Read-only Zoho Mail access for agents. No browser, no UI, no password in sight.

Credentials live in the macOS Keychain under service `zoho-mail-agent` and are read
on demand; nothing is written to this repo and nothing is echoed to stdout. The OAuth
scopes are read-only by construction (see README.md), so an agent holding
this tool cannot send, move or delete mail even if it wants to.

Commands
  zmail accounts                     account ids and addresses
  zmail folders                      folder list with ids and unread counts
  zmail list [-n N] [-f FOLDER]      newest N message summaries (default 25)
  zmail read <messageId> [-F fid]    one message, headers plus plain-text body
  zmail search <query> [-n N]        Zoho search syntax, newest first
  zmail whoami                       token state, no secrets

Every command prints JSON on stdout. Errors go to stderr with a non-zero exit, so a
caller can branch on the exit code without parsing prose.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

KEYCHAIN_SERVICE = "zoho-mail-agent"
# Zoho is region-partitioned: a token minted in one DC is rejected by the others, and
# the error for that ("invalid client") reads exactly like a wrong secret. Override
# with ZOHO_REGION=eu|in|com.au|jp if this account ever moves.
REGION = os.environ.get("ZOHO_REGION", "com")
ACCOUNTS_HOST = f"https://accounts.zoho.{REGION}"
API_HOST = f"https://mail.zoho.{REGION}"
CACHE_PATH = os.path.expanduser("~/.cache/zoho-mail-agent/token.json")


def die(msg: str, code: int = 1):
    print(msg, file=sys.stderr)
    raise SystemExit(code)


ENV_VARS = {
    "client_id": "ZOHO_CLIENT_ID",
    "client_secret": "ZOHO_CLIENT_SECRET",
    "refresh_token": "ZOHO_REFRESH_TOKEN",
}


def keychain(account: str) -> str:
    """Fetch one secret. Never logged, never returned in any command's output.

    Three sources, in order: the environment (for containers and CI, where no
    keyring daemon exists), the macOS Keychain, then libsecret on Linux. The
    environment comes first so a deployment can override without clearing a
    keyring it may not even have.
    """
    env = os.environ.get(ENV_VARS[account])
    if env:
        return env.strip()

    if sys.platform == "darwin":
        cmd = ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", account, "-w"]
        store = "macOS Keychain"
    else:
        cmd = ["secret-tool", "lookup", "service", KEYCHAIN_SERVICE, "account", account]
        store = "libsecret (secret-tool)"

    try:
        out = subprocess.run(cmd, capture_output=True, text=True, check=True)
    except FileNotFoundError:
        die(
            f"{store} is not available on this machine.\n"
            f"Either install it, or supply the credential in the environment:\n"
            f"  export {ENV_VARS[account]}=..."
        )
    except subprocess.CalledProcessError:
        die(
            f"no stored credential for {account!r} (service={KEYCHAIN_SERVICE}, {store}).\n"
            # realpath, not abspath: this script is reached through a symlink on PATH,
            # and abspath would point the reader at a README that is not there.
            f"Run ./bootstrap.sh -- see "
            f"{os.path.join(os.path.dirname(os.path.realpath(__file__)), 'README.md')}"
        )
    if not out.stdout.strip():
        die(f"stored credential for {account!r} is empty; re-run ./bootstrap.sh")
    return out.stdout.strip()


def post_form(url: str, fields: dict) -> dict:
    body = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        die(f"token endpoint HTTP {e.code}: {e.read().decode()[:400]}")


def access_token() -> str:
    """Return a live access token, refreshing only when the cached one is stale.

    Zoho access tokens last an hour but the refresh endpoint is rate limited, so a
    fresh token per invocation would throttle an agent doing a normal sweep.
    """
    try:
        with open(CACHE_PATH) as f:
            c = json.load(f)
        if c.get("expires_at", 0) - 120 > time.time():
            return c["access_token"]
    except (OSError, ValueError, KeyError):
        pass

    tok = post_form(f"{ACCOUNTS_HOST}/oauth/v2/token", {
        "refresh_token": keychain("refresh_token"),
        "client_id": keychain("client_id"),
        "client_secret": keychain("client_secret"),
        "grant_type": "refresh_token",
    })
    if "access_token" not in tok:
        die(f"refresh failed: {json.dumps(tok)[:400]}")

    os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
    fd = os.open(CACHE_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({
            "access_token": tok["access_token"],
            "expires_at": time.time() + int(tok.get("expires_in", 3600)),
        }, f)
    return tok["access_token"]


def api(path: str, params: dict | None = None) -> dict:
    url = f"{API_HOST}/api{path}"
    if params:
        url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    req = urllib.request.Request(url, headers={
        "Authorization": f"Zoho-oauthtoken {access_token()}",
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=45) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        detail = e.read().decode()[:600]
        if e.code in (401, 403):
            detail += (
                "\n\nA 401/403 here is usually the SCOPE, not the token: this client is "
                "minted read-only, so any write path is refused by design. If reads also "
                "fail, the refresh token was issued for a narrower scope than the command "
                "needs — re-mint it (README.md step 2)."
            )
        die(f"API HTTP {e.code} on {path}: {detail}")


def account_id() -> str:
    if os.environ.get("ZOHO_ACCOUNT_ID"):
        return os.environ["ZOHO_ACCOUNT_ID"]
    data = api("/accounts").get("data") or []
    if not data:
        die("no accounts returned; the token is valid but sees nothing")
    return str(data[0]["accountId"])


def strip_html(s: str) -> str:
    import html
    import re
    s = re.sub(r"(?is)<(script|style).*?</\1>", " ", s)
    s = re.sub(r"(?i)<br\s*/?>|</p>", "\n", s)
    s = re.sub(r"(?s)<[^>]+>", " ", s)
    s = html.unescape(s)
    return re.sub(r"[ \t]{2,}", " ", re.sub(r"\n{3,}", "\n\n", s)).strip()


def summary(m: dict) -> dict:
    """Keep the fields an agent triages on. Full bodies are one `read` away."""
    # The two fields are NOT the same clock, and the fallback used to hide that.
    # Measured on one message on 2026-08-28: receivedTime is a true epoch and
    # renders as 21:11 local, the wall-clock moment Send was pressed;
    # sentDateInGMT is that same wall clock stored as though it were a UTC
    # epoch, so localtime() on it yields 04:11 THE NEXT DAY. Seven hours and a
    # date boundary, silently, only for messages missing receivedTime. Despite
    # the name, sentDateInGMT is the one that needs gmtime().
    ts, gmt = m.get("receivedTime"), False
    if not ts:
        ts, gmt = m.get("sentDateInGMT"), True
    when = None
    if ts:
        try:
            secs = int(ts) / 1000
            when = time.strftime("%Y-%m-%d %H:%M",
                                 time.gmtime(secs) if gmt else time.localtime(secs))
        except (ValueError, TypeError):
            when = str(ts)
    return {
        "messageId": m.get("messageId"),
        "folderId": m.get("folderId"),
        "from": m.get("fromAddress"),
        "to": m.get("toAddress"),
        "subject": m.get("subject"),
        "received": when,
        "unread": m.get("status2") == "0" or m.get("isUnread") in (True, "true"),
        "hasAttachment": m.get("hasAttachment") in (1, "1", True),
        "size": m.get("size"),
    }


# Measured against this mailbox on 2026-08-28, not taken from the docs.
#
# `entire` and `content` filter correctly: a nonsense term returns 0, a real term
# returns a subset. `subject` and `sender` are ACCEPTED but match nothing, whatever
# the term -- unusable, so they are not offered.
#
# Everything else, including the two you would reach for first, `from` and
# `fromAddress`, behaves exactly like a field name that does not exist: Zoho ignores
# it and returns the newest messages UNFILTERED, with HTTP 200. `from:contains:arxiv`
# came back with mail from hh.ru. An agent reads that as "here are the arXiv mails"
# and is wrong with no error anywhere. Refusing the query is the only safe answer.
SEARCH_FIELDS = ("entire", "content")

# CORRECTION 2026-08-29. The `contains:` operator is dead on this mailbox, and it
# was the form this file generated for every bare word, so `zmail search <word>`
# could not match anything -- ever.
#
#   entire:contains:ternellism  -> 0     entire:ternellism  -> 1  (mail sent 09:36 that day)
#   entire:contains:baez        -> 0     entire:baez        -> 3  (March, long indexed)
#   entire:contains:calligo     -> 0     entire:zzzqqqnope  -> 0  (so `entire:` really filters)
#
# The empty answer is the dangerous part: a dedup check reads it as "we have never
# written to them" and the next thing that happens is a duplicate application. The
# two-term form is therefore rewritten rather than passed through.
SEARCH_OPERATORS = ("contains",)


def normalise_search(q: str) -> str:
    """Turn a user query into a searchKey Zoho actually honours.

    A bare word gets wrapped rather than rejected: Zoho answers an unwrapped term
    with "Invalid search query", which is a confusing failure for the most obvious
    thing to type.

    `contains:` is stripped wherever it appears, because it silently matches
    nothing -- see the measurement above.
    """
    q = q.strip()
    if ":" not in q:
        return f"entire:{q}"
    field, rest = q.split(":", 1)
    field = field.strip()
    if field.lower() not in SEARCH_FIELDS:
        die(
            f"unsupported search field {field!r}.\n"
            f"Zoho does not reject an unknown field -- it IGNORES it and returns your "
            f"newest mail unfiltered, with HTTP 200, so the results would look like "
            f"matches and would not be.\n"
            f"Use one of: {', '.join(SEARCH_FIELDS)} -- e.g. entire:{q.split(':')[-1]}\n"
            f"To match a sender, search the address as text: entire:arxiv.org"
        )
    op, _, tail = rest.partition(":")
    if tail and op.strip().lower() in SEARCH_OPERATORS:
        # Measured dead on 2026-08-29: this form returns 0 for every term.
        rest = tail
    return f"{field}:{rest}"


def out(obj):
    json.dump(obj, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")


def main():
    ap = argparse.ArgumentParser(prog="zmail", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("accounts")
    sub.add_parser("folders")
    sub.add_parser("whoami")

    p = sub.add_parser("list")
    p.add_argument("-n", "--limit", type=int, default=25)
    p.add_argument("-f", "--folder", help="folder id; default is every folder")

    p = sub.add_parser("read")
    p.add_argument("message_id")
    p.add_argument("-F", "--folder", required=False, help="folder id (looked up if omitted)")
    p.add_argument("--raw", action="store_true", help="keep the HTML body as-is")
    p.add_argument("--full", action="store_true", help="do not truncate the body")
    p.add_argument("--max-chars", type=int, default=20000,
                   help="truncate the body at N chars (default 20000)")

    p = sub.add_parser("search")
    p.add_argument("query")
    p.add_argument("-n", "--limit", type=int, default=25)

    a = ap.parse_args()

    if a.cmd == "whoami":
        try:
            with open(CACHE_PATH) as f:
                c = json.load(f)
            left = int(c.get("expires_at", 0) - time.time())
            cache = {"cached": True, "expires_in_sec": max(0, left)}
        except (OSError, ValueError):
            cache = {"cached": False}
        out({"region": REGION, "api": API_HOST, "keychain_service": KEYCHAIN_SERVICE,
             "token": cache, "accounts": [
                 {"accountId": d.get("accountId"), "address": d.get("primaryEmailAddress")}
                 for d in (api("/accounts").get("data") or [])]})
        return

    if a.cmd == "accounts":
        out([{k: d.get(k) for k in ("accountId", "primaryEmailAddress", "displayName", "type")}
             for d in (api("/accounts").get("data") or [])])
        return

    aid = account_id()

    if a.cmd == "folders":
        # Zoho's folder payload carries no unreadCount/messageCount. Asking for them
        # yielded null on every folder, and a null unread count reads as "nothing
        # unread" -- a wrong answer dressed as an answer. Report what exists.
        out([{k: d.get(k) for k in ("folderId", "folderName", "path", "folderType", "imapAccess")}
             for d in (api(f"/accounts/{aid}/folders").get("data") or [])])
        return

    if a.cmd == "list":
        data = api(f"/accounts/{aid}/messages/view",
                   {"limit": a.limit, "start": 1, "folderId": a.folder}).get("data") or []
        out([summary(m) for m in data])
        return

    if a.cmd == "search":
        out([summary(m) for m in api(
            f"/accounts/{aid}/messages/search",
            {"searchKey": normalise_search(a.query), "limit": a.limit, "start": 1},
        ).get("data") or []])
        return

    if a.cmd == "read":
        # The content endpoint returns exactly two fields, `content` and `messageId` --
        # no headers at all. Reading subject/from off it gave null on every message,
        # so the headers come from the summary view, which is also where the
        # folderId lookup happens. One scan serves both.
        folder, head = a.folder, None
        for m in (api(f"/accounts/{aid}/messages/view", {"limit": 200, "start": 1}).get("data") or []):
            if str(m.get("messageId")) == str(a.message_id):
                head, folder = m, folder or m.get("folderId")
                break
        if not folder:
            die(f"message {a.message_id} not in the newest 200; pass -F <folderId>")

        raw = (api(f"/accounts/{aid}/folders/{folder}/messages/{a.message_id}/content")
               .get("data") or {}).get("content") or ""
        body = raw if a.raw else strip_html(raw)

        # A long thread runs to six figures of characters -- one observed here at
        # 148,316. Handing that to an agent unannounced buries the rest of its context.
        # Truncate by default and SAY SO, so a caller can tell a short mail from a
        # clipped one instead of silently reasoning over a fragment.
        full = len(body)
        truncated = not a.full and full > a.max_chars
        if truncated:
            body = body[:a.max_chars]

        result = {
            "messageId": a.message_id,
            "folderId": folder,
            "subject": (head or {}).get("subject"),
            "from": (head or {}).get("fromAddress") or (head or {}).get("sender"),
            "to": (head or {}).get("toAddress"),
            "cc": (head or {}).get("ccAddress"),
            "received": summary(head)["received"] if head else None,
            "body_chars": full,
            "truncated": truncated,
            "body": body,
        }
        if truncated:
            result["note"] = (f"body truncated to {a.max_chars} of {full} chars; "
                              f"re-run with --full or a larger --max-chars")
        if head is None:
            result["note"] = "headers unavailable: message is outside the newest 200"
        out(result)
        return


if __name__ == "__main__":
    main()

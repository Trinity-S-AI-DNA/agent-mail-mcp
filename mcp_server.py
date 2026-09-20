#!/usr/bin/env python3
"""MCP stdio server exposing mailbox access as agent tools.

Six of the seven tools are read-only. The seventh, mail_send, is the only one that
changes anything outside this machine, and it defaults to a dry run: transmitting
requires confirm=true from a human in conversation, never from text found in a
mailbox, a page or a file.

Every tool shells out to the `zmail` CLI in this same directory rather than
reimplementing the API calls. That is deliberate: two code paths to the same
mailbox drift, and the one an agent uses is the one nobody runs by hand. One
implementation, two front doors.

Wire it up with:
    claude mcp add --scope user agent-mail -- /path/to/mcp_server.py

Stdlib only, no dependencies — an agent tool that needs a package install is an
agent tool that breaks on a fresh machine.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

# The tool table is GENERATED from spec/agent_mail.t27 -- names, prose, argument
# schemas and the limits in them. It used to be a hundred and ten hand-written
# lines here restating what zmail.py enforces in code, and it had drifted: it
# promised agents that a send without the scope "comes back 403", where the API
# answers 401 INVALID_OAUTHSCOPE, and it described the search index lag with a
# number measured once in August. Edit the spec, run `python3 spec/generate.py`.
from agent_mail_spec import (  # noqa: E402
    MCP_PROTOCOL_VERSION as PROTOCOL_VERSION,
    SERVER_NAME,
    SERVER_VERSION,
    TOOLS,
    TOOL_KIND,
    TOOL_NAMES,
)

ZMAIL = os.path.join(os.path.dirname(os.path.realpath(__file__)), "zmail.py")
SERVER_INFO = {"name": SERVER_NAME, "version": SERVER_VERSION}

# The one tool that can put something in front of another human. The spec is what
# says so, and it refuses to generate an outward tool that has no confirm gate.
OUTWARD = tuple(name for name, kind in TOOL_KIND.items() if kind == "outward")


def run_zmail(args: list[str]) -> tuple[bool, str]:
    """Return (ok, text). A non-zero exit is surfaced as an error, not as empty output.

    Silent empty results are the failure mode that costs the most time downstream:
    an agent reads "[]" as "no mail" and stops, when the truth was "not authenticated".
    """
    try:
        p = subprocess.run([sys.executable, ZMAIL, *args],
                           capture_output=True, text=True, timeout=90)
    except subprocess.TimeoutExpired:
        return False, "zmail timed out after 90s (network or Zoho outage)"
    except OSError as e:
        return False, f"could not execute {ZMAIL}: {e}"
    if p.returncode != 0:
        return False, (p.stderr or p.stdout or f"zmail exited {p.returncode}").strip()
    return True, p.stdout.strip()


# Which arguments a tool cannot run without, read off the generated schema
# rather than repeated here. The check used to be six hand-written lines that
# happened to agree with the schema; nothing made them agree.
REQUIRED_ARGS = {t["name"]: t["inputSchema"].get("required", []) for t in TOOLS}


def call_tool(name: str, args: dict) -> tuple[bool, str]:
    def opt(flag, key, cast=str):
        v = args.get(key)
        return [flag, cast(v)] if v is not None else []

    if name not in TOOL_NAMES:
        return False, f"unknown tool: {name}"
    for key in REQUIRED_ARGS[name]:
        if not args.get(key):
            return False, f"{key} is required"

    if name == "mail_list":
        return run_zmail(["list", *opt("-n", "limit"), *opt("-f", "folder")])
    if name == "mail_search":
        return run_zmail(["search", str(args["query"]), *opt("-n", "limit")])
    if name == "mail_read":
        extra = ["--raw"] if args.get("raw") else []
        return run_zmail(["read", str(args["message_id"]), *opt("-F", "folder"), *extra])
    if name == "mail_folders":
        return run_zmail(["folders"])
    if name == "mail_accounts":
        return run_zmail(["accounts"])
    if name == "mail_whoami":
        return run_zmail(["whoami"])
    if name == "mail_send":
        # The body travels as an argv element, not through a shell, so newlines,
        # quotes and non-ASCII need no escaping and must not be mangled here.
        cmd = ["send", "-t", str(args["to"]), "-s", str(args["subject"]),
               "--body", str(args["body"])]
        cmd += opt("--cc", "cc") + opt("--bcc", "bcc") + opt("--from", "from_address")
        cmd += opt("--reply-to", "reply_to")
        if args.get("html"):
            cmd.append("--html")
        # confirm must be literally true. A truthy string like "no" or "false"
        # arriving from a sloppy caller would otherwise send a real letter.
        if args.get("confirm") is True:
            cmd.append("--send")
        return run_zmail(cmd)
    # Reachable only if the spec declares a tool this dispatch has never heard
    # of, which is a build that should not have shipped rather than a bad call.
    return False, f"{name} is in the spec but has no implementation here"


def send(msg: dict):
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except ValueError:
            continue

        method, rid = req.get("method"), req.get("id")

        # Notifications carry no id and MUST NOT be answered; replying to one is a
        # protocol error that some clients treat as a fatal handshake failure.
        if rid is None:
            continue

        if method == "initialize":
            send({"jsonrpc": "2.0", "id": rid, "result": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": SERVER_INFO,
            }})
        elif method == "tools/list":
            send({"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}})
        elif method == "tools/call":
            params = req.get("params") or {}
            ok, text = call_tool(params.get("name", ""), params.get("arguments") or {})
            send({"jsonrpc": "2.0", "id": rid, "result": {
                "content": [{"type": "text", "text": text}],
                "isError": not ok,
            }})
        elif method == "ping":
            send({"jsonrpc": "2.0", "id": rid, "result": {}})
        else:
            send({"jsonrpc": "2.0", "id": rid,
                  "error": {"code": -32601, "message": f"method not found: {method}"}})


if __name__ == "__main__":
    main()

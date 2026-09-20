#!/usr/bin/env python3
"""Generate agent_mail_spec.py from spec/agent_mail.t27.

The spec is the source. This reads it with `t27c parse --json` -- never with
`t27c gen`, whose Zig backend drops the quotes from string literals and so
cannot express a tool description at all -- checks the laws the spec states
about itself, and writes one Python module for mcp_server.py and zmail.py to
import.

    python3 spec/generate.py            # write agent_mail_spec.py
    python3 spec/generate.py --check    # exit 1 unless it is already identical

Finding the compiler, in order: $T27C, ~/t27/target/release/t27c, then `t27c`
on PATH. If none of them exists this exits NON-ZERO and says so. A generator
that cannot find its compiler and reports success is worse than one that is
missing, because the silence reads as a pass.
"""

import json
import os
import re
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.realpath(__file__))
SPEC = os.path.join(HERE, "agent_mail.t27")
OUT = os.path.join(os.path.dirname(HERE), "agent_mail_spec.py")

# A marker struct's prefix says whether the argument is required; its `v` field
# says what JSON type it is. Both are read off the spec, so a marker that is not
# one of these shapes is an error rather than a guess.
REQUIREDNESS = {"Req": True, "Opt": False}
JSON_TYPE = {"str": "string", "usize": "integer", "bool": "boolean"}


class SpecError(Exception):
    pass


def find_t27c():
    for path in (os.environ.get("T27C"),
                 os.path.expanduser("~/t27/target/release/t27c"),
                 shutil.which("t27c")):
        if path and os.path.exists(path):
            return path
    raise SpecError(
        "t27c not found. The spec cannot be read without it.\n"
        "  Set $T27C, or build it: cd ~/t27 && cargo build -p t27c --release")


def parse_spec():
    t27c = find_t27c()
    proc = subprocess.run([t27c, "parse", "--json", SPEC],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        raise SpecError(f"t27c parse failed:\n{proc.stderr.strip()}")
    return json.loads(proc.stdout)


def snake(name):
    """MailList -> mail_list. The naming law between a struct and its tool."""
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def array_elements(node):
    """[entire,content] -> ['entire', 'content'].

    An array literal reaches the AST as a single identifier holding the joined
    element text, because the Zig-facing lexer drops the quotes from literals
    inside it. Elements are therefore restricted to bare words -- which is all
    this spec needs, and the length declared in the type is checked against the
    count so a silent truncation cannot pass.
    """
    child = node["children"][0]
    text = child["name"].strip()
    if not (text.startswith("[") and text.endswith("]")):
        raise SpecError(f"{node['name']}: expected an array literal, got {text!r}")
    items = [x.strip() for x in text[1:-1].split(",") if x.strip()]
    declared = re.match(r"\[(\d+)\]", node["extra_type"])
    if declared and int(declared.group(1)) != len(items):
        raise SpecError(
            f"{node['name']}: type says {declared.group(1)} elements, "
            f"the value has {len(items)}: {items}")
    for item in items:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", item):
            raise SpecError(
                f"{node['name']}: {item!r} is not a bare word. Array elements "
                f"lose their quotes in the parser, so only bare words survive.")
    return items


def collect(ast):
    consts, structs, enums = {}, {}, {}
    order = []
    for node in ast["children"]:
        kind, name = node["kind"], node["name"]
        if kind == "ConstDecl":
            if node["extra_type"].startswith("["):
                consts[name] = tuple(array_elements(node))
            else:
                literal = node["children"][0]
                if literal["kind"] != "ExprLiteral":
                    raise SpecError(
                        f"{name}: value is {literal['kind']}, not a literal. "
                        f"Only literal constants can be generated from.")
                value = literal["value"]
                consts[name] = int(value) if node["extra_type"] in (
                    "usize", "u8", "u32", "u64", "isize") else value
        elif kind == "StructDecl":
            structs[name] = [(f["name"], f["extra_type"]) for f in node["children"]]
            order.append(name)
        elif kind == "EnumDecl":
            enums[name] = [v["name"] for v in node["children"]]
    return consts, structs, enums, order


def check_ascii(consts):
    """Non-ASCII inside a string literal is silently mangled by the lexer -- an
    em dash arrives as three code points -- so it is refused here instead of
    shipping mojibake into a tool description an agent has to read."""
    for name, value in consts.items():
        for item in (value if isinstance(value, tuple) else [value]):
            if isinstance(item, str) and not item.isascii():
                bad = [c for c in item if not c.isascii()]
                raise SpecError(
                    f"{name}: non-ASCII in a string literal: {bad!r}. The t27 "
                    f"lexer reads bytes as characters and would mangle it. "
                    f"Write ASCII (`--` for an em dash); comments may say "
                    f"anything, they never reach the AST.")


def build(consts, structs, enums, order):
    markers, tools = {}, []
    for name in order:
        fields = structs[name]
        if len(fields) == 1 and fields[0][0] == "v":
            prefix, vtype = name[:3], fields[0][1]
            if prefix not in REQUIREDNESS or vtype not in JSON_TYPE:
                raise SpecError(
                    f"{name}: a one-field `v` struct is an argument marker, so "
                    f"its name must start with Req or Opt and `v` must be one "
                    f"of {sorted(JSON_TYPE)}. Got {prefix!r}/{vtype!r}.")
            markers[name] = (REQUIREDNESS[prefix], JSON_TYPE[vtype])
        else:
            tools.append(name)

    if not markers:
        raise SpecError("the spec declares no argument markers")

    kinds = enums.get("ToolKind")
    if kinds != ["read_only", "outward"]:
        raise SpecError(f"ToolKind must be read_only, outward -- got {kinds}")
    outward = set(consts.get("OUTWARD_TOOLS", ()))

    described = {n for n in consts if n.endswith("_DESC")}
    used = set()
    table = []
    for struct_name in tools:
        tool = snake(struct_name)
        desc_key = tool.upper() + "_DESC"
        if desc_key not in consts:
            raise SpecError(f"tool {tool}: no {desc_key} in the spec")
        used.add(desc_key)

        properties, required = {}, []
        for field, ftype in structs[struct_name]:
            if ftype not in markers:
                raise SpecError(
                    f"{tool}.{field}: type {ftype!r} is not a declared argument "
                    f"marker. Declared: {sorted(markers)}")
            is_required, json_type = markers[ftype]
            field_key = f"{tool}_{field}".upper() + "_DESC"
            if field_key not in consts:
                raise SpecError(f"{tool}.{field}: no {field_key} in the spec")
            used.add(field_key)
            prop = {"type": json_type, "description": consts[field_key]}
            if json_type == "integer":
                prop["minimum"] = consts["LIMIT_MIN"]
                prop["maximum"] = consts["LIMIT_MAX"]
            properties[field] = prop
            if is_required:
                required.append(field)

        if tool in outward:
            # An outward tool with no dry run is a tool that sends on its first
            # call. The gate is the spec's, not the implementation's.
            if structs[struct_name] and ("confirm", "OptBool") not in structs[struct_name]:
                raise SpecError(
                    f"{tool} is in OUTWARD_TOOLS but has no `confirm: OptBool`. "
                    f"An outward tool must default to a dry run.")
            outward.discard(tool)

        schema = {"type": "object", "properties": properties}
        if required:
            schema["required"] = required
        table.append({"name": tool,
                      "description": consts[desc_key],
                      "inputSchema": schema,
                      "kind": "outward" if tool in set(consts.get("OUTWARD_TOOLS", ())) else "read_only"})

    if outward:
        raise SpecError(f"OUTWARD_TOOLS names tools that do not exist: {sorted(outward)}")
    orphans = described - used
    if orphans:
        raise SpecError(
            f"descriptions with nothing to describe: {sorted(orphans)}. "
            f"Every *_DESC must belong to a tool or one of its arguments.")

    lim = (consts["LIMIT_MIN"], consts["LIMIT_DEFAULT"], consts["LIMIT_MAX"])
    if not lim[0] <= lim[1] <= lim[2]:
        raise SpecError(f"LIMIT_MIN <= LIMIT_DEFAULT <= LIMIT_MAX is false: {lim}")

    laws = {n: v for n, v in consts.items() if not n.endswith("_DESC")}
    return table, laws, kinds


def py(value, indent):
    """Render a dict/list/scalar as Python source, one key per line.

    repr() on its own would put a whole inputSchema on a single line, and the
    generated file is read by people deciding whether a tool is safe to call.
    """
    pad, inner = " " * indent, " " * (indent + 4)
    if isinstance(value, dict):
        if not value:
            return "{}"
        body = "".join(f"{inner}{key!r}: {py(val, indent + 4)},\n"
                       for key, val in value.items())
        return "{\n" + body + pad + "}"
    if isinstance(value, list):
        if not value:
            return "[]"
        body = "".join(f"{inner}{py(item, indent + 4)},\n" for item in value)
        return "[\n" + body + pad + "]"
    return repr(value)


def render(table, laws, kinds):
    out = [
        '"""GENERATED from spec/agent_mail.t27 by spec/generate.py. DO NOT EDIT.',
        "",
        "Edit the spec and re-run the generator; test_mailbot.py fails if this",
        "file and the spec disagree.",
        '"""',
        "",
        "# --- laws -------------------------------------------------------------",
    ]
    for name in sorted(laws):
        out.append(f"{name} = {laws[name]!r}")
    out += [
        "",
        f"TOOL_KINDS = {tuple(kinds)!r}",
        "",
        "# --- tool table -------------------------------------------------------",
        "TOOLS = " + py([{k: v for k, v in t.items() if k != "kind"} for t in table], 0),
        "",
        "TOOL_KIND = " + py({t["name"]: t["kind"] for t in table}, 0),
        "",
        'TOOL_NAMES = tuple(t["name"] for t in TOOLS)',
        "",
    ]
    return "\n".join(out)


def main():
    check = "--check" in sys.argv
    try:
        ast = parse_spec()
        consts, structs, enums, order = collect(ast)
        check_ascii(consts)
        text = render(*build(consts, structs, enums, order))
    except SpecError as exc:
        print(f"spec/agent_mail.t27: {exc}", file=sys.stderr)
        return 1

    if check:
        current = open(OUT).read() if os.path.exists(OUT) else None
        if current == text:
            print(f"OK: {os.path.basename(OUT)} matches the spec "
                  f"({len(text.splitlines())} lines)")
            return 0
        print(f"STALE: {os.path.basename(OUT)} does not match "
              f"spec/agent_mail.t27. Run: python3 spec/generate.py",
              file=sys.stderr)
        return 1

    with open(OUT, "w") as fh:
        fh.write(text)
    print(f"wrote {OUT} ({len(text.splitlines())} lines)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

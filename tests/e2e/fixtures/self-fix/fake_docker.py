#!/usr/bin/env python3
"""A MODEL OF THE DOCKER CLI on a host, for BOSS's replays: the subset the shell BOSS sends to a
host uses to find and retire containers, answered the way the real CLI answers it.

State: $FAKE_DOCKER_STATE, a JSON file {"containers": [{"id", "name", "labels", "ports",
"state"}], "log": [...]}. Ids are 64-hex like the real ones (`ps -q` prints 12); names are
separate. Every command that removes or stops something is appended to "log", and removed
containers leave the state, so a later `ps` no longer lists them.

  ps [-a] [-q] [--no-trunc] [--filter K=V]... [--format TPL]   TPL: .ID .Names .Label "K"
       .Labels .State .Status .Ports
       filters (ANDed, like the CLI): label=KEY, label=KEY=VALUE, name=SUBSTR, id=PREFIX,
       publish=PORT, status=STATE
  inspect [-f|--format TPL] ID|NAME...      TPL: {{.Name}}, {{.Id}}, {{.State.Status}},
       {{index .Config.Labels "K"}}, {{.Config.Labels}}, `json` of any of them, parenthesised
       or not ({{json (index .Config.Labels "K")}}) — anything else is an error, as a template
       the CLI cannot parse is
  rm [-f] [-v] [--] ID|NAME...    stop ID|NAME...    kill ID|NAME...
  compose -p P down [...]          removes every container of project P
  anything else (pull, compose up, network, ...) succeeds and changes nothing
An unknown container is "Error: No such container: X" on stderr, exit 1, as the CLI says.
"""
from __future__ import annotations

import json
import os
import re
import sys

STATE = os.environ["FAKE_DOCKER_STATE"]


def load() -> dict:
    with open(STATE, encoding="utf-8") as f:
        return json.load(f)


def save(state: dict) -> None:
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump(state, f)


def find(state: dict, ref: str):
    for c in state["containers"]:
        if c["id"].startswith(ref) and len(ref) >= 4 or c["name"] == ref.lstrip("/"):
            return c
    return None


def matches(c: dict, flt: str) -> bool:
    key, _, value = flt.partition("=")
    if key == "label":
        k, eq, v = value.partition("=")
        return k in c["labels"] and (not eq or c["labels"][k] == v)
    if key == "name":
        return value in c["name"]
    if key == "id":
        return c["id"].startswith(value)
    if key == "publish":
        return value.split("/")[0] in [str(p) for p in c.get("ports") or []]
    if key == "status":
        return c.get("state", "running") == value
    return False


def _value(c: dict, expr: str, ps: bool):
    """The Go-template value of one pipeline-free expression: a field, `index .Config.Labels
    "K"` (inspect) or `.Label "K"` (ps), parenthesised or not, or `json <expr>` of any of them."""
    expr = expr.strip()
    while expr.startswith("(") and expr.endswith(")"):
        expr = expr[1:-1].strip()
    if expr.startswith("json "):
        # Go's encoding/json: compact, map keys sorted.
        return json.dumps(_value(c, expr[5:], ps), separators=(",", ":"), sort_keys=True)
    if ps:
        m = re.fullmatch(r'\.Label "([^"]+)"', expr)
        if m:
            return c["labels"].get(m.group(1), "")
        fields = {".ID": c["id"][:12], ".Names": c["name"],
                  ".Labels": ",".join(f"{k}={v}" for k, v in sorted(c["labels"].items())),
                  ".State": c.get("state", "running"), ".Status": "Up 2 days",
                  ".Ports": ", ".join(f"0.0.0.0:{p}->{p}/tcp" for p in c.get("ports") or [])}
        if expr in fields:
            return fields[expr]
        raise ValueError(f"template: {expr}")
    fields = {".Name": "/" + c["name"], ".Id": c["id"], ".State.Status": c.get("state", "running"),
              ".Config.Labels": dict(c["labels"])}
    if expr in fields:
        return fields[expr]
    m = re.fullmatch(r'index \.Config\.Labels "([^"]+)"', expr)
    if m:
        return c["labels"].get(m.group(1), "")
    raise ValueError(f"template: {expr}")


def render(c: dict, tpl: str, *, ps: bool = False) -> str:
    """One container through a Go template, as `inspect -f` (ps=False) or `ps --format`
    (ps=True: the CLI's own ps fields, .ID, .Names, .Label "K", .Labels, .State, .Ports). A map
    prints as Go prints it (map[k:v ...]); `json` prints JSON, as the CLI's template func does."""
    def one(expr: str) -> str:
        value = _value(c, expr, ps)
        if isinstance(value, dict):
            return "map[" + " ".join(f"{k}:{v}" for k, v in sorted(value.items())) + "]"
        return str(value)
    return re.sub(r"\{\{(.*?)\}\}", lambda m: one(m.group(1)), tpl)


def remove(state: dict, c: dict, how: str) -> None:
    state["containers"] = [x for x in state["containers"] if x["id"] != c["id"]]
    state["log"].append({"op": how, "name": c["name"]})


def main(argv: list[str]) -> int:
    state = load()
    cmd, args = (argv[0] if argv else ""), argv[1:]
    if cmd == "ps":
        filters, fmt, quiet, everything, trunc = [], None, False, False, True
        i = 0
        while i < len(args):
            a = args[i]
            if a in ("-a", "--all"):
                everything = True
            elif a in ("-q", "--quiet"):
                quiet = True
            elif a == "--no-trunc":
                trunc = False
            elif a in ("-aq", "-qa"):
                everything = quiet = True
            elif a in ("--filter", "-f"):
                i += 1
                filters.append(args[i])
            elif a.startswith("--filter="):
                filters.append(a.split("=", 1)[1])
            elif a == "--format":
                i += 1
                fmt = args[i]
            i += 1
        for c in state["containers"]:
            if not everything and c.get("state", "running") != "running":
                continue
            if all(matches(c, f) for f in filters):
                if fmt:
                    try:
                        print(render(c, fmt, ps=True))
                    except ValueError as error:
                        print(f"template parsing error: {error}", file=sys.stderr)
                        return 1
                else:
                    print(c["id"][:12] if trunc else c["id"]) if quiet else print(c["name"])
        return 0
    if cmd == "inspect":
        fmt, refs, i = None, [], 0
        while i < len(args):
            if args[i] in ("-f", "--format"):
                fmt = args[i + 1]
                i += 2
                continue
            refs.append(args[i])
            i += 1
        code = 0
        for ref in refs:
            c = find(state, ref)
            if c is None:
                print(f"Error: No such object: {ref}", file=sys.stderr)
                code = 1
                continue
            try:
                print(render(c, fmt) if fmt else json.dumps([c]))
            except ValueError as error:
                print(f"template parsing error: {error}", file=sys.stderr)
                return 1
        return code
    if cmd in ("rm", "stop", "kill"):
        flags = [a for a in args if a.startswith("-") and a != "--"]
        refs = [a for a in args if not a.startswith("-")]
        code = 0
        for ref in refs:
            c = find(state, ref)
            if c is None:
                print(f"Error: No such container: {ref}", file=sys.stderr)
                code = 1
                continue
            how = cmd + ("" if not flags else " " + " ".join(flags))
            if cmd == "rm" and c.get("state", "running") == "running" and \
                    not any(f in ("-f", "--force") or ("f" in f and not f.startswith("--"))
                            for f in flags):
                print(f"Error: cannot remove a running container {ref}", file=sys.stderr)
                code = 1
                continue
            if cmd == "rm":
                remove(state, c, how)
            else:
                c["state"] = "exited"
                state["log"].append({"op": how, "name": c["name"]})
            print(ref)
        save(state)
        return code
    if cmd == "compose":
        project = None
        for i, a in enumerate(args):
            if a in ("-p", "--project-name") and i + 1 < len(args):
                project = args[i + 1]
            elif a.startswith("--project-name="):
                project = a.split("=", 1)[1]
        if "down" in args and project:
            for c in [x for x in state["containers"]
                      if x["labels"].get("com.docker.compose.project") == project]:
                remove(state, c, "compose down" + (" -v" if "-v" in args else ""))
            save(state)
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

"""THE RUN FILESYSTEM (AP-P6, BOSSAPP-1057; AGENT_PLATFORM.md §4.7): a state-backed, path-routed
virtual filesystem a kernel run and its tools share.

deepagents gives every agent `ls`, `read_file`, `write_file` and `grep` over a pluggable backend
and routes paths by prefix (a CompositeBackend). BOSS's runs get the same, over what BOSS already
has:

    /scratch/...              the run's own notes and archives      RunState.extra["_fs"]
    /large_tool_results/...   tool results too large to show whole   RunState.extra["_fs"]
    /memories/...             the memory API (services/memory_api)   the run's MemoryPort
    /skills/<name>/SKILL.md   held SKILL.md skills, read-only        RunState.extra["_skills"]

STATE-BACKED. `/scratch` and `/large_tool_results` live in the RunState, so they ride in every
checkpoint the run's store writes (kernel/durable.py): a resumed run reads exactly what it wrote,
and a forked thread forks its files with its messages. Nothing here touches a disk.

`/memories` is the memory API seen as files (`/memories/<namespace>/...`): `ls /memories` names
the namespaces this run may read, `read_file` returns the namespace's budgeted pack, and
`write_file` records one memory through the API, with its trust tier — a lesson arrives
`proposed` and binds nothing until a person reviews it. A run with no MemoryPort says so.

THE TOOLS ARE THE KERNEL'S. `RunFSToolSet` joins every run whose stack holds the context policy
(kernel/context_policy.py); the same four names are registered in the tool registry
(tools/builtin.register_run_fs_tools) with their permissions, for the catalogue, the pins and
the identity matrices — that registration answers "no run filesystem" outside a kernel run, and
no surface but the kernel offers it (RUN_FS_TOOLS is excluded from MCP and the legacy CLI loop).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Callable, Optional, Protocol

from .types import ToolCall, ToolResult, ToolSchema

#: The four tools, in the order they are offered (a stable order: part of the cached prefix).
LS, READ_FILE, GREP, WRITE_FILE = "ls", "read_file", "grep", "write_file"
RUN_FS_TOOLS: tuple = (LS, READ_FILE, GREP, WRITE_FILE)

SCRATCH = "/scratch"
LARGE = "/large_tool_results"
MEMORIES = "/memories"
SKILLS = "/skills"
#: Routes that are the run's own state (checkpointed).
STATE_ROUTES = (SCRATCH, LARGE)
ROUTES = (SCRATCH, LARGE, MEMORIES, SKILLS)

#: Where the files live in RunState.extra (a checkpoint carries `extra` whole).
FS_KEY = "_fs"
SKILLS_KEY = "_skills"

#: read_file pages: lines per page by default, the most one call may ask for, and a character
#: ceiling per page so one page of a minified blob is not itself a context bomb. The page and
#: grep ceilings keep every FS reply inside the smallest clip a preset uses (16,000), so a
#: read of an offloaded result is never itself cut.
DEFAULT_PAGE_LINES = 200
MAX_PAGE_LINES = 2_000
MAX_PAGE_CHARS = 12_000
#: grep: matches returned per call and characters of each matching line.
MAX_GREP_MATCHES = 40
MAX_GREP_LINE = 240
#: write_file: what one file may hold (a run's notes, not a second transcript).
MAX_WRITE_CHARS = 200_000


class FSError(ValueError):
    """A request the filesystem refuses, in words the model can act on."""


class MemoryPort(Protocol):
    """What `/memories` routes to: services/memory_api.RunMemory over the run's connection."""

    def namespaces(self) -> list: ...

    def read(self, namespace: str, *, query: str = "") -> str: ...

    def write(self, namespace: str, body: str, *, kind: str = "") -> dict: ...


def normalise(path: Any) -> str:
    """An absolute path with no `.`/`..` segments and no trailing slash ("/" stays "/")."""
    raw = str(path or "/").strip() or "/"
    if not raw.startswith("/"):
        raw = "/" + raw
    parts: list = []
    for part in raw.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if parts:
                parts.pop()
            continue
        parts.append(part)
    return "/" + "/".join(parts)


def route_of(path: str) -> str:
    """The route a normalised path is under, or "" (the root, or nowhere)."""
    for route in ROUTES:
        if path == route or path.startswith(route + "/"):
            return route
    return ""


class RunFS:
    """The run's filesystem over its RunState (and the run's MemoryPort for /memories)."""

    def __init__(self, state: Any, *, memory: Optional[MemoryPort] = None):
        self.state = state
        self.memory = memory

    # ── storage ────────────────────────────────────────────────────────────────────────────
    @property
    def files(self) -> dict:
        extra = self.state.extra
        files = extra.get(FS_KEY)
        if not isinstance(files, dict):
            files = {}
            extra[FS_KEY] = files
        return files

    @property
    def skills(self) -> dict:
        skills = self.state.extra.get(SKILLS_KEY)
        return skills if isinstance(skills, dict) else {}

    def exists(self, path: str) -> bool:
        return normalise(path) in self.files

    def put(self, path: str, content: str, *, meta: Optional[dict] = None) -> str:
        """Write a STATE file (the kernel's own writes: offloads, archives). Returns the path."""
        path = normalise(path)
        if route_of(path) not in STATE_ROUTES:
            raise FSError(f"{path} is not a state route ({', '.join(STATE_ROUTES)})")
        self.files[path] = {"content": str(content), "step": int(getattr(self.state, "step", 0)),
                            **(meta or {})}
        return path

    def text_of(self, path: str) -> str:
        path = normalise(path)
        route = route_of(path)
        if route in STATE_ROUTES:
            entry = self.files.get(path)
            if entry is None:
                raise FSError(f"no such file: {path}. `ls` {route} to see what is there.")
            return str(entry.get("content") or "")
        if route == SKILLS:
            name = path[len(SKILLS) + 1:].split("/", 1)[0]
            skill = self.skills.get(name)
            if skill is None:
                raise FSError(f"no skill named {name!r}. `ls /skills` lists the skills you hold.")
            return str(skill.get("body") or "")
        if route == MEMORIES:
            if self.memory is None:
                raise FSError("this run has no memory store, so /memories is empty")
            namespace = path[len(MEMORIES) + 1:].strip("/")
            if not namespace:
                raise FSError("read a namespace: `ls /memories` lists them")
            return self.memory.read(namespace)
        raise FSError(f"{path} is outside the run filesystem; the roots are {', '.join(ROUTES)}")

    # ── the operations ─────────────────────────────────────────────────────────────────────
    def ls(self, path: str = "/") -> dict:
        path = normalise(path)
        if path == "/":
            return {"ok": True, "path": "/", "entries": [
                {"path": r, "kind": "dir", "about": _ABOUT[r]} for r in ROUTES]}
        route = route_of(path)
        if route in STATE_ROUTES:
            prefix = path.rstrip("/") + "/"
            entries = [{"path": p, "kind": "file", "chars": len(str(e.get("content") or "")),
                        "lines": str(e.get("content") or "").count("\n") + 1,
                        **({"tool": e["tool"]} if e.get("tool") else {})}
                       for p, e in sorted(self.files.items())
                       if p == path or p.startswith(prefix)]
            return {"ok": True, "path": path, "entries": entries}
        if route == SKILLS:
            return {"ok": True, "path": path, "entries": [
                {"path": f"{SKILLS}/{name}/SKILL.md", "kind": "file",
                 "description": str(skill.get("description") or "")}
                for name, skill in sorted(self.skills.items())]}
        if route == MEMORIES:
            if self.memory is None:
                return {"ok": True, "path": path, "entries": [],
                        "note": "this run has no memory store"}
            return {"ok": True, "path": path, "entries": [
                {"path": f"{MEMORIES}/{ns}", "kind": "namespace"}
                for ns in self.memory.namespaces()]}
        raise FSError(f"{path} is outside the run filesystem; the roots are {', '.join(ROUTES)}")

    def read(self, path: str, *, offset: int = 0, limit: int = DEFAULT_PAGE_LINES) -> dict:
        """One page of a file: `limit` lines from line `offset` (0-based), numbered, with the
        total and where the next page starts — a large file is read in pages, never whole."""
        path = normalise(path)
        lines = self.text_of(path).split("\n")
        offset = max(0, int(offset or 0))
        limit = max(1, min(int(limit or DEFAULT_PAGE_LINES), MAX_PAGE_LINES))
        page: list = []
        used = 0
        end = offset
        for number in range(offset, min(len(lines), offset + limit)):
            line = f"{number + 1:>6}\t{lines[number]}"
            if page and used + len(line) > MAX_PAGE_CHARS:
                break
            page.append(line[:MAX_PAGE_CHARS])
            used += len(line) + 1
            end = number + 1
        out = {"ok": True, "path": path, "total_lines": len(lines), "offset": offset,
               "content": "\n".join(page)}
        if end < len(lines):
            out["next_offset"] = end
        return out

    def grep(self, pattern: str, path: str = "/", *, ignore_case: bool = True,
             max_matches: int = MAX_GREP_MATCHES) -> dict:
        """Lines matching `pattern` (a regular expression; a literal when it does not compile)
        in every readable file under `path`: [{path, line, text}]."""
        if not str(pattern or ""):
            raise FSError("grep needs a pattern")
        flags = re.IGNORECASE if ignore_case else 0
        try:
            rx = re.compile(str(pattern), flags)
        except re.error:
            rx = re.compile(re.escape(str(pattern)), flags)
        root = normalise(path)
        targets = self._files_under(root)
        matches: list = []
        truncated = False
        for target in targets:
            try:
                body = self.text_of(target)
            except FSError:
                continue
            for number, line in enumerate(body.split("\n"), start=1):
                if rx.search(line):
                    if len(matches) >= max(1, min(int(max_matches), MAX_GREP_MATCHES)):
                        truncated = True
                        break
                    matches.append({"path": target, "line": number,
                                    "text": line[:MAX_GREP_LINE]})
            if truncated:
                break
        out = {"ok": True, "pattern": str(pattern), "path": root, "matches": matches,
               "files_searched": len(targets)}
        if truncated:
            out["note"] = "more matches exist; narrow the pattern or the path"
        return out

    def write(self, path: str, content: str, *, append: bool = False) -> dict:
        path = normalise(path)
        route = route_of(path)
        content = str(content if content is not None else "")
        if route == SCRATCH:
            if path == SCRATCH:
                raise FSError("write to a file under /scratch, e.g. /scratch/notes.md")
            before = str((self.files.get(path) or {}).get("content") or "") if append else ""
            body = before + content
            if len(body) > MAX_WRITE_CHARS:
                raise FSError(f"a scratch file holds at most {MAX_WRITE_CHARS:,} characters")
            self.put(path, body)
            return {"ok": True, "path": path, "chars": len(body)}
        if route == MEMORIES:
            if self.memory is None:
                raise FSError("this run has no memory store, so nothing can be remembered here")
            namespace = path[len(MEMORIES) + 1:].strip("/")
            if not namespace:
                raise FSError("write to a namespace: `ls /memories` lists them")
            return {"ok": True, "path": path, **self.memory.write(namespace, content)}
        if route in (LARGE, SKILLS):
            raise FSError(f"{route} is read-only")
        raise FSError(f"{path} is outside the writable roots ({SCRATCH}, {MEMORIES})")

    def _files_under(self, root: str) -> list:
        route = route_of(root) if root != "/" else ""
        out: list = []
        if root == "/" or route in STATE_ROUTES:
            prefix = root.rstrip("/") + "/"
            out.extend(p for p in sorted(self.files)
                       if root == "/" or p == root or p.startswith(prefix))
        if root == "/" or route == SKILLS:
            out.extend(f"{SKILLS}/{n}/SKILL.md" for n in sorted(self.skills))
        if route == MEMORIES and self.memory is not None:
            out.extend(f"{MEMORIES}/{ns}" for ns in self.memory.namespaces())
        return out


_ABOUT = {
    SCRATCH: "your own notes for this run (write_file here); archived conversation lives here",
    LARGE: "tool results too large to show whole, by call id: read_file in pages or grep them",
    MEMORIES: "what BOSS remembers across runs, by namespace (read; write records a memory)",
    SKILLS: "the skills you hold: read one's SKILL.md when its description fits the task",
}


# ── Offloading a large result ─────────────────────────────────────────────────────────────────

def offload_path(call_id: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(call_id or "result"))[:120] or "result"
    return f"{LARGE}/{safe}"


def offload(fs: RunFS, *, call_id: str, tool: str, body: str, preview_lines: int = 10,
            tokens: int = 0) -> dict:
    """Write `body` (a tool result as the model would have read it) to the run FS and return
    the stub the model reads instead: where it is, how big, and its first `preview_lines`.

    A JSON result is stored ONE VALUE PER LINE (indented), so grep finds the record that holds
    the match and read_file pages it, instead of both meeting a single 100,000-character line."""
    try:
        parsed = json.loads(body)
    except (TypeError, ValueError):
        parsed = None
    if isinstance(parsed, (dict, list)):
        body = json.dumps(parsed, indent=1, ensure_ascii=False, default=str)
    path = fs.put(offload_path(call_id), body, meta={"tool": tool})
    lines = body.split("\n")
    head = "\n".join(line[:MAX_GREP_LINE] for line in lines[:max(0, preview_lines)])
    if len(lines) == 1 and len(head) > 2_000:
        head = head[:2_000]
    return {"status": "offloaded", "path": path, "chars": len(body),
            "approx_tokens": int(tokens or max(1, len(body) // 4)), "lines": len(lines),
            "preview": head,
            "note": (f"This result was too large to show whole, so NOTHING WAS CUT: all of it "
                     f"is at {path}. Use grep (pattern, path) to find what you need in it, or "
                     f"read_file (path, offset, limit) to page through it.")}


# ── The tools ─────────────────────────────────────────────────────────────────────────────────

def _obj(properties: dict, required: tuple = ()) -> dict:
    return {"type": "object", "properties": properties, "required": list(required),
            "additionalProperties": False}


#: (name, description, parameters, kind) — the ONE definition the kernel toolset and the tool
#: registry (tools/builtin.register_run_fs_tools) both read, so they cannot drift.
SPECS: tuple = (
    (LS, "List the files in your run filesystem: /scratch (your notes; archived conversation), "
         "/large_tool_results (results too large to show whole), /memories (what BOSS "
         "remembers, by namespace) and /skills (the skills you hold).",
     _obj({"path": {"type": "string", "description": "a directory, default /"}}), "read"),
    (READ_FILE, "Read one page of a file in your run filesystem: `limit` lines from line "
                "`offset` (0-based), numbered. Large files are read in pages; the reply says "
                "where the next page starts.",
     _obj({"path": {"type": "string"},
           "offset": {"type": "integer", "minimum": 0},
           "limit": {"type": "integer", "minimum": 1, "maximum": MAX_PAGE_LINES}},
          ("path",)), "read"),
    (GREP, "Search the files in your run filesystem for a regular expression (case-insensitive) "
           "and return the matching lines with their paths and line numbers. The fastest way "
           "to find one fact in a large offloaded result or in archived conversation.",
     _obj({"pattern": {"type": "string"},
           "path": {"type": "string", "description": "a file or directory, default /"}},
          ("pattern",)), "read"),
    (WRITE_FILE, "Write a file under /scratch (your own notes for this run — a plan, findings, "
                 "a draft) or record a memory by writing to /memories/<namespace>. Lessons "
                 "written to memory wait for a person's review before they bind.",
     _obj({"path": {"type": "string"}, "content": {"type": "string"},
           "append": {"type": "boolean"}}, ("path", "content")), "write"),
)


def invoke(fs: RunFS, name: str, args: dict) -> dict:
    """Run one FS tool. Raises FSError for a request the filesystem refuses."""
    args = dict(args or {})
    if name == LS:
        return fs.ls(str(args.get("path") or "/"))
    if name == READ_FILE:
        return fs.read(str(args.get("path") or ""), offset=int(args.get("offset") or 0),
                       limit=int(args.get("limit") or DEFAULT_PAGE_LINES))
    if name == GREP:
        return fs.grep(str(args.get("pattern") or ""), str(args.get("path") or "/"))
    if name == WRITE_FILE:
        return fs.write(str(args.get("path") or ""), str(args.get("content") or ""),
                        append=bool(args.get("append")))
    raise FSError(f"no run filesystem tool named {name!r}")


@dataclass
class RunFSToolSet:
    """The four tools as a kernel ToolSet over the run's own state. Their permission is None:
    the run's scratch and its offloaded results are its own state (the toolset's gates decide),
    and `/memories` is decided where it lands — the MemoryPort asks the memory API, which keeps
    the trust tiers and the review gate."""

    memory_for: Optional[Callable[[Any], Optional[MemoryPort]]] = None

    def schemas(self, *, step: int, state: Any) -> list:
        return [ToolSchema(name=name, description=description, parameters=parameters,
                           kind=kind, idempotency="idempotent" if kind == "write" else None)
                for name, description, parameters, kind in SPECS]

    def invoke(self, call: ToolCall, *, state: Any,
               approved: Optional[dict] = None) -> ToolResult:
        memory = self.memory_for(state) if self.memory_for is not None else None
        fs = RunFS(state, memory=memory)
        try:
            value = invoke(fs, call.name, dict(call.arguments or {}))
        except FSError as error:
            return ToolResult(call.id, call.name, "error", {"ok": False, "error": str(error)})
        return ToolResult(call.id, call.name, "ok", value)


def stub_text(stub: dict) -> str:
    return json.dumps(stub)


__all__ = ["FSError", "GREP", "LARGE", "LS", "MEMORIES", "MemoryPort", "READ_FILE",
           "ROUTES", "RUN_FS_TOOLS", "RunFS", "RunFSToolSet", "SCRATCH", "SKILLS", "SPECS",
           "WRITE_FILE", "invoke", "normalise", "offload", "offload_path", "route_of",
           "stub_text"]

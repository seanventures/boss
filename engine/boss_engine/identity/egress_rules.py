"""The egress proxy's rule table: what a run may send to GitHub, decided per request. Owned by S3.

IDENTITY.md §7.9–§7.10. Deny by default. A request is allowed only when:
  1. its repo is one of the RUN's repos (not merely the installation's) — A55;
  2. its method + path template is in the table below, and the run's effective GitHub permissions
     (grant ∩ passports' external ceilings ∩ step needs) reach the level the template needs;
  3. it is not an admin endpoint (collaborators, hooks, keys, protection, repo delete, MERGE —
     merging is a human approval, never an agent's);
  4. for a push (git-receive-pack): every ref update targets `refs/heads/boss/<run>-*`, none is a
     deletion of another ref, none is a force-push (non fast-forward), and no pushed commit changes
     `.github/workflows/` unless the grant explicitly holds `workflows: write` — A56.
     The pushed pack is parsed (commits and trees only) to decide 4.
"""
from __future__ import annotations

import fnmatch
import re
import struct
import zlib
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping, Optional

LEVELS = {"none": 0, "read": 1, "write": 2}
ZERO_SHA = "0" * 40


@dataclass(frozen=True)
class RuleDecision:
    allowed: bool
    template: str
    reason: str = ""
    need: Optional[tuple[str, str]] = None


def _deny(template: str, reason: str) -> RuleDecision:
    return RuleDecision(False, template, reason)


# (method, regex on the path after /repos/{o}/{r}, template, (permission, level) | None for metadata)
_API_RULES: list[tuple[str, str, str, tuple[str, str]]] = [
    ("GET", r"", "/repos/{o}/{r}", ("metadata", "read")),
    ("GET", r"/pulls", "/repos/{o}/{r}/pulls", ("pull_requests", "read")),
    ("GET", r"/pulls/\d+", "/repos/{o}/{r}/pulls/{n}", ("pull_requests", "read")),
    ("GET", r"/pulls/\d+/(files|commits|reviews|comments|requested_reviewers)", "/repos/{o}/{r}/pulls/{n}/{sub}", ("pull_requests", "read")),
    ("POST", r"/pulls", "/repos/{o}/{r}/pulls", ("pull_requests", "write")),
    ("PATCH", r"/pulls/\d+", "/repos/{o}/{r}/pulls/{n}", ("pull_requests", "write")),
    ("POST", r"/pulls/\d+/(reviews|comments|requested_reviewers)", "/repos/{o}/{r}/pulls/{n}/{sub}", ("pull_requests", "write")),
    ("POST", r"/pulls/\d+/comments/\d+/replies", "/repos/{o}/{r}/pulls/{n}/comments/{c}/replies", ("pull_requests", "write")),
    ("GET", r"/issues", "/repos/{o}/{r}/issues", ("issues", "read")),
    ("GET", r"/issues/\d+(/comments)?", "/repos/{o}/{r}/issues/{n}[/comments]", ("issues", "read")),
    ("POST", r"/issues", "/repos/{o}/{r}/issues", ("issues", "write")),
    ("POST", r"/issues/\d+/comments", "/repos/{o}/{r}/issues/{n}/comments", ("pull_requests", "write")),
    ("GET", r"/(commits|branches|tags|compare|git/(refs|ref|commits|trees|blobs|tags))(/.*)?", "/repos/{o}/{r}/{git-read}", ("contents", "read")),
    ("GET", r"/contents(/.*)?", "/repos/{o}/{r}/contents/{path}", ("contents", "read")),
    ("PUT", r"/contents/.+", "/repos/{o}/{r}/contents/{path}", ("contents", "write")),
    ("GET", r"/(check-runs|check-suites)(/.*)?|/commits/[0-9a-f]{7,40}/(check-runs|check-suites)", "/repos/{o}/{r}/checks", ("checks", "read")),
    ("GET", r"/commits/[0-9a-f]{7,40}/(status|statuses)", "/repos/{o}/{r}/commits/{sha}/status", ("statuses", "read")),
    ("GET", r"/actions/runs(/.*)?", "/repos/{o}/{r}/actions/runs", ("actions", "read")),
]
#: Never, whatever the permissions say.
_FORBIDDEN = re.compile(r"^/(collaborators|hooks|keys|deployments|environments|branches/[^/]+/protection|"
                        r"pulls/\d+/merge|merges|transfer|actions/secrets|actions/variables|dependabot|"
                        r"invitations|teams|rulesets|pages|import|forks)(/|$)")
_REPO_PATH = re.compile(r"^/repos/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)(/.*)?$")


class PathRefused(ValueError):
    """A request path the proxy will not interpret: refused before any rule is consulted."""


#: Encodings of structural characters: `/` `\` `.` `%` NUL. A path carrying one is refused outright
#: (never decoded and re-matched), so no rule can be matched on one path and another sent upstream.
_ENCODED_STRUCTURE = re.compile(r"%(2f|5c|2e|25|00)", re.IGNORECASE)
_PCHAR_SAFE = "/:@!$&'()*+,;=-._~"


def canonical_path(raw: str) -> tuple[str, str]:
    """(decoded, encoded) for a raw request path, or PathRefused. Refuses `..` and `.` segments,
    empty segments (`//`), backslashes, control characters, and encoded slashes, backslashes, dots,
    percents or NULs. `decoded` is what the rules match; `encoded` is what the upstream receives
    (the same path, canonically encoded), so the two can never disagree."""
    from urllib.parse import quote, unquote

    if not raw.startswith("/") or len(raw) > 2048:
        raise PathRefused("bad_path")
    if _ENCODED_STRUCTURE.search(raw):
        raise PathRefused("encoded_path_separator")
    if "\\" in raw or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in raw):
        raise PathRefused("bad_path_characters")
    segments = raw[1:].split("/")
    for seg in segments:
        if seg in ("", ".", ".."):
            raise PathRefused("dot_or_empty_segment")
    try:
        decoded_segments = [unquote(seg, errors="strict") for seg in segments]
    except UnicodeDecodeError as exc:
        raise PathRefused("bad_encoding") from exc
    for seg in decoded_segments:
        if seg in ("", ".", "..") or "/" in seg or "\\" in seg or any(ord(ch) < 0x20 for ch in seg):
            raise PathRefused("dot_or_empty_segment")
    decoded = "/" + "/".join(decoded_segments)
    encoded = "/" + "/".join(quote(seg, safe=_PCHAR_SAFE.replace("/", "")) for seg in decoded_segments)
    return decoded, encoded


def _level_ok(perms: Mapping[str, str], need: tuple[str, str]) -> bool:
    key, level = need
    if key == "metadata":
        return True
    return LEVELS.get(perms.get(key, "none"), 0) >= LEVELS[level]


def _in_run(repo: str, repos: Iterable[str]) -> bool:
    return repo.lower() in {r.lower() for r in repos}


#: What an agent may change on a pull request (PATCH): never its base, never anything but its own.
_PR_PATCH_FIELDS = frozenset({"title", "body", "state", "maintainer_can_modify"})


def check_api(method: str, path: str, *, perms: Mapping[str, str], repos: Iterable[str],
              body: Optional[Mapping[str, Any]] = None, push_pattern: str = "",
              pr_head: Optional[Callable[[str, int], Optional[str]]] = None) -> RuleDecision:
    """`path` is the DECODED canonical path (canonical_path). `pr_head(repo, number)` returns the
    head branch of a pull request (asked of GitHub by the proxy) for the PATCH rule."""
    method = method.upper()
    m = _REPO_PATH.match(path)
    if not m:
        return _deny("{non-repo}", "not_a_repo_path")         # /user, /orgs, /app, /installation…
    repo, tail = f"{m.group(1)}/{m.group(2)}", m.group(3) or ""
    if not _in_run(repo, repos):
        return _deny("/repos/{o}/{r}", "repo_outside_run")
    if method == "DELETE" and tail == "":
        return _deny("/repos/{o}/{r}", "admin_endpoint")
    if _FORBIDDEN.match(tail):
        return _deny("/repos/{o}/{r}/" + tail.strip("/").split("/")[0], "admin_endpoint")
    for rule_method, pattern, template, need in _API_RULES:
        if rule_method == method and re.fullmatch(pattern, tail):
            if not _level_ok(perms, need):
                return RuleDecision(False, template, "insufficient_permission", need)
            if method == "PUT" and template.endswith("/contents/{path}"):
                file_path = tail[len("/contents/"):]
                if file_path.startswith(".github/workflows/") and not _level_ok(perms, ("workflows", "write")):
                    return RuleDecision(False, template, "workflows_write", ("workflows", "write"))
                branch = (body or {}).get("branch")
                if not branch or not fnmatch.fnmatchcase(f"refs/heads/{branch}", push_pattern or "refs/heads/boss/*"):
                    return RuleDecision(False, template, "ref_not_allowed")
            if method == "POST" and template == "/repos/{o}/{r}/pulls":
                head = str((body or {}).get("head", "")).split(":")[-1]
                if not fnmatch.fnmatchcase(f"refs/heads/{head}", push_pattern or "refs/heads/boss/*"):
                    return RuleDecision(False, template, "pr_head_not_run_branch")
            if method == "POST" and template == "/repos/{o}/{r}/pulls/{n}/{sub}" and tail.endswith("/reviews"):
                # An approving review is a human's (branch protection counts it): never an agent's.
                if str((body or {}).get("event", "")).upper() not in ("", "COMMENT", "REQUEST_CHANGES"):
                    return RuleDecision(False, template, "approve_not_allowed")
            if method == "PATCH" and template == "/repos/{o}/{r}/pulls/{n}":
                if body is None or not set(body) <= _PR_PATCH_FIELDS:
                    return RuleDecision(False, template, "pr_field_not_allowed")
                number = int(tail.rsplit("/", 1)[1])
                head = pr_head(repo, number) if pr_head is not None else None
                if not head or not fnmatch.fnmatchcase(f"refs/heads/{head}", push_pattern or "refs/heads/boss/*"):
                    return RuleDecision(False, template, "pr_not_the_runs")
            return RuleDecision(True, template, need=need)
    return _deny("/repos/{o}/{r}/{other}", "not_in_rule_table")


_GIT_PATH = re.compile(r"^/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/(info/refs|git-upload-pack|git-receive-pack)$")


def check_git(method: str, path: str, query: Mapping[str, str], *, perms: Mapping[str, str],
              repos: Iterable[str]) -> RuleDecision:
    m = _GIT_PATH.match(path)
    if not m:
        return _deny("{git}", "not_a_git_path")
    repo, op = f"{m.group(1)}/{m.group(2)}", m.group(3)
    if not _in_run(repo, repos):
        return _deny("/{o}/{r}.git", "repo_outside_run")
    service = query.get("service") if op == "info/refs" else op
    if service == "git-upload-pack" and method.upper() in ("GET", "POST"):
        need = ("contents", "read")
    elif service == "git-receive-pack" and method.upper() in ("GET", "POST"):
        need = ("contents", "write")
    else:
        return _deny("/{o}/{r}.git/" + op, "bad_git_service")
    if not _level_ok(perms, need):
        return RuleDecision(False, "/{o}/{r}.git/" + op, "insufficient_permission", need)
    return RuleDecision(True, "/{o}/{r}.git/" + op, need=need)


# ---- receive-pack: commands and the pack ----------------------------------------------------------------

@dataclass(frozen=True)
class RefUpdate:
    old: str
    new: str
    ref: str

    @property
    def is_delete(self) -> bool:
        return self.new == ZERO_SHA

    @property
    def is_create(self) -> bool:
        return self.old == ZERO_SHA


def parse_receive_pack(body: bytes) -> tuple[list[RefUpdate], bytes]:
    """pkt-line commands up to the flush packet, then the pack (absent for delete-only pushes)."""
    updates: list[RefUpdate] = []
    pos = 0
    while True:
        if pos + 4 > len(body):
            raise ValueError("truncated pkt-line")
        size = int(body[pos:pos + 4], 16)
        if size == 0:
            pos += 4
            break
        line = body[pos + 4:pos + size]
        pos += size
        line = line.split(b"\0", 1)[0].rstrip(b"\n")
        if line.startswith(b"shallow ") or line.startswith(b"push-cert"):
            raise ValueError("unsupported push feature")
        parts = line.decode("utf-8", "strict").split(" ")
        if len(parts) != 3 or not all(re.fullmatch(r"[0-9a-f]{40}", p) for p in parts[:2]):
            raise ValueError("bad command")
        updates.append(RefUpdate(*parts))
    rest = body[pos:]
    # push-options (after a second flush) are not used by BOSS; refuse rather than guess.
    if rest and not rest.startswith(b"PACK"):
        raise ValueError("unexpected data after commands")
    return updates, rest


OBJ_COMMIT, OBJ_TREE, OBJ_BLOB, OBJ_TAG, OBJ_OFS_DELTA, OBJ_REF_DELTA = 1, 2, 3, 4, 6, 7
_TYPE_NAMES = {OBJ_COMMIT: b"commit", OBJ_TREE: b"tree", OBJ_BLOB: b"blob", OBJ_TAG: b"tag"}


def _apply_delta(base: bytes, delta: bytes) -> bytes:
    def varint(i: int) -> tuple[int, int]:
        shift = value = 0
        while True:
            b = delta[i]
            i += 1
            value |= (b & 0x7F) << shift
            shift += 7
            if not b & 0x80:
                return value, i
    _, i = varint(0)
    size, i = varint(i)
    out = bytearray()
    while i < len(delta):
        op = delta[i]
        i += 1
        if op & 0x80:
            off = ln = 0
            for bit in range(4):
                if op & (1 << bit):
                    off |= delta[i] << (8 * bit)
                    i += 1
            for bit in range(3):
                if op & (1 << (4 + bit)):
                    ln |= delta[i] << (8 * bit)
                    i += 1
            ln = ln or 0x10000
            out += base[off:off + ln]
        elif op:
            out += delta[i:i + op]
            i += op
        else:
            raise ValueError("bad delta")
    if len(out) != size:
        raise ValueError("delta size")
    return bytes(out)


#: A push body larger than this is refused before it is parsed (the pack size limit).
MAX_PUSH_BYTES = 64 * 1024 * 1024
#: The most a pack may inflate to, in total and per object (a zip bomb is refused, not inflated).
MAX_INFLATED_BYTES = 512 * 1024 * 1024
MAX_OBJECT_BYTES = 100 * 1024 * 1024


class UnverifiablePack(ValueError):
    """A thin pack whose delta bases could not be fetched and verified."""


def parse_pack(pack: bytes, *, fetch_base: Optional[Callable[[str], Optional[tuple[int, bytes]]]] = None
               ) -> dict[str, tuple[int, bytes]]:
    """sha -> (type, data) for every object in the pack. A REF_DELTA against an object NOT in the
    pack (a thin pack — git's default for a push) is resolved by fetching its base from the
    remote (`fetch_base(sha)` -> (type, data), verified against the sha); a delta that still
    cannot be resolved raises UnverifiablePack: its type and sha are unknown, so nothing can be
    said about what it changes."""
    import hashlib

    if not pack:
        return {}
    if len(pack) > MAX_PUSH_BYTES:
        raise ValueError("pack too large")
    if pack[:4] != b"PACK" or len(pack) < 32:
        raise ValueError("not a pack")
    version, count = struct.unpack(">II", pack[4:12])
    if version not in (2, 3) or count > 200_000:
        raise ValueError("bad pack header")
    pos = 12
    inflated = 0
    by_offset: dict[int, tuple[int, bytes]] = {}
    pending: list[tuple[int, int, Any, bytes]] = []        # (offset, kind, base ref, delta)
    for _ in range(count):
        start = pos
        c = pack[pos]
        pos += 1
        kind = (c >> 4) & 7
        while c & 0x80:
            c = pack[pos]
            pos += 1
        base: Any = None
        if kind == OBJ_OFS_DELTA:
            c = pack[pos]
            pos += 1
            off = c & 0x7F
            while c & 0x80:
                c = pack[pos]
                pos += 1
                off = ((off + 1) << 7) | (c & 0x7F)
            base = start - off
        elif kind == OBJ_REF_DELTA:
            base = pack[pos:pos + 20].hex()
            pos += 20
        d = zlib.decompressobj()
        data = d.decompress(pack[pos:], MAX_OBJECT_BYTES)
        if not d.eof:
            raise ValueError("truncated or oversized object")
        inflated += len(data)
        if inflated > MAX_INFLATED_BYTES:
            raise ValueError("pack inflates too far")
        pos = len(pack) - len(d.unused_data)
        if kind in _TYPE_NAMES:
            by_offset[start] = (kind, data)
        else:
            pending.append((start, kind, base, data))
    by_sha: dict[str, tuple[int, bytes]] = {}

    def sha_of(kind: int, data: bytes) -> str:
        return hashlib.sha1(_TYPE_NAMES[kind] + b" " + str(len(data)).encode() + b"\0" + data).hexdigest()

    for kind, data in by_offset.values():
        by_sha[sha_of(kind, data)] = (kind, data)
    progress = True
    while pending and progress:
        progress = False
        for item in list(pending):
            start, kind, base, delta = item
            resolved = by_offset.get(base) if kind == OBJ_OFS_DELTA else by_sha.get(base)
            if resolved is None:
                continue
            obj = (resolved[0], _apply_delta(resolved[1], delta))
            by_offset[start] = obj
            by_sha[sha_of(*obj)] = obj
            pending.remove(item)
            progress = True
        if not progress and pending and fetch_base is not None:
            # Thin pack: fetch each missing REF_DELTA base once, verify it, and go round again.
            for start, kind, base, delta in list(pending):
                if kind != OBJ_REF_DELTA or base in by_sha:
                    continue
                fetched = fetch_base(base)
                if fetched is not None and fetched[0] in _TYPE_NAMES and sha_of(*fetched) == base:
                    by_sha[base] = fetched
                    progress = True
    if pending:
        raise UnverifiablePack("unresolvable delta")
    return by_sha


def _commit_info(data: bytes) -> tuple[str, list[str]]:
    tree, parents = "", []
    for line in data.split(b"\n"):
        if not line:
            break
        if line.startswith(b"tree "):
            tree = line[5:].decode()
        elif line.startswith(b"parent "):
            parents.append(line[7:].decode())
    return tree, parents


def _tree_entries(data: bytes) -> dict[bytes, tuple[bytes, str]]:
    out: dict[bytes, tuple[bytes, str]] = {}
    i = 0
    while i < len(data):
        sp = data.index(b" ", i)
        nul = data.index(b"\0", sp)
        mode, name = data[i:sp], data[sp + 1:nul]
        out[name] = (mode, data[nul + 1:nul + 21].hex())
        i = nul + 21
    return out


#: (tree sha) -> {name: (mode, sha)} for a tree NOT in the pack, from the remote; None if unknown.
RemoteTree = Callable[[str], Optional[dict[bytes, tuple[bytes, str]]]]
#: (commit sha) -> its tree sha, for a commit NOT in the pack; None if unknown.
RemoteCommitTree = Callable[[str], Optional[str]]


def check_push(updates: list[RefUpdate], objects: Mapping[str, tuple[int, bytes]], *, push_pattern: str,
               perms: Mapping[str, str], is_ancestor: Callable[[str, str], bool],
               remote_tree: Optional[RemoteTree] = None,
               remote_commit_tree: Optional[RemoteCommitTree] = None) -> RuleDecision:
    """`is_ancestor(old, descendant)` asks GitHub (compare API) about commits outside the pack.

    Workflows: EVERY pushed commit's `.github/workflows` must be identical to its FIRST parent's
    (resolved from the pack, else from the remote). So neither an intermediate commit that changes
    them and a later one that changes them back, nor a commit that reuses an existing tree (not in
    the pack, so "unchanged" by presence) with other workflows, nor a merge that takes them from a
    second parent, gets through without `workflows: write`."""
    tpl = "/{o}/{r}.git/git-receive-pack"
    if not updates:
        return _deny(tpl, "no_updates")
    workflows_ok = _level_ok(perms, ("workflows", "write"))
    for u in updates:
        if not fnmatch.fnmatchcase(u.ref, push_pattern):
            return _deny(tpl, "ref_not_allowed")               # main, other branches, tags
        if u.is_delete:
            continue                                           # deleting the run's own branch
        commit = objects.get(u.new)
        if commit is None or commit[0] != OBJ_COMMIT:
            return _deny(tpl, "unverifiable_push")             # new tip not in the pack: refuse
        # Walk the pushed commits: find workflow changes and decide fast-forward.
        seen: set[str] = set()
        boundary: set[str] = set()
        frontier = [u.new]
        reached_old = False
        while frontier:
            sha = frontier.pop()
            if sha in seen:
                continue
            seen.add(sha)
            if sha == u.old:
                reached_old = True
                continue
            obj = objects.get(sha)
            if obj is None:
                boundary.add(sha)
                continue
            if obj[0] != OBJ_COMMIT:
                return _deny(tpl, "unverifiable_push")
            tree, parents = _commit_info(obj[1])
            if not workflows_ok:
                verdict = _workflows_unchanged(tree, parents, objects, remote_tree, remote_commit_tree)
                if verdict is None:
                    return _deny(tpl, "unverifiable_push")
                if not verdict:
                    return RuleDecision(False, tpl, "workflows_write", ("workflows", "write"))
            frontier.extend(parents)
        if not u.is_create and not reached_old:
            if not boundary or not any(b == u.old or is_ancestor(u.old, b) for b in boundary):
                return _deny(tpl, "force_push")
    return RuleDecision(True, tpl)


_ABSENT = "absent"


def _entries(tree_sha: str, objects: Mapping[str, tuple[int, bytes]],
             remote_tree: Optional[RemoteTree]) -> Optional[dict[bytes, tuple[bytes, str]]]:
    obj = objects.get(tree_sha)
    if obj is not None:
        return _tree_entries(obj[1]) if obj[0] == OBJ_TREE else None
    return remote_tree(tree_sha) if remote_tree is not None else None


def _workflows_id(tree_sha: str, objects: Mapping[str, tuple[int, bytes]],
                  remote_tree: Optional[RemoteTree]) -> Optional[str]:
    """The sha of `.github/workflows` in a root tree, `absent`, or None when it cannot be known."""
    root = _entries(tree_sha, objects, remote_tree)
    if root is None:
        return None
    gh = root.get(b".github")
    if gh is None:
        return _ABSENT
    if not gh[0].startswith(b"4"):                    # `.github` as a file or a link
        return "nondir:" + gh[1]
    gh_entries = _entries(gh[1], objects, remote_tree)
    if gh_entries is None:
        return None
    wf = gh_entries.get(b"workflows")
    if wf is None:
        return _ABSENT
    return ("" if wf[0].startswith(b"4") else "nondir:") + wf[1]


def _workflows_unchanged(tree: str, parents: list[str], objects: Mapping[str, tuple[int, bytes]],
                         remote_tree: Optional[RemoteTree],
                         remote_commit_tree: Optional[RemoteCommitTree]) -> Optional[bool]:
    mine = _workflows_id(tree, objects, remote_tree)
    if mine is None:
        return None
    if not parents:
        return mine == _ABSENT                          # a root commit may not bring workflows
    first = parents[0]
    obj = objects.get(first)
    if obj is not None:
        if obj[0] != OBJ_COMMIT:
            return None
        parent_tree = _commit_info(obj[1])[0]
    else:
        parent_tree = remote_commit_tree(first) if remote_commit_tree is not None else None
    if not parent_tree:
        return None
    theirs = _workflows_id(parent_tree, objects, remote_tree)
    if theirs is None:
        return None
    return mine == theirs


def _touches_workflows(root_tree: str, objects: Mapping[str, tuple[int, bytes]]) -> bool:
    """Kept for callers outside the push check: does the pack carry a changed workflows tree?"""
    root = objects.get(root_tree)
    if root is None:
        return False
    gh = _tree_entries(root[1]).get(b".github")
    if gh is None:
        return False
    gh_tree = objects.get(gh[1])
    if gh_tree is None:
        return False
    wf = _tree_entries(gh_tree[1]).get(b"workflows")
    return wf is None or wf[1] in objects


def raw_tree_from_api(entries: list[Mapping[str, Any]]) -> bytes:
    """Rebuild a tree object's bytes from GitHub's JSON (`GET /git/trees/{sha}`), so a thin-pack
    base can be verified against its sha before a delta is applied to it."""
    out = bytearray()
    for e in entries:
        mode = str(e["mode"]).lstrip("0") or "0"
        out += f"{mode} {e['path']}".encode() + b"\0" + bytes.fromhex(str(e["sha"]))
    return bytes(out)

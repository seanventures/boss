"""What "Share this run" masks before the person even sees the bundle (docs/product/RUN_SCORECARDS.md).

PRECISION AND RECALL, both measured (apps/api/tests/test_improve_boss_share_scrub.py runs the
independent tester's corpus: every known name masked, at most 2 of 28 harmless tokens touched).

  recall     this install's own names, from every table that holds one — repositories, branches,
             apps, deploy hosts and targets, previews, projects, orgs, workspaces, people (full
             names and handles), workflow inputs, the operator's "Always mask" words — each in its
             separator variants (snake_case, kebab-case, camelCase, joined); a word after ssh/scp/
             ping is a host; IPv4, IPv6, URLs, emails and secrets (the Activity log's scrub)
  precision  a dotted token is a HOST only if it ends in a real public suffix (a bundled list,
             minus the suffixes that are also file extensions or code: md, sh, py, map, name …)
             or is a known install host — so `self.items`, `os.path.join`, `package.json` stay;
             a PATH must be path-shaped (rooted, ≥ 2 separators, or ending in a file name); a
             bare FILE only if it is a known file of this install's repositories; a person's name
             is masked whole, and its parts only when ≥ 4 letters and not a common or role word

Timestamps are kept as they are (set aside before the scrub, put back after). The person still
reads and edits the result, under a plain "Check for anything private" notice.
"""
from __future__ import annotations

import re
from typing import Iterable

from sqlalchemy import text

from .redaction import redact

_TIMESTAMP = re.compile(
    r"\b\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)?\b"
    r"|\b\d{2}:\d{2}:\d{2}(?:\.\d+)?\b|\b\d{2}:\d{2}\b")
_URL = re.compile(r"\b[a-z][a-z0-9+.\-]*://[^\s'\"<>)\]]+|\bgit@[\w.\-]+:[\w./\-]+", re.I)
_IPV6 = re.compile(r"(?<![\w:])(?:(?:[0-9a-f]{1,4}:){7}[0-9a-f]{1,4}"
                   r"|(?:[0-9a-f]{1,4}(?::[0-9a-f]{1,4})*)?::(?:[0-9a-f]{1,4}(?::[0-9a-f]{1,4})*)?)"
                   r"(?![\w:])", re.I)
#: A word right after one of these is a machine: `ssh buildbox07`, `scp x deploy@web3:/srv`.
_AFTER_REMOTE = re.compile(r"(?i)\b(?:ssh|scp|sftp|rsync|ping|telnet|mosh)\s+(?:-\S+\s+)*"
                           r"(?:[\w.\-]+@)?(?P<host>[A-Za-z][\w\-]{1,62})(?![\w\-])")

#: PUBLIC SUFFIXES a host may end in (a bundled subset of the public-suffix list: the generic and
#: country suffixes in common use, the multi-label ones, and the private-use names). Left out on
#: purpose: suffixes that are also file extensions or common code words, where a dotted token
#: is far more often `README.md`, `deploy.sh`, `user.name` or `Array.prototype.map` than a host.
_SUFFIXES = frozenset("""
com net org edu gov mil int io ai co dev app cloud tech site online info biz xyz me tv cc us uk de
fr nl be ch at se no dk fi is ie es pt it gr pl cz sk hu ro bg hr si lt lv ee ua by kz tr il ae
sa qa in pk bd lk np sg my id th vn ph hk tw jp kr cn au nz za ng ke eg ma br ar cl mx pe ve uy ca
ru eu asia global network systems solutions services digital agency studio software company
business email team works space live store shop host hosting server engineering
local localhost internal intranet lan corp home localdomain test example invalid
co.uk org.uk ac.uk gov.uk com.au net.au org.au co.nz co.jp co.in co.za com.br com.mx com.cn
com.sg com.hk com.tr home.arpa
""".split())
_EXCLUDED_SUFFIXES = frozenset("md sh py rs pl ps so mk am map name zip mov run json yaml yml".split())

#: Words never masked on their own, whatever an install named itself: roles, product words,
#: branch names and the words a log is made of. A full name containing them still is.
STOP_WORDS = frozenset("""
boss system release manager console agent agents operator executive chief officer founder runtime
admin administrator user users owner team member members worker workers bot ceo cto cfo coo cpo
ops dev prod main master develop development staging production trunk head default project
projects workspace org organization company test tests demo local engineer engineering software
product lead director president architect architecture technology the and for with build deploy
review research panel workflow workflows step steps app apps service services api web server
client data error info debug warning note
""".split())

_HOST_CANDIDATE = re.compile(r"(?<![\w@.\-/])(?:[a-z0-9](?:[a-z0-9_\-]{0,62})?\.)+[a-z][a-z0-9\-]{0,62}"
                             r"(?![\w\-]|\.\w)", re.I)
_PATH_CANDIDATE = re.compile(r"(?<![\w:/.\-])(?:~|\.{1,2})?/?(?:[\w.\-@]+/)+[\w.\-@]+"
                             r"|\b[A-Za-z]:\\[^\s'\"]+")
_FILE_NAME = re.compile(r"\.[A-Za-z][A-Za-z0-9]{0,7}$")

#: The fields that NAME things, in task specs, run state, workflow inputs.
_NAME_KEYS = ("repo", "repository", "owner", "base_branch", "branch", "head_branch", "ref", "app",
              "app_name", "app_slug", "project", "project_name", "service", "host", "hostname")


def _words(name: str) -> list[str]:
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", name)
    return [w for w in re.split(r"[\s_\-./]+", spaced) if w]


def variants(name: str) -> set[str]:
    """A name and its separator variants: `Payroll Pilot` → payroll-pilot, payroll_pilot,
    payrollPilot, PayrollPilot, payrollpilot."""
    out = {name}
    words = _words(name)
    if len(words) >= 2:
        low = [w.lower() for w in words]
        out |= {"-".join(low), "_".join(low), " ".join(words), "".join(low),
                low[0] + "".join(w.capitalize() for w in low[1:]),
                "".join(w.capitalize() for w in low)}
    return out


def _usable(name: str) -> bool:
    n = name.strip()
    if len(n) < 3 or n.isdigit() or n.lower() in STOP_WORDS:
        return False
    # A name made only of role and common words ("Release Manager", "Executive Console
    # Operator") identifies nobody, and masking it masks ordinary sentences.
    words = _words(n)
    return not (words and all(w.lower() in STOP_WORDS for w in words))


def install_names(connection, project_id: str) -> list[tuple[str, str]]:
    """(label, name) for every name this install knows that must not leave in a shared run.
    Labels: REPO, NAME, HOST (a known machine), FILE (a known repository file)."""
    out: list[tuple[str, str]] = []

    def rows(sql: str, **p) -> Iterable:
        try:
            with connection.begin_nested():
                return connection.execute(text(sql), p).all()
        except Exception:  # noqa: BLE001 — a missing table in a partial schema hides nothing
            return []

    def named(blob, default_label: str = "NAME") -> None:
        if not isinstance(blob, dict):
            return
        for key in _NAME_KEYS:
            value = blob.get(key)
            if isinstance(value, str) and value.strip():
                label = "REPO" if key in ("repo", "repository", "owner") else (
                    "HOST" if key in ("host", "hostname") else default_label)
                out.append((label, value.strip()))
                if "/" in value:  # owner/name, feature/x: the parts too
                    out.extend((label, part) for part in value.split("/") if part.strip())

    for owner, name, full in rows("SELECT owner, name, full_name FROM github_repository_bindings"):
        out.extend([("REPO", full), ("REPO", owner), ("REPO", name)])
    for spec, fc, state in rows("SELECT spec, finalize_ctx, state FROM code_task_runs "
                                "WHERE project_id = :p ORDER BY created_at DESC LIMIT 300",
                                p=project_id):
        named(spec)
        named(fc)
        if isinstance(state, dict):
            for key in ("impl_files", "test_files"):
                for f in state.get(key) or []:
                    if isinstance(f, str) and f.strip():
                        out.extend([("FILE", f.strip()), ("FILE", f.strip().rsplit("/", 1)[-1])])
    for (inp,) in rows("SELECT input FROM workflow_runs WHERE project_id = :p "
                       "ORDER BY created_at DESC LIMIT 300", p=project_id):
        named(inp)
    for name, address in rows("SELECT name, address FROM deployment_hosts"):
        out.extend([("HOST", str(name)), ("HOST", str(address))])
    for app, repo, ref in rows("SELECT app_name, repo, target_ref FROM deployment_targets"):
        out.append(("NAME", str(app)))
        named({"repo": repo})
        if ref and "/" not in str(ref) and "." in str(ref):
            out.append(("HOST", str(ref)))
    for repo, ref in rows("SELECT repository, ref FROM stack_deployments WHERE project_id = :p",
                          p=project_id):
        named({"repo": repo, "branch": ref})
    for table in ("app_previews", "preview_sandboxes"):
        for repo, branch in rows(f"SELECT repo, branch FROM {table}"):
            named({"repo": repo, "branch": branch})
    for row in rows("SELECT id, name, slug FROM projects"):
        out.extend(("NAME", str(v)) for v in row if v)
    for row in rows("SELECT id, name FROM orgs"):
        out.extend(("NAME", str(v)) for v in row if v)
    for row in rows("SELECT id, name FROM workspaces"):
        out.extend(("NAME", str(v)) for v in row if v)
    for table in ("project_members", "org_members"):
        for mid, display in rows(f"SELECT member_id, display_name FROM {table}"):
            out.extend([("NAME", str(mid)), ("NAME", str(display))])
            # A part of a person's name only when it could identify them: ≥ 4 letters, not a
            # role or common word ("Executive Console Operator" is not three secrets).
            out.extend(("NAME", part) for part in _words(str(display))
                       if len(part) >= 4 and part.lower() not in STOP_WORDS)
    try:
        from .improve_boss import mask_words

        out.extend(("NAME", w) for w in mask_words(connection))
    except Exception:  # noqa: BLE001
        pass
    # Variants, filtered, longest first ("acme/payroll" before "acme"), each once.
    seen: set[str] = set()
    names: list[tuple[str, str]] = []
    expanded = [(label, v) for label, n in out for v in (variants(n) if label != "FILE" else {n})]
    for label, name in sorted(expanded, key=lambda x: -len(x[1])):
        n = name.strip()
        if not _usable(n) or n.lower() in seen:
            continue
        seen.add(n.lower())
        names.append((label, n))
    return names


def _is_host(token: str, known: set[str]) -> bool:
    low = token.lower().rstrip(".")
    if low in known or low.split(".", 1)[0] in known:
        return True
    labels = low.split(".")
    if len(labels) < 2:
        return False
    last, last2 = labels[-1], ".".join(labels[-2:])
    if last in _EXCLUDED_SUFFIXES:
        return False
    return last in _SUFFIXES or last2 in _SUFFIXES


def _is_path(token: str, known_files: set[str]) -> bool:
    if token.startswith(("/", "~/", "./", "../")) or re.match(r"^[A-Za-z]:\\", token):
        return True
    if token.count("/") >= 2:
        return True
    last = token.rsplit("/", 1)[-1]
    return bool(_FILE_NAME.search(last)) or last.lower() in known_files


def scrub(raw: str, names: list[tuple[str, str]]) -> tuple[str, dict[str, int]]:
    """(masked text, {label: count})."""
    keep: list[str] = []

    def hold(m: re.Match) -> str:
        keep.append(m.group(0))
        return f"\x00{len(keep) - 1}\x00"

    work = _TIMESTAMP.sub(hold, raw or "")
    counts: dict[str, int] = {}

    def bump(label: str) -> str:
        counts[label] = counts.get(label, 0) + 1
        return f"[{label}]"

    known_hosts = {n.lower() for label, n in names if label == "HOST"}
    known_files = {n.lower() for label, n in names if label == "FILE" and "/" not in n}

    work = _URL.sub(lambda m: bump("URL"), work)
    work, found = redact(work)
    for k, v in found.items():
        counts[k] = counts.get(k, 0) + v
    work = _IPV6.sub(lambda m: bump("IP"), work)
    work = _AFTER_REMOTE.sub(
        lambda m: m.group(0)[: m.start("host") - m.start(0)] + bump("HOST"), work)
    # Hosts and paths before names: `db-01.acme.internal` is one host and `/srv/acme/x.py` one
    # path — not a host or a path with a name inside it.
    work = _PATH_CANDIDATE.sub(lambda m: bump("PATH") if _is_path(m.group(0), known_files)
                               else m.group(0), work)
    work = _HOST_CANDIDATE.sub(lambda m: bump("HOST") if _is_host(m.group(0), known_hosts)
                               else m.group(0), work)
    for label, name in names:
        if label == "FILE" and "/" not in name:
            continue  # a bare known file name is matched below, as a whole token
        pattern = re.compile(rf"(?<![A-Za-z0-9]){re.escape(name)}(?![A-Za-z0-9])", re.I)
        work = pattern.sub(lambda m, lab=("PATH" if label == "FILE" else label): bump(lab), work)
    if known_files:
        work = re.sub(r"(?<![\w./\-])[\w.\-]+(?![\w.\-])",
                      lambda m: bump("FILE") if m.group(0).lower() in known_files else m.group(0),
                      work)
    work = re.sub(r"\x00(\d+)\x00", lambda m: keep[int(m.group(1))], work)
    return work, counts

"""What "Share this run" masks before the person even sees the bundle (docs/product/RUN_SCORECARDS.md).

PRECISION AND RECALL, both measured (apps/api/tests/test_improve_boss_share_scrub.py and
test_improve_boss_recheck4.py run the independent tester's corpora: every known name masked, the
harmless tokens kept).

  recall     this install's own names, from every table that holds one — repositories, branches,
             apps, deploy hosts and targets (their domains and public URL too), previews,
             projects, orgs, workspaces, people (full names and handles), EVERY string a workflow
             was started with, the operator's "Always mask" words — each in its separator
             variants (snake_case, kebab-case, camelCase, joined) and inside compounds
             (`KestrelApiService`, `kestrel-api2`); the machine a command or an error names —
             ssh, ping, telnet, scp/rsync `user@host:`, psql/mysql `-h`, ENOTFOUND, `lookup X
             on`; UNC paths; IPv4, IPv6, URLs, emails and secrets (the Activity log's scrub)
  precision  a dotted token is a HOST when it ends in a public suffix (the full Public Suffix
             List, bundled, plus the private-use names .local, .internal, .svc, .consul …) —
             except where that suffix is also a file extension or a code member (`README.md`,
             `deploy.sh`, `user.name`, `response.data.map`): then it is a host only with ≥ 2
             labels before the suffix and no code look, or after a host-taking tool, and a file
             handed to a command (`bash get.sh`) stays a file; a PATH must be path-shaped; a bare
             FILE only if it is a known file of this install's repositories; a person's name is
             masked whole, and its parts only when ≥ 4 letters and not a common or role word

Timestamps are kept as they are (set aside before the scrub, put back after). The person still
reads and edits the result, under a plain "Check for anything private" notice — the mandatory
review for what no rule here can know.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy import text

from .redaction import redact

_TIMESTAMP = re.compile(
    r"\b\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)?\b"
    r"|\b\d{2}:\d{2}:\d{2}(?:\.\d+)?\b|\b\d{2}:\d{2}\b")
_URL = re.compile(r"\b[a-z][a-z0-9+.\-]*://[^\s'\"<>)\]]+|\bgit@[\w.\-]+:[\w./\-]+", re.I)
_IPV6 = re.compile(r"(?<![\w:])(?:(?:[0-9a-f]{1,4}:){7}[0-9a-f]{1,4}"
                   r"|(?:[0-9a-f]{1,4}(?::[0-9a-f]{1,4})*)?::(?:[0-9a-f]{1,4}(?::[0-9a-f]{1,4})*)?)"
                   r"(?![\w:])", re.I)
#: `\\filesrv01\share\x` — a Windows share names its machine.
_UNC = re.compile(r"\\\\[A-Za-z0-9][\w.\-$]*\\[^\s'\"<>|]*")

# ------------------------------------------------------------------------------ public suffixes
#: PRIVATE-USE suffixes the public list does not carry, and a log full of internal machines does.
_PRIVATE_SUFFIXES = frozenset("""
local localhost internal intranet lan corp home localdomain test example invalid home.arpa svc
consul vpn cluster.local fritz.box docker.internal private wg
""".split())
#: Real top-level domains that are ALSO common file extensions — `README.md`, `deploy.sh`,
#: `setup.py`, `Cargo.rs`, `lib.so`, `Makefile.am`, `build.mk`. One label before one is a file;
#: two (`get.acme.sh`, `docs.zephyrco.md`) is a host.
_EXT_SUFFIXES = frozenset("""
md sh py rs pl ps so mk am zip mov map pid properties java fish inc in id ml pm tf sc st mo
""".split())
#: Real top-level domains that are ALSO everyday member names in code — `user.name`, `job.run`,
#: `logger.info`, `this.page`, `self.app`. The same rule as a file extension.
_CODE_SUFFIXES = frozenset("""
name run info data date new next now open read post show save search call link list live menu
page plus style test tools top watch get docs email events group help host key mail media news
one pay photo place press pro rest review sale select site space store stream talk team tech
tips today video vote web win zone fit fun game games global ink law meet money movie music
network party pet play report safe shop social spot support systems tax trade travel tube work
works world app dev box cloud codes cool fail foo free hot how ltd
""".split())
#: A dotted token that starts like code is code, whatever it ends in.
_CODE_RECEIVERS = frozenset("""
self this cls obj os sys response res resp req request data props state ctx context config
settings window document process module exports console logger log item x e err error event el
node user args kwargs opts options params result results value values list dict df np pd torch
math json array object string number promise ev evt target model view job task run app
""".split())
#: Commands that take a FILE: `bash get.acme.sh` names a script, not a machine.
_FILE_COMMANDS = frozenset("""
bash sh zsh source python python3 node deno bun ruby perl php cat less more head tail vim vi nano
code open chmod chown cp mv rm ls touch tar unzip gzip gunzip make cargo go pytest npx tsx
""".split())

_PSL_FILE = Path(__file__).resolve().parent.parent / "data" / "public_suffix_list.dat"
_PSL: tuple[frozenset[str], frozenset[str]] | None = None


def _psl() -> tuple[frozenset[str], frozenset[str]]:
    """(rules, wildcard parents) of the bundled Public Suffix List (MPL-2.0, publicsuffix.org;
    THIRD-PARTY-NOTICES.md), plus the private-use names. `!exception` rules only shorten a
    suffix, so they are ignored."""
    global _PSL
    if _PSL is None:
        rules: set[str] = set(_PRIVATE_SUFFIXES)
        wild: set[str] = set()
        for line in _PSL_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith(("//", "!")):
                continue
            rule = line.split()[0].lower()
            (wild.add(rule[2:]) if rule.startswith("*.") else rules.add(rule))
        _PSL = (frozenset(rules), frozenset(wild))
    return _PSL


def public_suffix(labels: list[str]) -> int:
    """How many trailing labels are the public suffix (0: none — not a host by its shape)."""
    rules, wild = _psl()
    for i in range(len(labels)):
        cand = ".".join(labels[i:])
        if cand in rules:
            return len(labels) - i
        if i + 1 < len(labels) and ".".join(labels[i + 1:]) in wild:
            return len(labels) - i
    return 0


# ------------------------------------------------------------------------------ host-taking tools
#: The single-letter options that take an argument, per tool, skipped with it:
#: `ssh -i key -o K=v root@box` names `box` — never `K` or the key.
_HOST_TOOLS = {
    "ssh": "bcDEeFIiJLlmOoPpQRSWw", "mosh": "", "telnet": "l", "ping": "ciIlmMpQsStTwW",
    "ping6": "ciIlmMpQsStTwW", "traceroute": "fgimNpqstwz", "nc": "psiwq", "dig": "bcfkpqtxy",
    "nslookup": "", "mtr": "ciIsaBf",
}
_TOOL_RE = re.compile(r"(?<![\w\-./])(" + "|".join(sorted(_HOST_TOOLS, key=len, reverse=True))
                      + r")(?=[ \t])")
_HOSTNAME = re.compile(r"^(?:[A-Za-z0-9][A-Za-z0-9_\-.]*[A-Za-z0-9]|[A-Za-z])$")
#: A copy command names its machine as `user@host:path` or `host:path`.
_COPY_RE = re.compile(r"(?<![\w\-./])(?:scp|rsync|sftp)(?=[ \t])")
_COPY_TARGET = re.compile(r"(?<![\w\-./@:])(?:[\w.\-]+@)?(?P<host>[A-Za-z0-9][\w\-.]*):(?=[/~\w.\[]|$|\s)")
#: `-h host`, `-hhost`, `--host=host`, `--host host` for the database and cache clients.
_DB_HOST = re.compile(
    r"(?<![\w\-./])(?:psql|pg_dump|pg_dumpall|pg_restore|pg_isready|pg_basebackup|mysql|mysqldump|"
    r"mysqladmin|mariadb|redis-cli|mongosh|mongo|clickhouse-client|cqlsh)(?=[ \t])[^\n;|&]*?"
    r"(?:[ \t]-h[ \t]*|[ \t]--host[= \t][ \t]*)(?P<host>[A-Za-z0-9][\w\-.]*)")
#: The machine an error names.
_ERROR_HOST = re.compile(
    r"(?:\bENOTFOUND|\bEAI_AGAIN|\bEHOSTUNREACH|\blookup|[Rr]esolve host:?|[Uu]nknown host:?|"
    r"host name|connect to host)[ \t]+[\"'`]?(?P<host>[A-Za-z][\w\-.]*[A-Za-z0-9])"
    r"(?=[\"'`]?(?:\s|:|$|,|;|\)))")


def _hostlike(token: str) -> bool:
    return bool(_HOSTNAME.match(token)) and not token.replace(".", "").isdigit()


def _tool_hosts(work: str, bump) -> str:
    """Mask the machine each host-taking command or error names."""
    spans: list[tuple[int, int]] = []
    for m in _TOOL_RE.finditer(work):
        opts = _HOST_TOOLS[m.group(1)]
        end = len(work)
        for stop in ("\n", ";", "|", "&&"):
            j = work.find(stop, m.end())
            if j != -1:
                end = min(end, j)
        skip = False
        for t in re.finditer(r"\S+", work[m.end():end]):
            tok = t.group(0)
            if skip:
                skip = False
                continue
            if tok.startswith("-"):
                # `-i key` takes the next word; `-ikey`, `-o=x` and `--long=x` carry their own.
                skip = len(tok) == 2 and tok[1] in opts
                continue
            if tok.startswith("@") or tok.startswith("["):  # dig @server; an already-masked host
                break
            start = m.end() + t.start()
            off = tok.index("@") + 1 if "@" in tok else 0
            host = re.split(r"[:/,;)\]]", tok[off:], maxsplit=1)[0]
            if _hostlike(host):
                spans.append((start + off, start + off + len(host)))
            break
    for m in _COPY_RE.finditer(work):
        end = work.find("\n", m.end())
        end = len(work) if end == -1 else end
        for t in _COPY_TARGET.finditer(work, m.end(), end):
            if _hostlike(t.group("host")):
                spans.append(t.span("host"))
    for rx in (_DB_HOST, _ERROR_HOST):
        for m in rx.finditer(work):
            if _hostlike(m.group("host")):
                spans.append(m.span("host"))
    out, last = [], 0
    for a, b in sorted(set(spans)):
        if a < last:
            continue
        out.append(work[last:a])
        out.append(bump("HOST"))
        last = b
    out.append(work[last:])
    return "".join(out)


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

#: Everyday words a workflow's free-text inputs are made of. A capitalised word from a title or a
#: directive is a name ("Fernhill onboarding") unless it is one of these ("Fix the Login page").
_COMMON = STOP_WORDS | frozenset("""
fix add update remove delete create make new old bug feature page login logout sign signup
account settings dashboard home about contact report reports search filter sort list table form
button modal dialog menu header footer sidebar navbar layout theme dark light mode check checks
sync migrate migration cluster onboarding rollback hotfix patch refactor cleanup docs readme
please should would could when then this that from into onto over under after before there
their these those what which while where will your have been were they them also only just more
most some such than very each every other another first last next same primary secondary support
customer customers order orders invoice invoices payment payments price prices products monday
tuesday wednesday thursday friday saturday sunday january february march april june july august
september october november december true false none null high medium low done open closed ready
draft pending failed passed success run runs start stop restart retry ship launch write read
""".split())

_HOST_CANDIDATE = re.compile(r"(?<![\w@.\-/])(?:[a-z0-9](?:[a-z0-9_\-]{0,62})?\.)+[a-z][a-z0-9\-]{0,62}"
                             r"(?![\w\-]|\.\w)", re.I)
_PATH_CANDIDATE = re.compile(r"(?<![\w:/.\-])(?:~|\.{1,2})?/?(?:[\w.\-@]+/)+[\w.\-@]+"
                             r"|\b[A-Za-z]:\\[^\s'\"]+")
_FILE_NAME = re.compile(r"\.[A-Za-z][A-Za-z0-9]{0,7}$")

#: The fields that NAME things, in task specs, run state, workflow inputs.
_NAME_KEYS = ("repo", "repository", "owner", "base_branch", "branch", "head_branch", "ref", "app",
              "app_name", "app_slug", "project", "project_name", "service", "host", "hostname")

#: A name this long or longer is masked inside a compound too: `KestrelApiService`, `kestrel-api2`.
_COMPOUND_MIN = 5


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


def _url_host(value: str) -> str:
    m = re.match(r"^[a-z][a-z0-9+.\-]*://(?:[^@/\s]+@)?(\[[^\]]+\]|[^:/\s?#]+)", value.strip(), re.I)
    return m.group(1) if m else ""


def _looks_like_host(value: str) -> bool:
    labels = value.lower().rstrip(".").split(".")
    return len(labels) >= 2 and all(labels) and 0 < public_suffix(labels) < len(labels)


def input_names(blob: Any, depth: int = 0) -> list[tuple[str, str]]:
    """(label, name) from EVERY string a workflow was started with (re-check 4): a customer, a
    server, a bucket, a channel, a domain, a title — not only the keys that conventionally name
    things. A single token is taken when it is identifier-shaped (a separator, a digit or a
    capital: `pg-west-3`, `#fernhill-ops`, `Quillfeather`) or a host; a short phrase whole, and its
    capitalised uncommon words and identifier-shaped tokens; a URL by its host."""
    out: list[tuple[str, str]] = []
    if depth > 4:
        return out
    if isinstance(blob, dict):
        for v in blob.values():
            out.extend(input_names(v, depth + 1))
        return out
    if isinstance(blob, list):
        for v in blob[:50]:
            out.extend(input_names(v, depth + 1))
        return out
    if not isinstance(blob, str):
        return out
    value = blob.strip()
    if not value or len(value) > 400:
        return out

    def token(tok: str) -> None:
        tok = tok.strip(".,;:!?()[]{}\"'`").lstrip("#@")
        if len(tok) < 3:
            return
        host = _url_host(tok)
        if host:
            out.append(("HOST", host))
        elif "/" in tok and len(tok) <= 120:
            out.append(("NAME", tok))
            out.extend(("NAME", part) for part in tok.split("/") if len(part) >= 3)
        elif "." in tok and _looks_like_host(tok):
            out.append(("HOST", tok))
        elif re.search(r"[\-_0-9]", tok) and re.search(r"[A-Za-z]{2}", tok):
            out.append(("NAME", tok))
        elif tok[:1].isupper() and len(tok) >= 4 and tok.lower() not in _COMMON:
            out.append(("NAME", tok))

    words = value.split()
    if len(words) == 1:
        # A plain lowercase word ("high", "python", "verbose") is a setting, not a name: left to
        # the person's review rather than masked everywhere it appears.
        token(value)
        return out
    if len(words) <= 4 and len(value) <= 80 and not all(w.lower() in _COMMON for w in words):
        out.append(("NAME", value))
    for w in words:
        token(w)
    return out


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
        out.extend(input_names(inp))
    for name, address in rows("SELECT name, address FROM deployment_hosts"):
        out.extend([("HOST", str(name)), ("HOST", str(address))])
    for app, repo, ref, domains, public_url in rows(
            "SELECT app_name, repo, target_ref, domains, public_url FROM deployment_targets"):
        out.append(("NAME", str(app)))
        named({"repo": repo})
        if ref and "/" not in str(ref) and "." in str(ref):
            out.append(("HOST", str(ref)))
        for d in domains if isinstance(domains, list) else []:
            if isinstance(d, str) and d.strip():
                out.append(("HOST", _url_host(d) or d.strip()))
        if public_url and _url_host(str(public_url)):
            out.append(("HOST", _url_host(str(public_url))))
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


def _is_host(token: str, known: set[str], before: str = "", after: str = "") -> bool:
    low = token.lower().rstrip(".")
    if low in known or low.split(".", 1)[0] in known:
        return True
    labels = low.split(".")
    if len(labels) < 2 or not all(labels) or after.startswith("("):
        return False
    n = public_suffix(labels)
    if n == 0 or n >= len(labels):
        return False
    if n == 1 and (labels[-1] in _EXT_SUFFIXES or labels[-1] in _CODE_SUFFIXES):
        # A file name or a code member unless it plainly is not: two labels before the suffix,
        # not a code receiver or a Capitalised class, not a file handed to a command.
        first = token.split(".", 1)[0]
        prev = (before.split() or [""])[-1].lower()
        return (len(labels) - n >= 2 and first.lower() not in _CODE_RECEIVERS
                and not first[:1].isupper() and prev not in _FILE_COMMANDS)
    return True


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
    work = _UNC.sub(lambda m: bump("PATH"), work)
    work = _tool_hosts(work, bump)
    # Hosts and paths before names: `db-01.acme.internal` is one host and `/srv/acme/x.py` one
    # path — not a host or a path with a name inside it.
    work = _PATH_CANDIDATE.sub(lambda m: bump("PATH") if _is_path(m.group(0), known_files)
                               else m.group(0), work)
    work = _HOST_CANDIDATE.sub(
        lambda m: bump("HOST") if _is_host(m.group(0), known_hosts,
                                           m.string[max(0, m.start() - 40):m.start()],
                                           m.string[m.end():m.end() + 1])
        else m.group(0), work)
    for label, name in names:
        if label == "FILE" and "/" not in name:
            continue  # a bare known file name is matched below, as a whole token
        # A long name is masked with whatever is glued after it (`KestrelApiService`,
        # `kestrel-api2`, `NightjarSSO`); a short one only as a whole word.
        tail = r"[A-Za-z0-9]*" if len(name) >= _COMPOUND_MIN else r"(?![A-Za-z0-9])"
        pattern = re.compile(rf"(?<![A-Za-z0-9]){re.escape(name)}{tail}", re.I)
        work = pattern.sub(lambda m, lab=("PATH" if label == "FILE" else label): bump(lab), work)
    if known_files:
        work = re.sub(r"(?<![\w./\-])[\w.\-]+(?![\w.\-])",
                      lambda m: bump("FILE") if m.group(0).lower() in known_files else m.group(0),
                      work)
    work = re.sub(r"\x00(\d+)\x00", lambda m: keep[int(m.group(1))], work)
    return work, counts

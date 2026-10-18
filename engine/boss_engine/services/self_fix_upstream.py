"""SEND TO BOSS TEAM: a verified self-fix reaches the BOSS team, previewed, checked, deduplicated.

BOSSAPP-571, PRD R9 R10 R11 R17 R18; spec §7 (and F10–F15, which override the PRD where they say
so). Everything here runs only when a PERSON asks (Preview, Send, Create my fork, Export patch);
nothing leaves the machine by itself, and nothing at all leaves when `BOSS_TELEMETRY=off` or
`DO_NOT_TRACK=1` (§7.1: then only the patch export is offered, and the dedupe search is skipped).

THE PREVIEW IS WHAT IS SENT (R9, the Improve-BOSS rule). Preview builds the whole payload — the
title, the upstream labels, the diff, the body (the PR template, §7.6), the redacted evidence,
the trailers and the machine block — stores its exact bytes (gzip + sha256) in
`self_fix_upstream_sends`, and Send sends those bytes, once, only after the person ticked "I
checked this for private content". Editing the body or the evidence makes new bytes and clears
the tick. The diff is never editable: to change it, request changes on the fix.

TWO PRIVACY RULES FOR TWO THINGS (§7.3, F10):
  * PROSE (the body, the evidence) is scrubbed: share_scrub masks this install's names, hosts,
    addresses, URLs, emails, people, secrets; the allow-list's text gate runs after it.
  * CODE (the diff, the title, the commit message, the test names, the trailers) is CHECKED,
    NEVER MASKED — a masked diff is a broken diff. Any hit blocks the send: the private-
    infrastructure denylist (services/private_infra.py, the P0 test's own list), this install's
    names, an IPv4/IPv6 address outside RFC 5737 / RFC 6598 / loopback / documentation, a
    secret-shaped value (`redaction.redact` would change it), a host this install knows. The
    person sees what hit and where; the fix goes back for neutral fixtures.
  Run, deployment and project ids are install data: kept in the local PR, absent upstream.

DEDUPE BY CLASS SIGNATURE (R11, F13): `deploy:port_held@stack_deploy.plan_stack_deploy` — the
cause without install parameters, and the code it was fixed in. Before the preview BOSS searches
upstream PRs and issues for the machine block carrying it, and merged commits for its
`BOSS-Signature:` trailer:
    merged commit   no PR: the self-fix is "In release"; it drops on the next upgrade
    open PR         Add evidence: a comment on that PR (`+1 verified on <version> with <engine>`)
    closed PR       said, with its link; the person may still send
    open issue      the new PR says `Fixes #n`
    nothing         a new PR

WHERE IT GOES (R18, §6.2, F15):
    github_pr       the person's fork of the upstream repository (their OWN GitHub connection,
                    never an agent's run token): a branch with the fix's files on the release it
                    was made on, and a pull request from it to upstream `main`
    github_comment  the open upstream PR for the same cause
    collector       no GitHub connection, an Improve-BOSS collector configured: the `self_fix`
                    share (counts per signature on the BOSS team's side, R17)
    patch_export    offline, telemetry off, or no other way: a .tar.gz with the patch (mbox, the
                    fix commit as `git am` reads it), the evidence and a README
"Create my fork" forks the upstream repository with the person's own token, as an explicit act.

LABELS (R10, F11). A PR from a fork cannot label the upstream repository, and a label per
signature would make thousands: the upstream labeler workflow (.github/workflows/
boss-agent-labels.yml) applies `source:boss-agent`, `boss:self-fix`, `area:*`, `verified:local`
and `engine:*` from the machine block; a PR without the block is `source:user`. The signature
travels as the machine block and the `BOSS-Signature:` trailer, never as a label. The approver's
name and email never go upstream (F12): `BOSS-Approved: yes`.
"""
from __future__ import annotations

import base64
import gzip
import hashlib
import hmac
import io
import ipaddress
import json
import logging
import os
import re
import tarfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from . import self_fix as sf
from .core import make_id, now_utc

log = logging.getLogger(__name__)

DEFAULT_UPSTREAM = "seanventures/boss"
CHANNELS = ("github_pr", "github_comment", "collector", "patch_export")
UPSTREAM_LABELS = ("source:boss-agent", "boss:self-fix")
MACHINE_TAG = "boss-self-fix v1"
ENGINE_FAMILIES = ("claude", "codex", "openrouter", "ollama", "scripted")
#: The prose the person may edit, and its bounds (the SHARE allow-list's, §7.2).
BODY_MAX = 60_000
EVIDENCE_MAX = 50_000
PATCH_MAX = 256 * 1024
LOG_LINES = 12


class UpstreamRefused(RuntimeError):
    """A move on the upstream path BOSS refuses, with the reason a person reads."""


# ── where upstream is, and whether anything may leave ───────────────────────────────────────

def upstream_repo(settings: Any = None) -> str:
    value = (os.environ.get("BOSS_SELF_FIX_UPSTREAM") or
             str(getattr(settings, "self_fix_upstream", "") or "")).strip()
    return value if re.fullmatch(r"[\w.-]+/[\w.-]+", value or "") else DEFAULT_UPSTREAM


def telemetry_off(settings: Any = None) -> Optional[str]:
    """`BOSS_TELEMETRY=off` / `DO_NOT_TRACK=1`: nothing leaves; the reason, or None."""
    from .improve_boss import env_disabled

    return env_disabled(settings)


# ── the class signature (F13) ────────────────────────────────────────────────────────────────

def location_key(location: Any) -> str:
    """`engine/boss_engine/services/stack_deploy.py::plan_stack_deploy` → `stack_deploy.plan_stack_
    deploy`: the module and the function, never a path of this install."""
    raw = str(location or "")
    path, _, fn = raw.partition("::")
    stem = Path(path).stem if path else ""
    fn = fn.split(".")[-1].strip()
    key = ".".join(p for p in (stem, fn) if p)
    return re.sub(r"[^\w.]", "", key)[:120]


def upstream_class(sfx: Mapping[str, Any]) -> str:
    klass = str(sfx.get("class_signature") or "unknown")
    loc = location_key(sfx.get("location"))
    return f"{klass}@{loc}" if loc else klass


def self_fix_ref(connection: Connection, sfx_id: str) -> str:
    """`BOSS-Self-Fix-Ref`: an HMAC of the self-fix id with this install's secret, 12 hex. The BOSS
    team can tell two sends of one fix apart from two installs; nobody can read the id."""
    from .improve_boss import identity

    secret = str(identity(connection).get("secret") or "")
    return hmac.new(secret.encode(), sfx_id.encode(), hashlib.sha256).hexdigest()[:12]


# ── the privacy check of code (checked, never masked) ────────────────────────────────────────

_IPV4 = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.]*\d)")
_IPV6 = re.compile(r"(?<![\w:])(?:[0-9a-f]{0,4}:){2,7}[0-9a-f]{0,4}(?![\w:])", re.I)
#: BOSS's own record ids: install data, kept locally, never in what goes upstream (§7.3).
_RECORD_ID = re.compile(r"\b(?:wfr|crun|deprun|sdep|dec|chg|sfx|sft|sfu|brec|job|act|deptgt|"
                        r"dh|usr|agt|tsk|mrun|prev|res|p|proj|ws|org)_[A-Za-z0-9]{6,}\b")


def _v6_candidates(line: str) -> list[str]:
    """IPv6-looking tokens that could be an address: two or more hex groups (a slice `[::2]`,
    a time `12:30:45` or a scope `a::b` is not one)."""
    out = []
    for token in _IPV6.findall(line):
        groups = [g for g in token.split(":") if g]
        if len(groups) >= 2 and len(token) >= 6 and ("::" in token or token.count(":") >= 7):
            out.append(token)
    return out
_ALLOWED_NETS = [ipaddress.ip_network(n) for n in (
    "192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24",      # RFC 5737 documentation
    "100.64.0.0/10",                                           # RFC 6598 shared space
    "127.0.0.0/8", "0.0.0.0/32", "255.255.255.255/32",
    "::1/128", "::/128", "2001:db8::/32")]                     # loopback, unspecified, RFC 3849


def _address_allowed(token: str) -> Optional[bool]:
    """True: a documentation/loopback address. False: a real one. None: not an address."""
    try:
        addr = ipaddress.ip_address(token)
    except ValueError:
        return None
    return any(addr in net for net in _ALLOWED_NETS if net.version == addr.version)


def _install_words(names: Iterable[tuple[str, str]]) -> list[tuple[str, str]]:
    """This install's names that a DIFF must not carry. FILE names are the user's app files:
    BOSS's own diff names BOSS's files, and `main.py` in both is not a leak."""
    return [(label, n) for label, n in names if label != "FILE" and len(n) >= 3]


def check_code(parts: Mapping[str, str], *, names: Iterable[tuple[str, str]] = (),
               known_hosts: Iterable[str] = ()) -> list[dict[str, str]]:
    """Every privacy hit in the parts of a send that are CODE or travel verbatim (diff, title,
    trailers, the commit message): [{where, kind, detail}]. Never changes the text. The detail
    names the line and the kind; the person sees their own install's name (it is their data,
    and it has not left)."""
    from .private_infra import findings as denylisted
    from .redaction import redact_secrets

    words = _install_words(names)
    hosts = sorted({str(h).strip() for h in known_hosts
                    if str(h or "").strip() and str(h).strip().lower() not in
                    ("localhost", "127.0.0.1", "::1")}, key=len, reverse=True)
    out: list[dict[str, str]] = []
    for where, value in parts.items():
        body = str(value or "")
        if not body:
            continue
        for category in denylisted(body):
            out.append({"where": where, "kind": "private infrastructure", "detail": category})
        low_lines = body.splitlines()
        for number, line in enumerate(low_lines, 1):
            for token in _IPV4.findall(line) + _v6_candidates(line):
                if _address_allowed(token) is False:
                    out.append({"where": where, "kind": "address",
                                "detail": f"line {number}: an address outside the "
                                          "documentation ranges (use 203.0.113.x)"})
            for label, name in words:
                if re.search(rf"(?<![\w-]){re.escape(name)}(?![\w-])", line, re.I):
                    out.append({"where": where, "kind": f"install name ({label.lower()})",
                                "detail": f"line {number}: {name}"})
        # Credentials (redaction's secret patterns). Addresses are judged above by their range
        # (a documentation address in a fixture is right); people by the denylist and names.
        if redact_secrets(body) != body:
            out.append({"where": where, "kind": "secret-shaped value",
                        "detail": "a credential-shaped value"})
        for number, line in enumerate(low_lines, 1):
            for host in hosts:
                if re.search(rf"(?<![\w.-]){re.escape(host)}(?![\w-]|\.\w)", line, re.I):
                    out.append({"where": where, "kind": "a machine this install knows",
                                "detail": f"line {number}: {host}"})
    seen, unique = set(), []
    for item in out:
        key = (item["where"], item["kind"], item["detail"])
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return unique[:60]


def scrub_prose(value: str, names: list[tuple[str, str]], limit: int) -> tuple[str, int]:
    """(the prose with this install's names, hosts, addresses and secrets masked, how many)."""
    from .improve_allowlist import _scrub_text
    from .self_fix_privacy import mask_hosts
    from .share_scrub import scrub

    masked, counts = scrub(str(value or ""), names)
    hosts = mask_hosts(masked)
    ids = _RECORD_ID.sub("[id]", hosts)
    n = sum(counts.values()) + max(0, (hosts.count("[host]") + hosts.count("[ip]")) -
                                   (masked.count("[host]") + masked.count("[ip]"))) + \
        ids.count("[id]") - hosts.count("[id]")
    return (_scrub_text(ids, limit) or ""), max(0, n)


# ── what is sent ─────────────────────────────────────────────────────────────────────────────

def engine_family(engine: Any) -> str:
    name = str(engine or "").lower()
    return next((f for f in ENGINE_FAMILIES if f in name), "other")


def upstream_labels(sfx: Mapping[str, Any]) -> list[str]:
    """The labels the upstream labeler applies from the machine block (never `signature:*`)."""
    out = [*UPSTREAM_LABELS, f"area:{sfx.get('area') or 'engine'}"]
    if str(sfx.get("status")) in (sf.VERIFIED, sf.SUPERSEDED):
        out.append(sf.VERIFIED_LABEL)
    out.append(f"engine:{engine_family(sfx.get('engine'))}")
    return out


def _public_engine(engine: Any) -> str:
    from .improve_allowlist import public_model

    try:
        return public_model(engine) or engine_family(engine)
    except Exception:  # noqa: BLE001
        return engine_family(engine)


def machine_block(sfx: Mapping[str, Any], ref: str) -> str:
    """`<!-- boss-self-fix v1 {...} -->`: allow-listed fields only (SELF_FIX_UPSTREAM)."""
    from .improve_boss import _version

    gates = {k: bool((v or {}).get("ok")) for k, v in (sfx.get("gates") or {}).items()
             if isinstance(v, Mapping)}
    body = {"class_signature": upstream_class(sfx), "location": location_key(sfx.get("location")),
            "area": sfx.get("area") or "engine", "boss_version": _version(),
            "engine": _public_engine(sfx.get("engine")),
            "engine_family": engine_family(sfx.get("engine")),
            "verified_local": str(sfx.get("status")) == sf.VERIFIED,
            "red_green": {"tests": len((sfx.get("red_green") or {}).get("tests") or [])},
            "gates": gates, "tier": sfx.get("tier"), "upstream_ref": ref}
    return f"<!-- {MACHINE_TAG} {json.dumps(body, sort_keys=True)} -->"


def parse_machine_block(body: str) -> Optional[dict[str, Any]]:
    m = re.search(r"<!--\s*" + re.escape(MACHINE_TAG) + r"\s+(\{.*?\})\s*-->", body or "", re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except ValueError:
        return None


def upstream_trailers(sfx: Mapping[str, Any], ref: str) -> dict[str, str]:
    """§7.2: no run id, no person (F12), the engine's public name only."""
    return {"BOSS-Self-Fix-Ref": ref, "BOSS-Signature": upstream_class(sfx),
            "BOSS-Agent": f"mender ({sf.ACTOR_AGENT})",
            "BOSS-Engine": _public_engine(sfx.get("engine")), "BOSS-Approved": "yes"}


def _upstream_diff(sfx: Mapping[str, Any]) -> str:
    return str((sfx.get("pr") or {}).get("diff") or "")


def _tests_of(sfx: Mapping[str, Any]) -> list[str]:
    return [str(t) for t in (sfx.get("red_green") or {}).get("tests") or []]


def pr_template_body(sfx: Mapping[str, Any], *, machine: str, base_note: str,
                     fixes_issue: Optional[int] = None) -> str:
    """The PR body (.github/PULL_REQUEST_TEMPLATE/boss-self-fix.md, §7.6), before scrubbing."""
    from .improve_boss import _version

    diagnosis = sfx.get("diagnosis") or {}
    gates = sfx.get("gates") or {}
    review = (sfx.get("review") or {}).get("seats") or {}
    rejected = "; ".join(f"{r.get('hypothesis')}: {r.get('why')}" for r in
                         diagnosis.get("rejected") or [] if isinstance(r, Mapping))
    paths = ", ".join(f"{p.get('path')} (tier {p.get('tier')})" for p in sfx.get("paths") or []
                      if isinstance(p, Mapping))
    affected = (gates.get("affected_suites") or {}).get("detail") or {}
    p0 = (gates.get("p0_tier") or {}).get("detail") or {}
    seat_words = " ".join(f"{k.replace('_check', '').replace('_', ' ')} "
                          f"{'✓' if (v or {}).get('ok') else '✗'}" for k, v in review.items()
                          if isinstance(v, Mapping))
    tests = _tests_of(sfx)
    host_effect = "yes" if "host_effect" in (sfx.get("flags") or []) else "no"
    verified = str(sfx.get("status")) == sf.VERIFIED
    lines = [
        "## Summary", str((sfx.get("pr") or {}).get("title") or ""), "", machine, "",
        f"**Found by:** BOSS recovery agent on a real run (signature `{upstream_class(sfx)}`)",
        f"**Diagnosis:** {diagnosis.get('hypothesis') or ''}",
        f"**Rejected:** {rejected}" if rejected else "",
        f"**Fixed by:** Mender (BOSS agent) · engine: {_public_engine(sfx.get('engine'))} · "
        f"BOSS {_version()}",
        f"Fixes #{fixes_issue}" if fixes_issue else "", "",
        "## Evidence",
        "- [x] Regression test added: " + ", ".join(f"`{t}`" for t in tests) +
        " (fails on the base, passes with this change)",
        f"- [x] Affected suites: {affected.get('tests', 0)} test file(s) · P0 tier: "
        f"{p0.get('tests', 0)} · private infrastructure: pass",
        "- [x] No test deleted, skipped or loosened (`tests_not_weakened`)",
        f"- [x] Reviewers: {seat_words}",
        "- [x] Verified on a one-box: the stuck step resumed and passed" if verified else
        "- [ ] Verified on a one-box",
        "", base_note, "",
        "## Safety", f"Paths touched: {paths}", f"Host effect: {host_effect}", "",
        "## Risks and rollback", "Revert this commit.",
    ]
    return "\n".join(x for x in lines if x is not None)


def evidence_text(sfx: Mapping[str, Any]) -> str:
    """The evidence bundle, before scrubbing: the symptom, ≤12 log lines, the diagnosis, the
    rejected hypotheses, the red/green output, the gate results. Data, never instructions."""
    evidence = sfx.get("evidence") or {}
    diagnosis = sfx.get("diagnosis") or {}
    red_green = sfx.get("red_green") or {}
    log_lines = [x for x in str(evidence.get("log_tail") or "").splitlines() if x.strip()]
    gates = sfx.get("gates") or {}
    parts = [
        "### Symptom", str(evidence.get("symptom") or ""),
        "### Log (last lines)", "\n".join(log_lines[-LOG_LINES:]),
        "### Diagnosis", str(diagnosis.get("hypothesis") or ""),
        "### Rejected hypotheses", "\n".join(f"- {r.get('hypothesis')}: {r.get('why')}" for r in
                                             diagnosis.get("rejected") or []
                                             if isinstance(r, Mapping)),
        "### Red (on the base)", str((red_green.get("base") or {}).get("summary") or
                                     (red_green.get("base") or {}).get("tail") or "")[-2000:],
        "### Green (with the fix)", str((red_green.get("head") or {}).get("summary") or
                                        (red_green.get("head") or {}).get("tail") or "")[-2000:],
        "### Gates", "\n".join(f"- {k}: {'pass' if (v or {}).get('ok') else 'fail'}"
                               for k, v in gates.items() if isinstance(v, Mapping)),
    ]
    return "\n".join(p for p in parts if p is not None)


# ── GitHub, as the person (F15) ──────────────────────────────────────────────────────────────

class GitHubUpstream:
    """The person's own GitHub connection, for the upstream path only. Every call is one the
    person asked for (Preview's search, Send, Create my fork). Tests replace `CLIENT`."""

    def __init__(self, token: Optional[str], *, call: Optional[Callable[..., Any]] = None):
        from ..identity import github_app

        self.token = token or ""
        self._call = call or github_app._call

    def _json(self, method: str, path: str, body: Any = None) -> tuple[int, Any]:
        resp = self._call(method, path, token=self.token, body=body)
        try:
            data = resp.json()
        except Exception:  # noqa: BLE001
            data = None
        return int(resp.status), data

    def login(self) -> Optional[str]:
        if not self.token:
            return None
        status, data = self._json("GET", "/user")
        return (data or {}).get("login") if status == 200 else None

    def find_fork(self, upstream: str) -> Optional[str]:
        login = self.login()
        if not login:
            return None
        name = upstream.split("/", 1)[1]
        status, data = self._json("GET", f"/repos/{login}/{name}")
        if status == 200 and (data or {}).get("fork") and \
                ((data or {}).get("parent") or {}).get("full_name", "").lower() == upstream.lower():
            return str(data["full_name"])
        status, data = self._json("GET", f"/repos/{upstream}/forks?per_page=100")
        for repo in data or [] if status == 200 else []:
            if ((repo or {}).get("owner") or {}).get("login", "").lower() == login.lower():
                return str(repo["full_name"])
        return None

    def create_fork(self, upstream: str) -> str:
        status, data = self._json("POST", f"/repos/{upstream}/forks", {})
        if status not in (200, 201, 202) or not (data or {}).get("full_name"):
            raise UpstreamRefused(f"GitHub did not create the fork (HTTP {status})")
        return str(data["full_name"])

    def search(self, kind: str, query: str) -> Optional[list[dict[str, Any]]]:
        """GitHub search (issues/PRs or commits); None when GitHub did not answer."""
        from urllib.parse import quote

        status, data = self._json("GET", f"/search/{kind}?q={quote(query)}&per_page=10")
        if status != 200:
            return None
        return list((data or {}).get("items") or [])

    def comment(self, upstream: str, number: int, body: str) -> str:
        status, data = self._json("POST", f"/repos/{upstream}/issues/{int(number)}/comments",
                                  {"body": body})
        if status not in (200, 201):
            raise UpstreamRefused(f"GitHub did not take the comment (HTTP {status})")
        return str((data or {}).get("html_url") or "")

    def open_pr(self, *, upstream: str, fork: str, branch: str, base_sha: str,
                files: list[tuple[str, Optional[str]]], message: str, title: str,
                body: str) -> dict[str, Any]:
        """A branch in the fork on the release the fix was made on (one commit: the fix's files,
        the message), and a pull request from it to upstream `main`."""
        status, base = self._json("GET", f"/repos/{fork}/git/commits/{base_sha}")
        if status != 200:
            raise UpstreamRefused("your fork does not have the release this fix was made on; "
                                  "sync the fork, then send again")
        tree = []
        for path, content in files:
            if content is None:
                tree.append({"path": path, "mode": "100644", "type": "blob", "sha": None})
                continue
            status, blob = self._json("POST", f"/repos/{fork}/git/blobs", {
                "content": base64.b64encode(content.encode()).decode(), "encoding": "base64"})
            if status not in (200, 201):
                raise UpstreamRefused(f"GitHub did not take a file (HTTP {status})")
            tree.append({"path": path, "mode": "100644", "type": "blob", "sha": blob["sha"]})
        status, made = self._json("POST", f"/repos/{fork}/git/trees",
                                  {"base_tree": (base.get("tree") or {}).get("sha"), "tree": tree})
        if status not in (200, 201):
            raise UpstreamRefused(f"GitHub did not take the tree (HTTP {status})")
        status, commit = self._json("POST", f"/repos/{fork}/git/commits", {
            "message": message, "tree": made["sha"], "parents": [base_sha],
            "author": {"name": "BOSS Mender", "email": "mender@boss.invalid",
                       "date": now_utc().replace(microsecond=0).isoformat()}})
        if status not in (200, 201):
            raise UpstreamRefused(f"GitHub did not take the commit (HTTP {status})")
        status, _ = self._json("POST", f"/repos/{fork}/git/refs",
                               {"ref": f"refs/heads/{branch}", "sha": commit["sha"]})
        if status == 422:
            status, _ = self._json("PATCH", f"/repos/{fork}/git/refs/heads/{branch}",
                                   {"sha": commit["sha"], "force": True})
        if status not in (200, 201):
            raise UpstreamRefused(f"GitHub did not take the branch (HTTP {status})")
        owner = fork.split("/", 1)[0]
        status, pr = self._json("POST", f"/repos/{upstream}/pulls", {
            "title": title, "body": body, "head": f"{owner}:{branch}", "base": "main",
            "maintainer_can_modify": True})
        if status not in (200, 201):
            raise UpstreamRefused(f"GitHub did not open the pull request (HTTP {status})")
        return {"url": str(pr.get("html_url") or ""), "number": pr.get("number"),
                "commit": commit["sha"]}


#: How BOSS reaches GitHub for a person: (connection, actor) -> client. Tests replace it.
def _default_client(connection: Connection, actor: str) -> GitHubUpstream:
    token = None
    try:
        from ..identity import vault

        token = vault.token_for_access_check(actor, provider="github", db=connection)
    except Exception as error:  # noqa: BLE001 — no connection: no GitHub path
        log.info("no GitHub connection for %s: %s", actor, type(error).__name__)
    return GitHubUpstream(token)


CLIENT: Callable[[Connection, str], GitHubUpstream] = _default_client


# ── dedupe (R11) ─────────────────────────────────────────────────────────────────────────────

def dedupe(client: GitHubUpstream, upstream: str, klass: str) -> dict[str, Any]:
    """{state: none|merged|open_pr|closed_pr|open_issue|skipped, match, why, checked_at}."""
    at = now_utc().isoformat()
    commits = client.search("commits", f'repo:{upstream} "BOSS-Signature: {klass}"')
    items = client.search("issues", f'repo:{upstream} "{MACHINE_TAG}" "{klass}"')
    if commits is None and items is None:
        return {"state": "skipped", "match": None, "checked_at": at,
                "why": "GitHub did not answer the search; nothing similar was checked"}
    for c in commits or []:
        return {"state": "merged", "checked_at": at, "why": "a merged commit carries this cause",
                "match": {"url": c.get("html_url"), "number": None,
                          "title": ((c.get("commit") or {}).get("message") or "")[:120]}}
    prs = [i for i in items or [] if i.get("pull_request")]
    issues = [i for i in items or [] if not i.get("pull_request")]

    def match(i: Mapping[str, Any]) -> dict[str, Any]:
        return {"url": i.get("html_url"), "number": i.get("number"),
                "title": str(i.get("title") or "")[:160]}

    for pr in prs:
        if (pr.get("pull_request") or {}).get("merged_at"):
            return {"state": "merged", "match": match(pr), "checked_at": at,
                    "why": "the BOSS team merged a fix for this cause"}
    for pr in prs:
        if pr.get("state") == "open":
            return {"state": "open_pr", "match": match(pr), "checked_at": at,
                    "why": "an open pull request fixes this cause; this adds evidence to it"}
    for pr in prs:
        return {"state": "closed_pr", "match": match(pr), "checked_at": at,
                "why": "a pull request for this cause was closed without merging"}
    for issue in issues:
        if issue.get("state") == "open":
            return {"state": "open_issue", "match": match(issue), "checked_at": at,
                    "why": "an open issue reports this cause; the pull request fixes it"}
    return {"state": "none", "match": None, "checked_at": at, "why": "nothing similar upstream"}


# ── the stored send ──────────────────────────────────────────────────────────────────────────

def _encode(payload: Mapping[str, Any]) -> tuple[bytes, str]:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    return gzip.compress(raw, mtime=0), hashlib.sha256(raw).hexdigest()


def _decode(body_gz: bytes) -> dict[str, Any]:
    return json.loads(gzip.decompress(bytes(body_gz)))


def _row_view(row: Mapping[str, Any]) -> dict[str, Any]:
    payload = _decode(row["body_gz"])
    meta = row["meta"] if isinstance(row["meta"], dict) else json.loads(row["meta"] or "{}")
    result = row["result"] if isinstance(row["result"], dict) else json.loads(row["result"] or
                                                                              "{}")
    return {"send_id": row["id"], "status": row["status"], "channel": row["channel"],
            "channel_why": meta.get("channel_why") or "",
            "target": payload.get("target") or {}, "title": payload.get("title"),
            "labels": payload.get("labels") or [], "diff_stat": meta.get("diff_stat") or {},
            "diff": payload.get("diff") or "", "body": payload.get("body") or "",
            "evidence": payload.get("evidence") or "", "trailers": payload.get("trailers") or {},
            "machine_block": payload.get("machine_block") or "",
            "check": meta.get("check") or {"ok": False, "findings": []},
            "scrubbed": meta.get("scrubbed") or {"body": 0, "evidence": 0},
            "dedupe": meta.get("dedupe") or {"state": "skipped", "match": None, "why": "",
                                             "checked_at": None},
            "sha256": row["body_sha256"], "checked": bool(row["checked_at"]),
            "created_at": (row["created_at"].isoformat() if isinstance(row["created_at"],
                                                                      datetime)
                           else row["created_at"]),
            "result": result or None}


def latest(connection: Connection, sfx_id: str) -> Optional[dict[str, Any]]:
    row = connection.execute(text(
        "SELECT * FROM self_fix_upstream_sends WHERE self_fix_id = :i AND status <> "
        "'superseded' ORDER BY created_at DESC LIMIT 1"), {"i": sfx_id}).mappings().first()
    return _row_view(row) if row else None


def _sendable(sfx: Optional[Mapping[str, Any]]) -> dict[str, Any]:
    if sfx is None:
        raise UpstreamRefused("no such self-fix")
    if str(sfx.get("status")) != sf.VERIFIED:
        raise UpstreamRefused("only a verified self-fix goes to the BOSS team: the stuck step "
                              "must have passed with it installed")
    if not _upstream_diff(sfx):
        raise UpstreamRefused("this self-fix has no diff to send")
    return dict(sfx)


def _parts_to_check(payload: Mapping[str, Any]) -> dict[str, str]:
    return {"diff": str(payload.get("diff") or ""), "title": str(payload.get("title") or ""),
            "trailers": "\n".join(f"{k}: {v}" for k, v in (payload.get("trailers") or {}).items()),
            "body": str(payload.get("body") or ""), "evidence": str(payload.get("evidence") or ""),
            "message": str(payload.get("message") or "")}


def _names(connection: Connection, project_id: str) -> list[tuple[str, str]]:
    from .share_scrub import install_names

    try:
        return install_names(connection, project_id)
    except Exception as error:  # noqa: BLE001 — a partial schema: the patterns still run
        log.info("install names unavailable: %s", error)
        return []


def _check(connection: Connection, project_id: str, payload: Mapping[str, Any]
           ) -> dict[str, Any]:
    from .self_fix_privacy import known_hosts

    findings = check_code(_parts_to_check(payload), names=_names(connection, project_id),
                          known_hosts=known_hosts(connection))
    return {"ok": not findings, "findings": findings}


def _channel(connection: Connection, settings: Any, *, actor: str, dedupe_state: str,
             client: Optional[GitHubUpstream], upstream: str) -> tuple[str, str, Optional[str]]:
    """(channel, why, fork)."""
    from .improve_boss import endpoint

    off = telemetry_off(settings)
    if off:
        return "patch_export", f"{off}: nothing leaves this machine; export the patch", None
    if dedupe_state == "merged":
        return ("patch_export", "The BOSS team already merged a fix for this; it drops from "
                "your local stack on the next upgrade. Nothing needs sending.", None)
    has_github = bool(client and client.token)
    if has_github and dedupe_state == "open_pr":
        return "github_comment", "Adds this install's evidence to the open pull request", None
    if has_github:
        fork = client.find_fork(upstream) if client else None
        if fork:
            return "github_pr", f"Opens a pull request on github.com/{upstream} from {fork}", fork
        return ("github_pr", f"Opens a pull request on github.com/{upstream} from your fork; "
                "you have no fork yet — Create my fork first", None)
    url, _ = endpoint(connection, settings)
    if url:
        return "collector", "Goes to the BOSS team's collector (Improve BOSS)", None
    return ("patch_export", "No GitHub connection and no collector: export the patch and send "
            "it yourself", None)


def preview(connection: Connection, settings: Any, sfx_id: str, *, actor: str,
            client: Optional[GitHubUpstream] = None) -> dict[str, Any]:
    """Build the exact send and store its bytes (§7.1). The only network use is the dedupe
    search with the person's own connection, and none when telemetry is off."""
    sfx = _sendable(sf.get(connection, sfx_id))
    upstream = upstream_repo(settings)
    ref = self_fix_ref(connection, sfx_id)
    klass = upstream_class(sfx)
    off = telemetry_off(settings)
    if client is None and not off:
        client = CLIENT(connection, actor)
    if off:
        found = {"state": "skipped", "match": None, "checked_at": None,
                 "why": f"{off}: BOSS did not search upstream"}
    elif check_code({"query": klass}):
        found = {"state": "skipped", "match": None, "checked_at": None,
                 "why": "the cause's name is not safe to search for"}
    else:
        try:
            found = dedupe(client, upstream, klass) if client is not None else {
                "state": "skipped", "match": None, "checked_at": None, "why": "no GitHub"}
        except Exception as error:  # noqa: BLE001 — offline: said, never fatal
            found = {"state": "skipped", "match": None, "checked_at": None,
                     "why": f"BOSS could not reach GitHub ({type(error).__name__})"}
    channel, why, fork = _channel(connection, settings, actor=actor, dedupe_state=found["state"],
                                  client=client, upstream=upstream)
    names = _names(connection, str(sfx["project_id"]))
    machine = machine_block(sfx, ref)
    issue = (found.get("match") or {}).get("number") if found["state"] == "open_issue" else None
    base_note = (f"Made on the release {str(sfx.get('base_sha'))[:12]} (the commit this BOSS "
                 "ran); rebase onto main if it does not apply.")
    body_raw = pr_template_body(sfx, machine="@@MACHINE@@", base_note=base_note,
                                fixes_issue=issue)
    body, body_n = scrub_prose(body_raw, names, BODY_MAX)
    body = body.replace("@@MACHINE@@", machine)
    if channel == "github_comment":
        from .improve_boss import _version

        body = (f"+1 verified on BOSS {_version()} with {_public_engine(sfx.get('engine'))}: "
                f"the same cause (`{klass}`) was fixed by this install's BOSS agent and the "
                "stuck step passed.\n\n" + machine)
        body_n = 0
    evidence, evidence_n = scrub_prose(evidence_text(sfx), names, EVIDENCE_MAX)
    trailers = upstream_trailers(sfx, ref)
    message = "\n".join([str((sfx.get("pr") or {}).get("title") or ""), "",
                         f"Written by Mender, BOSS's maintainer agent ({klass}).", "",
                         *[f"{k}: {v}" for k, v in trailers.items()]]) + "\n"
    payload = {"v": 1, "self_fix_ref": ref, "channel": channel,
               "target": {"upstream": upstream, "fork": fork,
                          "pr_url": (found.get("match") or {}).get("url")
                          if channel == "github_comment" else None,
                          "pr_number": (found.get("match") or {}).get("number")
                          if channel == "github_comment" else None},
               "title": str((sfx.get("pr") or {}).get("title") or "")[:200],
               "labels": upstream_labels(sfx), "diff": _upstream_diff(sfx), "body": body,
               "evidence": evidence, "trailers": trailers, "machine_block": machine,
               "message": message, "branch": str(sfx.get("branch") or ""),
               "base_sha": str(sfx.get("base_sha") or ""), "tests": _tests_of(sfx)}
    return _store(connection, sfx, payload, channel=channel, actor=actor, meta={
        "channel_why": why, "dedupe": found, "scrubbed": {"body": body_n, "evidence": evidence_n},
        "diff_stat": sfx.get("diff_stat") or {}})


def _store(connection: Connection, sfx: Mapping[str, Any], payload: Mapping[str, Any], *,
           channel: str, actor: str, meta: Mapping[str, Any]) -> dict[str, Any]:
    check = _check(connection, str(sfx["project_id"]), payload)
    body_gz, sha = _encode(payload)
    connection.execute(text(
        "UPDATE self_fix_upstream_sends SET status = 'superseded', updated_at = now() WHERE "
        "self_fix_id = :i AND status IN ('previewed', 'blocked')"), {"i": sfx["id"]})
    send_id = make_id("sfu")
    status = "previewed" if check["ok"] else "blocked"
    connection.execute(text(
        "INSERT INTO self_fix_upstream_sends (id, self_fix_id, channel, body_gz, body_sha256, "
        "meta, status, created_by) VALUES (:i, :f, :c, :b, :h, CAST(:m AS JSONB), :s, :a)"),
        {"i": send_id, "f": sfx["id"], "c": channel, "b": body_gz, "h": sha,
         "m": json.dumps({**dict(meta), "check": check}, default=str), "s": status, "a": actor})
    if str(sfx.get("upstream_state") or "local_only") in ("local_only", "previewed"):
        connection.execute(text("UPDATE self_fixes SET upstream_state = 'previewed', updated_at "
                                "= now() WHERE id = :i"), {"i": sfx["id"]})
    if (meta.get("dedupe") or {}).get("state") == "merged":
        connection.execute(text(
            "UPDATE self_fixes SET upstream_state = 'merged', upstream_url = :u, updated_at = "
            "now() WHERE id = :i"),
            {"i": sfx["id"], "u": ((meta.get("dedupe") or {}).get("match") or {}).get("url")})
    sf.emit(connection, sf.get(connection, str(sfx["id"])) or sfx, "upstream_previewed",
            actor=actor, sentence=(
                f"{actor} previewed what goes to the BOSS team ({channel.replace('_', ' ')})" +
                ("" if check["ok"] else f"; blocked: {len(check['findings'])} private item(s) "
                                        "in it")),
            data={"send_id": send_id, "channel": channel, "sha256": sha,
                  "dedupe": (meta.get("dedupe") or {}).get("state")})
    return latest(connection, str(sfx["id"])) or {}


def edit(connection: Connection, settings: Any, sfx_id: str, send_id: str, *, actor: str,
         body: Optional[str] = None, evidence: Optional[str] = None) -> dict[str, Any]:
    """The person edits the body or the evidence: new bytes, checked again, the tick cleared."""
    row = connection.execute(text(
        "SELECT * FROM self_fix_upstream_sends WHERE id = :s AND self_fix_id = :i"),
        {"s": send_id, "i": sfx_id}).mappings().first()
    if row is None or row["status"] not in ("previewed", "blocked"):
        raise UpstreamRefused("this preview is no longer the one to edit; preview again")
    sfx = _sendable(sf.get(connection, sfx_id))
    payload = _decode(row["body_gz"])
    meta = dict(row["meta"] if isinstance(row["meta"], dict) else json.loads(row["meta"]))
    if body is not None:
        machine = str(payload.get("machine_block") or "")
        text_body = str(body)[:BODY_MAX]
        if machine and machine not in text_body:
            text_body = text_body.rstrip() + "\n\n" + machine  # the labeler reads it
        payload["body"] = text_body
    if evidence is not None:
        payload["evidence"] = str(evidence)[:EVIDENCE_MAX]
    meta.pop("check", None)
    return _store(connection, sfx, payload, channel=str(row["channel"]), actor=actor, meta=meta)


def _fix_files(settings: Any, sfx: Mapping[str, Any], bundle: Optional[bytes]
               ) -> list[tuple[str, Optional[str]]]:
    """The fix's files after the change (None: deleted), from the mirror + the fix's bundle."""
    import tempfile

    from . import self_fix_source as src

    if not bundle:
        raise UpstreamRefused("this self-fix has no commit bundle to send")
    with tempfile.TemporaryDirectory(prefix="boss-upstream-") as tmp:
        work = Path(tmp) / "work"
        src.workspace(settings, str(sfx["base_sha"]), work)
        packed = Path(tmp) / "fix.bundle"
        packed.write_bytes(bytes(bundle))
        branch = str(sfx.get("branch") or "")
        src.git(["fetch", "-q", str(packed), f"+refs/heads/{branch}:refs/heads/{branch}"],
                cwd=work)
        src.git(["checkout", "-q", str(sfx["fix_commit_sha"])], cwd=work)
        out = []
        for change in src.changes(work, str(sfx["base_sha"])):
            out.append((change.path, None if change.status == "D" else change.after))
        return out


def _row(connection: Connection, sfx_id: str, send_id: str) -> Mapping[str, Any]:
    row = connection.execute(text(
        "SELECT * FROM self_fix_upstream_sends WHERE id = :s AND self_fix_id = :i FOR UPDATE"),
        {"s": send_id, "i": sfx_id}).mappings().first()
    if row is None:
        raise UpstreamRefused("no such preview")
    return row


def send(connection: Connection, settings: Any, sfx_id: str, send_id: str, *, actor: str,
         checked: bool, client: Optional[GitHubUpstream] = None,
         post: Optional[Callable[..., Any]] = None) -> dict[str, Any]:
    """Send the STORED bytes of one preview, once (§7.1). Refused: not ticked, a blocked
    preview, telemetry off, a patch-export channel, a preview already sent or superseded."""
    if not checked:
        raise UpstreamRefused("tick “I checked this for private content” first")
    off = telemetry_off(settings)
    if off:
        raise UpstreamRefused(f"{off}: nothing leaves this machine; export the patch instead")
    row = _row(connection, sfx_id, send_id)
    if row["status"] == "blocked":
        raise UpstreamRefused("BOSS found private content in what would be sent; it cannot go")
    if row["status"] != "previewed":
        raise UpstreamRefused(f"this preview is {row['status']}; preview again")
    raw = gzip.decompress(bytes(row["body_gz"]))
    if hashlib.sha256(raw).hexdigest() != row["body_sha256"]:
        raise UpstreamRefused("the stored preview does not match its hash; preview again")
    payload = json.loads(raw)
    sfx = _sendable(sf.get(connection, sfx_id))
    channel = str(row["channel"])
    connection.execute(text(
        "UPDATE self_fix_upstream_sends SET checked_by = :a, checked_at = now(), updated_at = "
        "now() WHERE id = :s"), {"a": actor, "s": send_id})
    try:
        if channel == "github_pr":
            client = client or CLIENT(connection, actor)
            fork = (payload.get("target") or {}).get("fork")
            if not fork:
                raise UpstreamRefused("create your fork first, then preview again")
            files = _fix_files(settings, sfx, sf.bundle_of(connection, sfx_id))
            result = client.open_pr(upstream=payload["target"]["upstream"], fork=fork,
                                    branch=payload["branch"], base_sha=payload["base_sha"],
                                    files=files, message=payload["message"],
                                    title=payload["title"], body=payload["body"] + (
                                        "\n\n<details><summary>Redacted evidence</summary>\n\n" +
                                        payload["evidence"] + "\n\n</details>"))
        elif channel == "github_comment":
            client = client or CLIENT(connection, actor)
            url = client.comment(payload["target"]["upstream"],
                                 int(payload["target"]["pr_number"]),
                                 payload["body"] + "\n\n<details><summary>Redacted evidence"
                                 "</summary>\n\n" + payload["evidence"] + "\n\n</details>")
            result = {"url": url or payload["target"].get("pr_url")}
        elif channel == "collector":
            result = _send_collector(connection, settings, sfx, payload, post=post)
        else:
            raise UpstreamRefused("this fix is exported as a patch, not sent")
    except UpstreamRefused as error:
        _finish(connection, send_id, "failed", {"error": str(error)})
        raise
    except Exception as error:  # noqa: BLE001 — GitHub or the network: said, recorded
        _finish(connection, send_id, "failed", {"error": f"{type(error).__name__}"})
        raise UpstreamRefused(f"the send failed ({type(error).__name__}); nothing was "
                              "changed upstream") from error
    _finish(connection, send_id, "sent", result)
    state = "sent"
    connection.execute(text(
        "UPDATE self_fixes SET upstream_state = :s, upstream_url = :u, updated_at = now() "
        "WHERE id = :i"), {"s": state, "u": result.get("url"), "i": sfx_id})
    fresh = sf.get(connection, sfx_id) or {}
    sf.emit(connection, fresh, "upstream_sent", actor=actor,
            sentence=f"{actor} sent the fix to the BOSS team ({channel.replace('_', ' ')})"
                     + (f": {result.get('url')}" if result.get("url") else "."),
            data={"send_id": send_id, "channel": channel, "url": result.get("url"),
                  "sha256": row["body_sha256"]})
    if channel == "github_comment":
        sf.emit(connection, fresh, "upstream_linked", actor=actor,
                sentence=f"Linked to the open pull request {result.get('url')} instead of "
                         "opening a new one.", data={"url": result.get("url")})
    sf.audit(connection, "self_fix.upstream_sent", actor=actor, sfx=fresh,
             details={"channel": channel, "sha256": row["body_sha256"]})
    return latest(connection, sfx_id) or {}


def _finish(connection: Connection, send_id: str, status: str, result: Mapping[str, Any]) -> None:
    connection.execute(text(
        "UPDATE self_fix_upstream_sends SET status = :s, result = CAST(:r AS JSONB), "
        "updated_at = now() WHERE id = :i"),
        {"s": status, "r": json.dumps(dict(result), default=str), "i": send_id})


def collector_payload(connection: Connection, settings: Any, sfx: Mapping[str, Any],
                      payload: Mapping[str, Any]) -> dict[str, Any]:
    """The `self_fix` share (improve_allowlist.SELF_FIX): the allow-listed facts and the patch."""
    from . import improve_allowlist as allow
    from .improve_boss import _edition, _version, identity, secret_hash

    ident = identity(connection)
    block = parse_machine_block(str(payload.get("machine_block") or "")) or {}
    clean, _ = allow.sanitize({
        "v": 1, "kind": "self_fix", "install_id": ident["id"],
        "secret_hash": secret_hash(ident["secret"]),
        "class_signature": block.get("class_signature"), "area": block.get("area"),
        "engine": block.get("engine_family"), "verified_local": block.get("verified_local"),
        "tests": (block.get("red_green") or {}).get("tests"), "tier": block.get("tier"),
        "upstream_ref": payload.get("self_fix_ref"), "title": payload.get("title"),
        "patch": (str(payload.get("diff") or ""))[:PATCH_MAX],
        "evidence": str(payload.get("evidence") or "")[:EVIDENCE_MAX],
        "context": {"version": _version(), "edition": _edition(settings)}}, allow.SELF_FIX)
    return clean


def _send_collector(connection: Connection, settings: Any, sfx: Mapping[str, Any],
                    payload: Mapping[str, Any], *, post: Optional[Callable[..., Any]] = None
                    ) -> dict[str, Any]:
    from .improve_boss import collector_url, encode, endpoint, transmit

    url = collector_url(endpoint(connection, settings)[0], "share")
    body = collector_payload(connection, settings, sfx, payload)
    if not body.get("patch") or not body.get("class_signature"):
        raise UpstreamRefused("the collector share lost its patch to the allow-list")
    _raw, gz = encode(body)
    status, error = transmit(settings, url, gz, post)
    if status is None or not 200 <= int(status) < 300:
        raise UpstreamRefused(f"the collector did not take it ({error or status})")
    return {"url": None, "collector": True, "status": status}


def create_fork(connection: Connection, settings: Any, sfx_id: str, *, actor: str,
                client: Optional[GitHubUpstream] = None) -> dict[str, Any]:
    """Create my fork: the person's own token, their explicit act (F15)."""
    off = telemetry_off(settings)
    if off:
        raise UpstreamRefused(f"{off}: nothing leaves this machine")
    _sendable(sf.get(connection, sfx_id))
    client = client or CLIENT(connection, actor)
    if not client.token:
        raise UpstreamRefused("connect your GitHub account first (My account › Agents)")
    upstream = upstream_repo(settings)
    fork = client.find_fork(upstream) or client.create_fork(upstream)
    sf.emit(connection, sf.get(connection, sfx_id) or {"id": sfx_id}, "upstream_fork",
            actor=actor, sentence=f"{actor} has a fork of {upstream} for self-fixes: {fork}.",
            data={"fork": fork})
    return {"fork": fork}


# ── patch export (offline, R18) ──────────────────────────────────────────────────────────────

def patch_text(sfx: Mapping[str, Any], *, message: Optional[str] = None) -> str:
    """The fix as `git am` reads it (mbox): the commit message, then the diff."""
    title = str((sfx.get("pr") or {}).get("title") or "fix")
    commit = str(sfx.get("fix_commit_sha") or "0" * 40)
    when = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")
    body = message or "\n".join(f"{k}: {v}" for k, v in
                                ((sfx.get("pr") or {}).get("trailers") or {}).items())
    return (f"From {commit} Mon Sep 17 00:00:00 2001\nFrom: BOSS Mender <mender@boss.invalid>\n"
            f"Date: {when}\nSubject: [PATCH] {title}\n\n{body.strip()}\n---\n"
            f"{_upstream_diff(sfx).rstrip()}\n-- \nboss self-fix\n")


def export(connection: Connection, settings: Any, sfx_id: str, *, actor: str) -> tuple[bytes,
                                                                                      str]:
    """(a .tar.gz, its file name): the patch, the redacted evidence, a README. For the person's
    own disk — it never leaves by itself. The README says what the privacy check found."""
    sfx = sf.get(connection, sfx_id)
    if sfx is None or not _upstream_diff(sfx):
        raise UpstreamRefused("this self-fix has no diff to export")
    names = _names(connection, str(sfx["project_id"]))
    ref = self_fix_ref(connection, sfx_id)
    trailers = upstream_trailers(sfx, ref)
    message = "\n".join([f"Written by Mender, BOSS's maintainer agent ({upstream_class(sfx)}).",
                         "", *[f"{k}: {v}" for k, v in trailers.items()]])
    patch = patch_text(sfx, message=message)
    evidence, _ = scrub_prose(evidence_text(sfx), names, EVIDENCE_MAX)
    check = _check(connection, str(sfx["project_id"]), {"diff": _upstream_diff(sfx),
                                                        "title": (sfx.get("pr") or {}).get(
                                                            "title"), "trailers": trailers})
    readme = "\n".join([
        f"BOSS self-fix {upstream_class(sfx)}", "",
        "Apply on a BOSS checkout of the same release:  git am < fix.patch",
        f"Send it to the BOSS team as a pull request on github.com/{upstream_repo(settings)}, "
        "with evidence.md in its description.", "",
        "Privacy check of the diff: " + ("nothing private found." if check["ok"] else
                                         "FOUND private content — neutralise it before sending:"),
        *[f"  - {f['where']}: {f['kind']} — {f['detail']}" for f in check["findings"]], ""])
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, content in (("fix.patch", patch), ("evidence.md", evidence),
                              ("README.txt", readme)):
            data = content.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mtime = int(now_utc().timestamp())
            tar.addfile(info, io.BytesIO(data))
    # A read: the export writes nothing (it is a GET, and the bytes stay on the person's disk).
    return buf.getvalue(), f"boss-selffix-{location_key(sfx.get('location')) or sfx_id}.tar.gz"


__all__ = ["CHANNELS", "CLIENT", "DEFAULT_UPSTREAM", "GitHubUpstream", "UpstreamRefused",
           "check_code", "collector_payload", "create_fork", "dedupe", "edit", "export",
           "latest", "location_key", "machine_block", "parse_machine_block", "patch_text",
           "preview", "scrub_prose", "self_fix_ref", "send", "telemetry_off", "upstream_class",
           "upstream_labels", "upstream_repo", "upstream_trailers"]

"""RULESET CHANGES (SPEC §4): the console reads the base branch's rulesets and proposes an edit
as a small local change — "Ruleset northgate v4" — that takes effect only when it merges. The
repo file stays the one source of truth. A change that LOWERS a P0 (its P-level, disables it, or
turns an auto-fix on for a `never` rule, or drops it) is raised at the two-person tier: a second
approver merges it.
"""
from __future__ import annotations

import hashlib
import logging
import re
import secrets
from typing import Any, Mapping, Optional, Sequence

from . import rulesets as _rulesets
from . import validators as _validators

log = logging.getLogger(__name__)

TWO_PERSON_TIER = 3
#: A new ruleset's id becomes its file name: lower-case words, digits, `-` and `_`.
_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


class RulesetChangeError(Exception):
    def __init__(self, status: int, code: str, message: str, **extra: Any):
        super().__init__(message)
        self.status, self.code, self.message, self.extra = status, code, message, extra

    def body(self) -> dict:
        return {"error": self.code, "message": self.message, **self.extra}


def _repo(c: Any, settings: Any, project_id: str) -> tuple[Any, str]:
    """The project's local repository adapter and its default branch."""
    from .local_projects import local_binding
    from .local_repo_host import LocalGitAdapter

    b = local_binding(c, project_id)
    if b is None:
        raise RulesetChangeError(422, "not_local", "Ruleset changes are kept in the project's "
                                 "local repository; this project has none yet.")
    adapter = LocalGitAdapter(settings, owner=str(b["owner"]), repo=str(b["name"]),
                              project_id=project_id)
    return adapter, str(b.get("default_branch") or adapter.default_branch())


def _view(rs: Mapping[str, Any], path: str) -> dict:
    return {"id": rs["ruleset"], "ruleset": rs["ruleset"], "version": rs.get("version"),
            "sha": rs.get("sha"), "path": path, "validator": rs.get("validator"),
            "rules": rs.get("rules") or {}, "custom": rs.get("custom") or []}


def list_for_project(c: Any, settings: Any, project_id: str) -> dict:
    """The base branch's rulesets: ``{rulesets: [{id, version, sha, path, validator, rules,
    custom}], branch}``. A file that does not parse is listed with its error, never guessed."""
    adapter, branch = _repo(c, settings, project_id)
    out: list[dict] = []
    for path in adapter.list_paths(branch):
        if not (path.startswith(_rulesets.DIR + "/") and path.endswith((".yaml", ".yml"))) \
                or "/" in path[len(_rulesets.DIR) + 1:]:
            continue
        body = adapter.read_text_file(branch, path)
        try:
            out.append(_view(_rulesets.parse(body or "", path=path), path))
        except _rulesets.RulesetError as exc:
            out.append({"id": path.rsplit("/", 1)[-1].rsplit(".", 1)[0], "path": path,
                        "error": str(exc)})
    return {"rulesets": out, "branch": branch}


def p0_downgrades(base: Mapping[str, Any], rules: Mapping[str, Mapping[str, Any]]) -> list[str]:
    """The P0 rules this edit weakens (§4: needs a second approver)."""
    out = []
    for rid, was in (base.get("rules") or {}).items():
        if was.get("severity") != "P0":
            continue
        now = rules.get(rid)
        if now is None or now.get("severity") != "P0" or now.get("enabled") is False or (
                (was.get("autofix") in (None, "never")) and now.get("autofix") not in (None, "never")):
            out.append(rid)
    return out


def render(base: Mapping[str, Any], version: int, rules: Sequence[Mapping[str, Any]]) -> tuple[str, dict]:
    """The new file and its parsed form. ``rules`` as the console sends them:
    ``[{id, enabled, severity, autofix, guide, custom?, check?}]``."""
    import yaml

    plain: dict[str, dict] = {}
    custom: list[dict] = []
    for r in rules:
        rid = str(r.get("id") or "").strip()
        if not rid:
            continue
        if r.get("custom"):
            custom.append({k: v for k, v in (("id", rid), ("severity", r.get("severity")),
                                             ("check", "agent"), ("guide", r.get("guide")))
                           if v not in (None, "")})
            continue
        entry: dict[str, Any] = {"severity": r.get("severity"), "autofix": r.get("autofix")}
        if r.get("enabled") is False:
            entry["enabled"] = False
        if r.get("guide"):
            entry["guide"] = str(r["guide"])
        plain[rid] = {k: v for k, v in entry.items() if v is not None}
    doc: dict[str, Any] = {"ruleset": base["ruleset"], "version": version}
    if base.get("validator"):
        doc["validator"] = base["validator"]
    doc["rules"] = plain
    if custom:
        doc["custom"] = custom
    body = yaml.safe_dump(doc, sort_keys=False, allow_unicode=True, width=100)
    return body, _rulesets.parse(body, path="proposal")


# ── the smallest edit of the file a person changed ─────────────────────────────────────────────

_ENTRY = re.compile(r"^(?P<indent>\s+)(?P<key>[^\s#:][^:#]*?)\s*:(?P<rest>.*)$")
_TOP = re.compile(r"^(?P<key>[A-Za-z_][\w-]*)\s*:(?P<rest>.*)$")


def _scalar(value: Any) -> str:
    """A YAML scalar for a flow or block entry: a plain word when it is one, else JSON's double
    quotes (valid YAML)."""
    import json

    if isinstance(value, bool):
        return "true" if value else "false"
    text_ = str(value)
    if re.fullmatch(r"[A-Za-z0-9_.-]+", text_) and text_.lower() not in (
            "true", "false", "yes", "no", "null", "on", "off", "~"):
        return text_
    return json.dumps(text_, ensure_ascii=False)


def _fields(rule: Mapping[str, Any]) -> list[tuple[str, Any]]:
    out: list[tuple[str, Any]] = []
    for k in ("severity", "autofix"):
        if rule.get(k) is not None:
            out.append((k, rule[k]))
    if rule.get("enabled") is False:
        out.append(("enabled", False))
    if rule.get("guide"):
        out.append(("guide", rule["guide"]))
    return out


def _entry_lines(rid: str, rule: Mapping[str, Any], *, indent: str, flow: bool, pad: str,
                 child: str) -> list[str]:
    pairs = _fields(rule)
    if flow:
        return [f"{indent}{rid}:{pad}{{" + ", ".join(f"{k}: {_scalar(v)}" for k, v in pairs) + "}"]
    return [f"{indent}{rid}:"] + [f"{child}{k}: {_scalar(v)}" for k, v in pairs]


_VALUE = r'"(?:[^"\\\n]|\\.)*"|\'(?:[^\'\n]|\'\')*\'|[^\s,{}#"\'][^,{}#\n]*?(?=\s*(?:[,}]|#|$))'


def _in_place(span: Sequence[str], was: Mapping[str, Any], now: Mapping[str, Any]
              ) -> Optional[list[str]]:
    """One rule's lines with only the changed field VALUES replaced (each key written once in
    the span), or None when a field was added or dropped (the caller rewrites the entry)."""
    text_ = "\n".join(span)
    for key in set(was) | set(now):
        if was.get(key) == now.get(key):
            continue
        if key not in was or key not in now:
            return None
        found = list(re.finditer(rf"(?<![\w-]){re.escape(key)}\s*:\s*(?P<v>{_VALUE})", text_,
                                 re.MULTILINE))
        if len(found) != 1:
            return None
        v = found[0].span("v")
        text_ = text_[:v[0]] + _scalar(now[key]) + text_[v[1]:]
    return text_.split("\n")


def minimal_edit(text_: str, base: Mapping[str, Any], new: Mapping[str, Any],
                 version: int) -> Optional[str]:
    """The ruleset file with ONLY what changed rewritten: the `version:` value, each edited
    rule's own entry (in the style it was written: a `{…}` flow map stays one, a block stays a
    block, its column kept), a dropped rule's entry removed, a new rule appended. Every other
    line — comments, alignment, quoting — is the base's, byte for byte. A console edit of one
    guide rewrote the whole file (+35 −14, dry run 2, gap 12). None when the file's shape is not
    one this can edit safely (the caller renders it whole); the result must parse to ``new``."""
    lines = text_.splitlines()
    nl = text_.endswith("\n")
    if (base.get("custom") or []) != (new.get("custom") or []):
        return None
    ver = next((i for i, x in enumerate(lines) if _TOP.match(x)
                and _TOP.match(x).group("key") == "version"), None)
    rules_at = next((i for i, x in enumerate(lines) if _TOP.match(x)
                     and _TOP.match(x).group("key") == "rules"), None)
    if ver is None or rules_at is None:
        return None
    m = _TOP.match(lines[ver])
    tail = re.match(r"\s*\S+(?P<c>\s+#.*)?$", m.group("rest"))
    lines[ver] = f"version: {version}" + ((tail.group("c") or "") if tail else "")
    # The rules map's entries: [start, end) spans, trailing blank/comment lines left outside.
    end_of_rules = next((i for i in range(rules_at + 1, len(lines))
                         if lines[i].strip() and not lines[i].startswith((" ", "\t", "#"))),
                        len(lines))
    entries: dict[str, tuple[int, int, Any]] = {}
    indent_of = None
    i = rules_at + 1
    while i < end_of_rules:
        em = _ENTRY.match(lines[i])
        if not em or lines[i].lstrip().startswith("#"):
            i += 1
            continue
        if indent_of is None:
            indent_of = em.group("indent")
        if em.group("indent") != indent_of:
            i += 1
            continue
        j = i + 1
        while j < end_of_rules and (not lines[j].strip() or
                                    len(lines[j]) - len(lines[j].lstrip()) > len(indent_of)):
            j += 1
        k = j
        while k > i + 1 and (not lines[k - 1].strip() or lines[k - 1].lstrip().startswith("#")):
            k -= 1
        entries[em.group("key").strip().strip("'\"")] = (i, k, em)
        i = j
    if indent_of is None:
        return None
    base_rules, new_rules = base.get("rules") or {}, new.get("rules") or {}
    flow_style = any(e[2].group("rest").strip().startswith("{") for e in entries.values())
    edits: list[tuple[int, int, list[str]]] = []
    for rid, (i, k, em) in entries.items():
        if rid not in new_rules:
            edits.append((i, k, []))
            continue
        if new_rules[rid] == base_rules.get(rid):
            continue
        # The edited VALUES only, in place: a `{…}` entry wrapped over lines keeps its wrap.
        inplace = _in_place(lines[i:k], base_rules.get(rid) or {}, new_rules[rid])
        if inplace is not None:
            edits.append((i, k, inplace))
            continue
        rest = em.group("rest")
        flow = rest.strip().startswith("{")
        pad = rest[:len(rest) - len(rest.lstrip())] or " "
        child = (lines[i + 1][:len(lines[i + 1]) - len(lines[i + 1].lstrip())]
                 if not flow and k > i + 1 else indent_of + "  ")
        edits.append((i, k, _entry_lines(rid, new_rules[rid], indent=indent_of, flow=flow,
                                         pad=pad, child=child)))
    added = [rid for rid in new_rules if rid not in entries]
    if added:
        last = max((e[1] for e in entries.values()), default=rules_at + 1)
        extra = [x for rid in added for x in _entry_lines(
            rid, new_rules[rid], indent=indent_of, flow=flow_style, pad=" ",
            child=indent_of + "  ")]
        edits.append((last, last, extra))
    for i, k, repl in sorted(edits, key=lambda e: e[0], reverse=True):
        lines[i:k] = repl
    out = "\n".join(lines) + ("\n" if nl else "")
    try:
        got = _rulesets.parse(out, path="proposal")
    except _rulesets.RulesetError:
        return None
    if got["rules"] != new_rules or got.get("version") != version \
            or got.get("custom") != (new.get("custom") or []):
        return None
    return out


def _fresh_branch(adapter: Any, work: str, base_branch: str) -> None:
    """The proposal's branch, cut from the base's head. A branch left by a closed (rejected)
    proposal is reset first, so the new change never carries the rejected commit."""
    from . import local_repo

    if adapter.branch_exists(work):
        git_dir = adapter._require()
        head = local_repo.branch_sha(git_dir, base_branch)
        if head:
            local_repo.git(["update-ref", f"refs/heads/{work}", head], git_dir=git_dir)
            return
    adapter.ensure_branch(work, base_branch)


def propose(c: Any, settings: Any, project_id: str, ruleset_id: str, *,
            rules: Sequence[Mapping[str, Any]], base_version: Any, base_sha: Optional[str],
            actor: str, validator: Optional[str] = None) -> dict:
    """Open "Ruleset <id> v<n+1>" as a local change editing `.boss/rulesets/<id>.yaml`, with its
    merge decision. 409 when the base moved since the person read it. When the project has no
    such ruleset yet, the change CREATES it (v1, ``created: true``): the person sends no base,
    and ``validator`` names the validator it layers on."""
    from .core import create_blocking_decision, emit_event
    from .local_repo_host import change_set_url

    # The id becomes a path: refuse a traversal before anything is read with it.
    if not ruleset_id or "/" in ruleset_id or ".." in ruleset_id:
        raise RulesetChangeError(404, "no_ruleset", f"There is no ruleset {ruleset_id}.")
    adapter, branch = _repo(c, settings, project_id)
    path = next((p for p in (f"{_rulesets.DIR}/{ruleset_id}.yaml", f"{_rulesets.DIR}/{ruleset_id}.yml")
                 if adapter.read_text_file(branch, p) is not None), None)
    created = path is None
    if created:
        if not _ID.match(ruleset_id):
            raise RulesetChangeError(422, "bad_id", "A ruleset id is lower-case letters, digits, "
                                     "`-` and `_` (it names the file).")
        if base_sha or base_version:
            raise RulesetChangeError(409, "stale_base", f"The ruleset {ruleset_id} is no longer "
                                     "on the base branch.")
        path = f"{_rulesets.DIR}/{ruleset_id}.yaml"
        base = {"ruleset": ruleset_id, "version": 0, "rules": {}, "custom": [], "sha": None,
                **({"validator": str(validator)} if validator else {})}
    else:
        base_text = adapter.read_text_file(branch, path) or ""
        base = _rulesets.parse(base_text, path=path)
    if not created and (base_sha != base.get("sha")
                        or (base_version is not None and base_version != base.get("version"))):
        raise RulesetChangeError(409, "stale_base", "The ruleset changed since you opened it.",
                                 ruleset=_view(base, path))
    version = int(base.get("version") or 0) + 1
    # EACH PROPOSAL ON ITS OWN BRANCH (dry run 4: #20 and #22 shared boss/ruleset/northgate-v2,
    # so closing one closed the other): the version plus a short id.
    stem = f"boss/ruleset/{ruleset_id}-v{version}"
    work = f"{stem}-{secrets.token_hex(3)}"
    # ONE OPEN PROPOSAL PER VERSION (dry run 3, gap 9: #20 "v2" opened beside #15 "v2"): an
    # open change for this version is named, with its link, never duplicated.
    already = adapter.open_pull_request_for(stem)
    _prefixed = getattr(adapter, "open_pull_request_with_prefix", None)
    if already is None and callable(_prefixed):
        # Only this version's own ids: ruleset "northgate-v2" v1 is never "northgate" v2.
        already = _prefixed(f"{stem}-", r"[0-9a-f]{6}")
    if already is not None:
        raise RulesetChangeError(
            409, "already_open", f"v{version} is already open as change #{already['number']}.",
            change=already["number"], url=already["url"], version=version)
    try:
        body, new = render(base, version, rules)
    except _rulesets.RulesetError as exc:
        raise RulesetChangeError(422, "bad_rules", str(exc)) from None
    if not created:
        # ONLY THE EDITED FIELDS CHANGE (minimal_edit): comments and layout are the base's.
        small = minimal_edit(base_text, base, new, version)
        if small is not None:
            body, new = small, _rulesets.parse(small, path="proposal")
    weaker = p0_downgrades(base, new["rules"])
    title = f"Ruleset {ruleset_id} v{version}"
    _fresh_branch(adapter, work, branch)
    head = adapter.commit_files(work, {path: body}, f"{title}\n\nOn-behalf-of: {actor}")
    pr = adapter.create_pull_request(
        work, title, (f"Adds `{path}` (v{version})." if created else
                      f"Edits `{path}` (v{base.get('version')} → v{version}).")
        + (f" Lowers P0 rule(s): {', '.join(weaker)} — needs a second approver." if weaker else ""),
        base=branch)
    url = change_set_url(adapter.repository_full_name, pr.number)
    decision = create_blocking_decision(
        c, project_id, None, f"Merge {title} (change #{pr.number})?",
        {"action": "review_pull_request", "pull_request_url": url, "branch_name": work,
         "head_sha": head, "title": title, "requires_person_approval": True,
         "no_auto_merge": True,
         "ruleset_change": {"id": ruleset_id, "path": path, "from_version": base.get("version"),
                            "to_version": version, "from_sha": base.get("sha"),
                            "to_sha": new.get("sha"), "p0_downgrades": weaker,
                            "second_approver": bool(weaker), "created": created}},
        {}, "ruleset_change", risk_tier=TWO_PERSON_TIER if weaker else 2, default_action="hold")
    emit_event(c, project_id, "ruleset.change_proposed", actor, "decision", decision,
               {"ruleset": ruleset_id, "version": version, "change": pr.number,
                "p0_downgrades": weaker, "created": created})
    return {"change": pr.number, "url": url, "decision_id": decision, "version": version,
            "second_approver": bool(weaker), "p0_downgrades": weaker, "created": created,
            "path": path, "branch": work}


# ── .boss/validators.yaml, proposed the same way ───────────────────────────────────────────────

def _sha(text: Optional[str]) -> Optional[str]:
    return hashlib.sha256(text.encode()).hexdigest()[:12] if text else None


def list_validators(c: Any, settings: Any, project_id: str) -> dict:
    """The base branch's `.boss/validators.yaml`: ``{validators, sha, path, exists, branch}``
    (``error`` when it does not parse — said, never guessed)."""
    adapter, branch = _repo(c, settings, project_id)
    body = adapter.read_text_file(branch, _validators.FILE)
    out = {"path": _validators.FILE, "branch": branch, "exists": body is not None,
           "sha": _sha(body), "validators": []}
    if body:
        try:
            out["validators"] = _validators.parse(body)
        except _validators.ValidatorsError as exc:
            out["error"] = str(exc)
    return out


def _render_validators(items: Sequence[Mapping[str, Any]]) -> tuple[str, list[dict]]:
    import yaml

    docs = []
    for v in items:
        d: dict[str, Any] = {"id": str(v.get("id") or "").strip(),
                             "applies_to": v.get("applies_to"), "run": v.get("run")}
        if v.get("fix"):
            d["fix"] = v["fix"]
        adapter = v.get("adapter") or ((v.get("output") or {}).get("adapter")
                                       if isinstance(v.get("output"), Mapping) else None)
        if adapter:
            d["output"] = {"adapter": adapter}
        for key in ("rules_doc", "ruleset"):
            if v.get(key):
                d[key] = v[key]
        docs.append(d)
    body = yaml.safe_dump({"validators": docs}, sort_keys=False, allow_unicode=True, width=100)
    return body, _validators.parse(body)


def validators_weakened(base: Sequence[Mapping[str, Any]], new: Sequence[Mapping[str, Any]]
                        ) -> list[str]:
    """Validators this edit removes, narrows (a file pattern dropped) or moves to another
    ruleset: the second-approver case, as a P0 downgrade is for a ruleset."""
    now = {v["id"]: v for v in new}
    out = []
    for v in base:
        n = now.get(v["id"])
        if n is None or set(v["applies_to"]) - set(n["applies_to"]) \
                or (v.get("ruleset") and n.get("ruleset") != v.get("ruleset")):
            out.append(v["id"])
    return out


def propose_validators(c: Any, settings: Any, project_id: str, *,
                       validators: Sequence[Mapping[str, Any]], base_sha: Optional[str],
                       actor: str) -> dict:
    """Open "Validators config" as a local change that adds or edits `.boss/validators.yaml`,
    with its merge decision (a person merges it). 409 when the base moved; 422 when the file
    would not parse. Removing or narrowing a validator needs a second approver."""
    from .core import create_blocking_decision, emit_event
    from .local_repo_host import change_set_url

    adapter, branch = _repo(c, settings, project_id)
    current = adapter.read_text_file(branch, _validators.FILE)
    if (base_sha or None) != _sha(current):
        raise RulesetChangeError(409, "stale_base", f"{_validators.FILE} changed since you "
                                 "opened it.", validators=list_validators(c, settings, project_id))
    try:
        body, parsed = _render_validators(validators)
    except _validators.ValidatorsError as exc:
        raise RulesetChangeError(422, "bad_validators", str(exc)) from None
    base = []
    if current:
        try:
            base = _validators.parse(current)
        except _validators.ValidatorsError:
            base = []
    weaker = validators_weakened(base, parsed)
    created = current is None
    title = "Validators config" + (" (new)" if created else "")
    to_sha = _sha(body)
    work = f"boss/validators/{to_sha}"
    adapter.ensure_branch(work, branch)
    head = adapter.commit_files(work, {_validators.FILE: body}, f"{title}\n\nOn-behalf-of: {actor}")
    pr = adapter.create_pull_request(
        work, title, (f"Adds `{_validators.FILE}`." if created else f"Edits `{_validators.FILE}`.")
        + (f" Removes or narrows: {', '.join(weaker)} — needs a second approver." if weaker
           else ""), base=branch)
    url = change_set_url(adapter.repository_full_name, pr.number)
    decision = create_blocking_decision(
        c, project_id, None, f"Merge {title} (change #{pr.number})?",
        {"action": "review_pull_request", "pull_request_url": url, "branch_name": work,
         "head_sha": head, "title": title, "requires_person_approval": True,
         "no_auto_merge": True,
         "validators_change": {"path": _validators.FILE, "from_sha": _sha(current),
                               "to_sha": to_sha, "created": created, "weakened": weaker,
                               "second_approver": bool(weaker)}},
        {}, "validators_change", risk_tier=TWO_PERSON_TIER if weaker else 2, default_action="hold")
    emit_event(c, project_id, "validators.change_proposed", actor, "decision", decision,
               {"change": pr.number, "created": created, "weakened": weaker})
    return {"change": pr.number, "url": url, "decision_id": decision, "created": created,
            "second_approver": bool(weaker), "weakened": weaker, "path": _validators.FILE}

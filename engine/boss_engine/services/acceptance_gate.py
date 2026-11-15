"""THE ACCEPTANCE GATE (`acceptance_met`, BOSSAPP-1211 / RCA-E): did the build deliver what was
asked, on the whole branch, before anyone is asked to merge it?

WHY IT EXISTS. The Northgate build (2026-11-15) shipped missing about fifteen must-haves. Its
reviewers said so (adversarial refuted, consistency drift, review BLOCKERs), but under the
community default profile `lean` those were advisory opinions, the PR opened, and the follow-up
run's spec was two MINOR findings, so its merge card said "Automated checks: passed". Nothing in
the pipeline ever held the build up against the brief's own must-haves.

WHAT IT DOES.
  - The items are the ROOT run's: the brief's must-haves (render_brief's "Must-haves:" block),
    the PRD's tested-and-gating acceptance list, and the run's acceptance criteria. A follow-up
    run inherits its root's items (its own spec is a handful of findings) — `items_spec`.
  - An LLM acceptance reviewer (the consistency lane's agent and model, read-only, the WHOLE
    branch tree, not the diff) answers every item MET / PARTIAL / MISSING with file evidence.
    It is a reviewer that thinks, not a keyword list: the code decides only the protocol (every
    item answered, evidence cited).
  - A deterministic check that the stack the spec requires has its manifest in the tree (a Go
    brief has a go.mod). `required_stack` is the one function RCA-A's structured
    `required_stack` replaces.
  - The gate FAILS CLOSED: it passes only when every item is MET with evidence that exists in
    the tree (not docs alone) and the reviewer's own summary line agrees. A PARTIAL, MISSING or
    unanswered item, a contradiction, or items that could not be found never pass.
  - A MISSING item or a missing manifest is a fact, not an opinion: the gate is in the advisory
    hard floor (governed_code_task_service._ADVISORY_HARD_FLOOR), so under `lean` it gets its
    repair rounds and then a labelled checks-failing draft PR with a follow-up carrying the
    missing items — never a "passed" card.
  - The merge card reads the UNION of the gates the lineage shipped past (`lineage_checks`), so
    a clean follow-up on a PR whose earlier builds failed can never say "passed".
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

GATE = "acceptance_met"
SEAT = "acceptance_check"
#: The key on the evidence (code_task_runs.state), the merge decision and the PR body.
KEY = "acceptance"
#: The evidence key holding the gate's findings as FINDING lines (checks_failing.SEAT_OF_GATE).
SUMMARY_KEY = "acceptance_summary"
#: Items per reviewer pass: a longer list is judged in several passes (`run_check`), never cut.
MAX_ITEMS = 40
MAX_TREE_FILES = 600

MET, PARTIAL, MISSING, UNJUDGED = "met", "partial", "missing", "unjudged"

_SKIP_DIRS = frozenset({".git", "node_modules", "vendor", ".venv", "venv", "dist", "build",
                        "__pycache__", ".next", "target", ".pytest_cache", ".mypy_cache"})


# ---------------------------------------------------------------------------------------------
# What was asked: the ACCEPTANCE CONTRACT (acceptance/contract.py, BOSSAPP-1214). The item
# extraction moved there unchanged and is re-exported here, so every caller and test of the gate
# reads the same functions.
# ---------------------------------------------------------------------------------------------

from .acceptance import contract as _contract  # noqa: E402
from .acceptance.contract import (  # noqa: E402,F401 — re-exported (#136's names)
    MAX_ITEM_CHARS, MAX_TOTAL_ITEMS, UNION_KEY, _BOLD_HEADING, _HEADING, _ITEM_LINE,
    _NOT_GATING, _items_and_dropped, _items_of, _norm, _raw_items, _sections, _spec_of,
    acceptance_items, brief_must_haves, item_key, items_dropped, lineage_build_rows,
    prd_gating_criteria, union_spec)


def items_spec(connection: Any, *, run_id: str, fctx: Mapping[str, Any],
               own_spec: Mapping[str, Any]) -> tuple[dict, dict]:
    """(the spec whose items this run is held to, where they came from {run_id, root}).

    HELD TO THE CONTRACT (BOSSAPP-1214): when the lineage has a frozen acceptance contract (one
    is frozen here the first time a lineage is checked), its items come FIRST — the ticket's
    criteria among them — and whatever the lineage's builds add beyond it (a later decomposed
    slice) follows, so a check never holds a run to less than it did before. `held_to_contract`.

    A FOLLOW-UP is held to the UNION of its lineage's builds (`lineage_build_rows`): its own spec is the
    findings it was sent to fix, and judged against those alone it "passes" while the must-haves
    the root missed stay missing (crun_15f0504ac9f64eaeb4a1). When that build cannot be found
    the follow-up has NO items (`root: False`, and the check is not_run) — never its own. Every
    other run (a repair child copies its parent's spec) is held to its own, which is the root's."""
    fctx = fctx or {}
    is_followup = bool(fctx.get("is_followup") or fctx.get("deferred_from_run"))
    if not is_followup:
        return held_to_contract(connection, run_id=run_id, fctx=fctx, own_spec=own_spec,
                                spec=_spec_of(own_spec), source={"run_id": run_id, "root": True})
    root = str(fctx.get("root_run_id") or fctx.get("deferred_from_run") or "").strip()
    builds = ([b for b in lineage_build_rows(connection, root) if b["id"] != run_id]
              if (root and connection is not None) else [])
    if not builds:
        return held_to_contract(
            connection, run_id=run_id, fctx=fctx, own_spec=own_spec, spec={},
            source={"run_id": None, "root": False,
                    "why": "the lineage's builds (and so its must-haves) could not be found"})
    # Found, with or without items: an item-less build's follow-up is `no_items`, not a failure.
    return held_to_contract(connection, run_id=run_id, fctx=fctx, own_spec=own_spec,
                            spec=union_spec(builds),
                            source={"run_id": builds[0]["id"], "root": True,
                                    "builds": [b["id"] for b in builds]})


def held_to_contract(connection: Any, *, run_id: str, fctx: Mapping[str, Any],
                     own_spec: Mapping[str, Any], spec: Mapping[str, Any],
                     source: Mapping[str, Any]) -> tuple[dict, dict]:
    """(spec, source) with the lineage's acceptance contract applied: the contract's items, then
    the items of `spec` it does not hold. Unchanged when there is no contract to hold the run
    to, or no database to keep one in (a check never fails because the contract could not be
    read: it falls back to exactly what it judged before)."""
    if connection is None or not hasattr(connection, "begin_nested"):
        return dict(spec), dict(source)
    try:
        with connection.begin_nested():
            contract = _contract.ensure_for_run(connection, run_id=run_id, fctx=fctx,
                                                own_spec=own_spec)
    except Exception:  # noqa: BLE001 — the check judges what it judged before
        import logging

        logging.getLogger(__name__).warning("acceptance contract unavailable for this check")
        return dict(spec), dict(source)
    if contract is None:
        return dict(spec), dict(source)
    held = _contract.gate_items(contract)
    keys = {_contract._key(i["text"]) for i in held}  # noqa: SLF001
    beyond = [{"source": s, "text": t} for s, t in _raw_items(spec)
              if _contract._key(t) not in keys]  # noqa: SLF001
    base = dict(spec)
    out_source = dict(source)
    if not out_source.get("root"):
        # The builds are gone but the contract is not: the run is held to it, never to nothing.
        out_source = {"run_id": contract["lineage_root_run_id"], "root": True}
    out_source["contract"] = {"id": contract["id"], "version": contract["version"],
                              "items": len(held)}
    return {**base, UNION_KEY: held + beyond}, out_source


# ---------------------------------------------------------------------------------------------
# The required stack (RCA-A replaces `required_stack` with the spec's structured field)
# ---------------------------------------------------------------------------------------------

def _language_key(word: str) -> Optional[str]:
    from . import stacks

    word = re.split(r"[\s/\-_.,;:()]+", str(word or "").strip().lower(), maxsplit=1)[0]
    key = stacks._normalise_language(word)  # noqa: SLF001
    return key if key in stacks.LANGUAGES else None


def required_stack(spec: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The stacks the spec names, each `hard` or not: [{stack, manifests, because, hard}].

    HARD (a missing manifest fails the gate): the spec's structured `required_stack` (RCA-A), or
    a `Stack:` line (stacks.declared_stack — the add-project start's and the architecture's).
    SOFT: languages the brief's words name (stacks.languages_named on the directive, with its
    negation and migration-source handling). Words are not a declaration ("React to alerts"),
    so a soft stack never fails the gate by itself: the reviewer is told and judges it, and the
    record carries a note. THE ONE PLACE RCA-A replaces with the structured field."""
    from . import stacks

    spec = _spec_of(spec)
    out: list[dict[str, Any]] = []

    def add(key: Optional[str], because: str, hard: bool) -> None:
        if not key or any(o["stack"] == key for o in out):
            return
        out.append({"stack": key, "manifests": list(stacks.LANGUAGES[key].manifests),
                    "because": because, "hard": hard})

    structured = spec.get("required_stack")
    if isinstance(structured, list) and structured:
        for s in structured:
            add(_language_key(s.get("language") if isinstance(s, Mapping) else s),
                "the spec's required_stack", True)
        return out
    instructions = str(spec.get("instructions") or "")
    for d in stacks.declared_stack(instructions) or []:
        add(_language_key(d.get("language")), f"Stack: line ({d.get('service')})", True)
    if out:
        return out
    directive = stacks.directive_text(instructions)
    first = directive.strip().splitlines()[0] if directive.strip() else ""
    words = "\n".join([first, *brief_must_haves(directive)])
    for key in stacks.languages_named(words):
        add(key, "named in the brief's words", False)
    return out


def _walk(root: Path, max_depth: int = 6) -> Iterable[Path]:
    root = Path(root)
    if not root.is_dir():
        return
    for dirpath, dirnames, filenames in os.walk(root):
        rel_depth = len(Path(dirpath).relative_to(root).parts)
        dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS and rel_depth < max_depth)
        for f in sorted(filenames):
            yield Path(dirpath) / f


def stack_manifest_check(root: Any, spec: Mapping[str, Any]) -> dict[str, Any]:
    """Each named stack and the manifest found for it (None = absent). `missing` holds only the
    HARD stacks with no manifest (they fail the gate); `notes` the soft ones (they do not)."""
    required = required_stack(spec)
    if not required or root is None:
        return {"required": required, "found": {}, "missing": [], "notes": []}
    wanted = {m for r in required for m in r["manifests"]}
    present: dict[str, str] = {}
    for p in _walk(Path(root)):
        if p.name in wanted and p.name not in present:
            present[p.name] = str(p.relative_to(root))
    found = {r["stack"]: next((present[m] for m in r["manifests"] if m in present), None)
             for r in required}
    return {"required": required, "found": found,
            "missing": [r["stack"] for r in required if r["hard"] and not found[r["stack"]]],
            "notes": [f"the brief's words name {r['stack']} but no {'/'.join(r['manifests'])} "
                      "is in the tree (not a declared stack, so the reviewer judges it)"
                      for r in required if not r["hard"] and not found[r["stack"]]]}


def tree_listing(root: Any, limit: int = MAX_TREE_FILES) -> str:
    """The branch's files, relative, for the reviewer's orientation (it reads them itself)."""
    if root is None:
        return ""
    paths = []
    for p in _walk(Path(root)):
        paths.append(str(p.relative_to(root)))
        if len(paths) >= limit:
            paths.append(f"... (more than {limit} files; open the tree)")
            break
    return "\n".join(paths)


# ---------------------------------------------------------------------------------------------
# The reviewer
# ---------------------------------------------------------------------------------------------

def _stack_lines(stack: Mapping[str, Any]) -> str:
    lines = []
    for r in stack.get("required") or []:
        path = (stack.get("found") or {}).get(r["stack"])
        state = f"manifest {path}" if path else "NO manifest found: " + "/".join(r["manifests"])
        kind = ("REQUIRED (declared)" if r["hard"]
                else "named in the brief's words (judge whether it is really required)")
        lines.append(f"- {r['stack']}: {kind}; {state}")
    return ("\n\nSTACK:\n" + "\n".join(lines)) if lines else ""


def acceptance_prompt(items: list[dict], *, title: str, stack: Mapping[str, Any],
                      tree: str) -> str:
    listed = "\n".join(f"ITEM {i['n']} [{i['source']}]: {i['text']}" for i in items)
    return (
        "You are BOSS's ACCEPTANCE REVIEWER for the build \"" + str(title or "") + "\". "
        "Your one question: does the code ON THIS BRANCH — the whole working tree in front of "
        "you, not only the latest change — deliver each item the person and the approved PRD "
        "asked for? Read the files. Judge what the code does, not what its README, comments or "
        "commit messages claim. A stub, a TODO, a hard-coded demo answer, a test that asserts "
        "nothing, or a feature present in a different language/stack than required is not MET. "
        "Do NOT edit any file.\n\n"
        "THE ITEMS:\n" + listed + _stack_lines(stack) + "\n\n"
        "Answer EVERY item, one line each, in this exact form:\n"
        "  ITEM <n>: MET — <the source file path(s) in this tree, and the function or test that "
        "delivers it>\n"
        "  ITEM <n>: PARTIAL — <what exists (files) and what is still missing>\n"
        "  ITEM <n>: MISSING — <what you looked for and where; what is absent>\n"
        "MET requires a path that exists in the tree and is code, configuration or a test — a "
        "README or other document alone is not evidence. When unsure, say PARTIAL and why.\n"
        "MET also requires the requirement to be WIRED: show the code that loads or uses what "
        "the item names. A file named after the requirement is not delivery on its own — a "
        "rules YAML the linter never reads (its checks hard-coded instead), a JSON Schema "
        "nothing validates against, a config field nothing enforces (a JWT audience no verifier "
        "checks, a JWKS URL while tokens are checked against a shared secret), a rule that "
        "exists only in a list or a doc. Name the loading/using function in the MET line; if "
        "you cannot find one, the item is PARTIAL (say what is not wired). Judge each part of "
        "an item that lists several (every rule, every field, every endpoint): one part "
        "missing makes it PARTIAL.\n"
        "End with EXACTLY one final line:\n  ACCEPTANCE: MET\n  ACCEPTANCE: NOT MET\n\n"
        "THE BRANCH'S FILES:\n" + (tree or "(empty)") + "\n"
    )


_ANSWER = re.compile(
    r"(?im)^[\s>*\-•]*\**\s*ITEM\s*#?\s*(\d+)\s*\**\s*[:.)\-]\s*\**\s*(MET|PARTIAL|MISSING)\b\**"
    r"\s*(?:[—–:\-]+\s*)?(.*)$")
_SUMMARY = re.compile(r"(?im)^[\s>*`]*ACCEPTANCE\s*:\s*\**\s*(NOT\s+MET|MET)\b")
_PATHISH = re.compile(r"(?<![\w/.-])([\w.\-]+(?:/[\w.\-]+)+|[\w\-]+\.[A-Za-z][\w]{0,8})"
                      r"(?::\d+)?(?![\w/])")
_DOC_PATH = re.compile(r"(?i)(^|/)(readme[^/]*|changelog[^/]*|docs?/.*|[^/]*\.(md|rst|txt|adoc))$")
#: Manifests and lockfiles declare a dependency; they deliver no behaviour.
#: Data files: what a requirement may NAME (a rulebook, a schema, a config) — delivery only when
#: code in the tree loads it (BOSSAPP-1233: a rules YAML the linter never read was cited as MET).
_DATA_PATH = re.compile(r"(?i)\.(ya?ml|json|toml|ini|cfg|conf|csv|xml)$")
_DECL_PATH = re.compile(r"(?i)(^|/)(go\.(mod|sum)|package(-lock)?\.json|[^/]*\.lock|"
                        r"pnpm-lock\.ya?ml|requirements[^/]*\.txt|pyproject\.toml|setup\.(py|cfg)|"
                        r"pipfile|cargo\.toml|pom\.xml|build\.gradle(\.kts)?|gemfile)$")


def _resolve(root: Path, cited: str) -> Optional[str]:
    """The tree path a cited path names (exact, else a unique suffix match), or None."""
    cited = cited.strip("`'\".,;:()[]").lstrip("./")
    if not cited or ".." in Path(cited).parts:
        return None
    direct = root / cited
    if direct.is_file():
        return cited
    hits = [p for p in _walk(root) if str(p.relative_to(root)).endswith("/" + cited)
            or (("/" not in cited) and p.name == cited)]
    return str(hits[0].relative_to(root)) if len(hits) == 1 else None


def evidence_paths(root: Any, why: str) -> tuple[list[str], list[str]]:
    """(cited paths that exist in the tree, cited paths that do not)."""
    if root is None:
        return [], []
    found, absent = [], []
    for m in _PATHISH.finditer(str(why or "")):
        cited = m.group(1)
        if re.fullmatch(r"\d+(\.\d+)*", cited):
            continue
        hit = _resolve(Path(root), cited)
        (found if hit else absent).append(hit or cited)
    return list(dict.fromkeys(found)), list(dict.fromkeys(absent))


def parse_acceptance(reply: str, items: list[dict], *, root: Any) -> list[dict[str, Any]]:
    """Each item with the reviewer's status and evidence (the LAST answer for an item wins).

    A MET stands only on evidence: at least one cited path that EXISTS in the tree and is not a
    document (README, *.md, docs/). Otherwise it reads as PARTIAL, saying why — an unevidenced
    or invented claim is what this gate exists to stop."""
    answers: dict[int, tuple[str, str]] = {}
    for m in _ANSWER.finditer(str(reply or "")):
        answers[int(m.group(1))] = (m.group(2).lower(), _norm(m.group(3))[:600])
    out = []
    loaders = _Loaders(root)
    for it in items:
        status, why = answers.get(int(it["n"]), (UNJUDGED, ""))
        if status == MET:
            found, absent = evidence_paths(root, why)
            code = [f for f in found if not _DOC_PATH.search(f) and not _DECL_PATH.search(f)]
            unwired = [f for f in code if _DATA_PATH.search(f)
                       and Path(f).name.lower() in str(it.get("text") or "").lower()
                       and not loaders.loads(f)]
            reason = ""
            if not code:
                reason = ("only manifests/lockfiles cited" if found and all(
                              _DECL_PATH.search(f) for f in found)
                          else "only documents cited" if found
                          else f"cited paths not in the tree: {', '.join(absent[:4])}" if absent
                          else "no file evidence cited")
            elif all(_DATA_PATH.search(f) for f in code):
                reason = ("only data files cited, no code shown loading them: "
                          + ", ".join(code[:4]))
            elif unwired:
                reason = ("the item's file is in the tree but nothing in it loads it: "
                          + ", ".join(unwired[:4]))
            if reason:
                status, why = PARTIAL, f"{why} ({reason})".strip()
        out.append({**it, "status": status, "evidence": why})
    return out


class _Loaders:
    """Which of the tree's data files some OTHER non-document file refers to (by path, file
    name, or its folder) — the least a loaded rulebook/schema/config shows. Read once, lazily."""

    MAX_BYTES = 512 * 1024

    def __init__(self, root: Any) -> None:
        self.root = Path(root) if root is not None else None
        self._texts: Optional[dict[str, str]] = None

    def _all(self) -> dict[str, str]:
        if self._texts is None:
            self._texts = {}
            for p in _walk(self.root) if self.root is not None else ():
                rel = str(p.relative_to(self.root))
                if _DOC_PATH.search(rel):
                    continue
                try:
                    if p.stat().st_size <= self.MAX_BYTES:
                        self._texts[rel] = p.read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    continue
        return self._texts

    def loads(self, rel: str) -> bool:
        path = Path(rel)
        names = {rel, path.name}
        if path.parent != Path("."):
            names |= {f"{path.parent.as_posix()}/", f'"{path.parent.as_posix()}"',
                      f"'{path.parent.as_posix()}'"}
        return any(any(n in text for n in names)
                   for other, text in self._all().items() if other != rel)


def judge(items: list[dict], reply: Optional[str], stack: Mapping[str, Any], *,
          root: Any, source: Optional[Mapping[str, Any]] = None,
          dropped: int = 0, summary_line: Optional[str] = "") -> dict[str, Any]:
    """The gate's record. It FAILS CLOSED; verdict:
      met       EVERY item MET with evidence in the tree, every declared manifest present, and
                the reviewer's own summary line says MET
      not_met   an item MISSING or PARTIAL, a declared stack's manifest absent, or the reviewer's
                summary contradicting its items (facts — repair, then a checks-failing PR)
      not_run   the reviewer left items unanswered, or the items could not be found (the
                check's fault, not the code's: it is re-run, and never passes)
      no_items  the run states nothing to hold it to (a non-follow-up with no criteria)"""
    stack = dict(stack or {})
    source = dict(source or {"root": True})
    # WHICH ITEMS this check judged: a later check clears this one's failure only if it judged
    # at least these (lineage_failures).
    source["item_keys"] = [item_key(i["text"]) for i in items]
    judged = parse_acceptance(reply or "", items, root=root) if items else []
    m = list(_SUMMARY.finditer(str(reply or "")))
    summary = (" ".join(m[-1].group(1).upper().split()) if m else None)
    if summary_line != "":
        summary = summary_line      # several passes: run_check combined their summary lines
    missing = [i["n"] for i in judged if i["status"] == MISSING]
    partial = [i["n"] for i in judged if i["status"] == PARTIAL]
    unjudged = [i["n"] for i in judged if i["status"] == UNJUDGED]
    met = [i["n"] for i in judged if i["status"] == MET]
    why = ""
    if stack.get("missing") or missing or partial:
        verdict = "not_met"
    elif not source.get("root"):
        verdict, why = "not_run", str(source.get("why") or "the must-haves were not found")
    elif not items:
        verdict = "no_items"
    elif unjudged:
        verdict, why = "not_run", f"{len(unjudged)} of {len(items)} items were not answered"
    elif dropped:
        # Items nobody judged are not met: never a pass over a list that was cut.
        verdict, why = "not_run", f"{dropped} items beyond {MAX_TOTAL_ITEMS} were not judged"
    elif summary != "MET":
        # A reviewer's slip, not the code's: re-ask the reviewer (HARNESS), never send the
        # coder to repair it. Still never a pass.
        verdict = "not_run"
        why = ("the reviewer's summary says NOT MET although every item reads met"
               if summary == "NOT MET" else "the reviewer gave no ACCEPTANCE summary line")
    else:
        verdict = "met"
    return {"verdict": verdict, "why": why, "summary_line": summary, "items": judged,
            "missing": missing, "partial": partial, "unjudged": unjudged, "met": met,
            "stack": stack, "source": source, "items_dropped": int(dropped or 0),
            "counts": {s: sum(1 for i in judged if i["status"] == s)
                       for s in (MET, PARTIAL, MISSING, UNJUDGED)}}


def batch_summary(replies: list[Optional[str]]) -> Optional[str]:
    """The combined ACCEPTANCE line of several passes: MET only when every pass said MET; NOT
    MET when any did; None when a pass gave none (or did not run)."""
    lines = []
    for reply in replies:
        m = list(_SUMMARY.finditer(str(reply or "")))
        lines.append(" ".join(m[-1].group(1).upper().split()) if m else None)
    if any(x == "NOT MET" for x in lines):
        return "NOT MET"
    return "MET" if lines and all(x == "MET" for x in lines) else None


def run_check(items: list[dict], *, ask: Any, title: str, stack: Mapping[str, Any], root: Any,
              source: Optional[Mapping[str, Any]] = None, dropped: int = 0
              ) -> tuple[dict[str, Any], int]:
    """Judge EVERY item: one reviewer pass per MAX_ITEMS batch (`ask(prompt) -> run_role
    result`), the item numbers global across passes. A pass that fails leaves its items
    unjudged, so the check is not_run — never met. Returns (record, cost in micros)."""
    import logging

    tree = tree_listing(root)
    replies: list[Optional[str]] = []
    cost = 0
    model = None
    for start in range(0, len(items), MAX_ITEMS):
        batch = items[start:start + MAX_ITEMS]
        try:
            rr = ask(acceptance_prompt(batch, title=title, stack=stack, tree=tree)) or {}
        except Exception as exc:  # noqa: BLE001 — this pass's items stay unjudged (not_run)
            logging.getLogger(__name__).warning("acceptance pass failed (%s)",
                                                type(exc).__name__)
            replies.append(None)
            continue
        cost += int(rr.get("cost_usd_micros") or 0)
        model = model or rr.get("model") or (rr.get("usage") or {}).get("model")
        replies.append(str(rr.get("summary") or ""))
    rec = judge(items, "\n".join(r or "" for r in replies), stack, root=root, source=source,
                dropped=dropped, summary_line=batch_summary(replies))
    rec["passes"] = len(replies)
    rec["model"] = model
    rec["reply"] = "\n".join(r or "" for r in replies)[:6000]
    return rec, cost


def passed(record: Optional[Mapping[str, Any]]) -> bool:
    return (record or {}).get("verdict") in ("met", "no_items")


def findings_summary(record: Optional[Mapping[str, Any]]) -> str:
    """The gate's findings as FINDING lines (review_comments.parse_findings reads them), so the
    checks-failing PR, its card and its follow-up carry each unmet item verbatim."""
    record = record or {}
    lines = []
    stack = record.get("stack") or {}
    for name in stack.get("missing") or []:
        req = next((r for r in stack.get("required") or [] if r.get("stack") == name), {})
        lines.append(f"FINDING: MAJOR — Required stack '{name}' has no manifest in the tree "
                     f"(expected {' or '.join(req.get('manifests') or [])}; declared by "
                     f"{req.get('because', 'the spec')}).")
    for status, word in ((MISSING, "MISSING"), (PARTIAL, "only PARTIAL"),
                         (UNJUDGED, "not judged")):
        for i in record.get("items") or []:
            if i.get("status") == status:
                lines.append(f"FINDING: MAJOR — Must-have {i['n']} {word}: {i['text']}"
                             + (f" — {i['evidence']}" if i.get("evidence") else ""))
    if record.get("why"):
        lines.append(f"FINDING: MAJOR — Acceptance check: {record['why']}.")
    for note in stack.get("notes") or []:
        lines.append(f"Note: {note}.")
    if record.get("items_dropped"):
        lines.append(f"Note: {record['items_dropped']} further items beyond the first "
                     f"{MAX_TOTAL_ITEMS} were not checked.")
    return "\n".join(lines)


_STATUS_WORD = {MET: "met", PARTIAL: "PARTIAL", MISSING: "MISSING", UNJUDGED: "NOT JUDGED"}


def must_have_table(record: Optional[Mapping[str, Any]]) -> str:
    """The must-have table (markdown) for the PR body and the merge card."""
    record = record or {}
    items = record.get("items") or []
    if not items and not (record.get("stack") or {}).get("required"):
        return ""

    def cell(s: Any) -> str:
        return _norm(s).replace("|", "\\|")
    rows = ["| # | Must-have | Status | Evidence |", "|---|---|---|---|"]
    rows += [f"| {i['n']} | {cell(i['text'])} | {_STATUS_WORD.get(i['status'], i['status'])} "
             f"| {cell(i.get('evidence'))} |" for i in items]
    for r in (record.get("stack") or {}).get("required") or []:
        path = ((record.get("stack") or {}).get("found") or {}).get(r["stack"])
        status = "met" if path else ("MISSING" if r.get("hard") else "not declared, not found")
        rows.append(f"| stack | {r['stack']} manifest ({' or '.join(r['manifests'])}) "
                    f"| {status} | {cell(path or 'not in the tree')} |")
    if record.get("items_dropped"):
        rows.append(f"\n{record['items_dropped']} further items beyond the first {MAX_TOTAL_ITEMS} "
                    "were not checked.")
    return "\n".join(rows)


def headline(record: Optional[Mapping[str, Any]]) -> str:
    record = record or {}
    c = record.get("counts") or {}
    if record.get("verdict") == "not_checked":
        return f"Must-haves: {record.get('why') or 'not checked'}"
    if record.get("verdict") == "not_run" and not record.get("items"):
        return f"Must-haves: not checked ({record.get('why') or 'no answer'})"
    if not record.get("items"):
        return ""
    bits = [f"{c.get(MET, 0)} met", f"{c.get(PARTIAL, 0)} partial", f"{c.get(MISSING, 0)} missing"]
    if c.get(UNJUDGED):
        bits.append(f"{c[UNJUDGED]} not judged")
    if (record.get("stack") or {}).get("missing"):
        bits.append("required stack missing: " + ", ".join(record["stack"]["missing"]))
    if record.get("items_dropped"):
        bits.append(f"{record['items_dropped']} not checked (over {MAX_TOTAL_ITEMS})")
    return "Must-haves: " + ", ".join(bits)


def pr_section(record: Optional[Mapping[str, Any]]) -> str:
    table = must_have_table(record)
    if not table:
        return ""
    return f"## Must-haves (acceptance check)\n\n{headline(record)}.\n\n{table}\n\n"


# ---------------------------------------------------------------------------------------------
# The lineage, for the merge card
# ---------------------------------------------------------------------------------------------

#: Gates a later build judges on the WHOLE tree, so its pass clears an earlier failure.
#: Judgment seats (review, security, consistency) re-judge only a follow-up's own delta and do
#: not; `acceptance_met` clears only by a check against the ROOT's items (see lineage_failures).
WHOLE_TREE_GATES = frozenset({"nothing_broke", "app_serves", "existing_tests_intact"})


def _gates_shipped_past(fctx: Mapping[str, Any]) -> list[str]:
    fctx = fctx or {}
    out: list[str] = []
    out += [str(g) for g in fctx.get("advisory_failing_gates") or []]
    out += [str(g) for g in (fctx.get("unresolved_dissent") or {}).get("unresolved_gates") or []]
    out += [str(g.get("gate")) for g in (fctx.get("checks_failing") or {}).get("gates") or []
            if isinstance(g, Mapping) and g.get("gate")]
    return [g for g in dict.fromkeys(out) if g]


def _gates_passed(value: Any) -> set[str]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return set()
    return {str(g) for g in value or []} if isinstance(value, list) else set()


def lineage_failures(connection: Any, *, run_id: str, pr_url: Optional[str] = None,
                     current_acceptance: Optional[Mapping[str, Any]] = None,
                     current_failing: Iterable[str] = ()) -> dict[str, Any]:
    """The gates THIS pull request's builds shipped past, across the lineage (root_run_id):
    {"gates": {gate: [run ids]}, "acceptance": the latest acceptance record against the root's
    items (None: never checked)}.

    Scoped to the pull request (`pr_url`; the current run is always in): an earlier, merged PR
    of the same lineage is not this one. A run that went on to a repair child never shipped and
    is not counted. A WHOLE_TREE_GATES failure clears when a later build of the PR passed that
    gate; `acceptance_met` clears only when the latest acceptance check AGAINST THE ROOT'S ITEMS
    met them; a judgment seat's failure never clears (a follow-up re-judges its own delta)."""
    from sqlalchemy import text

    row = connection.execute(text(
        "SELECT project_id, COALESCE(finalize_ctx->>'root_run_id', id) FROM code_task_runs "
        "WHERE id = :id"), {"id": run_id}).first()
    gates: dict[str, list[str]] = {}
    for g in current_failing or ():
        gates.setdefault(str(g), []).append(run_id)
    cur = dict(current_acceptance) if current_acceptance else None
    cur_ok = bool(cur and cur.get("verdict") in ("met", "not_met")
                  and (cur.get("source") or {"root": True}).get("root"))
    if row is None:
        acc = cur if cur_ok else None
        if acc and acc.get("verdict") == "not_met":
            gates.setdefault(GATE, [run_id])
        return {"gates": gates, "acceptance": acc}
    project_id, root = row[0], row[1]
    rows = connection.execute(text(
        "SELECT r.id, r.status, r.pull_request_url, r.finalize_ctx, r.state->'acceptance' AS acc, "
        "q.gates_passed FROM code_task_runs r LEFT JOIN quality_gates q "
        "ON q.code_task_run_id = r.id AND q.project_id = r.project_id "
        "WHERE r.project_id = :p AND (r.id = :root OR r.finalize_ctx->>'root_run_id' = :root) "
        "ORDER BY r.created_at, r.id"),
        {"p": project_id, "root": root}).mappings().all()
    checks: list[tuple[str, dict]] = []
    passed_after: dict[str, int] = {}
    failed_at: dict[str, int] = {}
    for idx, r in enumerate(rows):
        this = r["id"] == run_id
        if not this and pr_url and r["pull_request_url"] != pr_url:
            continue
        fctx = _spec_of(r["finalize_ctx"])
        shipped = this or r["pull_request_url"] or r["status"] == "completed"
        if shipped and r["status"] not in ("repairing", "cancelled", "failed"):
            for g in _gates_shipped_past(fctx):
                runs = gates.setdefault(g, [])
                if r["id"] not in runs:
                    runs.append(r["id"])
                failed_at[g] = idx
            for g in _gates_passed(r["gates_passed"]) & WHOLE_TREE_GATES:
                passed_after[g] = idx
        acc = _spec_of(r["acc"])
        if (acc.get("verdict") in ("met", "not_met") and not this
                and (acc.get("source") or {"root": True}).get("root")):
            checks.append((r["id"], acc))
    for g in list(gates):
        if g in WHOLE_TREE_GATES and passed_after.get(g, -1) > failed_at.get(g, 10 ** 9):
            gates.pop(g)
    if cur_ok:
        checks.append((run_id, cur))
    open_failures = _unresolved(checks)
    if open_failures:
        runs = gates.setdefault(GATE, [])
        for rid, _ in open_failures:
            if rid not in runs:
                runs.append(rid)
        latest = open_failures[-1][1]
    else:
        gates.pop(GATE, None)
        latest = checks[-1][1] if checks else None
        if cur and not cur_ok:
            latest = cur        # this run's own check could not run: say so, not an old pass
        elif latest and not cur_ok and checks[-1][0] != run_id:
            # THIS RUN CHANGED THE CODE AND NO CHECK SAW IT (a tier-0 follow-up has no
            # acceptance seat): an earlier `met` is not a pass for code it never read.
            latest = {**latest, "verdict": "not_checked",
                      "why": f"not checked since {checks[-1][0]}"}
    return {"gates": gates, "acceptance": latest}


def _unresolved(checks: list[tuple[str, dict]]) -> list[tuple[str, dict]]:
    """The not_met checks no LATER met check covered: a met clears a failure only when it
    judged at least the failed check's items (`source.item_keys`). Slice 3 meeting its own
    items never clears slice 2's gaps; a follow-up held to the union of every slice clears all."""
    out = []
    for i, (rid, acc) in enumerate(checks):
        if acc.get("verdict") != "not_met":
            continue
        keys = set((acc.get("source") or {}).get("item_keys") or [])
        cleared = any(later.get("verdict") == "met"
                      and keys <= set((later.get("source") or {}).get("item_keys") or [])
                      for _, later in checks[i + 1:])
        if not cleared:
            out.append((rid, acc))
    return out


def card_checks_failing(lineage: Mapping[str, Any], *, run_id: str) -> Optional[dict[str, Any]]:
    """A checks-failing record (checks_failing.collect's shape) for a merge card whose own run
    passed but whose lineage shipped past failing gates. None when nothing failed."""
    from . import checks_failing as cf

    gates = dict((lineage or {}).get("gates") or {})
    if not gates:
        return None
    rec = cf.collect(list(gates))
    for g in rec["gates"]:
        runs = [r for r in gates.get(g["gate"]) or [] if r != run_id]
        if runs:
            g["detail"] = ("shipped failing by an earlier build of this pull request ("
                           + ", ".join(runs[:4]) + ")")
    acc = (lineage or {}).get("acceptance")
    if GATE in gates and acc:
        from .review_comments import parse_findings

        rec["findings"] = [{"gate": GATE, "stage": SEAT, "security": False,
                            "severity": c.severity, "text": c.body}
                           for c in parse_findings(findings_summary(acc), stage=SEAT)]
    rec["lineage"] = True
    return rec


def merge_card_checks(connection: Any, *, run_id: Optional[str], evidence: Any,
                      checks_failing: Optional[dict], pr_url: Optional[str] = None
                      ) -> tuple[Optional[dict], Optional[dict]]:
    """(the checks-failing record the merge card carries, the acceptance record it shows).

    The run's own checks-failing record when it has one; else the pull request's lineage's
    (`lineage_failures`) — so a follow-up whose own checks passed still shows the gates its PR's
    earlier builds shipped past. The acceptance record is None when the must-haves were never
    checked against the root's items: the card then says so, never "passed". Never raises."""
    import logging

    acceptance = evidence.get(KEY) if isinstance(evidence, Mapping) else None
    card = checks_failing
    if not run_id:
        return card, acceptance
    try:
        from . import checks_failing as cf

        with connection.begin_nested():
            lineage = lineage_failures(connection, run_id=run_id, pr_url=pr_url,
                                       current_acceptance=acceptance,
                                       current_failing=cf.gate_names(checks_failing))
        acceptance = lineage.get("acceptance") or (
            acceptance if isinstance(acceptance, Mapping) else None)
        if not checks_failing:
            card = card_checks_failing(lineage, run_id=run_id)
    except Exception as exc:  # noqa: BLE001 — the card still opens; its own checks still show
        # Never the exception text: a driver error can carry the connection URL.
        logging.getLogger(__name__).warning("lineage checks not read for %s (%s)", run_id,
                                            type(exc).__name__)
    return card, acceptance


def card_words(card: Optional[Mapping[str, Any]], acceptance: Optional[Mapping[str, Any]],
               validation_status: str) -> str:
    """The card's "Automated checks" sentence. Never "passed" while a gate of the PR failed or
    the must-haves were not checked, met in full, against the root's items."""
    from . import checks_failing as cf

    if card:
        return f"Automated checks: failing ({cf.headline(card)}); CI {validation_status}"
    if not acceptance or acceptance.get("verdict") not in ("met", "no_items"):
        why = headline(acceptance) if acceptance else "Must-haves: not checked"
        return f"Automated checks: CI {validation_status}; {why}"
    return f"Automated checks: {validation_status}"


__all__ = [
    "batch_summary", "card_words", "evidence_paths", "run_check", "item_key", "items_dropped", "lineage_build_rows",
    "merge_card_checks", "union_spec", "UNION_KEY",
    "GATE", "SEAT", "KEY", "SUMMARY_KEY", "acceptance_items", "acceptance_prompt",
    "brief_must_haves", "card_checks_failing", "findings_summary", "headline", "items_spec",
    "judge", "lineage_failures", "must_have_table", "parse_acceptance",
    "passed", "pr_section", "prd_gating_criteria", "required_stack", "stack_manifest_check",
    "tree_listing",
]

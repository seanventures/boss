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
import logging
import re
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

GATE = "acceptance_met"
SEAT = "acceptance_check"
#: The key on the evidence (code_task_runs.state), the merge decision and the PR body.
KEY = "acceptance"
#: The evidence key holding the gate's findings as FINDING lines (checks_failing.SEAT_OF_GATE).
SUMMARY_KEY = "acceptance_summary"
MAX_TREE_FILES = 600


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

# ---------------------------------------------------------------------------------------------
# The verdict (acceptance/verdict.py, AA-F BOSSAPP-1220): parse, evidence and judge moved there
# unchanged — shared with the Acceptance Agent, which adds command evidence — and are
# re-exported here under #136's names, so every caller and test of the gate reads the same code.
# ---------------------------------------------------------------------------------------------

from .acceptance.verdict import (  # noqa: E402,F401 — re-exported (#136's names)
    MAX_ITEMS, MAX_PASSES, MET, MISSING, PARTIAL, UNJUDGED, _ANSWER, _BARE, _DATA_FILE,
    _DECL_PATH, _DOC_PATH, _KEYWORDS, _LOADED_BY, _PATHISH, _SKIP_DIRS, _STRING, _SUMMARY,
    _TEST_PATH, _advisory, _code_lines, _defines, _extensionless, _item_data_files, _refers_to,
    _resolve, _walk, batch_summary, clip, evidence_paths, findings_summary, judge,
    parse_acceptance, passed, wiring_problem)


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


def prepare_contract(db_engine: Any, *, run_id: str, fctx: Mapping[str, Any],
                     own_spec: Mapping[str, Any], agent: Any,
                     model: Optional[str] = None) -> None:
    """Before the check's transaction opens: read the run's ticket prose with `agent` (one
    single-turn verdict) and store it with the contract (acceptance.contract.prepare_for_run),
    so no model call ever runs inside an open database transaction (BOSSAPP-1232/1233). Never
    fails the check: the structural items stand without it."""
    from .engine_contract import run_verdict

    def ask(prompt: str) -> str:
        return str((run_verdict(agent, prompt, max_turns=2, model=model,
                                temp_prefix="boss-acceptance-prose-") or {}).get("summary") or "")
    try:
        _contract.prepare_for_run(db_engine, run_id=run_id, fctx=fctx, own_spec=own_spec,
                                  ask=ask, model=model)
    except Exception as exc:  # noqa: BLE001 — the check runs on what the contract holds
        logging.getLogger(__name__).warning("acceptance contract: prose not read (%s)",
                                            type(exc).__name__)


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
    # An item a change request explicitly replaced or removed is not added back from a
    # build's own spec (BOSSAPP-1322): the version that dropped it says so.
    keys = ({_contract._key(i["text"]) for i in held}  # noqa: SLF001
            | _contract.removed_item_keys(connection, contract))
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
    def tags(i: Mapping[str, Any]) -> str:
        t = _contract.floor_tags(i.get("text"), i, where="prompt")
        return "".join([", must be loaded" if t.get("needs_loader") else "",
                        ", judgement" if t.get("judgement") else ""])
    listed = "\n".join(f"ITEM {i['n']} [{i['source']}{tags(i)}]: {i['text']}" for i in items)
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
        "MET also requires the requirement to be WIRED. Decide for each item whether a file it "
        "names must be READ BY THE CODE (a rulebook, a schema, a config the program loads) or "
        "IS the deliverable itself (a compose file, an example, a CI workflow, a Makefile). "
        "For a file the code must read, a file named after the requirement is not delivery on "
        "its own — a rules YAML the linter never reads (its checks hard-coded instead), a JSON "
        "Schema nothing validates against, a config field nothing enforces (a JWT audience no "
        "verifier checks, a JWKS URL while tokens are checked against a shared secret), a rule "
        "that exists only in a list or a doc. Put the loader in the MET line as "
        "`LOADED BY <file>:<function>` (the non-test code that reads it; BOSS checks that the "
        "function is in that file); if you cannot find one, the item is PARTIAL (say what is "
        "not wired). Judge each part of an item that lists several (every rule, every field, "
        "every endpoint): one part missing makes it PARTIAL.\n"
        "An item marked `must be loaded` names a file the code must read: its MET line MUST "
        "carry `LOADED BY <file>:<function>`, the function DEFINED in that file, whose code "
        "names the file (its path, its folder or a glob). An item marked `judgement` is a "
        "matter of taste: judge it like any other.\n"
        "End with EXACTLY one final line:\n  ACCEPTANCE: MET\n  ACCEPTANCE: NOT MET\n\n"
        "THE BRANCH'S FILES:\n" + (tree or "(empty)") + "\n"
    )




#: Source files a project tree holds (any language BOSS builds).
_SOURCE = re.compile(r"(?i)\.(go|py|js|jsx|ts|tsx|mjs|cjs|java|kt|kts|scala|rb|rs|cs|php|swift|c|cc|"
                     r"cpp|h|hpp|ex|exs|dart|lua)$")
#: A reviewer saying a cited file is not there.
_NOT_THERE = re.compile(r"(?i)\b(does not exist|doesn't exist|no such file|not found|none of these "
                        r"files exist|not in the (working )?tree|no [\w./-]+ exists?|absent)\b")


def project_files(root: Any) -> dict[str, int]:
    """What a check root holds: {"manifests": n, "sources": n} (non-test and test alike)."""
    from .app_root import APP_MANIFESTS

    out = {"manifests": 0, "sources": 0}
    if root is None or not Path(root).is_dir():
        return out
    for p in _walk(Path(root)):
        if p.name in APP_MANIFESTS:
            out["manifests"] += 1
        elif _SOURCE.search(p.name):
            out["sources"] += 1
        if out["manifests"] and out["sources"] >= 3:
            break
    return out


def _not_there_paths(root: Any, evidence: str) -> list[str]:
    """The tree paths an answer said were NOT THERE that ARE in the tree: the paths cited in a
    clause that says so ("no go.mod exists"), or in the clause right before it ("looked for
    cmd/gwlint/main.go and examples/x.yaml; absent"). A path cited elsewhere in the answer
    ("this is Node: package.json, src/server.js") is not a claim that it is absent."""
    parts = [p for p in re.split(r";\s+|\.\s+(?=[A-Z(])|\n", str(evidence or "")) if p.strip()]
    out: list[str] = []
    for k, part in enumerate(parts):
        if not _NOT_THERE.search(part):
            continue
        for clause in (part, parts[k - 1] if k else ""):
            found, _ = evidence_paths(root, clause)
            out += [f for f in found if f not in out]
    return out


def refusal(record: Mapping[str, Any], root: Any) -> Optional[str]:
    """Why a check must not stand as a verdict on the code, or None (BOSSAPP-1323) — only on
    PROOF that the reviewer did not see the tree:

      the reviewer called items MISSING because their files were "not there", and those very
      files ARE in the tree it was sent (crun_95aca9c7's re-check called all 30 must-haves
      missing — "no go.mod exists anywhere in the tree" — on a branch with go.mod and 53 Go
      files). Two or more, and at least half of the missing items, so contradicted: the check
      is NOT RUN.

    Never the words alone, and never the tree's contents: a correct "go.mod does not exist, this
    is Node" verdict, or an empty build, is a CODE problem — MISSING, and the coder is sent
    (PR #164 review E1). A session that started outside its tree never answers at all
    (engine_contract.session_tree_problem refuses it at the start, so its pass fails: not run)."""
    if root is None:
        return None
    missing = [i for i in record.get("items") or [] if i.get("status") == MISSING]
    if not missing:
        return None
    seen = {i.get("n"): _not_there_paths(root, str(i.get("evidence") or "")) for i in missing}
    contradicted = {n: p for n, p in seen.items() if p}
    # At least two such items, and half of the missing ones: a single misread file is one
    # item's finding to re-ask, not a reviewer that never saw the tree.
    if len(contradicted) >= 2 and len(contradicted) * 2 >= len(missing):
        sample = sorted({p for ps in contradicted.values() for p in ps})[:4]
        return (f"the reviewer did not see the tree: it called {len(contradicted)} of "
                f"{len(missing)} missing items absent, but the files it said were not there are "
                f"in the tree ({', '.join(sample)})")
    return None


#: The run's acceptance tables, oldest first (state / evidence key).
HISTORY_KEY = "acceptance_history"


def keep_history(history: list, record: dict,
                 judged: Optional[dict] = None, *, keep_on_not_run: bool = False) -> dict:
    """Append this check's table to the run's `history` (never replacing an earlier one) and
    return the record the gate stands on: this one — unless it REFUSED to judge (`refusal`)
    while an earlier check of the run did judge (`judged`, a holder {"record": …} the caller
    keeps), in which case the earlier table stands, marked with why the later one did not
    count (crun_95aca9c7: a judged 25 met / 5 partial was replaced by a blind 0 / 30, and the
    follow-up's brief was built from the blind one)."""
    from datetime import datetime, timezone

    history.append({"at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "verdict": record.get("verdict"), "why": record.get("why") or "",
                    "counts": dict(record.get("counts") or {}),
                    "refused": bool(record.get("refused")),
                    "items": [{"n": i.get("n"), "status": i.get("status"),
                               # Never cut mid-sentence; BOSS's reason is kept whole.
                               "evidence": clip(str(i.get("evidence") or ""), 400)}
                              for i in record.get("items") or []]})
    holder = judged if judged is not None else {}
    # A re-check (`keep_on_not_run`): a check that did not run judges nothing either, so the
    # change's earlier judged table stands (change_recheck, #168 review M1).
    if record.get("refused") or (keep_on_not_run and record.get("verdict") == "not_run"):
        earlier = holder.get("record")
        if isinstance(earlier, dict):
            return {**earlier, "later_check_refused": record.get("why") or ""}
        return record
    if record.get("verdict") in ("met", "not_met"):
        holder["record"] = record
    return record


def run_check(items: list[dict], *, ask: Any, title: str, stack: Mapping[str, Any], root: Any,
              source: Optional[Mapping[str, Any]] = None, dropped: int = 0,
              batch_size: Optional[int] = None,
              checks: Any = None) -> tuple[dict[str, Any], int]:
    """Judge EVERY item: one reviewer pass per `batch_size` (default MAX_ITEMS) batch
    (`ask(prompt) -> run_role result`), the item numbers global across passes. A pass that fails
    leaves its items unjudged, so the check is not_run — never met. A pass stopped short (its
    turn or spend ceiling) keeps what it answered. EVERY pass that did not finish is recorded
    with WHY (`stops`), so the record's `why` and the card say what bound it — "pass 1: stopped
    at the $2.00 session ceiling", never only "30 of 30 items were not answered".
    Returns (record, cost in micros)."""
    import logging

    from .engine_contract import stop_words

    tree = tree_listing(root)
    replies: list[Optional[str]] = []
    blocked: list[Optional[str]] = []
    stops: list[dict[str, Any]] = []
    cost = 0
    model = None
    size = max(1, int(batch_size or MAX_ITEMS))
    cap = MAX_PASSES * MAX_ITEMS
    over = max(0, len(items) - cap)
    items = items[:cap]
    for start in range(0, len(items), size):
        batch = items[start:start + size]
        n = len(replies) + 1
        try:
            rr = ask(acceptance_prompt(batch, title=title, stack=stack, tree=tree)) or {}
        except Exception as exc:  # noqa: BLE001 — this pass's items stay unjudged (not_run)
            logging.getLogger(__name__).warning("acceptance pass failed (%s)",
                                                type(exc).__name__)
            # The class only: an exception's text can carry a connection URL.
            stops.append({"pass": n, "stop": "error", "items": len(batch),
                          "words": f"failed ({type(exc).__name__})"})
            replies.append(None)
            continue
        cost += int(rr.get("cost_usd_micros") or 0)
        model = model or rr.get("model") or (rr.get("usage") or {}).get("model")
        words = stop_words(rr)
        if words:
            stops.append({"pass": n, "stop": str(rr.get("stop_reason")), "items": len(batch),
                          "ceiling_usd": rr.get("budget_ceiling_usd"), "words": words})
        replies.append(str(rr.get("summary") or ""))
        blocked.append(rr.get("tools_blocked"))
    rec = judge(items, "\n".join(r or "" for r in replies), stack, root=root, source=source,
                dropped=dropped, summary_line=batch_summary(replies), over_cap=over,
                checks=checks)
    refused = refusal(rec, root) if items else None
    if items and not refused:
        # THE JUDGE'S TOOLS COULD NOT RUN (BOSSAPP-1350: bwrap, 30 items "could not be
        # verified"): nothing was read, so the items are unjudged, never partial. The engine's
        # own report of blocked tools decides (`tools_blocked`), or a judge that cited no file
        # and claimed nothing missing; never a MISSING item, and never the model's prose.
        from . import review_tools

        blind = next((b for b in blocked if b), None) or (
            "the judge cited no file and line for any item"
            if review_tools.reads_nothing(rec.get("items") or [], root) else None)
        if blind:
            refused = review_tools.not_run(blind)
    if refused:
        # NOT A VERDICT ON THE CODE: not run (re-asked, never met), its items unjudged so no
        # "missing" reaches the card, the ledger or a follow-up's brief.
        rec.update(verdict="not_run", why=refused, refused=True,
                   missing=[], partial=[], met=[],
                   unjudged=[i["n"] for i in rec.get("items") or []],
                   items=[{**i, "status": UNJUDGED, "evidence": f"not judged: {refused}"}
                          for i in rec.get("items") or []],
                   counts={"met": 0, "partial": 0, "missing": 0,
                           "unjudged": len(rec.get("items") or [])})
    rec["passes"] = len(replies)
    rec["batch_size"] = size
    rec["stops"] = stops
    if stops and rec.get("verdict") != "met":
        said = "; ".join(f"pass {x['pass']}: {x['words']}" for x in stops)
        rec["why"] = f"{rec['why']} ({said})" if rec.get("why") else said
    rec["model"] = model
    rec["reply"] = "\n".join(r or "" for r in replies)[:6000]
    return rec, cost


#: How much an escalation raises a spend ceiling that bound a pass.
CEILING_ESCALATION = 2.0


def escalation_plan(record: Optional[Mapping[str, Any]], *, max_turns: int,
                    default_ceiling_usd: Optional[float] = None,
                    clamp: Optional[Any] = None) -> Optional[dict[str, Any]]:
    """How to re-ask a check that left items unanswered, LIFTING THE LIMIT THAT BOUND IT.

    A pass stopped at its spend ceiling is re-asked with a raised ceiling AND half the batch;
    a turn-capped pass with `max_turns` (the caller's escalated turns). Returns
    {"max_turns", "max_budget_usd" (None = the session default), "batch_size"} — or None when
    the re-ask would run under the very limit that stopped it (refused: it would fail the
    same way, crun_9a9a535c). `clamp(usd) -> usd` bounds the raised ceiling (the pipeline's
    build_budget.lifted_ceiling_usd: at most MAX_LIFT x the session budget and within every
    run-level allowance), so repeated escalations never compound."""
    record = record or {}
    stops = list(record.get("stops") or [])
    batch = int(record.get("batch_size") or MAX_ITEMS)
    budget = [x for x in stops if x.get("stop") == "budget_cap"]
    if not budget:
        return {"max_turns": int(max_turns), "max_budget_usd": None, "batch_size": batch}
    bound = [float(x["ceiling_usd"]) for x in budget if x.get("ceiling_usd") is not None]
    previous = max(bound) if bound else (float(default_ceiling_usd)
                                         if default_ceiling_usd else None)
    ceiling = round(previous * CEILING_ESCALATION, 2) if previous else None
    if ceiling is not None and clamp is not None:
        ceiling = round(float(clamp(ceiling)), 2)
    if ceiling is not None and previous and ceiling <= previous:
        ceiling = None   # the clamp leaves no lift: only a smaller batch can change anything
    smaller = max(1, batch // 2)
    if ceiling is None and smaller >= batch:
        return None   # neither the ceiling nor the batch can change: the same limit again
    return {"max_turns": int(max_turns),
            # No lift left: re-ask under the ceiling that bound it, with the smaller batch.
            "max_budget_usd": ceiling if ceiling is not None else previous,
            "batch_size": smaller}



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

    def row(i: Mapping[str, Any]) -> str:
        # The CONTRACT's id (AC-n) when the item has one: the id a person names to replace it
        # (PR #161 review C2); the check's own position otherwise.
        return (f"| {i.get('id') or i['n']} | {cell(i['text'])}" + (" _(taste)_" if i.get("judgement") else "")
                + f" | {_STATUS_WORD.get(i['status'], i['status'])}"
                + (" — advisory (taste)" if _advisory(i) else "")
                + f" | {cell(i.get('evidence'))} |")
    rows += [row(i) for i in items if not i.get("request")]
    # THE ITEMS A CHANGE REQUEST ADDED, under the request that added them (BOSSAPP-1322).
    for request in dict.fromkeys(str(i["request"]) for i in items if i.get("request")):
        rows.append(f"| | **Added by the change \"{cell(request)}\"** | | |")
        rows += [row(i) for i in items if i.get("request") == request]
    for r in (record.get("stack") or {}).get("required") or []:
        path = ((record.get("stack") or {}).get("found") or {}).get(r["stack"])
        status = "met" if path else ("MISSING" if r.get("hard") else "not declared, not found")
        rows.append(f"| stack | {r['stack']} manifest ({' or '.join(r['manifests'])}) "
                    f"| {status} | {cell(path or 'not in the tree')} |")
    if record.get("items_not_judged"):
        rows.append(f"\n{record['items_not_judged']} items not judged (beyond {MAX_PASSES} "
                    f"reviewer passes of {MAX_ITEMS}): the check is not met.")
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
    reg = record.get("regressed_vs_parent") or {}
    bits = ([str(reg.get("words"))] if reg.get("words") else []) + [f"{c.get(MET, 0)} met", f"{c.get(PARTIAL, 0)} partial", f"{c.get(MISSING, 0)} missing"]
    if c.get(UNJUDGED):
        bits.append(f"{c[UNJUDGED]} not judged")
    if record.get("advisory"):
        bits.append(f"{len(record['advisory'])} advisory (taste)")
    if record.get("items_not_judged"):
        bits.append(f"{record['items_not_judged']} not judged (over {MAX_PASSES} passes)")
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

#: Gates every later build judges on the WHOLE tree, so its pass clears an earlier failure.
#: Judgment seats (review, security, consistency) clear only from a build that re-judged the
#: whole change, never from a follow-up, which judges its own delta; `acceptance_met` clears
#: only by a check against the ROOT's items (see lineage_failures).
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


def _full_review_gates(fctx: Mapping[str, Any], passed: set[str]) -> set[str]:
    """The passed gates a (non-follow-up) build RE-JUDGED over the whole change: the whole-tree
    gates always; a judgment seat's gate (security_clean, code_review_passed, consistent) only
    when that seat ran in FULL mode in this build. A repair or extend with a parent may review
    SCOPED (repair_review_scope: only its delta), and a scoped pass never re-read the file an
    earlier failure was about. A build with no parent is never scoped; one with a parent and no
    recorded mode is treated as scoped (fail closed)."""
    from .checks_failing import SEAT_OF_GATE

    modes = fctx.get("review_scope") if isinstance(fctx.get("review_scope"), Mapping) else {}
    has_parent = bool(str(fctx.get("parent_run_id") or "").strip())
    out = set()
    for g in passed - {GATE}:
        seat = (SEAT_OF_GATE.get(g) or (None,))[0]
        if g in WHOLE_TREE_GATES or seat is None:
            out.add(g)
        elif not has_parent or modes.get(seat) == "full":
            out.add(g)
    return out


def lineage_failures(connection: Any, *, run_id: str, pr_url: Optional[str] = None,
                     current_acceptance: Optional[Mapping[str, Any]] = None,
                     current_failing: Iterable[str] = ()) -> dict[str, Any]:
    """The gates THIS pull request's builds shipped past, across the lineage (root_run_id):
    {"gates": {gate: [run ids]}, "acceptance": the latest acceptance record against the root's
    items (None: never checked)}.

    Scoped to the pull request (`pr_url`; the current run is always in): an earlier, merged PR
    of the same lineage is not this one. A run that went on to a repair child never shipped and
    is not counted. A gate's failure clears when a later build of the PR re-ran that gate over
    the whole change and passed it (its quality gate lists it passed): the card reports the
    latest build's checks, and an earlier failure counts only while no later build re-ran that
    check (BOSSAPP-1320: dec_81fa4797 listed Security clean, Review passed and Consistent as
    failing "by an earlier build" although the build it spoke for had passed all three). A
    follow-up's judgment seats judge only its own delta, so from a follow-up only the
    WHOLE_TREE_GATES clear. `acceptance_met` clears only when the latest acceptance check
    AGAINST THE ROOT'S ITEMS met them."""
    from sqlalchemy import text

    row = connection.execute(text(
        "SELECT project_id, COALESCE(finalize_ctx->>'root_run_id', id), "
        "finalize_ctx->>'parent_run_id' FROM code_task_runs "
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
    project_id, root, parent_run = row[0], row[1], row[2]
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
            # THE LATEST BUILD'S VERDICT ON A CHECK IS THE PR'S (BOSSAPP-1320): a gate a later
            # build of this PR re-ran over the whole change and passed clears its earlier
            # failure. A follow-up's judgment seats read only its own delta (its base is the
            # shipped branch), so from a follow-up only the WHOLE_TREE_GATES clear.
            rejudged = (WHOLE_TREE_GATES if fctx.get("is_followup")
                        else _full_review_gates(fctx, _gates_passed(r["gates_passed"])))
            for g in _gates_passed(r["gates_passed"]) & rejudged:
                passed_after[g] = idx
        acc = _spec_of(r["acc"])
        if (acc.get("verdict") in ("met", "not_met") and not this
                and (acc.get("source") or {"root": True}).get("root")):
            checks.append((r["id"], acc))
    for g in list(gates):
        if g != GATE and passed_after.get(g, -1) > failed_at.get(g, 10 ** 9):
            gates.pop(g)
    if cur_ok:
        checks.append((run_id, cur))
    removed: set[str] = set()
    try:
        with connection.begin_nested():
            removed = {item_key(k) for k in _contract.removed_item_keys(
                connection, _contract.latest(connection, root_run_id=root,
                                             project_id=project_id))}
    except Exception:  # noqa: BLE001 — no contract: nothing was removed
        removed = set()
    open_failures = _unresolved(checks, removed)
    bar_followups = _unresolved(checks, removed, bar_accepted=True)
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
    return {"gates": gates, "acceptance": latest,
            "followups": [rid for rid, _ in bar_followups],
            "regressed": regressed_vs_parent(checks, run_id=run_id, parent_run_id=parent_run)}


def regressed_vs_parent(checks: list[tuple[str, dict]], *, run_id: str,
                        parent_run_id: Optional[str]) -> Optional[dict[str, Any]]:
    """{"parent_run_id", "parent": the parent's check, "from_met", "to_met"} when THIS run's
    acceptance check met fewer of the same must-haves than its parent's, else None
    (BOSSAPP-1330). A checks-failing follow-up of FXQuote went from 17 met to 13 and its merge
    card showed only the 13: the better verdict was quietly replaced. Compared only on the
    same items (the parent's item keys within this check's), so a slice judged on different
    items is never a regression."""
    if not parent_run_id:
        return None
    mine = next((acc for rid, acc in reversed(checks) if rid == run_id), None)
    parent = next((acc for rid, acc in reversed(checks) if rid == parent_run_id), None)
    if not mine or not parent:
        return None
    keys = set((parent.get("source") or {}).get("item_keys") or [])
    mine_keys = set((mine.get("source") or {}).get("item_keys") or [])
    if keys and mine_keys and not keys <= mine_keys:
        return None
    was = int((parent.get("counts") or {}).get(MET, 0) or 0)
    now = int((mine.get("counts") or {}).get(MET, 0) or 0)
    if now >= was:
        return None
    return {"parent_run_id": parent_run_id, "from_met": was, "to_met": now,
            "parent": parent, "words": f"regressed vs parent: {was}→{now} met"}


def regression_card(reg: Mapping[str, Any]) -> dict[str, Any]:
    """What a merge card carries of a regression: the words, both counts, the parent's table."""
    parent = reg.get("parent") or {}
    return {"words": reg.get("words"), "parent_run_id": reg.get("parent_run_id"),
            "from_met": reg.get("from_met"), "to_met": reg.get("to_met"),
            "parent_counts": parent.get("counts") or {},
            "parent_headline": headline({k: v for k, v in parent.items()
                                         if k != "regressed_vs_parent"}),
            "parent_table": must_have_table(parent)}


def _unresolved(checks: list[tuple[str, dict]],
                removed: Iterable[str] = (), *,
                bar_accepted: bool = False) -> list[tuple[str, dict]]:
    """The not_met checks no LATER met check covered: a met clears a failure only when it
    judged at least the failed check's items (`source.item_keys`). Slice 3 meeting its own
    items never clears slice 2's gaps; a follow-up held to the union of every slice clears all.
    `removed`: item keys a person has since replaced or removed from the contract — a later
    check need not judge those to clear the failure (PR #161 review C3).
    `bar_accepted`: the OTHER list — the unmet checks the quality bar let through (their gaps are
    follow-ups, shown as such on the card, never hidden and never counted as failures)."""
    out = []
    gone = set(removed or ())
    for i, (rid, acc) in enumerate(checks):
        if acc.get("verdict") != "not_met":
            continue
        if bool((acc.get("bar") or {}).get("nonblocking")) != bar_accepted:
            continue          # the quality bar let this record through (follow-ups, not a failure)
        keys = set((acc.get("source") or {}).get("item_keys") or []) - gone
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
        reg = lineage.get("regressed")
        if lineage.get("followups") and isinstance(acceptance, Mapping):
            acceptance = {**acceptance, "bar_followups": list(lineage["followups"])}
        if reg and isinstance(acceptance, Mapping):
            # BOTH VERDICTS ON THE CARD (BOSSAPP-1330): the worse one leads, said as a
            # regression, and the parent's better one stays beside it.
            acceptance = {**acceptance, "regressed_vs_parent": reg}
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
    if (acceptance or {}).get("verdict") == "not_met" and (
            (acceptance or {}).get("bar") or {}).get("nonblocking"):
        gaps = len([i for i in acceptance.get("items") or []
                    if isinstance(i, Mapping) and i.get("status") != "met"])
        return (f"Automated checks: {validation_status}; must-haves: {gaps} gap"
                f"{'s' if gaps != 1 else ''} are follow-ups at the "
                f"{acceptance['bar'].get('bar') or 'standard'} quality bar (not blocking)")
    if not acceptance or acceptance.get("verdict") not in ("met", "no_items"):
        why = headline(acceptance) if acceptance else "Must-haves: not checked"
        return f"Automated checks: CI {validation_status}; {why}"
    n = len((acceptance or {}).get("advisory") or [])
    gaps = len([i for i in (acceptance or {}).get("items") or []
                if isinstance(i, Mapping) and i.get("status") != "met"]) if (
        (acceptance or {}).get("bar") or {}).get("nonblocking") else 0
    return (f"Automated checks: {validation_status}" + (f", {n} advisory" if n else "")
            + (f", {gaps} must-have follow-up{'s' if gaps != 1 else ''} (quality bar)"
               if gaps else ""))


__all__ = [
    "batch_summary", "card_words", "evidence_paths", "run_check", "item_key", "items_dropped", "lineage_build_rows",
    "merge_card_checks", "union_spec", "UNION_KEY", "regressed_vs_parent", "regression_card",
    "GATE", "SEAT", "KEY", "SUMMARY_KEY", "acceptance_items", "acceptance_prompt",
    "brief_must_haves", "card_checks_failing", "findings_summary", "headline", "items_spec",
    "judge", "lineage_failures", "must_have_table", "parse_acceptance",
    "passed", "pr_section", "prd_gating_criteria", "required_stack", "stack_manifest_check",
    "tree_listing", "wiring_problem", "MAX_PASSES", "prepare_contract",
]

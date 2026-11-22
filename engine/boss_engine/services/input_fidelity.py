"""INPUT FIDELITY: a build that remediates an input artifact ships a minimal edit OF THAT INPUT.

THE RUN (northgate-gateway, workflow api_onboarding v13, wfr_40229c5c, build crun_58aecc16). The
step said: validate the partner's tenant file `examples/lumen-pay.bad.yaml` with the repo's
linter, apply `--fix`, hand-edit what is never fixable, write the result to
`config/tenants/lumen-pay.yaml`. The shipped file was the repo's reference answer
`examples/lumen-pay.good.yaml` (plus a refunds route the partner never sent, carried over from an
earlier build), with a comment header claiming "gwlint --fix applied" — no `--fix` ever ran. The
build's own "learned" note recorded `cp examples/lumen-pay.good.yaml …` as a reusable
workaround. Tests passed, review passed: nothing in the pipeline compared the output to the
input it was supposed to come from.

So, for any build that is "remediate this input" (declared as ``spec["remediate"]`` or
recognised from the brief — ``contract``):

1. THE BRIEF names the input, the output and the ANSWER FILES (a reference/golden/expected copy
   of the same subject, e.g. ``*.good.yaml``) and forbids reading or copying them (``brief``).
   The prompt is not the guard:
2. THE ENGINE CHECKS after every implement attempt (``check``): a structural diff of input →
   output (YAML/JSON) flags a block ADDED to a collection (a route, a listener…) and, when the
   validator's findings are known, a key added that no finding asks for; an output equal to an
   answer file, to any other data file in the tree, or to the pre-existing output left as it was
   is a COPIED ANSWER; a claim in the agent's words ("`--fix` applied") with no executed command
   behind it in the build's tool log is an UNBACKED CLAIM. A violation fails the attempt into the
   repair loop with the precise reason (``repair_evidence``) and holds the ``input_fidelity``
   gate, so a copied answer never ships.
3. THE VALIDATOR'S OWN OUTPUT is recorded (``run_validation``): findings before, the fixes
   applied (line before/after, auto or manual), findings after — the step output's
   ``validation`` key (shape in ``validation_shape``).

Generic: no linter, tenant or file name is special here.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
import shutil
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

logger = logging.getLogger(__name__)

#: The gate (quality_bar, checks_failing.HELD_GATES): a copied answer never opens a PR.
GATE = "input_fidelity"
#: The run state / step output keys.
STATE_KEY = "input_fidelity"
VALIDATION_KEY = "validation"

DATA_EXTS = (".yaml", ".yml", ".json")
#: A file whose name carries one of these words is an ANSWER for its subject, not an input.
ANSWER_MARKERS = frozenset({"good", "golden", "expected", "correct", "fixed", "solution",
                            "answer", "answers", "reference", "remediated", "known"})
#: Words that mark the input among several candidate files of one subject.
INPUT_MARKERS = frozenset({"bad", "broken", "invalid", "input", "raw", "original", "failing",
                           "unvalidated", "submitted", "partner"})
#: Keys that name an item of a collection, in the order they are tried.
IDENTITY_KEYS = ("path", "id", "name", "key", "route", "host", "uri", "match", "tenant", "url")
_SKIP_DIRS = frozenset({".git", "node_modules", "vendor", "dist", "build", ".venv", "venv",
                        "__pycache__", ".boss-validation", ".next", "target"})
_MAX_SCAN = 3000
_MAX_CHANGES = 200

_PATH_RE = re.compile(r"(?<![\w/.-])((?:\.?/)?[\w][\w./-]*\.(?:ya?ml|json))(?![\w-])")
_REMEDIATE_RE = re.compile(r"--fix\b|\bremediat|\bfix(?:es|ed|ing)?\b|\brepair|\bcorrect(?:s|ed)?\b",
                           re.I)
_OUTPUT_RE = re.compile(
    r"(?:write|save|put|store|place)\s+(?:the\s+)?(?:result|output|fixed\s+\w+|remediated\s+\w+|it)"
    r"\s+(?:to|at|as|in|into)\s+`?((?:\.?/)?[\w][\w./-]*\.(?:ya?ml|json))", re.I)
_BACKTICK_RE = re.compile(r"`([^`\n]{3,300})`")
_RUNNERS = frozenset({"go", "run", "cd", "sudo", "env", "npx", "pnpm", "yarn", "npm", "exec",
                      "python", "python3", "bash", "sh", "node", "uv", "poetry", "cargo", "-m"})
_CLAIM_VERBS = re.compile(r"\b(applied|ran|run|executed|passes|passed|returns|returned|reports|"
                          r"reported|clean|succeeds|succeeded|derived|generated)\b", re.I)
_CLAIM_PHRASE_RE = re.compile(
    r"\b([\w][\w./-]*(?:\s+[\w][\w-]*)?\s+--[\w-]+)\s+(?:was\s+|were\s+|has\s+been\s+|is\s+)?"
    r"(?:applied|run|ran|executed)\b", re.I)


# ── names ──────────────────────────────────────────────────────────────────────────────────────

def _clean_path(value: Any) -> str:
    p = str(value or "").strip().strip("`'\" ")
    while p.startswith("./"):
        p = p[2:]
    return p


def _name_tokens(path: str) -> list[str]:
    return [t for t in re.split(r"[^a-z0-9]+", str(path or "").lower()) if t]


def is_answer_path(path: Any) -> bool:
    """`examples/lumen-pay.good.yaml`, `fixtures/golden/x.json`, `y.expected.yml`: a reference
    answer, by its name. Data files only (YAML/JSON)."""
    p = _clean_path(path)
    if PurePosixPath(p).suffix.lower() not in DATA_EXTS:
        return False
    return bool(set(_name_tokens(p)) & ANSWER_MARKERS)


def answer_paths_in(text: Any) -> list[str]:
    """The answer files a piece of text names (a command, a learned note)."""
    out: list[str] = []
    for m in _PATH_RE.finditer(str(text or "")):
        p = _clean_path(m.group(1))
        if is_answer_path(p) and p not in out:
            out.append(p)
    return out


def _subject(path: str) -> str:
    """`examples/lumen-pay.bad.yaml` → `lumen pay`: the name without markers or extension."""
    stem = PurePosixPath(_clean_path(path)).name
    for ext in DATA_EXTS:
        if stem.lower().endswith(ext):
            stem = stem[: -len(ext)]
            break
    toks = [t for t in _name_tokens(stem) if t not in ANSWER_MARKERS | INPUT_MARKERS]
    return " ".join(toks)


def _data_files(root: Path) -> Iterable[str]:
    n = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for f in filenames:
            if f.lower().endswith(DATA_EXTS):
                n += 1
                if n > _MAX_SCAN:
                    return
                yield str(Path(dirpath, f).relative_to(root)).replace(os.sep, "/")


def _read(path: Path) -> Optional[str]:
    try:
        return path.read_text(errors="replace")
    except OSError:
        return None


def _scratch_write(root: Path, path: Path, text: str) -> None:
    """A copy BOSS writes for the VALIDATOR to run on, writable by the run's uid.

    The validator runs as the run's sandbox uid, which has no supplementary groups: a copy BOSS
    wrote 0644 in a 0755 directory is read-only to it. `gwlint --fix` then failed to write its
    fixes ("permission denied", exit 2, and `go run` reports exit 1), the copy came back
    unchanged, and BOSS's mechanical fix pass never seeded the target: 0 `auto` fixes on the
    real run (dry run 2, gap 4). Directories and the copy are opened to the run's uid, as
    seed_workspace does: safe, the run directory is <uid>:boss 2770."""
    from . import workspace_perms

    root = Path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    d = path.parent
    while d != root and root in d.parents:
        try:
            os.chmod(d, 0o2777)
        except OSError:
            pass
        d = d.parent
    workspace_perms.write_text(path, text)


# ── the contract ───────────────────────────────────────────────────────────────────────────────

def _resolve_program(command: str, root: Path) -> str:
    """`gwlint validate …` with no gwlint on PATH but a `cmd/gwlint` Go package → `go run
    ./cmd/gwlint validate …`. Anything else unchanged."""
    try:
        argv = shlex.split(command)
    except ValueError:
        return command
    if not argv or "/" in argv[0] or shutil.which(argv[0]):
        return command
    if (root / "cmd" / argv[0]).is_dir() and (root / "go.mod").is_file():
        return " ".join(["go", "run", f"./cmd/{argv[0]}", *map(shlex.quote, argv[1:])])
    return command


#: Shells a brief (or a lesson an agent wrote) wraps a command in: `/usr/bin/bash -lc '…'`.
_SHELLS = frozenset({"bash", "sh", "zsh", "dash"})
_SHELL_OPS = frozenset({"&&", "||", "|", ";", ">", ">>", "<", "&", "$(", "`"})


def normalise_command(command: str) -> tuple[Optional[str], Optional[str]]:
    """An INFERRED validator command as the sandbox can run it, or (None, why) when it cannot.

    The sandbox runs one allow-listed executable by NAME, without a shell. A command read from
    prose is unwrapped from a shell (`/usr/bin/bash -lc 'go run … {file}'` → `go run … {file}`)
    and an absolute executable is named (`/usr/local/go/bin/go` → `go`); one that needs a shell
    (`&&`, a pipe, a redirect) is REJECTED with that reason — never handed to the sandbox to
    error the validation without one."""
    cmd = str(command or "").strip()
    for _ in range(2):
        try:
            argv = shlex.split(cmd)
        except ValueError:
            return None, f"`{cmd[:120]}` does not parse as a command"
        if not argv:
            return None, "an empty command"
        if PurePosixPath(argv[0]).name in _SHELLS and len(argv) == 3 \
                and argv[1] in ("-c", "-lc", "-cl"):
            cmd = argv[2]
            continue
        break
    if any(tok in _SHELL_OPS or tok.startswith("$(") for tok in argv) \
            or PurePosixPath(argv[0]).name in _SHELLS:
        return None, (f"`{cmd[:120]}` needs a shell, and validators run one allow-listed "
                      "command without one; declare it in .boss/validators.yaml")
    if "/" in argv[0] and not argv[0].startswith("./"):
        argv[0] = PurePosixPath(argv[0]).name
    def _word(a: str) -> str:
        # `{file}` stays a bare placeholder (run_validation quotes the path it becomes).
        plain = a.replace("{file}", "f")
        return a if shlex.quote(plain) == plain else shlex.quote(a)

    return " ".join(_word(a) for a in argv), None


def _infer_validator(instructions: str, input_path: str, root: Path) -> Optional[dict]:
    """The validator a brief names in backticks (`gwlint validate {file}`), each command
    normalised for the sandbox (``normalise_command``). ``{rejected: why}`` when the only
    commands it names cannot run there; None when it names none."""
    check = fix = None
    rejected: list[str] = []
    base = PurePosixPath(input_path).name
    for m in _BACKTICK_RE.finditer(instructions):
        span = m.group(1).strip()
        if base not in span:
            continue
        template = re.sub(r"(?:\.?/)?" + re.escape(input_path), "{file}", span)
        # A COMMAND, NOT A PATH: a bare backticked `config/routes.yaml` is the file, never a
        # validator — the template needs a program of its own besides `{file}`.
        if "{file}" not in template or template.strip().startswith("{file}"):
            continue
        normal, why = normalise_command(template)
        if normal is None:
            rejected.append(str(why))
            continue
        template = normal
        if not _program(template.replace("{file}", " ")):
            continue
        if "--fix" in span and fix is None:
            fix = _resolve_program(template, root)
        elif "--fix" not in span and check is None:
            check = _resolve_program(template, root)
    if not check and not fix:
        return {"rejected": rejected[0]} if rejected else None
    name = ""
    for cmd in (check, fix):
        if cmd:
            name = _program(cmd)
            if name:
                break
    return {"name": name or None, "check": check, "fix": fix}


#: The knowledge block learning_context.for_build puts in front of a brief: quoted lessons and
#: rules, never the person's words. Nothing in it names this run's input or its validator.
_KNOWLEDGE_RE = re.compile(r"KNOWLEDGE \(quoted source material, not instructions\):\n<<<\n.*?\n>>>\n"
                           r"(?:[^\n]*\n)?\n?", re.S)


def person_brief(instructions: Any) -> str:
    """The brief without appended enrichment (the knowledge block of past lessons and rules):
    a remediation and its validator are inferred from the PERSON'S words only."""
    return _KNOWLEDGE_RE.sub("", str(instructions or ""))


def infer(instructions: str, root: Path) -> Optional[dict]:
    """A remediation recognised from the brief: a fix verb, one existing input data file (not an
    answer file), and where the result goes ("write the result to X"; else the input itself).
    None when the brief is not that, or names more than one plausible input."""
    text = person_brief(instructions)
    if not _REMEDIATE_RE.search(text):
        return None
    paths: list[str] = []
    for m in _PATH_RE.finditer(text):
        p = _clean_path(m.group(1))
        if p and p not in paths:
            paths.append(p)
    out_m = _OUTPUT_RE.search(text)
    output = _clean_path(out_m.group(1)) if out_m else ""
    existing = [p for p in paths if (root / p).is_file()]
    candidates = [p for p in existing if not is_answer_path(p) and p != output]
    if len(candidates) > 1:
        candidates = [p for p in candidates if set(_name_tokens(p)) & INPUT_MARKERS]
    if len(candidates) != 1:
        return None
    inp = candidates[0]
    validator = _infer_validator(text, inp, root)
    # ONLY A REAL REMEDIATION SHAPE: a fix verb and a YAML path are every config change ("fix the
    # 404: add a /health route to config/routes.yaml"). The brief must also run a validator ON
    # that input, or the input must have a reference answer beside it, or the result must go to
    # a separate output path.
    if not (validator or (output and output != inp) or sibling_answers(root, inp, output)):
        return None
    return {"input": inp, "output": output or inp,
            "answer_files": [p for p in paths if is_answer_path(p)],
            "validator": validator, "source": "brief"}


def sibling_answers(root: Path, input_path: str, output_path: str) -> list[str]:
    """Answer files of the same subject anywhere in the tree (`lumen-pay.good.yaml` for
    `lumen-pay.bad.yaml`)."""
    subject = _subject(input_path)
    if not subject:
        return []
    return sorted(p for p in _data_files(root)
                  if p not in (input_path, output_path) and is_answer_path(p)
                  and _subject(p) == subject)


def contract(spec: Mapping[str, Any], instructions: str, root: Any, *,
             trusted: Optional[Callable[[str], Optional[str]]] = None) -> Optional[dict]:
    """The remediation this build is held to, with the BASE snapshot (input text, the output as
    it stood before the build, the answer files' text) taken now, before any agent runs. A
    declared ``spec["remediate"] = {input, output?, answer_files?, validator?: {name?, check?,
    fix?}}`` wins; else ``infer`` from the brief. None for every other build."""
    root = Path(root)
    instructions = person_brief(instructions)
    declared = spec.get("remediate") if isinstance(spec, Mapping) else None
    explicit = False
    if isinstance(declared, Mapping) and declared.get("input"):
        inp = _clean_path(declared["input"])
        validator = declared.get("validator") if isinstance(declared.get("validator"),
                                                            Mapping) else None
        explicit = bool(validator)
        c = {"input": inp, "output": _clean_path(declared.get("output")) or inp,
             "answer_files": [_clean_path(p) for p in declared.get("answer_files") or []],
             "validator": dict(validator) if validator else
             _infer_validator(str(instructions or ""), inp, root),
             "source": "declared"}
        if declared.get("start_from"):
            # START FROM A FILE (start_from_file): the original is the file the run was given
            # (uploaded, or a repo path read from the trusted base) and the fix is made IN PLACE
            # at the target, so the change's diff reads original → fixed.
            original = declared.get("content")
            true_sha = declared.get("content_sha256")
            if original is not None:
                # AN UPLOAD IS KEPT MASKED (start_from_file.masked): its secret values never
                # reach the branch, the record or an agent; the TRUE bytes' sha256 is recorded.
                from .start_from_file import masked as _masked
                from .validators import sha256 as _sha256

                raw = str(original)
                original = _masked(raw)
                true_sha = true_sha or _sha256(raw)
            src = _clean_path(declared.get("original_path"))
            if src and (src.startswith("/") or "\\" in src
                        or ".." in PurePosixPath(src).parts):
                # A path INSIDE the repository only: never a host file read into the change.
                logger.info("input_fidelity: the file to start from (%s) is outside the repo", src)
                return None
            if original is None and src:
                original = (trusted(src) if trusted is not None else None)
                if original is None:
                    original = _read(root / src)
            if original is None:
                logger.info("input_fidelity: the file to start from (%s) is not readable", src)
                return None
            c.update(output=inp, in_place=True, _original=str(original),
                     original_path=src or None, **{"from": "upload" if src == "" else "repo"})
            if true_sha:
                c["input_sha256"] = str(true_sha)
    else:
        c = infer(str(instructions or ""), root)
    if not c:
        return None
    # THE REPO'S OWN DECLARATION (.boss/validators.yaml, SPEC §2) names the validator for the
    # target (or the input) unless the spec declared one: its command, adapter and ruleset. Both
    # it and the ruleset are read from ``trusted`` (the base branch) when given: the branch the
    # agent edits never picks its own validator or lowers its own P0s. A change of either on
    # the branch is recorded as a PENDING config change, not applied (``pending_config``).
    from . import rulesets as _rulesets
    from . import validators as _validators

    pending: list[dict] = []
    # THE REPO'S DECLARATION WINS over anything inferred from prose, whenever it applies to the
    # input, the output or the file the run started from; only a validator the run's spec
    # declared explicitly is kept over it.
    if not explicit:
        try:
            repo_v = _validators.for_path(
                root, c["output"], c["input"],
                *([_clean_path(declared.get("original_path"))]
                  if isinstance(declared, Mapping) and declared.get("original_path") else []),
                read=trusted)
            if trusted is not None and _validators.for_path(root, c["output"], c["input"]) \
                    not in (None, repo_v):
                pending.append({"file": _validators.FILE, "applied": False})
        except Exception:  # noqa: BLE001 — a broken file is said on the record, not raised
            logger.info("input_fidelity: %s not read", _validators.FILE, exc_info=True)
            repo_v = None
        if repo_v:
            c["validator"] = repo_v
    # THE RULESET (.boss/rulesets/*.yaml, SPEC §4): P-levels, autofix policy and the guides the
    # fixing agent receives verbatim (``brief``).
    vname = (c.get("validator") or {}).get("ruleset") if isinstance(c.get("validator"),
                                                                     Mapping) else None
    if vname:
        try:
            c["ruleset"] = _rulesets.load(root, vname, read=trusted)
            if trusted is not None:
                change = _rulesets.unapplied(root, vname, c["ruleset"])
                if change:
                    pending.append(change)
        except Exception:  # noqa: BLE001 — a broken ruleset is said, never guessed
            logger.info("input_fidelity: ruleset %s not read", vname, exc_info=True)
    if isinstance(c.get("validator"), Mapping) and c["validator"].get("rejected"):
        # AN INFERRED COMMAND THE SANDBOX CANNOT RUN, and nothing declared: said on the record
        # (run_validation), never run to fail without a reason.
        c["validator_rejected"] = str(c["validator"]["rejected"])
        c["validator"] = None
    if pending:
        c["pending_config"] = pending
    input_text = c.pop("_original", None) if c.get("in_place") else _read(root / c["input"])
    if input_text is None:
        logger.info("input_fidelity: the input %s is not in the tree", c["input"])
        return None
    answers = list(dict.fromkeys([*c["answer_files"],
                                  *sibling_answers(root, c["input"], c["output"])]))
    c["answer_files"] = [p for p in answers if p not in (c["input"], c["output"])]
    c["_input_text"] = input_text
    c["_output_base"] = _read(root / c["output"]) if c["output"] != c["input"] else None
    c["_answer_texts"] = {p: t for p in c["answer_files"]
                          if (t := _read(root / p)) is not None}
    return c


def lineage_contract(connection: Any, project_id: str, run_id: str, root_run_id: Optional[str],
                     branch: Optional[str]) -> Optional[dict]:
    """THE CHANGE'S VALIDATION CONTRACT, for EVERY build on it: a repair, a follow-up, a
    review-fix run or a retry carries no ``remediate`` of its own, yet it writes the same
    target on the same branch, so it is held to the first build's contract (its declared
    ``remediate``, and the original commit its record names). ``{run_id, remediate, state}`` of
    the lineage's first build that declared one (same root, or the same work branch); None when
    the change has no contract."""
    from sqlalchemy import text as _text

    row = connection.execute(_text(
        "SELECT id, spec->'remediate' AS r, state->'remediation' AS s FROM code_task_runs "
        "WHERE project_id = :p AND id <> :me AND spec->'remediate' IS NOT NULL "
        "AND jsonb_typeof(spec->'remediate') = 'object' "
        "AND (id = :root OR finalize_ctx->>'root_run_id' = :root "
        "     OR (:b <> '' AND (finalize_ctx->>'branch_name' = :b "
        "                       OR finalize_ctx->>'work_branch' = :b))) "
        "ORDER BY created_at LIMIT 1"),
        {"p": project_id, "me": run_id, "root": str(root_run_id or run_id),
         "b": str(branch or "")}).mappings().first()
    if row is None:
        return None

    def _obj(v: Any) -> dict:
        if isinstance(v, str):
            try:
                v = json.loads(v)
            except ValueError:
                return {}
        return dict(v) if isinstance(v, Mapping) else {}

    rem = _obj(row["r"])
    if not rem.get("input"):
        return None
    return {"run_id": str(row["id"]), "remediate": rem, "state": _obj(row["s"])}


def public(c: Optional[Mapping[str, Any]]) -> Optional[dict]:
    """The contract without its snapshots, for the run's state and the PR."""
    if not c:
        return None
    return {k: v for k, v in c.items() if not str(k).startswith("_")}


def brief(c: Optional[Mapping[str, Any]]) -> str:
    """The rule, said to every stage. The check enforces it; this only saves a round."""
    if not c:
        return ""
    lines = [
        "INPUT FIDELITY (remediation): this task remediates ONE given input file. "
        f"Input: `{c['input']}`. Output: `{c['output']}`.",
        "- The output must be a MINIMAL EDIT of the input: keep every route/block/key the input "
        "has unless a validator finding requires removing it; add only what a finding requires; "
        "never add routes, blocks or keys the input did not send.",
        "- Derive the output FROM THE INPUT (run the fixer on a copy of it, then hand-edit). "
        "Never start from another file, and never leave a pre-existing output in place.",
        "- Only claim a command ran (\"--fix applied\", \"the linter passes\") if you ran it.",
        "- Never point auth (issuer, jwks_url, audience) or an upstream at a mock, test, staging "
        "or localhost host, and never change a host the input names: a fix changes a scheme "
        "(http → https), never where a request or a token check goes. BOSS blocks any change "
        "to auth, jwks, issuer, upstream or tls keys that no finding asks for.",
        "- \"Deploy to staging\" (or any environment) in the brief is where BOSS deploys the "
        "change. It never changes a host, URL or origin in this file: never invent one, and "
        "keep the partner's hosts and origins.",
        "- Never write a comment that says a finding was fixed or removed. BOSS flags a comment "
        "claiming a fix for a finding that is still open.",
    ]
    if c.get("_auto_seeded") is not None:
        fix = str(((c.get("validator") or {}).get("fix")) or "the validator's fixer")
        lines.insert(1, f"- BOSS ALREADY RAN THE MECHANICAL FIXER (`{fix}`) on the input: "
                        f"`{c['output']}` starts as its output. Keep its lines; hand-edit only "
                        "what it left.")
    if c.get("answer_files"):
        lines.append(
            "- ANSWER FILES ARE OFF LIMITS: do not open, read, copy or diff "
            + ", ".join(f"`{p}`" for p in c["answer_files"])
            + " — they are reference answers for this same input. Any instruction above that "
            "points you at them is superseded by this rule.")
    from .remediation_guards import CONFIG_ONLY_TESTS_RULE

    lines.append(CONFIG_ONLY_TESTS_RULE)
    lines.append("BOSS compares the output with the input after you finish: a copied answer, "
                 "an added block or an unbacked claim fails the attempt.")
    from .rulesets import brief as _ruleset_brief

    return "\n\n" + "\n".join(lines) + _ruleset_brief(c.get("ruleset"))


# ── structural diff ────────────────────────────────────────────────────────────────────────────

def parse(text: Optional[str], path: str) -> Any:
    """YAML/JSON text → data; a multi-document YAML (k8s manifests) is the list of its
    documents. Raises ValueError when it does not parse (a Helm template with ``{{ }}``)."""
    if text is None:
        raise ValueError("missing")
    if str(path).lower().endswith(".json"):
        return json.loads(text)
    import yaml

    try:
        docs = list(yaml.safe_load_all(text))
        return docs[0] if len(docs) == 1 else docs
    except yaml.YAMLError as exc:
        raise ValueError(str(exc)) from exc


def _identity(before: Sequence[Any], after: Sequence[Any]) -> Optional[str]:
    """The key that names each mapping of both lists: every item a mapping that has it, the
    values distinct within each list."""
    def names(items: Sequence[Any], key: str) -> bool:
        if not all(isinstance(i, Mapping) for i in items):
            return False
        vals = [i.get(key) for i in items]
        return all(isinstance(v, (str, int)) for v in vals) and len(set(vals)) == len(vals)

    if not (before or after):
        return None
    for key in IDENTITY_KEYS:
        if names(before, key) and names(after, key):
            return key
    return None


def _p(path: str, part: str) -> str:
    return f"{path}.{part}" if path else part


def structural_diff(before: Any, after: Any, path: str = "") -> list[dict]:
    """The changes input → output: ``{op: added|removed|changed, path, kind: key|item|value,
    key?, identity?, value?/before?/after?}``. Mappings by key; a list of mappings by the key
    that names its items (``IDENTITY_KEYS``), else by position; a list of scalars is one value.
    An added or removed subtree is ONE change (its children are not listed again)."""
    out: list[dict] = []
    if isinstance(before, Mapping) and isinstance(after, Mapping):
        for k in after:
            if k not in before:
                out.append({"op": "added", "kind": "key", "path": _p(path, str(k)),
                            "key": str(k), "value": after[k]})
        for k in before:
            if k not in after:
                out.append({"op": "removed", "kind": "key", "path": _p(path, str(k)),
                            "key": str(k), "value": before[k]})
        for k in before:
            if k in after:
                out.extend(structural_diff(before[k], after[k], _p(path, str(k))))
        return out
    if (isinstance(before, list) and isinstance(after, list)
            and any(isinstance(i, Mapping) for i in [*before, *after])):
        key = _identity(before, after)
        if key:
            b = {i.get(key): i for i in before}
            a = {i.get(key): i for i in after}
            for ident, item in a.items():
                where = f"{path}[{key}={ident}]"
                if ident not in b:
                    out.append({"op": "added", "kind": "item", "path": where, "key": key,
                                "identity": str(ident), "value": item})
                else:
                    out.extend(structural_diff(b[ident], item, where))
            for ident, item in b.items():
                if ident not in a:
                    out.append({"op": "removed", "kind": "item", "path": f"{path}[{key}={ident}]",
                                "key": key, "identity": str(ident), "value": item})
            return out
        for i in range(max(len(before), len(after))):
            where = f"{path}[{i}]"
            if i >= len(before):
                out.append({"op": "added", "kind": "item", "path": where, "value": after[i]})
            elif i >= len(after):
                out.append({"op": "removed", "kind": "item", "path": where, "value": before[i]})
            else:
                out.extend(structural_diff(before[i], after[i], where))
        return out
    if before != after:
        out.append({"op": "changed", "kind": "value", "path": path,
                    "before": before, "after": after})
    return out


# ── is an addition asked for? ──────────────────────────────────────────────────────────────────

def _words(text: Any) -> list[str]:
    return [w for w in re.split(r"[^a-z0-9]+", str(text or "").lower()) if w]


def _finding_words(finding: Mapping[str, Any]) -> list[str]:
    return _words(" ".join(str(finding.get(k) or "") for k in
                           ("rule", "message", "title", "why", "fix", "path")))


def justified(change: Mapping[str, Any], findings: Optional[Sequence[Mapping[str, Any]]]) -> bool:
    """Whether a validator finding asks for this addition: its key's words (the first two, each
    as a word prefix: `allow_origins` ← "allowed origins") or the added item's identity appear in
    a finding's text."""
    if not findings:
        return False
    if change.get("kind") == "item":
        ident = str(change.get("identity") or "").lower()
        return bool(ident) and any(ident in " ".join(str(v) for v in f.values()).lower()
                                   for f in findings)
    toks = _words(change.get("key"))[:2]
    if not toks:
        return False
    for f in findings:
        words = _finding_words(f)
        if all(any(w.startswith(t) or (len(w) >= 3 and t.startswith(w)) for w in words)
               for t in toks):
            return True
    return False


# ── claims and the tool log ────────────────────────────────────────────────────────────────────

def _program(command: str) -> str:
    try:
        argv = shlex.split(command)
    except ValueError:
        argv = command.split()
    for tok in argv:
        if tok in _RUNNERS or tok.startswith("-") or "=" in tok:
            continue
        name = PurePosixPath(tok).name
        if name and re.match(r"^[a-z][\w-]*$", name, re.I):
            return name.lower()
    return ""


def _claim_tokens(claim: str) -> list[str]:
    """What a claim's command must have run: its program and its flags. Never its file names —
    the honest way to run a fixer is on a copy (`gwlint --fix /tmp/t.yaml`)."""
    try:
        argv = shlex.split(claim)
    except ValueError:
        argv = claim.split()
    toks = [_program(claim)] if _program(claim) else []
    toks += [tok.split("=", 1)[0].lower() for tok in argv if tok.startswith("--")]
    return [t for t in dict.fromkeys(toks) if t]


def claims_in(texts: Iterable[Any]) -> list[str]:
    """The commands a text says were run: backticked commands on a line with a claim verb
    ("`gwlint validate --json x.yaml` returns …"), and "<tool> --flag applied/ran" phrases."""
    out: list[str] = []
    for text in texts:
        for line in str(text or "").splitlines():
            if not _CLAIM_VERBS.search(line):
                continue
            for m in _BACKTICK_RE.finditer(line):
                span = m.group(1).strip()
                if len(span.split()) >= 2 and re.search(r"(^|\s)--?\w", span) and _program(span):
                    out.append(span)
            for m in _CLAIM_PHRASE_RE.finditer(line):
                out.append(m.group(1).strip())
    return list(dict.fromkeys(out))


_WRAPPER_RUNNERS = {"npm", "pnpm", "yarn", "npx"}
_MAX_WRAPPER_CHARS = 200_000


def _expand_wrappers(command: str, root: Optional[Path]) -> str:
    """A command plus what it runs through a wrapper in the repo: the Makefile for `make`, the
    package.json scripts for npm/pnpm/yarn, a script file it names (`sh scripts/lint.sh`,
    `./fix.sh`). So `make fix-tenant` backs a claim that `gwlint --fix` ran."""
    if root is None:
        return command
    try:
        argv = shlex.split(command.replace("&&", " ; ").replace("||", " ; "))
    except ValueError:
        argv = command.split()
    extra: list[str] = []
    names = {PurePosixPath(t).name for t in argv}
    if "make" in names or "gmake" in names:
        extra += [str(_read(root / n) or "") for n in ("Makefile", "makefile", "GNUmakefile")]
    if names & _WRAPPER_RUNNERS:
        extra.append(str(_read(root / "package.json") or ""))
    for tok in argv:
        if tok.startswith("-") or len(tok) > 300:
            continue
        p = (root / tok.lstrip("./")) if not tok.startswith("/") else None
        try:
            if p is not None and p.is_file() and p.suffix.lower() in ("", ".sh", ".bash", ".mk"):
                extra.append(str(_read(p) or ""))
        except OSError:
            continue
    return "\n".join([command, *extra])[:_MAX_WRAPPER_CHARS]


def unbacked_claims(texts: Iterable[Any], executed: Sequence[str], *,
                    results: Sequence[str] = (), root: Optional[Path] = None) -> list[str]:
    """Claims no execution backs. A claim is backed by a command (or the wrapper it went
    through) that contains its program and its flags, or by a tool result that shows the
    program's own output."""
    ran = [_expand_wrappers(str(c or ""), root).lower() for c in executed]
    shown = [str(r or "").lower() for r in results]
    missing = []
    for claim in claims_in(texts):
        toks = _claim_tokens(claim)
        if len(toks) < 2:
            continue
        if any(all(t in cmd for t in toks) for cmd in ran):
            continue
        if any(toks[0] in out for out in shown) and all(
                any(f in cmd for cmd in ran) for f in toks[1:]):
            continue
        missing.append(claim)
    return missing


def executed_commands(connection: Any, run_id: str) -> Optional[dict]:
    """The build's tool log: ``{"commands", "results", "complete"}`` from agent_turns. None when
    the run recorded no tool calls at all (turn capture off). ``complete`` is False when a
    stored turn was truncated or a stage hit the stored-turns cap: then a claim is UNKNOWN, never
    violated."""
    from sqlalchemy import text as sql

    from .agent_turns import max_turns_stored

    rows = connection.execute(sql(
        "SELECT direction, content, truncated, stage FROM agent_turns "
        "WHERE code_task_run_id = :r AND direction IN ('tool_call', 'tool_result') "
        "ORDER BY created_at"), {"r": run_id}).mappings().all()
    if not any(r["direction"] == "tool_call" for r in rows):
        return None
    commands: list[str] = []
    results: list[str] = []
    per_stage: dict[str, int] = {}
    complete = True
    for r in rows:
        per_stage[str(r["stage"])] = per_stage.get(str(r["stage"]), 0) + 1
        if r["truncated"]:
            complete = False
        raw = str(r["content"] or "")
        if r["direction"] == "tool_result":
            results.append(raw[:20_000])
            continue
        try:
            data = json.loads(raw)
        except ValueError:
            data = None
        if isinstance(data, Mapping):
            cmd = data.get("command") or data.get("cmd")
            if isinstance(cmd, str) and cmd.strip():
                commands.append(cmd.strip())
    if any(n >= max_turns_stored() for n in per_stage.values()):
        complete = False
    return {"commands": commands, "results": results, "complete": complete}


def _new_comment_lines(text: Optional[str], input_text: Optional[str]) -> str:
    """The output's comment lines the input did not already have: only the build's own words
    are its claims."""
    had = {line.strip() for line in str(input_text or "").splitlines()}
    return "\n".join(line.strip().lstrip("#").strip() for line in str(text or "").splitlines()
                     if line.strip().startswith("#") and line.strip() not in had)


# ── the check ──────────────────────────────────────────────────────────────────────────────────

def _norm_text(text: Optional[str], *, comments: bool = True) -> str:
    return "\n".join(line.rstrip() for line in str(text or "").strip().splitlines()
                     if line.strip() and (comments or not line.strip().startswith("#")))


def _same_data(a_text: Optional[str], b_text: Optional[str], path: str) -> bool:
    try:
        return parse(a_text, path) == parse(b_text, path)
    except ValueError:
        return _norm_text(a_text, comments=False) == _norm_text(b_text, comments=False)


def _short(value: Any, limit: int = 160) -> str:
    s = json.dumps(value, default=str) if not isinstance(value, str) else value
    return s if len(s) <= limit else s[: limit - 1] + "…"


def _tool_log(executed: Any) -> Optional[dict]:
    if executed is None:
        return None
    if isinstance(executed, Mapping):
        return {"commands": list(executed.get("commands") or []),
                "results": list(executed.get("results") or []),
                "complete": bool(executed.get("complete", True))}
    return {"commands": list(executed), "results": [], "complete": True}


def check(root: Any, c: Mapping[str, Any], *,
          findings: Optional[Sequence[Mapping[str, Any]]] = None,
          claims: Iterable[Any] = (),
          executed: Any = None) -> dict:
    """Judge the output against the input. ``{"ok", "violations": [...], "advisories": [...],
    "notes": [...], "changes": [...], "input", "output", "answer_files"}``. A violation fails
    the attempt; an advisory is recorded and shown, never failing. Codes:

    - ``missing_output``: no output; ``unparseable``: the output is not YAML/JSON while the
      input is (an input that does not parse — a Helm template — skips the structural checks);
    - ``copied_answer``: the output equals (as text, or as data) an answer file;
    - ``copied_file``: the output's text equals another data file in the tree;
    - ``unchanged_preexisting``: the output existed before the build and is still the same
      data — nothing was derived from the input;
    - ``added_block``: an item added to a collection the input did not have, no finding asks;
    - ``added_key``: a key added no finding asks for (only when findings are known);
    - ``unbacked_claim``: the agent says a command ran and no execution backs it.

    Additions BLOCK for a declared contract, and for a contract read from the brief only when
    the validator ran and reported findings to judge them against; otherwise they are advisory.
    Claims are UNKNOWN (a note) when the tool log is incomplete.
    """
    root = Path(root)
    inp, outp = str(c["input"]), str(c["output"])
    violations: list[dict] = []
    advisories: list[dict] = []
    notes: list[str] = []
    result = {"ok": False, "input": inp, "output": outp,
              "answer_files": list(c.get("answer_files") or []), "violations": violations,
              "advisories": advisories, "notes": notes, "changes": []}
    out_text = _read(root / outp)
    if out_text is None:
        violations.append({"code": "missing_output",
                           "message": f"The output `{outp}` was not written."})
        return result
    changes: Optional[list[dict]] = None
    try:
        before = parse(c.get("_input_text"), inp)
    except ValueError:
        notes.append(f"`{inp}` is not parseable YAML/JSON: the structural checks were skipped.")
    else:
        try:
            after = parse(out_text, outp)
        except ValueError as exc:
            violations.append({"code": "unparseable",
                               "message": f"`{outp}` does not parse: {str(exc)[:200]}"})
            return result
        changes = structural_diff(before, after)
        from .validators import mask

        result["changes"] = [{k: (mask(_short(v)) if k in ("value", "before", "after") else v)
                              for k, v in ch.items()} for ch in changes[:_MAX_CHANGES]]
    differs = bool(changes) if changes is not None else (
        _norm_text(out_text, comments=False) != _norm_text(c.get("_input_text"), comments=False))

    # COPIED: an answer file, or any other data file, as the output.
    norm_out = _norm_text(out_text)
    for path, text in (c.get("_answer_texts") or {}).items():
        if _norm_text(text) == norm_out or (differs and _same_data(text, out_text, outp)):
            violations.append({"code": "copied_answer", "source": path, "message": (
                f"`{outp}` is a copy of the reference answer `{path}`, not a remediation of "
                f"`{inp}`. Start from `{inp}`, apply the fixer to a copy of it, and hand-edit "
                "only what the findings require. Do not read or copy the answer file.")})
    if not any(v["code"] == "copied_answer" for v in violations):
        for path in _data_files(root):
            if path in (inp, outp) or not path.lower().endswith(PurePosixPath(outp).suffix.lower()):
                continue
            other = _read(root / path)
            if other is not None and _norm_text(other) == norm_out and norm_out:
                violations.append({"code": "copied_file", "source": path, "message": (
                    f"`{outp}` is byte-for-byte the existing file `{path}`, not a remediation of "
                    f"`{inp}`.")})
                break
    base = c.get("_output_base")
    if base is not None and differs and _same_data(base, out_text, outp):
        violations.append({"code": "unchanged_preexisting", "message": (
            f"`{outp}` already existed before this build and its content is unchanged (comments "
            f"aside) — nothing in it was derived from `{inp}`. Rebuild it from `{inp}`.")})

    # ADDED: content the input never had and no finding asks for.
    additions_block = c.get("source") != "brief" or bool(findings)
    for ch in changes or []:
        if ch["op"] != "added":
            continue
        item = None
        if ch["kind"] == "item" and not justified(ch, findings):
            item = {"code": "added_block", "path": ch["path"], "message": (
                f"`{outp}` adds {ch['path']} — `{inp}` has no such entry and no finding asks "
                "for it. Remove it: a remediation keeps the input's entries and adds none.")}
        elif ch["kind"] == "key" and findings is not None and not justified(ch, findings):
            item = {"code": "added_key", "path": ch["path"], "message": (
                f"`{outp}` adds `{ch['path']}`, which `{inp}` does not have and no validator "
                "finding asks for. Remove it, or name the finding that requires it.")}
        if item:
            (violations if additions_block else advisories).append(item)

    # CLAIMED: a command the agent says ran must be backed by an execution.
    log = _tool_log(executed)
    if log is not None:
        texts = [*claims, _new_comment_lines(out_text, c.get("_input_text"))]
        missing = unbacked_claims(texts, log["commands"], results=log["results"], root=root)
        if missing and not log["complete"]:
            notes.append("The tool log is incomplete (truncated or capped): claims not judged: "
                         + ", ".join(f"`{m}`" for m in missing))
        else:
            for claim in missing:
                violations.append({"code": "unbacked_claim", "message": (
                    f"The build says `{claim}` ran, but no command in its tool log ran it. Run "
                    "it, or remove the claim.")})
    result["ok"] = not violations
    return result


def repair_evidence(result: Mapping[str, Any]) -> str:
    """The repair round's evidence: each violation, word for word."""
    lines = [f"INPUT FIDELITY FAILED for `{result.get('output')}` (input `{result.get('input')}`)."
             " The output must be a minimal, evidenced remediation of the input:"]
    lines += [f"- [{v['code']}] {v['message']}" for v in result.get("violations") or []]
    if result.get("answer_files"):
        lines.append("Answer files you must not read or copy: "
                     + ", ".join(f"`{p}`" for p in result["answer_files"]))
    return "\n".join(lines)


# ── the validator's own record ─────────────────────────────────────────────────────────────────

def validation_shape() -> dict:
    """The ``validation`` record (step output and code_task_runs.state): ``boss.validation/v1``
    (validators.record, SPEC §1.3), documented."""
    return {"schema": "boss.validation/v1",
            "validator": "{id, version, adapter}", "ruleset": "{id, version, sha}|None",
            "input": "{file, sha256, from} — the bytes the run received",
            "target": "path — the file the build writes",
            "before": "{command, exit, findings: [V-n]} — the validator on the input",
            "fix": "{command, exit}|None — the auto-fixer on a copy of the input",
            "fixes": "[{finding, rule, file, line, before, after, kind: auto|agent, hunk_id}]",
            "after": "{command, exit, findings: [V-n still open]}|None — on the target",
            "findings": "[boss.finding/v1]", "hunks": "[{id, line, removed, added}]",
            "evidence": "[{id: E-n, kind: command, cmd, exit}]",
            "guard": "{code: input_substituted, source, why}|None", "error": "str|None"}


_FINDING_LISTS = ("violations", "findings", "issues", "errors", "results", "problems",
                  "diagnostics", "messages")


def parse_findings(stdout: str, file: str) -> Optional[list[dict]]:
    """A linter's JSON report → ``[{rule, severity, file, line, message, fixable}]``. None when
    the output is not JSON with a findings list."""
    text = str(stdout or "").strip()
    data: Any = None
    for candidate in (text, text[text.find("{"):] if "{" in text else "",
                      text[text.find("["):] if "[" in text else ""):
        if not candidate:
            continue
        try:
            data = json.loads(candidate)
            break
        except ValueError:
            continue
    if data is None:
        return None
    items = data if isinstance(data, list) else None
    if isinstance(data, Mapping):
        for key in _FINDING_LISTS:
            if isinstance(data.get(key), list):
                items = data[key]
                break
    if items is None:
        return None
    out = []
    for f in items:
        if not isinstance(f, Mapping):
            continue
        line = f.get("line") if isinstance(f.get("line"), int) else (
            (f.get("location") or {}).get("line") if isinstance(f.get("location"), Mapping)
            else None)
        out.append({
            "rule": str(f.get("rule_id") or f.get("rule") or f.get("id") or f.get("code")
                        or f.get("check") or ""),
            "severity": str(f.get("severity") or f.get("level") or ""),
            "file": str(f.get("file") or f.get("path") or file),
            "line": line,
            "message": str(f.get("message") or f.get("title") or f.get("msg")
                           or f.get("description") or ""),
            "fixable": f.get("fixable"),
            # Kept for justification only (the fix hint names the keys a finding asks for).
            **({"fix": str(f["fix"])} if f.get("fix") else {}),
            **({"why": str(f["why"])} if f.get("why") else {}),
        })
    return out


def run_validation(root: Any, c: Mapping[str, Any],
                   run: Optional[Callable[[str], Any]], *,
                   ruleset: Optional[Mapping[str, Any]] = None) -> dict:
    """Run the validator on a copy of the input (before), the fixer on another copy, the
    validator on the target (after), and record it all as ``boss.validation/v1``
    (validators.record): findings V-n with P-levels, the fixes as hunks tied to findings, the
    input-substitution guard. ``run(command) -> result`` with ``stdout``/``exit_code`` (the
    sandbox's allow-listed runner). The copies carry the TARGET's file name (a linter may check
    that a file is named for what it holds). Never raises."""
    from . import validators as _v
    from .validator_adapters import parse as _adapt

    root = Path(root)
    v = c.get("validator") if isinstance(c.get("validator"), Mapping) else {}
    ruleset = ruleset or c.get("ruleset")
    commands: dict[str, Optional[str]] = {"before": None, "fix": None, "after": None}
    exits: dict[str, Any] = {}
    before_raw = after_raw = None
    auto_text = out_text = guard = error = None
    scratch = root / ".boss-validation"
    try:
        input_text = str(c.get("_input_text") or "")
        name = PurePosixPath(str(c["output"])).name
        if run is not None and (v.get("check") or v.get("fix")):
            copy = scratch / name
            _scratch_write(root, copy, input_text)
            if v.get("check"):
                cmd = str(v["check"]).replace("{file}", shlex.quote(str(copy.relative_to(root))))
                commands["before"] = cmd
                r = run(cmd)
                exits["before"] = getattr(r, "exit_code", None)
                before_raw = _adapt(v.get("adapter"), getattr(r, "stdout", "") or "", c["input"])
                if before_raw is None:
                    error = "the validator's output on the input is not a report"
            if v.get("fix"):
                fixed = scratch / "fixed" / name
                _scratch_write(root, fixed, input_text)
                cmd = str(v["fix"]).replace("{file}", shlex.quote(str(fixed.relative_to(root))))
                commands["fix"] = cmd
                r = run(cmd)
                exits["fix"] = getattr(r, "exit_code", None)
                auto_text = _read(fixed)
            if v.get("check") and (root / c["output"]).is_file():
                cmd = str(v["check"]).replace("{file}", shlex.quote(str(c["output"])))
                commands["after"] = cmd
                r = run(cmd)
                exits["after"] = getattr(r, "exit_code", None)
                after_raw = _adapt(v.get("adapter"), getattr(r, "stdout", "") or "", c["output"])
        if c.get("validator_rejected") and not (v.get("check") or v.get("fix")):
            error = f"no validator could run: {c['validator_rejected']}"
        out_text = _read(root / c["output"])
        guard = _v.substituted(root, c, out_text, _data_files(root))
    except Exception as exc:  # noqa: BLE001 — a record that could not be made says why
        logger.info("input_fidelity: validation record failed", exc_info=True)
        error = f"{type(exc).__name__}: {str(exc)[:200]}"
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    rec = _v.record(c=c, validator=v, version=v.get("version") or _repo_tool_version(root, v),
                    before_raw=before_raw,
                    after_raw=after_raw, commands=commands, exits=exits, out_text=out_text,
                    auto_text=auto_text, ruleset=ruleset, guard=guard, error=error)
    rec["_auto_text"] = auto_text            # enforce_autofix reads it; never stored
    # Justification only (check's added-key rule reads the fix hints); never stored.
    rec["_hints"] = [{"rule": f.get("rule_id"), "severity": f.get("native_severity"),
                      "file": c["input"], "line": f.get("line"), "message": f.get("title"),
                      "fixable": f.get("fixable"),
                      **({"fix": f["fix"]} if f.get("fix") else {}),
                      **({"why": f["why"]} if f.get("why") else {})}
                     for f in before_raw or []]
    return rec


def _repo_tool_version(root: Path, v: Mapping[str, Any]) -> Optional[str]:
    """A validator built from the repo (`go run ./cmd/gwlint …`) is versioned by the repo's
    commit; a tool from PATH has no version BOSS can vouch for."""
    if "./" not in str(v.get("check") or ""):
        return None
    import subprocess

    try:
        out = subprocess.run(["git", "-C", str(root), "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip() or None


def guard_violation(validation: Optional[Mapping[str, Any]]) -> Optional[dict]:
    """The input-substitution guard as a check violation (it fails the attempt)."""
    g = (validation or {}).get("guard")
    if not g:
        return None
    return {"code": "input_substituted", "source": g.get("source"), "message": (
        f"`{(validation or {}).get('target')}` is not derived from the input the run received "
        f"({g.get('why')}: `{g.get('source')}`). Rebuild it from the input.")}


def findings_for_check(validation: Optional[Mapping[str, Any]]) -> Optional[list[dict]]:
    """The findings that may justify an addition: the validator's findings on the input, or None
    when no validator ran (then only added collection items are judged)."""
    if not validation or validation.get("error") \
            or not (validation.get("before") or {}).get("command"):
        return None
    return list(validation.get("_hints") or [])


def public_validation(rec: Mapping[str, Any]) -> dict:
    """The stored record: without the justification-only fields (its lines are already
    secret-masked by validators.record)."""
    return {k: v for k, v in rec.items() if not str(k).startswith("_")}


# ── the mechanical fixer first, and the ruleset's autofix policy enforced ──────────────────────

def seed_auto_fix(root: Any, c: dict, run: Optional[Callable[[str], Any]]) -> Optional[str]:
    """BOSS runs the validator's mechanical fixer (``gwlint --fix``) on the partner's file BEFORE
    any agent, and the target starts as its output: the fixer's lines are then the build's
    ``auto`` fixes (validators.changed_lines reads provenance from the same fixer output) and
    the agent hand-edits only what it left. Returns the command, or None when there is no fixer,
    the target is the input itself, or the fixer wrote nothing. Never raises."""
    v = c.get("validator") if isinstance(c.get("validator"), Mapping) else {}
    root = Path(root)
    # IN PLACE (start_from_file): seeded only while the target still holds the original, so a
    # follow-up build never puts the fixer's output over fixes already on the change.
    fresh = bool(c.get("in_place")) and _read(root / str(c.get("output"))) == c.get("_input_text")
    if run is None or not v.get("fix") or (str(c.get("output")) == str(c.get("input"))
                                           and not fresh):
        c["auto_fix_seed"] = {"seeded": False, "why": (
            "no fixer declared" if not v.get("fix") else "no runner" if run is None
            else "the target already carries this change's fixes" if c.get("in_place")
            else "the output is the input")}
        return None
    scratch = root / ".boss-validation" / "seed"
    seed: dict = {"seeded": False}
    try:
        copy = scratch / PurePosixPath(str(c["output"])).name
        _scratch_write(root, copy, str(c.get("_input_text") or ""))
        cmd = str(v["fix"]).replace("{file}", shlex.quote(str(copy.relative_to(root))))
        seed["command"] = cmd
        r = run(cmd)
        seed["exit"] = getattr(r, "exit_code", None)
        fixed = _read(copy)
        if not fixed or fixed == c.get("_input_text"):
            # SAID, never silent: why the agent starts from the partner's file.
            tail = (str(getattr(r, "stderr", "") or "") + str(getattr(r, "stdout", "") or ""))
            seed["why"] = ("the fixer changed nothing" + (f": {tail.strip()[-200:]}"
                                                          if seed.get("exit") else ""))
            return None
        target = root / str(c["output"])
        _scratch_write(root, target, fixed)
        c["_auto_seeded"] = fixed
        seed["seeded"] = True
        return cmd
    except Exception as exc:  # noqa: BLE001 — no seed: the agent starts from the input, said
        logger.info("input_fidelity: the fixer did not seed the target", exc_info=True)
        seed["why"] = f"{type(exc).__name__}: {str(exc)[:200]}"
        return None
    finally:
        c["auto_fix_seed"] = seed
        shutil.rmtree(root / ".boss-validation", ignore_errors=True)


ORIGINAL_COMMIT_PREFIX = "Original file as given"


def commit_original(github: Any, branch: str, base_branch: Optional[str], c: dict) -> Optional[str]:
    """THE ORIGINAL FILE IS THE CHANGE'S FIRST COMMIT ("fix a given file"): the input's bytes,
    as given, written to the target path on the work branch BEFORE any fix. The change's own
    history then reads original → fixed, and "Revert to original" is a plain file revert.

    Only when the target is another path than the input, the build does not continue on the
    branch itself (a repair or retry clones ``branch``: its tree's target is the earlier fix),
    and the branch does not yet carry its own version of the target — judged against the BASE
    branch's target, never the cloned tree's. Returns the commit sha, recorded as
    c["original_commit"]; None when nothing was committed. Never raises."""
    out, inp = str(c.get("output") or ""), str(c.get("input") or "")
    text = c.get("_input_text")
    # START FROM A FILE (start_from_file): the original is fixed IN PLACE at its target, so the
    # target may equal the input; it is committed unless the target already holds it (a repo
    # file fixed in place) or the branch holds anything but the base's version (a follow-up).
    in_place = bool(c.get("in_place")) and c.get("source") != "repo_check"
    if not out or (out == inp and not in_place) or not isinstance(text, str) or not branch \
            or branch == base_branch:
        return None
    try:
        read = getattr(github, "read_text_file", None)

        def _at(ref: Optional[str]) -> Optional[str]:
            try:
                return read(ref, out) if callable(read) and ref else None
            except Exception:  # noqa: BLE001 — no branch / no file: nothing there
                return None

        on_branch = _at(branch)
        on_base = _at(base_branch)
        if in_place and (on_branch if on_branch is not None else on_base) == text:
            return None
        if on_branch is not None and on_branch != on_base:
            return None
        github.ensure_branch(branch, base_branch)
        from .software_factory import commit_change

        shas = commit_change(github, branch, [(out, text)], f"{ORIGINAL_COMMIT_PREFIX}: {inp} → {out}")
        sha = next((s for s in shas if s), None)
        if sha:
            c["original_commit"] = sha
        return sha
    except Exception:  # noqa: BLE001 — the build goes on from base; the diff is then whole-file
        logger.info("input_fidelity: the original file was not committed first", exc_info=True)
        return None


def _hint_of(rec: Mapping[str, Any]) -> list[dict]:
    return [{"id": f"V-{n:02d}", "line": h.get("line"), "fix": h.get("fix"),
             "rule_id": h.get("rule")} for n, h in enumerate(rec.get("_hints") or [], start=1)]


def refused_text(base_text: str, out_text: str, refuse: set) -> str:
    """The target with the refused changes undone against ``base_text`` (what the agent started
    from): a removed line put back, an added line dropped, every other change kept. ``refuse``
    holds ``(hunk_id, k)`` pairs of ``validators.hunks(base_text, out_text)``."""
    from . import validators as _v

    b = out_text.splitlines()
    res: list[str] = []
    pos = 0
    for h in _v.hunks(base_text, out_text):
        res += b[pos:int(h["j1"])]
        for k, old, new in _v._pairs(h):
            if (h["id"], k) in refuse:
                if old is not None:
                    res.append(old)
            elif new is not None:
                res.append(new)
        pos = int(h["j2"])
    res += b[pos:]
    return "\n".join(res) + ("\n" if out_text.endswith("\n") and res else "")


def enforce_autofix(root: Any, c: Mapping[str, Any], rec: dict,
                    run: Optional[Callable[[str], Any]], *,
                    ruleset: Optional[Mapping[str, Any]] = None) -> dict:
    """THE RULESET'S AUTOFIX POLICY, IN CODE (SPEC §4): a rule whose ``autofix`` is ``human`` or
    ``never`` is not the agent's to fix. Every change the agent made to such a finding's lines —
    its BLOCK (a route `- path:` and its keys; a key's own line), or a line attributed to it — is
    REFUSED: undone on the target against what the agent started from (the mechanical fixer's
    output when BOSS seeded it, else the partner's file), the finding left open as "Needs you"
    with the agent's change kept as ``proposed`` (a person may accept it: finding_overrides
    applies it), and the validator run again on the reset file. ``refused`` lists them.
    Returns the record (unchanged when nothing is refused). Never raises."""
    from . import validators as _v

    try:
        rules = ((ruleset or c.get("ruleset") or {}).get("rules")) or {}
        held = {f["id"]: f for f in rec.get("findings") or []
                if (f.get("source") or {}).get("kind") == "validator" and not f.get("introduced")
                and (rules.get((f.get("source") or {}).get("rule_id")) or {}).get("autofix")
                in _v.HUMAN_AUTOFIX}
        out_path = Path(root) / str(c["output"])
        out_text = _read(out_path)
        if not held or out_text is None:
            return rec
        input_text = str(c.get("_input_text") or "")
        seeded = c.get("_auto_seeded") is not None and rec.get("_auto_text") is not None
        # WHAT THIS BUILD'S AGENT STARTED FROM: a later build on the change (repair, follow-up,
        # retry) starts from the branch's target, which already carries earlier fixes and a
        # person's decisions; only the agent's own changes since are judged.
        start = c.get("_start_text")
        base = (str(start) if isinstance(start, str)
                else str(rec["_auto_text"]) if seeded else input_text)
        base_lines = base.splitlines()
        lm = _v.line_map(input_text, base)
        hint = [{**h, "line": lm.get(h["line"]) if isinstance(h.get("line"), int) else None}
                for h in _hint_of(rec)]
        blocks = {}
        for fid, f in held.items():
            at = lm.get((f.get("location") or {}).get("partner_line"))
            if at:
                blocks[fid] = _v.block(base_lines, at - 1)
        refuse: set = set()
        mine: list[dict] = []
        for h in _v.hunks(base, out_text):
            prev: Optional[str] = None
            hunk_owner: Optional[str] = None
            for k, old, new in _v._pairs(h):
                fid = _v.attribute(h, old if old is not None else new or "", hint, prev,
                                   removed=old is not None and new is None)
                prev = fid or prev
                at = int(h["i1"]) + k if old is not None else None
                owner = next((x for x, (lo, hi) in blocks.items()
                              if (at is not None and lo <= at < hi)
                              or (at is None and lo < int(h["i1"]) < hi)), None)
                owner = owner or (fid if fid in held else None) or (
                    hunk_owner if old is None else None)
                if owner:
                    hunk_owner = owner
                    refuse.add((h["id"], k))
                    mine.append({"finding": owner, "hunk": h["id"], "i1": int(h["i1"]),
                                 "old": old, "new": new})
        if not mine:
            return rec
        out_path.write_text(refused_text(base, out_text, refuse))
        proposals: dict[str, dict] = {}
        for fid in dict.fromkeys(x["finding"] for x in mine):
            pieces = []
            for hid in dict.fromkeys(x["hunk"] for x in mine if x["finding"] == fid):
                g = [x for x in mine if x["finding"] == fid and x["hunk"] == hid]
                i1 = g[0]["i1"]
                pieces.append({"remove": [_v.mask(x["old"]) for x in g if x["old"] is not None],
                               "after": [_v.mask(x["new"]) for x in g if x["new"] is not None],
                               "anchor": _v.mask(base_lines[i1 - 1]) if i1 > 0 else None})
            news = [x["new"] for x in mine if x["finding"] == fid and x["new"] is not None]
            proposals[fid] = {
                "before": _v._joined(x["old"] for x in mine if x["finding"] == fid),
                "after": _v._joined(news), "pieces": pieces,
                # A proposal is re-applied from its stored (masked) lines, so only when masking
                # left them as they were; else the person writes the line (Edit).
                "applicable": all(_v.mask(x) == x for x in news)}
        new_rec = run_validation(root, c, run, ruleset=ruleset)
        for f in new_rec.get("findings") or []:
            if f["id"] in proposals:
                f["proposed"] = proposals[f["id"]]
                if f.get("status") == "open":
                    f["fix_kind"] = "human"
        new_rec["refused"] = [
            {"finding": fid, "rule": held[fid]["source"]["rule_id"],
             "autofix": (rules.get(held[fid]["source"]["rule_id"]) or {}).get("autofix"),
             "lines": sum(1 for x in mine if x["finding"] == fid)} for fid in proposals]
        return new_rec
    except Exception:  # noqa: BLE001 — an enforcement that could not run is said on the record
        logger.info("input_fidelity: autofix policy not enforced", exc_info=True)
        rec["refused_error"] = "the autofix policy could not be enforced on this tree"
        return rec


# ── start from a file: the original is the change's first commit ──────────────────────────────

def place_original(root: Any, c: Mapping[str, Any],
                   base_text: Optional[str]) -> Optional[str]:
    """START FROM A FILE (start_from_file): put the original at the target IN THE BUILD'S TREE,
    so the agents and the fixer start from it (``commit_original`` makes it the change's first
    commit on the branch). Returns the text placed, or None when there is nothing to place: not
    a start-from contract, the original already is the target's text (a repo file fixed in
    place), or the target no longer holds the base's version (a follow-up build on a change that
    already carries the original and fixes). ``base_text`` is the target on the base branch
    (None when it does not exist there)."""
    if not c or not c.get("in_place") or c.get("source") == "repo_check":
        return None
    root = Path(root)
    target = root / str(c["output"])
    now = _read(target)
    original = str(c.get("_input_text") or "")
    if now == original or now != base_text:
        return None
    try:
        _scratch_write(root, target, original)
    except OSError:
        logger.info("input_fidelity: the original could not be placed at %s", c["output"],
                    exc_info=True)
        return None
    return original


# ── checks from the repo, in any workflow ──────────────────────────────────────────────────────

#: The most touched files one build checks (each runs the validator; a bounded cost).
REPO_CHECK_FILES = 20
#: The wall-clock budget for ALL of one build's repo checks, and one command's ceiling within it:
#: a slow or hanging checker in a misconfigured validators.yaml never holds a build for long.
REPO_CHECK_BUDGET_S = 300
REPO_CHECK_COMMAND_S = 120


def repo_checks(root: Any, touched: Iterable[str], run: Optional[Callable[[str], Any]], *,
                trusted: Optional[Callable[[str], Optional[str]]] = None,
                base_ref: Optional[str] = None,
                budget_s: float = REPO_CHECK_BUDGET_S) -> list[dict]:
    """CHECKERS COME FROM THE REPO, IN ANY WORKFLOW: every validator the repo declares
    (`.boss/validators.yaml`, read from ``trusted`` — the base branch — when given) whose
    ``applies_to`` matches a file this change touched runs on it, and each run is one
    ``boss.validation/v1`` record, exactly as a remediation's: findings on the base version
    (before), on the changed file (after), the fixes between. A file new on this change is
    checked as it is (before = after). Tagged ``kind: repo_check``. [] when the repo declares
    no validator or none applies. ``run(command, timeout)`` gets each command's ceiling; the
    checks stop at ``budget_s`` in all (a file not checked by then is said as an error record's
    reason in the log, never a failure). Never raises."""
    import fnmatch
    import time

    from . import rulesets as _rulesets
    from . import validators as _v

    root = Path(root)
    if run is None:
        return []
    try:
        declared = _v.load(root, trusted)
    except Exception:  # noqa: BLE001 — a broken declaration is no check, said in the log
        logger.info("input_fidelity: %s not read for repo checks", _v.FILE, exc_info=True)
        return []
    paths = [_clean_path(p) for p in touched if p]
    out: list[dict] = []
    deadline = time.monotonic() + max(0.0, float(budget_s))

    def _bounded(cmd: str) -> Any:
        left = deadline - time.monotonic()
        if left <= 1:
            raise TimeoutError("the repo checks' time budget is spent")
        return run(cmd, int(min(REPO_CHECK_COMMAND_S, left)))

    for v in declared:
        for path in paths:
            if len(out) >= REPO_CHECK_FILES:
                return out
            if time.monotonic() >= deadline:
                logger.info("input_fidelity: repo checks stopped at the %ss budget", budget_s)
                return out
            if not any(fnmatch.fnmatch(path, g) for g in v["applies_to"]):
                continue
            now = _read(root / path)
            if now is None:         # deleted on this change: nothing to check
                continue
            base = trusted(path) if trusted is not None else None
            c: dict = {"input": path, "output": path, "answer_files": [], "source": "repo_check",
                       "in_place": True, "from": "base" if base is not None else "new",
                       "validator": {"name": v["id"], "check": v["run"], "fix": v["fix"],
                                     "adapter": v["adapter"], "rules_doc": v["rules_doc"],
                                     "ruleset": v["ruleset"], "source": _v.FILE},
                       "_input_text": base if base is not None else now,
                       "_output_base": None, "_answer_texts": {}}
            if base is not None and base_ref:
                c["input_ref"] = base_ref
            if v["ruleset"]:
                try:
                    c["ruleset"] = _rulesets.load(root, v["ruleset"], read=trusted)
                except Exception:  # noqa: BLE001 — the tool's own severities, said by `basis`
                    logger.info("input_fidelity: ruleset %s not read", v["ruleset"],
                                exc_info=True)
            rec = public_validation(run_validation(root, c, _bounded))
            rec["kind"] = "repo_check"
            out.append(rec)
    return out


def open_blocking(rec: Optional[Mapping[str, Any]]) -> int:
    """A record's open P0/P1 findings (what would block a strict merge)."""
    return sum(1 for f in (rec or {}).get("findings") or []
               if f.get("status") in ("open", "reverted") and f.get("severity") in ("P0", "P1"))


def lead_record(records: Sequence[Mapping[str, Any]]) -> Optional[dict]:
    """The record a build carries as its ``validation`` when several ran: the one with the most
    open P0/P1 findings (then the most findings), first on a tie."""
    if not records:
        return None
    return dict(max(records, key=lambda r: (open_blocking(r), len(r.get("findings") or []))))

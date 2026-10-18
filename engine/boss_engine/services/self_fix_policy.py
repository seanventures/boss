"""WHAT A BOSS SELF-FIX MAY CHANGE: protected paths and symbols, the approval tier, the flags.

BOSSAPP-571 (docs/product/BOSS_FIXES_ITSELF.md §8.1, §8.2, F16, F17). The bar: nothing a
self-fix does can widen what BOSS or its agents may do. So the paths that decide what BOSS and
its agents may do — identity, approvals and seats, secrets, policy, the installer's trust, the
sandbox boundary, the gates, and this machinery itself — are NEVER self-fixable, and the check is
MECHANICAL: a path match, not a reviewer's opinion, so no model can argue past it.

THE POLICY is `packages/specpack/policies/self_fix_paths.yaml` (itself `never`). It is read from
the RUNNING BOSS's tree, never from the candidate's: a self-fix cannot change the rule it is
judged by.

THREE KINDS OF RULE
  path glob          `engine/boss_engine/identity/**`: any change under it.
  path#group         a HUNK rule (F17): BOSS's largest modules mix gates with plumbing, and the
                     run doc's real build-side fixes all changed governed_code_task_service.py
                     without touching a gate. A hunk inside one of the group's top-level
                     symbols (Python `ast`, on the base for removed lines and on the head for
                     added lines), a protected symbol deleted or renamed, or a changed line that
                     names one of the group's protected words (`words_from`) is refused; other
                     hunks fall through to the next rule. `shell_strings` (tier 3): a hunk that
                     changes a rendered shell string BOSS sends to a host.
  flags              `host_effect`: added lines carry a destructive host command → tier 3, a
                     card badge and the security reviewer's focus. `protected_adjacent`: an
                     added import of a `never` module → badge and focus.

THE TIER (§8.2): only tier-2 paths and no flags → 2 (one person holding the tier's seat); any
tier-3 path or a host_effect flag → 3 (two people, or the single-operator waiver); any `never`
path → no tier at all (not offered: the self-fix is refused and becomes a recommendation for a
person, with the reason).
"""
from __future__ import annotations

import ast
import difflib
import functools
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

import yaml

POLICY_PATH = Path(__file__).resolve().parents[3] / "packages" / "specpack" / "policies" \
    / "self_fix_paths.yaml"

NEVER = "never"
TIER3 = "tier3"
HOST_EFFECT = "host_effect"
PROTECTED_ADJACENT = "protected_adjacent"

_SHELL = re.compile(r"\b(?:docker|compose|rm|kill|systemctl|chmod|chown|mkdir|mv|cp|curl|ssh|"
                    r"sudo|tee|sed|awk|for|do|done|case|esac|then|fi)\b|\$\(|&&|\|\||;\s|"
                    r">\s*/")
_STRING = re.compile(r"(?P<q>\"|')(?P<body>(?:\\.|(?!(?P=q)).)*)(?P=q)")
_IMPORT = re.compile(r"^\s*(?:from\s+(?P<from>[.\w]+)\s+import\s+(?P<names>[\w, ()*]+)|"
                     r"import\s+(?P<mod>[\w.]+))")


class PolicyError(RuntimeError):
    """The policy file is missing or malformed: every self-fix is refused (fail closed)."""


# ── the policy ───────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Rule:
    raw: str
    glob: str
    group: Optional[str] = None


@dataclass(frozen=True)
class Policy:
    never: tuple[Rule, ...]
    tier3: tuple[Rule, ...]
    symbols: dict[str, dict[str, dict[str, Any]]]
    host_patterns: tuple[str, ...]
    max_files: int
    max_diff_lines: int


def _rules(items: Any) -> tuple[Rule, ...]:
    out = []
    for item in items or []:
        raw = str(item).strip()
        glob, _, group = raw.partition("#")
        out.append(Rule(raw=raw, glob=glob.strip(), group=group.strip() or None))
    return tuple(out)


@functools.lru_cache(maxsize=4)
def _load(path: str, mtime: float) -> Policy:
    try:
        doc = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    except Exception as error:  # noqa: BLE001
        raise PolicyError(f"the self-fix policy {path} could not be read: {error}") from error
    if not isinstance(doc, dict) or int(doc.get("version") or 0) != 1 or not doc.get("never"):
        raise PolicyError(f"the self-fix policy {path} is not a version-1 policy with a never list")
    flags = doc.get("flags") or {}
    limits = doc.get("limits") or {}
    symbols = doc.get("symbols") or {}
    for rule in _rules(doc.get("never")) + _rules(doc.get("tier3")):
        if rule.group and rule.group not in symbols:
            raise PolicyError(f"rule {rule.raw} names symbol group {rule.group!r}, which the "
                              "policy does not define")
    return Policy(
        never=_rules(doc.get("never")), tier3=_rules(doc.get("tier3")),
        symbols={str(k): dict(v or {}) for k, v in symbols.items()},
        host_patterns=tuple(str(p) for p in ((flags.get(HOST_EFFECT) or {}).get("patterns")
                                             or [])),
        max_files=int(limits.get("max_files") or 6),
        max_diff_lines=int(limits.get("max_diff_lines") or 300))


def load(path: Optional[Path] = None) -> Policy:
    """The running BOSS's policy (fail closed: a missing or malformed file raises)."""
    target = Path(path or POLICY_PATH)
    try:
        mtime = target.stat().st_mtime
    except OSError as error:
        raise PolicyError(f"the self-fix policy {target} is missing") from error
    return _load(str(target), mtime)


@functools.lru_cache(maxsize=512)
def _glob_re(glob: str) -> re.Pattern:
    out, i = "", 0
    while i < len(glob):
        if glob.startswith("**/", i):
            out += "(?:.*/)?"
            i += 3
        elif glob.startswith("**", i):
            out += ".*"
            i += 2
        elif glob[i] == "*":
            out += "[^/]*"
            i += 1
        elif glob[i] == "?":
            out += "[^/]"
            i += 1
        else:
            out += re.escape(glob[i])
            i += 1
    return re.compile(f"^{out}$")


def norm(path: Any) -> str:
    """A repository path as the policy reads it: no leading `./` (but `.github` stays)."""
    out = str(path or "").strip().replace("\\", "/")
    while out.startswith("./"):
        out = out[2:]
    return out


def matches(glob: str, path: str) -> bool:
    return bool(_glob_re(glob).match(norm(path)))


# ── the change ───────────────────────────────────────────────────────────────────────────────

@dataclass
class FileChange:
    """One changed file between the self-fix's base and its head."""
    path: str
    status: str                       # A | M | D | R
    before: Optional[str] = None      # the base content (None: added)
    after: Optional[str] = None       # the head content (None: deleted)
    old_path: Optional[str] = None    # a rename's base path

    @property
    def paths(self) -> list[str]:
        return [p for p in dict.fromkeys([self.path, self.old_path or self.path]) if p]


def _line_changes(before: Optional[str], after: Optional[str]
                  ) -> tuple[set[int], set[int], set[int]]:
    """(base lines removed or replaced, head lines added or replaced, insertion points: a pure
    insertion after base line n), 1-based."""
    a = (before or "").splitlines()
    b = (after or "").splitlines()
    removed, added, inserted_after = set(), set(), set()
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(a=a, b=b, autojunk=False).get_opcodes():
        if tag in ("replace", "delete"):
            removed.update(range(i1 + 1, i2 + 1))
        if tag in ("replace", "insert"):
            added.update(range(j1 + 1, j2 + 1))
        if tag == "insert":
            inserted_after.add(i1)
    return removed, added, inserted_after


def changed_lines(before: Optional[str], after: Optional[str]) -> tuple[set[int], set[int]]:
    """(base line numbers removed or replaced, head line numbers added or replaced), 1-based."""
    removed, added, _ = _line_changes(before, after)
    return removed, added


def diff_size(changes: Iterable[FileChange]) -> int:
    total = 0
    for c in changes:
        removed, added = changed_lines(c.before, c.after)
        total += len(removed) + len(added)
    return total


# ── symbols (F17) ────────────────────────────────────────────────────────────────────────────

def symbol_spans(source: Optional[str]) -> dict[str, tuple[int, int]]:
    """{name: (first line, last line)} of every top-level symbol (functions, classes, their
    methods as `Class.method`, assignments), decorators included. Unparseable: {}."""
    if not source:
        return {}
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return {}
    spans: dict[str, tuple[int, int]] = {}

    def start(node: ast.AST) -> int:
        decos = getattr(node, "decorator_list", None) or []
        return min([node.lineno] + [d.lineno for d in decos])

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            spans[node.name] = (start(node), node.end_lineno or node.lineno)
            if isinstance(node, ast.ClassDef):
                for inner in node.body:
                    if isinstance(inner, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        spans[f"{node.name}.{inner.name}"] = (start(inner),
                                                              inner.end_lineno or inner.lineno)
        elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for t in targets:
                if isinstance(t, ast.Name):
                    spans[t.id] = (node.lineno, node.end_lineno or node.lineno)
    return spans


def symbol_words(source: Optional[str], names: Iterable[str]) -> set[str]:
    """Every string constant held by the named top-level symbols (the gate names a frozenset of
    gate names defines), so a changed line naming one of them is seen."""
    if not source:
        return set()
    wanted = set(names)
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    words: set[str] = set()
    for node in tree.body:
        targets: list[str] = []
        if isinstance(node, ast.Assign):
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            targets = [node.target.id]
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            targets = [node.name]
        if not wanted.intersection(targets):
            continue
        for sub in ast.walk(node):
            if isinstance(sub, ast.Constant) and isinstance(sub.value, str) \
                    and re.fullmatch(r"[a-z][a-z0-9_]{3,}", sub.value):
                words.add(sub.value)
    return words


def _hits_span(lines: set[int], span: tuple[int, int]) -> bool:
    return any(span[0] <= n <= span[1] for n in lines)


def _group_hit(change: FileChange, spec: dict[str, Any]) -> Optional[str]:
    """Why a hunk of `change` falls under a symbol group's rule, or None."""
    removed, added, inserted_after = _line_changes(change.before, change.after)
    base_spans = symbol_spans(change.before)
    head_spans = symbol_spans(change.after)
    for name in spec.get("symbols") or []:
        name = str(name)
        span = base_spans.get(name)
        if span and name not in head_spans:
            return f"removes or renames {name}"
        if span and (_hits_span(removed, span)
                     or any(span[0] <= n < span[1] for n in inserted_after)):
            return f"changes {name}"
        if name in head_spans and _hits_span(added, head_spans[name]):
            return f"changes {name}"
    words = symbol_words(change.before, [str(n) for n in spec.get("words_from") or []])
    if words:
        before = (change.before or "").splitlines()
        after = (change.after or "").splitlines()
        touched = [before[n - 1] for n in sorted(removed) if 0 < n <= len(before)] + \
                  [after[n - 1] for n in sorted(added) if 0 < n <= len(after)]
        for line in touched:
            for word in words:
                if f'"{word}"' in line or f"'{word}'" in line:
                    return f"changes a line naming the gate {word!r}"
    if spec.get("shell_strings"):
        before = (change.before or "").splitlines()
        after = (change.after or "").splitlines()
        touched = [before[n - 1] for n in sorted(removed) if 0 < n <= len(before)] + \
                  [after[n - 1] for n in sorted(added) if 0 < n <= len(after)]
        for line in touched:
            if is_shell_line(line):
                return "changes a shell command BOSS runs on a host"
    return None


def is_shell_line(line: str) -> bool:
    """A line holding a string that reads like a host command."""
    for m in _STRING.finditer(line):
        if _SHELL.search(m.group("body")):
            return True
    return bool(re.search(r"\$\(|&&|\bdocker\s", line))


# ── the verdict ──────────────────────────────────────────────────────────────────────────────

@dataclass
class PathVerdict:
    tier: Optional[int]
    never: list[dict[str, str]] = field(default_factory=list)
    tier3: list[dict[str, str]] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    flag_detail: dict[str, list[str]] = field(default_factory=dict)
    paths: list[dict[str, Any]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.never

    def reason(self) -> str:
        if not self.never:
            return ""
        first = self.never[0]
        return (f"{first['path']} {first['why']} — {area_words(first['path'])}, which BOSS never "
                "changes by itself")

    def as_dict(self) -> dict[str, Any]:
        return {"tier": self.tier, "never": self.never, "tier3": self.tier3,
                "flags": self.flags, "flag_detail": self.flag_detail, "paths": self.paths,
                "ok": self.ok}


def _classify_one(change: FileChange, policy: Policy) -> tuple[str, str, str]:
    """(never|tier3|tier2, the rule, why) for one changed file."""
    for path in change.paths:
        for rule in policy.never:
            if not matches(rule.glob, path):
                continue
            if rule.group is None:
                return NEVER, rule.raw, "is protected"
            spec = (policy.symbols.get(rule.group) or {}).get(rule.glob) or {}
            hit = _group_hit(change, spec)
            if hit:
                return NEVER, rule.raw, hit
    for path in change.paths:
        for rule in policy.tier3:
            if not matches(rule.glob, path):
                continue
            if rule.group is None:
                return TIER3, rule.raw, "is BOSS's pipeline or park/resume plumbing"
            spec = (policy.symbols.get(rule.group) or {}).get(rule.glob) or {}
            hit = _group_hit(change, spec)
            if hit:
                return TIER3, rule.raw, hit
    return "tier2", "", ""


def _module_paths(importer: str, module: str, names: str) -> list[str]:
    """The repository paths an import statement may load (relative imports resolved against the
    importer's package)."""
    if module.startswith("."):
        dots = len(module) - len(module.lstrip("."))
        package = Path(importer).parent
        for _ in range(dots - 1):
            package = package.parent
        rest = module.lstrip(".")
        base = package / rest.replace(".", "/") if rest else package
    else:
        root = module.split(".")[0]
        if root not in ("boss_engine", "apps"):
            return []
        base = Path("engine") / module.replace(".", "/") if root == "boss_engine" \
            else Path(module.replace(".", "/"))
    out = [f"{base}.py", f"{base}/__init__.py"]
    for name in re.split(r"[\s,()]+", names or ""):
        if name and name != "*":
            out.append(f"{base}/{name}.py")
    return [p.replace("\\", "/") for p in out]


def adjacent_imports(change: FileChange, policy: Policy) -> list[str]:
    """`never` modules the head of `change` newly imports."""
    if not change.after or not change.path.endswith(".py"):
        return []
    _, added = changed_lines(change.before, change.after)
    lines = change.after.splitlines()
    hits = []
    for n in sorted(added):
        m = _IMPORT.match(lines[n - 1]) if 0 < n <= len(lines) else None
        if not m:
            continue
        module = m.group("from") or m.group("mod") or ""
        for candidate in _module_paths(change.path, module, m.group("names") or ""):
            if any(r.group is None and matches(r.glob, candidate) for r in policy.never):
                hits.append(candidate)
                break
    return sorted(set(hits))


def host_effects(change: FileChange, policy: Policy) -> list[str]:
    """Destructive host-command patterns on the head's added lines (never in a test file)."""
    if not change.after or is_test_path(change.path):
        return []
    _, added = changed_lines(change.before, change.after)
    lines = change.after.splitlines()
    found = []
    for n in sorted(added):
        line = lines[n - 1] if 0 < n <= len(lines) else ""
        for pattern in policy.host_patterns:
            if pattern in line:
                found.append(pattern)
    return sorted(set(found))


def classify(changes: Iterable[FileChange], policy: Optional[Policy] = None) -> PathVerdict:
    """THE `protected_paths` GATE: every changed path against the policy; the highest tier
    decides the approval tier; any `never` refuses."""
    policy = policy or load()
    changes = list(changes)
    verdict = PathVerdict(tier=2)
    for change in changes:
        kind, rule, why = _classify_one(change, policy)
        verdict.paths.append({"path": change.path, "status": change.status,
                              "tier": None if kind == NEVER else (3 if kind == TIER3 else 2),
                              "rule": rule, "why": why,
                              **({"old_path": change.old_path} if change.old_path else {})})
        if kind == NEVER:
            verdict.never.append({"path": change.path, "rule": rule, "why": why})
        elif kind == TIER3:
            verdict.tier3.append({"path": change.path, "rule": rule, "why": why})
        effects = host_effects(change, policy)
        if effects:
            verdict.flag_detail.setdefault(HOST_EFFECT, []).extend(
                f"{change.path}: {e}" for e in effects)
        adjacent = adjacent_imports(change, policy)
        if adjacent:
            verdict.flag_detail.setdefault(PROTECTED_ADJACENT, []).extend(
                f"{change.path} imports {a}" for a in adjacent)
    verdict.flags = sorted(verdict.flag_detail)
    if verdict.never:
        verdict.tier = None
    elif verdict.tier3 or HOST_EFFECT in verdict.flags:
        verdict.tier = 3
    return verdict


def diagnosis_refusal(paths: Iterable[str], functions: Iterable[str] = (),
                      policy: Optional[Policy] = None) -> Optional[str]:
    """Stage 3: a diagnosis that names a protected path (or a protected symbol of a hunk-rule
    file) stops the self-fix BEFORE any code is written. The reason, or None."""
    policy = policy or load()
    named = set(str(f).split(".")[-1] for f in functions if f)
    for raw in paths:
        path = norm(str(raw or "").split("::")[0].split("#")[0])
        if not path:
            continue
        for rule in policy.never:
            if not matches(rule.glob, path):
                continue
            if rule.group is None:
                return (f"the diagnosis names {path}: {area_words(path)}, which BOSS never "
                        "changes by itself")
            spec = (policy.symbols.get(rule.group) or {}).get(rule.glob) or {}
            protected = {str(s).split(".")[-1] for s in spec.get("symbols") or []}
            hit = named & protected
            if hit:
                return (f"the diagnosis names {path}#{sorted(hit)[0]}: BOSS's quality gates, "
                        "which BOSS never changes by itself")
    return None


def protected_symbols(policy: Optional[Policy] = None) -> list[tuple[str, str]]:
    """(path, symbol) for every symbol rule: the P0 test asserts each still exists."""
    policy = policy or load()
    out = []
    for group in policy.symbols.values():
        for path, spec in group.items():
            for name in list(spec.get("symbols") or []) + list(spec.get("words_from") or []):
                out.append((path, str(name)))
    return sorted(set(out))


# ── words ────────────────────────────────────────────────────────────────────────────────────

_AREA_WORDS = (
    ("engine/boss_engine/identity/", "its identity, sign-in, approvals and seats code"),
    ("engine/boss_engine/services/decision", "its approvals (decisions) code"),
    ("engine/boss_engine/services/self_fix", "its own self-fix machinery"),
    ("engine/boss_engine/services/step_recovery", "the recovery agent that decides what is "
                                                  "BOSS's bug"),
    ("engine/boss_engine/services/sandbox", "the sandbox boundary"),
    ("engine/boss_engine/sandbox", "the sandbox boundary"),
    ("engine/boss_engine/services/budget", "its budget controls"),
    ("packages/specpack/policies/", "its safety policy"),
    ("packages/identity/", "its permission catalogue"),
    ("scripts/install/", "its installer and upgrade"),
    ("db/", "its database schema"),
    (".github/", "its CI"),
)


def area_words(path: str) -> str:
    for prefix, words in _AREA_WORDS:
        if path.startswith(prefix):
            return words
    if is_test_path(path) or path.endswith(("conftest.py", "tiers.yaml", "boss.verify.yaml")):
        return "its test configuration"
    if re.search(r"requirements|package\.json|lock|Dockerfile|compose", path):
        return "its dependencies or images"
    return "a protected part of BOSS"


_TEST_GLOBS = ("engine/tests/**", "apps/*/tests/**", "tests/**", "**/conftest.py",
               "**/fixtures/**", "**/__snapshots__/**", "**/*.test.*", "**/*.spec.*",
               "**/test_*.py")


def is_test_path(path: str) -> bool:
    return any(matches(g, path) for g in _TEST_GLOBS)


_AREAS = (
    (r"^apps/web/", "console"), (r"^scripts/install/|^install\.sh", "install"),
    (r"preview", "preview"), (r"deploy|stack_|resource_plane|app_start", "deploy"),
    (r"review|security_check|consistency", "review"),
    (r"code_task|verification_target|software_factory|build", "build"),
    (r"workflow", "workflow"),
)


def area_of(paths: Iterable[str]) -> str:
    """The PRD's area labels (deploy|build|preview|review|workflow|console|install), from the
    first non-test path that names one."""
    for path in paths:
        if is_test_path(path):
            continue
        for pattern, area in _AREAS:
            if re.search(pattern, path):
                return area
    return "engine"


__all__ = ["FileChange", "HOST_EFFECT", "NEVER", "PROTECTED_ADJACENT", "PathVerdict", "Policy",
           "PolicyError", "TIER3", "adjacent_imports", "area_of", "area_words", "changed_lines",
           "classify", "diagnosis_refusal", "diff_size", "host_effects", "is_shell_line",
           "is_test_path", "load", "matches", "norm", "protected_symbols", "symbol_spans",
           "symbol_words"]

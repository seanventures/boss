"""THE VERDICT (AA-F, BOSSAPP-1220; boss-acceptance-agent-lld.md §4.5): one module that reads an
acceptance reply and decides, item by item, what was proven — for BOTH layers.

  inner layer  the in-build `acceptance_check` seat (acceptance_gate.run_check): static, file
               evidence only. Its behaviour is exactly #136/#143's: these functions moved here
               unchanged from acceptance_gate.py, which re-exports them under the same names.
  outer layer  the Acceptance Agent (acceptance/session.py): it RUNS the work, and every command
               it ran is in a command record BOSS kept (session.CommandRecord). Its MET lines
               cite commands (`E3 (exit 0) "HTTP 401"`), and BOSS — never the model — checks
               each one against that record (`verify_command`): the command exists, the quoted
               words are in its output, its exit (or HTTP status) is the one claimed. A MET whose
               evidence BOSS cannot verify is NOT JUDGED, never met (I2, I6).

Pass `exec_log` (a CommandRecord, or anything with `.get(id)`) to judge the outer layer; without
it every function behaves as the inner gate always did.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

from . import contract as _contract
from . import dependencies as _deps
from .contract import MAX_TOTAL_ITEMS, _norm, item_key


#: Items per reviewer pass: a longer list is judged in several passes (`run_check`), never cut.
MAX_ITEMS = 40
#: Reviewer passes per check (BOSSAPP-1233): items beyond MAX_PASSES x MAX_ITEMS are not judged,
#: and a check with items it did not judge is not met (the card says how many).
MAX_PASSES = 4

MET, PARTIAL, MISSING, UNJUDGED = "met", "partial", "missing", "unjudged"
#: The outer layer's fifth answer: a machine cannot judge it. Never met (AA-F, LLD §4.5).
HUMAN = "human"

_SKIP_DIRS = frozenset({".git", "node_modules", "vendor", ".venv", "venv", "dist", "build",
                        "__pycache__", ".next", "target", ".pytest_cache", ".mypy_cache"})


def _walk(root: Path, max_depth: int = 6) -> Iterable[Path]:
    root = Path(root)
    if not root.is_dir():
        return
    for dirpath, dirnames, filenames in os.walk(root):
        rel_depth = len(Path(dirpath).relative_to(root).parts)
        dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS and rel_depth < max_depth)
        for f in sorted(filenames):
            yield Path(dirpath) / f


_ANSWER = re.compile(
    r"(?im)^[\s>*\-•]*\**\s*ITEM\s*#?\s*(\d+)\s*\**\s*[:.)\-]\s*\**\s*(MET|PARTIAL|MISSING)\b\**"
    r"(\s*\(\s*judge?ment\s*\))?\**\s*(?:[—–:\-]+\s*)?(.*)$")
_SUMMARY = re.compile(r"(?im)^[\s>*`]*ACCEPTANCE\s*:\s*\**\s*(NOT\s+MET|MET)\b")
_PATHISH = re.compile(r"(?<![\w/.-])([\w.\-]+(?:/[\w.\-]+)+|[\w\-]+\.[A-Za-z][\w]{0,8})"
                      r"(?::\d+)?(?![\w/])")
#: A file name with more than one dot (`boss.deploy.yaml`, `docker-compose.override.yml`,
#: `jest.config.js`): `_PATHISH` reads only its first two parts (BOSSAPP-1333: "only documents
#: cited" when the answer named README.md AND boss.deploy.yaml). Evidence only when the tree
#: has the file; otherwise the shorter reading stands, so `cfg.Routes.Match` is unchanged.
_MULTIDOT = re.compile(r"(?<![\w/.-])([\w\-]+(?:\.[\w\-]+)+\.[A-Za-z]\w{0,8})(?::\d+(?:-\d+)?)?"
                       r"(?![\w/])")
#: "same as ITEM 1", "see AC-3", "as in item 2": an answer that points at another item's.
_SAME_AS = re.compile(r"(?i)\b(?:same(?:\s+(?:evidence|code|as\s+for))?\s+as|see(?:\s+also)?|"
                      r"as\s+(?:in|for|with)|per|cf\.?|identical\s+to)\s+(?:ITEM\s*#?\s*|AC-)(\d+)\b")
#: A bare word that may be an extensionless file (Makefile, Dockerfile, LICENSE, Procfile): it
#: counts as evidence only when a file of exactly that name is in the tree.
_BARE = re.compile(r"(?<![\w/.\-])([A-Za-z][\w\-]{2,})(?![\w/\-]|\.\w)")
_DOC_PATH = re.compile(r"(?i)(^|/)(readme[^/]*|changelog[^/]*|docs?/.*|[^/]*\.(md|rst|txt|adoc))$")
#: Manifests and lockfiles declare a dependency; they deliver no behaviour.
_DECL_PATH = re.compile(r"(?i)(^|/)(go\.(mod|sum)|package(-lock)?\.json|[^/]*\.lock|"
                        r"pnpm-lock\.ya?ml|requirements[^/]*\.txt|pyproject\.toml|setup\.(py|cfg)|"
                        r"pipfile|cargo\.toml|pom\.xml|build\.gradle(\.kts)?|gemfile)$")
#: A test file: it exercises a loader, it is not one.
_TEST_PATH = re.compile(r"(?i)(^|/)(tests?|__tests__|spec|testdata)/|_test\.\w+$|\.(test|spec)\.\w+$"
                        r"|(^|/)test_[^/]*$")
#: The reviewer's wiring claim (acceptance_prompt): "LOADED BY <file>:<symbol>".
_LOADED_BY = re.compile(r"(?i)\bLOADED\s+BY\s+`?([\w.\-/]+?):`?([A-Za-z_][\w.$]*)`?")


def _resolve(root: Path, cited: str) -> Optional[str]:
    """The tree path a cited path names (exact, else a unique suffix match), or None."""
    cited = cited.strip("`'\",;:()[]").rstrip(".")
    while cited.startswith(("./", "/")):           # never a leading dot of a name (.github/)
        cited = cited[1:] if cited.startswith("/") else cited[2:]
    if not cited or ".." in Path(cited).parts:
        return None
    direct = root / cited
    if direct.is_file():
        return cited
    hits = [p for p in _walk(root) if str(p.relative_to(root)).endswith("/" + cited)
            or (("/" not in cited) and p.name == cited)]
    return str(hits[0].relative_to(root)) if len(hits) == 1 else None


_GO_PATTERN = re.compile(r"^\.?/?((?:[\w.\-]+/)*?)\.\.\.$")


def _has_source(folder: Path) -> bool:
    """Whether `folder` (or below it) holds a source file of any language BOSS builds."""
    if not folder.is_dir():
        return False
    return any(_CODE_FILE.search(p.name) for p in _walk(folder, max_depth=4))


def _command_target(root: Path, cited: str, before: str) -> Optional[list[str]]:
    """What a command's package or module argument names, or None when `cited` is a path:
      Go     `./...`, `./internal/config/...` → nothing (a package pattern, #171 review (d));
      Python `-m pkg.mod` → pkg/mod.py (its file), else nothing;
      Maven  `-pl module` → nothing (a module folder).
    An empty list: a command argument that is not file evidence — and not a missing path
    either. An item whose requirement IS the suite run is evidenced by the run itself
    (`_suite_item`), never by these."""
    go = _GO_PATTERN.match(cited)
    if go is not None:
        # A PACKAGE PATTERN IS NOT FILE EVIDENCE (BOSSAPP-1329 review): `./...` names the
        # whole repository and `./internal/config/...` a folder — "`go test ./...` passes" says
        # nothing about which code delivers an item. Neither evidence nor a missing file.
        return []
    flag = before.rstrip()
    if flag.endswith("-m") and re.fullmatch(r"[A-Za-z_][\w.]*", cited):
        mod = cited.replace(".", "/")
        for cand in (f"{mod}.py", f"{mod}/__init__.py", f"{mod}/__main__.py"):
            hit = _resolve(root, cand)
            if hit:
                return [hit]
        return []                       # a package folder: not file evidence
    if flag.endswith(("-pl", "--projects")):
        return []                       # a module folder: not file evidence either
    return None


#: Directories a cited path starts with that mark it as a path even when the tree lacks it.
_KNOWN_DIRS = frozenset({"cmd", "internal", "pkg", "src", "app", "apps", "lib", "test", "tests",
                         "docs", "doc", "scripts", "deploy", "config", "configs", "build", "api",
                         "web", "public", "migrations", "mocks", "testdata", "bin", "tools"})


def _path_shaped(root: Path, cited: str) -> bool:
    """Whether a slash-joined token is written as a path: a file extension on its last part, a
    leading `./` or `.dir/`, a first folder the tree has (or a conventional one), or a file
    the tree has. `Makefile/compose/handler` in a sentence ("match actual Makefile/compose/
    handler behavior", AR-31) is prose listing three things, not a path — it is never a
    citation that is absent from the tree."""
    first, last = cited.split("/", 1)[0], cited.rsplit("/", 1)[-1]
    return bool("." in last or cited.startswith((".", "/")) or first.startswith(".")
                or first.lower() in _KNOWN_DIRS or (root / first).is_dir()
                or (root / cited).exists())


def _listed_parts(root: Path, cited: str) -> list[str]:
    """`Makefile/docker-compose/mocks` written as one token: each part that is a file (by name
    or by stem, uniquely), or a folder holding code. Empty unless EVERY part is one."""
    if "/" not in cited or "." in cited.rsplit("/", 1)[-1]:
        return []
    out = []
    files = {p.name: p for p in _walk(root, max_depth=2)}
    for part in cited.split("/"):
        hit = _resolve(root, part)
        if not hit:
            stems = [n for n in files if n.split(".", 1)[0] == part]
            hit = (str(files[stems[0]].relative_to(root)) if len(stems) == 1
                   else part if _has_source(root / part) or (root / part).is_dir() else None)
        if not hit:
            return []
        out.append(hit)
    return out


def clip(text: Any, limit: int) -> str:
    """`text` within `limit` characters, never cut mid-sentence (BOSSAPP-1329): at the last
    sentence end (or `;`) inside the limit, else the last word, marked with "…". A trailing
    parenthesised reason BOSS appended — "(loader not named: …)" — is always kept whole."""
    text = str(text or "")
    if len(text) <= limit:
        return text
    reason = ""
    m = re.search(r"\s(\([^()]*(?:\([^()]*\)[^()]*)*\))\s*$", text)
    if m and len(m.group(1)) < limit // 2:
        reason, text = m.group(1), text[:m.start()]
    room = max(40, limit - len(reason) - 3)
    if len(text) > room:
        head = text[:room]
        cut = max(head.rfind(". "), head.rfind("; "), head.rfind(".\n"))
        if cut >= room // 2:
            head = head[:cut + 1]
        else:
            head = head.rsplit(" ", 1)[0]
        text = head.rstrip() + " …"
    return f"{text} {reason}".strip() if reason else text


def _extensionless(root: Path) -> dict[str, list[str]]:
    """The tree's files with no extension (Makefile, Dockerfile, LICENSE), by name."""
    out: dict[str, list[str]] = {}
    for p in _walk(root):
        if "." not in p.name:
            out.setdefault(p.name, []).append(str(p.relative_to(root)))
    return out


def evidence_paths(root: Any, why: str, *,
                   defs: Optional["_Definitions"] = None,
                   item_text: Optional[str] = None) -> tuple[list[str], list[str]]:
    """(cited paths that exist in the tree, cited paths that do not). A bare extensionless
    name (Makefile) counts when exactly one file of that name is in the tree. A cited symbol
    (`auth.VerifyJWT`, `Store.Swap`) counts as the file that defines it (_Definitions)."""
    if root is None:
        return [], []
    found, absent = [], []
    defs = defs if defs is not None else _Definitions(Path(root))
    text_ = str(why or "")
    whole: list[tuple[int, int]] = []
    for m in _MULTIDOT.finditer(text_):
        hit = _resolve(Path(root), m.group(1))
        if hit:
            found.append(hit)
            whole.append((m.start(1), m.end(1)))
    for m in _PATHISH.finditer(text_):
        cited = m.group(1)
        if any(a <= m.start(1) < b for a, b in whole):
            continue                    # part of a multi-dot name already found whole
        if re.fullmatch(r"\d+(\.\d+)*", cited):
            continue
        # NOT A FILE (BOSSAPP-1329): a package pattern or a module a command names — Go's
        # `./...` and `./internal/config/...`, Python's `-m pkg.mod`, Maven's `-pl module` —
        # is the code it names, never a missing path; a slash-joined list of names
        # ("Makefile/docker-compose/mocks") is its parts.
        named = _command_target(Path(root), cited, text_[max(0, m.start() - 8):m.start()])
        if named is not None:
            found.extend(named)
            continue
        # A path, else a symbol the way its language writes it (`auth.VerifyJWT`,
        # `Store.Swap`, `module.func`): the file that DEFINES it is the evidence (AR-23 —
        # "cited paths not in the tree: auth.VerifyJWT" marked real Go evidence partial).
        hit = _resolve(Path(root), cited) or defs.symbol_file(cited)
        if not hit and "/" in cited and not _path_shaped(Path(root), cited):
            # PROSE, NOT A PATH: each part that is a file of that name in the tree is cited
            # (the Makefile), the rest are words and are never "not in the tree".
            found.extend(_listed_parts(Path(root), cited)
                         or [h for h in (_resolve(Path(root), part) for part in cited.split("/"))
                             if h])
            continue
        if not hit:
            parts = _listed_parts(Path(root), cited)
            if parts:
                found.extend(parts)
                continue
        (found if hit else absent).append(hit or cited)
    for m in _QUALIFIED.finditer(str(why or "")):
        # A qualified name too long to look like a path (`proxy.pathMatches`,
        # `session.createSession`): evidence when the tree defines it, never "absent".
        if m.group(1) not in absent:
            hit = defs.symbol_file(m.group(1))
            if hit:
                found.append(hit)
    for m in _TICKED_IDENT.finditer(text_):
        # A NAME THE ANSWER QUOTES (`TokenBucket`, `TestStore_ConcurrentSwapAndRead_NoRace`):
        # the one file that defines it is evidence — a test included, since it proves the
        # test exists (BOSSAPP-1329).
        hit = defs.defining_file(m.group(1))
        # A TEST quoted by name evidences only an item it is about (#171 review): its name or
        # package shares a word with the item's.
        if hit and _TEST_PATH.search(hit) and item_text is not None \
                and not _words(m.group(1) + " " + Path(hit).parent.as_posix()) & _words(item_text):
            hit = None
        if hit:
            found.append(hit)
    bare = None
    for m in _BARE.finditer(str(why or "")):
        if bare is None:
            bare = _extensionless(Path(root))
        hits = bare.get(m.group(1)) or []
        if len(hits) == 1:
            found.append(hits[0])
    return list(dict.fromkeys(found)), list(dict.fromkeys(absent))


#: One pass over a source file: comments, strings and compiler directives, leftmost first, so a
#: `/*` opened mid-line, a `//` inside a string and a definition written inside a string are all
#: read the way the language reads them (AR-23 review: `x := 1 /* …` then `func Hidden()`, and
#: `"func Fake() {}"`, were taken for definitions).
_TOKEN_PARTS = (
    ("tq", r'"""[\s\S]*?(?:"""|\Z)|\'\'\'[\s\S]*?(?:\'\'\'|\Z)'),
    ("bc", r"/\*[\s\S]*?(?:\*/|\Z)"),
    ("directive", r"^[ \t]*//go:[^\n]*"),
    ("lc", r"//[^\n]*"),
    ("hc", r"\#[^\n]*"),
    ("dq", r'"(?:\\.|[^"\\\n])*"?'),
    ("sq", r"'(?:\\.|[^'\\\n])*'?"),
    ("bt", r"`[^`]*`?"),
)
_TOKENS: dict[tuple[bool, bool], "re.Pattern[str]"] = {}


def _token_re(slash: bool, hash_: bool) -> "re.Pattern[str]":
    key = (slash, hash_)
    if key not in _TOKENS:
        parts = [f"(?P<{k}>{v})" for k, v in _TOKEN_PARTS
                 if (k != "lc" or slash) and (k != "hc" or hash_)
                 and (k != "bc" or slash)]
        _TOKENS[key] = re.compile("|".join(parts), re.M)
    return _TOKENS[key]


#: Languages whose `#` starts a comment, and those whose `//` does not (Python's floor division).
_HASH_COMMENTS = frozenset({"py", "pyi", "rb", "sh", "ex", "exs", "pl", "r", ""})
_NO_SLASH_COMMENTS = frozenset({"py", "pyi", "rb", "sh", "ex", "exs", "pl", "r"})
#: Languages whose lines starting `--` or `;` are comments.
_DASH_COMMENTS = frozenset({"lua", "sql", "clj", ""})


def _ext(path: str) -> str:
    name = str(path or "").rsplit("/", 1)[-1]
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def _lex(text: str, path: str = "") -> tuple[str, str]:
    """(the code without its comments, the same with every string literal emptied). A
    triple-quoted string (a Python docstring, a text block) is documentation or a blob: it is
    dropped from both. A compiler directive written as a comment (`//go:embed rules`) is code.
    Line breaks are kept, so line numbers still hold."""
    ext = _ext(path)
    kept: list[str] = []
    bare: list[str] = []
    pos = 0
    pattern = _token_re(ext not in _NO_SLASH_COMMENTS, ext in _HASH_COMMENTS)
    for m in pattern.finditer(text):
        kind = m.lastgroup
        tok = m.group(0)
        kept.append(text[pos:m.start()])
        bare.append(text[pos:m.start()])
        pos = m.end()
        lines = "\n" * tok.count("\n")
        if kind == "directive":
            kept.append(tok)
            bare.append(tok)
        elif kind in ("dq", "sq", "bt"):
            kept.append(tok)
            bare.append(tok[0] * 2 + lines)
        else:                               # a comment or a triple-quoted string: gone
            kept.append(lines)
            bare.append(lines)
    kept.append(text[pos:])
    bare.append(text[pos:])
    out_kept, out_bare = "".join(kept), "".join(bare)
    if ext in _DASH_COMMENTS:
        drop = re.compile(r"(?m)^[ \t]*(?:--|;)[^\n]*")
        out_kept, out_bare = drop.sub("", out_kept), drop.sub("", out_bare)
    return out_kept, out_bare


def _code_lines(text: str, path: str = "") -> str:
    """The text without its comments (line, block — opened anywhere on a line — and
    triple-quoted docstrings), string literals kept: what the code says and names. A compiler
    directive written as a comment (`//go:embed rules`) is code."""
    return _lex(text, path)[0]


def _bare_code(text: str, path: str = "") -> str:
    """`_code_lines` with every string literal emptied: where a definition can be."""
    return _lex(text, path)[1]


#: Words no loader is named: the languages' own keywords (and anything shorter than 3),
#: compared as written. A plain name (`Load`, `read`, `main`) is a real function in every
#: language: whether it loads the file is what `_defines` and `_refers_to` check. Rejecting
#: `Load` here marked four MET items of the Go Northgate gateway partial (AR-23).
_KEYWORDS = frozenset("""
func function def class return var let const val if else elif for while do switch case break
continue import from package new this self true false null nil none undefined async await
export default public private protected static void final struct interface type enum try
catch except finally raise throw throws with yield lambda pass go defer select chan map range
string int bool float""".split())
#: Source files a definition can live in (a symbol citation is resolved against these only).
_CODE_FILE = re.compile(r"(?i)\.(go|py|pyi|java|kt|kts|scala|groovy|ts|tsx|js|jsx|mjs|cjs|rb|"
                        r"rs|cs|php|swift|c|cc|cpp|h|hpp|m|ex|exs|clj|dart|lua|sh)$")
#: Extensions a `name.ext` citation means as a FILE, never as `qualifier.Symbol`.
_FILE_EXTS = frozenset("""
go py pyi java kt kts scala ts tsx js jsx mjs cjs rb rs cs php swift c cc cpp h hpp md rst txt
yaml yml json toml ini cfg conf csv xml html css scss sql sh mod sum lock proto env tf hcl
gradle properties adoc lua dart ex exs clj""".split())
_IDENT = re.compile(r"[A-Za-z_$][\w$]*")
#: A qualified name (`proxy.pathMatches`, `Store.Swap`) where it is not part of a path.
_QUALIFIED = re.compile(r"(?<![\w/.\-$])([A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)+)(?![\w/$])")
#: `file:Symbol` (`internal/config/loader.go:Load`, `atomic.go:Store.Swap`): a citation of a
#: definition in a named file, whether or not the reviewer wrote LOADED BY in front of it.
_FILE_SYMBOL = re.compile(r"`?([\w.\-/]+\.[A-Za-z]\w{0,8}):`?([A-Za-z_$][\w.$]*)")
#: A bare "*.yaml" glob string: it names every YAML in the tree, never one file.
_BARE_GLOB = re.compile(r"""(["'`])\*\.[\w]+\1""")
_DATA_FILE = re.compile(r"(?i)\.(ya?ml|json|toml|ini|cfg|conf|csv|xml)$")
_STRING = re.compile(r"""(["'`])((?:(?!\1).){1,200})\1""")


def _blank_strings(code: str) -> str:
    """`code` with every string literal's contents blanked, offsets kept: calls are found in
    this and their arguments read from `code` at the same positions."""
    return _STRING.sub(lambda m: m.group(1) + " " * len(m.group(2)) + m.group(1), code)


def _defines(code: str, symbol: str) -> bool:
    """Whether `code` DEFINES `symbol` (a function, method, class, constant or variable) —
    not only calls or mentions it."""
    e = re.escape(symbol)
    return any(re.search(p, code, re.M) for p in (
        r"\b(?:func|def|function|class|fn|fun|sub|struct|interface|type)\s+"
        r"(?:\([^)]*\)\s*)?\*?" + e + r"\b",
        r"\b(?:const|let|var|val)\s+" + e + r"\b",
        r"^\s*(?:export\s+)?(?:pub\s+)?" + e + r"\s*(?::=|=(?!=)|:\s*[\w\[\]<>.,*| ]+=(?!=))",
        r"\b" + e + r"\s*:\s*(?:async\s+)?(?:function\b|\()",
        r"^\s*(?:export\s+)?(?:(?:public|private|protected|static|final|async|override|"
        r"synchronized)\s+)*(?:[\w<>\[\],.?]+\s+)?" + e + r"\s*\([^;{]*\)\s*"
        r"(?:\{|:|throws\b|->)"))


def _refers_to(code: str, data_file: str) -> bool:
    """Whether `code` names the data file: its path or file name, its folder as a path string
    (`"rules"`, `"rules/"`, `//go:embed rules`), or a glob string that matches it. A MENTION,
    not a read: the wiring check uses `_reads`."""
    import fnmatch

    path = Path(data_file)
    if data_file in code or path.name in code:
        return True
    folder = path.parent.as_posix()
    for m in _STRING.finditer(code):
        lit = m.group(2).strip().rstrip("/").lstrip("./")
        if folder != "." and (lit == folder or lit.endswith("/" + folder)):
            return True
        if "*" in lit and (fnmatch.fnmatch(data_file, lit) or fnmatch.fnmatch(path.name, lit)
                           or fnmatch.fnmatch(data_file, "*/" + lit)):
            return True
    return False


#: Code that reads a file: open / read / load / parse / decode / glob / walk / embed, in the
#: spellings of the common languages and their standard libraries.
_READ_VERB = re.compile(
    r"\b(?:open|Open\w*|fopen|read\w*|Read\w*|load\w*|Load\w*|safe_load|parse\w*|Parse\w*|"
    r"[Uu]nmarshal\w*|[Dd]ecode\w*|glob|Glob|iglob|rglob|[Ww]alk\w*|embed|require|"
    r"getResource\w*|Files\.\w+|fs\.\w+|ioutil\.\w+)\s*[\(.`\"']|//go:embed\b|"
    r"\bimport\b[^\n]*[\"'][^\"'\n]+\.(?:ya?ml|json|toml|csv)[\"']")


def _lit(value: str) -> str:
    """A string literal as a tree path: no leading `/`, `./` or `../` parts, no trailing `/`."""
    return re.sub(r"^(?:\.{1,2}/|/)+", "", value.strip()).rstrip("/")


def _suffix_of(path: str, part: str) -> bool:
    """Whether `part` is `path` or a `/`-bounded tail of it."""
    return bool(part) and (path == part or path.endswith("/" + part))


def _reads(code: str, data_file: str, code_path: str = "") -> bool:
    """Whether `code` (comments already removed, `_code_lines`) READS the data file — embeds,
    opens, loads or globs it — not only names it (AR-23 review: a lint.go that never read
    `rules/naming.yaml` was taken for its loader because routes.go had `mux.Handle("/rules")`).

      - `//go:embed` naming the file, its folder or a glob matching it (relative to the code
        file's own directory, as Go resolves it);
      - in a file that reads files at all (`_READ_VERB`): a string naming the file (its path,
        a `/`-bounded tail of it, or its name), or a glob string matching its path;
      - its FOLDER only through a glob in that same file: a folder string (`"tenants"`,
        `"config/tenants"`) and a glob string matching the file's name (`"*.yaml"`). A bare
        folder string alone (a route, a log line) is not a read."""
    import fnmatch

    path = Path(data_file)
    name, folder = path.name, path.parent.as_posix()
    here = Path(code_path).parent.as_posix() if code_path else "."
    for line in code.splitlines():
        s = line.strip()
        if not s.startswith("//go:embed"):
            continue
        for pattern in s.split()[1:]:
            pattern = pattern.strip("\"`").rstrip("/")
            full = pattern if here in ("", ".") else f"{here}/{pattern}"
            for cand in (pattern, full):
                if cand in (data_file, folder) or fnmatch.fnmatch(data_file, cand):
                    return True
    if not _READ_VERB.search(code):
        return False
    lits = [_lit(m.group(2)) for m in _STRING.finditer(code)]
    m_lits: dict[str, list[Any]] = {}
    for m in _STRING.finditer(code):
        m_lits.setdefault(_lit(m.group(2)), []).append(m)
    folder_named = False
    name_glob = False
    for lit in lits:
        if not lit:
            continue
        if "*" in lit:
            if "/" in lit and (fnmatch.fnmatch(data_file, lit)
                               or fnmatch.fnmatch(data_file, "*/" + lit)):
                return True
            if "/" not in lit and fnmatch.fnmatch(name, lit):
                name_glob = True
            continue
        if _suffix_of(data_file, lit) and lit.endswith(name):
            return True
        # THE WHOLE FOLDER (BOSSAPP-1329 review): `"tenants"` alone is any tenants folder (a
        # cache dir); only "config/tenants" — written, or a join BOSS resolved — is this one.
        if folder != "." and lit == folder:
            folder_named = True
            for m in m_lits.get(lit, []):
                # The extension filter is the READ'S OWN (#171 review (b)): strings of the
                # function around the folder read, never a hint elsewhere in the file.
                scope = _scope_at(code, m.start())
                if _folder_read(code, [m]) and _pattern_admits(
                        [_lit(x.group(2)) for x in _STRING.finditer(scope)], name):
                    return True
    return folder_named and name_glob


_DEF_LINE = re.compile(r"^(\s*)(?:(?:export\s+)?(?:async\s+)?(?:func|def|function|fn|fun)\b"
                       r"|(?:public|private|protected|static)\b[^=;]*\()")


def _scope_at(code: str, pos: int) -> str:
    """The function around position `pos`: from the nearest definition line above it to the
    next definition at the same or a shallower indent (the whole text when there is none)."""
    lines = code.splitlines(keepends=True)
    offsets, at = [], 0
    for line in lines:
        offsets.append(at)
        at += len(line)
    idx = max(i for i, o in enumerate(offsets) if o <= pos) if offsets else 0
    start = next((i for i in range(idx, -1, -1) if _DEF_LINE.match(lines[i])), None)
    if start is None:
        return code
    indent = len(_DEF_LINE.match(lines[start]).group(1))
    end = next((j for j in range(start + 1, len(lines))
                if _DEF_LINE.match(lines[j])
                and len(_DEF_LINE.match(lines[j]).group(1)) <= indent), len(lines))
    return "".join(lines[start:end])


def _pattern_admits(lits: list[str], name: str) -> bool:
    """Whether a folder read takes the item's file: the code's extension filter (a `".yaml"`
    suffix, a `"*.yaml"` glob) admits the file's name, or the code filters by no extension at
    all (every file of the folder is read)."""
    import fnmatch

    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    family = {"yaml": {"yaml", "yml"}, "yml": {"yaml", "yml"}}.get(ext, {ext})
    filters = [lit for lit in lits if re.fullmatch(r"\*?\.[A-Za-z0-9]{1,8}", lit)]
    if not filters:
        return True
    return any(lit.lstrip("*.").lower() in family or fnmatch.fnmatch(name, lit) for lit in filters)


def _folder_read(code: str, matches: list[Any]) -> bool:
    """Whether a folder string is itself READ: it sits inside a read call
    (`os.ReadDir("rules")`), or it is assigned to a name a read call takes
    (`const rulesDir = "rules"` … `l.read(rulesDir)`). A folder string anywhere else (a route,
    a log line, `mux.Handle("/rules", …)`) is not."""
    for m in matches:
        line_start = code.rfind("\n", 0, m.start()) + 1
        line_end = code.find("\n", m.end())
        line = code[line_start:line_end if line_end >= 0 else len(code)]
        before = code[line_start:m.start()]
        if re.search(r"\b" + _READ_FN + r"\s*\([^)\n]*$", before):
            return True
        # Bound to a name (`rulesDir = "rules"`; a join BOSS resolved, `_bind_joins`, reads
        # the same: `tenantsDir := "config/tenants"`).
        bound = (re.search(r"(?:\b(?:const|var|let|val)\s+)?([A-Za-z_]\w*)\s*(?::=|=)\s*$",
                           before)
                 or re.search(r"\b([A-Za-z_]\w*)\s*=\s*$", before))
        if bound and line:
            name = re.escape(bound.group(1))
            if re.search(r"\b" + _READ_FN + r"\s*\([^)\n]*\b" + name + r"\b", code):
                return True
    return False


#: The functions that read a file or a folder, as a call (`os.ReadDir(`, `l.read(`, `glob(`).
_READ_FN = (r"(?:open|Open\w*|read\w*|Read\w*|load\w*|Load\w*|[Ww]alk\w*|glob|Glob|iglob|"
            r"rglob|listdir|scandir|readdirSync|readdir)")


def _reads_data(code: str) -> bool:
    """Whether `code` reads SOME data or config file (`_reads` for no file in particular):
    an embed, or a data-file string or data-file glob in code that reads files."""
    if re.search(r"(?m)^\s*//go:embed\s+\S", code):
        return True
    if not _READ_VERB.search(code):
        return False
    return any(_DATA_FILE.search(_lit(m.group(2)).replace("*", "x"))
               for m in _STRING.finditer(code))


class _Definitions:
    """The tree's code files, read once per check, and where a symbol is DEFINED.

    A cited symbol is resolved the way its language spells it:
      Go       `auth.VerifyJWT` (package dir `auth`), `Store.Swap` (method on a type),
               unexported `proxy.pathMatches`
      Python   `module.func`, `Class.method` (file stem `module`, or the file defining Class)
      Java/TS  `Class.method` (the file defining the class, or named after it)
    The qualifier narrows the files (a path part or file stem named after it, or a file that
    defines it); the last part must be defined there, outside comments and strings, in non-test
    code."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self._code: Optional[dict[str, str]] = None
        self._bare: dict[str, str] = {}

    def code(self) -> dict[str, str]:
        """{path: the file's code without comments, strings kept}."""
        if self._code is None:
            self._code = {}
            for p in _walk(self.root):
                if not _CODE_FILE.search(p.name):
                    continue
                try:
                    text = p.read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    continue
                rel = str(p.relative_to(self.root))
                self._code[rel], self._bare[rel] = _lex(text, rel)
                if len(self._code) >= 4000:
                    break
        return self._code

    def of(self, rel: str) -> tuple[Optional[str], Optional[str]]:
        """(code, code without strings) of one tree file, read from the walk when it was in it,
        else from disk (a file past the walk's depth or cap)."""
        codes = self.code()
        if rel in codes:
            return codes[rel], self._bare[rel]
        try:
            text = (self.root / rel).read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return None, None
        self._code[rel], self._bare[rel] = _lex(text, rel)
        return self._code[rel], self._bare[rel]

    def symbol_file(self, cited: str) -> Optional[str]:
        """The non-test file defining `cited` (`pkg.Name`, `Type.Method`), or None."""
        cited = str(cited or "").strip("`'\",;:()[]").rstrip(".")
        parts = cited.split(".")
        if "/" in cited or len(parts) < 2 or not all(_IDENT.fullmatch(x) for x in parts):
            return None
        if len(parts) == 2 and parts[1].lower() in _FILE_EXTS:
            return None                      # `loader.go`, `gateway.yaml`: a file, not a name
        name, quals = parts[-1], parts[:-1]
        if len(name) < 2 or name in _KEYWORDS:
            return None
        self.code()
        for rel, bare in list(self._bare.items()):
            if _TEST_PATH.search(rel) or not _defines(bare, name):
                continue
            path = Path(rel)
            # The qualifier is a package (a path part or the file's stem) or a TYPE the file
            # defines — never a variable: `l.Load` with a local `l` is not `zrules.Load`.
            if any(q in {*path.parts[:-1], path.stem} or _defines_type(bare, q) for q in quals):
                return rel
        return None

    def loader_of(self, data: list[str], among: Iterable[str],
                  cited: Optional[Mapping[str, Iterable[str]]] = None) -> Optional[str]:
        """The code that READS one of the data files, looked for only where the evidence points
        (`reaches` from the cited files and the cited symbols' bodies). None when there is none."""
        return self.reaches(data, _starts(among, cited or {}))

    def file_reads(self, rel: str, data: list[str]) -> bool:
        """Whether the non-test file `rel`, read WHOLE, reads one of `data` (`_reads`), or with
        no data file named, some data or config file (`_reads_data`). Only for a file the
        evidence cites with no symbol (#155): a file BOSS arrives at by a call is judged by the
        called function's body (`body_reads`)."""
        if _TEST_PATH.search(rel) or not _CODE_FILE.search(rel):
            return False
        code, _ = self.of(rel)
        if code is None:
            return False
        return (any(d != rel and _reads(code, d, rel) for d in data) if data
                else _reads_data(code))

    def body_reads(self, rel: str, sym: str, data: list[str],
                   bindings: Optional[Mapping[str, str]] = None) -> bool:
        """Whether the BODY of `sym` in `rel` reads the data (BOSSAPP-1329 review): what the
        function itself does — its own read calls and strings, the file's top-level string
        constants it may name, an embedded file it references, a path join whose base is a
        bound parameter (`bindings`, or resolved from every caller: `param_value`). A sibling
        function of the same file never counts."""
        if _TEST_PATH.search(rel) or not _CODE_FILE.search(rel):
            return False
        code, bare = self.of(rel)
        if code is None:
            return False
        body = _body_of(code, bare, sym, rel)
        if not body:
            return False
        binds = dict(bindings or {})
        read_args = _read_args(body)
        for name in _join_bases(body) + read_args:
            if name not in binds:
                value = self.param_value(rel, sym, name)
                if value is not None:
                    binds[name] = value
        text = _bind_joins(_top_level_strings(code, rel) + "\n" + body, binds)
        # THE PATH ITS CALLERS HAND IT (BOSSAPP-1333): `loadRateTable(cfgPath)` reads
        # `os.ReadFile(cfgPath)`, and every caller resolves cfgPath to "rates.yaml" (main's
        # env lookup with its default). The bound value is read as the string it is.
        text += "".join(f'\n"{binds[n]}"' for n in read_args if n in binds)
        if (any(d != rel and _reads(text, d, rel) for d in data) if data
                else _reads_data(text)):
            return True
        here = Path(rel).parent.as_posix()
        for var, patterns in _embedded(code).items():
            if not re.search(r"\b" + re.escape(var) + r"\b", body):
                continue
            if not data:
                return True
            for pattern in patterns:
                full = pattern if here in ("", ".") else f"{here}/{pattern}"
                # as `_reads` takes an embed: relative to the package, or as written
                if any(d == c or _fnmatch(d, c) or Path(d).parent.as_posix() == c
                       for c in (pattern, full) for d in data):
                    return True
        return False

    def param_value(self, rel: str, sym: str, name: str, depth: int = 3) -> Optional[str]:
        """The string a parameter of `sym` holds at EVERY call site of it in the tree's
        non-test code (a literal, a local assigned one — an env lookup's default included — or
        the caller's own parameter, resolved the same way), or None when the callers disagree,
        there are none, or it cannot be told."""
        if depth <= 0:
            return None
        code, bare = self.of(rel)
        if bare is None:
            return None
        params = _params_of(bare, sym)
        if name not in params:
            return None
        idx = params.index(name)
        values: set[str] = set()
        for crel, caller, args in self.call_sites(rel, sym):
            if args is None or idx >= len(args):
                return None
            value = self._expr_value(crel, caller, args[idx], depth - 1)
            if value is None:
                return None
            values.add(value)
        return values.pop() if len(values) == 1 else None

    def call_sites(self, rel: str, sym: str
                   ) -> list[tuple[str, Optional[str], Optional[list[str]]]]:
        """Every call of `rel`'s `sym` in the tree's non-test code — unqualified within its
        package, qualified by an import of its package elsewhere — as (file, enclosing
        function, arguments)."""
        # A Go file's package is its `package` clause: a module's root package (fxquote.go,
        # `package fxquote`, imported as "fxquote") has no directory name (BOSSAPP-1333).
        _, own_bare = self.of(rel)
        clause = (re.search(r"(?m)^\s*package\s+([A-Za-z_]\w*)", own_bare or "")
                  if _ext(rel) == "go" else None)
        pkg = clause.group(1) if clause else Path(rel).parent.name
        here = Path(rel).parent
        out: list[tuple[str, Optional[str], Optional[list[str]]]] = []
        self.code()
        for crel, cbare in list(self._bare.items()):
            if _TEST_PATH.search(crel):
                continue
            ccode = self._code.get(crel, "")
            same = _ext(crel) == _ext(rel) and Path(crel).parent == here
            quals = {a for a, p in _import_map(ccode, crel).items() if p == pkg}
            pattern = (r"(?<![\w.])" if same else r"\b(?:" + "|".join(map(re.escape, quals))
                       + r")\.") if (same or quals) else None
            if pattern is None:
                continue
            for m in re.finditer(pattern + re.escape(sym) + r"\s*\(", ccode):
                line_before = ccode[ccode.rfind("\n", 0, m.start()) + 1:m.start()]
                if re.search(r"\b(?:func|def|function|fn|class)\s*(?:\([^)]*\)\s*)?$",
                             line_before):
                    continue                    # its definition, not a call
                out.append((crel, _enclosing_func(cbare, ccode.count("\n", 0, m.start())),
                            _call_args(ccode, m.end() - 1)))
        return out

    def fed_by_loader(self, data: list[str], rel: str, sym: str, depth: int = 3
                      ) -> Optional[str]:
        """THE VALUE PASSED ON (#171 review (a)): the cited code is fed by a value its caller
        also hands to code that reads the data — `main` builds `store`, passes it to
        `config.ReloadOnce` (which reaches `Load`) and to `proxy.NewHandler`, whose handler
        calls the cited `applyCORS`. Followed up the callers of the cited function (a method
        through its type's constructor) at most `depth` levels; at each caller, only a variable
        passed BOTH on the cited path and to a call that reaches the reader counts. A value
        built by hand (`RateLimit{Rate: 5}`) is passed from nothing. Returns the reading file,
        or None."""
        frontier, seen = [(rel, sym)], set()
        for _ in range(depth):
            nxt = []
            for frel, fsym in frontier:
                if (frel, fsym) in seen or fsym is None:
                    continue
                seen.add((frel, fsym))
                _, fbare = self.of(frel)
                recv = _receiver_type(fbare or "", fsym)
                sites = (self.constructions(frel, recv) if recv
                         else self.call_sites(frel, fsym))
                for crel, caller, args in sites:
                    idents = {a.strip().lstrip("&*") for a in args or ()
                              if re.fullmatch(r"\s*[&*]?[A-Za-z_]\w*\s*", a)}
                    if caller and idents:
                        hit = self._passes_to_reader(crel, caller, idents, data)
                        if hit:
                            return hit
                    if caller and any(not re.fullmatch(r"\s*(?:[\"'`].*|\d.*|[&\w.]*\{.*)\s*",
                                                       a, re.S) for a in args or ()):
                        nxt.append((crel, caller))
            frontier = nxt
        return None

    def constructions(self, rel: str, type_name: str
                      ) -> list[tuple[str, Optional[str], Optional[list[str]]]]:
        """The call sites that build a `type_name` of `rel`'s package: its constructors
        (`func NewHandler(…) *Handler`) and the type called directly (a class)."""
        out = list(self.call_sites(rel, type_name))
        for r in self.package_files(rel):
            _, bare = self.of(r)
            for m in re.finditer(r"(?m)^func\s+([A-Za-z_]\w*)\s*\([^)]*\)\s*\(?\*?"
                                 + re.escape(type_name) + r"\b", bare or ""):
                out += self.call_sites(r, m.group(1))
        return out

    def _passes_to_reader(self, rel: str, func: str, idents: set[str], data: list[str]
                          ) -> Optional[str]:
        code, bare = self.of(rel)
        body = _body_of(code or "", bare or "", func, rel)
        bare_body = _blank_strings(body)
        imports = _import_map(code or "", rel)
        for m in _CALL.finditer(bare_body):
            pkg = imports.get(m.group(1)) or (m.group(1) if self.is_type(m.group(1)) else None)
            hit = self.symbol_file(f"{pkg}.{m.group(2)}") if pkg else None
            args = _call_args(body, m.end() - 1) or []
            # passed IN to the reader (`ReloadOnce(dir, store)`), or what the reader RETURNED
            # (`cfg, err := config.Load(dir)`)
            if not hit:
                continue
            # WHAT THE READER RETURNED, then used on the cited path (`cfg, err :=
            # config.Load(dir)` → `New(cfg)`); a discarded result (`_ =`, a bare call) is not.
            returned = _assigned_names(bare_body, m.start())
            # OR A VALUE THE READER FILLS: an argument its body assigns into, or hands the
            # loaded value to (`store.Swap(snap)` in ReloadOnce) — never one it only takes.
            params = _params_of(self.of(hit)[1] or "", m.group(2))
            filled = {a.strip().lstrip("&*") for p, a in zip(params, args)
                      if a.strip().lstrip("&*") in idents
                      and self.fills(hit, m.group(2), p, data)}
            if idents & (returned | filled):
                found = self.reaches(data, [(hit, m.group(2))])
                if found:
                    return found
        return None

    def fills(self, rel: str, func: str, param: str, data: list[str], depth: int = 2) -> bool:
        """Whether `func`'s body writes the loaded data into its parameter `param`: assigns
        into it (`p.Rate = …`, `*p = …`, `p[k] = …`), passes it a value a reader returned
        (`p.Swap(snap)` after `snap, err := Load(dir)`), or hands it to a call that fills it."""
        code, bare = self.of(rel)
        if code is None:
            return False
        body = _body_of(code, bare or "", func, rel)
        blank = _blank_strings(body)
        p = re.escape(param)
        # into it, not over it: `p = x` rebinds the local and fills nothing
        if re.search(r"(?<![\w.])(?:\*" + p + r"|" + p + r"(?:\.\w+|\[[^\]\n]*\])+)\s*=(?!=)",
                     blank):
            return True
        imports = _import_map(code, rel)
        loaded: set[str] = set()
        calls = []
        for m in list(_CALL.finditer(blank)) + list(_LOCAL_CALL.finditer(blank)):
            if m.re is _CALL:
                pkg = imports.get(m.group(1)) or (
                    m.group(1) if self.is_type(m.group(1)) else None)
                hit, name = (self.symbol_file(f"{pkg}.{m.group(2)}") if pkg else None,
                             m.group(2))
            else:
                hit, name = self.local_def(rel, m.group(1)), m.group(1)
            if not hit or name == func:
                continue
            calls.append((m, hit, name))
            if self.reaches(data, [(hit, name)]):
                loaded |= _assigned_names(blank, m.start())
        for m in re.finditer(r"(?<![\w.])" + p + r"\.\w+\s*\(", blank):
            args = {a.strip().lstrip("&*") for a in _call_args(body, m.end() - 1) or []}
            if args & loaded:
                return True
        if depth > 0:
            for m, hit, name in calls:
                args = [a.strip().lstrip("&*") for a in _call_args(body, m.end() - 1) or []]
                cparams = _params_of(self.of(hit)[1] or "", name)
                if any(a == param and self.fills(hit, name, cp, data, depth - 1)
                       for cp, a in zip(cparams, args)):
                    return True
        return False

    def _expr_value(self, rel: str, func: Optional[str], expr: str, depth: int
                    ) -> Optional[str]:
        expr = expr.strip()
        lit = re.fullmatch(r"\"([^\"\n]*)\"|'([^'\n]*)'|`([^`\n]*)`", expr)
        if lit:
            return next(g for g in lit.groups() if g is not None)
        if not re.fullmatch(r"[A-Za-z_]\w*", expr) or func is None:
            return None
        code, bare = self.of(rel)
        body = _body_of(code or "", bare or "", func, rel)
        assigns = list(re.finditer(r"(?m)^\s*(?:var\s+)?" + re.escape(expr)
                                   + r"\s*(?::=|=)\s*(.+)$", body))
        if assigns:
            # EVERY assignment, and an environment lookup's NAME is never the value
            # (BOSSAPP-1333: `cfgPath := os.Getenv("FXQUOTE_CONFIG")` then `cfgPath =
            # "rates.yaml"` resolved to "FXQUOTE_CONFIG"). A lookup's default
            # (`os.environ.get("X", "rates.yaml")`) is a value; one value, or none.
            values: list[str] = []
            for a in assigns:
                rhs = a.group(1)
                strings = re.findall(r"\"([^\"\n]*)\"|'([^'\n]*)'", rhs)
                strings = [x or y for x, y in strings]
                if _ENV_LOOKUP.search(rhs) and strings:
                    # CONFIGURED BY THE ENVIRONMENT (PR #172 review P3): when the Dockerfile
                    # or compose file sets the variable, that is the value that runs, never
                    # the code's default.
                    configured = self.env_values(strings[0])
                    if configured:
                        distinct = set(configured)
                        if _UNRESOLVED in distinct:
                            return None
                        return distinct.pop() if len(distinct) == 1 else None
                    strings = strings[1:]       # the variable's name, then any default
                values.extend(strings[-1:])
            distinct = set(values)
            return distinct.pop() if len(distinct) == 1 else None
        return self.param_value(rel, func, expr, depth)

    def env_values(self, name: str) -> list[str]:
        """What the tree's Dockerfiles and compose files set environment variable `name` to
        (`ENV NAME=value`, `ENV NAME value`, compose `NAME: value` / `- NAME=value`)."""
        if not re.fullmatch(r"[A-Za-z_]\w*", name or ""):
            return []
        cache = self.__dict__.setdefault("_env_cache", {})
        if name in cache:
            return cache[name]
        found: list[str] = []
        n = re.escape(name)
        unresolved = False
        for p in _walk(self.root):
            low = p.name.lower()
            if not (low.startswith("dockerfile") or low.endswith(".dockerfile")
                    or ("compose" in low and low.endswith((".yml", ".yaml")))):
                continue
            try:
                text = p.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            here = p.parent.relative_to(self.root)
            if "compose" in low:
                got, unknown = _compose_env(text, here, p.parent, name)
                if got is not None:
                    found.extend(got)
                    unresolved = unresolved or unknown
                    continue
            # A path inside the image is the tree file a COPY put there (`COPY rates.yaml
            # /app/rates.yaml`), so the value names what the tree holds.
            copies = _image_copies(text, here)
            for m in re.finditer(r"(?m)^\s*ENV\s+(.*)$", text):
                for k, v in re.findall(r"([A-Za-z_]\w*)(?:=|\s+)(\"[^\"]*\"|\S+)", m.group(1)):
                    if k == name:
                        found.append(_image_path(v.strip("\"'"), copies))
            if "compose" in low:                    # a compose file YAML could not read
                unresolved = unresolved or bool(re.search(r"\b" + n + r"\b", text))
        if unresolved:
            found = [_UNRESOLVED]
        cache[name] = found
        return found

    def defining_file(self, name: str) -> Optional[str]:
        """The ONE code file (a test included) that defines `name`, or None (none, or several)."""
        if name in _KEYWORDS or len(name) < 3:
            return None
        self.code()
        hits = [rel for rel, bare in self._bare.items() if _defines(bare, name)]
        return hits[0] if len(hits) == 1 else None

    def package_dir(self, name: str) -> Optional[str]:
        """The ONE folder named `name` that holds code (`proxy` → internal/proxy), or None."""
        self.code()
        dirs = {Path(rel).parent.as_posix() for rel in self._bare
                if Path(rel).parent.name == name}
        return dirs.pop() if len(dirs) == 1 else None

    def package_files(self, rel: str) -> list[str]:
        """The non-test code files of `rel`'s package: its directory for Go, itself otherwise."""
        if _ext(rel) != "go":
            return [rel]
        here = Path(rel).parent
        self.code()
        return [r for r in self._bare if Path(r).parent == here and r.endswith(".go")
                and not _TEST_PATH.search(r)]

    def local_def(self, rel: str, name: str) -> Optional[str]:
        """The file of `rel`'s package that defines `name` (an unqualified call's target)."""
        if len(name) < 2 or name in _KEYWORDS:
            return None
        for r in self.package_files(rel):
            _, bare = self.of(r)
            if bare is not None and _defines(bare, name):
                return r
        return None

    def reaches(self, data: list[str], starts: Iterable[tuple[str, Optional[str]]],
                *, depth: int = 4, cap: int = 60) -> Optional[str]:
        """The code that reads the item's data, reached from where the evidence points
        (BOSSAPP-1329): a cited file with no symbol is read whole (#155); every function — a
        cited symbol, or one BOSS arrives at by a call — is judged by its own BODY
        (`body_reads`), never by its file's other functions. From a function's body BOSS
        follows, at most `depth` calls deep, a qualified call whose qualifier is an import (its
        alias resolved) or a type, and an unqualified call within the same package, carrying
        the call's string arguments into the callee's parameters. A cited `package main` file
        starts from its `main`. Never a call outside a body on the path (dead code), a local
        variable's method, a config type merely taken, or a file the evidence does not lead
        to. Returns the reading file, or None."""
        queue: list[tuple[str, Optional[str], int, dict]] = [(r, s, 0, {}) for r, s in starts]
        seen: set[tuple[str, Optional[str]]] = set()
        while queue:
            rel, sym, level, binds = queue.pop(0)
            if (rel, sym) in seen or len(seen) >= cap or _TEST_PATH.search(rel):
                continue
            seen.add((rel, sym))
            if sym is None and self.file_reads(rel, data):
                return rel
            if sym is not None and self.body_reads(rel, sym, data, binds):
                return rel
            if level >= depth:
                continue
            code, bare = self.of(rel)
            if code is None:
                continue
            symbols = [sym] if sym else (
                ["main"] if _ext(rel) == "go" and re.search(r"(?m)^package\s+main\b", bare)
                else [])
            imports = _import_map(code, rel)
            for s in symbols:
                body = _body_of(code, bare, s, rel)
                bare_body = _blank_strings(body)
                hops: list[tuple[str, str, int]] = []
                for m in list(_CALL.finditer(bare_body))[:60]:
                    qual, name = m.group(1), m.group(2)
                    pkg = imports.get(qual) or (qual if self.is_type(qual) else None)
                    hit = self.symbol_file(f"{pkg}.{name}") if pkg else None
                    if hit:
                        hops.append((hit, name, m.end() - 1))
                for m in list(_LOCAL_CALL.finditer(bare_body))[:60]:
                    hit = self.local_def(rel, m.group(1))
                    if hit and m.group(1) != s:
                        hops.append((hit, m.group(1), m.end() - 1))
                for hit, name, paren in hops:
                    args = _call_args(body, paren) or []
                    _, hbare = self.of(hit)
                    params = _params_of(hbare or "", name)
                    carried = {}
                    for p, a in zip(params, args):
                        v = (self._expr_value(rel, s, a, 2) if a.strip() not in binds
                             else binds[a.strip()])
                        if v is not None:
                            carried[p] = v
                    queue.append((hit, name, level + 1, carried))
        return None

    def is_type(self, name: str) -> bool:
        """Whether a non-test file of the tree defines `name` as a type (class, struct, …)."""
        self.code()
        return any(not _TEST_PATH.search(rel) and _defines_type(bare, name)
                   for rel, bare in self._bare.items())


def _defines_type(code: str, name: str) -> bool:
    """Whether `code` defines `name` as a TYPE: a class, struct, interface, enum, trait,
    object or record (Go `type Name struct`, Python/Java/TS `class Name`)."""
    e = re.escape(name)
    return re.search(r"\b(?:class|struct|interface|enum|trait|object|record|type)\s+" + e
                     + r"\b", code) is not None


def _import_map(code: str, path: str = "") -> dict[str, str]:
    """{qualifier: the package it names}: a Go import alias resolves to the imported package's
    own name (`rulesyaml "northgate/rules"` → rules), every other import to itself."""
    out: dict[str, str] = {}
    if _ext(path) == "go":
        blocks = re.findall(r"(?ms)^\s*import\s*\((.*?)\)", code)
        singles = re.findall(r"(?m)^\s*import\s+((?:[A-Za-z_]\w*\s+)?\"[^\"]+\")", code)
        for spec in [ln for blk in blocks for ln in blk.splitlines()] + singles:
            m = re.match(r"\s*(?:([A-Za-z_]\w*)\s+)?\"([^\"]+)\"", spec)
            if not m:
                continue
            parts = m.group(2).rstrip("/").split("/")
            last = re.sub(r"\.v\d+$", "", parts[-1])
            if re.fullmatch(r"v\d+", last) and len(parts) > 1:
                last = parts[-2]
            pkg = re.sub(r"[^\w]", "_", last)
            out[m.group(1) or pkg] = pkg
        return out
    return {n: n for n in _import_names(code, path)}


def _import_names(code: str, path: str = "") -> set[str]:
    """The names a file's imports bind as qualifiers: a Go import's alias or package name (its
    last path element, `gopkg.in/yaml.v3` → `yaml`), Python's `import a` / `import a as b` /
    `from p import a`, and JS/TS `import x from` / `import * as x` / `const x = require(…)`."""
    names: set[str] = set()
    ext = _ext(path)
    if ext == "go":
        blocks = re.findall(r"(?ms)^\s*import\s*\((.*?)\)", code)
        singles = re.findall(r"(?m)^\s*import\s+((?:[A-Za-z_]\w*\s+)?\"[^\"]+\")", code)
        for spec in [ln for blk in blocks for ln in blk.splitlines()] + singles:
            m = re.match(r"\s*(?:([A-Za-z_]\w*)\s+)?\"([^\"]+)\"", spec)
            if not m:
                continue
            last = m.group(2).rstrip("/").rsplit("/", 1)[-1]
            last = re.sub(r"\.v\d+$", "", last)
            if re.fullmatch(r"v\d+", last) and "/" in m.group(2):
                last = m.group(2).rstrip("/").rsplit("/", 2)[-2]
            names.add(m.group(1) or re.sub(r"[^\w]", "_", last))
        return names
    for m in re.finditer(r"(?m)^\s*import\s+([\w.]+)(?:\s+as\s+(\w+))?", code):
        names.add(m.group(2) or m.group(1).split(".")[0])
    for m in re.finditer(r"(?m)^\s*from\s+[\w.]+\s+import\s+([^\n(]+)", code):
        for part in m.group(1).split(","):
            bits = part.split()
            if bits:
                names.add(bits[-1] if "as" in bits else bits[0])
    for m in re.finditer(r"\bimport\s+(?:\*\s+as\s+)?([A-Za-z_$][\w$]*)\s+from\b", code):
        names.add(m.group(1))
    for m in re.finditer(r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*require\(", code):
        names.add(m.group(1))
    return names


def _body_of(code: str, bare: str, symbol: str, path: str = "") -> str:
    """The code of `symbol`'s definition (its body), or "" when the file does not define it:
    braces counted in the string-free code for brace languages, the indented block for Python.
    Line numbers are the same in `code` and `bare` (`_lex` keeps every line break)."""
    lines, bare_lines = code.splitlines(), bare.splitlines()
    e = re.escape(symbol)
    head = re.compile(
        r"\b(?:func|def|function|fn|fun|sub)\s+(?:\([^)]*\)\s*)?\*?" + e + r"\b"
        r"|^\s*(?:export\s+)?(?:(?:public|private|protected|static|final|async|override)\s+)*"
        r"(?:[\w<>\[\],.?]+\s+)?" + e + r"\s*\([^;{]*\)\s*(?:\{|:|throws\b|->)"
        r"|\b(?:const|let|var)\s+" + e + r"\s*=")
    start = next((i for i, ln in enumerate(bare_lines) if head.search(ln)), None)
    if start is None:
        return ""
    if _ext(path) in ("py", "pyi"):
        indent = len(bare_lines[start]) - len(bare_lines[start].lstrip())
        end = start + 1
        while end < len(bare_lines) and (not bare_lines[end].strip() or (
                len(bare_lines[end]) - len(bare_lines[end].lstrip()) > indent)):
            end += 1
        return "\n".join(lines[start:end])
    depth, opened = 0, False
    for i in range(start, len(bare_lines)):
        for ch in bare_lines[i]:
            if ch == "{":
                depth, opened = depth + 1, True
            elif ch == "}":
                depth -= 1
        if opened and depth <= 0:
            return "\n".join(lines[start:i + 1])
    return "\n".join(lines[start:])


def _fnmatch(name: str, pattern: str) -> bool:
    import fnmatch

    return fnmatch.fnmatch(name, pattern)


def _top_level_strings(code: str, path: str) -> str:
    """The file's top-level string constants (`const rulesDir = "rules"`), which a function
    body may name: the only part of a file outside the body a body is judged with."""
    lines = []
    for line in code.splitlines():
        if re.match(r"^(?:const|var|let|val|final|static)?\s*[A-Za-z_]\w*(?:\s+\w+)?\s*:?=\s*"
                    r"[\"'`][^\"'`\n]*[\"'`]\s*;?\s*$", line):
            lines.append(line)
    return "\n".join(lines)


def _embedded(code: str) -> dict[str, list[str]]:
    """{var: [patterns]} for Go's `//go:embed <patterns>` directives and the var each one
    declares."""
    out: dict[str, list[str]] = {}
    lines = code.splitlines()
    for i, line in enumerate(lines):
        if not line.strip().startswith("//go:embed"):
            continue
        pats = [p.strip("\"`").rstrip("/") for p in line.split()[1:]]
        for nxt in lines[i + 1:i + 4]:
            m = re.match(r"^\s*var\s+([A-Za-z_]\w*)", nxt)
            if m:
                out.setdefault(m.group(1), []).extend(pats)
                break
    return out


_JOIN = re.compile(r"\b(?:filepath|path|os\.path|Paths)\.(?:[Jj]oin|get|resolve)\s*\(")


#: An environment lookup whose first string is the variable's NAME, not a value.
_ENV_LOOKUP = re.compile(r"\b(?:os\.(?:Getenv|LookupEnv|getenv)|os\.environ\.get|environ\.get|"
                         r"System\.getenv|getenv|env::var|ENV\.fetch)\s*\(")


#: An environment value BOSS cannot know (a compose `env_file:` that is not in the tree).
_UNRESOLVED = "\x00unresolved"


def _compose_env(text: str, here: Path, folder: Path, name: str
                 ) -> tuple[Optional[list[str]], bool]:
    """(values, unknown) of environment variable `name` in a compose file, each SERVICE's value
    mapped back to the tree through THAT service's bind mounts (the service that sets it is the
    one whose process reads it). `unknown`: a service's `env_file` is not in the tree. (None,
    False) when the file is not YAML compose can read."""
    try:
        import yaml

        doc = yaml.safe_load(text)
    except Exception:  # noqa: BLE001 — not readable: the caller falls back to plain text
        return None, False
    services = (doc or {}).get("services") if isinstance(doc, dict) else None
    if not isinstance(services, dict):
        return None, False
    base = "" if str(here) in ("", ".") else here.as_posix() + "/"
    values: list[str] = []
    unknown = False
    for svc in services.values():
        if not isinstance(svc, dict):
            continue
        mounts: list[tuple[list[str], str]] = []
        for vol in svc.get("volumes") or []:
            src = dst = None
            if isinstance(vol, str) and ":" in vol:
                parts = vol.split(":")
                src, dst = parts[0], parts[1]
            elif isinstance(vol, dict) and vol.get("type", "bind") == "bind":
                src, dst = vol.get("source"), vol.get("target")
            if src and dst and (src.startswith((".", "/")) is False or src.startswith(".")) \
                    and str(dst).startswith("/"):
                mounts.append(([base + re.sub(r"^\./", "", str(src)).rstrip("/")], str(dst)))
        env = svc.get("environment") or {}
        said: list[str] = []
        if isinstance(env, dict):
            if name in env and env[name] is not None:
                said.append(str(env[name]))
        elif isinstance(env, list):
            for e in env:
                k, _, val = str(e).partition("=")
                if k.strip() == name and _:
                    said.append(val.strip())
        files = svc.get("env_file") or []
        for ref in ([files] if isinstance(files, (str, dict)) else files):
            path = ref.get("path") if isinstance(ref, dict) else ref
            env_path = folder / str(path or "")
            if not path or not env_path.is_file():
                unknown = True
                continue
            for em in re.finditer(r"(?m)^\s*(?:export\s+)?" + re.escape(name) + r"\s*=\s*(.*?)\s*$",
                                  env_path.read_text(encoding="utf-8", errors="ignore")):
                said.append(em.group(1))
        values.extend(_image_path(x.strip("\"'"), mounts) for x in said)
    return values, unknown


def _image_copies(text: str, here: Path) -> list[tuple[list[str], str]]:
    """(tree sources, path in the container) for every Dockerfile `COPY` (relative to its
    WORKDIR) and every compose bind mount (`./config:/app/config-staging`), the sources
    relative to the tree root."""
    out: list[tuple[list[str], str]] = []
    workdir = "/"
    base = "" if str(here) in ("", ".") else here.as_posix() + "/"
    for ln in text.splitlines():
        w = re.match(r"\s*WORKDIR\s+(\S+)", ln, re.I)
        if w:
            workdir = w.group(1) if w.group(1).startswith("/") else workdir.rstrip("/") + "/" + w.group(1)
            continue
        c = re.match(r"\s*COPY\s+((?:--\S+\s+)*)(.+?)\s+(\S+)\s*$", ln, re.I)
        if c and not re.search(r"(?i)--from\b", c.group(1)):
            # A COPY from another stage or image (`--from` among its flags) brings nothing of
            # the tree.
            c = re.match(r"\s*COPY\s+(?:--\S+\s+)*(.+?)\s+(\S+)\s*$", ln, re.I)
        else:
            c = None
        if c:
            dest = c.group(2)
            if not dest.startswith("/"):
                dest = workdir.rstrip("/") + "/" + re.sub(r"^\./", "", dest)
            out.append(([base + re.sub(r"^\./", "", s_) for s_ in c.group(1).split()], dest))
            continue
        v = re.match(r"\s*-\s*[\"']?(\.{1,2}/[^:\s\"']*|[\w\-][^:\s\"']*/[^:\s\"']*)"
                     r":(/[^:\s\"']+)", ln)
        if v:
            out.append(([base + re.sub(r"^\./", "", v.group(1)).rstrip("/")], v.group(2)))
    return out


def _image_path(value: str, copies: list[tuple[list[str], str]]) -> str:
    """`value` (a path in the container) as the tree path a COPY or a bind mount put there —
    the file itself, or a path under a copied/mounted directory — else unchanged."""
    for srcs, dest in copies:
        dest = dest.rstrip("/") or "/"
        for src in srcs:
            if value.rstrip("/") == dest:
                return src.rstrip("/") or "."
            if value.startswith(dest + "/"):
                return f"{src.rstrip('/')}/{value[len(dest) + 1:]}"
            if dest + "/" + Path(src).name == value:
                return src
    return value


def _read_args(body: str) -> list[str]:
    """The identifiers a read call takes as its first argument (`os.ReadFile(cfgPath)` →
    cfgPath, `open(path)` → path): a path the function was handed, not one it names."""
    out = []
    for m in re.finditer(r"(?<![\w])(?:[A-Za-z_][\w]*\.)*" + _READ_FN + r"\s*\(", body):
        args = _call_args(body, m.end() - 1) or []
        if args and re.fullmatch(r"[A-Za-z_]\w*", args[0].strip()):
            out.append(args[0].strip())
    return list(dict.fromkeys(out))


def code_evidence(found: list[str], item_text: Any = None) -> list[str]:
    """The cited files that evidence the item: code, configuration or tests — never a document,
    and never a manifest or lockfile, which a dependency item's claim is checked against
    instead (acceptance/dependencies.py, PR #172 review P1)."""
    return [f for f in found if not _DOC_PATH.search(f) and not _DECL_PATH.search(f)]


#: How alike two items must be for one's evidence to be the other's (Jaccard of content words).
NEAR_DUPLICATE = 0.6


def near_duplicate(a: Any, b: Any) -> bool:
    """Whether two items say the same thing (the 1↔10 and 2↔11 pairs of FXQuote: "A quote applies
    the pair's spread and rounds half-even" / "A quote correctly applies the pair's configured
    spread and rounds half-even")."""
    ta, tb = str(a or ""), str(b or "")
    wa, wb = _words(ta), _words(tb)
    if not (wa and wb) or len(wa & wb) / len(wa | wb) < NEAR_DUPLICATE:
        return False
    # NEVER ACROSS OPPOSITE MEANINGS (PR #172 re-review): "Rejects >1 MB with 413" is not
    # "Accepts >1 MB". Two items that differ in a negation, an opposite verb, or any number
    # or status code are different items, however many words they share.
    if _negations(ta) != _negations(tb) or _numbers(ta) != _numbers(tb):
        return False
    la, lb = _stems(ta), _stems(tb)
    return not any((x in la and y in lb) or (y in la and x in lb) for x, y in _OPPOSITES)


_NEGATION = re.compile(r"(?i)\b(?:no|not|never|none|nor|without|cannot)\b|n't\b")
_OPPOSITES = (("accept", "reject"), ("allow", "deny"), ("allow", "block"), ("enable", "disable"),
              ("include", "exclude"), ("pass", "fail"), ("succeed", "fail"), ("start", "stop"),
              ("open", "close"), ("lock", "unlock"), ("add", "remove"), ("grant", "revoke"),
              ("show", "hide"), ("valid", "invalid"), ("permit", "forbid"), ("above", "below"),
              ("over", "under"), ("more", "less"), ("min", "max"), ("increase", "decrease"),
              ("encrypt", "decrypt"), ("before", "after"), ("expose", "hide"), ("keep", "drop"))


def _negations(text: str) -> list[str]:
    return sorted(m.group(0).lower() for m in _NEGATION.finditer(text))


def _numbers(text: str) -> set[str]:
    return set(re.findall(r"\d+(?:\.\d+)?", text))


def _stems(text: str) -> set[str]:
    out = set()
    for w in re.findall(r"[A-Za-z]+", text.lower()):
        if len(w) > 4 and w.endswith(("ied", "ies")):
            w = w[:-3] + "y"                    # denied, denies → deny
        for suffix in ("ing", "ed", "es", "s"):
            if w.endswith(suffix) and len(w) - len(suffix) >= 3:
                w = w[:-len(suffix)]
                break
        out.add(w[:-1] if w.endswith("e") and len(w) > 4 else w)
        out.add(w)
    return out


def same_as(answers: Mapping[int, tuple[str, str]], texts: Mapping[int, str], n: int,
            why: str) -> tuple[str, list[int]]:
    """(`why` with the MET answer of every NEAR-DUPLICATE item it points at appended, the items
    it points at that are NOT near-duplicates). "same as ITEM 1" on an item that says what
    item 1 says carries item 1's evidence, verified here as this item's (BOSSAPP-1333); on any
    other item it borrows nothing — "413 on >1 MB" is not evidenced by "written in Go" (PR #172
    review P2). Followed up to three hops, never back to itself."""
    out, foreign, seen, frontier = [why], [], {n}, [why]
    for _ in range(3):
        nxt = []
        for text_ in frontier:
            for m in _SAME_AS.finditer(text_ or ""):
                ref = int(m.group(1))
                if ref in seen or ref not in texts:
                    continue
                seen.add(ref)
                if not near_duplicate(texts.get(n), texts.get(ref)):
                    foreign.append(ref)
                    continue
                status, other = answers.get(ref, (UNJUDGED, ""))
                if status == MET and other:
                    out.append(other)
                    nxt.append(other)
        frontier = nxt
    return " ".join(out), foreign


def _join_bases(body: str) -> list[str]:
    """The identifiers a path join takes as its base (`filepath.Join(dir, "tenants")` → dir)."""
    out = []
    for m in _JOIN.finditer(body):
        args = _call_args(body, m.end() - 1) or []
        if args and re.fullmatch(r"[A-Za-z_]\w*", args[0].strip()):
            out.append(args[0].strip())
    return out


def _bind_joins(text: str, binds: Mapping[str, str]) -> str:
    """Every path join whose parts are all known (string literals, or bound names) written as
    the one path it makes: `filepath.Join(dir, "tenants")` with dir = "config" →
    `"config/tenants"`. A join with an unknown part is left as it is, and a folder string in it
    is no longer the folder (BOSSAPP-1329 review: a `tenants` cache dir is not config/tenants)."""
    out, pos = [], 0
    for m in _JOIN.finditer(text):
        if m.start() < pos:
            continue
        args = _call_args(text, m.end() - 1)
        if not args:
            continue
        parts = []
        for a in args:
            a = a.strip()
            lit = re.fullmatch(r"\"([^\"\n]*)\"|'([^'\n]*)'", a)
            if lit:
                parts.append(next(g for g in lit.groups() if g is not None))
            elif a in binds:
                parts.append(binds[a])
            else:
                parts = None
                break
        end = _close_paren(text, m.end() - 1)
        if parts is None or end is None:
            continue
        joined = "/".join(p.strip("/") for p in parts if p and p != ".")
        out.append(text[pos:m.start()])
        out.append(f'"{joined}"')
        pos = end + 1
    out.append(text[pos:])
    return "".join(out)


def _close_paren(text: str, open_at: int) -> Optional[int]:
    depth = 0
    for i in range(open_at, min(len(text), open_at + 2000)):
        ch = text[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i
    return None


def _call_args(text: str, open_at: int) -> Optional[list[str]]:
    """The top-level comma-separated arguments of the call whose `(` is at `open_at`."""
    end = _close_paren(text, open_at)
    if end is None:
        return None
    inner, args, depth, cur = text[open_at + 1:end], [], 0, ""
    for ch in inner:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == "," and depth == 0:
            args.append(cur)
            cur = ""
        else:
            cur += ch
    if cur.strip():
        args.append(cur)
    return args


def _params_of(bare: str, sym: str) -> list[str]:
    """A function's parameter names, in order (Go `func (r *T) Load(dir string, s *Store)`,
    Go's grouped `a, b string`, Python `def load(self, dir, n=1)`, JS/TS/Java)."""
    m = re.search(r"\b(?:func|def|function|fn|fun)\s+(?:\([^)]*\)\s*)?" + re.escape(sym)
                  + r"\s*\(", bare) or re.search(r"\b" + re.escape(sym) + r"\s*\(", bare)
    if not m:
        return []
    args = _call_args(bare, m.end() - 1) or []
    names = []
    for a in args:
        a = a.strip().split("=")[0].strip()
        tokens = re.findall(r"[A-Za-z_]\w*", a)
        if not tokens:
            continue
        if ":" in a:                                   # TS `dir: string`, Python annotations
            names.append(a.split(":")[0].strip().lstrip("*"))
        elif len(tokens) >= 2 and _ext_hint(bare) == "java":
            names.append(tokens[-1])
        else:
            names.append(tokens[0])
    return [n for n in names if n not in ("self", "cls")]


def _assigned_names(text: str, call_at: int) -> set[str]:
    """The names a call's result is assigned to (`cfg, err := config.Load(dir)` → cfg, err),
    never `_`; empty for a bare call."""
    line = text[text.rfind("\n", 0, call_at) + 1:call_at]
    lhs = re.search(r"^\s*(?:if\s+)?(?:var\s+|let\s+|const\s+)?([\w\s,]+?)\s*:?=\s*$", line)
    if not lhs:
        return set()
    return {n.strip() for n in lhs.group(1).split(",")
            if re.fullmatch(r"[A-Za-z]\w*", n.strip())} - {"err", "ok"}


def _receiver_type(bare: str, sym: str) -> Optional[str]:
    """The type a Go method belongs to (`func (h *Handler) proxy(` → Handler), or None."""
    m = re.search(r"\bfunc\s+\(\s*\w*\s*\*?\s*([A-Za-z_]\w*)(?:\[[^\]]*\])?\s*\)\s*"
                  + re.escape(sym) + r"\s*\(", bare)
    return m.group(1) if m else None


def _ext_hint(bare: str) -> str:
    return "java" if re.search(r"\b(?:public|private|protected)\s+\w", bare) else ""


def _enclosing_func(bare: str, line_no: int) -> Optional[str]:
    """The function line `line_no` (0-based; the same in the code and its string-free copy)
    is inside: the nearest definition at or above it."""
    best = None
    for i, line in enumerate(bare.splitlines()[:line_no + 1]):
        m = re.match(r"^\s*(?:func\s+(?:\([^)]*\)\s*)?|def\s+|function\s+)([A-Za-z_]\w*)",
                     line)
        if m:
            best = m.group(1)
    return best


#: An unqualified call (`ReloadOnce(dir, …)`), not a method (`x.F(`) nor a definition.
_LOCAL_CALL = re.compile(r"(?<![\w.])(?<!func )(?<!def )([A-Za-z_]\w*)\s*\(")


#: A qualified call in code (`rules.Load(`, `loader.read_rules(`).
_CALL = re.compile(r"\b([A-Za-z_]\w*)\.([A-Za-z_]\w*)\s*\(")


def _cited_symbols(root: Path, why: str, defs: _Definitions) -> dict[str, list[str]]:
    """{file: [symbols]} the evidence cites: each `file:Symbol` (`docs.go:RenderReadme`), and
    each qualified name resolved to the file defining it (`auth.VerifyJWT`)."""
    out: dict[str, list[str]] = {}
    for m in _FILE_SYMBOL.finditer(str(why or "")):
        hit = _resolve(root, m.group(1))
        if hit:
            out.setdefault(hit, []).append(m.group(2).split(".")[-1])
    for m in _QUALIFIED.finditer(str(why or "")):
        hit = defs.symbol_file(m.group(1))
        if hit:
            out.setdefault(hit, []).append(m.group(1).split(".")[-1])
    for m in _DIR_SYMBOL.finditer(str(why or "")):
        # A package and a symbol (`internal/lint:RenderReadme`): the package file defining it.
        folder = m.group(1).strip("./")
        if folder and (root / folder).is_dir():
            for rel, bare in list(defs._bare.items()) if defs.code() is not None else []:
                if Path(rel).parent.as_posix() == folder and not _TEST_PATH.search(rel) \
                        and _defines(bare, m.group(2)):
                    out.setdefault(rel, []).append(m.group(2))
                    break
    return {k: list(dict.fromkeys(v)) for k, v in out.items()}


_STOP_WORDS = frozenset(
    "test tests the and for with that this from into are not all its has have when then "
    "must should will can each every any internal pkg src lib main cmd app".split())


def _words(text: str) -> set[str]:
    """Content words of a name or a sentence: camelCase, snake_case, kebab and path parts
    split, lower-cased, a plural `s` dropped, 3+ letters, no filler."""
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", str(text or ""))
    out = set()
    for w in re.findall(r"[A-Za-z]{3,}", spaced):
        w = w.lower()
        w = w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith("ss") else w
        if w not in _STOP_WORDS:
            out.add(w)
    return out


#: A single quoted name in an answer (`ServeHTTP`), not a path or a qualified name.
_TICKED_IDENT = re.compile(r"`([A-Za-z_]\w{2,})(?:\(\))?`")

#: `package/dir:Symbol` (a Go package, a Python package folder) — no file extension.
_DIR_SYMBOL = re.compile(r"`?((?:[\w.\-]+/)*[\w.\-]+):`?([A-Za-z_]\w*)")


def _starts(found: Iterable[str], cited: Mapping[str, Iterable[str]]
            ) -> list[tuple[str, Optional[str]]]:
    """Where a loader search starts: each cited symbol's body, and each cited code file."""
    out: list[tuple[str, Optional[str]]] = [(rel, s) for rel, syms in cited.items()
                                            for s in syms]
    # A file cited WITH a symbol is that symbol's body only (BOSSAPP-1329 review: a sibling
    # beside a dead loader is not the loader); a file cited bare is read whole (#155).
    out += [(rel, None) for rel in found if _CODE_FILE.search(rel) and not cited.get(rel)]
    return list(dict.fromkeys(out))


def _named_loader(root: Path, why: str, data: list[str],
                  defs: Optional[_Definitions] = None) -> Optional[str]:
    """A `file:Symbol` the evidence cites that is non-test code DEFINING the symbol and READING
    one of the item's data files (`_reads`) — or, when the item names no data file, reading
    some data/config file (`_reads_data`): a defined function alone is not a loader (AR-23
    review). None when there is no such one."""
    defs = defs if defs is not None else _Definitions(root)
    for m in _FILE_SYMBOL.finditer(str(why or "")):
        hit = _resolve(root, m.group(1))
        symbol = m.group(2).split(".")[-1]
        if not hit or _TEST_PATH.search(hit) or not _CODE_FILE.search(hit) \
                or len(symbol) < 3 or symbol in _KEYWORDS:
            continue
        code, bare = defs.of(hit)
        if code is None or not _defines(bare, symbol):
            continue
        # The cited function's own body, and what it calls (`reaches`), never its file's
        # siblings (BOSSAPP-1329 review).
        if defs.reaches(data, [(hit, symbol)]):
            return hit
    return None


def _item_data_files(root: Path, item_text: str, found: Iterable[str]) -> list[str]:
    """The data files an item's wiring is about: the cited ones, and those the item names."""
    out = [f for f in found if _DATA_FILE.search(f) and not _DECL_PATH.search(f)]
    for m in _PATHISH.finditer(str(item_text or "")):
        if _DATA_FILE.search(m.group(1)):
            hit = _resolve(root, m.group(1))
            if hit and not _DECL_PATH.search(hit):
                out.append(hit)
    return list(dict.fromkeys(out))


def wiring_problem(root: Any, why: str, *, item_text: str = "",
                   needs_loader: bool = False,
                   defs: Optional[_Definitions] = None) -> Optional[str]:
    """Why a MET's wiring does not hold, or None (BOSSAPP-1233: a rules YAML the linter never
    read was cited as MET). Every "LOADED BY <file>:<symbol>" the reviewer wrote must hold:

      - the symbol is a real name (not a language keyword, at least 3 characters);
      - the file is in the tree and is not a test;
      - the file DEFINES the symbol (func/def/function/class/const/var/assignment), outside
        comments and strings;
      - the file's code READS the data file the item is about (`_reads`: embeds, opens, loads
        or globs it — its folder only through a glob in that same file); with no data file in
        the item, it reads some data/config file.

    An item tagged `needs_loader` at extraction needs such a loader: without a LOADED BY it may
    still be named as a cited `file:Symbol`, or found among the files the evidence points at
    (`_Definitions.loader_of`) — never anywhere else in the tree; neither is PARTIAL ("loader
    not named"). Any other item's MET stands on its evidence. `defs` is the tree, read once per
    check (parse_acceptance / parse_exercised pass theirs)."""
    if root is None:
        return None
    root = Path(root)
    defs = defs if defs is not None else _Definitions(root)
    claims = list(_LOADED_BY.finditer(str(why or "")))
    if needs_loader and not claims:
        # NO "LOADED BY", BUT THE EVIDENCE MAY STILL NAME THE LOADER (AR-23): a reviewer who
        # wrote `internal/lint/docs.go:RenderReadme` and confirmed the table is generated from
        # the YAML said what loads it. A cited `file:Symbol` that is defined there and reads the
        # item's file holds; failing that, a file the evidence points at (or one it calls) that
        # reads it. Neither: the loader is not named.
        found, _absent = evidence_paths(root, why, defs=defs)
        data = _item_data_files(root, item_text, found)
        if _named_loader(root, why, data, defs) or (
                data and defs.loader_of(data, found, _cited_symbols(root, why, defs))):
            return None
        # FOLLOWED FROM THE CITED CODE (BOSSAPP-1329): the watcher that calls the loader, the
        # generator whose package reads the rulebook, the handler that takes the loaded config.
        cited = _cited_symbols(root, why, defs)
        if defs.reaches(data, _starts(found, cited)):
            return None
        # Or the value the cited code works on is one a caller also passed to the loader.
        if any(defs.fed_by_loader(data, rel, s) for rel, syms in cited.items() for s in syms
               if not _TEST_PATH.search(rel)):
            return None
        return "loader not named: this item's file must be read by the code (LOADED BY)"
    found, _absent = evidence_paths(root, why, defs=defs) if claims else ([], [])
    data = _item_data_files(root, item_text, found) if claims else []
    for m in claims:
        cited, symbol = m.group(1), m.group(2).split(".")[-1]
        if len(symbol) < 3 or symbol in _KEYWORDS:
            return f"the loader it names, {symbol!r}, is not a function or variable name"
        hit = _resolve(root, cited)
        if not hit:
            return f"the loader it names, {cited}, is not in the tree"
        if _TEST_PATH.search(hit):
            return f"the loader it names is a test ({hit}), not the code that loads the file"
        code, bare = defs.of(hit)
        if code is None:
            return f"the loader it names, {hit}, could not be read"
        if not _defines(bare, symbol):
            return f"{hit} does not define {symbol} (only in a comment, only called, or absent)"
        # The claim holds when the named code reads the file, or the code it calls does
        # (`Watch` → `ReloadOnce` → `Load`, BOSSAPP-1329) — followed from its body only.
        if defs.reaches(data, [(hit, symbol)]):
            continue
        if data:
            return (f"{hit} does not name {', '.join(data[:3])} in code that reads it "
                    f"({symbol}'s own body, or the code it calls)")
        return f"{hit} reads no data or config file"
    return None


def parse_acceptance(reply: str, items: list[dict], *, root: Any, exec_log: Any = None,
                     replay: Optional[Mapping[str, Any]] = None,
                     checks: Any = None) -> list[dict[str, Any]]:
    """Each item with the reviewer's status and evidence (the LAST answer for an item wins).

    `exec_log` / `replay` (the outer layer, AA-F): the Acceptance Agent's reply is read by
    `parse_exercised` instead — command evidence verified against the record, HUMAN answers,
    and items a replayed probe proved without a model. Without them: the inner gate, unchanged.

    A MET stands only on evidence: at least one cited path that EXISTS in the tree and is not a
    document (README, *.md, docs/), and wiring that holds (wiring_problem). Otherwise it reads
    as PARTIAL, saying why — an unevidenced or invented claim is what this gate exists to stop.
    Whether an item is a judgement call is the CONTRACT's (the item's `judgement`, set at
    extraction); a reviewer's own "(judgement)" tag changes nothing."""
    if exec_log is not None or replay:
        return parse_exercised(reply, items, root=root, exec_log=exec_log, replay=replay)
    answers: dict[int, tuple[str, str]] = {}
    for m in _ANSWER.finditer(str(reply or "")):
        answers[int(m.group(1))] = (m.group(2).lower(), clip(_norm(m.group(4)), 600))
    out = []
    defs = _Definitions(Path(root)) if root is not None else None  # the tree, read once
    texts = {int(i["n"]): str(i.get("text") or "") for i in items}
    for it in items:
        status, why = answers.get(int(it["n"]), (UNJUDGED, ""))
        # The contract's tags under the floor (contract.floor_tags): what this check acts on.
        it = {**{k: v for k, v in it.items() if k not in _contract.ITEM_TAGS},
              **_contract.floor_tags(it.get("text"), it, where="gate")}
        # A STACK ITEM IS SETTLED PART BY PART (stack_claim); a DEPENDENCY ITEM BY READING THE
        # MANIFESTS (PR #172 review P1). No recorded commands at this door.
        dep = ((_deps.stack_claim(it.get("text"), root) or _deps.claim(it.get("text"), root))
               if status == MET else None)
        if dep is not None:
            verdict_, said = dep
            if verdict_ == "false":
                status, why = PARTIAL, f"{why} ({said})".strip()
            elif verdict_ == "human":
                status, why = HUMAN, f"{why} (needs a person: {said})".strip()
            out.append({**it, "status": status, "evidence": why})
            continue
        if status == MET:
            # "same as ITEM 1" on a near-duplicate: that item's answer is evidence here too.
            cited_why, foreign = same_as(answers, texts, int(it["n"]), why)
            found, absent = evidence_paths(root, cited_why, defs=defs,
                                           item_text=str(it.get("text") or ""))
            code = code_evidence(found, it.get("text"))
            reason = ""
            if not code and it.get("judgement") and defs is not None:
                # A JUDGEMENT ABOUT THE CODEBASE still stands only on evidence (BOSSAPP-1329
                # review): the packages it quotes (`proxy`, `auth`), when the tree has them.
                code = [d for d in (defs.package_dir(m.group(1))
                                    for m in _TICKED_IDENT.finditer(why)) if d]
            if not code and _suite_item(str(it.get("text") or "")) and _suite_run(why):
                # THE ITEM IS THE SUITE RUN ("All tests pass", "`go test -race ./...` passes"):
                # the answer's own test run over the packages is its evidence. Only then — a
                # `go test ./...` never evidences anything else (BOSSAPP-1329 review).
                code = ["(test run)"]
            if not code and not absent and recorded_command_evidence(
                    cited_why, str(it.get("text") or ""), checks):
                # A COMMAND THE RUN RECORDED PASSING, about which the item is: its evidence.
                code = ["(recorded check)"]
            if not code and foreign and not found and not absent:
                # BORROWED, NOT EVIDENCED: it points at an item that says something else.
                out.append({**it, "status": UNJUDGED, "unverified": True,
                            "evidence": f"{why} (not judged: cites another item's evidence, "
                                        f"ITEM {', '.join(map(str, foreign))})".strip()})
                continue
            if not code:
                reason = ("only manifests/lockfiles cited" if found and all(
                              _DECL_PATH.search(f) for f in found)
                          else "only documents cited" if found
                          else f"cited paths not in the tree: {', '.join(absent[:4])}" if absent
                          else "no file evidence cited")
            else:
                reason = wiring_problem(
                    root, cited_why, item_text=str(it.get("text") or ""),
                    needs_loader=bool(it.get("needs_loader")), defs=defs) or ""
            if reason:
                status, why = PARTIAL, f"{why} ({reason})".strip()
        out.append({**it, "status": status, "evidence": why})
    return out


#: An item whose requirement IS the test suite running green (its own words).
_SUITE_ITEM = re.compile(r"(?i)\b(?:all\s+)?tests?\s+(?:all\s+)?pass(?:es)?\b|"
                         r"`?(?:go\s+test|pytest|npm\s+test|mvn\s+test|gradle\s+test|"
                         r"cargo\s+test)\b[^`]*`?\s+passes\b")
#: An answer that ran the suite over the packages (`go test ./...`, `pytest`, `npm test`).
_SUITE_RUN = re.compile(r"(?i)\b(?:go\s+test\b[^`\n]*\./|pytest\b|npm\s+(?:run\s+)?test\b|"
                        r"mvn\s+(?:-\S+\s+)*test\b|gradle\s+test\b|cargo\s+test\b)")


#: Words that only scope or qualify a suite claim ("all packages pass clean with -race").
_SUITE_FILLER = frozenset(
    "all every the and with without across over whole full entire suite unit integration "
    "package packages module modules clean cleanly green locally ci race detector flag run "
    "runs ran test tests passe pass passing failure fail failing zero none must should still "
    "always both also".split())


def _suite_item(text: str) -> bool:
    """Whether the item is ONLY the suite claim (#171 review): its tests clause removed,
    nothing substantive is left. "All tests pass" and "`go test -race ./...` passes" are;
    "Add CSV export of invoices and tests pass" is not — the export needs its own evidence."""
    text = str(text or "")
    if not _SUITE_ITEM.search(text):
        return False
    rest = _SUITE_ITEM.sub(" ", text)
    rest = re.sub(r"`[^`]*`|\./\S*|-\w+", " ", rest)
    return not (_words(rest) - _SUITE_FILLER)


def _suite_run(why: str) -> bool:
    return bool(_SUITE_RUN.search(why))


_RUNNER = re.compile(r"(?i)^(?:go\s+(?:vet|test|build)|(?:python3?\s+-m\s+)?pytest|npm\s+(?:run\s+)?\w+|"
                     r"mvn\s+(?:-\S+\s+)*\w+|gradle\s+\w+|cargo\s+\w+|make\s+\w[\w\-]*)")


#: First words of the commands a build/test chain is made of (the allowlist for splitting one).
_RUNNERS = frozenset("""go npm npx pnpm yarn bun node deno pytest python python3 uv poetry tox make
    cargo mvn ./mvnw gradle ./gradlew dotnet tsc eslint ruff mypy golangci-lint gofmt rustc swift
    bundle rake rspec php composer phpunit mix java javac""".split())
_DRY_RUN = re.compile(r"(?:^|\s)(?:-n|--dry-run|--just-print|--recon)(?:\s|$)")
_SHELL_WORDS = frozenset("if then else elif fi for while until do done case esac in function "
                         "{ } ! [[ ]]".split())


def recorded_command_evidence(why: str, item_text: str, checks: Any) -> list[str]:
    """The commands an answer cites (`go vet ./...`) that BOSS itself recorded passing in this
    run's checks (`checks`: [{command, ok}]; exact match with a recorded command or one part of a plain
    `&&` chain, arguments included), when the
    ITEM is about that command (its runner, "go vet", is in the item's words): a recorded run is
    evidence, a command nobody ran is not, and a `go test` never evidences an item about
    something else (AR-31: "`go vet ./...` and `go test ./...` run clean" read as no evidence)."""
    ran: set[str] = set()
    for chk in checks or []:
        raw = str(chk.get("command") or "")
        command = _ws(raw)
        # STRICT, NOT A SHELL PARSER: only a passed command with nothing that can hide or
        # ignore a part (a comment, `;`, `||`, a pipe, `&`, a subshell, a substitution, a
        # redirection, a quote, a backslash, a newline) credits anything; a recorded
        # `... || true` credits nothing. The checks read the RAW command, before whitespace is
        # normalised, and a part that starts with a shell reserved word is compound syntax.
        if (not chk.get("ok") or not command
                or re.search(r"[#;|&`()<>'\"\\\n\r]|\$\(", raw.replace("&&", " "))
                or any(p.split()[:1] and p.split()[0] in _SHELL_WORDS for p in command.split("&&"))):
            continue
        parts = [_ws(p) for p in command.split("&&") if p.strip()]
        # A dry run ran nothing (`make -n`, `--dry-run`, `--just-print`, `--recon`, go's `-n`).
        if any(_DRY_RUN.search(p) for p in parts):
            continue
        ran.add(command)
        # Its PARTS are credited only when every part starts with a known build/test runner: a
        # builtin or wrapper (`exit 0 &&`, `exec true &&`, `set -n &&`, `eval`, `env`, `sh -c`...)
        # can end or neuter the chain, so such a chain credits only as a whole.
        if all(p.split()[0] in _RUNNERS for p in parts):
            ran.update(parts)
    if not ran:
        return []
    out = []
    for m in re.finditer(r"`([^`\n]{2,120})`", str(why or "")):
        cmd = _ws(m.group(1))
        runner = _RUNNER.match(cmd)
        if cmd in ran and runner and _ws(runner.group(0)).lower() in _ws(item_text).lower():
            out.append(cmd)
    return list(dict.fromkeys(out))


def _advisory(item: Mapping[str, Any]) -> bool:
    """A judgement item (the contract's tag) answered PARTIAL: reported, never failing. A
    MISSING is never advisory."""
    return bool(item.get("judgement")) and item.get("status") == PARTIAL


def judge(items: list[dict], reply: Optional[str], stack: Mapping[str, Any], *,
          root: Any, source: Optional[Mapping[str, Any]] = None,
          dropped: int = 0, summary_line: Optional[str] = "",
          over_cap: int = 0, exec_log: Any = None,
          replay: Optional[Mapping[str, Any]] = None, checks: Any = None) -> dict[str, Any]:
    """The gate's record. It FAILS CLOSED; verdict:
      met       EVERY item MET with evidence in the tree, every declared manifest present, and
                the reviewer's own summary line says MET
      not_met   an item MISSING or PARTIAL, a declared stack's manifest absent, or the reviewer's
                summary contradicting its items (facts — repair, then a checks-failing PR)
      not_run   the reviewer left items unanswered, or the items could not be found (the
                check's fault, not the code's: it is re-run, and never passes)
      no_items  the run states nothing to hold it to (a non-follow-up with no criteria)
    A PARTIAL on an item the CONTRACT tags `judgement` (a matter of taste, decided at
    extraction) is reported and ADVISORY; a MISSING never is, and a reviewer cannot make one. `over_cap`: items beyond MAX_PASSES reviewer passes, not judged — not met.

    THE OUTER LAYER (`exec_log` / `replay`, AA-F): a MET whose evidence BOSS could not verify is
    NOT JUDGED (`unverified`) and an item that needs a person is HUMAN; either makes the check
    not_met, never met (I2) — and the record says how many and why."""
    stack = dict(stack or {})
    source = dict(source or {"root": True})
    # WHICH ITEMS this check judged: a later check clears this one's failure only if it judged
    # at least these (lineage_failures).
    source["item_keys"] = [item_key(i["text"]) for i in items]
    exercised = exec_log is not None or bool(replay)
    judged = (parse_acceptance(reply or "", items, root=root, exec_log=exec_log, replay=replay,
                               checks=checks)
              if items else [])
    m = list(_SUMMARY.finditer(str(reply or "")))
    summary = (" ".join(m[-1].group(1).upper().split()) if m else None)
    if summary_line != "":
        summary = summary_line      # several passes: run_check combined their summary lines
    if exercised and summary is None and not str(reply or "").strip() and judged and all(
            i.get("replayed") for i in judged):
        # EVERY ITEM WAS PROVEN BY REPLAY: no model was asked, so there is no summary line to
        # read; the replayed probes are the answer (each one verified, carried as MET).
        summary = "MET"
    missing = [i["n"] for i in judged if i["status"] == MISSING]
    partial = [i["n"] for i in judged if i["status"] == PARTIAL and not _advisory(i)]
    advisory = [i["n"] for i in judged if _advisory(i)]
    unjudged = [i["n"] for i in judged if i["status"] == UNJUDGED]
    met = [i["n"] for i in judged if i["status"] == MET]
    human = [i["n"] for i in judged if i["status"] == HUMAN]
    unverified = [i["n"] for i in judged if i.get("unverified")]
    why = ""
    if stack.get("missing") or missing or partial:
        verdict = "not_met"
    elif unverified or human:
        verdict = "not_met"
        why = "; ".join(x for x in (
            f"{len(unverified)} items claimed MET with evidence BOSS could not verify"
            if unverified else "",
            f"{len(human)} items need a person" if human else "") if x)
    elif over_cap:
        verdict, why = "not_met", (f"{over_cap} items not judged (beyond {MAX_PASSES} reviewer "
                                   f"passes of {MAX_ITEMS})")
    elif not source.get("root"):
        verdict, why = "not_run", str(source.get("why") or "the must-haves were not found")
    elif not items:
        verdict = "no_items"
    elif unjudged:
        verdict, why = "not_run", f"{len(unjudged)} of {len(items)} items were not answered"
    elif dropped:
        # Items nobody judged are not met: never a pass over a list that was cut.
        verdict, why = "not_run", f"{dropped} items beyond {MAX_TOTAL_ITEMS} were not judged"
    elif summary == "NOT MET" and advisory:
        # The reviewer's NOT MET is about the advisory (taste) items only: every other item
        # is met. Not a slip — the check is met, with advisory items said on the card.
        verdict = "met"
    elif summary != "MET":
        # A reviewer's slip, not the code's: re-ask the reviewer (HARNESS), never send the
        # coder to repair it. Still never a pass.
        verdict = "not_run"
        why = ("the reviewer's summary says NOT MET although every item reads met"
               if summary == "NOT MET" else "the reviewer gave no ACCEPTANCE summary line")
    else:
        verdict = "met"
    record = {"verdict": verdict, "why": why, "summary_line": summary, "items": judged,
              "missing": missing, "partial": partial, "unjudged": unjudged, "met": met,
              "advisory": advisory, "items_not_judged": int(over_cap or 0),
              "stack": stack, "source": source, "items_dropped": int(dropped or 0),
              "counts": {s: sum(1 for i in judged if i["status"] == s)
                         for s in (MET, PARTIAL, MISSING, UNJUDGED)}}
    if exercised:
        record["counts"][HUMAN] = len(human)
        record.update({"human": human, "unverified": unverified,
                       "replayed": [i["n"] for i in judged if i.get("replayed")],
                       "mode": "exercise"})
    return record


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
                         (UNJUDGED, "not judged"), (HUMAN, "needs a person")):
        for i in record.get("items") or []:
            if i.get("status") == status and not _advisory(i):
                lines.append(f"FINDING: MAJOR — Must-have {i['n']} {word}: {i['text']}"
                             + (f" — {i['evidence']}" if i.get("evidence") else ""))
    if record.get("why"):
        lines.append(f"FINDING: MAJOR — Acceptance check: {record['why']}.")
    for note in stack.get("notes") or []:
        lines.append(f"Note: {note}.")
    for i in record.get("items") or []:
        if _advisory(i):
            lines.append(f"Note: must-have {i['n']} is a judgement call ({i['status']}, "
                         f"advisory): {i['text']}")
    if record.get("items_dropped"):
        lines.append(f"Note: {record['items_dropped']} further items beyond the first "
                     f"{MAX_TOTAL_ITEMS} were not checked.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------------------------
# The outer layer: command evidence (AA-F, LLD §4.5, I6)
# ---------------------------------------------------------------------------------------------

#: The Acceptance Agent's answer line: `AC-3: MET — evidence: E4 (exit 0) "HTTP/1.1 401"`, or
#: the inner gate's `ITEM 3: MET — ...`; HUMAN is the fifth answer (a machine cannot judge it).
_ANSWER_OUTER = re.compile(
    r"(?im)^[\s>*\-•]*\**\s*(?:AC-(\d+)|ITEM\s*#?\s*(\d+))\s*\**\s*[:.)\-]\s*\**\s*"
    r"(MET|PARTIAL|MISSING|HUMAN)\b\**\s*(?:[—–:\-]+\s*)?(.*)$")
#: A command cited as evidence: E<id> as the session's record numbered it.
_EXEC_REF = re.compile(r"(?<![\w\-/.])E(\d{1,5})\b")
_QUOTED = re.compile(r'"([^"]{1,600})"|“([^”]{1,600})”|`([^`]{1,600})`')
_EXIT_CLAIM = re.compile(r"(?i)\bexit(?:\s*code)?\s*[=:]?\s*(-?\d{1,3})\b")
_STATUS_CLAIM = re.compile(r"(?i)\b(?:HTTP(?:/\d(?:\.\d)?)?|status)\s*[=:]?\s*([1-5]\d\d)\b")
_LINE_CITE = re.compile(r"(?<![\w/.-])([\w.\-]+(?:/[\w.\-]+)*\.[A-Za-z][\w]{0,8}|[\w.\-]+(?:/[\w.\-]+)+)"
                        r":(\d{1,6})\b")


def _field(cmd: Any, name: str, default: Any = None) -> Any:
    if cmd is None:
        return default
    if isinstance(cmd, Mapping):
        return cmd.get(name, default)
    return getattr(cmd, name, default)


def _ws(text: Any) -> str:
    return " ".join(str(text or "").split())


def _lookup(exec_log: Any, ref: str) -> Any:
    if exec_log is None:
        return None
    getter = getattr(exec_log, "get", None)
    if callable(getter):
        return getter(ref)
    for cmd in exec_log or []:
        if _field(cmd, "id") == ref:
            return cmd
    return None


def exec_claims(why: str) -> list[dict[str, Any]]:
    """Each command an answer cites: {ref: "E4", excerpt, exit (claimed or None), status}. The
    excerpt, exit and status are read from the text between this E-id and the next one."""
    text = str(why or "")
    hits = list(_EXEC_REF.finditer(text))
    out = []
    for k, m in enumerate(hits):
        seg = text[m.end(): hits[k + 1].start() if k + 1 < len(hits) else len(text)]
        q = _QUOTED.search(seg)
        excerpt = next((g for g in q.groups() if g), None) if q else None
        e = _EXIT_CLAIM.search(seg)
        st = _STATUS_CLAIM.search(seg)
        out.append({"ref": f"E{int(m.group(1))}", "excerpt": excerpt,
                    "exit": int(e.group(1)) if e else None,
                    "status": int(st.group(1)) if st else None})
    return out


#: The shortest quote that counts: "401" or "ok" alone appears in almost any output.
MIN_EXCERPT = 6
#: Interpreters whose inline-code flag makes the command line itself the output's author, by
#: family: (the option letters / names that take code, the options whose NEXT word is a value,
#: the options that end option parsing because what follows is the program — python's -m).
_FAMILIES: dict[str, tuple[frozenset, frozenset, frozenset]] = {
    "python": (frozenset({"c"}), frozenset({"-W", "-X", "--check-hash-based-pycs"}),
               frozenset({"-m"})),
    "node": (frozenset({"e", "p", "--eval", "--print"}),
             frozenset({"-r", "--require", "--import", "--loader", "--experimental-loader",
                        "--input-type", "-C", "--conditions", "--env-file"}), frozenset()),
    "shell": (frozenset({"c"}), frozenset({"-o", "+o", "-O", "+O"}), frozenset()),
    "ruby": (frozenset({"e"}), frozenset({"-I", "-r", "-C"}), frozenset()),
    "perl": (frozenset({"e", "E"}), frozenset({"-I", "-M", "-m"}), frozenset()),
    "php": (frozenset({"r", "B", "R", "E"}), frozenset({"-c", "-d", "-z"}), frozenset()),
    "lua": (frozenset({"e"}), frozenset({"-l"}), frozenset()),
    "rscript": (frozenset({"e"}), frozenset(), frozenset()),
    "pwsh": (frozenset({"c", "-Command", "-command", "-EncodedCommand", "-e"}), frozenset(),
             frozenset()),
    "osascript": (frozenset({"e"}), frozenset(), frozenset()),
}
_FAMILY_OF = (
    (re.compile(r"^(python[\d.]*|pypy[\d.]*)$"), "python"),
    (re.compile(r"^(node(js)?|deno|bun)$"), "node"),
    (re.compile(r"^(sh|bash|dash|zsh|ksh|fish|ash)$"), "shell"),
    (re.compile(r"^ruby$"), "ruby"), (re.compile(r"^perl[\d.]*$"), "perl"),
    (re.compile(r"^php[\d.]*$"), "php"), (re.compile(r"^lua[\d.]*$"), "lua"),
    (re.compile(r"^Rscript$"), "rscript"), (re.compile(r"^(pwsh|powershell)$"), "pwsh"),
    (re.compile(r"^osascript$"), "osascript"),
)
_INTERPRETERS = re.compile("|".join(f"(?:{r.pattern})" for r, _ in _FAMILY_OF))


def inline_code(argv: list) -> Optional[str]:
    """Why this command line authors its own output, or None: a printer (echo, printf), or an
    interpreter given code on its command line (`python3 -c`, `node -pe`, `sh -xc`). Only the
    INTERPRETER'S OWN options are read — parsing stops at its program (a script path, or
    python's `-m <module>`), so `python3 -m pytest -q -p no:cacheprovider` is a test run, not
    inline code (BOSSAPP-1317)."""
    if not argv:
        return None
    prog = Path(str(argv[0])).name
    if prog in _PRINTERS:
        return f"{prog} prints what it is told"
    family = next((f for r, f in _FAMILY_OF if r.match(prog)), None)
    if family is None:
        return None
    code, takes_value, stops = _FAMILIES[family]
    args = [str(a) for a in argv[1:]]
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--" or a in stops or a == "-" or not a.startswith(("-", "+")):
            return None                       # the program starts here
        name = a.split("=", 1)[0]
        if a in code or name in code:
            return f"{prog} ran code written on its own command line"
        if a in takes_value:
            i += 2
            continue
        if any(a.startswith(v) and len(a) > len(v) and not v.startswith("--")
               for v in takes_value):
            i += 1                            # a value attached (-Wignore, -Ilib)
            continue
        if (not a.startswith("--") and a[0] == "-" and len(a) > 1 and a[1:].isalpha()
                and set(a[1:]) & {c for c in code if len(c) == 1}):
            return f"{prog} ran code written on its own command line"
        i += 1
    return None


#: Commands whose output is whatever they were told to print.
_PRINTERS = frozenset({"echo", "printf", "yes", "true", "false"})


#: Why a command run after the session edited the clone is not evidence (and the mark that
#: turns such a MET into HUMAN: parse_exercised).
EDITED = "the session had changed the clone before it ran"


def manufactured(cmd: Any) -> Optional[str]:
    """Why a recorded command's output cannot be evidence of the work, or None: an inline-code
    interpreter (`python3 -c "print('401')"`, `node -e`, `sh -c`) or a printer (echo, printf)
    writes whatever the agent asked it to; and a command run after the session changed the
    clone (a file it wrote, an edit) — or when BOSS could not tell — reads the agent's words,
    not the delivered work (`tree_dirty`, recorded by the session)."""
    import shlex

    argv = list(_field(cmd, "argv") or [])
    if not argv and _field(cmd, "kind") != "http":
        try:
            argv = shlex.split(str(_field(cmd, "cmd") or ""))
        except ValueError:
            return "the command could not be read"
    made = inline_code(argv)
    if made:
        return made
    plain = _field(cmd, "plain")
    if plain is False:
        return ("not a plain call into the delivered work"
                + (f" ({_field(cmd, 'not_plain')})" if _field(cmd, "not_plain") else ""))
    if plain is None:
        return "BOSS did not classify it as a plain call into the delivered work"
    dirty = _field(cmd, "tree_dirty")
    if dirty is None:
        return "BOSS could not tell whether the session had changed the clone"
    if dirty:
        return EDITED            # a visible edit: an edit-then-test item, a person's (HUMAN)
    tainted = _field(cmd, "tainted_by")
    if tainted:
        # A HIDDEN plant: the tracked tree is clean, but a code-writing command changed what
        # the work reads (a gitignored .env, node_modules, HOME, temp) — BOSSAPP-1316.
        return (f"an earlier command ({tainted}) that wrote its own code changed files outside "
                "the tracked tree (ignored files, HOME or temp) that later commands may read")
    return None


def verify_command(claim: Mapping[str, Any], exec_log: Any) -> tuple[bool, str]:
    """(verified, why not). BOSS checks a cited command against the record IT kept — never the
    model's prose (I6): the command ran in this check, the quoted words are in its output
    (whitespace-normalised), and its exit code — the one claimed, else 0, since a MET cites a
    command that worked — and its HTTP status, when one is claimed, are what it really had."""
    ref = str(claim.get("ref") or "")
    cmd = _lookup(exec_log, ref)
    if cmd is None:
        return False, f"{ref} is not a command this check ran"
    if _field(cmd, "refused"):
        return False, f"{ref} was refused and never ran"
    excerpt = _ws(claim.get("excerpt"))
    if not excerpt:
        return False, f"{ref} cited without the output it showed"
    if len(excerpt) < MIN_EXCERPT:
        return False, f"the quote from {ref} is too short to prove anything"
    made = manufactured(cmd)
    if made:
        return False, f"{ref} is not evidence: {made}"
    output = _ws(f"{_field(cmd, 'stdout', '')}\n{_field(cmd, 'stderr', '')}")
    if excerpt not in output:
        return False, f"the quoted words are not in {ref}'s output"
    if _field(cmd, "timed_out"):
        return False, f"{ref} timed out"
    want_exit = claim.get("exit")
    want_exit = 0 if want_exit is None else int(want_exit)
    got_exit = _field(cmd, "exit")
    if got_exit is None or int(got_exit) != want_exit:
        return False, f"{ref} exited {got_exit}, not {want_exit}"
    if claim.get("status") is not None:
        got = _field(cmd, "status")
        if got is None or int(got) != int(claim["status"]):
            return False, f"{ref} answered HTTP {got}, not {claim['status']}"
    return True, ""


def _file_refs(root: Any, why: str, *, defs: Optional[_Definitions] = None,
               item_text: Optional[str] = None) -> tuple[list[dict[str, Any]], str]:
    """Verified file evidence of an answer and why there is none: a cited path in the tree that
    is code, configuration or a test (never a document or manifest alone), and a cited
    `path:line` whose line exists. `defs`: the tree, read once per check."""
    found, absent = evidence_paths(root, why, defs=defs, item_text=item_text)
    code = code_evidence(found, item_text)
    bad_lines = set()
    for m in _LINE_CITE.finditer(str(why or "")):
        hit = _resolve(Path(root), m.group(1)) if root is not None else None
        if hit and hit in code:
            try:
                n = len((Path(root) / hit).read_text(encoding="utf-8", errors="ignore")
                        .splitlines())
            except OSError:
                n = 0
            if int(m.group(2)) > n or int(m.group(2)) < 1:
                bad_lines.add(hit)
    code = [f for f in code if f not in bad_lines]
    if code:
        return [{"kind": "file", "ref": f, "verified": True} for f in code], ""
    return [], ("a cited line is past the end of its file" if bad_lines
                else "only manifests/lockfiles cited" if found and all(
                    _DECL_PATH.search(f) for f in found)
                else "only documents cited" if found
                else f"cited paths not in the tree: {', '.join(absent[:4])}" if absent
                else "no evidence cited")


def parse_exercised(reply: str, items: list[dict], *, root: Any, exec_log: Any = None,
                    replay: Optional[Mapping[str, Any]] = None) -> list[dict[str, Any]]:
    """The Acceptance Agent's answers, every MET held to evidence BOSS verifies (LLD §4.5):

      replayed  an item whose recorded probe passed again (session replay, no model) and whose
                files did not change: MET, evidence kind 'replay', verified.
      MET       citing commands: EVERY cited command must verify against the record
                (verify_command) — one fabricated, misquoted or wrong-exit citation and the item
                is NOT JUDGED (`unverified`), never met. Citing only files: the files must be
                in the tree as code (never a README), lines must exist, and a loader the item
                needs must hold (wiring_problem: a wiring gap is PARTIAL, a real gap).
      HUMAN     a machine cannot judge it: never met.
    `evidence` stays the answer's words (the card's column); `evidence_refs` is what BOSS
    verified."""
    answers: dict[int, tuple[str, str]] = {}
    by_id = {str(i.get("id")): int(i["n"]) for i in items if i.get("id")}
    for m in _ANSWER_OUTER.finditer(str(reply or "")):
        n = by_id.get(f"AC-{m.group(1)}") if m.group(1) else int(m.group(2))
        if n is None and m.group(1):
            n = int(m.group(1))
        answers[int(n)] = (m.group(3).lower(), clip(_norm(m.group(4)), 600))
    replay = dict(replay or {})
    out = []
    defs = _Definitions(Path(root)) if root is not None else None  # the tree, read once
    texts = {int(i["n"]): str(i.get("text") or "") for i in items}
    for it in items:
        key = str(it.get("id") or it["n"])
        it = {**{k: v for k, v in it.items() if k not in _contract.ITEM_TAGS},
              **_contract.floor_tags(it.get("text"), it, where="gate")}
        carried = replay.get(key) or replay.get(str(it["n"]))
        if carried and carried.get("result") == "pass" and int(it["n"]) not in answers:
            cmds = list(carried.get("command_ids") or [])
            out.append({**it, "status": MET, "replayed": True,
                        "evidence": f"replayed: {', '.join(cmds)} passed again "
                                    f"(probe of check {carried.get('check_id') or 'before'})",
                        "evidence_refs": [{"kind": "replay", "ref": c, "verified": True}
                                          for c in cmds]})
            continue
        status, why = answers.get(int(it["n"]), (UNJUDGED, ""))
        row: dict[str, Any] = {**it, "status": status, "evidence": why, "evidence_refs": []}
        dep = None
        if status == MET:
            # A STACK ITEM PART BY PART, its build property proven by a command BOSS recorded and
            # verified; a DEPENDENCY ITEM by reading the manifests (PR #172 review P1).
            ran = [_lookup(exec_log, c["ref"]) for c in exec_claims(why)
                   if verify_command(c, exec_log)[0]]
            dep = (_deps.stack_claim(it.get("text"), root, commands=ran)
                   or _deps.claim(it.get("text"), root))
        if dep is not None:
            verdict_, said = dep
            if verdict_ == "met":
                row["evidence_refs"] = [{"kind": "manifest", "ref": said, "verified": True}]
            elif verdict_ == "false":
                row.update(status=PARTIAL, evidence=f"{why} ({said})".strip())
            else:
                row.update(status=HUMAN, evidence=f"{why} (needs a person: {said})".strip())
            out.append(row)
            continue
        if status == MET:
            claims = exec_claims(why)
            refs: list[dict[str, Any]] = []
            failed: list[str] = []
            for c in claims:
                ok, problem = verify_command(c, exec_log)
                refs.append({"kind": "exec", "ref": c["ref"], "excerpt": c.get("excerpt"),
                             "verified": ok, **({"why": problem} if problem else {})})
                if not ok:
                    failed.append(problem)
            cited_why, foreign = same_as(answers, texts, int(it["n"]), why)
            files, no_file = (_file_refs(root, cited_why, defs=defs,
                                         item_text=str(it.get("text") or ""))
                              if root is not None
                              else ([], "no tree"))
            row["evidence_refs"] = refs + files
            reason = ""
            if failed and all(EDITED in f for f in failed):
                # ONLY PROVABLE BY EDITING A FILE (hot reload, a config change): BOSS has no
                # supported edit-then-test probe yet, so it is a person's to confirm — HUMAN
                # (a minor ledger row), never a major "unverified" gap.
                row["status"] = HUMAN
                reason = ("needs a person: it was tested by editing the clone, and an "
                          "edit-then-test check is not supported yet")
            elif failed:
                row.update(status=UNJUDGED, unverified=True)
                reason = "evidence not verified: " + "; ".join(failed[:3])
            elif not refs and not files and foreign and no_file == "no evidence cited":
                row.update(status=UNJUDGED, unverified=True)
                reason = ("not judged: cites another item's evidence, ITEM "
                          + ", ".join(map(str, foreign)))
            elif not refs and not files:
                row.update(status=UNJUDGED, unverified=True)
                reason = f"no verifiable evidence ({no_file})"
            elif files and not refs:
                wiring = wiring_problem(root, cited_why, item_text=str(it.get("text") or ""),
                                        needs_loader=bool(it.get("needs_loader")), defs=defs)
                if wiring:
                    row["status"], reason = PARTIAL, wiring
            if reason:
                row["evidence"] = f"{why} ({reason})".strip()
        out.append(row)
    return out


def diff(prev: Optional[Mapping[str, Any]], cur: Mapping[str, Any]) -> dict[str, list[str]]:
    """What changed since the last check, by item id (LLD §4.5): newly_met, regressed (met
    before, not now), still_open (not met in either)."""
    def status(rec: Optional[Mapping[str, Any]]) -> dict[str, str]:
        return {str(i.get("id") or i.get("n")): str(i.get("status"))
                for i in (rec or {}).get("items") or []}
    before, now = status(prev), status(cur)
    return {"newly_met": [k for k, v in now.items() if v == MET and before.get(k) not in (None, MET)],
            "regressed": [k for k, v in now.items() if v != MET and before.get(k) == MET],
            "still_open": [k for k, v in now.items() if v != MET and k in before
                           and before[k] != MET]}

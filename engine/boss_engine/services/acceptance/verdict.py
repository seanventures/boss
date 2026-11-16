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
#: A bare word that may be an extensionless file (Makefile, Dockerfile, LICENSE, Procfile): it
#: counts as evidence only when a file of exactly that name is in the tree.
_BARE = re.compile(r"(?<![\w/.\-])([A-Za-z][\w\-]{2,})(?![\w/.\-])")
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


def _extensionless(root: Path) -> dict[str, list[str]]:
    """The tree's files with no extension (Makefile, Dockerfile, LICENSE), by name."""
    out: dict[str, list[str]] = {}
    for p in _walk(root):
        if "." not in p.name:
            out.setdefault(p.name, []).append(str(p.relative_to(root)))
    return out


def evidence_paths(root: Any, why: str, *,
                   defs: Optional["_Definitions"] = None) -> tuple[list[str], list[str]]:
    """(cited paths that exist in the tree, cited paths that do not). A bare extensionless
    name (Makefile) counts when exactly one file of that name is in the tree. A cited symbol
    (`auth.VerifyJWT`, `Store.Swap`) counts as the file that defines it (_Definitions)."""
    if root is None:
        return [], []
    found, absent = [], []
    defs = defs if defs is not None else _Definitions(Path(root))
    for m in _PATHISH.finditer(str(why or "")):
        cited = m.group(1)
        if re.fullmatch(r"\d+(\.\d+)*", cited):
            continue
        # A path, else a symbol the way its language writes it (`auth.VerifyJWT`,
        # `Store.Swap`, `module.func`): the file that DEFINES it is the evidence (AR-23 —
        # "cited paths not in the tree: auth.VerifyJWT" marked real Go evidence partial).
        hit = _resolve(Path(root), cited) or defs.symbol_file(cited)
        (found if hit else absent).append(hit or cited)
    for m in _QUALIFIED.finditer(str(why or "")):
        # A qualified name too long to look like a path (`proxy.pathMatches`,
        # `session.createSession`): evidence when the tree defines it, never "absent".
        if m.group(1) not in absent:
            hit = defs.symbol_file(m.group(1))
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
        if folder != "." and _suffix_of(folder, lit):
            folder_named = True
            if _folder_read(code, m_lits.get(lit, [])):
                return True
    return folder_named and name_glob


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
        bound = re.search(r"(?:\b(?:const|var|let|val)\s+)?([A-Za-z_]\w*)\s*(?::=|=)\s*$",
                          before) or re.search(r"\b([A-Za-z_]\w*)\s*=\s*$", before)
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
            if any(q in {*path.parts[:-1], path.stem} or _defines(bare, q) for q in quals):
                return rel
        return None

    def loader_of(self, data: list[str], among: Iterable[str]) -> Optional[str]:
        """The file that READS one of the data files (`_reads`), looked for only where the
        evidence points: a cited code file, a file defining a symbol the evidence cites (both
        already in `among`), or a file defining a qualified function one of those calls
        (`rules.Load()` called from the cited docs.go). Never any file in the tree that merely
        mentions the data file or its folder (AR-23 review). None when there is none."""
        cands = [r for r in dict.fromkeys(among) if _CODE_FILE.search(r)
                 and not _TEST_PATH.search(r)]
        hops: list[str] = []
        for rel in cands:
            code, _ = self.of(rel)
            if code is None:
                continue
            if any(d != rel and _reads(code, d, rel) for d in data):
                return rel
            for m in list(_CALL.finditer(code))[:40]:
                hit = self.symbol_file(f"{m.group(1)}.{m.group(2)}")
                if hit and hit not in cands and hit not in hops:
                    hops.append(hit)
        for rel in hops:
            code, _ = self.of(rel)
            if code is not None and any(d != rel and _reads(code, d, rel) for d in data):
                return rel
        return None


#: A qualified call in code (`rules.Load(`, `loader.read_rules(`).
_CALL = re.compile(r"\b([A-Za-z_]\w*)\.([A-Za-z_]\w*)\s*\(")


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
        if (any(d != hit and _reads(code, d, hit) for d in data) if data
                else _reads_data(code)):
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
        if _named_loader(root, why, data, defs) or (data and defs.loader_of(data, found)):
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
        if data:
            missing = [d for d in data if d != hit and not _reads(code, d, hit)]
            if len(missing) == len(data):
                return f"{hit} does not name {', '.join(missing[:3])} in code that reads it"
        elif not _reads_data(code):
            return f"{hit} reads no data or config file"
    return None


def parse_acceptance(reply: str, items: list[dict], *, root: Any, exec_log: Any = None,
                     replay: Optional[Mapping[str, Any]] = None) -> list[dict[str, Any]]:
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
        answers[int(m.group(1))] = (m.group(2).lower(), _norm(m.group(4))[:600])
    out = []
    defs = _Definitions(Path(root)) if root is not None else None  # the tree, read once
    for it in items:
        status, why = answers.get(int(it["n"]), (UNJUDGED, ""))
        # The contract's tags under the floor (contract.floor_tags): what this check acts on.
        it = {**{k: v for k, v in it.items() if k not in _contract.ITEM_TAGS},
              **_contract.floor_tags(it.get("text"), it, where="gate")}
        if status == MET:
            found, absent = evidence_paths(root, why, defs=defs)
            code = [f for f in found if not _DOC_PATH.search(f) and not _DECL_PATH.search(f)]
            reason = ""
            if not code:
                reason = ("only manifests/lockfiles cited" if found and all(
                              _DECL_PATH.search(f) for f in found)
                          else "only documents cited" if found
                          else f"cited paths not in the tree: {', '.join(absent[:4])}" if absent
                          else "no file evidence cited")
            else:
                reason = wiring_problem(
                    root, why, item_text=str(it.get("text") or ""),
                    needs_loader=bool(it.get("needs_loader")), defs=defs) or ""
            if reason:
                status, why = PARTIAL, f"{why} ({reason})".strip()
        out.append({**it, "status": status, "evidence": why})
    return out


def _advisory(item: Mapping[str, Any]) -> bool:
    """A judgement item (the contract's tag) answered PARTIAL: reported, never failing. A
    MISSING is never advisory."""
    return bool(item.get("judgement")) and item.get("status") == PARTIAL


def judge(items: list[dict], reply: Optional[str], stack: Mapping[str, Any], *,
          root: Any, source: Optional[Mapping[str, Any]] = None,
          dropped: int = 0, summary_line: Optional[str] = "",
          over_cap: int = 0, exec_log: Any = None,
          replay: Optional[Mapping[str, Any]] = None) -> dict[str, Any]:
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
    judged = (parse_acceptance(reply or "", items, root=root, exec_log=exec_log, replay=replay)
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
#: Interpreters whose inline-code flag makes the command line itself the output's author.
_INTERPRETERS = re.compile(r"^(python[\d.]*|pypy[\d.]*|node(js)?|deno|bun|ruby|perl|php|"
                           r"sh|bash|dash|zsh|ksh|fish|pwsh|powershell|osascript|lua|Rscript)$")
_INLINE_FLAGS = frozenset({"-c", "-e", "--eval", "-p", "--print", "-r", "--command",
                           "-Command", "-E"})
#: Commands whose output is whatever they were told to print.
_PRINTERS = frozenset({"echo", "printf", "yes", "true", "false"})


def _inline_flag(arg: str) -> bool:
    """`-c`, `-e`, `--eval`, or a short-flag cluster carrying one (`-xc`, `-pe`). Fails closed:
    a harmless cluster that happens to hold one of those letters is refused too."""
    if arg in _INLINE_FLAGS:
        return True
    return (arg.startswith("-") and not arg.startswith("--") and len(arg) > 1
            and arg[1:].isalpha() and bool(set(arg[1:]) & {"c", "e", "p"}))


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
    if argv:
        prog = Path(str(argv[0])).name
        if prog in _PRINTERS:
            return f"{prog} prints what it is told"
        if _INTERPRETERS.match(prog) and any(_inline_flag(a) for a in argv[1:]):
            return f"{prog} ran code written on its own command line"
    dirty = _field(cmd, "tree_dirty")
    if dirty is None:
        return "BOSS could not tell whether the session had changed the clone"
    if dirty:
        return EDITED
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


def _file_refs(root: Any, why: str, *,
               defs: Optional[_Definitions] = None) -> tuple[list[dict[str, Any]], str]:
    """Verified file evidence of an answer and why there is none: a cited path in the tree that
    is code, configuration or a test (never a document or manifest alone), and a cited
    `path:line` whose line exists. `defs`: the tree, read once per check."""
    found, absent = evidence_paths(root, why, defs=defs)
    code = [f for f in found if not _DOC_PATH.search(f) and not _DECL_PATH.search(f)]
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
        answers[int(n)] = (m.group(3).lower(), _norm(m.group(4))[:600])
    replay = dict(replay or {})
    out = []
    defs = _Definitions(Path(root)) if root is not None else None  # the tree, read once
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
            files, no_file = (_file_refs(root, why, defs=defs) if root is not None
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
            elif not refs and not files:
                row.update(status=UNJUDGED, unverified=True)
                reason = f"no verifiable evidence ({no_file})"
            elif files and not refs:
                wiring = wiring_problem(root, why, item_text=str(it.get("text") or ""),
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

"""Static code extraction with tree-sitter (KNOWLEDGE_GRAPH_SPEC.md §3.3, Phase 1).

One file in, one :class:`FileFacts` out: what the file defines, what it imports, every name it
refers to (with the definition it was referred from), and the framework facts static analysis
can see — HTTP endpoints, database tables, queues and webhooks, configuration keys. Nothing here
touches the database or resolves names across files; `code_index.py` does that over the whole
repository, so a file's facts can be cached by content hash and re-parsed alone on a commit.

Languages: Python, TypeScript/JavaScript, Go, Java, C#. Framework patterns (Django/FastAPI/Flask,
Express, net/http and gin, Spring, ASP.NET; Celery; SQL in strings; env/settings reads) are hints
for static extraction, not rules any agent reasons with.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import PurePosixPath

EXTENSIONS = {
    ".py": "python", ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".ts": "typescript", ".tsx": "tsx", ".go": "go", ".java": "java", ".cs": "csharp",
}
MAX_FILE_BYTES = 1_000_000          # generated bundles and fixtures are not code to index
HTTP_VERBS = ("get", "post", "put", "patch", "delete", "head", "options")
SQL_READ = re.compile(r"\b(?:FROM|JOIN)\s+\"?([A-Za-z_][A-Za-z0-9_]*)\"?", re.I)
SQL_WRITE = re.compile(r"\b(?:INSERT\s+INTO|DELETE\s+FROM)\s+\"?([A-Za-z_][A-Za-z0-9_]*)\"?"
                       r"|\bUPDATE\s+\"?([A-Za-z_][A-Za-z0-9_]*)\"?\s+SET\b", re.I)
# A string is treated as SQL only when it has SQL's shape, not merely an English verb in it.
SQL_SHAPE = re.compile(r"\bSELECT\b[\s\S]+?\bFROM\b|\bINSERT\s+INTO\b|\bUPDATE\s+\S+\s+SET\b|\bDELETE\s+FROM\b", re.I)
ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]{2,}$")
SQL_NOT_TABLES = {"select", "where", "set", "values", "the", "a", "an", "lateral", "unnest", "only", "dual"}


@dataclass
class Def:
    qualname: str          # "Order", "Order.save", "complete_checkout"
    kind: str              # function | class | method | property
    line: int
    end_line: int


@dataclass
class Import:
    alias: str             # the local name ("" for a side-effect import)
    module: str            # the imported module, resolved against the file for relative imports
    name: str | None       # an imported member, or None when the module itself is bound


@dataclass
class Ref:
    src: str               # qualname of the enclosing definition; "" at file level
    chain: tuple[str, ...] # ("checkout", "complete_checkout") for checkout.complete_checkout
    call: bool
    line: int


@dataclass
class FileFacts:
    path: str
    lang: str
    module: str
    content_hash: str
    lines: int
    defs: list[Def] = field(default_factory=list)
    imports: list[Import] = field(default_factory=list)
    refs: list[Ref] = field(default_factory=list)
    endpoints: list[tuple[str, str, str]] = field(default_factory=list)   # (src, METHOD, route)
    tables: list[tuple[str, str, str]] = field(default_factory=list)      # (src, table, reads|writes|defines)
    queues: list[tuple[str, str, str]] = field(default_factory=list)      # (src, queue, publishes|subscribes)
    config: list[tuple[str, str]] = field(default_factory=list)           # (src, KEY)
    error: str | None = None


def language_for(path: str) -> str | None:
    return EXTENSIONS.get(PurePosixPath(path).suffix.lower())


@lru_cache(maxsize=None)
def _parser(lang: str):
    import tree_sitter as ts

    if lang == "python":
        import tree_sitter_python as m
        capsule = m.language()
    elif lang == "javascript":
        import tree_sitter_javascript as m
        capsule = m.language()
    elif lang in ("typescript", "tsx"):
        import tree_sitter_typescript as m
        capsule = m.language_tsx() if lang == "tsx" else m.language_typescript()
    elif lang == "go":
        import tree_sitter_go as m
        capsule = m.language()
    elif lang == "java":
        import tree_sitter_java as m
        capsule = m.language()
    elif lang == "csharp":
        import tree_sitter_c_sharp as m
        capsule = m.language()
    else:
        raise ValueError(lang)
    return ts.Parser(ts.Language(capsule))


def module_name(path: str, lang: str) -> str:
    """A file's module: dotted for Python, the extensionless path for JS/TS, the directory for Go.
    Java and C# use the package/namespace declared in the file (filled in while parsing)."""
    p = PurePosixPath(path)
    if lang == "python":
        parts = list(p.with_suffix("").parts)
        if parts and parts[-1] == "__init__":
            parts = parts[:-1]
        return ".".join(parts)
    if lang in ("javascript", "typescript", "tsx"):
        stem = str(p.with_suffix(""))
        return stem[: -len("/index")] if stem.endswith("/index") else stem
    if lang == "go":
        return str(p.parent)
    return str(p.with_suffix("")).replace("/", ".")


def _same(a, b) -> bool:
    """tree-sitter hands out a new wrapper per access, so compare positions, never identity."""
    return a is not None and b is not None and a.type == b.type and a.start_byte == b.start_byte \
        and a.end_byte == b.end_byte


def _text(node) -> str:
    return node.text.decode("utf-8", "replace") if node is not None else ""


def _string_value(node) -> str | None:
    """The literal value of a string node in any of the five grammars."""
    if node is None:
        return None
    t = node.type
    if t in ("string", "string_literal", "interpreted_string_literal", "raw_string_literal",
             "verbatim_string_literal", "template_string"):
        raw = _text(node)
        if t == "string" and node.children and node.children[0].type == "string_start":
            raw = "".join(_text(c) for c in node.children if c.type == "string_content")
            return raw
        return raw.strip("`\"'@").strip("\"'")
    return None


def _chain(node, attr_types: tuple[str, ...], ident_types: tuple[str, ...], obj_field: str,
           prop_field: str) -> tuple[str, ...] | None:
    """a.b.c as ("a", "b", "c") when built only from identifiers; None otherwise."""
    parts: list[str] = []
    while node is not None and node.type in attr_types:
        prop = node.child_by_field_name(prop_field)
        if prop is None:
            return None
        parts.append(_text(prop))
        node = node.child_by_field_name(obj_field)
    if node is None or node.type not in ident_types:
        return None
    parts.append(_text(node))
    return tuple(reversed(parts))


class _Extractor:
    """Shared walk: definitions open scopes; every reference is recorded against the innermost one."""

    lang = ""
    def_types: dict[str, str] = {}
    attr_types: tuple[str, ...] = ()
    ident_types: tuple[str, ...] = ()
    call_types: tuple[str, ...] = ()
    obj_field = "object"
    prop_field = "attribute"
    call_fn_field = "function"
    skip_ident_parents: frozenset[str] = frozenset()

    def __init__(self, facts: FileFacts, source: bytes):
        self.f = facts
        self.source = source
        self.scope: list[str] = []

    @property
    def src(self) -> str:
        return ".".join(self.scope)

    def run(self, root) -> None:
        self.walk(root)

    # Hooks per language -------------------------------------------------------------------------
    def def_name(self, node) -> str | None:
        name = node.child_by_field_name("name")
        return _text(name) if name is not None else None

    def on_def(self, node, qualname: str, kind: str) -> None:
        pass

    def is_property(self, node) -> bool:
        """A method read as an attribute (no call), e.g. Python's @property."""
        return False

    def _annotated(self, name_node) -> str:
        """The definition an annotation/attribute belongs to. Annotations sit inside the definition,
        so the walk is usually already in its scope."""
        if name_node is None:
            return self.src
        name = _text(name_node)
        if self.scope and self.scope[-1] == name:
            return self.src
        return f"{self.src}.{name}" if self.scope else name

    def on_node(self, node) -> bool:
        """Handle a node; True means its children were handled too."""
        return False

    def on_call(self, node, chain: tuple[str, ...] | None) -> None:
        pass

    def on_string(self, node) -> None:
        value = _string_value(node)
        parent = node.parent
        if parent is not None and parent.type == "expression_statement":
            return                                   # a docstring, not data
        if value and SQL_SHAPE.search(value):
            writes = {(a or b).lower() for a, b in SQL_WRITE.findall(value)}
            for table in writes:
                self.f.tables.append((self.src, table, "writes"))
            for table in {m.lower() for m in SQL_READ.findall(value)} - writes - SQL_NOT_TABLES:
                self.f.tables.append((self.src, table, "reads"))

    # Walk ---------------------------------------------------------------------------------------
    def walk(self, node) -> None:
        t = node.type
        kind = self.def_types.get(t)
        if kind:
            name = self.def_name(node)
            if name:
                qual = f"{self.src}.{name}" if self.scope else name
                if kind == "function" and self.scope and self.scope_kinds and self.scope_kinds[-1] == "class":
                    kind = "method"
                if kind == "method" and self.is_property(node):
                    kind = "property"
                self.f.defs.append(Def(qual, kind, node.start_point[0] + 1, node.end_point[0] + 1))
                self.on_def(node, qual, kind)
                self.scope.append(name)
                self.scope_kinds.append(kind if kind not in ("method", "property") else "function")
                name_node = node.child_by_field_name("name")
                for child in node.children:
                    if not _same(child, name_node):
                        self.walk(child)
                self.scope.pop()
                self.scope_kinds.pop()
                return
        if self.on_node(node):
            return
        if t in self.call_types:
            fn = node.child_by_field_name(self.call_fn_field)
            chain = _chain(fn, self.attr_types, self.ident_types, self.obj_field, self.prop_field) if fn else None
            if chain:
                self.f.refs.append(Ref(self.src, chain, True, node.start_point[0] + 1))
            elif fn is not None and fn.type in self.attr_types and fn.child_by_field_name(self.prop_field) is not None:
                # A method called on an expression (a call's result, an index): x.filter(...).not_expired().
                # The receiver's type is unknown; "?" lets the resolver try the unique-name rule.
                prop = _text(fn.child_by_field_name(self.prop_field))
                self.f.refs.append(Ref(self.src, ("?", prop), True, node.start_point[0] + 1))
            self.on_call(node, chain)
            for child in node.children:
                if chain and _same(child, fn):
                    continue
                self.walk(child)
            return
        if t in self.attr_types:
            chain = _chain(node, self.attr_types, self.ident_types, self.obj_field, self.prop_field)
            if chain:
                self.f.refs.append(Ref(self.src, chain, False, node.start_point[0] + 1))
                return
        if t in self.ident_types:
            parent = node.parent
            if parent is not None and parent.type not in self.skip_ident_parents:
                self.f.refs.append(Ref(self.src, (_text(node),), False, node.start_point[0] + 1))
            return
        if t in ("string", "string_literal", "interpreted_string_literal", "raw_string_literal",
                 "template_string", "verbatim_string_literal"):
            self.on_string(node)
            return
        for child in node.children:
            self.walk(child)

    scope_kinds: list[str]


class _Python(_Extractor):
    lang = "python"

    def is_property(self, node) -> bool:
        parent = node.parent
        if parent is None or parent.type != "decorated_definition":
            return False
        return any(d.type == "decorator" and _text(d).lstrip("@").split("(")[0].rsplit(".", 1)[-1]
                   in ("property", "cached_property") for d in parent.children)
    def_types = {"function_definition": "function", "class_definition": "class"}
    attr_types = ("attribute",)
    ident_types = ("identifier",)
    call_types = ("call",)
    skip_ident_parents = frozenset({"parameters", "default_parameter", "typed_parameter", "typed_default_parameter",
                                    "keyword_argument", "lambda_parameters", "global_statement",
                                    "nonlocal_statement", "aliased_import", "dotted_name", "import_from_statement",
                                    "list_splat_pattern", "dictionary_splat_pattern"})

    def on_def(self, node, qualname, kind):
        if kind == "class":
            supers = node.child_by_field_name("superclasses")
            bases = [_text(a) for a in (supers.named_children if supers else [])]
            if any(b in ("models.Model", "Model") or b.endswith(".Model") for b in bases):
                self.f.tables.append((qualname, self._django_table(node, qualname), "defines"))
        parent = node.parent
        if parent is not None and parent.type == "decorated_definition":
            for dec in parent.children:
                if dec.type != "decorator":
                    continue
                expr = dec.named_children[0] if dec.named_children else None
                text = _text(expr)
                head = text.split("(")[0]
                if head.endswith((".task", "shared_task")) or head in ("task", "shared_task"):
                    self.f.queues.append((qualname, f"celery:{self.f.module}.{qualname}", "subscribes"))
                verb = head.rsplit(".", 1)[-1].lower()
                if expr is not None and expr.type == "call" and (verb in HTTP_VERBS or verb in ("route", "api_route")):
                    args = expr.child_by_field_name("arguments")
                    route = next((_string_value(a) for a in (args.named_children if args else [])
                                  if _string_value(a) is not None), None)
                    if route is not None:
                        method = verb.upper() if verb in HTTP_VERBS else "ANY"
                        self.f.endpoints.append((qualname, method, route))
                        self._webhook_consumer(qualname, route)
                # Decorators are references too (permission wrappers, registries).
                if expr is not None:
                    self.walk_with_scope(expr, self.scope[:-0] if False else self.scope)

    def walk_with_scope(self, node, scope):
        saved = self.scope
        self.scope = list(scope)
        self.walk(node)
        self.scope = saved

    def _django_table(self, node, qualname: str) -> str:
        body = node.child_by_field_name("body")
        for child in (body.named_children if body else []):
            if child.type == "class_definition" and _text(child.child_by_field_name("name")) == "Meta":
                for stmt in re.finditer(r"db_table\s*=\s*[\"']([^\"']+)[\"']", _text(child)):
                    return stmt.group(1)
        parts = PurePosixPath(self.f.path).parts
        app = parts[-2] if len(parts) >= 2 else "app"
        if parts and parts[-1] != "models.py" and len(parts) >= 3 and parts[-2] == "models":
            app = parts[-3]
        return f"{app}_{qualname.split('.')[-1].lower()}"

    def _webhook_consumer(self, src: str, route: str) -> None:
        if "webhook" in route.lower():
            event = route.rstrip("/").rsplit("/", 1)[-1]
            if event and not event.startswith("{"):
                self.f.queues.append((src, "webhook:" + event.replace("-", "_").upper(), "subscribes"))

    def on_node(self, node):
        t = node.type
        if t == "decorated_definition":
            for child in node.children:
                if child.type != "decorator":
                    self.walk(child)
            return True
        if t == "import_statement":
            for name in node.named_children:
                if name.type == "dotted_name":
                    mod = _text(name)
                    self.f.imports.append(Import(mod.split(".")[0], mod.split(".")[0], None))
                    if "." in mod:
                        self.f.imports.append(Import("", mod, None))
                elif name.type == "aliased_import":
                    self.f.imports.append(Import(_text(name.child_by_field_name("alias")),
                                                 _text(name.child_by_field_name("name")), None))
            return True
        if t == "import_from_statement":
            mod_node = node.child_by_field_name("module_name")
            mod = self._resolve_relative(_text(mod_node))
            names = [c for c in node.named_children if not _same(c, mod_node)]
            if any(c.type == "wildcard_import" for c in names):
                self.f.imports.append(Import("*", mod, "*"))
            for c in names:
                if c.type == "dotted_name":
                    self.f.imports.append(Import(_text(c).split(".")[-1], mod, _text(c)))
                elif c.type == "aliased_import":
                    self.f.imports.append(Import(_text(c.child_by_field_name("alias")), mod,
                                                 _text(c.child_by_field_name("name"))))
            return True
        if t == "assignment":
            # Only the right-hand side and attribute targets are references; plain names on the
            # left are new local bindings.
            left, right = node.child_by_field_name("left"), node.child_by_field_name("right")
            if left is not None and left.type not in ("identifier", "pattern_list", "tuple_pattern"):
                self.walk(left)
            for extra in (node.child_by_field_name("type"), right):
                if extra is not None:
                    self.walk(extra)
            return True
        if t == "subscript":
            value = node.child_by_field_name("value")
            chain = _chain(value, self.attr_types, self.ident_types, self.obj_field, self.prop_field)
            if chain in (("os", "environ"),):
                key = _string_value(node.child_by_field_name("subscript"))
                if key:
                    self.f.config.append((self.src, key))
        if t in self.attr_types:
            chain = _chain(node, self.attr_types, self.ident_types, self.obj_field, self.prop_field)
            if chain and len(chain) == 2 and chain[0] == "settings" and ENV_NAME.match(chain[1]):
                self.f.config.append((self.src, chain[1]))
            if chain and len(chain) >= 2 and chain[-2] == "WebhookEventAsyncType":
                self.f.queues.append((self.src, "webhook:" + chain[-1], "publishes"))
        return False

    def on_call(self, node, chain):
        args = node.child_by_field_name("arguments")
        first = args.named_children[0] if args is not None and args.named_children else None
        if chain in (("os", "getenv"), ("os", "environ", "get"), ("env",), ("env", "str"), ("env", "bool"),
                     ("env", "int"), ("config",)):
            key = _string_value(first)
            if key and ENV_NAME.match(key):
                self.f.config.append((self.src, key))
        if chain and chain[-1] in ("path", "re_path", "url") and len(chain) <= 2 and args is not None:
            route = _string_value(first)
            view = args.named_children[1] if len(args.named_children) > 1 else None
            if route is not None and view is not None:
                vchain = _chain(view.child_by_field_name("function") if view.type == "call" else view,
                                self.attr_types, self.ident_types, self.obj_field, self.prop_field)
                if vchain:
                    self.f.endpoints.append(("#ref:" + ".".join(vchain), "ANY", route))
        if chain and chain[-1] in ("delay", "apply_async") and len(chain) >= 2:
            self.f.refs.append(Ref(self.src, chain[:-1] + ("#publish",), True, node.start_point[0] + 1))

    def _resolve_relative(self, mod: str) -> str:
        if not mod.startswith("."):
            return mod
        dots = len(mod) - len(mod.lstrip("."))
        rest = mod[dots:]
        package = self.f.module.split(".")
        is_pkg = self.f.path.endswith("__init__.py")
        base = package if is_pkg else package[:-1]
        base = base[: len(base) - (dots - 1)] if dots > 1 else base
        return ".".join([*base, rest] if rest else base)


class _JavaScript(_Extractor):
    lang = "javascript"
    def_types = {"function_declaration": "function", "class_declaration": "class", "method_definition": "function",
                 "generator_function_declaration": "function", "interface_declaration": "class",
                 "abstract_class_declaration": "class"}
    attr_types = ("member_expression",)
    ident_types = ("identifier",)
    call_types = ("call_expression",)
    prop_field = "property"
    skip_ident_parents = frozenset({"formal_parameters", "required_parameter", "optional_parameter",
                                    "import_specifier", "namespace_import", "import_clause", "variable_declarator",
                                    "shorthand_property_identifier_pattern", "pair_pattern", "object_pattern",
                                    "array_pattern", "labeled_statement"})

    def def_name(self, node):
        name = node.child_by_field_name("name")
        return _text(name) if name is not None else None

    def on_node(self, node):
        t = node.type
        if t == "import_statement":
            source = _string_value(node.child_by_field_name("source"))
            if source is None:
                return True
            mod = self._resolve(source)
            clause = next((c for c in node.named_children if c.type == "import_clause"), None)
            if clause is None:
                self.f.imports.append(Import("", mod, None))
            for c in (clause.named_children if clause else []):
                if c.type == "identifier":
                    self.f.imports.append(Import(_text(c), mod, "default"))
                elif c.type == "namespace_import":
                    ident = next((x for x in c.named_children if x.type == "identifier"), None)
                    self.f.imports.append(Import(_text(ident), mod, None))
                elif c.type == "named_imports":
                    for spec in c.named_children:
                        name, alias = spec.child_by_field_name("name"), spec.child_by_field_name("alias")
                        self.f.imports.append(Import(_text(alias or name), mod, _text(name)))
            return True
        if t == "variable_declarator":
            name, value = node.child_by_field_name("name"), node.child_by_field_name("value")
            if value is not None and value.type in ("arrow_function", "function_expression", "function") \
                    and name is not None and name.type == "identifier":
                qual = f"{self.src}.{_text(name)}" if self.scope else _text(name)
                self.f.defs.append(Def(qual, "function", node.start_point[0] + 1, node.end_point[0] + 1))
                self.scope.append(_text(name))
                self.scope_kinds.append("function")
                self.walk(value)
                self.scope.pop()
                self.scope_kinds.pop()
                return True
            if value is not None and value.type == "call_expression":
                fn = value.child_by_field_name("function")
                args = value.child_by_field_name("arguments")
                if _text(fn) == "require" and args is not None and args.named_children:
                    source = _string_value(args.named_children[0])
                    if source is not None and name is not None:
                        mod = self._resolve(source)
                        if name.type == "identifier":
                            self.f.imports.append(Import(_text(name), mod, None))
                        else:
                            for ident in name.named_children:
                                if ident.type in ("shorthand_property_identifier_pattern", "identifier"):
                                    self.f.imports.append(Import(_text(ident), mod, _text(ident)))
                        return True
            if value is not None:
                self.walk(value)
            return True
        if t in self.attr_types:
            chain = _chain(node, self.attr_types, self.ident_types, self.obj_field, self.prop_field)
            if chain and len(chain) == 3 and chain[:2] == ("process", "env"):
                self.f.config.append((self.src, chain[2]))
        if t == "subscript_expression":
            obj = node.child_by_field_name("object")
            if _text(obj) == "process.env":
                key = _string_value(node.child_by_field_name("index"))
                if key:
                    self.f.config.append((self.src, key))
        if t == "decorator":
            expr = node.named_children[0] if node.named_children else None
            if expr is not None and expr.type == "call_expression":
                verb = _text(expr.child_by_field_name("function")).rsplit(".", 1)[-1].lower()
                if verb in HTTP_VERBS:
                    args = expr.child_by_field_name("arguments")
                    route = _string_value(args.named_children[0]) if args is not None and args.named_children else ""
                    target = node.parent
                    name = target.child_by_field_name("name") if target is not None else None
                    src = (f"{self.src}.{_text(name)}" if self.scope else _text(name)) if name is not None else self.src
                    self.f.endpoints.append((src, verb.upper(), route or "/"))
        return False

    def on_call(self, node, chain):
        if chain and len(chain) == 2 and chain[1] in HTTP_VERBS + ("all", "use") and chain[0] in ("app", "router", "server"):
            args = node.child_by_field_name("arguments")
            if args is None or not args.named_children:
                return
            route = _string_value(args.named_children[0])
            handler = args.named_children[-1]
            hchain = _chain(handler, self.attr_types, self.ident_types, self.obj_field, self.prop_field)
            if route is not None:
                src = "#ref:" + ".".join(hchain) if hchain else self.src
                self.f.endpoints.append((src, chain[1].upper() if chain[1] in HTTP_VERBS else "ANY", route))

    def _resolve(self, source: str) -> str:
        if not source.startswith("."):
            return source
        base = PurePosixPath(self.f.path).parent
        joined = PurePosixPath(*([*base.parts] + [source]))
        parts: list[str] = []
        for part in joined.parts:
            if part == "..":
                if parts:
                    parts.pop()
            elif part != ".":
                parts.append(part)
        resolved = "/".join(parts)
        return re.sub(r"\.(?:[cm]?[jt]sx?)$", "", resolved).removesuffix("/index")


class _Go(_Extractor):
    lang = "go"
    def_types = {"function_declaration": "function", "method_declaration": "method", "type_spec": "class"}
    attr_types = ("selector_expression",)
    ident_types = ("identifier", "package_identifier")
    call_types = ("call_expression",)
    obj_field = "operand"
    prop_field = "field"
    skip_ident_parents = frozenset({"parameter_declaration", "short_var_declaration", "var_spec", "const_spec",
                                    "import_spec", "range_clause", "labeled_statement", "field_declaration"})

    def def_name(self, node):
        name = node.child_by_field_name("name")
        if name is None:
            return None
        if node.type == "method_declaration":
            receiver = node.child_by_field_name("receiver")
            rtype = re.findall(r"[A-Za-z_][A-Za-z0-9_]*", _text(receiver))
            return f"{rtype[-1]}.{_text(name)}" if rtype else _text(name)
        return _text(name)

    def on_node(self, node):
        if node.type == "import_spec":
            path = _string_value(node.child_by_field_name("path")) or ""
            alias = node.child_by_field_name("name")
            self.f.imports.append(Import(_text(alias) if alias is not None else path.rsplit("/", 1)[-1], path, None))
            return True
        return False

    def on_call(self, node, chain):
        args = node.child_by_field_name("arguments")
        first = args.named_children[0] if args is not None and args.named_children else None
        if chain == ("os", "Getenv") or chain == ("os", "LookupEnv"):
            key = _string_value(first)
            if key:
                self.f.config.append((self.src, key))
        if chain and chain[-1] in ("HandleFunc", "Handle", "GET", "POST", "PUT", "PATCH", "DELETE") and first is not None:
            route = _string_value(first)
            handler = args.named_children[-1]
            hchain = _chain(handler, self.attr_types, self.ident_types, self.obj_field, self.prop_field)
            if route is not None:
                verb = chain[-1] if chain[-1].isupper() else "ANY"
                self.f.endpoints.append(("#ref:" + ".".join(hchain) if hchain else self.src, verb, route))


class _Java(_Extractor):
    lang = "java"
    def_types = {"class_declaration": "class", "interface_declaration": "class", "enum_declaration": "class",
                 "record_declaration": "class", "method_declaration": "function", "constructor_declaration": "function"}
    attr_types = ("field_access",)
    ident_types = ("identifier", "type_identifier")
    call_types = ("method_invocation",)
    obj_field = "object"
    prop_field = "field"
    skip_ident_parents = frozenset({"formal_parameter", "variable_declarator", "catch_formal_parameter",
                                    "import_declaration", "package_declaration", "scoped_identifier",
                                    "enhanced_for_statement", "lambda_expression", "inferred_parameters"})

    def on_node(self, node):
        t = node.type
        if t == "package_declaration":
            name = next((c for c in node.named_children if c.type in ("scoped_identifier", "identifier")), None)
            if name is not None:
                self.f.module = _text(name)
            return True
        if t == "import_declaration":
            name = next((c for c in node.named_children if c.type in ("scoped_identifier", "identifier")), None)
            full = _text(name)
            if full:
                if "*" in _text(node):
                    self.f.imports.append(Import("*", full, "*"))
                else:
                    pkg, _, member = full.rpartition(".")
                    self.f.imports.append(Import(member, pkg, member))
            return True
        if t == "method_invocation":
            obj, name = node.child_by_field_name("object"), node.child_by_field_name("name")
            chain = None
            if obj is None:
                chain = (_text(name),)
            else:
                head = _chain(obj, self.attr_types, self.ident_types, self.obj_field, self.prop_field)
                chain = head + (_text(name),) if head else None
            if chain:
                self.f.refs.append(Ref(self.src, chain, True, node.start_point[0] + 1))
                if chain == ("System", "getenv"):
                    args = node.child_by_field_name("arguments")
                    key = _string_value(args.named_children[0]) if args is not None and args.named_children else None
                    if key:
                        self.f.config.append((self.src, key))
            for child in node.children:
                if not _same(child, name) and (obj is None or not chain or not _same(child, obj)):
                    self.walk(child)
            return True
        if t in ("annotation", "marker_annotation"):
            name = _text(node.child_by_field_name("name"))
            if name.endswith("Mapping"):
                verb = name[: -len("Mapping")].upper() or "ANY"
                verb = "ANY" if verb == "REQUEST" else verb
                route = re.search(r"\"([^\"]*)\"", _text(node))
                target = node.parent.parent if node.parent is not None else None
                tname = target.child_by_field_name("name") if target is not None else None
                self.f.endpoints.append((self._annotated(tname), verb, route.group(1) if route else "/"))
            return True
        return False


class _CSharp(_Extractor):
    lang = "csharp"
    def_types = {"class_declaration": "class", "interface_declaration": "class", "struct_declaration": "class",
                 "record_declaration": "class", "enum_declaration": "class", "method_declaration": "function",
                 "constructor_declaration": "function"}
    attr_types = ("member_access_expression",)
    ident_types = ("identifier",)
    call_types = ("invocation_expression",)
    obj_field = "expression"
    prop_field = "name"
    skip_ident_parents = frozenset({"parameter", "variable_declarator", "using_directive", "namespace_declaration",
                                    "file_scoped_namespace_declaration", "qualified_name", "for_each_statement",
                                    "catch_declaration", "name_equals", "argument_list_name"})

    def on_node(self, node):
        t = node.type
        if t in ("namespace_declaration", "file_scoped_namespace_declaration"):
            name = node.child_by_field_name("name")
            if name is not None:
                self.f.module = _text(name)
            return False
        if t == "using_directive":
            name = next((c for c in node.named_children if c.type in ("qualified_name", "identifier")), None)
            if name is not None:
                self.f.imports.append(Import("*", _text(name), "*"))
            return True
        if t == "attribute":
            name = _text(node.child_by_field_name("name"))
            if name.startswith("Http") or name == "Route":
                verb = name[4:].upper() if name.startswith("Http") else "ANY"
                route = re.search(r"\"([^\"]*)\"", _text(node))
                target = node.parent.parent if node.parent is not None else None
                tname = target.child_by_field_name("name") if target is not None else None
                self.f.endpoints.append((self._annotated(tname), verb or "ANY", route.group(1) if route else "/"))
            return True
        return False

    def on_call(self, node, chain):
        args = node.child_by_field_name("arguments")
        first = args.named_children[0] if args is not None and args.named_children else None
        first = first.named_children[0] if first is not None and first.type == "argument" and first.named_children else first
        if chain == ("Environment", "GetEnvironmentVariable"):
            key = _string_value(first)
            if key:
                self.f.config.append((self.src, key))
        if chain and chain[-1] in ("MapGet", "MapPost", "MapPut", "MapDelete", "MapPatch") and first is not None:
            route = _string_value(first)
            if route is not None:
                self.f.endpoints.append((self.src, chain[-1][3:].upper(), route))


EXTRACTORS = {"python": _Python, "javascript": _JavaScript, "typescript": _JavaScript, "tsx": _JavaScript,
              "go": _Go, "java": _Java, "csharp": _CSharp}


def parse_file(path: str, source: bytes) -> FileFacts:
    """Extract one file's facts. Never raises: a file that will not parse records its error."""
    lang = language_for(path)
    if lang is None:
        raise ValueError(f"not an indexed language: {path}")
    facts = FileFacts(path=path, lang=lang, module=module_name(path, lang),
                      content_hash=hashlib.sha256(source).hexdigest(), lines=source.count(b"\n") + 1)
    if len(source) > MAX_FILE_BYTES:
        facts.error = "too large"
        return facts
    try:
        tree = _parser(lang).parse(source)
        extractor = EXTRACTORS[lang](facts, source)
        extractor.scope_kinds = []
        extractor.run(tree.root_node)
    except RecursionError:
        facts.error = "too deeply nested"
    if facts.lang == "go":
        # Every file of a Go package shares its directory as the module.
        facts.module = module_name(path, "go")
    return facts

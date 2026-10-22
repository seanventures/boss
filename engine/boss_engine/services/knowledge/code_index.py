"""Code graph indexer.

Python uses the stdlib AST. JavaScript, TypeScript, Go, Java, and C# use
tree-sitter when that package and the language grammar import. Otherwise
those languages use the declaration scan. This module does not claim a
Saleor timing.
"""
from __future__ import annotations

import ast
import hashlib
import re
from dataclasses import dataclass, field
from typing import Optional

from sqlalchemy.engine import Connection

from .store import PostgresKnowledgeStore

LANGUAGES = frozenset({"python", "typescript", "javascript", "go", "java", "csharp"})

_DECL = {
    "javascript": (
        re.compile(r"(?:function|class)\s+([A-Za-z_]\w*)"),
        re.compile(r"import\s+(?:[^'\"]+from\s+)?['\"]([^'\"]+)['\"]"),
    ),
    "typescript": (
        re.compile(r"(?:function|class|interface)\s+([A-Za-z_]\w*)"),
        re.compile(r"import\s+(?:[^'\"]+from\s+)?['\"]([^'\"]+)['\"]"),
    ),
    "go": (
        re.compile(r"func\s+(?:\([^)]+\)\s+)?([A-Za-z_]\w*)\s*\("),
        re.compile(r"import\s+(?:\(\s*)?\"([^\"]+)\""),
    ),
    "java": (
        re.compile(r"\b(?:class|interface|enum)\s+([A-Za-z_]\w*)"),
        re.compile(r"import\s+([\w.]+)\s*;"),
    ),
    "csharp": (
        re.compile(r"\b(?:class|interface|record|struct)\s+([A-Za-z_]\w*)"),
        re.compile(r"using\s+([\w.]+)\s*;"),
    ),
}
_CALL = re.compile(r"\b([A-Za-z_]\w*)\s*\(")


@dataclass
class ParsedFile:
    path: str
    language: str
    symbols: list[str] = field(default_factory=list)
    imports: list[str] = field(default_factory=list)
    calls: list[tuple[str, str]] = field(default_factory=list)
    backend: str = "declaration_scan"


def parser_backend(language: str) -> str:
    if language == "python":
        return "python_ast"
    if language not in LANGUAGES:
        raise ValueError(f"unsupported language {language!r}")
    try:
        import tree_sitter  # noqa: F401
        import importlib

        importlib.import_module(f"tree_sitter_{language.replace('csharp', 'c_sharp')}")
    except Exception:
        return "declaration_scan"
    return "tree_sitter"


def _python(path: str, source: str) -> ParsedFile:
    tree = ast.parse(source)
    parsed = ParsedFile(path=path, language="python", backend="python_ast")
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            parsed.symbols.append(node.name)
            for child in ast.walk(node):
                if isinstance(child, ast.Call):
                    called = _call_name(child.func)
                    if called:
                        parsed.calls.append((node.name, called))
        elif isinstance(node, ast.Import):
            parsed.imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            parsed.imports.append(node.module)
    return parsed


def _call_name(node: ast.AST) -> Optional[str]:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _scan(path: str, language: str, source: str) -> ParsedFile:
    symbol_re, import_re = _DECL[language]
    parsed = ParsedFile(path=path, language=language, backend="declaration_scan")
    parsed.symbols = list(dict.fromkeys(symbol_re.findall(source)))
    parsed.imports = list(dict.fromkeys(import_re.findall(source)))
    names = set(parsed.symbols)
    for match in _CALL.finditer(source):
        callee = match.group(1)
        if callee in names or callee in {"if", "for", "while", "switch", "catch", "return"}:
            continue
        if parsed.symbols:
            parsed.calls.append((parsed.symbols[0], callee))
    return parsed


_TS_QUERY = {
    "javascript": """
(function_declaration name: (identifier) @name) @def
(class_declaration name: (identifier) @name) @def
(method_definition name: (property_identifier) @name) @def
(variable_declarator name: (identifier) @name value: (arrow_function)) @def
(variable_declarator name: (identifier) @name value: (function_expression)) @def
(import_statement source: (string) @mod)
(call_expression function: (identifier) @callee) @call
(call_expression function: (member_expression property: (property_identifier) @callee)) @call
""",
    "typescript": """
(function_declaration name: (identifier) @name) @def
(class_declaration name: (type_identifier) @name) @def
(interface_declaration name: (type_identifier) @name) @def
(method_definition name: (property_identifier) @name) @def
(method_signature name: (property_identifier) @name) @def
(variable_declarator name: (identifier) @name value: (arrow_function)) @def
(import_statement source: (string) @mod)
(call_expression function: (identifier) @callee) @call
(call_expression function: (member_expression property: (property_identifier) @callee)) @call
""",
    "go": """
(function_declaration name: (identifier) @name) @def
(method_declaration name: (field_identifier) @name) @def
(import_spec path: (interpreted_string_literal) @mod)
(call_expression function: (identifier) @callee) @call
(call_expression function: (selector_expression field: (field_identifier) @callee)) @call
""",
    "java": """
(class_declaration name: (identifier) @name) @def
(interface_declaration name: (identifier) @name) @def
(method_declaration name: (identifier) @name) @def
(import_declaration (scoped_identifier) @mod)
(import_declaration (identifier) @mod)
(method_invocation name: (identifier) @callee) @call
""",
    "csharp": """
(class_declaration name: (identifier) @name) @def
(interface_declaration name: (identifier) @name) @def
(method_declaration name: (identifier) @name) @def
(using_directive (qualified_name) @mod)
(using_directive (identifier) @mod)
(invocation_expression function: (identifier) @callee) @call
""",
}


def _language_object(language: str):
    import importlib

    from tree_sitter import Language

    if language == "typescript":
        module = importlib.import_module("tree_sitter_typescript")
        return Language(module.language_typescript())
    module = importlib.import_module(
        "tree_sitter_" + language.replace("csharp", "c_sharp")
    )
    return Language(module.language())


def _capture_nodes(value) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'", "`"}:
        return value[1:-1]
    return value


def _with_tree_sitter(path: str, language: str, source: str) -> ParsedFile:
    from tree_sitter import Parser, Query, QueryCursor

    parsed = ParsedFile(path=path, language=language, backend="tree_sitter")
    grammar = _language_object(language)
    tree = Parser(grammar).parse(source.encode("utf-8"))
    defs: list[tuple[int, int, str]] = []
    calls: list[tuple[int, str]] = []
    for _pattern, captured in QueryCursor(Query(grammar, _TS_QUERY[language])).matches(
        tree.root_node
    ):
        names = _capture_nodes(captured.get("name"))
        bodies = _capture_nodes(captured.get("def"))
        if names and bodies:
            defs.append((bodies[0].start_byte, bodies[0].end_byte, names[0].text.decode()))
        for node in _capture_nodes(captured.get("mod")):
            parsed.imports.append(_unquote(node.text.decode()))
        callees = _capture_nodes(captured.get("callee"))
        sites = _capture_nodes(captured.get("call"))
        if callees and sites:
            calls.append((sites[0].start_byte, callees[0].text.decode()))
    defs.sort(key=lambda item: (item[0], item[1]))
    parsed.symbols = list(dict.fromkeys(name for _start, _end, name in defs))
    parsed.imports = list(dict.fromkeys(parsed.imports))
    known = set(parsed.symbols)
    for start, callee in calls:
        if callee not in known:
            continue
        enclosing = [
            (end - begin, name)
            for begin, end, name in defs
            if begin <= start < end and name != callee
        ]
        if not enclosing:
            continue
        caller = min(enclosing)[1]
        parsed.calls.append((caller, callee))
    return parsed


def parse_source(path: str, language: str, source: str) -> ParsedFile:
    if language not in LANGUAGES:
        raise ValueError(f"unsupported language {language!r}")
    if language == "python":
        return _python(path, source)
    if parser_backend(language) == "tree_sitter":
        return _with_tree_sitter(path, language, source)
    return _scan(path, language, source)


def index_files(
    connection: Connection,
    store: PostgresKnowledgeStore,
    *,
    project_id: str,
    files: list[dict],
) -> dict:
    """Idempotent index. An unchanged file hash does not write again."""
    indexed = 0
    skipped = 0
    symbols = 0
    for item in files:
        path = str(item["path"])
        language = str(item["language"])
        source = str(item["source"])
        digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
        file_key = f"file:{path}"
        existing = store.find_by_key(
            connection, project_id=project_id, kind="file", node_key=file_key
        )
        if existing is not None and existing.get("content_hash"):
            # content_hash covers kind, key, name, body, tier. Body is the digest.
            if existing["body"] == digest:
                skipped += 1
                continue
        parsed = parse_source(path, language, source)
        file_id = store.upsert_node(
            connection,
            project_id=project_id,
            kind="file",
            node_key=file_key,
            name=path,
            body=digest,
            trust_tier="observed",
            actor_kind="system",
            source="code_index",
            attrs={"language": language, "backend": parsed.backend},
        )
        indexed += 1
        for name in parsed.symbols:
            symbol_id = store.upsert_node(
                connection,
                project_id=project_id,
                kind="symbol",
                node_key=f"symbol:{path}:{name}",
                name=name,
                body=f"{language} {name}",
                trust_tier="observed",
                actor_kind="system",
                source="code_index",
            )
            store.upsert_edge(
                connection,
                project_id=project_id,
                src_id=file_id,
                dst_id=symbol_id,
                kind="defines",
                actor_kind="system",
            )
            symbols += 1
        for imported in parsed.imports:
            module_id = store.upsert_node(
                connection,
                project_id=project_id,
                kind="module",
                node_key=f"module:{imported}",
                name=imported,
                body=imported,
                trust_tier="observed",
                actor_kind="system",
                source="code_index",
            )
            store.upsert_edge(
                connection,
                project_id=project_id,
                src_id=file_id,
                dst_id=module_id,
                kind="imports",
                actor_kind="system",
            )
        known = {
            name: f"symbol:{path}:{name}" for name in parsed.symbols
        }
        for caller, callee in parsed.calls:
            if callee not in known or caller not in known:
                continue
            src = store.find_by_key(
                connection, project_id=project_id, kind="symbol", node_key=known[caller]
            )
            dst = store.find_by_key(
                connection, project_id=project_id, kind="symbol", node_key=known[callee]
            )
            if src and dst:
                store.upsert_edge(
                    connection,
                    project_id=project_id,
                    src_id=src["id"],
                    dst_id=dst["id"],
                    kind="calls",
                    actor_kind="system",
                )
    return {"indexed": indexed, "skipped": skipped, "symbols": symbols}

"""Code graph indexer.

Python uses the stdlib AST. The other four languages use a declaration scan.
A tree-sitter backend is used only when that package and a language grammar
are both importable. This module does not claim a Saleor timing.
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


def parse_source(path: str, language: str, source: str) -> ParsedFile:
    if language not in LANGUAGES:
        raise ValueError(f"unsupported language {language!r}")
    if language == "python":
        return _python(path, source)
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

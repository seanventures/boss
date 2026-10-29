"""Deterministic evidence computation for the L4 PR integrity/reachability review.

Feeds the review seat (an agent that judges each item) rather than producing a
verdict itself. See the L4 brief: catch hollow PRs (unreachable code, test-only
stubs, recorded-but-unenacted decisions) and gate-gaming PRs (deleted refusal
rules, stray lockfiles, sandbox-escaping subprocess calls).
"""
import ast
from dataclasses import dataclass, field


@dataclass
class IntegrityEvidence:
    unreachable: list = field(default_factory=list)
    test_only: list = field(default_factory=list)
    unenacted: list = field(default_factory=list)
    integrity: list = field(default_factory=list)
    policy: list = field(default_factory=list)


def _is_test_path(path):
    return "test" in path.lower()


_ENTRY_PATH_MARKERS = ("route", "cli", "worker", "loader", "main", "workflow", "runtime", "handler")


def _is_entry_path(path):
    lowered = path.lower()
    return any(marker in lowered for marker in _ENTRY_PATH_MARKERS)


def _safe_parse(text):
    try:
        return ast.parse(text)
    except SyntaxError:
        return None


def _top_level_functions(tree):
    names = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names.append(node.name)
    return names


def _stub_only_body(func_node):
    body = func_node.body
    stmts = [s for s in body if not isinstance(s, ast.Expr) or not isinstance(s.value, ast.Constant)]
    if not stmts:
        return False
    if len(stmts) == 1:
        s = stmts[0]
        if isinstance(s, ast.Raise):
            exc = s.exc
            if isinstance(exc, ast.Call) and isinstance(exc.func, ast.Name) and exc.func.id == "NotImplementedError":
                return True
            if isinstance(exc, ast.Name) and exc.id == "NotImplementedError":
                return True
        if isinstance(s, ast.Pass):
            return True
        if isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant) and s.value.value is Ellipsis:
            return True
    return False


def _refusal_markers(tree):
    markers = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Raise):
            markers.add("raise")
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Constant) and node.value.value is False:
            markers.add("return_false")
    return markers


def compute_integrity_evidence(changed_files, existing_files=None):
    existing_files = existing_files or {}
    evidence = IntegrityEvidence()

    all_text_for_refs = []
    for path, text in changed_files.items():
        if text is not None:
            all_text_for_refs.append(text)
    for path, text in existing_files.items():
        if path not in changed_files and text is not None:
            all_text_for_refs.append(text)
    combined_source = "\n".join(all_text_for_refs)

    for path, text in changed_files.items():
        if text is None:
            continue
        if _is_test_path(path):
            continue
        if not path.endswith(".py"):
            continue

        tree = _safe_parse(text)
        if tree is None:
            continue

        func_names = []
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                func_names.append((node.name, node))

        for name, node in func_names:
            if name.startswith("_"):
                continue

            references_elsewhere = combined_source.count(name) > text.count(name)
            if not references_elsewhere and not _is_entry_path(path):
                evidence.unreachable.append({
                    "path": path,
                    "reason": f"'{name}' is not referenced from any other changed or existing file",
                })

            if _stub_only_body(node):
                evidence.test_only.append({
                    "path": path,
                    "reason": f"'{name}' body is a stub (NotImplementedError/pass/...) in production code",
                })

        writer_funcs = {}
        for name, node in func_names:
            for inner in ast.walk(node):
                if isinstance(inner, ast.Subscript) and isinstance(inner.ctx, ast.Store):
                    writer_funcs[name] = node
                if isinstance(inner, ast.Assign):
                    for target in inner.targets:
                        if isinstance(target, ast.Subscript):
                            writer_funcs[name] = node

        for name, node in writer_funcs.items():
            key_names = set()
            for inner in ast.walk(node):
                if isinstance(inner, ast.Assign):
                    for target in inner.targets:
                        if isinstance(target, ast.Subscript) and isinstance(target.slice, ast.Constant):
                            key_names.add(target.slice.value)
            if key_names:
                read_somewhere = False
                for other_path, other_text in list(changed_files.items()) + list(existing_files.items()):
                    if other_text is None or other_path == path:
                        continue
                    for key in key_names:
                        if f"[{key!r}]" in other_text or f'["{key}"]' in other_text or f"'{key}'" in other_text and "get(" in other_text:
                            read_somewhere = True
                if not read_somewhere:
                    evidence.unenacted.append({
                        "path": path,
                        "reason": f"'{name}' records a decision ({sorted(key_names)}) that no other code reads back",
                    })

        old_text = existing_files.get(path)
        if old_text is not None:
            old_tree = _safe_parse(old_text)
            if old_tree is not None:
                old_markers = _refusal_markers(old_tree)
                new_markers = _refusal_markers(tree)
                if "raise" in old_markers and "raise" not in new_markers:
                    evidence.integrity.append({
                        "path": path,
                        "reason": "a 'raise' refusal/guard path present in the prior version was removed",
                    })
                elif "return_false" in old_markers and "return_false" not in new_markers:
                    evidence.integrity.append({
                        "path": path,
                        "reason": "a 'return False' refusal path present in the prior version was removed",
                    })

        if "subprocess" in text and "shell=True" in text:
            evidence.policy.append({
                "path": path,
                "reason": "subprocess call uses shell=True against a workspace path, a potential sandbox escape",
            })

    lockfile_managers = {
        "yarn.lock": "yarn",
        "package-lock.json": "npm",
        "pnpm-lock.yaml": "pnpm",
    }
    existing_lockfiles = {p for p in existing_files if p in lockfile_managers and existing_files[p] is not None}
    for path, text in changed_files.items():
        if path in lockfile_managers and text is not None:
            other_lockfiles = existing_lockfiles - {path}
            if other_lockfiles:
                evidence.policy.append({
                    "path": path,
                    "reason": f"stray lockfile for {lockfile_managers[path]}; repo already uses {sorted(other_lockfiles)}",
                })

    return evidence

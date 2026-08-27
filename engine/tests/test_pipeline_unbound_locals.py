"""A local read on a path that never bound it kills the run, and blames the platform.

Every code task at risk_tier 0 failed with:

    cannot access local variable 'skipped_seats' where it is not associated with a value

`skipped_seats` was declared inside `if do_review:` and read unconditionally further down the
same function. `do_review = risk_tier >= 1`, so at tier 0 the branch never ran and the read
raised UnboundLocalError -- recorded as reason_code=pipeline_exception, failure_class=infra,
which reads as a broken platform rather than a two-line bug. Runs failed this way for hours,
and the operator's Factory looked empty as a result.

NEITHER RUFF NOR PYFLAKES REPORTS THIS. Both were run against the exact shape and passed it:
the name IS bound somewhere in the function, so it is not an undefined name -- it is a path
that does not bind it, which needs flow analysis they do not do here. That is why it shipped,
and why this check exists rather than a linter rule.
"""

import ast
import pathlib

SERVICE = pathlib.Path(__file__).resolve().parents[1] / (
    "boss_engine/services/governed_code_task_service.py")


#: Nodes that introduce their OWN scope. A comprehension's loop variable is not a function
#: local in Python 3, so counting it as one reported `[F(path=p) for p, c in items]` as two
#: unbound reads -- a false alarm, and a check that cries wolf gets weakened until it is silent.
_OWN_SCOPE = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda,
              ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)


def _walk_same_scope(node: ast.AST):
    """ast.walk, but not descending into anything with its own scope."""
    todo = [node]
    while todo:
        n = todo.pop()
        yield n
        for child in ast.iter_child_nodes(n):
            if isinstance(child, _OWN_SCOPE):
                continue
            todo.append(child)


def _binds(node: ast.AST) -> set[str]:
    """Names this statement assigns, in this scope only."""
    out = set()
    for n in _walk_same_scope(node):
        targets = []
        if isinstance(n, ast.Assign):
            targets = n.targets
        elif isinstance(n, (ast.AnnAssign, ast.AugAssign)):
            targets = [n.target]
        elif isinstance(n, (ast.For, ast.AsyncFor)):
            targets = [n.target]
        elif isinstance(n, ast.NamedExpr):
            targets = [n.target]
        for t in targets:
            for sub in _walk_same_scope(t):
                if isinstance(sub, ast.Name):
                    out.add(sub.id)
    return out


def _sequential_body(stmts: list[ast.stmt]) -> list[ast.stmt]:
    """Statements that ALWAYS run, flattened.

    `with` and the body of `try` always execute, so a binding inside them is unconditional --
    treating them as branches was what made the first version of this check report five names
    that were perfectly safe. `if`, `for` and `while` may not run at all.
    """
    out = []
    for s in stmts:
        if isinstance(s, (ast.With, ast.AsyncWith)):
            out.extend(_sequential_body(s.body))
        elif isinstance(s, ast.Try):
            out.extend(_sequential_body(s.body))
        else:
            out.append(s)
    return out


def conditionally_bound_then_read(tree: ast.AST) -> list[tuple[str, str, int]]:
    """(function, name, line) where a name bound only inside a branch is read outside it."""
    out = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        certain = {a.arg for a in fn.args.args} | {a.arg for a in fn.args.kwonlyargs}
        risky: dict[str, int] = {}

        for stmt in _sequential_body(fn.body):
            if isinstance(stmt, (ast.If, ast.For, ast.While, ast.AsyncFor)):
                # A read in the CONDITION happens before the branch does.
                test = getattr(stmt, "test", None) or getattr(stmt, "iter", None)
                if test is not None:
                    for n in _walk_same_scope(test):
                        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load):
                            if n.id in risky and n.id not in certain:
                                out.append((fn.name, n.id, stmt.lineno))
                bound_here = _binds(stmt)
                # An if/else that binds in BOTH arms always binds.
                if isinstance(stmt, ast.If) and stmt.orelse:
                    both = _binds_all(stmt.body) & _binds_all(stmt.orelse)
                    certain |= both
                    bound_here -= both
                for name in bound_here - certain:
                    risky.setdefault(name, stmt.lineno)
            else:
                for n in _walk_same_scope(stmt):
                    if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load):
                        if n.id in risky and n.id not in certain:
                            out.append((fn.name, n.id, getattr(n, "lineno", stmt.lineno)))
                certain |= _binds(stmt)
    return out


def _binds_all(stmts: list[ast.stmt]) -> set[str]:
    names: set[str] = set()
    for s in stmts:
        names |= _binds(s)
    return names


def test_no_local_is_read_on_a_path_that_never_bound_it():
    offenders = conditionally_bound_then_read(
        ast.parse(SERVICE.read_text(encoding="utf-8")))
    assert offenders == [], (
        "bound only inside a branch and read outside it — an UnboundLocalError waiting for "
        f"that branch not to run: {offenders}")


def test_the_check_catches_the_exact_bug_it_was_written_for():
    """A rule that cannot fail is not a rule."""
    broken = ast.parse(
        "def f(risk):\n"
        "    do_review = risk >= 1\n"
        "    if do_review:\n"
        "        skipped = {}\n"
        "    if skipped:\n"
        "        pass\n"
    )
    assert [(n, v) for n, v, _ in conditionally_bound_then_read(broken)] == [("f", "skipped")]


def test_a_binding_inside_with_or_try_is_not_a_false_alarm():
    """Both bodies always execute. Treating them as branches is what made the first version of
    this check report five names that were entirely safe — and a check that cries wolf gets
    weakened until it reports nothing."""
    fine = ast.parse(
        "def f():\n"
        "    with open('x') as fh:\n"
        "        data = fh.read()\n"
        "    try:\n"
        "        parsed = int(data)\n"
        "    except ValueError:\n"
        "        parsed = 0\n"
        "    return data, parsed\n"
    )
    assert conditionally_bound_then_read(fine) == []


def test_an_if_else_that_binds_in_both_arms_is_not_a_false_alarm():
    fine = ast.parse(
        "def f(x):\n"
        "    if x:\n"
        "        v = 1\n"
        "    else:\n"
        "        v = 2\n"
        "    return v\n"
    )
    assert conditionally_bound_then_read(fine) == []


def test_a_comprehension_variable_is_not_a_function_local():
    """`[F(path=p) for p, c in items]` was reported as two unbound reads. A comprehension has
    its own scope in Python 3, and a check that cries wolf gets weakened until it is silent."""
    fine = ast.parse(
        "def f(items, flag):\n"
        "    if flag:\n"
        "        acc = {}\n"
        "    else:\n"
        "        acc = {}\n"
        "    return [F(path=p, content=c) for p, c in acc.items()]\n"
    )
    assert conditionally_bound_then_read(fine) == []

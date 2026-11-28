"""A list or a sweep of builds never reads the builds' files (BOSSAPP-1506).

The laptop's API grew to 5.2 GB in 18 h. Its heaps (read from /proc, the process untouched) held
the source files and package-lock.json of saved build workspaces in FREED chunks: the console's
overview and the monitor tick selected `code_task_runs.finalize_ctx` whole over a project's runs,
and that column carries each build's saved workspace, pending diff and inherited design
(46 MB of its 49 MB on that install). Every call moved ~34 MB of JSON and parsed it into ~90 MB of
strings on a pool thread, and glibc kept each thread's high-water mark.

Three checks:
* the class, statically: a query that reads `finalize_ctx` (or `*`) from code_task_runs for MORE
  than one run selects `run_payload.lean_finalize_ctx()` / `run_columns()` instead, or is named
  in ALLOWED with the reason it needs the files;
* the behaviour: the hot routes over install-shaped runs allocate a bounded amount per call and
  keep nothing between calls (tracemalloc);
* RUN_COLUMNS is the table's real column list, so `run_columns()` is `SELECT *` minus the files.
"""
from __future__ import annotations

import ast
import gc
import json
import pathlib
import re
import threading
import tracemalloc
import uuid

from sqlalchemy import text

ROOT = pathlib.Path(__file__).resolve().parents[3]

#: Multi-row readers that keep the whole column, each with why. Shrink this list; never grow it
#: without the reason a person can check.
ALLOWED = {
    # Boot warm-up: reads the widest rows ONCE so the first /v1/code-tasks is not a cold read.
    "apps/api/app/main.py::_warm",
    # One sandbox's runs, read to find its stored plan when it is destroyed.
    "apps/api/app/main.py::sandbox_destroy_endpoint",
    # They need the files themselves: the app's written files
    # and held diffs (app_root), the verifier ratchet's re-apply, template apply/secret scan of
    # what builds wrote, and the share scrubber (it must see every byte it scrubs).
    "engine/boss_engine/services/app_root.py",
    "engine/boss_engine/services/verifier_ratchet.py",
    "engine/boss_engine/templates/apply.py",
    "engine/boss_engine/templates/secrets.py",
    "engine/boss_engine/services/share_scrub.py",
    # One subject's few runs, on a person's click (not a tick, not a polled list): one change set,
    # one app's security scope, one review URL, one PR's findings, one task's completed run, one
    # PR's re-review, one release's ids, one Shepherd escalation set, the newest completed run.
    "engine/boss_engine/multirepo/change_sets.py",
    "engine/boss_engine/security_scan/scope.py",
    "engine/boss_engine/services/change_review.py",
    "engine/boss_engine/services/finding_overrides.py",
    "engine/boss_engine/services/intent_router.py",
    "engine/boss_engine/services/pr_review_again.py",
    "engine/boss_engine/services/release_scope.py",
    "engine/boss_engine/services/shepherd_service.py",
    "engine/boss_engine/services/metatron_captain.py",
    # Not yet reviewed for which keys they read: project pages and a precheck, LIMIT <= 50.
    # Each is a follow-up to move onto lean_finalize_ctx (BOSSAPP-1506), not a reason to add more.
    "engine/boss_engine/services/project_experience/work.py",
    "engine/boss_engine/services/project_experience/work_story.py",
    "engine/boss_engine/services/project_experience/your_app.py",
    "engine/boss_engine/services/workflow_precheck_behaviour.py",
    "engine/boss_engine/tools/builtin.py",
}

# `finalize_ctx` as a selected column (not `… AS finalize_ctx`, a projection named like it), or `*`.
_WHOLE = re.compile(r"(?:(?:^|[\s,(])(?<!AS )(?:\w+\.)?finalize_ctx\s*(?:,|$|\)|\s+AS\b)"
                    r"|(?:^|[\s,])(?:\w+\.)?\*\s*(?:,|$))", re.I)
_ONE_RUN = re.compile(r"\bWHERE\s+(?:\w+\.)?id\s*=\s*(?::|%\()", re.I)


def _sql_text(node: ast.AST) -> str:
    """The literal text of a string expression, other parts as '?' (f-strings, + concatenation)."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(v.value if isinstance(v, ast.Constant) else " ? " for v in node.values)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _sql_text(node.left) + _sql_text(node.right)
    return " ? "


def whole_column_readers() -> list[str]:
    found = []
    for path in sorted([*ROOT.glob("apps/api/app/**/*.py"), *ROOT.glob("engine/boss_engine/**/*.py")]):
        rel = str(path.relative_to(ROOT))
        if "/tests/" in rel:
            continue
        tree = ast.parse(path.read_text())
        # each string node -> its innermost function (BFS order); module-level SQL constants
        # (a query kept in a NAME = "SELECT …" at the top of a module) belong to "<module>"
        owner: dict[int, str] = {id(n): "<module>" for n in ast.walk(tree)
                                 if isinstance(n, (ast.Constant, ast.JoinedStr, ast.BinOp))}
        for fn in ast.walk(tree):
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for node in ast.walk(fn):
                    if isinstance(node, (ast.Constant, ast.JoinedStr, ast.BinOp)):
                        owner[id(node)] = fn.name
        for node in ast.walk(tree):
            if id(node) in owner:
                fn_name = owner[id(node)]
                sql = _sql_text(node)
                for m in re.finditer(r"SELECT((?:(?!SELECT).)*?)FROM\s+code_task_runs\b(.{0,400})", sql, re.S | re.I):
                    cols, rest = m.group(1), m.group(2)
                    if "SELECT" in cols.upper() or not _WHOLE.search(cols):
                        continue
                    if _ONE_RUN.search(rest.split("ORDER BY")[0]):
                        continue
                    found.append(f"{rel}::{fn_name}")
    return sorted(set(found))


def test_no_multi_row_read_of_the_runs_takes_their_files():
    offenders = [f for f in whole_column_readers()
                 if f not in ALLOWED and f.split("::")[0] not in ALLOWED]
    assert not offenders, (
        "These read code_task_runs.finalize_ctx (or *) whole for many runs, which pulls every "
        "build's saved workspace into the API (BOSSAPP-1506). Select "
        "boss_engine.services.run_payload.lean_finalize_ctx() / run_columns(), or add the reader "
        f"to ALLOWED with the reason it needs the files: {offenders}")


def test_the_guard_sees_the_readers_it_was_written_for():
    # The scanner itself: the shape that leaked is caught, the single-run read is not.
    leaked = ast.parse('def overview(c):\n    c.execute("SELECT id, spec, finalize_ctx, state FROM '
                       'code_task_runs WHERE project_id = :p ORDER BY created_at DESC LIMIT 2000")')
    one = ast.parse('def get(c):\n    c.execute("SELECT finalize_ctx FROM code_task_runs WHERE id = :i")')
    lean = ast.parse('def ok(c):\n    c.execute("SELECT id, " + lean_finalize_ctx() + " FROM code_task_runs '
                     'WHERE project_id = :p")')

    def hits(tree):
        return [n for n in ast.walk(tree) if isinstance(n, (ast.Constant, ast.BinOp))
                and _WHOLE.search((re.search(r"SELECT(.*?)FROM\s+code_task_runs(.{0,400})",
                                             _sql_text(n), re.S) or [None, ""])[1] or "")
                and not _ONE_RUN.search((re.search(r"FROM\s+code_task_runs(.{0,400})", _sql_text(n), re.S)
                                         or [None, ""])[1] or "")]
    assert hits(leaked) and not hits(one) and not hits(lean)


def test_run_columns_is_the_whole_row(engine):
    from boss_engine.services.run_payload import RUN_COLUMNS

    with engine.connect() as c:
        real = [r[0] for r in c.execute(text(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'code_task_runs' "
            "AND table_schema = current_schema() ORDER BY ordinal_position"))]
    assert list(RUN_COLUMNS) == real, "run_columns() must stay SELECT * minus the files"


# --------------------------------------------------------------------------------------------- #
# The behaviour: hot routes over install-shaped builds
# --------------------------------------------------------------------------------------------- #
def _seed_builds(engine, project: str, runs: int) -> list[str]:
    body = ("export function Page() { return <div className=\"px-4\">{rows}</div> }\n" * 40)[:3000]
    ids = []
    with engine.begin() as c:
        for i in range(runs):
            run_id = f"ctr_soak_{uuid.uuid4().hex[:16]}"
            ids.append(run_id)
            ctx = {"saved_workspace": {"after": "abc", "file_count": 100,
                                       "files": [{"path": f"src/f{j}.tsx", "content": body} for j in range(100)]},
                   "pending_diff": [{"path": "src/a.tsx", "content": body}] * 5,
                   "inherited_design": {"doc": body},
                   "diagnosis": {"at": "2026-11-27T00:00:00+00:00", "cause_code": "x"} if i % 3 == 0 else None,
                   "app_id": f"app{i % 6}"}
            c.execute(text(
                "INSERT INTO code_task_runs (id, project_id, status, phase, title, spec, state, finalize_ctx) "
                "VALUES (:i, :p, :s, 'done', :t, CAST(:sp AS jsonb), '{}'::jsonb, CAST(:f AS jsonb))"),
                {"i": run_id, "p": project, "s": ("blocked", "failed", "completed")[i % 3], "t": f"build {i}",
                 "sp": json.dumps({"repo": "r", "instructions": "build it"}),
                 "f": json.dumps({k: v for k, v in ctx.items() if v is not None})})
    return ids


HOT = [
    ("get", "/v1/code-tasks?project_id=boss-dogfood&limit=200&exclude_internal=true", None),
    ("get", "/v1/console/v2/overview?project_id=boss-dogfood", None),
    ("post", "/v1/monitoring/tick", None),
    ("post", "/v1/factory/flow/tick", {"project_id": "boss-dogfood", "actor": "system"}),
    ("post", "/v1/shepherd/watch", {}),
]


def test_the_hot_routes_stay_small_and_keep_nothing(client, engine):
    """40 builds carrying ~330 KB of files each (13 MB in all, the install's shape). Before the
    fix the overview alone allocated ~45 MB per call; the budget is 6 MB per call, and fifteen
    rounds after the warm ones may not leave more than 3 MB behind."""
    _seed_builds(engine, "boss-dogfood", 40)

    def call(method, path, body):
        r = client.get(path) if method == "get" else client.post(path, json=body)
        assert r.status_code == 200, (path, r.status_code, r.text[:300])

    for spec in HOT:          # warm: imports, caches, schema checks
        call(*spec)
    gc.collect()
    tracemalloc.start()
    try:
        threads = threading.active_count()
        peaks: dict[str, int] = {}
        base = 0
        for round_ in range(20):
            if round_ == 5:       # the first rounds fill bounded caches (compiled SQL, memos)
                gc.collect()
                base, _ = tracemalloc.get_traced_memory()
            for spec in HOT:
                before, _ = tracemalloc.get_traced_memory()
                tracemalloc.reset_peak()
                call(*spec)
                _, peak = tracemalloc.get_traced_memory()
                peaks[spec[1]] = max(peaks.get(spec[1], 0), peak - before)
        gc.collect()
        kept = tracemalloc.get_traced_memory()[0] - base
    finally:
        tracemalloc.stop()
    over = {path: f"{n / 1e6:.1f} MB" for path, n in peaks.items() if n > 6_000_000}
    assert not over, f"a hot route read the builds' files again: {over}"
    # TestClient runs each request on a fresh event loop it never frees (anyio's RunVar table):
    # ~6 KB a request here, uvicorn has one loop. Anything past that is the app keeping data.
    assert kept < 3_000_000, f"{kept / 1e6:.1f} MB kept across {15 * len(HOT)} calls"
    assert threading.active_count() <= threads + 2

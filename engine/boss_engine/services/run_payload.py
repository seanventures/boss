"""A build's FILES stay in the database when a list or a sweep reads its runs (BOSSAPP-1506).

`code_task_runs.finalize_ctx` carries, besides the few dozen small facts every screen reads, the
build's files: the workspace saved for a resume (`saved_workspace`, every file's content), the
diff a verifier re-applies (`pending_diff`) and the design a follow-up inherits
(`inherited_design`). On the laptop install those three keys were 46 MB of the column's 49 MB,
about 300 KB a run. A screen or a tick that selected the whole column over a project's runs
pulled all of it on every call: the console's overview (2,000 runs, 90 days) moved ~34 MB of JSON
per render and parsed it into ~90 MB of Python strings, once a minute, on a worker thread. The
API's RSS climbed to 5.2 GB in 18 h because glibc kept every thread's high-water mark (see
services/memory_trim.py).

So a MULTI-ROW read of the runs selects `lean_finalize_ctx()` instead of `finalize_ctx`: the same
object without those keys. The code that needs the files (resume, repair, the verifier ratchet,
build detail) reads ONE run by id, which keeps the whole column. Enforced by
apps/api/tests/test_a_run_list_never_reads_the_build_files.py.
"""
from __future__ import annotations

#: finalize_ctx keys that hold a build's files rather than facts about it.
WORKSPACE_KEYS = ("saved_workspace", "pending_diff", "inherited_design")


#: Every column of code_task_runs, in table order: what `SELECT *` returned, for a multi-row read
#: that wants the whole row but not the files (`run_columns`). A test holds it to the schema.
RUN_COLUMNS = ("id", "project_id", "task_id", "status", "phase", "title", "spec", "state", "usage",
               "finalize_ctx", "reservation_id", "pull_request_url", "pull_request_number",
               "decision_id", "error", "created_at", "updated_at", "owner_token", "state_version",
               "fence_epoch", "work_item_id", "execution_policy")


def lean_finalize_ctx(alias: str = "") -> str:
    """`finalize_ctx` minus WORKSPACE_KEYS, still named `finalize_ctx`, for a SELECT list.
    `alias` is the table alias the query uses (`"c"` → `c.finalize_ctx`)."""
    column = f"{alias}.finalize_ctx" if alias else "finalize_ctx"
    stripped = " - ".join([column, *(f"'{key}'" for key in WORKSPACE_KEYS)])
    return f"({stripped}) AS finalize_ctx"


def run_columns(alias: str = "") -> str:
    """`SELECT *` of code_task_runs with `lean_finalize_ctx` in place of the whole column."""
    prefix = f"{alias}." if alias else ""
    return ", ".join(lean_finalize_ctx(alias) if name == "finalize_ctx" else prefix + name
                     for name in RUN_COLUMNS)


__all__ = ["RUN_COLUMNS", "WORKSPACE_KEYS", "lean_finalize_ctx", "run_columns"]

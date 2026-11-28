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
object without the files (paths and sizes of a pending diff, the plan of an inherited design). The code that needs the files (resume, repair, the verifier ratchet,
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


#: What a list keeps of a parent's inherited design: the plan a build page shows, not its spec,
#: review, packages and standards (build_detail reads `.plan`).
DESIGN_KEPT = ("plan", "plan_ok", "source", "prd_decision_id")


def lean_finalize_ctx(alias: str = "", *, workspace_paths: bool = False) -> str:
    """`finalize_ctx` without the build's files, still named `finalize_ctx`, for a SELECT list.
    `alias` is the table alias the query uses (`"c"` → `c.finalize_ctx`).

    * `saved_workspace` is dropped;
    * `pending_diff` becomes `[{path, bytes}]`, the shape the API's list rows always sent
      (main.without_internal_bulk), computed in SQL so the bodies never leave the database;
    * `inherited_design` keeps only DESIGN_KEPT;
    * with `workspace_paths`, `saved_workspace` stays as `{file_count, files: [{path}]}` for a
      reader of which files the builds wrote (project_scope, app_root._paths_of).
    A value that is not a JSON object (an old row, NULL) is passed through as it is."""
    x = f"{alias}.finalize_ctx" if alias else "finalize_ctx"
    pending = (f"(SELECT COALESCE(jsonb_agg(CASE WHEN jsonb_typeof(f) = 'object' "
               f"THEN jsonb_build_object('path', f->'path', 'bytes', length(COALESCE(f->>'content', ''))) "
               f"ELSE f END), '[]'::jsonb) FROM jsonb_array_elements({x}->'pending_diff') AS f)")
    design = "jsonb_strip_nulls(jsonb_build_object(" + ", ".join(
        f"'{k}', {x}->'inherited_design'->'{k}'" for k in DESIGN_KEPT) + "))"
    lean = (f"({x} - {', '.join(repr(k) for k in WORKSPACE_KEYS).replace(', ', ' - ')}"
            f" || CASE WHEN jsonb_typeof({x}->'pending_diff') = 'array' "
            f"THEN jsonb_build_object('pending_diff', {pending}) ELSE '{{}}'::jsonb END"
            f" || CASE WHEN jsonb_typeof({x}->'inherited_design') = 'object' "
            f"THEN jsonb_build_object('inherited_design', {design}) ELSE '{{}}'::jsonb END"
            + (f" || CASE WHEN jsonb_typeof({x}->'saved_workspace'->'files') = 'array' "
               f"THEN jsonb_build_object('saved_workspace', jsonb_build_object("
               f"'file_count', {x}->'saved_workspace'->'file_count', 'files', "
               f"(SELECT COALESCE(jsonb_agg(jsonb_build_object('path', w->'path')), '[]'::jsonb) "
               f"FROM jsonb_array_elements({x}->'saved_workspace'->'files') AS w "
               f"WHERE jsonb_typeof(w) = 'object'))) ELSE '{{}}'::jsonb END" if workspace_paths else "")
            + ")")
    return f"(CASE WHEN jsonb_typeof({x}) = 'object' THEN {lean} ELSE {x} END) AS finalize_ctx"


def run_columns(alias: str = "") -> str:
    """`SELECT *` of code_task_runs with `lean_finalize_ctx` in place of the whole column."""
    prefix = f"{alias}." if alias else ""
    return ", ".join(lean_finalize_ctx(alias) if name == "finalize_ctx" else prefix + name
                     for name in RUN_COLUMNS)


__all__ = ["DESIGN_KEPT", "RUN_COLUMNS", "WORKSPACE_KEYS", "lean_finalize_ctx", "run_columns"]

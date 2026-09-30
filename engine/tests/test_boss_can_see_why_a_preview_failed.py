"""BOSS could not fix a failing preview because it could not SEE the failure.

"preview not running (status=failed) … this always fails but BOSS agent could not fix it."

The reason was not reasoning. `list_previews` selected `id, run_id, status, url, open_url,
substrate` from a row that also carries `error` and `failure_kind`, and no tool read a preview's
log at all. So asked why a preview was failing, BOSS had the word "failed" and nothing else, and
did the only thing left: gave advice.

The evidence was one column away. prev_52cc502f's error reads "app did not serve HTTP within
timeout. error: x Build failed in 19ms | Could not resolve entry module \"index.html\"" — a Vite
build failure, which names the fault exactly and points at the app, not at BOSS.
"""
import inspect

from boss_engine.tools.builtin import build_default_registry, build_discuss_read_registry


def _spec(name: str):
    # The DEFAULT registry, because the discuss one wraps every handler to pin project_id —
    # reading the wrapper's source would assert nothing about the tool.
    return build_default_registry().get(name)


def test_a_failed_preview_reports_why():
    source = inspect.getsource(_spec("list_previews").handler)
    assert '"failure_kind": p.get("failure_kind")' in source
    assert '"error": p.get("error")' in source


def test_only_for_the_failed_ones():
    """A running preview's row carries an empty error; printing it on every entry is noise."""
    source = inspect.getsource(_spec("list_previews").handler)
    assert 'if p.get("status") == "failed" else {}' in source


def test_the_startup_log_is_readable():
    assert "read_preview_log" in build_discuss_read_registry().names()


def test_the_log_tool_is_fenced_to_the_project():
    """A preview id is keyed alone in the snapshot, so the tenant check belongs in the tool."""
    source = inspect.getsource(_spec("read_preview_log").handler)
    assert 'owner["project_id"] != pid' in source


def test_the_log_tool_hands_back_the_run_that_can_be_fixed():
    """Every repair tool for an app takes a crun_, not a prev_. Without it the agent has a
    diagnosis and no handle to act on."""
    source = inspect.getsource(_spec("read_preview_log").handler)
    assert 'snapshot["run_id"] = owner["run_id"]' in source


def test_the_agent_is_told_where_the_repair_lives():
    """Seeing the fault is half of it — the tools that FIX an app are named in the description,
    because "the preview is broken" does not obviously route to "edit the app's code"."""
    described = _spec("read_preview_log").description
    assert "extend_code_task" in described
    assert "start_preview" in described
    assert "the APP, not in BOSS" in described


def test_and_that_it_can_actually_run_those():
    reachable = set(build_discuss_read_registry().names())
    assert {"extend_code_task", "steer_code_task", "start_preview"} <= reachable

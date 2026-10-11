"""crun_966aa44e (HelpDesk, login-oauth template): the coder `cp -r`-ed the read-only template
reference (files 0444, dirs 0555) into its workspace, and an hour later BOSS's own write to
`helpdesk-ai/auth/config.mjs` (the mutation probe's restore) raised EACCES and failed the build
as `infra`. The same run spent 3 x 300s re-downloading Chromium.

Pinned here: (a) the workspace is made writable after every agent turn and on a BOSS-side
EACCES; (b) an agent's write to such a file is a tool result, never a crash; (c) an infra
failure gets one automatic retry; (d) Playwright browsers are installed once per install and
reused."""
from __future__ import annotations

import inspect
import os
import stat
from contextlib import contextmanager
from pathlib import Path

import pytest

from boss_engine.services import workspace_perms


def _read_only_copy(root: Path) -> Path:
    """What `cp -r $BOSS_TEMPLATE_DIR/auth .` leaves: 0555 dirs, 0444 files."""
    auth = root / "helpdesk-ai" / "auth"
    auth.mkdir(parents=True)
    f = auth / "config.mjs"
    f.write_text("export default {}\n")
    os.chmod(f, 0o444)
    os.chmod(auth, 0o555)
    return f


def _restore(root: Path) -> None:
    for dirpath, _d, _f in os.walk(root):
        os.chmod(dirpath, 0o755)


@pytest.fixture
def ws(tmp_path):
    yield tmp_path
    _restore(tmp_path)


# ---- (a) the workspace stays writable ---------------------------------------------------------

def test_normalize_makes_a_read_only_copy_writable(ws):
    f = _read_only_copy(ws)
    assert workspace_perms.normalize(ws) >= 2
    assert os.stat(f).st_mode & stat.S_IWUSR
    assert os.stat(f.parent).st_mode & stat.S_IWUSR
    f.write_text("changed\n")                      # the write that killed crun_966aa44e


def test_normalize_skips_node_modules_and_git(ws):
    for name in ("node_modules", ".git"):
        d = ws / name
        d.mkdir()
        (d / "x").write_text("x")
        os.chmod(d / "x", 0o444)
    workspace_perms.normalize(ws)
    assert not os.stat(ws / "node_modules" / "x").st_mode & stat.S_IWUSR
    assert not os.stat(ws / ".git" / "x").st_mode & stat.S_IWUSR


def test_a_boss_side_write_retries_after_eacces(ws):
    f = _read_only_copy(ws)
    workspace_perms.write_text(f, "restored\n")
    assert f.read_text() == "restored\n"


def test_the_mutation_restore_goes_through_the_retrying_write():
    from boss_engine.services import governed_code_task_service as g

    src = inspect.getsource(g)
    assert "(root / p).write_text(" not in src
    assert "_ws_write(root / p, content)" in src


def test_every_agent_tidies_the_workspace_after_its_turn():
    from boss_engine.services.cli_harness import GenericCliCodeAgent
    from boss_engine.services.code_agent import ClaudeCodeAgent, NativeCodeAgent

    for cls in (ClaudeCodeAgent, NativeCodeAgent, GenericCliCodeAgent):
        assert getattr(cls.run_role, "__boss_tidies_workspace__", False), cls.__name__


def test_tidies_workspace_runs_after_a_writing_turn_only(ws):
    f = _read_only_copy(ws)

    class Agent:
        @workspace_perms.tidies_workspace
        def run_role(self, prompt, cwd, *, read_only=False):
            return {"summary": "ok"}

    Agent().run_role("p", ws, read_only=True)
    assert not os.stat(f).st_mode & stat.S_IWUSR
    Agent().run_role("p", ws)
    assert os.stat(f).st_mode & stat.S_IWUSR


def test_a_file_the_sandbox_uid_owns_is_fixed_as_that_uid(ws, monkeypatch):
    """BOSS cannot chmod another uid's file: it runs chmod AS the run's uid (the helper)."""
    _read_only_copy(ws)
    calls = []
    monkeypatch.setattr(workspace_perms, "_fix_one", lambda p, st, me: None)
    monkeypatch.setattr(workspace_perms, "_identity", lambda root: type("I", (), {"uid": 61001})())
    monkeypatch.setattr(workspace_perms, "_as_run_uid",
                        lambda ident, script, *a: calls.append((ident.uid, script, a)) or True)
    workspace_perms.normalize(ws)
    assert calls and calls[0][0] == 61001 and "chmod ug+rw" in calls[0][1]
    assert "node_modules" in calls[0][1] and calls[0][2] == (str(ws),)


# ---- (b) an agent's write is a tool result, never a crash -------------------------------------

def test_the_agent_can_edit_a_read_only_copy(ws):
    from boss_engine.services.code_agent_tools import SandboxToolset

    _read_only_copy(ws)
    tools = SandboxToolset(ws)
    out = tools.call("edit_file", {"path": "helpdesk-ai/auth/config.mjs",
                                   "old_string": "{}", "new_string": "{ a: 1 }"})
    assert not out.is_error, out.text
    assert "a: 1" in (ws / "helpdesk-ai/auth/config.mjs").read_text()


def test_an_unfixable_permission_error_is_a_tool_error(ws, monkeypatch):
    from boss_engine.services import code_agent_tools

    _read_only_copy(ws)
    monkeypatch.setattr(workspace_perms, "make_writable", lambda *a, **k: None)
    out = code_agent_tools.SandboxToolset(ws).call(
        "write_file", {"path": "helpdesk-ai/auth/config.mjs", "content": "x"})
    assert out.is_error
    assert "not writable" in out.text and "chmod u+w" in out.text


def test_make_writable_never_leaves_the_workspace(ws, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside") / "f"
    outside.write_text("x")
    os.chmod(outside, 0o444)
    workspace_perms.make_writable(outside, within=ws)
    assert not os.stat(outside).st_mode & stat.S_IWUSR
    os.chmod(outside, 0o644)


# ---- (c) one automatic retry for an infra failure ---------------------------------------------

class _Conn:
    def __init__(self, log):
        self.log = log

    def execute(self, stmt, params=None):
        self.log.append(("sql", str(stmt), params))


def _wire(monkeypatch, log, retry):
    from boss_engine.services import code_task_service
    from boss_engine.services import governed_code_task_service as g

    @contextmanager
    def tx(_engine):
        yield _Conn(log)

    monkeypatch.setattr(g, "_transaction", tx)
    monkeypatch.setattr(g, "emit_event", lambda c, p, kind, *a: log.append(("event", kind, a[-1])))
    monkeypatch.setattr(code_task_service, "retry_code_task", retry)
    return g


def test_an_infra_failure_is_retried_once_automatically(monkeypatch):
    log, retried = [], []

    def retry(engine, settings, jobs, run_id, **kw):
        retried.append(run_id)
        return {"status": "queued", "run_id": "crun_next"}

    g = _wire(monkeypatch, log, retry)
    out = g._retry_after_infra_failure(None, None, None, "crun_a", "p", {}, {"status": "failed"})
    assert retried == ["crun_a"]
    assert out["auto_retry_run_id"] == "crun_next" and out["status"] == "failed"
    flagged = [e for e in log if e[0] == "sql"]
    assert flagged and flagged[0][2] == {"k": g.AUTO_INFRA_RETRY_KEY, "id": "crun_next"}
    assert any(e[0] == "event" and e[1] == "code_task.auto_retried" for e in log)


def test_the_automatic_retry_is_not_retried_again(monkeypatch):
    log, retried = [], []
    g = _wire(monkeypatch, log, lambda *a, **k: retried.append(1))
    out = g._retry_after_infra_failure(None, None, None, "crun_b", "p",
                                       {g.AUTO_INFRA_RETRY_KEY: True}, {"status": "failed"})
    assert retried == []
    assert "press Retry" in out["next_step"]


def test_a_refused_retry_leaves_a_plain_message(monkeypatch):
    log = []

    def retry(*a, **k):
        raise ValueError("circuit breaker")

    g = _wire(monkeypatch, log, retry)
    out = g._retry_after_infra_failure(None, None, None, "crun_c", "p", {}, {"status": "failed"})
    assert out["auto_retry"] == "refused" and "press Retry" in out["next_step"]
    assert any(e[0] == "event" and e[1] == "code_task.auto_retry_failed" for e in log)


def test_the_failure_handler_calls_the_retry_for_infra_only():
    from boss_engine.services import governed_code_task_service as g

    src = inspect.getsource(g)
    assert 'if fclass == "infra":' in src and "_retry_after_infra_failure(" in src


# ---- (d) Playwright browsers once per install -------------------------------------------------

@pytest.fixture
def pw_cache(tmp_path, monkeypatch):
    root = tmp_path / "toolchain-cache"
    root.mkdir(mode=0o700)
    monkeypatch.setenv("BOSS_TOOLCHAIN_CACHE_ROOT", str(root))
    return root


def _app(tmp_path, version="1.48.2"):
    app = tmp_path / "app"
    pkg = app / "node_modules" / "@playwright" / "test"
    pkg.mkdir(parents=True)
    (pkg / "package.json").write_text('{"name": "@playwright/test", "version": "%s"}' % version)
    return app


def test_browsers_are_downloaded_once_and_reused(tmp_path, pw_cache):
    from boss_engine.services import playwright_cache

    runs = []

    def runner(argv, *, env, cwd, timeout):
        runs.append((argv, env["PLAYWRIGHT_BROWSERS_PATH"], timeout))
        (Path(env["PLAYWRIGHT_BROWSERS_PATH"]) / "chromium-1140").mkdir()
        return 0, "installed"

    version = playwright_cache.project_version(_app(tmp_path))
    assert version == "1.48.2"
    first = playwright_cache.ensure(version, runner=runner)
    second = playwright_cache.ensure(version, runner=runner)
    assert first["ok"] and not first["cached"]
    assert second["ok"] and second["cached"]
    assert len(runs) == 1                                # later builds skip the install
    argv, path, timeout = runs[0]
    assert argv == ["npx", "--yes", "playwright@1.48.2", "install", "chromium"]
    assert path == str(pw_cache / "playwright")
    assert timeout >= 300                                # long enough for a cold download
    # builds read and run them; the cache root is traversable, not listable
    assert stat.S_IMODE(os.stat(pw_cache / "playwright").st_mode) == 0o755
    assert stat.S_IMODE(os.stat(pw_cache).st_mode) & 0o011 == 0o011
    assert not stat.S_IMODE(os.stat(pw_cache).st_mode) & 0o004


def test_a_failed_download_is_not_marked_cached(tmp_path, pw_cache):
    from boss_engine.services import playwright_cache

    out = playwright_cache.ensure("1.48.2", runner=lambda *a, **k: (1, "network down"))
    assert not out["ok"] and "network down" in out["detail"]
    assert not playwright_cache.is_cached("1.48.2")


def test_a_version_that_is_not_a_version_is_never_installed(tmp_path, pw_cache):
    from boss_engine.services import playwright_cache

    assert playwright_cache.project_version(_app(tmp_path, "1.0.0; rm -rf /")) is None
    assert not playwright_cache.ensure("latest && curl x")["ok"]


def test_the_sandbox_points_builds_at_the_shared_browsers(pw_cache):
    from boss_engine.services import sandbox

    assert sandbox._shared_tool_caches()["PLAYWRIGHT_BROWSERS_PATH"] == str(pw_cache / "playwright")


def test_both_verify_paths_use_the_shared_cache():
    from boss_engine.services import governed_code_task_service as g, stack_verify

    assert "playwright_cache.ensure(" in inspect.getsource(g)
    assert "_ensure_browsers(plan, record)" in inspect.getsource(stack_verify.run_stack_plan)


def test_the_stack_plan_skips_a_covered_install_step(tmp_path, pw_cache, monkeypatch):
    from boss_engine.services import playwright_cache, stack_verify
    from boss_engine.services.verification_target import PLAYWRIGHT_INSTALL_CMD

    app = _app(tmp_path)
    monkeypatch.setattr(playwright_cache, "ensure", lambda v, **k: {"ok": True, "cached": True})
    plan = type("P", (), {"e2e_steps": [(PLAYWRIGHT_INSTALL_CMD, app), ("npm run test:e2e", app)]})()
    record: dict = {}
    covered = stack_verify._ensure_browsers(plan, record)
    assert covered == {(PLAYWRIGHT_INSTALL_CMD, str(app))}
    assert record["browsers"]["ok"]

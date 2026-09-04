"""BOSS could not open the source of the apps it writes, and did not know it.

An operator pasted an error from a preview — "❌ X-Request-ID header is required" — and named
the run it came from. The answer was two greps away: the app's client sends that header, its
server requires it, and BOSS's own preview proxy was stripping it in transit. Instead BOSS
reported that it could not determine the cause and suggested fetching container logs.

The tool existed. `search_code` takes a repo and greps it. What it did not take was a REF, and
it cloned the default branch — while an app BOSS builds lives on `boss/code/<run_id>` behind an
open PR, unmerged by design because a human gates the merge. `server/index.js` does not exist
on main. So the search ran against a repository that does not contain the app, found nothing,
and "nothing" was indistinguishable from "this code does not exist".

What is pinned here is the resolution: given a run id, BOSS finds the repo and the branch that
run's code is actually on — including the follow-up case, where a task commits onto the branch
of the run it continues and so lives under an EARLIER run's name.
"""
from __future__ import annotations

from contextlib import contextmanager

import pytest

from boss_engine.tools import builtin


class FakeResult:
    def __init__(self, row):
        self._row = row

    def mappings(self):
        return self

    def first(self):
        return self._row


class FakeConnection:
    def __init__(self, row):
        self._row = row
        self.queries: list[str] = []

    def execute(self, statement, params=None):
        self.queries.append(str(statement))
        return FakeResult(self._row)


class FakeCtx:
    """Just enough context: a connection and settings."""

    def __init__(self, row):
        self.connection = FakeConnection(row)
        self.settings = type("S", (), {
            "target_repo_host": "github", "target_github_owner": "paramg",
            "target_github_repo": "boss-sandbox", "github_token": "",
        })()

    @contextmanager
    def begin(self):
        yield self.connection


@pytest.fixture
def branches(monkeypatch):
    """Which branches the remote has. `git ls-remote` is answered from this set."""
    existing: set[str] = set()

    def fake_run(cmd, **kwargs):
        if "ls-remote" in cmd:
            wanted = cmd[-1]
            out = f"abc123\trefs/heads/{wanted}" if wanted in existing else ""
            return type("R", (), {"returncode": 0, "stdout": out, "stderr": ""})()
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(builtin._subprocess, "run", fake_run)
    return existing


class TestFindingTheCodeForARun:
    def test_the_branch_named_after_the_run(self, branches):
        """The ordinary case: a code task commits to boss/code/<its own run id>."""
        branches.add("boss/code/crun_abc")
        ctx = FakeCtx({"spec": {"owner": "paramg", "repo": "boss-sandbox"}})
        repo, ref, err = builtin._source_for_run(ctx, "crun_abc")
        assert (repo, ref, err) == ("paramg/boss-sandbox", "boss/code/crun_abc", None)

    def test_a_follow_up_run_lives_under_the_branch_it_continues(self, branches):
        """THE CASE THAT LOOKS LIKE A BUG.

        A follow-up task commits onto the branch of the run it continues, so its code is under
        an EARLIER run's name and `boss/code/<this run>` does not exist. Guessing the
        convention and stopping there finds an empty tree for exactly the runs an operator is
        most likely to be asking about — the ones that were already fixed once.
        """
        branches.add("boss/code/crun_earlier")
        ctx = FakeCtx({"spec": {"owner": "paramg", "repo": "boss-sandbox",
                                "base_branch": "boss/code/crun_earlier"}})
        repo, ref, err = builtin._source_for_run(ctx, "crun_followup")
        assert ref == "boss/code/crun_earlier"
        assert err is None

    def test_no_branch_matches_so_the_default_is_read_and_not_an_error(self, branches):
        """A merged app is on the default branch. Degrade to it; do not refuse the question."""
        ctx = FakeCtx({"spec": {"owner": "paramg", "repo": "boss-sandbox"}})
        repo, ref, err = builtin._source_for_run(ctx, "crun_merged")
        assert (repo, ref, err) == ("paramg/boss-sandbox", None, None)

    def test_an_unknown_run_says_so(self, branches):
        ctx = FakeCtx(None)
        _repo, _ref, err = builtin._source_for_run(ctx, "crun_nope")
        assert err and "crun_nope" in err

    def test_a_run_with_no_repository_says_so(self, branches):
        ctx = FakeCtx({"spec": {"owner": "paramg"}})
        _repo, _ref, err = builtin._source_for_run(ctx, "crun_x")
        assert err and "repositor" in err.lower()


class TestWhatTheSourceToolsResolve:
    def test_run_id_wins_over_repo(self, branches):
        """Passing both must not read one repo and cite another."""
        branches.add("boss/code/crun_abc")
        ctx = FakeCtx({"spec": {"owner": "paramg", "repo": "boss-sandbox"}})
        repo, ref, err = builtin._resolve_source(
            ctx, {"run_id": "crun_abc", "repo": "boss"})
        assert (repo, ref, err) == ("paramg/boss-sandbox", "boss/code/crun_abc", None)

    def test_an_explicit_ref_overrides_the_resolved_branch(self, branches):
        """An operator who names a ref means that ref."""
        branches.add("boss/code/crun_abc")
        ctx = FakeCtx({"spec": {"owner": "paramg", "repo": "boss-sandbox"}})
        _repo, ref, _err = builtin._resolve_source(
            ctx, {"run_id": "crun_abc", "ref": "v1.2.0"})
        assert ref == "v1.2.0"

    def test_without_a_run_id_it_is_boss_own_source(self, branches):
        ctx = FakeCtx(None)
        assert builtin._resolve_source(ctx, {}) == ("boss", None, None)

    def test_a_bad_run_id_is_reported_rather_than_silently_searching_boss(self, branches):
        """Falling back to BOSS's own source would answer the wrong question convincingly."""
        ctx = FakeCtx(None)
        repo, _ref, err = builtin._resolve_source(ctx, {"run_id": "crun_nope"})
        assert err and repo == ""


class TestNoSourceToolIsBlind:
    """The guard that makes this stop recurring.

    Three tools read a repository and only one of them was ever taught that an app lives on a
    branch — which is how `search_code` was fixed once for one app and `git_history` stayed
    broken. This asserts the property across the whole toolkit, including tools nobody has
    written yet: if a tool can be pointed at a customer repo, it must accept the run whose code
    the operator is asking about.
    """

    #: Every tool that resolves a repository. Adding one without run_id fails this test.
    SOURCE_TOOLS = {"search_code", "read_code", "list_source", "git_history"}

    def _schemas(self):
        reg = builtin.build_default_registry()
        return {spec["function"]["name"]: spec["function"] for spec in reg.openai_schema()}

    def test_every_source_tool_accepts_a_run_id(self):
        schemas = self._schemas()
        missing = [
            name for name in sorted(self.SOURCE_TOOLS)
            if "run_id" not in (schemas.get(name, {})
                                .get("parameters", {}).get("properties", {}))
        ]
        assert not missing, (
            f"{missing} can be pointed at a customer repo but cannot be pointed at the "
            "branch its code is on — the exact blindness that made BOSS unable to read the "
            "app it had just written"
        )

    def test_every_source_tool_accepts_an_explicit_ref(self):
        """An operator debugging a specific build needs to name it."""
        schemas = self._schemas()
        missing = [
            name for name in sorted(self.SOURCE_TOOLS)
            if "ref" not in (schemas.get(name, {})
                             .get("parameters", {}).get("properties", {}))
        ]
        assert not missing, f"{missing} cannot be pinned to a ref"

    def test_the_toolkit_can_actually_investigate_an_app(self):
        """The minimum set for debugging something BOSS built, present and registered.

        Reading a file is useless without knowing which files exist; grepping is useless
        without being able to read the hit; neither says whether the thing is running.
        """
        schemas = self._schemas()
        for name in ("list_source", "search_code", "read_code", "git_history",
                     "read_logs", "http_probe", "get_code_task"):
            assert name in schemas, f"BOSS cannot debug without {name}"

    def test_the_source_tools_say_which_ref_they_read(self):
        """"No matches" means nothing unless the answer says where it looked.

        This is the reporting half of the bug: a search of a branch that does not contain the
        app returned an empty list that read as "this code does not exist".
        """
        import inspect

        source = inspect.getsource(builtin)
        for marker in ('"ref": ref or "(default branch)"',):
            assert source.count(marker) >= 3, (
                "every source tool must report the ref it actually read")

"""GitHub intake for API onboarding (BOSSAPP-1169, BOSSAPP-1175), without a
database: a pull request's changed files, the `glob` filter op, and the repository poller
against a fake GitHub API.

* G11 — a `pull_request` envelope carried no changed-file list, so a route could not take only
  PRs touching `tenants/*.yaml`. The list is now fetched LAZILY (only when a route's filter reads
  it), page by page, capped, and a `glob` leaf matches it.
* G1  — an install GitHub cannot reach (a laptop on 127.0.0.1) never received a webhook. The
  poller reads open pull requests and branch heads since a cursor and hands back deliveries the
  same shape as GitHub's own, so the processor and every route treat them alike.
"""
from __future__ import annotations

import json

import pytest

from boss_engine.connectors.intake import get_intake_adapter
from boss_engine.connectors.intake.contract import (
    AuthFailed,
    Cursor,
    IntakeConnection,
    RateLimited,
    StoredRaw,
)
from boss_engine.connectors.intake.github import adapter as gh
from boss_engine.services import intake_filters as f

from ._intake_support import github_pr_opened, github_request

REPO = "northgate/northgate-tenants"


def _conn(**config) -> IntakeConnection:
    secrets = {"webhook_secret": "s", "api_token": config.pop("token", None)}
    return IntakeConnection("icn_gh", "p_1", "github", mode=config.pop("mode", "push"),
                            config={"repository": REPO, **config},
                            secret_resolver=secrets.get)


def _parse(adapter, conn, event, body, delivery="d-1"):
    req = github_request("s", event, body, delivery=delivery)
    raw = StoredRaw(body=req.body, headers=dict(req.headers),
                    dedupe_key=adapter.dedupe_key(conn, req))
    [ev] = adapter.parse(conn, raw)
    return adapter.enrich(conn, ev)


class FakeGitHub:
    """A GitHub API that answers the calls the adapter makes, and records them."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.pulls: list[dict] = []
        self.branches: list[dict] = []
        self.files: dict[int, list[dict]] = {}
        self.etag = {"pulls": 'W/"p1"', "branches": 'W/"b1"'}
        self.answer: tuple | None = None          # force one status for every call

    def __call__(self, method, url, headers, body, purpose):
        self.calls.append((url, dict(headers)))
        if self.answer is not None:
            return self.answer
        path = url.split("api.github.com", 1)[1]
        if "/files" in path:
            number = int(path.split("/pulls/")[1].split("/")[0])
            page = int(path.rsplit("page=", 1)[1])
            items = self.files.get(number, [])
            return 200, items[(page - 1) * 100: page * 100]
        name = "pulls" if path.startswith(f"/repos/{REPO}/pulls?") else "branches"
        if headers.get("If-None-Match") == self.etag[name]:
            return 304, None, {}
        data = self.pulls if name == "pulls" else self.branches
        return 200, data, {"ETag": self.etag[name]}


def _pr(number, sha, updated="2026-11-14T10:00:00Z", login="lumen-dev", kind="User"):
    pr = github_pr_opened(number, repo=REPO)["pull_request"]
    return {**pr, "head": {"ref": f"onboard-{number}", "sha": sha},
            "updated_at": updated, "user": {"login": login, "id": 7, "type": kind},
            "base": {"ref": "main", "sha": "a" * 40,
                     "repo": {"full_name": REPO, "private": True}}}


@pytest.fixture()
def fake(monkeypatch):
    api = FakeGitHub()
    monkeypatch.setattr(gh, "TRANSPORT", api)
    return api


# ── G11: a pull request's changed files, lazily ─────────────────────────────────────────────

def test_a_pull_request_marks_its_files_pending_and_fetches_them_only_when_asked(fake):
    adapter = get_intake_adapter("github")
    conn = _conn(token="ghp_SECRET")
    body = github_pr_opened(7, repo=REPO)
    fake.files[7] = ([{"filename": f"docs/{n}.md", "status": "modified"} for n in range(150)]
                     + [{"filename": "tenants/lumen-pay.yaml", "status": "added",
                         "additions": 40}])
    ev = _parse(adapter, conn, "pull_request", body)
    assert ev.data["changed_files"] is None and ev.data["changed_files_source"] == "pending"
    assert fake.calls == [], "nothing is fetched until a route's filter reads the list"
    assert adapter.lazy_fields == ("data.changed_files",)

    ev = adapter.load_lazy(conn, ev, "data.changed_files")
    assert ev.data["changed_files_source"] == "pulls_api"
    assert len(ev.data["changed_files"]) == 151 and not ev.data["changed_files_truncated"]
    assert ev.data["changed_files"][-1] == {"filename": "tenants/lumen-pay.yaml",
                                            "status": "added", "additions": 40, "deletions": 0}
    urls = [u for u, _ in fake.calls]
    assert urls == [f"https://api.github.com/repos/{REPO}/pulls/7/files?per_page=100&page=1",
                    f"https://api.github.com/repos/{REPO}/pulls/7/files?per_page=100&page=2"]
    assert fake.calls[0][1]["Authorization"] == "Bearer ghp_SECRET"
    assert "ghp_SECRET" not in json.dumps(ev.to_dict())
    # Loaded once: asking again costs nothing.
    adapter.load_lazy(conn, ev, "data.changed_files")
    assert len(fake.calls) == 2


def test_the_file_list_is_capped(fake):
    adapter = get_intake_adapter("github")
    conn = _conn()
    fake.files[7] = [{"filename": f"f{n}.txt"} for n in range(1000)]
    ev = adapter.load_lazy(conn, _parse(adapter, conn, "pull_request",
                                        github_pr_opened(7, repo=REPO)), "data.changed_files")
    assert len(ev.data["changed_files"]) == gh.MAX_CHANGED_FILES
    assert ev.data["changed_files_truncated"] is True
    assert len(fake.calls) == gh.MAX_CHANGED_FILES // gh.PAGE_SIZE


def test_a_failed_fetch_leaves_the_event_routable(fake):
    adapter = get_intake_adapter("github")
    conn = _conn()
    fake.answer = (404, {"message": "Not Found"})
    ev = adapter.load_lazy(conn, _parse(adapter, conn, "pull_request",
                                        github_pr_opened(7, repo=REPO)), "data.changed_files")
    assert ev.data["changed_files"] is None and ev.data["changed_files_source"] == "error"
    assert ev.data["changed_files_error"] == "GitHub answered 404"


# ── the glob op ─────────────────────────────────────────────────────────────────────────────

def _files(*names):
    return {"data": {"changed_files": [{"filename": n, "status": "added"} for n in names]}}


@pytest.mark.parametrize("pattern,names,expected", [
    ("tenants/*.yaml", ["README.md", "tenants/lumen-pay.yaml"], True),
    ("tenants/*.yaml", ["tenants/archive/old.yaml"], False),     # * stays in one segment
    ("tenants/*.yaml", ["README.md"], False),
    ("**/*.yaml", ["a/b/c.yaml"], True),
    ("**/*.yaml", ["c.yaml"], True),                             # ** may be zero segments
    ("tenants/[!_]*.y?ml", ["tenants/lumen.yaml"], True),
    ("tenants/[!_]*.y?ml", ["tenants/_draft.yaml"], False),
    (["docs/*", "tenants/*.yml"], ["tenants/x.yml"], True),
])
def test_glob_matches_paths_and_lists_of_files(pattern, names, expected):
    rule = f.compile_filter({"field": "data.changed_files", "op": "glob", "value": pattern})
    assert rule.matches(_files(*names)) is expected


def test_glob_over_a_plain_path_and_a_missing_field():
    rule = f.compile_filter({"field": "data.path", "op": "glob", "value": "src/**"})
    assert rule.matches({"data": {"path": "src/a/b.py"}})
    assert not rule.matches({"data": {}})
    assert not f.compile_filter({"field": "data.changed_files", "op": "glob",
                                 "value": "*"}).matches({"data": {"changed_files": None}})


@pytest.mark.parametrize("bad", ["", "/etc/*", "a//b", [], ["x"] * 17, "x" * 300, 5,
                                 "tenants/[abc", "tenants/[]", "tenants/[!]", "[z-a]*"])
def test_a_bad_glob_is_refused_when_saved(bad):
    with pytest.raises(f.FilterError):
        f.compile_filter({"field": "data.changed_files", "op": "glob", "value": bad})


def test_glob_is_linear_on_a_hostile_file_name():
    import time

    rule = f.compile_filter({"field": "subject", "op": "glob", "value": "*a*a*a*a*a*b"})
    started = time.monotonic()
    assert not rule.matches({"subject": "a" * 1000})
    assert time.monotonic() - started < 0.5


# ── G1: the poller ──────────────────────────────────────────────────────────────────────────

def _poll(adapter, conn, value=None, since="2026-11-14T09:00:00+00:00"):
    return adapter.poll(conn, Cursor("poll", value, since), 50)


def test_the_first_poll_is_a_baseline_then_new_heads_become_webhook_shaped_events(fake):
    adapter = get_intake_adapter("github")
    assert adapter.can("poll") and "poll" in adapter.manifest.modes
    conn = _conn(mode="poll")
    fake.pulls = [_pr(3, "1" * 40, updated="2026-11-13T08:00:00Z"),   # before polling began
                  _pr(4, "2" * 40)]                                   # since: emitted
    fake.branches = [{"name": "main", "commit": {"sha": "9" * 40}}]
    first = _poll(adapter, conn)
    assert [ev.type for ev in first.events] == ["com.github.pull_request.opened"]
    [ev] = first.events
    # The same envelope a webhook gives, so the same routes match it.
    assert ev.subject == f"{REPO}#4" and ev.data["record_id"] == "4"
    assert ev.data["head_sha"] == "2" * 40 and ev.data["base_ref"] == "main"
    assert ev.data["delivered_by"] == "poll" and ev.data["head_key"] == f"pr:{REPO}#4@{'2' * 40}"
    assert first.deliveries[0].dedupe_key == f"github:poll:pr:{REPO}#4@{'2' * 40}"
    assert first.deliveries[0].headers["x-github-event"] == "pull_request"
    cursor = json.loads(first.next_cursor.value)
    assert cursor["pulls"] == {"3": "1" * 40, "4": "2" * 40}
    assert cursor["branches"] == {"main": "9" * 40}

    # Nothing changed: conditional requests answer 304, nothing is emitted.
    again = _poll(adapter, conn, first.next_cursor.value)
    assert again.events == () and json.loads(again.next_cursor.value)["pulls"] == cursor["pulls"]
    assert fake.calls[-1][1]["If-None-Match"] == 'W/"b1"'

    # PR 4 moves, PR 5 opens, a tenant branch moves: synchronize, opened, push.
    fake.etag = {"pulls": 'W/"p2"', "branches": 'W/"b2"'}
    fake.pulls = [_pr(4, "3" * 40), _pr(5, "4" * 40)]
    fake.branches = [{"name": "main", "commit": {"sha": "9" * 40}},
                     {"name": "onboard-4", "commit": {"sha": "3" * 40}}]
    third = _poll(adapter, conn, again.next_cursor.value)
    kinds = sorted((ev.type, ev.subject) for ev in third.events)
    assert kinds == [("com.github.pull_request.opened", f"{REPO}#5"),
                     ("com.github.pull_request.synchronize", f"{REPO}#4"),
                     ("com.github.push", f"{REPO}@refs/heads/onboard-4")]
    assert json.loads(third.next_cursor.value)["pulls"] == {"4": "3" * 40, "5": "4" * 40}

    fake.etag = {"pulls": 'W/"p3"', "branches": 'W/"b3"'}
    fake.branches = [{"name": "main", "commit": {"sha": "9" * 40}},
                     {"name": "onboard-4", "commit": {"sha": "5" * 40}}]
    fourth = _poll(adapter, conn, third.next_cursor.value)
    [push] = [ev for ev in fourth.events if ev.type == "com.github.push"]
    assert push.subject == f"{REPO}@refs/heads/onboard-4"
    assert push.data["before"] == "3" * 40 and push.data["after"] == "5" * 40
    assert push.data["changed_files_source"] == "pending", "fetched by the compare, lazily"
    assert push.data["head_key"] == f"push:{REPO}@refs/heads/onboard-4@{'5' * 40}"


def test_a_new_branch_is_a_push_from_nothing(fake):
    adapter = get_intake_adapter("github")
    conn = _conn(mode="poll")
    first = _poll(adapter, conn)
    fake.etag = {"pulls": 'W/"p2"', "branches": 'W/"b2"'}
    fake.branches = [{"name": "feature", "commit": {"sha": "6" * 40}}]
    [ev] = _poll(adapter, conn, first.next_cursor.value).events
    assert ev.type == "com.github.push" and ev.data["created"] is True
    assert ev.data["before"] == "0" * 40


def test_poll_pushes_can_be_switched_off(fake):
    adapter = get_intake_adapter("github")
    conn = _conn(mode="poll", poll_pushes="false")
    first = _poll(adapter, conn)
    fake.etag = {"pulls": 'W/"p2"', "branches": 'W/"b2"'}
    fake.branches = [{"name": "feature", "commit": {"sha": "6" * 40}}]
    assert _poll(adapter, conn, first.next_cursor.value).events == ()


def test_a_bot_pull_request_is_still_our_own_echo(fake):
    adapter = get_intake_adapter("github")
    conn = _conn(mode="poll", bot_login="boss-localhost[bot]")
    fake.pulls = [_pr(9, "7" * 40, login="boss-localhost[bot]", kind="Bot")]
    [ev] = _poll(adapter, conn).events
    assert adapter.is_self(conn, ev), "BOSS's own fix PR is dropped, as it is by webhook"


def test_the_webhook_names_the_same_commit_as_the_poll():
    adapter = get_intake_adapter("github")
    conn = _conn()
    ev = _parse(adapter, conn, "pull_request", github_pr_opened(4, repo=REPO))
    assert ev.data["delivered_by"] == "webhook"
    assert ev.data["head_key"] == f"pr:{REPO}#4@{'b' * 40}"
    closed = {**github_pr_opened(4, repo=REPO), "action": "closed"}
    assert "head_key" not in _parse(adapter, conn, "pull_request", closed).data


@pytest.mark.parametrize("answer,raised", [
    ((401, {"message": "Bad credentials"}, {}), AuthFailed),
    ((403, {"message": "API rate limit"}, {"X-RateLimit-Remaining": "0"}), RateLimited),
    ((429, {}, {"Retry-After": "120"}), RateLimited),
    ((403, {"message": "Resource not accessible"}, {}), AuthFailed),
])
def test_a_refusal_or_a_rate_limit_is_raised_for_the_poller_to_back_off(fake, answer, raised):
    adapter = get_intake_adapter("github")
    fake.answer = answer
    with pytest.raises(raised) as error:
        _poll(adapter, _conn(mode="poll"))
    if answer[0] == 429:
        assert error.value.retry_after == 120


def test_a_polled_connection_needs_its_repository(fake):
    from boss_engine.connectors.intake.contract import ParseRefused

    conn = IntakeConnection("icn_gh", "p_1", "github", mode="poll", config={})
    with pytest.raises(ParseRefused):
        get_intake_adapter("github").poll(conn, Cursor("poll"), 50)

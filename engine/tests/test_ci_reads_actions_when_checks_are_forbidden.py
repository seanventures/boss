"""HelpDesk PR #223: a fine-grained PAT cannot read /check-runs (403); CI is read from Actions."""
import httpx

from boss_engine.services.ci_gate import HttpCiReader


def test_a_forbidden_check_runs_read_falls_back_to_actions_runs():
    calls = []

    def get(url, token):
        calls.append(url)
        if "/check-runs" in url:
            req = httpx.Request("GET", url)
            raise httpx.HTTPStatusError("403", request=req, response=httpx.Response(403, request=req))
        return {"workflow_runs": [{"name": "helpdesk-ci", "status": "completed",
                                   "conclusion": "success", "html_url": "u"}]}

    runs = HttpCiReader("https://api.github.com", "o/r", "t", get=get).check_runs("abc")
    assert runs == [{"name": "helpdesk-ci", "status": "completed", "conclusion": "success",
                     "html_url": "u", "source": "actions"}]
    assert any("/actions/runs?head_sha=abc" in c for c in calls)


def test_other_errors_still_raise():
    def get(url, token):
        req = httpx.Request("GET", url)
        raise httpx.HTTPStatusError("500", request=req, response=httpx.Response(500, request=req))

    try:
        HttpCiReader("https://api.github.com", "o/r", "t", get=get).check_runs("abc")
    except httpx.HTTPStatusError:
        return
    raise AssertionError("a 500 must not be swallowed")

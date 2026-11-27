"""THE PREVIEW-SIDE FAULTS ARE TOLD APART (test plan ENT-REC-09..13), with no model.

The fault kit's generators (scripts/install/qa_fault.py — the faults a tester pushes with
`boss qa fault`) break each sample app the way the plan's case says; BOSS's app-start proof
(preview_service.prove_app_serves: the check a build must pass, and the words its repair round and
the preview are given) must then say WHICH failure it is, in words a person and the repair round
act on:

    REC-09 crash on start        "the app exited (code N) before serving HTTP", the app's own error
    REC-10 wrong port            "did not answer HTTP" (the app never served on the $PORT BOSS gave)
    REC-11 404 at /              "GET / returned 404": up, but no page — never "live"
    REC-12 a missing variable    the crash names PAGE_SIZE; the coder's to fix, never a person's
    REC-13 a secret only a       "needs a secret: PAYMENTS_API_KEY", the app not started, a person
           person has            asked — never a repair round

Where the product does not do what the plan says yet, the test is an xfail(strict) naming the case:
it turns into a failure the day the product changes, so the expectation is revisited.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

from boss_engine.services import preview_service as ps
from boss_engine.templates import run_env

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts" / "install"))
import qa_fault  # noqa: E402

FIXTURES = {"node": REPO / "tests/e2e/fixtures/plain-node/plain",
            "python": REPO / "tests/e2e/fixtures/fault-kit/python",
            "go": REPO / "tests/e2e/fixtures/fault-kit/go"}

#: Where the product parts from the plan (each listed in the PR as a product bug).
LOGGED_PORT_IS_FOLLOWED = pytest.mark.xfail(strict=True, reason=(
    "ENT-REC-10: the serve proof finds the app on the port it LOGS (5999) and passes it, so a "
    "server that ignores the $PORT BOSS gave is never reported as 'did not answer HTTP' nor "
    "repaired (preview_service._discover_working_port reads the port from the log)"))
SERVICE_404_IS_UP = pytest.mark.xfail(strict=True, reason=(
    "ENT-REC-11: on an app BOSS reads as an API service (JSON answers, no page), a 404 at / "
    "counts as up (ABSENT_ROUTE_STATUSES: an API gateway routes only its paths), so the plan's "
    "'404 at / is failed, not live' holds only for apps that serve pages"))
APP_CANNOT_DECLARE_A_SECRET = pytest.mark.xfail(strict=True, reason=(
    "ENT-REC-13: an existing app's own files cannot declare a secret only a person has: "
    ".env.example names are optional and boss.deploy.yaml {secret} is the deploy's "
    "(templates/run_env.py), so PAYMENTS_API_KEY is sent to the coder ('must start without "
    "it') instead of a secret card; only an applied template's SecretSpec asks a person"))


def _need(stack: str) -> None:
    tool = {"node": "node", "go": "go"}.get(stack)
    if tool and not shutil.which(tool):
        pytest.skip(f"{tool} is not on this machine")


def _faulted(tmp_path: Path, stack: str, case: str | None) -> tuple[Path, list[str]]:
    app = tmp_path / stack
    shutil.copytree(FIXTURES[stack], app)
    if stack == "node":       # a build declares its run command (RUN_DECLARATION_INSTRUCTION)
        (app / "boss.preview.command").write_text("node src/server.js\n")
    if case:
        qa_fault.apply(case, app)
    return app, [p.relative_to(app).as_posix() for p in app.rglob("*") if p.is_file()]


def _prove(tmp_path: Path, stack: str, case: str | None, **kw) -> dict:
    _need(stack)
    app, files = _faulted(tmp_path, stack, case)
    return ps.prove_app_serves(app, files, known_service=True, timeout=20, **kw)


def kind(record: dict) -> str:
    """The one name BOSS's record gives the failure (what a repair round or a card acts on)."""
    error = str(record.get("error") or "")
    if record.get("ok"):
        return "serves"
    if record.get("needs_secret"):
        return "needs a secret"
    if record.get("optional_unset"):
        return "must start without a variable"
    if record.get("exit_code") is not None and error.startswith("the app exited (code"):
        return "exited before serving"
    if "returned 404" in error:
        return "404 at /"
    if "did not answer HTTP" in error:
        return "did not answer HTTP"
    return "other: " + error[:80]


STACKS = list(FIXTURES)


@pytest.mark.parametrize("stack", STACKS)
def test_the_sample_app_serves_before_any_fault(tmp_path, stack):
    assert kind(_prove(tmp_path, stack, None)) == "serves"


@pytest.mark.parametrize("stack", STACKS)
def test_rec09_a_crash_on_start_is_exited_before_serving_with_the_apps_own_error(tmp_path, stack):
    record = _prove(tmp_path, stack, "REC-09")
    assert kind(record) == "exited before serving"
    assert "boom: BOSS fault REC-09" in record["error"]
    assert record["harness"] is False, "the app's fault, not BOSS's"


@pytest.mark.parametrize("stack", [
    "node", pytest.param("python", marks=LOGGED_PORT_IS_FOLLOWED),
    pytest.param("go", marks=LOGGED_PORT_IS_FOLLOWED)])
def test_rec10_a_wrong_port_did_not_answer_http(tmp_path, stack):
    record = _prove(tmp_path, stack, "REC-10")
    assert kind(record) == "did not answer HTTP", record


@pytest.mark.parametrize("stack", [pytest.param("node", marks=SERVICE_404_IS_UP), "python", "go"])
def test_rec11_a_404_at_home_is_failed_not_live(tmp_path, stack):
    record = _prove(tmp_path, stack, "REC-11")
    assert kind(record) == "404 at /", record
    assert record["harness"] is False


@pytest.mark.parametrize("stack", STACKS)
def test_rec12_a_missing_variable_is_named_and_is_the_coders(tmp_path, stack):
    record = _prove(tmp_path, stack, "REC-12")
    assert kind(record) == "exited before serving"
    assert "PAGE_SIZE" in record["error"]
    assert not record.get("needs_secret"), "BOSS can supply it: never a person's card"


@pytest.mark.parametrize("stack", STACKS)
def test_rec13_a_secret_a_template_declares_asks_a_person_and_never_starts_the_app(tmp_path,
                                                                                 stack):
    needs = run_env.needs_from_specs([{"name": "PAYMENTS_API_KEY"}], source="template payments")
    record = _prove(tmp_path, stack, "REC-13", needs=needs)
    assert kind(record) == "needs a secret"
    assert record["needs_secret"] == ["PAYMENTS_API_KEY"]
    assert "BOSS did not start it" in record["error"]


@APP_CANNOT_DECLARE_A_SECRET
@pytest.mark.parametrize("stack", ["node", "python"])
def test_rec13_a_secret_the_app_declares_itself_asks_a_person(tmp_path, stack):
    record = _prove(tmp_path, stack, "REC-13")
    assert kind(record) == "needs a secret", record


def test_the_preview_faults_have_five_different_names(tmp_path):
    """The plan's point: a person and the repair round can tell these apart by BOSS's words."""
    needs = run_env.needs_from_specs([{"name": "PAYMENTS_API_KEY"}], source="template payments")
    names = {
        "REC-09": kind(_prove(tmp_path / "a", "python", "REC-09")),
        "REC-10": kind(_prove(tmp_path / "b", "node", "REC-10")),
        "REC-11": kind(_prove(tmp_path / "c", "python", "REC-11")),
        "REC-13": kind(_prove(tmp_path / "d", "python", "REC-13", needs=needs)),
    }
    assert len(set(names.values())) == len(names), names
    assert not any(n.startswith("other") for n in names.values()), names

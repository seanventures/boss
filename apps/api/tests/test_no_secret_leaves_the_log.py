"""No credential leaves the log, whichever stream or field it was written into (D2).

Every stream is seeded with free text carrying a real-shaped key — an Anthropic key in an
agent turn ("export ANTHROPIC_API_KEY=sk-ant-…"), a rotated key in event data, a Slack token
in a deploy's failure, a PEM block in a step's error, a JWT in app output… — and every read of
the log is checked for each raw token: the page, one row, the lines around it, the labels, and
the CSV and NDJSON exports.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from apps.api.tests import log_seed as seed
from apps.api.tests.log_seed import PROJECT

# One of every family the redactor knows. Fake values, real shapes.
TOKENS = {
    "anthropic": "sk-ant-api03-Zx9Qw8Er7Ty6Ui5Op4As3Df2Gh1Jk0LmNbVcXz-_Q1w2E3r4",
    "openai_proj": "sk-proj-Ab12Cd34Ef56Gh78Ij90Kl12Mn34",
    "openai": "sk-Qw12Er34Ty56Ui78Op90As12Df34Gh56",
    "slack": "xoxb-2402-1180-Zq8xWv7uTs6r",
    "github": "ghp_Zq8xWv7uTs6rQp5oNm4lKj3iHg2fEd1cBa0",
    "github_pat": "github_pat_11AAQ7ZPY0Zq8xWv7uTs6r_Qp5oNm4lKj3iHg2fEd1cBa0",
    "aws": "AKIAZQ8XWV7UTS6RQP5O",
    "gitlab": "glpat-" + ("fixture0" * 3),
    "google": "AIzaSyZq8xWv7uTs6rQp5oNm4lKj3iHg2fEd1cBa0",
    "assignment": "hunter2-correct-horse",
    "jwt": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJib3NzLXRlc3QifQ.Zq8xWv7uTs6rQp5oNm4lKj3i",
    "pem": "MIIEowIBAAKCAQEAzq8xWv7uTs6rQp5oNm4lKj3iHg2fEd1cBa0",
}
PEM = f"-----BEGIN RSA PRIVATE KEY-----\n{TOKENS['pem']}\n-----END RSA PRIVATE KEY-----"


@pytest.fixture
def seeded(engine):
    now = datetime.now(timezone.utc)
    t = TOKENS
    with engine.begin() as c:
        seed.wipe(c)
        crun = seed.build(c, title=f"Rotate {t['github']}")
        wfr = seed.workflow(c, name="Rotate keys")
        seed.turn(c, id="s_turn", at=now - timedelta(minutes=10), run_id=crun, level="info",
                  content=f"export ANTHROPIC_API_KEY={t['anthropic']}\nnpm test")
        seed.event(c, id="s_event", event_type="secret.rotated", at=now - timedelta(minutes=9),
                   entity_type="code_task_run", entity_id=crun,
                   data={"note": f"rotated to {t['anthropic']}", "nested": [f"old {t['openai']}"]})
        seed.llm_call(c, id="s_call", at=now - timedelta(minutes=8), run_id=crun, ok=False,
                      error=f"401 for key {t['openai_proj']}")
        seed.app_log(c, id="s_app", at=now - timedelta(minutes=7), level="error",
                     message=f"auth header Bearer {t['jwt']} rejected; DB_PASSWORD={t['assignment']}")
        seed.step(c, id="s_step", workflow_run_id=wfr, status="failed", last_error=PEM,
                  created_at=now - timedelta(minutes=6), completed_at=now - timedelta(minutes=6))
        seed.deployment(c, id="s_deploy", at=now - timedelta(minutes=5), status="failed",
                        failure_reason=f"slack hook {t['slack']} refused")
        job = seed.job(c, id="s_job", at=now - timedelta(minutes=4), status="failed")
        dl = seed.deadletter(c, id="s_dl", at=now - timedelta(minutes=3))
        rc = seed.receipt(c, id="s_rcpt", at=now - timedelta(minutes=2))
        ps = seed.process_step(c, id="s_ps", at=now - timedelta(minutes=1))
        # Free text in the columns those streams show.
        c.execute(seed.text("UPDATE jobs SET lease_owner = :v WHERE id = :i"),
                  {"v": f"worker {t['gitlab']}", "i": job})
        c.execute(seed.text("UPDATE deadletters SET source_kind = :v WHERE id = :i"),
                  {"v": f"job {t['aws']}", "i": dl})
        c.execute(seed.text("UPDATE receipts SET effect_kind = :v WHERE id = :i"),
                  {"v": f"github.push token={t['github_pat']}", "i": rc})
        c.execute(seed.text("UPDATE agent_process_steps SET action = :v, handoff_from = :h WHERE id = :i"),
                  {"v": f"handoff {t['google']}", "h": f"api_key: {t['assignment']}", "i": ps})
    yield {"crun": crun, "wfr": wfr}
    with engine.begin() as c:
        seed.wipe(c)


def _assert_clean(text: str, where: str) -> None:
    leaked = [name for name, token in TOKENS.items() if token in text]
    assert not leaked, f"{where} shows {leaked}"


ROWS = ["s_turn", "s_event", "s_call", "s_app", "s_step", "s_deploy", "s_job", "s_dl", "s_rcpt",
        "s_ps"]


def test_every_stream_is_seeded_and_read(client, seeded):
    page = client.get("/v1/activity", params={"project_id": PROJECT, "limit": 50})
    assert page.status_code == 200
    ids = {i["id"] for i in page.json()["items"]}
    assert set(ROWS) <= ids, set(ROWS) - ids
    _assert_clean(page.text, "the page")
    # The masks say what was there.
    turn = next(i for i in page.json()["items"] if i["id"] == "s_turn")
    assert "ANTHROPIC_API_KEY=[SECRET]" in turn["body"]


def test_one_row_and_its_context_are_clean(client, seeded):
    for row_id in ROWS:
        row = client.get(f"/v1/activity/row/{row_id}", params={"project_id": PROJECT})
        assert row.status_code == 200, row_id
        _assert_clean(row.text, f"row {row_id}")
        around = client.get("/v1/activity/context", params={"project_id": PROJECT, "id": row_id,
                                                            "before": 20, "after": 20})
        assert around.status_code == 200, row_id
        _assert_clean(around.text, f"context of {row_id}")


@pytest.mark.parametrize("fmt", ["csv", "ndjson"])
def test_exports_are_clean(client, seeded, fmt):
    got = client.get("/v1/activity/export", params={"project_id": PROJECT, "format": fmt})
    assert got.status_code == 200
    # The secret-bearing lines are in the file, masked.
    for marker in ("ANTHROPIC_API_KEY=[SECRET]", "rotated to [SECRET]", "DB_PASSWORD=[SECRET]",
                   "[PRIVATE_KEY]", "slack hook [SECRET] refused"):
        assert marker in got.text, marker
    _assert_clean(got.text, f"{fmt} export")


def test_scoped_reads_and_labels_are_clean(client, seeded):
    for params in ({"run": seeded["crun"]}, {"wfr": seeded["wfr"]}, {"q": "rotated"}):
        got = client.get("/v1/activity", params={"project_id": PROJECT,
                                                 **params})
        assert got.status_code == 200
        _assert_clean(got.text, f"scope {params}")

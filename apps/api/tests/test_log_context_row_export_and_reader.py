"""One line, the lines around it, the export of a filter, and who may read the log.

  * `row/{id}` returns one line with its labels; another project's line is 404, not a leak.
  * `context` stays inside the line's run.
  * `export` is redacted, honours the filter, and is bounded (`X-Truncated`).
  * every log read requires a reader of the project (it used to require nothing).
"""

from __future__ import annotations

import csv
import io
import json
from datetime import datetime, timedelta, timezone

import pytest

from apps.api.app import main as api_main
from apps.api.tests import log_seed as seed
from apps.api.tests.log_seed import PROJECT

OTHER = "boss-log-other"
SECRET = "AKIAIOSFODNN7EXAMPLE"


@pytest.fixture
def log(engine):
    now = datetime.now(timezone.utc)
    with engine.begin() as c:
        seed.wipe(c)
        c.execute(seed.text("INSERT INTO projects (id, workspace_id, name, slug) "
                            "SELECT :o, workspace_id, 'Other', :o FROM projects WHERE id = :p "
                            "ON CONFLICT (id) DO NOTHING"), {"o": OTHER, "p": PROJECT})
        crun = seed.build(c, title="Ship it #182")
        for i in range(30):
            seed.turn(c, id=f"t{i:02d}", at=now - timedelta(minutes=60 - i), run_id=crun,
                      level="error" if i % 10 == 0 else "info",
                      content=f"line {i} key={SECRET}")
        seed.turn(c, id="t_elsewhere", at=now - timedelta(minutes=45), run_id="crun_other")
        seed.event(c, id="evt_other_project", event_type="job.failed", at=now, project=OTHER)
    yield crun
    with engine.begin() as c:
        seed.wipe(c)
        seed.wipe(c, OTHER)


def test_one_row_with_its_labels_and_never_another_projects(client, log):
    body = client.get("/v1/activity/row/t15", params={"project_id": PROJECT}).json()
    assert body["item"]["id"] == "t15" and body["labels"][log] == "Ship it #182"
    gone = client.get("/v1/activity/row/evt_other_project", params={"project_id": PROJECT})
    assert gone.status_code == 404 and gone.json()["detail"] == "This line is gone."


def test_context_stays_inside_the_run(client, log):
    body = client.get("/v1/activity/context", params={"project_id": PROJECT, "id": "t15",
                                                      "before": 5, "after": 5}).json()
    ids = [i["id"] for i in body["items"]]
    assert ids == [f"t{i:02d}" for i in range(20, 9, -1)]
    assert "t_elsewhere" not in ids and body["anchor"] == "t15"


def test_export_is_redacted_filtered_and_bounded(client, log, monkeypatch):
    ndjson = client.get("/v1/activity/export", params={"project_id": PROJECT, "level": "error",
                                                       "format": "ndjson"})
    assert ndjson.status_code == 200 and ndjson.headers["x-truncated"] == "0"
    rows = [json.loads(line) for line in ndjson.text.splitlines()]
    assert [r["id"] for r in rows] == ["t20", "t10", "t00"]
    assert SECRET not in ndjson.text
    as_csv = client.get("/v1/activity/export", params={"project_id": PROJECT, "format": "csv",
                                                       "run_id": log})
    table = list(csv.reader(io.StringIO(as_csv.text)))
    assert table[0][0] == "created_at" and len(table) == 31
    assert SECRET not in as_csv.text
    from boss_engine.services import activity_log

    monkeypatch.setattr(activity_log, "EXPORT_MAX_ROWS", 7)
    capped = client.get("/v1/activity/export", params={"project_id": PROJECT})
    assert capped.headers["x-truncated"] == "1"
    assert len(capped.text.splitlines()) == 7


def test_every_log_read_requires_a_reader(client, log, monkeypatch):
    monkeypatch.setattr(api_main.settings, "require_api_auth", True)
    monkeypatch.setattr(api_main.settings, "api_keys", "ceo-key,stranger-key")
    monkeypatch.setattr(api_main.settings, "api_key_principals",
                        "ceo-key:ceo,stranger-key:stranger")
    stranger = {"Authorization": "Bearer stranger-key"}
    ceo = {"Authorization": "Bearer ceo-key"}
    for path, extra in (("/v1/activity", {}), ("/v1/activity/facets", {}),
                        ("/v1/activity/histogram", {}), ("/v1/activity/row/t15", {}),
                        ("/v1/activity/context", {"id": "t15"}),
                        ("/v1/activity/export", {})):
        params = {"project_id": PROJECT, **extra}
        assert client.get(path, params=params, headers=stranger).status_code == 403, path
        assert client.get(path, params=params, headers=ceo).status_code == 200, path

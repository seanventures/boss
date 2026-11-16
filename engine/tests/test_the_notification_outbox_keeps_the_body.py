"""The notification outbox keeps what was said, and can be narrowed.

A row keeps its subject and body and never its payload or dedupe key. since/until
is a half-open window on created_at. q is a case-insensitive substring of either,
with LIKE wildcards taken literally. Newest first, at most 50, and capped when
older matches were left off. Another project's rows stay out.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import text

from boss_engine.services import notify

from ._ambient_support import eng, make_project, project, purge  # noqa: F401

T0 = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)


def _point(c, pid: str, name: str = "Ops") -> str:
    cid = f"cp_{uuid4().hex[:12]}"
    c.execute(text(
        "INSERT INTO contact_points (id, project_id, name, kind, created_by) "
        "VALUES (:i, :p, :n, 'console', 'tester')"),
        {"i": cid, "p": pid, "n": name})
    return cid


def _row(c, pid: str, cp: str, *, subject: str, body: str, at: datetime,
         dedupe: str | None = None) -> str:
    nid = f"nt_{uuid4().hex[:12]}"
    c.execute(text(
        "INSERT INTO notifications (id, project_id, group_key, contact_point_id, kind, "
        "dedupe_key, subject, body, payload, status, created_at) "
        "VALUES (:i, :p, 'g', :cp, 'test', :d, :s, :b, CAST(:payload AS JSONB), "
        "'sent', :at)"),
        {"i": nid, "p": pid, "cp": cp, "d": dedupe or f"dedupe-{nid}",
         "s": subject, "b": body, "payload": json.dumps({"token": f"sekrit-{nid}"}),
         "at": at})
    return nid


def test_the_outbox_keeps_the_body_and_drops_payload_and_dedupe_key(eng, project):
    with eng.begin() as c:
        cp = _point(c, project, name="On-call")
        dedupe = f"dedupe-secret-xyz-{uuid4().hex}"
        nid = _row(c, project, cp, subject="Checkout is down",
                   body="The error budget is spent.", at=T0, dedupe=dedupe)
        page = notify.outbox(c, project)
        hidden_payload = notify.outbox(c, project, q="sekrit-")
        hidden_dedupe = notify.outbox(c, project, q=dedupe)

    assert isinstance(page, dict)
    assert set(page) == {"notifications", "capped"}
    assert page["capped"] is False
    (row,) = page["notifications"]
    assert row["id"] == nid
    assert row["subject"] == "Checkout is down"
    assert row["body"] == "The error budget is spent."
    assert row["contact_point"] == "On-call"
    assert row["channel"] == "console"
    assert "payload" not in row and "dedupe_key" not in row
    dumped = json.dumps(row, default=str)
    assert "sekrit-" not in dumped
    assert dedupe not in dumped
    assert hidden_payload["notifications"] == []
    assert hidden_dedupe["notifications"] == []


def test_a_time_window_on_created_at_is_half_open(eng, project):
    early, middle, late = T0, T0 + timedelta(hours=1), T0 + timedelta(hours=2)
    with eng.begin() as c:
        cp = _point(c, project)
        _row(c, project, cp, subject="early", body="one", at=early)
        mid = _row(c, project, cp, subject="middle", body="two", at=middle)
        _row(c, project, cp, subject="late", body="three", at=late)
        page = notify.outbox(c, project, since=middle, until=late)
        as_text = notify.outbox(c, project, since=middle.isoformat(), until=late.isoformat())
        empty = notify.outbox(c, project, since=early, until=early)
        opened = notify.outbox(c, project, since=early, until=middle)

    assert [n["id"] for n in page["notifications"]] == [mid]
    assert page["capped"] is False
    assert [n["id"] for n in as_text["notifications"]] == [mid]
    assert empty["notifications"] == []
    assert [n["subject"] for n in opened["notifications"]] == ["early"]


def test_text_query_matches_subject_or_body_and_like_wildcards_are_literal(eng, project):
    with eng.begin() as c:
        cp = _point(c, project)
        invoice = _row(c, project, cp, subject="Invoice ready", body="please review",
                       at=T0)
        percent = _row(c, project, cp, subject="plain", body="100% ready",
                       at=T0 + timedelta(minutes=1))
        other = _row(c, project, cp, subject="plain", body="100X ready",
                     at=T0 + timedelta(minutes=2))
        underscored = _row(c, project, cp, subject="a_b", body="no",
                           at=T0 + timedelta(minutes=3))
        axb = _row(c, project, cp, subject="axb", body="no",
                   at=T0 + timedelta(minutes=4))
        slashed = _row(c, project, cp, subject="path", body=r"a\b",
                       at=T0 + timedelta(minutes=5))
        plain = _row(c, project, cp, subject="path", body="ab",
                     at=T0 + timedelta(minutes=6))
        by_subject = notify.outbox(c, project, q="invoice")
        by_body = notify.outbox(c, project, q="PLEASE")
        literal_percent = notify.outbox(c, project, q="%")
        hundred = notify.outbox(c, project, q="100%")
        literal_under = notify.outbox(c, project, q="_")
        literal_slash = notify.outbox(c, project, q=r"a\b")
        blank = notify.outbox(c, project, q="")

    assert [n["id"] for n in by_subject["notifications"]] == [invoice]
    assert [n["id"] for n in by_body["notifications"]] == [invoice]
    assert [n["id"] for n in literal_percent["notifications"]] == [percent]
    assert [n["id"] for n in hundred["notifications"]] == [percent]
    assert other not in [n["id"] for n in hundred["notifications"]]
    assert [n["id"] for n in literal_under["notifications"]] == [underscored]
    assert axb not in [n["id"] for n in literal_under["notifications"]]
    assert [n["id"] for n in literal_slash["notifications"]] == [slashed]
    assert plain not in [n["id"] for n in literal_slash["notifications"]]
    assert {n["id"] for n in blank["notifications"]} == {
        invoice, percent, other, underscored, axb, slashed, plain}


def test_the_list_is_newest_first_and_capped_past_fifty(eng, project):
    with eng.begin() as c:
        cp = _point(c, project)
        ids = [_row(c, project, cp, subject=f"n{i}", body="body",
                    at=T0 + timedelta(seconds=i)) for i in range(51)]
        page = notify.outbox(c, project)
        asked = notify.outbox(c, project, limit=1000)
        one = notify.outbox(c, project, limit=1)
        c.execute(text("DELETE FROM notifications WHERE id = :i"), {"i": ids[0]})
        exact = notify.outbox(c, project)

    newest_first = list(reversed(ids))
    assert [n["id"] for n in page["notifications"]] == newest_first[:50]
    assert page["capped"] is True
    assert [n["id"] for n in asked["notifications"]] == newest_first[:50]
    assert asked["capped"] is True
    assert [n["id"] for n in one["notifications"]] == [ids[-1]]
    assert one["capped"] is True
    assert exact["capped"] is False
    assert len(exact["notifications"]) == 50
    assert exact["notifications"][0]["id"] == ids[-1]


def test_another_project_is_absent(eng, project):
    other = make_project(eng)
    try:
        with eng.begin() as c:
            cp = _point(c, project)
            theirs = _point(c, other, name="Theirs")
            mine = _row(c, project, cp, subject="Mine", body="keep", at=T0)
            _row(c, other, theirs, subject="Mine", body="keep", at=T0 + timedelta(seconds=1))
            page = notify.outbox(c, project, q="keep")
        assert [n["id"] for n in page["notifications"]] == [mine]
    finally:
        purge(eng, other)

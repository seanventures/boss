"""A rack is drawn only from stored fields (infra-observability spec §4.3, §5.4, §9.1).

Residents, slots, ports, strips and counts of the Fleet body equal a direct SQL recount of the
seeded rows; nothing is guessed: no CPU or memory key anywhere, the monitoring stack's state is
what BOSS recorded (never a probe), and an empty 45-minute bucket is a gap, never green.
"""
from __future__ import annotations

import json
import os

import pytest
from sqlalchemy import create_engine, text

from boss_engine.config import Settings
from boss_engine.services import infra_view as iv

PG_URL = os.environ.get("BOSS_TEST_DATABASE_URL", "")
HOME = "boss-dogfood"


@pytest.fixture(scope="module")
def eng():
    if not PG_URL.startswith("postgres"):
        pytest.skip("needs BOSS_TEST_DATABASE_URL")
    from apps.api.tests.infra_seed import clear, seed

    e = create_engine(PG_URL, future=True)
    seed(e, HOME)
    yield e
    with e.begin() as c:
        clear(c, HOME)


@pytest.fixture(scope="module")
def body(eng):
    with eng.begin() as c:
        return iv.fleet(c, Settings(), HOME)


def _keys(value, out=None):
    out = set() if out is None else out
    if isinstance(value, dict):
        for k, v in value.items():
            out.add(k)
            _keys(v, out)
    elif isinstance(value, list):
        for v in value:
            _keys(v, out)
    return out


def test_no_cpu_or_memory_anywhere(body):
    keys = _keys(body)
    assert not [k for k in keys if k.startswith(("cpu", "memory", "mem_", "load"))], keys
    blob = json.dumps(body).lower()
    assert "cpu" not in blob and "memory" not in blob
    assert body["partial"] == []


def test_racks_are_ordered_this_computer_then_state_then_name(body):
    names = [(r["id"], r["state"]["word"]) for r in body["racks"]]
    assert names[0][0] == "this" and body["racks"][0]["this_computer"]
    assert [n[0] for n in names[1:]] == ["h_down", "h_setup", "h_ready"], names
    assert body["retired_count"] == 1
    assert "h_retired" not in [r["id"] for r in body["racks"]]


def test_slots_and_ports_equal_a_sql_recount(eng, body):
    with eng.begin() as c:
        for rack in body["racks"]:
            if rack["id"] == "this":
                continue
            host = c.execute(text("SELECT port_min, port_max, max_apps FROM deployment_hosts "
                                  "WHERE id = :i"), {"i": rack["id"]}).mappings().one()
            targets = c.execute(text("SELECT id, domains FROM deployment_targets "
                                     "WHERE host_id = :h AND status <> 'retired'"),
                                {"h": rack["id"]}).mappings().all()
            assert rack["apps"] == len(targets)
            assert {r["id"] for r in rack["residents"] if r["kind"] == "target"} \
                == {t["id"] for t in targets}
            ports = {int(str(d).rpartition(":")[2]) for t in targets for d in t["domains"]
                     if str(d).rpartition(":")[2].isdigit()}
            assert rack["ports_used"] == len(ports)
            assert rack["ports_total"] == host["port_max"] - host["port_min"] + 1
            assert rack["max_apps"] == host["max_apps"]
        live = c.execute(text("SELECT id, port FROM app_previews WHERE project_id = :p "
                              "AND status IN ('running', 'starting')"), {"p": HOME}).all()
    this = body["racks"][0]
    assert {r["id"] for r in this["residents"] if r["kind"] == "preview"} == {p[0] for p in live}
    assert this["ports_used"] == len({p[1] for p in live if p[1]})


def test_unplaced_are_the_targets_with_no_machine(eng, body):
    with eng.begin() as c:
        ids = {r[0] for r in c.execute(text(
            "SELECT id FROM deployment_targets WHERE project_id = :p AND host_id IS NULL "
            "AND status <> 'retired'"), {"p": HOME})}
    assert {u["id"] for u in body["unplaced"]} == ids == {"t_cloud", "t_never"}


def test_every_strip_cell_equals_a_sql_recount(eng, body):
    start, end = iv.strip_window()
    tiles = {r["id"]: r for rack in body["racks"] for r in rack["residents"]
             if r["kind"] == "target"}
    with eng.begin() as c:
        for tid, tile in tiles.items():
            assert len(tile["strip"]) == iv.STRIP_CELLS
            for b, (ok, n) in enumerate(tile["strip"]):
                lo = start.timestamp() + b * iv.STRIP_CELL_S
                row = c.execute(text(
                    "SELECT count(*), count(*) FILTER (WHERE ok) FROM deployment_health_samples "
                    "WHERE deployment_target_id = :t AND checked_at >= to_timestamp(:lo) "
                    "AND checked_at < to_timestamp(:hi)"),
                    {"t": tid, "lo": lo, "hi": lo + iv.STRIP_CELL_S}).one()
                assert [ok, n] == [row[1], row[0]], (tid, b)
    never = tiles["t_stack"]["strip"]
    assert all(cell == [0, 0] for cell in never), "no probes is a gap, never green"


def test_counts_equal_a_recount(eng, body):
    with eng.begin() as c:
        apps = c.execute(text("SELECT count(*) FROM deployment_targets WHERE project_id = :p "
                              "AND status <> 'retired'"), {"p": HOME}).scalar()
        live = c.execute(text("SELECT count(*) FROM app_previews WHERE project_id = :p "
                              "AND status IN ('running', 'starting')"), {"p": HOME}).scalar()
    assert body["counts"] == {"ready": 1, "setting_up": 1, "unreachable": 1, "apps": apps,
                              "previews_live": live, "needs_you": len(body["attention"])}


def test_health_words_come_from_the_probes(body):
    tiles = {r["id"]: r for rack in body["racks"] for r in rack["residents"]}
    tiles.update({u["id"]: u for u in body["unplaced"]})
    assert tiles["t_live"]["health"]["word"] == "Up"
    assert tiles["t_slow"]["health"]["word"] == "Slow"
    assert tiles["t_down"]["health"]["word"] == "Down"
    assert tiles["t_never"]["health"]["word"] == "Not deployed"
    assert tiles["t_live"]["last_deploy"]["sha7"] == "a1b2c3d"
    assert tiles["t_never"]["last_deploy"] is None


def test_the_monitoring_stack_is_what_boss_recorded(eng):
    with eng.begin() as c:
        before = iv.fleet(c, Settings(), HOME)
    mon = [r for rack in before["racks"] for r in rack["residents"] if r["kind"] == "monitoring"]
    assert len(mon) == 1 and mon[0]["health"]["word"] == "Status unknown"
    assert mon[0]["url"] == "http://203.0.113.10:3006"
    assert any(r["kind"] == "monitoring" for r in before["racks"][-1]["residents"])  # h_ready
    with eng.begin() as c:
        c.execute(text("INSERT INTO events (id, project_id, event_type, actor, entity_type, "
                       "entity_id, data, created_at) VALUES ('ev_mon_chk', :p, "
                       "'observability.stack_checked', 'boss', 'deployment_target', 't_live', "
                       "'{\"running\": true}'::jsonb, clock_timestamp() + interval '1 second')"),
                  {"p": HOME})
    try:
        with eng.begin() as c:
            after = iv.fleet(c, Settings(), HOME)
        mon = [r for rack in after["racks"] for r in rack["residents"]
               if r["kind"] == "monitoring"]
        assert mon[0]["health"]["word"] == "Running"
        assert after["etag"] != before["etag"]
    finally:
        with eng.begin() as c:
            c.execute(text("DELETE FROM events WHERE id = 'ev_mon_chk'"))


def test_an_unchanged_world_is_an_unchanged_etag(eng, body):
    with eng.begin() as c:
        again = iv.fleet(c, Settings(), HOME)
    assert again["etag"] == body["etag"]

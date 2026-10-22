"""The database image upgrade is an image swap (BOSSAPP-622; operator decision 2026-10-22).

A data directory written by postgres:16-alpine (every install before this image) is opened by
boss-community-db (the same PostgreSQL 16 on the same Alpine libc, plus pgvector and AGE). The
proof, on real containers: row counts are unchanged, amcheck finds every B-tree index intact (a
libc change would break the text indexes), the schema step creates both extensions, and both
load. A fresh volume on the new image gets the same. Needs a local Docker daemon and both images
already present; nothing is pulled, and every container and volume made here is removed.
"""
from __future__ import annotations

import socket
import subprocess
import time
import uuid

import pytest
from sqlalchemy import create_engine, text

from boss_engine.db import ensure_schema

OLD = "postgres:16-alpine"
NEW = "boss-community-db:pg16.12-vector0.8.1-age1.6.0"
PASSWORD = "upgrade-proof"   # a throwaway container's password, nothing else uses it


def _docker(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=check)


def _available() -> bool:
    try:
        return (_docker("info", check=False).returncode == 0
                and _docker("image", "inspect", OLD, check=False).returncode == 0
                and _docker("image", "inspect", NEW, check=False).returncode == 0)
    except FileNotFoundError:
        return False


pytestmark = pytest.mark.skipif(not _available(), reason=f"needs Docker with {OLD} and {NEW} present")


def _port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _start(name: str, image: str, volume: str, port: int, *command: str) -> None:
    _docker("run", "-d", "--name", name, "--pull", "never", "-e", "POSTGRES_USER=boss",
            "-e", f"POSTGRES_PASSWORD={PASSWORD}", "-e", "POSTGRES_DB=boss_upgrade_test",
            "-p", f"127.0.0.1:{port}:5432", "-v", f"{volume}:/var/lib/postgresql/data", image, *command)
    for _ in range(90):
        ready = _docker("exec", name, "pg_isready", "-U", "boss", "-d", "boss_upgrade_test", check=False)
        if ready.returncode == 0:
            time.sleep(1)
            return
        time.sleep(1)
    raise RuntimeError(f"{name} did not become ready: {_docker('logs', name, check=False).stderr[-400:]}")


def _engine(port: int):
    return create_engine(f"postgresql+psycopg://boss:{PASSWORD}@127.0.0.1:{port}/boss_upgrade_test", future=True)


def _counts(engine) -> dict[str, int]:
    with engine.connect() as c:
        return {t: c.execute(text(f"SELECT COUNT(*) FROM {t}")).scalar_one()
                for t in ("projects", "kg_nodes", "kg_edges", "decisions")}


def _btree_intact(engine) -> int:
    with engine.begin() as c:
        c.execute(text("CREATE EXTENSION IF NOT EXISTS amcheck"))
        indexes = c.execute(text(
            "SELECT c.oid FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
            "JOIN pg_am a ON a.oid = c.relam JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE a.amname = 'btree' AND n.nspname = 'public' AND c.relpersistence = 'p' AND c.relkind = 'i'")).scalars().all()
        for oid in indexes:
            c.execute(text("SELECT bt_index_check(:o, true)"), {"o": oid})     # raises on a broken index
    return len(indexes)


def _extensions_load(engine) -> dict[str, str]:
    with engine.begin() as c:
        versions = dict(c.execute(text(
            "SELECT extname, extversion FROM pg_extension WHERE extname IN ('vector', 'age')")).all())
        c.execute(text("SELECT CAST('[1,2,3]' AS vector) <=> CAST('[1,2,4]' AS vector)"))
        c.execute(text("LOAD 'age'"))
        c.execute(text("SELECT ag_catalog.create_graph('kg_upgrade_probe')"))
        c.execute(text("SELECT ag_catalog.drop_graph('kg_upgrade_probe', true)"))
    return versions


def _populate(engine) -> None:
    with engine.begin() as c:
        c.execute(text("INSERT INTO orgs (id, name) VALUES ('org_up', 'Upgrade')"))
        c.execute(text("INSERT INTO workspaces (id, org_id, name) VALUES ('ws_up', 'org_up', 'Upgrade')"))
        c.execute(text("INSERT INTO projects (id, workspace_id, name, slug) VALUES ('proj_up', 'ws_up', 'Up', 'up')"))
        # Text keys in mixed case, accents and symbols: what a libc collation change reorders.
        c.execute(text(
            "INSERT INTO kg_nodes (id, project_id, kind, node_key, name, body) "
            "SELECT 'n' || g, 'proj_up', 'symbol', 'symbol:' || (ARRAY['Ärger','apple','Zebra','_x','éclair','b-2'])[1 + g % 6]"
            " || g, 'name ' || g, 'body ' || g FROM generate_series(1, 3000) g"))
        c.execute(text(
            "INSERT INTO kg_edges (id, project_id, src_id, dst_id, kind) "
            "SELECT 'e' || g, 'proj_up', 'n' || g, 'n' || (g + 1), 'calls' FROM generate_series(1, 2999) g"))


@pytest.fixture()
def names():
    tag = uuid.uuid4().hex[:8]
    made = {"old": f"boss-dbswap-old-{tag}", "new": f"boss-dbswap-new-{tag}",
            "fresh": f"boss-dbswap-fresh-{tag}", "volume": f"boss-dbswap-{tag}",
            "fresh_volume": f"boss-dbswap-fresh-{tag}"}
    yield made
    for key in ("old", "new", "fresh"):
        _docker("rm", "-f", made[key], check=False)
    for key in ("volume", "fresh_volume"):
        _docker("volume", "rm", "-f", made[key], check=False)


def test_a_populated_alpine_volume_opens_on_the_new_image_intact(names):
    port = _port()
    _start(names["old"], OLD, names["volume"], port)
    old_engine = _engine(port)
    ensure_schema(old_engine)            # the extensions are skipped: the stock image has none
    _populate(old_engine)
    before = _counts(old_engine)
    old_engine.dispose()
    _docker("stop", names["old"])
    _docker("rm", names["old"])

    port = _port()
    _start(names["new"], NEW, names["volume"], port, "postgres", "-c", "shared_preload_libraries=age")
    new_engine = _engine(port)
    try:
        assert _counts(new_engine) == before
        assert _btree_intact(new_engine) > 100
        ensure_schema(new_engine)        # the upgrade's schema step
        assert _extensions_load(new_engine) == {"vector": "0.8.1", "age": "1.6.0"}
        assert _counts(new_engine) == before
    finally:
        new_engine.dispose()


def test_a_fresh_install_on_the_new_image_has_both_extensions(names):
    port = _port()
    _start(names["fresh"], NEW, names["fresh_volume"], port)       # preload from the image's own conf
    engine = _engine(port)
    try:
        ensure_schema(engine)
        assert _extensions_load(engine) == {"vector": "0.8.1", "age": "1.6.0"}
        with engine.connect() as c:
            assert c.execute(text("SHOW shared_preload_libraries")).scalar_one() == "age"
    finally:
        engine.dispose()

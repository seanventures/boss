"""The Alpine → Debian database image upgrade on a real, populated Postgres (KG spec §3.1).

Opening a musl-written data directory under glibc leaves text indexes in the wrong order.
These tests prove the hazard is real, that the BOSS DB image refuses such a directory, and
that scripts/install/db_image_migrate.py moves a populated database by dump and restore with
equal row counts, clean B-tree indexes, roles, passwords and a rollback-safe old volume.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import uuid
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
OLD_IMAGE = "postgres:16-alpine"
PLAIN_DEBIAN = "postgres:16-trixie"
NEW_IMAGE = "boss-community-db:migration-test"
pytestmark = pytest.mark.skipif(os.environ.get("BOSS_RUN_DOCKER_LIFECYCLE_TEST") != "1",
                                reason="requires a Docker daemon and the Postgres base images")

spec = importlib.util.spec_from_file_location("db_image_migrate", REPO / "scripts/install/db_image_migrate.py")
mig = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mig)

# Strings whose byte order (musl) and en_US order (glibc) disagree: case, accents, punctuation.
POPULATE = """
CREATE TABLE products (id int PRIMARY KEY, sku text UNIQUE NOT NULL, title text NOT NULL);
CREATE INDEX products_title ON products (title);
INSERT INTO products
SELECT i, (ARRAY['apple','Banana','éclair','_under','Zebra','äpfel','-dash','a b','A-b'])[1 + i % 9]
          || '-' || md5(i::text),
       (ARRAY['Ölfilter','oil','Oil','zeta','Äpfel','apfel','_x','x_','Ω'])[1 + i % 9] || md5(i::text)
FROM generate_series(1, 20000) i;
CREATE TABLE notes (k text PRIMARY KEY, body text);
INSERT INTO notes SELECT 'n' || i || (CASE WHEN i % 2 = 0 THEN 'A' ELSE 'a' END), repeat('x', i % 50)
FROM generate_series(1, 3000) i;
CREATE ROLE reader LOGIN PASSWORD 'reader-fixture';
GRANT SELECT ON products TO reader;
CREATE DATABASE infisical;
"""


def run(*args, input=None, timeout=300):
    return subprocess.run([str(a) for a in args], input=input, capture_output=True, text=True,
                          timeout=timeout, check=False)


def ok(*args, input=None, timeout=300):
    result = run(*args, input=input, timeout=timeout)
    assert result.returncode == 0, (result.stdout + result.stderr)[-3000:]
    return result.stdout.strip()


def psql(container, sql, db="boss", user="boss"):
    return ok("docker", "exec", container, "psql", "-X", "-v", "ON_ERROR_STOP=1", "-U", user, "-d", db, "-Atc", sql)


def wait(container, user="boss", fresh=True):
    mig.wait_ready(mig.Docker(["docker"]), container, user, fresh=fresh)


@pytest.fixture(scope="module")
def new_image():
    for image in (OLD_IMAGE, PLAIN_DEBIAN):
        if run("docker", "image", "inspect", image).returncode:
            pytest.skip(f"{image} is not cached locally")
    ok("docker", "build", "-q", "-t", NEW_IMAGE, "-f", REPO / "scripts/install/db-image/Dockerfile",
       REPO / "scripts/install", timeout=900)
    return NEW_IMAGE


@pytest.fixture
def names(request):
    tag = "bossdbmig" + uuid.uuid4().hex[:8]
    made = {"containers": [], "volumes": []}

    def container(suffix):
        made["containers"].append(f"{tag}-{suffix}")
        return f"{tag}-{suffix}"

    def volume(suffix):
        made["volumes"].append(f"{tag}_{suffix}")
        return f"{tag}_{suffix}"

    yield tag, container, volume
    for c in made["containers"]:
        run("docker", "rm", "-f", c)
    for v in made["volumes"]:
        run("docker", "volume", "rm", "-f", v)


def populated_alpine(container, volume):
    """A BOSS-shaped database written by the current Alpine image; returns the container."""
    ok("docker", "run", "-d", "--name", container, "--network", "none", "-v", f"{volume}:/var/lib/postgresql/data",
       "-e", "POSTGRES_USER=boss", "-e", "POSTGRES_PASSWORD=boss-fixture", "-e", "POSTGRES_DB=boss", OLD_IMAGE)
    wait(container)
    psql(container, POPULATE.replace("CREATE DATABASE infisical;", ""))
    psql(container, "CREATE DATABASE infisical")
    psql(container, "CREATE TABLE secrets (name text PRIMARY KEY); INSERT INTO secrets SELECT 'S' || i FROM generate_series(1,500) i",
         db="infisical")
    return container


def btree_check(container, db="boss"):
    psql(container, "CREATE EXTENSION IF NOT EXISTS amcheck", db=db)
    return run("docker", "exec", container, "psql", "-X", "-v", "ON_ERROR_STOP=1", "-U", "boss", "-d", db, "-Atc",
               mig.BTREE_CHECK_SQL)


def test_opening_an_alpine_directory_under_glibc_corrupts_text_indexes(new_image, names):
    """Why the upgrade must not reuse the volume: the hazard, reproduced."""
    _, container, volume = names
    vol = volume("pg")
    old = populated_alpine(container("old"), vol)
    assert btree_check(old).returncode == 0
    ok("docker", "stop", old)
    swapped = container("swapped")
    ok("docker", "run", "-d", "--name", swapped, "--network", "none", "-v", f"{vol}:/var/lib/postgresql/data", PLAIN_DEBIAN)
    wait(swapped, fresh=False)
    result = btree_check(swapped)
    assert result.returncode != 0 and "invariant violated" in result.stderr, result.stderr


def test_the_boss_db_image_refuses_an_alpine_data_directory(new_image, names):
    _, container, volume = names
    vol = volume("pg")
    ok("docker", "stop", populated_alpine(container("old"), vol))
    result = run("docker", "run", "--rm", "--network", "none", "-v", f"{vol}:/var/lib/postgresql/data", new_image, timeout=60)
    assert result.returncode == 64
    assert "would corrupt text indexes" in result.stderr and "db_image_migrate.py" in result.stderr


def test_upgrade_moves_a_populated_database_with_equal_rows_and_clean_indexes(new_image, names):
    tag, container, volume = names
    legacy, new = volume(mig.LEGACY_VOLUME_KEY), volume("boss_community_pg_debian")
    old = populated_alpine(container("old"), legacy)
    docker = mig.Docker(["docker"])
    before = mig.snapshot(docker, old, "boss", ["boss", "infisical"])
    assert before["boss"]["tables"]["public.products"] == 20000
    old_order = psql(old, "SELECT string_agg(sku, ',' ORDER BY sku) FROM products")

    config = {"name": tag,
              "services": {"db": {"image": new_image, "environment": {"POSTGRES_USER": "boss"},
                                  "volumes": [{"type": "volume", "source": "boss_community_pg_debian",
                                               "target": "/var/lib/postgresql/data"}]}},
              "volumes": {"boss_community_pg_debian": {"name": new}}}
    assert mig.from_compose(docker, config, old) == "migrated"
    assert run("docker", "inspect", "--format", "{{.State.Running}}", old).stdout.strip() == "false"

    upgraded = container("new")
    ok("docker", "run", "-d", "--name", upgraded, "--network", "none", "-v", f"{new}:/var/lib/postgresql/data", new_image)
    wait(upgraded, fresh=False)
    assert mig.snapshot(docker, upgraded, "boss", ["boss", "infisical"]) == before
    assert btree_check(upgraded).returncode == 0
    assert btree_check(upgraded, db="infisical").returncode == 0
    # Text order is now glibc's, rebuilt by the restore rather than inherited from musl.
    new_order = psql(upgraded, "SET enable_seqscan = off; SELECT string_agg(sku, ',' ORDER BY sku) FROM products").splitlines()[-1]
    assert new_order == psql(upgraded, "SET enable_indexscan = off; SET enable_bitmapscan = off; "
                                       "SELECT string_agg(sku, ',' ORDER BY sku) FROM products").splitlines()[-1]
    assert new_order != old_order
    assert psql(upgraded, "SELECT datcollate FROM pg_database WHERE datname = 'boss'") == "en_US.utf8"
    # Roles and their passwords came across (scram hashes), not just the data.
    assert ok("docker", "exec", "-e", "PGPASSWORD=reader-fixture", upgraded, "psql", "-X", "-h", "127.0.0.1",
              "-U", "reader", "-d", "boss", "-Atc", "SELECT count(*) FROM products") == "20000"
    assert ok("docker", "exec", "-e", "PGPASSWORD=boss-fixture", upgraded, "psql", "-X", "-h", "127.0.0.1",
              "-U", "boss", "-d", "boss", "-Atc", "SELECT 1") == "1"
    # The point of the new image.
    assert psql(upgraded, "CREATE EXTENSION vector; CREATE EXTENSION age; "
                          "SELECT extname FROM pg_extension WHERE extname IN ('vector','age') ORDER BY 1") .splitlines()[-2:] == ["age", "vector"]
    record = json.loads(ok("docker", "exec", upgraded, "cat", f"/var/lib/postgresql/data/{mig.DONE_MARKER}"))
    assert record["databases"]["boss"]["rows"] == 23000 and record["databases"]["boss"]["btree_indexes_checked"] >= 4
    ok("docker", "stop", upgraded)

    # A rerun converges; the old volume is untouched, so rollback still finds its data.
    assert mig.from_compose(docker, config, old) == "already-migrated"
    ok("docker", "start", old)
    wait(old, fresh=False)
    assert psql(old, "SELECT count(*) FROM products") == "20000"
    # ...but if the old release then writes (a rollback), the copy is stale: refuse, never guess.
    psql(old, "INSERT INTO notes VALUES ('after-rollback', '')")
    ok("docker", "stop", old)
    with pytest.raises(mig.MigrationError, match="used after it was migrated"):
        mig.from_compose(docker, config, old)


def test_a_half_finished_target_volume_is_refused_and_a_failed_copy_leaves_no_volume(new_image, names):
    tag, container, volume = names
    legacy, new = volume(mig.LEGACY_VOLUME_KEY), volume("boss_community_pg_debian")
    old = populated_alpine(container("old"), legacy)
    docker = mig.Docker(["docker"])
    config = {"name": tag,
              "services": {"db": {"image": new_image, "environment": {"POSTGRES_USER": "boss"},
                                  "volumes": [{"type": "volume", "source": "pgd", "target": "/var/lib/postgresql/data"}]}},
              "volumes": {"pgd": {"name": new}}}
    ok("docker", "volume", "create", new)
    with pytest.raises(mig.MigrationError, match="holds no completed migration"):
        mig.from_compose(docker, config, old)
    ok("docker", "volume", "rm", new)
    # A copy that fails part-way (a superuser that does not exist) removes its target volume.
    ok("docker", "stop", old)
    with pytest.raises(mig.MigrationError):
        mig.migrate(docker, legacy_volume=legacy, new_volume=new, old_image=OLD_IMAGE, new_image=new_image,
                    superuser="nobody")
    assert run("docker", "volume", "inspect", new).returncode != 0


def test_a_fresh_install_has_nothing_to_migrate(names):
    tag, _, volume = names
    config = {"name": tag, "services": {"db": {"image": NEW_IMAGE, "volumes": [
        {"type": "volume", "source": "pgd", "target": "/var/lib/postgresql/data"}]}},
        "volumes": {"pgd": {"name": volume("pgd")}}}
    assert mig.from_compose(mig.Docker(["docker"]), config, None) == "fresh"

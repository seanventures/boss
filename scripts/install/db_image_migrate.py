#!/usr/bin/env python3
"""Move the BOSS database from the Alpine (musl) image to the Debian (glibc) image.

musl and glibc sort text differently, so a data directory written under one has text
indexes in the wrong order under the other (KNOWLEDGE_GRAPH_SPEC.md §3.1). This helper never
opens the old directory with the new image. It dumps every role and database from the old
image and restores them into a NEW volume initialised by the new image, then proves the copy:
equal row counts per table, equal index counts, and amcheck over every B-tree index.

The old volume is left exactly as it was, so `boss upgrade`'s rollback (the previous release
on the previous image and volume) still finds its data, and the protected backup taken before
this runs is a second copy. Stdlib only; prints nothing secret.

    db_image_migrate.py from-compose [--old-db-container ID] [--docker-prefix "sudo -n docker"]
        reads `docker compose config --format json` on stdin (install.sh)
    db_image_migrate.py migrate --legacy-volume V --new-volume W --old-image I --new-image J
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import shlex
import subprocess
import sys
import time

LEGACY_VOLUME_KEY = "boss_community_pg"
DATA = "/var/lib/postgresql/data"
DONE_MARKER = "boss-migrated"
COUNTS_SQL = (
    "SELECT n.nspname || '.' || c.relname, (xpath('/row/c/text()', query_to_xml("
    "format('SELECT count(*) AS c FROM %I.%I', n.nspname, c.relname), false, true, '')))[1]::text "
    "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
    "WHERE c.relkind IN ('r', 'p', 'm') AND n.nspname NOT IN ('pg_catalog', 'information_schema') "
    "AND n.nspname NOT LIKE 'pg\\_toast%' ORDER BY 1"
)
INDEX_COUNT_SQL = (
    "SELECT count(*) FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
    "JOIN pg_namespace n ON n.oid = c.relnamespace "
    "WHERE n.nspname NOT IN ('pg_catalog', 'information_schema') AND n.nspname NOT LIKE 'pg\\_toast%'"
)
BTREE_CHECK_SQL = (
    "SELECT count(bt_index_check(c.oid, true)) FROM pg_index i "
    "JOIN pg_class c ON c.oid = i.indexrelid JOIN pg_am am ON am.oid = c.relam "
    "JOIN pg_namespace n ON n.oid = c.relnamespace "
    "WHERE am.amname = 'btree' AND c.relpersistence <> 't' AND i.indisvalid AND i.indisready"
)


class MigrationError(RuntimeError):
    pass


def say(message: str) -> None:
    print(f"db-migrate: {message}", file=sys.stderr, flush=True)


class Docker:
    def __init__(self, prefix: list[str]):
        self.prefix = prefix

    def run(self, *args: str, check: bool = True, stdin: str | None = None) -> str:
        proc = subprocess.run([*self.prefix, *args], input=stdin, capture_output=True, text=True)
        if check and proc.returncode:
            raise MigrationError(f"docker {args[0]} failed: {proc.stderr.strip()[-400:]}")
        return proc.stdout

    def ok(self, *args: str) -> bool:
        return subprocess.run([*self.prefix, *args], capture_output=True).returncode == 0

    def volume_exists(self, name: str) -> bool:
        return self.ok("volume", "inspect", name)

    def read_volume_file(self, volume: str, image: str, path: str) -> str | None:
        proc = subprocess.run(
            [*self.prefix, "run", "--rm", "--network", "none", "-v", f"{volume}:/d:ro",
             "--entrypoint", "cat", image, f"/d/{path}"], capture_output=True, text=True)
        return proc.stdout if proc.returncode == 0 else None

    def file_digest(self, volume: str, image: str, path: str) -> str:
        out = self.run("run", "--rm", "--network", "none", "-v", f"{volume}:/d:ro",
                       "--entrypoint", "sha256sum", image, f"/d/{path}")
        return out.split()[0]

    def psql(self, container: str, user: str, db: str, sql: str) -> list[list[str]]:
        out = self.run("exec", container, "psql", "-X", "-v", "ON_ERROR_STOP=1", "-U", user,
                       "-d", db, "-AtF", "\t", "-c", sql)
        return [line.split("\t") for line in out.splitlines() if line]

    def pipe(self, src: list[str], dst: list[str], transform=None) -> None:
        """Stream `docker exec` src into `docker exec -i` dst; both must succeed."""
        producer = subprocess.Popen([*self.prefix, *src], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if transform is None:
            consumer = subprocess.Popen([*self.prefix, *dst], stdin=producer.stdout,
                                        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            producer.stdout.close()
            _, cerr = consumer.communicate()
            _, perr = producer.communicate()
        else:
            data, perr = producer.communicate()
            consumer = subprocess.run([*self.prefix, *dst], input=transform(data),
                                      stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            cerr = consumer.stderr
        if producer.returncode or consumer.returncode:
            detail = (perr or b"").decode(errors="replace")[-300:] + (cerr or b"").decode(errors="replace")[-300:]
            raise MigrationError(f"stream {src[2:4]} → {dst[2:4]} failed: {detail.strip()}")


def wait_ready(docker: Docker, container: str, user: str, fresh: bool, timeout: float = 180) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = docker.run("inspect", "--format", "{{.State.Running}}", container, check=False).strip()
        if state != "true":
            logs = docker.run("logs", "--tail", "15", container, check=False)
            raise MigrationError(f"{container} stopped while starting: {logs.strip()[-500:]}")
        # A fresh directory is first served by the entrypoint's temporary init server; only the
        # final server counts, so wait for the entrypoint to say initialisation is over.
        if fresh and "init process complete" not in _all_logs(docker, container):
            time.sleep(1)
            continue
        if docker.ok("exec", container, "psql", "-X", "-U", user, "-d", "postgres", "-Atc", "SELECT 1"):
            return
        time.sleep(1)
    raise MigrationError(f"{container} did not become ready in {int(timeout)} s")


def _all_logs(docker: Docker, container: str) -> str:
    proc = subprocess.run([*docker.prefix, "logs", container], capture_output=True, text=True)
    return proc.stdout + proc.stderr


def snapshot(docker: Docker, container: str, user: str, dbs: list[str]) -> dict[str, dict]:
    out = {}
    for db in dbs:
        counts = {row[0]: int(row[1]) for row in docker.psql(container, user, db, COUNTS_SQL)}
        indexes = int(docker.psql(container, user, db, INDEX_COUNT_SQL)[0][0])
        out[db] = {"tables": counts, "indexes": indexes}
    return out


def check_btrees(docker: Docker, container: str, user: str, db: str) -> int:
    had = docker.psql(container, user, db, "SELECT count(*) FROM pg_extension WHERE extname = 'amcheck'")[0][0] == "1"
    if not had:
        docker.psql(container, user, db, "CREATE EXTENSION amcheck")
    try:
        return int(docker.psql(container, user, db, BTREE_CHECK_SQL)[0][0])
    finally:
        if not had:
            docker.psql(container, user, db, "DROP EXTENSION amcheck")


def migrate(docker: Docker, *, legacy_volume: str, new_volume: str, old_image: str, new_image: str,
            superuser: str = "boss") -> dict:
    """Dump/restore legacy_volume (old_image) into a fresh new_volume (new_image) and prove it."""
    if docker.volume_exists(new_volume):
        raise MigrationError(f"target volume {new_volume} already exists; refusing to write into it")
    tag = f"{os.getpid()}-{secrets.token_hex(3)}"
    src, dst = f"boss-db-migrate-src-{tag}", f"boss-db-migrate-dst-{tag}"
    completed = False
    try:
        say(f"reading {legacy_volume} with the image that wrote it")
        docker.run("run", "-d", "--name", src, "--network", "none",
                   "-v", f"{legacy_volume}:{DATA}", old_image, "postgres")
        wait_ready(docker, src, superuser, fresh=False)
        docker.run("volume", "create", "--label", "io.boss.db-libc=glibc",
                   "--label", f"io.boss.migrated-from={legacy_volume}", new_volume)
        say(f"initialising {new_volume} with the Debian image")
        docker.run("run", "-d", "--name", dst, "--network", "none", "-v", f"{new_volume}:{DATA}",
                   "-e", f"POSTGRES_USER={superuser}", "-e", "POSTGRES_DB=postgres",
                   "-e", f"POSTGRES_PASSWORD={secrets.token_urlsafe(24)}", new_image, "postgres")
        wait_ready(docker, dst, superuser, fresh=True)

        dbs = [r[0] for r in docker.psql(src, superuser, "postgres",
               "SELECT datname FROM pg_database WHERE NOT datistemplate AND datname <> 'postgres' ORDER BY 1")]
        before = snapshot(docker, src, superuser, dbs)

        # Roles first. The bootstrap superuser already exists in the new directory; its ALTER
        # ROLE (attributes and password hash) still applies, only the CREATE is dropped.
        skip = f"CREATE ROLE {superuser};".encode()
        docker.pipe(["exec", src, "pg_dumpall", "-U", superuser, "--globals-only"],
                    ["exec", "-i", dst, "psql", "-X", "-q", "-v", "ON_ERROR_STOP=1", "-U", superuser, "-d", "postgres"],
                    transform=lambda sql: b"\n".join(l for l in sql.splitlines() if l.strip() != skip))
        for db in dbs:
            say(f"copying database {db}")
            docker.pipe(["exec", src, "pg_dump", "-U", superuser, "-Fc", "-d", db],
                        ["exec", "-i", dst, "pg_restore", "-U", superuser, "--create", "--exit-on-error", "-d", "postgres"])

        after = snapshot(docker, dst, superuser, dbs)
        if after != before:
            diff = sorted(f"{db}.{t}" for db in dbs for t in set(before[db]["tables"]) | set(after.get(db, {}).get("tables", {}))
                          if before[db]["tables"].get(t) != after.get(db, {}).get("tables", {}).get(t))
            raise MigrationError(f"the copy differs from the original: {diff[:10] or 'index counts'}")
        checked = {db: check_btrees(docker, dst, superuser, db) for db in dbs}

        docker.run("stop", "-t", "60", src)
        legacy_control = docker.file_digest(legacy_volume, new_image, "global/pg_control")
        record = {"from_volume": legacy_volume, "from_image": old_image, "legacy_pg_control": legacy_control,
                  "databases": {db: {"tables": len(before[db]["tables"]),
                                     "rows": sum(before[db]["tables"].values()),
                                     "btree_indexes_checked": checked[db]} for db in dbs},
                  "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        docker.run("exec", "-i", "-u", "postgres", dst, "sh", "-c", f"cat > {DATA}/{DONE_MARKER}",
                   stdin=json.dumps(record) + "\n")
        docker.run("stop", "-t", "60", dst)
        completed = True
        say(f"done: {sum(r['rows'] for r in record['databases'].values())} rows in {len(dbs)} database(s); "
            f"{legacy_volume} is kept unchanged")
        return record
    finally:
        for name in (src, dst):
            docker.run("rm", "-f", name, check=False)
        if not completed and docker.volume_exists(new_volume):
            docker.run("volume", "rm", new_volume, check=False)


def plan_from_compose(config: dict) -> dict:
    project = config["name"]
    db = config["services"]["db"]
    mounts = [v for v in db.get("volumes", []) if v.get("target") == DATA and v.get("type") == "volume"]
    if len(mounts) != 1:
        raise MigrationError("the db service needs exactly one named volume for its data")
    new_volume = config["volumes"][mounts[0]["source"]]["name"]
    legacy = config["volumes"].get(LEGACY_VOLUME_KEY, {}).get("name") or f"{project}_{LEGACY_VOLUME_KEY}"
    env = db.get("environment") or {}
    return {"legacy_volume": legacy, "new_volume": new_volume, "new_image": db["image"],
            "superuser": env.get("POSTGRES_USER") or "postgres"}


def from_compose(docker: Docker, config: dict, old_db_container: str | None) -> str:
    plan = plan_from_compose(config)
    legacy, new = plan["legacy_volume"], plan["new_volume"]
    if legacy == new or not docker.volume_exists(legacy):
        return "fresh"
    if docker.volume_exists(new):
        raw = docker.read_volume_file(new, plan["new_image"], DONE_MARKER)
        if raw is None:
            raise MigrationError(f"{new} exists but holds no completed migration, and {legacy} still exists. "
                                 f"Nothing was changed; see scripts/install/README.md (database image).")
        record = json.loads(raw)
        if docker.file_digest(legacy, plan["new_image"], "global/pg_control") != record["legacy_pg_control"]:
            raise MigrationError(f"{legacy} was used after it was migrated (a rollback?), so {new} may miss "
                                 f"newer data. Nothing was changed; see scripts/install/README.md (database image).")
        return "already-migrated"
    if not old_db_container:
        raise MigrationError(f"{legacy} exists but its database container is missing, so the image that wrote it "
                             "is unknown. No image was substituted; see scripts/install/README.md.")
    old_image = docker.run("inspect", "--format", "{{.Image}}", old_db_container).strip()
    running = [c for c in docker.run("ps", "-q", "--filter", f"volume={legacy}").split() if c]
    if running:
        say("stopping the old database for the copy (its container and volume are kept for rollback)")
        docker.run("stop", "-t", "60", *running)
    migrate(docker, legacy_volume=legacy, new_volume=new, old_image=old_image,
            new_image=plan["new_image"], superuser=plan["superuser"])
    return "migrated"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--docker-prefix", default="docker")
    sub = parser.add_subparsers(dest="command", required=True)
    fc = sub.add_parser("from-compose")
    fc.add_argument("--old-db-container", default="")
    mg = sub.add_parser("migrate")
    for flag in ("--legacy-volume", "--new-volume", "--old-image", "--new-image"):
        mg.add_argument(flag, required=True)
    mg.add_argument("--superuser", default="boss")
    args = parser.parse_args(argv)
    docker = Docker(shlex.split(args.docker_prefix))
    try:
        if args.command == "from-compose":
            print(from_compose(docker, json.load(sys.stdin), args.old_db_container.strip() or None))
        else:
            print(json.dumps(migrate(docker, legacy_volume=args.legacy_volume, new_volume=args.new_volume,
                                     old_image=args.old_image, new_image=args.new_image, superuser=args.superuser)))
    except MigrationError as exc:
        say(str(exc))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""BOSSAPP-826: workspace snapshots across ephemeral sandboxes.

Pinned here:
  * snapshot -> restore round trip against a FAKE object store (the
    tmpdir-backed DirectoryObjectStore, same interface as the S3 client) and a
    fake REMOTE sandbox (a real git checkout in a tmpdir, spoken to only
    through the Sandbox protocol + the 824 sync seam);
  * the seed cache: hit on the same lockfiles (a fresh run warm-starts its
    dependencies), miss on changed lockfiles or another project;
  * off is byte-identical: the guard reads settings only and the governed
    hooks are guarded by it (enforced at the source level);
  * the TTL reaper deletes object then row, and touches nothing with no store;
  * SSHSandbox.sync_out(preserve_links=True) keeps symlinks/+x, and dotfiles
    survive (the lstrip('./') bug).

Schema idempotency on real Postgres lives in test_workspace_snapshots_schema_826.py.
"""
from __future__ import annotations

import base64
import gzip
import io
import json
import os
import shlex
import stat
import subprocess
import tarfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text

from boss_engine.schemas import FileChange
from boss_engine.services import workspace_snapshots as ws
from boss_engine.services.object_store import (
    DirectoryObjectStore,
    ObjectNotFound,
    ObjectStoreError,
    S3ObjectStore,
    object_store_from_settings,
)
from boss_engine.services.sandbox import CommandResult
from boss_engine.services.ssh_environment import _extract_preserving_links

ROOT = Path(__file__).resolve().parents[2]
START = "-- WORKSPACE SNAPSHOTS (BOSSAPP-826)"
END = "-- ================================ end WORKSPACE SNAPSHOTS (BOSSAPP-826)"


def schema_block() -> str:
    sql = (ROOT / "db" / "schema.sql").read_text(encoding="utf-8")
    return sql[sql.index(START):sql.index(END)]


@pytest.fixture()
def engine():
    """SQLite with the REAL schema block (Postgres-only spellings softened)."""
    from boss_engine.db import split_statements

    eng = create_engine("sqlite://")
    block = (schema_block().replace("'{}'::jsonb", "'{}'")
             .replace("DEFAULT now()", "DEFAULT CURRENT_TIMESTAMP"))
    with eng.begin() as c:
        for statement in split_statements(block):
            c.exec_driver_sql(statement)
    return eng


def settings(**overrides) -> SimpleNamespace:
    base = dict(workspace_snapshots="remote", s3_bucket="boss-snapshots", s3_endpoint=None,
                workspace_snapshot_ttl_hours=72, workspace_seed_ttl_hours=168)
    base.update(overrides)
    return SimpleNamespace(**base)


# ── a fake REMOTE sandbox: a real checkout, reached only through the protocol ──


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
                          cwd=cwd, check=True, capture_output=True, text=True).stdout


def _tar_dir(source: Path) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        archive.add(source, arcname=".")
    return buffer.getvalue()


class FakeRemoteSandbox:
    """Speaks what SSHSandbox speaks (run / diff / sync_in / sync_out) over a
    local dir, moving data as tarballs the way the SSH driver does."""

    def __init__(self, workdir: Path):
        self.workdir = workdir
        self.path = workdir
        self.base = _git(workdir, "rev-parse", "HEAD").strip()
        self.calls: list[str] = []

    def run(self, command, *, timeout=None, cwd=None, env=None):
        self.calls.append(f"run:{command}")
        argv = shlex.split(command)
        proc = subprocess.run(argv, cwd=self.workdir, capture_output=True, text=True,
                              env={**os.environ, **(env or {})})
        return CommandResult(command=command, argv=argv, exit_code=proc.returncode,
                             duration_ms=1, timed_out=False, stdout=proc.stdout,
                             stderr=proc.stderr)

    def diff(self):
        self.calls.append("diff")
        _git(self.workdir, "add", "-A")
        names = _git(self.workdir, "diff", "--cached", "--name-only", self.base).split()
        out = []
        for name in names:
            path = self.workdir / name
            if path.is_file():
                out.append(FileChange(path=name, content=path.read_text()))
        return out

    def sync_out(self, remote_rel, local_path, *, preserve_links=False):
        self.calls.append(f"sync_out:{remote_rel}")
        blob = _tar_dir(self.workdir / remote_rel)
        destination = Path(local_path)
        destination.mkdir(parents=True, exist_ok=True)
        with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as archive:
            _extract_preserving_links(archive, destination)

    def sync_in(self, local_path, remote_rel=""):
        self.calls.append(f"sync_in:{remote_rel}")
        blob = _tar_dir(Path(local_path))
        dest = self.workdir / remote_rel if remote_rel else self.workdir
        dest.mkdir(parents=True, exist_ok=True)
        with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as archive:
            # what the remote `tar -xzf` does: links and modes kept
            _extract_preserving_links(archive, dest)


REMOTE = SimpleNamespace(kind="ssh")
LOCAL = SimpleNamespace(kind="local")


@pytest.fixture()
def origin(tmp_path) -> Path:
    """The repository every sandbox clones: a lockfile, a tracked file, a
    .gitignore that keeps node_modules out of git."""
    repo = tmp_path / "origin"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "package-lock.json").write_text('{"lockfileVersion": 3}\n')
    (repo / "app.js").write_text("module.exports = 1;\n")
    (repo / ".gitignore").write_text("node_modules/\n.venv/\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    return repo


def clone(origin: Path, where: Path) -> FakeRemoteSandbox:
    subprocess.run(["git", "clone", "-q", str(origin), str(where)], check=True)
    return FakeRemoteSandbox(where)


def install_deps(box: FakeRemoteSandbox) -> None:
    """What `npm ci` leaves: a package, an executable, a .bin symlink, a dotfile."""
    nm = box.workdir / "node_modules"
    (nm / "left-pad" / "bin").mkdir(parents=True)
    (nm / "left-pad" / "index.js").write_text("module.exports = 'pad';\n")
    tool = nm / "left-pad" / "bin" / "pad.js"
    tool.write_text("#!/usr/bin/env node\n")
    tool.chmod(0o755)
    (nm / ".bin").mkdir()
    os.symlink("../left-pad/bin/pad.js", nm / ".bin" / "pad")
    (nm / ".package-lock.json").write_text("{}\n")


def ctx_for(engine, store, *, run_id, root_run_id, project_id="proj_1", **over):
    return ws.SnapshotContext(engine=engine, settings=settings(**over), store=store,
                              run_id=run_id, root_run_id=root_run_id,
                              project_id=project_id, repo="acme/app")


def rows(engine, kind=None):
    with engine.begin() as c:
        q = "SELECT * FROM workspace_snapshots"
        if kind:
            q += f" WHERE kind = '{kind}'"
        return [dict(r) for r in c.execute(text(q + " ORDER BY created_at, generation"))
                .mappings().all()]


# ── round trip ───────────────────────────────────────────────────────────────


class TestRoundTrip:
    def test_capture_then_repair_restore(self, tmp_path, engine, origin):
        store = DirectoryObjectStore(tmp_path / "store")
        first = clone(origin, tmp_path / "box1")
        install_deps(first)
        (first.workdir / "src").mkdir()
        (first.workdir / "src" / "new.js").write_text("export const fresh = true;\n")
        (first.workdir / "app.js").write_text("module.exports = 2;  // agent edit\n")

        summary = ws.capture(ctx_for(engine, store, run_id="crun_a", root_run_id="crun_a"),
                             first, "implement")
        assert summary["generation"] == 1
        assert summary["deps"] == ["node_modules"]
        assert summary["seed"] == "written"
        assert summary["tree_files"] == 2
        full, = rows(engine, "full")
        seed, = rows(engine, "seed")
        assert full["object_key"] == "workspace-snapshots/runs/crun_a/g000001-crun_a.tar.gz"
        assert seed["object_key"].startswith("workspace-snapshots/seeds/proj_1/acme__app/")
        assert seed["seed_key"] == full["seed_key"] == summary["seed_key"]
        for row in (full, seed):
            assert store.exists(row["object_key"])
            from boss_engine.services.object_store import sha256_file

            assert sha256_file(store._path(row["object_key"])) == row["digest"]
            assert row["bytes"] > 0 and row["expires_at"]
        # The full snapshot leans on the seed — it does not carry node_modules again.
        assert json.loads(full["manifest"])["deps"] == []

        # A repair round of the same root run on a FRESH remote box. Its branch
        # already moved app.js on (the checkpoint push), so that file is git's.
        second = clone(origin, tmp_path / "box2")
        (second.workdir / "app.js").write_text("module.exports = 3;  // newer on branch\n")
        result = ws.restore(ctx_for(engine, store, run_id="crun_b", root_run_id="crun_a"),
                            second, is_repair=True)
        assert result["full"] == full["object_key"]
        assert result["tree_files"] == 1
        assert result["deps"] == ["node_modules"]
        assert result["seed"] == seed["object_key"]
        assert (second.workdir / "src" / "new.js").read_text() == "export const fresh = true;\n"
        # never overwrites what the clone already has
        assert "newer on branch" in (second.workdir / "app.js").read_text()
        nm = second.workdir / "node_modules"
        assert (nm / "left-pad" / "index.js").read_text() == "module.exports = 'pad';\n"
        link = nm / ".bin" / "pad"
        assert link.is_symlink() and os.readlink(link) == "../left-pad/bin/pad.js"
        assert os.stat(nm / "left-pad" / "bin" / "pad.js").st_mode & stat.S_IXUSR
        assert (nm / ".package-lock.json").exists()  # dotfiles survive

    def test_a_second_checkpoint_reuses_the_seed_and_bumps_the_generation(
            self, tmp_path, engine, origin):
        store = DirectoryObjectStore(tmp_path / "store")
        box = clone(origin, tmp_path / "box")
        install_deps(box)
        ctx = ctx_for(engine, store, run_id="crun_a", root_run_id="crun_a")
        ws.capture(ctx, box, "write_tests")
        box.calls.clear()
        again = ws.capture(ctx, box, "implement")
        assert again["seed"] == "reused" and again["generation"] == 2
        # deps were NOT pulled again
        assert not any(c.startswith("sync_out:") for c in box.calls)
        assert len(rows(engine, "seed")) == 1
        assert [r["generation"] for r in rows(engine, "full")] == [1, 2]

    def test_without_a_lockfile_the_full_snapshot_carries_the_deps(
            self, tmp_path, engine):
        repo = tmp_path / "origin"
        repo.mkdir()
        _git(repo, "init", "-q", "-b", "main")
        (repo / ".gitignore").write_text(".venv/\n")
        (repo / "main.py").write_text("print(1)\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "init")
        store = DirectoryObjectStore(tmp_path / "store")
        box = clone(repo, tmp_path / "box1")
        (box.workdir / ".venv" / "lib").mkdir(parents=True)
        (box.workdir / ".venv" / "lib" / "dep.py").write_text("X = 1\n")
        summary = ws.capture(ctx_for(engine, store, run_id="r1", root_run_id="r1"),
                             box, "implement")
        assert summary["seed_key"] is None and summary["deps"] == [".venv"]
        assert rows(engine, "seed") == []
        fresh = clone(repo, tmp_path / "box2")
        result = ws.restore(ctx_for(engine, store, run_id="r2", root_run_id="r1"),
                            fresh, is_repair=True)
        assert result["deps"] == [".venv"]
        assert (fresh.workdir / ".venv" / "lib" / "dep.py").read_text() == "X = 1\n"

    def test_a_corrupted_object_fails_its_digest_and_the_hook_swallows_it(
            self, tmp_path, engine, origin):
        store = DirectoryObjectStore(tmp_path / "store")
        box = clone(origin, tmp_path / "box1")
        install_deps(box)
        ctx = ctx_for(engine, store, run_id="crun_a", root_run_id="crun_a")
        ws.capture(ctx, box, "implement")
        full, = rows(engine, "full")
        store._path(full["object_key"]).write_bytes(b"not the snapshot")
        fresh = clone(origin, tmp_path / "box2")
        repair = ctx_for(engine, store, run_id="crun_b", root_run_id="crun_a")
        with pytest.raises(ObjectStoreError, match="digest"):
            ws.restore(repair, fresh, is_repair=True)
        assert ws.restore_best_effort(repair, fresh, is_repair=True) is None

    def test_capture_best_effort_never_raises(self, engine):
        class Broken:
            def diff(self):
                raise RuntimeError("remote host gone")

        ctx = ctx_for(engine, DirectoryObjectStore("/nonexistent"), run_id="r", root_run_id="r")
        assert ws.capture_best_effort(ctx, Broken(), "implement") is None
        assert ws.capture_best_effort(None, Broken(), "implement") is None


# ── seed cache ───────────────────────────────────────────────────────────────


class TestSeedCache:
    def _seeded(self, tmp_path, engine, origin):
        store = DirectoryObjectStore(tmp_path / "store")
        box = clone(origin, tmp_path / "seeder")
        install_deps(box)
        ws.capture(ctx_for(engine, store, run_id="crun_a", root_run_id="crun_a"), box,
                   "implement")
        return store

    def test_hit_a_fresh_run_with_the_same_lockfiles_warm_starts(
            self, tmp_path, engine, origin):
        store = self._seeded(tmp_path, engine, origin)
        fresh = clone(origin, tmp_path / "fresh")
        result = ws.restore(ctx_for(engine, store, run_id="crun_new", root_run_id="crun_new"),
                            fresh, is_repair=False)
        assert result["full"] is None  # a fresh run never gets another lineage's tree
        assert result["seed"] and result["deps"] == ["node_modules"]
        assert (fresh.workdir / "node_modules" / "left-pad" / "index.js").exists()
        seed, = rows(engine, "seed")
        assert seed["last_used_at"]

    def test_miss_changed_lockfile(self, tmp_path, engine, origin):
        store = self._seeded(tmp_path, engine, origin)
        fresh = clone(origin, tmp_path / "fresh")
        (fresh.workdir / "package-lock.json").write_text('{"lockfileVersion": 3, "x": 1}\n')
        result = ws.restore(ctx_for(engine, store, run_id="n", root_run_id="n"), fresh,
                            is_repair=False)
        assert result["seed"] is None and result["deps"] == []
        assert not (fresh.workdir / "node_modules").exists()

    def test_miss_another_project(self, tmp_path, engine, origin):
        store = self._seeded(tmp_path, engine, origin)
        fresh = clone(origin, tmp_path / "fresh")
        result = ws.restore(ctx_for(engine, store, run_id="n", root_run_id="n",
                                    project_id="proj_other"), fresh, is_repair=False)
        assert result["seed"] is None

    def test_expired_seed_is_a_miss(self, tmp_path, engine, origin):
        store = self._seeded(tmp_path, engine, origin)
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        with engine.begin() as c:
            c.execute(text("UPDATE workspace_snapshots SET expires_at = :p"), {"p": past})
        fresh = clone(origin, tmp_path / "fresh")
        result = ws.restore(ctx_for(engine, store, run_id="n", root_run_id="n"), fresh,
                            is_repair=False)
        assert result["seed"] is None

    def test_the_key_is_the_lockfiles_git_blob_ids(self, tmp_path, origin):
        box = clone(origin, tmp_path / "box")
        data = (box.workdir / "package-lock.json").read_bytes()
        blob = ws.git_blob_sha(data)
        assert blob == _git(box.workdir, "hash-object", "package-lock.json").strip()
        assert ws.lock_key(box) == ws.seed_key_from([("package-lock.json", blob)])
        assert ws.seed_key_from([]) is None
        # requirements*.txt counts, a random txt does not
        assert ws.LOCKFILE_RE.match("requirements-dev.txt")
        assert not ws.LOCKFILE_RE.match("notes.txt")


# ── off is byte-identical ────────────────────────────────────────────────────


class Untouchable:
    """Any attribute access beyond the probe is a failure."""

    def __init__(self):
        self.touched: list[str] = []

    def __getattr__(self, name):
        self.touched.append(name)
        return lambda *a, **k: (_ for _ in ()).throw(AssertionError(f"called {name}"))


class TestOffIsByteIdentical:
    def test_off_never_looks_at_the_sandbox_or_store(self):
        box = Untouchable()
        assert ws.enabled_for(settings(workspace_snapshots="off"), REMOTE, box) is False
        assert ws.enabled_for(SimpleNamespace(), REMOTE, box) is False  # knob unset = off
        assert box.touched == []

    def test_remote_mode_still_skips_local_sandboxes_and_missing_store(self, tmp_path, origin):
        box = clone(origin, tmp_path / "box")
        assert ws.enabled_for(settings(), LOCAL, box) is False
        assert ws.enabled_for(settings(), SimpleNamespace(kind="microvm"), box) is False
        assert ws.enabled_for(settings(s3_bucket=None), REMOTE, box) is False
        assert ws.enabled_for(settings(), REMOTE, box) is True
        assert box.calls == []  # deciding never runs anything remotely
        assert ws.mode(settings(workspace_snapshots="bogus")) == "off"

    def test_config_default_is_off(self):
        from boss_engine.config import Settings

        assert Settings.model_fields["workspace_snapshots"].default == "off"

    def test_the_governed_hooks_are_guarded(self):
        src = (ROOT / "engine" / "boss_engine" / "services"
               / "governed_code_task_service.py").read_text(encoding="utf-8")
        assert src.count("_ws_snapshot: list = [None]") == 1
        start = src.index("    def _checkpoint(label: str) -> int:")
        body = src[start:src.index("\n    def ", start + 10)]
        # the capture sits behind the armed slot, which only the guard below arms
        guard = body.index("if _ws_snapshot[0] is not None:")
        assert guard < body.index("capture_best_effort(")
        assert src.count("_ws_snapshots.capture_best_effort(") == 1
        assert src.count("_ws_snapshots.restore_best_effort(") == 1
        provision = src.index("_ws_snapshots.enabled_for(settings, provider, sandbox)")
        assert src.index("_ws_snapshot[0] = _ws_snapshots.context_for(") > provision
        assert "if not use_microvm and _ws_snapshots.enabled_for(" in src
        # the factory seam and the provider lines were not moved by this change
        assert src.count("provider = make_governed_sandbox_provider(") == 1


# ── reaper ───────────────────────────────────────────────────────────────────


class TestReaper:
    def test_expired_snapshots_go_with_their_objects(self, tmp_path, engine, origin):
        store = DirectoryObjectStore(tmp_path / "store")
        box = clone(origin, tmp_path / "box")
        install_deps(box)
        ctx = ctx_for(engine, store, run_id="crun_a", root_run_id="crun_a")
        ws.capture(ctx, box, "write_tests")
        ws.capture(ctx, box, "implement")
        everything = rows(engine)
        assert len(everything) == 3
        past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        stale = [r for r in everything if r["kind"] == "full" and r["generation"] == 1][0]
        with engine.begin() as c:
            c.execute(text("UPDATE workspace_snapshots SET expires_at = :p WHERE id = :i"),
                      {"p": past, "i": stale["id"]})
        report = ws.reap_expired(engine, settings(), store=store)
        assert report == {"expired": 1, "failed": 0}
        left = rows(engine)
        assert stale["id"] not in {r["id"] for r in left} and len(left) == 2
        assert not store.exists(stale["object_key"])
        assert all(store.exists(r["object_key"]) for r in left)

    def test_a_failed_delete_keeps_the_row_for_the_next_tick(self, engine):
        past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        with engine.begin() as c:
            c.execute(text(
                "INSERT INTO workspace_snapshots (id, run_id, kind, object_key, digest, "
                "expires_at, created_at) VALUES ('wss_1', 'r', 'full', 'k/x.tar.gz', 'd', :p, :p)"),
                {"p": past})

        class Flaky(DirectoryObjectStore):
            def delete(self, key):
                raise ObjectStoreError("store down")

        assert ws.reap_expired(engine, settings(), store=Flaky("/x")) == {
            "expired": 0, "failed": 1}
        assert len(rows(engine)) == 1
        # an already-gone object is fine: the row goes
        assert ws.reap_expired(engine, settings(),
                               store=DirectoryObjectStore("/nonexistent"))["expired"] == 1

    def test_no_store_touches_nothing(self):
        class NoEngine:
            def begin(self):
                raise AssertionError("the reaper touched the database with no store")

        assert ws.reap_expired(NoEngine(), settings(s3_bucket=None)) == {
            "expired": 0, "skipped": "no_store"}

    def test_pre_826_schema_is_tolerated(self, tmp_path):
        eng = create_engine("sqlite://")
        report = ws.reap_expired(eng, settings(), store=DirectoryObjectStore(tmp_path))
        assert report["expired"] == 0 and "skipped" in report

    def test_registered_on_the_resource_reaper_tick(self):
        # Source-level, like the 824 lease reaper: running tick() for real would
        # sweep this laptop's own BOSS temp dirs.
        src = (ROOT / "engine" / "boss_engine" / "services"
               / "resource_reaper.py").read_text(encoding="utf-8")
        tick = src[src.index("def tick("):]
        assert 'report["workspace_snapshots"] = _safe(' in tick
        assert "workspace_snapshots.reap_expired(engine, settings)" in tick


# ── object store + the 824 seam extension ────────────────────────────────────


class TestObjectStore:
    def test_directory_store_round_trip_and_key_rules(self, tmp_path):
        store = DirectoryObjectStore(tmp_path / "s")
        src = tmp_path / "f"
        src.write_bytes(b"abc")
        store.put_file("a/b.tar.gz", src)
        assert store.exists("a/b.tar.gz")
        store.get_file("a/b.tar.gz", tmp_path / "out")
        assert (tmp_path / "out").read_bytes() == b"abc"
        store.delete("a/b.tar.gz")
        store.delete("a/b.tar.gz")  # idempotent
        with pytest.raises(ObjectNotFound):
            store.get_file("a/b.tar.gz", tmp_path / "out2")
        for bad in ("../x", "/abs", "a/../../x", ""):
            with pytest.raises(ObjectStoreError):
                store.put_file(bad, src)

    def test_from_settings(self, tmp_path):
        assert object_store_from_settings(SimpleNamespace(s3_bucket=None)) is None
        local = object_store_from_settings(SimpleNamespace(
            s3_bucket="snaps", s3_endpoint=f"file://{tmp_path}"))
        assert isinstance(local, DirectoryObjectStore) and local.root == tmp_path / "snaps"
        s3 = object_store_from_settings(SimpleNamespace(
            s3_bucket="snaps", s3_endpoint="http://minio:9000", s3_access_key_id="a",
            s3_secret_access_key="b", s3_region=None))
        assert isinstance(s3, S3ObjectStore) and s3.bucket == "snaps"
        with pytest.raises(ObjectStoreError):
            S3ObjectStore(bucket="Not_A_Bucket")

    def test_s3_store_speaks_boto_and_maps_missing(self, tmp_path):
        class Missing(Exception):
            response = {"Error": {"Code": "404"}}

        class FakeClient:
            def __init__(self):
                self.objects = {}

            def upload_file(self, path, bucket, key):
                self.objects[(bucket, key)] = Path(path).read_bytes()

            def download_file(self, bucket, key, path):
                if (bucket, key) not in self.objects:
                    raise Missing()
                Path(path).write_bytes(self.objects[(bucket, key)])

            def delete_object(self, Bucket, Key):  # noqa: N803 — boto's names
                self.objects.pop((Bucket, Key), None)

            def head_object(self, Bucket, Key):  # noqa: N803
                if (Bucket, Key) not in self.objects:
                    raise Missing()

        store = S3ObjectStore(bucket="snaps", client=FakeClient())
        src = tmp_path / "f"
        src.write_bytes(b"xyz")
        store.put_file("k/1", src)
        assert store.exists("k/1") and not store.exists("k/2")
        store.get_file("k/1", tmp_path / "o")
        assert (tmp_path / "o").read_bytes() == b"xyz"
        with pytest.raises(ObjectNotFound):
            store.get_file("k/2", tmp_path / "o2")


def _b64_tarball(build) -> str:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        build(archive)
    return base64.b64encode(gzip.compress(buffer.getvalue())).decode()


class TestSSHSyncOutPreservesLinks:
    def _sandbox(self, stdout: str):
        from boss_engine.services.ssh_environment import SSHSandbox
        from boss_engine.services.stack_deploy import HostAccess

        class Transport:
            def stack_exec(self, **kwargs):
                return subprocess.CompletedProcess([], 0, stdout, "")

        access = HostAccess(host="203.0.113.5", user="root", port=22,
                            key_material="FAKE-KEY", adapter=Transport())
        box = SSHSandbox(SimpleNamespace(sandbox_validation_timeout_seconds=600),
                         task_id="crun_826", access=access, environment={"config": {}})
        box.path = Path("/var/tmp/x")
        return box

    def _archive(self, archive, outside="/nonexistent-outside"):
        def add(name, data=b"", mode=0o644, **kw):
            info = tarfile.TarInfo(name)
            for k, v in kw.items():
                setattr(info, k, v)
            info.mode = mode
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))

        add("./.bin", type=tarfile.DIRTYPE, mode=0o755)
        add("./pkg/cli.js", b"#!/usr/bin/env node\n", mode=0o755)
        add("./.bin/cli", type=tarfile.SYMTYPE, linkname="../pkg/cli.js")
        add("./escape", type=tarfile.SYMTYPE, linkname=outside)
        add("./escape/passwd", b"pwned")  # must never be written through the link
        add("./pkg/hard.js", type=tarfile.LNKTYPE, linkname="./pkg/cli.js")

    def test_preserve_links_keeps_symlinks_modes_and_never_writes_outside(self, tmp_path):
        outside = tmp_path / "outside"
        outside.mkdir()
        box = self._sandbox(_b64_tarball(lambda a: self._archive(a, str(outside))) + "\n")
        out = tmp_path / "out"
        box.sync_out("node_modules", out, preserve_links=True)
        assert os.readlink(out / ".bin" / "cli") == "../pkg/cli.js"
        assert os.stat(out / "pkg" / "cli.js").st_mode & stat.S_IXUSR
        assert (out / "pkg" / "hard.js").read_bytes() == b"#!/usr/bin/env node\n"
        assert (out / "escape").is_symlink()
        assert not (outside / "passwd").exists()  # never written through the link

    def test_default_sync_out_is_unchanged_but_keeps_dotfiles(self, tmp_path):
        box = self._sandbox(_b64_tarball(self._archive) + "\n")
        out = tmp_path / "out"
        box.sync_out("node_modules", out)
        assert not (out / ".bin" / "cli").exists()  # links still dropped by default
        assert (out / ".bin").is_dir()  # but ".bin" is no longer renamed "bin"
        assert not (out / "bin").exists()
        assert (out / "pkg" / "cli.js").is_file()


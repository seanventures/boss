"""`boss steward` (scripts/install/steward.py), the Infra Agent's host helper (LLD §7.2, §9):
a tampered, expired, foreign or unlisted job is refused; a real run without the dry run's
plan_hash is refused; the label gate on the host never deletes an unlabelled image, volume or a
backup outside BACKUP_ROOT, whatever the job asks. Docker is a fake; the backups folder is a
temporary directory — never the laptop's.
"""
from __future__ import annotations

import base64
import importlib.util
import json
import os
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _load():
    spec = importlib.util.spec_from_file_location("_steward", ROOT / "scripts" / "install" / "steward.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


steward = _load()


def test_colima_trim_uses_selected_profile_and_measures_reclaimed_bytes(env, monkeypatch):
    env.is_mac = True
    env.docker = ["docker", "--context", "colima-example"]
    monkeypatch.setattr(steward, "docker_raw", lambda _: (8_000_000_000, 20_000_000_000))
    monkeypatch.setattr(steward, "docker_df", lambda *_: dict(images=1, cache=0, volumes=0, containers=0))
    calls = []
    def runner(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")
    assert steward.verb_docker_trim(env, {}, dry_run=True, runner=runner)[0] == ["colima:example"]
    assert calls == []
    sizes = iter([(8_000_000_000, 20_000_000_000), (6_000_000_000, 20_000_000_000)])
    monkeypatch.setattr(steward, "docker_raw", lambda _: next(sizes))
    monkeypatch.setattr(steward.time, "sleep", lambda _: None)
    assert steward.verb_docker_trim(env, {}, dry_run=False, runner=runner)[1] == 2_000_000_000
    assert calls == [["colima", "--profile", "example", "ssh", "--", "sudo", "fstrim", "-a"]]


def test_colima_missing_disk_never_falls_back_to_desktop(env, monkeypatch, tmp_path):
    env.is_mac = True
    env.docker = ["docker", "--context", "colima-example"]
    monkeypatch.setenv("COLIMA_HOME", str(tmp_path))
    assert steward.docker_raw(env) == (None, None)


def test_steward_pins_configured_context_without_requiring_shell_profile(tmp_path, monkeypatch):
    monkeypatch.delenv("BOSS_DOCKER_CONTEXT", raising=False)
    (tmp_path / ".env").write_text("BOSS_DOCKER_CONTEXT=colima-example\n")
    assert steward.Env(str(tmp_path)).docker == ["docker", "--context", "colima-example"]
    assert steward.Env(str(tmp_path), docker=["fake-docker"]).docker == ["fake-docker"]
    (tmp_path / ".env").write_text("")
    assert steward.Env(str(tmp_path)).docker == ["docker"]


@pytest.fixture()
def keypair():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    priv = Ed25519PrivateKey.generate()
    raw = priv.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    pub = {"key_id": "k1", "algorithm": "Ed25519", "public_key": base64.b64encode(raw).decode()}
    return priv, pub


@pytest.fixture()
def env(tmp_path, monkeypatch):
    install = tmp_path / "install"
    install.mkdir()
    (install / ".env").write_text("BOSS_INSTALL_ID=inst_abc\nBOSS_HOST_API_KEY=not-a-real-key\n"
                                  "BOSS_COMMUNITY_IMAGE_TAG=v1\nBOSS_DEDICATED_HOST=1\n")
    backups = tmp_path / "backups"
    backups.mkdir()
    for name in ("BOSS_INSTALL_ID", "BOSS_HOST_ID", "BOSS_HOST_API_KEY", "BOSS_BACKUP_DIR"):
        monkeypatch.delenv(name, raising=False)
    e = steward.Env(str(install), api_url="http://127.0.0.1:1", backup_root=str(backups),
                    state_dir=str(tmp_path / "state"), docker=["fake-docker"])
    e.is_mac = False
    return e


def _job(priv, env, verb, args, *, host_id=None, expires_in=600, key_id="k1", tamper=False):
    exp = (datetime.now(timezone.utc) + timedelta(seconds=expires_in)).isoformat()
    jid = "hjob_1"
    payload = steward.canonical(jid, host_id or env.host_id, verb, args, exp)
    sig = base64.b64encode(priv.sign(payload)).decode()
    job = {"id": jid, "host_id": host_id or env.host_id, "verb": verb, "args": args, "expires_at": exp,
           "signature": sig, "key_id": key_id}
    if tamper:
        job["args"] = {**args, "keep_newest": 0}
    return job


class FakeDocker:
    """`docker` as the steward drives it: images/volumes with labels (and sizes only `system df
    -v` knows, as on a real daemon), containers that use volumes; rm records what went."""

    def __init__(self, images, volumes, containers=None):
        self.images, self.volumes, self.removed = images, volumes, []
        self.containers = containers or []   # {"id", "name", "state", "volumes", "labels"}
        self.on_call = None                  # a test's hook, called with argv before each answer

    @staticmethod
    def _label_filters(a):
        return [a[i + 1][len("label="):].partition("=") for i, x in enumerate(a)
                if x == "--filter" and i + 1 < len(a) and a[i + 1].startswith("label=")]

    def __call__(self, argv, timeout=120.0):
        if self.on_call:
            self.on_call(argv)
        a = argv[1:]
        out, rc = "", 0
        if a[:1] == ["images"]:
            label = next((x.split("=", 2)[2] for x in a if x.startswith("label=")), None)
            rows = [i for i in self.images if label is None or i["labels"].get("boss.install_id") == label]
            out = "\n".join(json.dumps({"Repository": i["repo"], "Tag": i["tag"], "ID": i["id"], "Size": i["size"]}) for i in rows)
        elif a[:2] == ["volume", "ls"]:
            rows = [v for v in self.volumes
                    if all(v["labels"].get(k) == val for k, _, val in self._label_filters(a))
                    and ("dangling=true" not in a or not any(v["name"] in c["volumes"] for c in self.containers))]
            # a real `docker volume ls` prints "N/A" for the size
            out = "\n".join(json.dumps({"Name": v["name"], "Size": "N/A",
                                        "Labels": ",".join(f"{k}={x}" for k, x in v["labels"].items())})
                            for v in rows)
        elif a[:2] == ["volume", "inspect"]:
            name = a[-1]
            v = next(v for v in self.volumes if v["name"] == name)
            # {{index .Labels "<key>"}} reads that label; {{.CreatedAt}} the birth
            out = v["labels"].get(a[3].split('"')[1], "") if "Labels" in a[3] else v["created"]
        elif a[:2] == ["system", "df"] and "-v" in a:
            out = json.dumps({"Images": [], "Containers": [], "BuildCache": [],
                              "Volumes": [{"Name": v["name"], "Size": v.get("size", "1GB")} for v in self.volumes]})
        elif a[:1] == ["ps"] and any(x.startswith("volume=") for x in a):
            vol = next(x for x in a if x.startswith("volume="))[len("volume="):]
            out = "\n".join(json.dumps({"ID": c["id"], "Names": c["name"], "State": c["state"],
                                        "Labels": ",".join(f"{k}={x}" for k, x in c["labels"].items())})
                            for c in self.containers if vol in c["volumes"])
        elif a[:1] == ["rm"]:
            self.removed.append(("container", a[-1]))
            self.containers = [c for c in self.containers if c["id"] != a[-1]]
        elif a[:2] == ["image", "inspect"]:
            ref = a[-1]
            i = next((i for i in self.images if f"{i['repo']}:{i['tag']}" == ref or i["id"] == ref), None)
            out = (i or {"labels": {}})["labels"].get("boss.install_id", "")
        elif a[:1] == ["ps"]:
            out = "boss-community-api:v1"
        elif a[:2] == ["volume", "rm"]:
            if any(a[-1] in c["volumes"] for c in self.containers):
                rc = 1     # Docker refuses to remove a volume a container still references
            else:
                self.removed.append(("volume", a[-1]))
                self.volumes = [v for v in self.volumes if v["name"] != a[-1]]
        elif a[:1] == ["rmi"]:
            self.removed.append(("image", a[-1]))
        elif a[:2] == ["system", "df"]:
            out = "\n".join(json.dumps(r) for r in [
                {"Type": "Images", "Size": "30GB", "Reclaimable": "10GB (33%)"},
                {"Type": "Build Cache", "Size": "18GB", "Reclaimable": "18GB"},
                {"Type": "Local Volumes", "Size": "4GB", "Reclaimable": "4GB"},
                {"Type": "Containers", "Size": "600MB", "Reclaimable": "0B"}])
        return subprocess.CompletedProcess(argv, rc, out, "")


@pytest.fixture()
def docker():
    old = datetime(2026, 11, 1, tzinfo=timezone.utc).isoformat()
    return FakeDocker(
        images=[{"repo": "boss-community-api", "tag": "v1", "id": "sha1", "size": "5GB", "labels": {"boss.install_id": "inst_abc"}},
                {"repo": "boss-community-api", "tag": "prev-v1", "id": "sha2", "size": "5GB", "labels": {"boss.install_id": "inst_abc"}},
                {"repo": "boss-community-api", "tag": "v0", "id": "sha3", "size": "5GB", "labels": {"boss.install_id": "inst_abc"}},
                {"repo": "wordpress", "tag": "latest", "id": "sha4", "size": "1GB", "labels": {}},
                {"repo": "boss-community-api", "tag": "v-other", "id": "sha5", "size": "5GB", "labels": {"boss.install_id": "inst_other"}}],
        volumes=[{"name": "boss_tmp_1", "labels": {"boss.install_id": "inst_abc", "boss.kind": "test"}, "created": old,
                  "size": "2.5GB"},
                 # ours, but a DATA volume (a stopped preview's database) and one with no kind
                 {"name": "boss_preview_pgdata", "labels": {"boss.install_id": "inst_abc", "boss.kind": "data"},
                  "created": old},
                 {"name": "boss_unkinded", "labels": {"boss.install_id": "inst_abc"}, "created": old},
                 {"name": "my_wordpress_db", "labels": {}, "created": old},
                 {"name": "other_install_vol", "labels": {"boss.install_id": "inst_other"}, "created": old}])


# ── signature, expiry, verb, install ────────────────────────────────────────────────────────

def test_host_job_signature_and_expiry(env, keypair, docker):
    priv, pub = keypair
    good = _job(priv, env, "du", {})
    assert steward.perform(env, good, pub, runner=docker)["ok"]
    tampered = _job(priv, env, "remove_backups", {"install_id": "inst_abc", "dry_run": True}, tamper=True)
    out = steward.perform(env, tampered, pub, runner=docker)
    assert not out["ok"] and "signature" in out["error"]
    expired = _job(priv, env, "du", {}, expires_in=-5)
    out = steward.perform(env, expired, pub, runner=docker)
    assert not out["ok"] and "expired" in out["error"]
    other_key = _job(priv, env, "du", {}, key_id="k-unknown")
    assert "key" in steward.perform(env, other_key, pub, runner=docker)["error"]
    unsigned = {**good, "signature": base64.b64encode(b"\0" * 64).decode()}
    assert not steward.perform(env, unsigned, pub, runner=docker)["ok"]


def test_unlisted_verb_other_host_and_other_install_are_refused(env, keypair, docker):
    priv, pub = keypair
    assert "not allowed" in steward.perform(env, _job(priv, env, "rm_rf", {"install_id": "inst_abc"}), pub, runner=docker)["error"]
    assert "another host" in steward.perform(env, _job(priv, env, "du", {}, host_id="someone-else"), pub, runner=docker)["error"]
    out = steward.perform(env, _job(priv, env, "remove_images", {"install_id": "inst_other", "dry_run": True}), pub, runner=docker)
    assert "another install" in out["error"]


def test_a_real_run_without_the_dry_runs_plan_hash_is_refused(env, keypair, docker):
    priv, pub = keypair
    out = steward.perform(env, _job(priv, env, "remove_images", {"install_id": "inst_abc", "dry_run": False}), pub, runner=docker)
    assert not out["ok"] and "plan_hash" in out["error"] and docker.removed == []
    wrong = _job(priv, env, "remove_images", {"install_id": "inst_abc", "dry_run": False, "plan_hash": "nope"})
    out = steward.perform(env, wrong, pub, runner=docker)
    assert not out["ok"] and "changed since the dry run" in out["error"] and docker.removed == []


# ── the label gate on the host ──────────────────────────────────────────────────────────────

def test_helper_label_gate_images(env, keypair, docker):
    priv, pub = keypair
    dry = steward.perform(env, _job(priv, env, "remove_images", {"install_id": "inst_abc", "dry_run": True}), pub, runner=docker)
    assert dry["ok"] and dry["dry_run"]
    # only the labelled, non-current, non-rollback, not-in-use image of THIS install
    assert dry["items"] == ["boss-community-api:v0"]
    real = steward.perform(env, _job(priv, env, "remove_images", {"install_id": "inst_abc", "dry_run": False,
                                                                 "plan_hash": dry["plan_hash"]}), pub, runner=docker)
    assert real["ok"] and real["items"] == ["boss-community-api:v0"]
    assert docker.removed == [("image", "boss-community-api:v0")]
    assert ("image", "wordpress:latest") not in docker.removed


def test_helper_label_gate_volumes(env, keypair, docker):
    priv, pub = keypair
    dry = steward.perform(env, _job(priv, env, "remove_orphan_volumes", {"install_id": "inst_abc", "dry_run": True, "older_than_hours": 24}), pub, runner=docker)
    assert dry["items"] == ["boss_tmp_1"]
    real = steward.perform(env, _job(priv, env, "remove_orphan_volumes", {"install_id": "inst_abc", "dry_run": False, "older_than_hours": 24,
                                                                         "plan_hash": dry["plan_hash"]}), pub, runner=docker)
    assert real["ok"] and docker.removed == [("volume", "boss_tmp_1")]


def test_a_labelled_data_volume_is_never_removed_even_when_a_job_names_it(env, keypair, docker):
    """ST-14: the install label says WHOSE a volume is; `boss.kind` says WHAT it holds. A data
    volume (or one with no kind) left dangling — a stopped preview's database, an install brought
    down for a day — is irreversible: not a candidate, and a real run re-reads the labels at the
    moment of removal, so one whose kind changed after the plan was checked is left alone."""
    priv, pub = keypair
    args = {"install_id": "inst_abc", "dry_run": True, "older_than_hours": 24}
    dry = steward.perform(env, _job(priv, env, "remove_orphan_volumes", args), pub, runner=docker)
    assert dry["items"] == ["boss_tmp_1"]
    assert "boss_preview_pgdata" not in dry["items"] and "boss_unkinded" not in dry["items"]

    # How many times one plan reads boss_tmp_1's kind: the real run plans twice (the plan-hash
    # check, then the verb's own list) and then reads it once more, just before `volume rm`.
    reads = []

    def count(argv):
        if argv[1:3] == ["volume", "inspect"] and "boss.kind" in argv[4] and argv[-1] == "boss_tmp_1":
            reads.append(1)

    docker.on_call = count
    steward.verb_remove_orphan_volumes(env, args, dry_run=True, runner=docker)
    per_plan = len(reads)
    assert per_plan >= 1

    # The kind flips to data AFTER both plans have read it (so the plan hash still matches) and
    # BEFORE the removal-time re-check: only that re-check can stop the removal.
    reads.clear()

    def flip_after_planning(argv):
        # the first docker call after both plans' last read of the kind (its answer was "test")
        if len(reads) == 2 * per_plan and docker.volumes[0]["labels"]["boss.kind"] == "test":
            docker.volumes[0]["labels"]["boss.kind"] = "data"
        count(argv)

    docker.on_call = flip_after_planning
    out = steward.perform(env, _job(priv, env, "remove_orphan_volumes",
                                    {**args, "dry_run": False, "plan_hash": dry["plan_hash"]}), pub, runner=docker)
    assert out["ok"], out              # the plan-hash check passed: the plans saw kind=test
    assert len(reads) == 2 * per_plan + 1, "the real run re-read the kind once more, at removal"
    assert out["items"] == [] and out["bytes"] == 0
    assert docker.removed == [], "the removal-time re-check refused the volume that turned into data"


def test_a_preview_volume_is_not_rebuildable(env, keypair, docker):
    """A preview runs the app's own database; a reviewer may have typed data into it. Labelled
    boss.kind=preview, dangling and old, it is still never a candidate (BOSSAPP-1193)."""
    priv, pub = keypair
    old = datetime(2026, 11, 1, tzinfo=timezone.utc).isoformat()
    docker.volumes.append({"name": "boss_preview_app_db", "created": old,
                           "labels": {"boss.install_id": "inst_abc", "boss.kind": "preview"}})
    dry = steward.perform(env, _job(priv, env, "remove_orphan_volumes",
                                    {"install_id": "inst_abc", "dry_run": True, "older_than_hours": 24}), pub, runner=docker)
    assert dry["items"] == ["boss_tmp_1"]
    real = steward.perform(env, _job(priv, env, "remove_orphan_volumes",
                                     {"install_id": "inst_abc", "dry_run": False, "older_than_hours": 24,
                                      "plan_hash": dry["plan_hash"]}), pub, runner=docker)
    assert ("volume", "boss_preview_app_db") not in docker.removed


def test_orphan_volume_sizes_are_measured_so_planned_and_freed_bytes_are_real(env, keypair, docker):
    """`docker volume ls` prints no size: the dry run's bytes and the freed bytes come from
    `docker system df -v`, measured before removal (the job used to report 0 GB freed)."""
    priv, pub = keypair
    args = {"install_id": "inst_abc", "dry_run": True, "older_than_hours": 24}
    dry = steward.perform(env, _job(priv, env, "remove_orphan_volumes", args), pub, runner=docker)
    assert dry["items"] == ["boss_tmp_1"] and dry["bytes"] == int(2.5e9)
    real = steward.perform(env, _job(priv, env, "remove_orphan_volumes",
                                     {**args, "dry_run": False, "plan_hash": dry["plan_hash"]}), pub, runner=docker)
    assert real["ok"] and real["items"] == ["boss_tmp_1"] and real["bytes"] == int(2.5e9)
    # the reading: what a job could really free, not every volume on the disk
    docker.volumes.append({"name": "boss_tmp_2", "created": datetime(2026, 11, 1, tzinfo=timezone.utc).isoformat(),
                           "size": "1.5GB", "labels": {"boss.install_id": "inst_abc", "boss.kind": "cache"}})
    sample = steward.collect(env, runner=docker)
    assert sample["volumes_gb"] == 4.0, "the disk figure stays the whole of Docker's volumes"
    assert sample["by_type"]["volumes_reclaimable"] == 1.5


def _compose_volume(name, key, labels=None, created=None):
    base = {"com.docker.compose.project": "boss-community", "com.docker.compose.volume": key}
    return {"name": name, "created": created or datetime(2026, 11, 1, tzinfo=timezone.utc).isoformat(),
            "labels": {**base, **(labels or {})}}


OLD_PG_TEST = "boss-community_boss_community_pg_test"            # retired, pre-label keys
OLD_CACHE = "boss-community_boss_community_toolchain_cache"
NEW_PG_TEST = "boss-community_boss_community_pg_test_v2"         # their labelled successors
NEW_CACHE = "boss-community_boss_community_toolchain_cache_v2"
OURS = {"boss.install_id": "inst_abc"}


def test_an_unlabelled_boss_volume_is_reported_not_ignored(env, keypair, docker):
    """A retired pre-label BOSS volume (or a current one made without this install's id) cannot
    be judged: the dry run names it as needing a relabel (never an item), and the reading counts
    it. Labelled `_v2` volumes and data volumes are not reported."""
    priv, pub = keypair
    docker.volumes += [
        _compose_volume(OLD_PG_TEST, "boss_community_pg_test", {"owner": "boss", "kind": "test"}),
        _compose_volume(NEW_PG_TEST, "boss_community_pg_test_v2", {**OURS, "boss.kind": "test"}),
        _compose_volume(NEW_CACHE, "boss_community_toolchain_cache_v2", {"boss.install_id": "", "boss.kind": "cache"}),
        _compose_volume("boss-community_boss_community_pg", "boss_community_pg"),
    ]
    docker.containers = [{"id": "c-api", "name": "boss-community-api-1", "state": "running", "volumes": [NEW_CACHE],
                          "labels": {"com.docker.compose.project": "boss-community", "com.docker.compose.service": "api"}}]
    dry = steward.perform(env, _job(priv, env, "remove_orphan_volumes",
                                    {"install_id": "inst_abc", "dry_run": True, "older_than_hours": 24}), pub, runner=docker)
    # the labelled, dangling test volume is a candidate; the retired one and data are not
    assert sorted(dry["items"]) == [NEW_PG_TEST, "boss_tmp_1"]
    assert dry["needs_relabel"] == [OLD_PG_TEST, NEW_CACHE]
    assert steward.collect(env, runner=docker)["by_type"]["volumes_needs_relabel_n"] == 2.0
    assert steward.NEEDS_RELABEL == "unlabelled BOSS volume — needs relabel"


def test_relabel_removes_only_unused_retired_or_unlabelled_boss_volumes(env, docker):
    """THE UPGRADE STEP: the services moved to the labelled `_v2` volumes; a retired pre-label
    volume is removed once nothing mounts it. A running user, a container outside BOSS, or a BOSS
    service Compose would not recreate keeps it, reported. It never stops a service, and a data
    volume is never touched, whatever its labels."""
    docker.volumes += [
        _compose_volume(OLD_PG_TEST, "boss_community_pg_test", {"kind": "test"}),
        _compose_volume(OLD_CACHE, "boss_community_toolchain_cache"),
        _compose_volume(NEW_CACHE, "boss_community_toolchain_cache_v2", {**OURS, "boss.kind": "cache"}),
        _compose_volume("boss-community_boss_community_pg", "boss_community_pg"),
        _compose_volume("boss-community_boss_community_backups", "boss_community_backups"),
    ]
    boss = {"com.docker.compose.project": "boss-community"}
    docker.containers = [
        # db-test stopped on the old volume: Compose recreates it on `boss dev up`, so it may go
        {"id": "c-dbtest", "name": "boss-community-db-test-1", "state": "exited",
         "volumes": [OLD_PG_TEST], "labels": {**boss, "com.docker.compose.service": "db-test"}},
        # an API not yet recreated still runs on the old cache: in use, left alone
        {"id": "c-api", "name": "boss-community-api-1", "state": "running",
         "volumes": [OLD_CACHE, "boss-community_boss_community_pg"],
         "labels": {**boss, "com.docker.compose.service": "api"}},
    ]
    calls = []
    docker.on_call = calls.append
    seen = steward.relabel_volumes(env, docker, apply=False)
    assert {r["volume"]: r["action"] for r in seen} == {OLD_PG_TEST: "would_relabel", OLD_CACHE: "skipped"}
    assert docker.removed == [], "a dry run changes nothing"

    done = {r["volume"]: r for r in steward.relabel_volumes(env, docker)}
    assert done[OLD_PG_TEST]["action"] == "relabelled"
    assert done[OLD_CACHE]["action"] == "skipped"
    assert steward.NEEDS_RELABEL in done[OLD_CACHE]["reason"]
    assert "boss-community-api-1" in done[OLD_CACHE]["reason"]
    assert docker.removed == [("container", "c-dbtest"), ("volume", OLD_PG_TEST)]
    names = {v["name"] for v in docker.volumes}
    assert {"boss-community_boss_community_pg", "boss-community_boss_community_backups", OLD_CACHE, NEW_CACHE} <= names

    # the API moved to the labelled cache: the old one is free and goes
    docker.containers[0]["volumes"] = [NEW_CACHE, "boss-community_boss_community_pg"]
    after = {r["volume"]: r for r in steward.relabel_volumes(env, docker)}
    assert after == {OLD_CACHE: after[OLD_CACHE]} and after[OLD_CACHE]["action"] == "relabelled"
    assert NEW_CACHE in {v["name"] for v in docker.volumes}, "the labelled successor stays"
    assert steward.unlabelled_boss_volumes(env, docker) == [], "nothing left to report"

    # a stopped container outside BOSS holding a retired volume: still not ours to remove
    docker.volumes.append(_compose_volume(OLD_CACHE, "boss_community_toolchain_cache"))
    docker.containers = [{"id": "c-x", "name": "someones-tool", "state": "exited", "volumes": [OLD_CACHE], "labels": {}}]
    again = {r["volume"]: r for r in steward.relabel_volumes(env, docker)}
    assert again[OLD_CACHE]["action"] == "skipped" and "someones-tool" in again[OLD_CACHE]["reason"]
    assert ("container", "c-x") not in docker.removed

    # no install id: nothing is removed (a recreated volume would be labelled with nothing)
    env.install_id = ""
    assert steward.relabel_volumes(env, docker)[0]["action"] == "skipped"
    assert not any(c[1:2] in (["stop"], ["kill"], ["compose"]) for c in calls), "never stops a service"


def test_relabel_re_reads_the_volume_at_the_moment_of_removal(env, docker):
    """Between listing and removal the name can come to mean something else (a volume of another
    project, or a data key, made under the same name): the step re-reads the volume's Compose
    project and key just before `volume rm`, and leaves it when either no longer qualifies."""
    docker.volumes.append(_compose_volume(OLD_PG_TEST, "boss_community_pg_test"))
    v = docker.volumes[-1]

    def swap_after_listing(argv):
        # after the list and the who-uses-it check, before the removal-time re-read
        if argv[1:2] == ["ps"] and any(x.startswith(f"volume={OLD_PG_TEST}") for x in argv):
            v["labels"]["com.docker.compose.volume"] = "boss_community_pg"

    docker.on_call = swap_after_listing
    out = steward.relabel_volumes(env, docker)
    assert out == [{"volume": OLD_PG_TEST, "action": "skipped", "reason": "no longer a rebuildable BOSS volume"}]
    assert docker.removed == []

    v["labels"]["com.docker.compose.volume"] = "boss_community_pg_test"

    def other_project(argv):
        if argv[1:2] == ["ps"] and any(x.startswith(f"volume={OLD_PG_TEST}") for x in argv):
            v["labels"]["com.docker.compose.project"] = "someone-else"

    docker.on_call = other_project
    assert steward.relabel_volumes(env, docker)[0]["action"] == "skipped"
    assert docker.removed == []


def test_relabel_volumes_from_the_command_line(env, docker, monkeypatch, capsys):
    """What upgrade.sh and `boss dev up` run; --dry-run says what would change and changes nothing."""
    docker.volumes.append(_compose_volume(OLD_PG_TEST, "boss_community_pg_test"))
    monkeypatch.setattr(steward, "run_cmd", docker)
    monkeypatch.setenv("BOSS_STEWARD_STATE", str(env.state_dir))   # never the laptop's ~/.boss
    argv = ["--install-dir", str(env.install_dir), "--backup-dir", str(env.backup_root), "--relabel-volumes"]
    assert steward.main([*argv, "--dry-run"]) == 0
    assert f"{OLD_PG_TEST}: would_relabel" in capsys.readouterr().out
    assert docker.removed == []
    assert steward.main(argv) == 0
    assert "relabelled" in capsys.readouterr().out
    assert docker.removed == [("volume", OLD_PG_TEST)]


def _real_backup(root, stamp: str, pid: int, *, install_id="inst_abc", age_days: float, size=4096,
                 complete=True):
    """A backup as backup.sh writes it: boss-<stamp>-<pid> with a complete manifest naming the install."""
    d = root / f"boss-{stamp}-{pid}"
    d.mkdir()
    (d / "boss.dump").write_bytes(b"x" * size)
    (d / "manifest.json").write_text(json.dumps({"format": "boss-backup/2", "complete": complete,
                                                 "install_id": install_id, "created_utc": stamp}))
    t = time.time() - age_days * 86400
    os.utime(d, (t, t))
    return d


def test_helper_label_gate_backups_need_a_manifest_naming_this_install(env, keypair, docker, tmp_path):
    priv, pub = keypair
    root = env.backup_root
    ours = [_real_backup(root, f"202610{i + 1:02d}T000000Z", 100 + i, age_days=30 - i) for i in range(12)]
    # things in the folder that are NOT ours, whatever the job says:
    operator_file = root / "boss-backup-x"                       # an operator's own file
    operator_file.write_bytes(b"mine" * 100)
    no_manifest = root / "boss-20260901T000000Z-7"               # BOSS-shaped name, no manifest
    no_manifest.mkdir(); (no_manifest / "boss.dump").write_bytes(b"y" * 10)
    foreign = _real_backup(root, "20260801T000000Z", 9, install_id="inst_other", age_days=90)
    incomplete = _real_backup(root, "20260701T000000Z", 8, age_days=120, complete=False)
    stranger = root / "my-photos"; stranger.mkdir(); (stranger / "a.jpg").write_bytes(b"z" * 5000)
    outside = tmp_path / "elsewhere"; outside.mkdir(); (outside / "manifest.json").write_text(
        json.dumps({"complete": True, "install_id": "inst_abc"}))
    os.symlink(outside, root / "boss-20260601T000000Z-5")       # a link out, with a matching manifest

    # sampling counts ours only
    sampled = steward.walk_backups(env)
    assert sorted(b["name"] for b in sampled) == sorted(d.name for d in ours)
    assert steward.collect(env, runner=docker)["backups_n"] == 12

    args = {"install_id": "inst_abc", "dry_run": True, "keep_newest": 10, "keep_days": 7, "cap_gb": 20}
    dry = steward.perform(env, _job(priv, env, "remove_backups", args), pub, runner=docker)
    assert dry["ok"]
    assert dry["items"] == [ours[0].name, ours[1].name], "keep the newest 10 of ours: the two oldest go"
    real = steward.perform(env, _job(priv, env, "remove_backups", {**args, "dry_run": False,
                                                                 "plan_hash": dry["plan_hash"]}), pub, runner=docker)
    assert real["ok"] and sorted(real["items"]) == sorted([ours[0].name, ours[1].name])
    assert not ours[0].exists() and not ours[1].exists() and ours[2].exists() and ours[11].exists()
    for untouched in (operator_file, no_manifest, foreign, incomplete, stranger, outside / "manifest.json"):
        assert untouched.exists(), untouched
    assert (root / "boss-20260601T000000Z-5").is_symlink()


def test_select_backups_cap_wins_but_never_below_the_floor():
    now = time.time()
    root = Path("/tmp/fake-root")
    backups = [{"path": f"/tmp/fake-root/boss-2026100{i}T000000Z-{i}", "name": f"b{i}", "bytes": 10e9,
                "mtime": now - i * 86400, "install_id": "inst_abc"} for i in range(1, 7)]   # 6 × 10 GB, 1..6 days old

    def pick(**kw):
        import types
        fake_paths = {b["path"] for b in backups}
        orig_resolve, orig_is_symlink = Path.resolve, Path.is_symlink
        Path.resolve = lambda self: self  # noqa: E731 — the tree is imaginary
        Path.is_symlink = lambda self: False  # noqa: E731
        try:
            return [b["name"] for b in steward.select_backups(backups, install_id="inst_abc", root=root, now=now, **kw)]
        finally:
            Path.resolve, Path.is_symlink = orig_resolve, orig_is_symlink

    # keep-N / keep-days: nothing is protected by age (all ≥ 1 day with keep_days=0.5) → keep the 3 newest
    assert pick(keep_newest=3, keep_days=0.5, cap_gb=1000) == ["b6", "b5", "b4"]
    # the cap wins over keep 5: the kept five hold 50 GB → the oldest kept go (b5, b4) until the
    # floor of 3 stops it at 30 GB, whatever the cap says
    assert pick(keep_newest=5, keep_days=0.5, cap_gb=45) == ["b6", "b5"]
    assert pick(keep_newest=5, keep_days=0.5, cap_gb=25) == ["b6", "b5", "b4"]
    assert pick(keep_newest=5, keep_days=0.5, cap_gb=1) == ["b6", "b5", "b4"], "never below CAP_FLOOR"
    # a foreign or unlabelled entry is never selected
    backups.append({"path": "/tmp/fake-root/boss-x", "name": "foreign", "bytes": 99e9, "mtime": now - 400 * 86400,
                    "install_id": "inst_other"})
    backups.append({"path": "/tmp/fake-root/boss-y", "name": "nolabel", "bytes": 99e9, "mtime": now - 400 * 86400,
                    "install_id": None})
    assert "foreign" not in pick(keep_newest=1, keep_days=0.5, cap_gb=1000)
    assert "nolabel" not in pick(keep_newest=1, keep_days=0.5, cap_gb=1000)


def test_an_item_that_appeared_since_the_dry_run_is_left_alone(env, keypair, docker):
    priv, pub = keypair
    root = env.backup_root
    ours = [_real_backup(root, f"2026100{i + 1}T000000Z", 10 + i, age_days=40 - i) for i in range(3)]
    args = {"install_id": "inst_abc", "dry_run": True, "keep_newest": 1, "keep_days": 7, "cap_gb": 20}
    dry = steward.perform(env, _job(priv, env, "remove_backups", args), pub, runner=docker)
    assert dry["items"] == [ours[0].name, ours[1].name]
    late = _real_backup(root, "20260901T000000Z", 99, age_days=50)
    real = steward.perform(env, _job(priv, env, "remove_backups", {**args, "dry_run": False, "plan_hash": dry["plan_hash"]}), pub, runner=docker)
    assert not real["ok"] and "changed since the dry run" in real["error"]
    assert late.exists() and ours[0].exists()


def test_build_cache_prune_is_scoped_to_boss(env, keypair, docker):
    priv, pub = keypair
    args = {"install_id": "inst_abc", "dry_run": True, "keep_storage_gb": 10}
    # a shared machine with no BOSS builder: refused, never host-wide
    env.buildx_builder, env.dedicated = "", False
    out = steward.perform(env, _job(priv, env, "prune_build_cache", args), pub, runner=docker)
    assert not out["ok"] and "Docker-wide" in out["error"]
    # a BOSS buildx builder: pruned through it only
    env.buildx_builder = "boss-builder"
    calls = []

    def runner(argv, timeout=120.0):
        calls.append(argv)
        return docker(argv, timeout)

    dry = steward.perform(env, _job(priv, env, "prune_build_cache", args), pub, runner=runner)
    assert dry["ok"] and dry["items"] == ["build-cache:builder:boss-builder:above-10GB"]
    real = steward.perform(env, _job(priv, env, "prune_build_cache", {**args, "dry_run": False, "plan_hash": dry["plan_hash"]}), pub, runner=runner)
    assert real["ok"]
    prunes = [c for c in calls if "prune" in c]
    assert prunes and all(c[1:3] == ["buildx", "prune"] and "--builder" in c and "boss-builder" in c for c in prunes)
    assert not any(c[1:3] == ["builder", "prune"] for c in calls), "never the host-wide prune on a shared machine"


def test_the_pinned_signing_key_is_trusted_on_first_use_and_a_change_is_refused(env, keypair, docker, tmp_path):
    priv, pub = keypair
    fetches = []

    def fetch():
        fetches.append(1)
        return pub

    first = steward.public_key(env, fetch)
    assert first["key_id"] == "k1" and len(fetches) == 1
    assert steward.pinned_key(env)["public_key"] == pub["public_key"]
    # a rotated key published by the API is NOT picked up by itself
    other = {**pub, "key_id": "k2", "public_key": base64.b64encode(b"\x01" * 32).decode()}
    again = steward.public_key(env, lambda: other)
    assert again["key_id"] == "k1", "the pin is the truth"
    job = _job(priv, env, "du", {}, key_id="k2")
    out = steward.perform(env, job, again, runner=docker)
    assert not out["ok"] and "rekey" in out["error"]
    # the operator confirms the rotation: rekey forgets the pin, the next run pins the new key
    assert steward.unpin_key(env) and steward.pinned_key(env) is None
    assert steward.public_key(env, lambda: other)["key_id"] == "k2"
    assert steward.main(["--install-dir", str(env.install_dir), "--backup-dir", str(env.backup_root), "--rekey"]) == 0


def test_expires_at_round_trips_through_json_and_timezones(env, keypair):
    """What the API signs (a datetime) and what the helper verifies (the JSON string) are the
    same bytes; Z and +00:00 spellings both parse to the same moment."""
    from boss_engine.services import host_jobs

    at = datetime(2026, 11, 14, 18, 2, 3, tzinfo=timezone.utc)
    api_bytes = host_jobs.canonical("j1", "h", "du", {"a": 1}, at)
    helper_bytes = steward.canonical("j1", "h", "du", {"a": 1}, json.loads(json.dumps(at.isoformat())))
    assert api_bytes == helper_bytes
    assert steward.parse_iso("2026-11-14T18:02:03Z") == steward.parse_iso("2026-11-14T18:02:03+00:00") == at
    assert steward.parse_iso("2026-11-14T18:02:03") == at, "a naive stamp is read as UTC"
    priv, pub = keypair
    job = _job(priv, env, "du", {})
    assert steward.check_job(env, job, pub, now=at - timedelta(days=1)) == {}


def test_pure_python_ed25519_verifier_agrees_with_cryptography(keypair):
    priv, pub = keypair
    payload = b"the bytes BOSS signed"
    sig = priv.sign(payload)
    pk = base64.b64decode(pub["public_key"])
    assert steward._ed25519_verify_pure(pk, payload, sig)
    assert not steward._ed25519_verify_pure(pk, payload + b"!", sig)
    assert not steward._ed25519_verify_pure(pk, payload, sig[:-1] + bytes([sig[-1] ^ 1]))


def test_the_helper_never_prints_the_host_key(env, capsys):
    """A refusal, a log line and the --print reading carry no credential."""
    report = {"host_id": env.host_id, "sample_error": "URLError: refused"}
    steward.log_line(env, report)
    text = env.log_path.read_text()
    assert "not-a-real-key" not in text
    assert "not-a-real-key" not in json.dumps(steward.collect(env, runner=lambda argv, timeout=0: subprocess.CompletedProcess(argv, 1, "", "")))


def test_a_fresh_install_gets_a_stable_id_and_backups_carry_it(tmp_path):
    """install.sh mints BOSS_INSTALL_ID; an archive update of an older install gets one from the
    reconciler, once; backup.sh hands it to the manifest; the steward prunes only backups that
    carry it."""
    import importlib.util as _iu
    import re

    spec = _iu.spec_from_file_location("_reconcile", ROOT / "scripts" / "install" / "reconcile_api_credentials.py")
    reconcile = _iu.module_from_spec(spec)
    spec.loader.exec_module(reconcile)
    env_file = tmp_path / ".env"
    env_file.write_text("BOSS_API_KEY=owner-key\nBOSS_WORKER_API_KEY=worker-key\n")
    reconcile.reconcile(env_file, ensure_worker=True)
    first = re.search(r"^BOSS_INSTALL_ID=(inst_[0-9a-f]{16})$", env_file.read_text(), re.M)
    assert first, env_file.read_text()
    reconcile.reconcile(env_file, ensure_worker=True)
    assert env_file.read_text().count("BOSS_INSTALL_ID=") == 1
    assert re.search(r"^BOSS_INSTALL_ID=(inst_[0-9a-f]{16})$", env_file.read_text(), re.M)[1] == first[1], "stable"
    install_sh = (ROOT / "install.sh").read_text()
    assert 'BOSS_INSTALL_ID="inst_$(openssl rand -hex 8)"' in install_sh
    assert '_existing BOSS_INSTALL_ID' in install_sh and "BOSS_INSTALL_ID=${BOSS_INSTALL_ID}" in install_sh
    assert "BOSS_INSTALL_ID: ${BOSS_INSTALL_ID:-}" in (ROOT / "scripts/install/community-compose.yml").read_text()
    backup_sh = (ROOT / "scripts/install/backup.sh").read_text()
    assert "^BOSS_INSTALL_ID=" in backup_sh and "install_id=" in backup_sh


def test_an_install_without_an_id_prunes_nothing_and_says_so(tmp_path, keypair, docker, monkeypatch, capsys):
    priv, pub = keypair
    install = tmp_path / "install"
    install.mkdir()
    (install / ".env").write_text("BOSS_HOST_API_KEY=not-a-real-key\nBOSS_DEDICATED_HOST=1\n")   # no BOSS_INSTALL_ID
    root = tmp_path / "backups"
    root.mkdir()
    for name in ("BOSS_INSTALL_ID", "BOSS_HOST_ID"):
        monkeypatch.delenv(name, raising=False)
    env = steward.Env(str(install), api_url="http://127.0.0.1:1", backup_root=str(root),
                      state_dir=str(tmp_path / "state"), docker=["fake-docker"])
    env.is_mac = False
    assert env.install_id == ""
    _real_backup(root, "20261001T000000Z", 1, install_id="", age_days=40)
    _real_backup(root, "20261002T000000Z", 2, install_id="inst_abc", age_days=40)
    assert steward.walk_backups(env) == [], "nothing can be ours without an id"
    # a job that also carries an empty id ("" == "") is refused, not matched
    for verb, args in (("remove_backups", {"install_id": "", "dry_run": True, "keep_newest": 0}),
                       ("remove_images", {"install_id": "", "dry_run": True}),
                       ("prune_build_cache", {"install_id": "", "dry_run": True})):
        out = steward.perform(env, _job(priv, env, verb, args), pub, runner=docker)
        assert not out["ok"] and "BOSS_INSTALL_ID" in out["error"], (verb, out)
    assert (root / "boss-20261001T000000Z-1").exists() and (root / "boss-20261002T000000Z-2").exists()
    assert docker.removed == []
    # the reading still goes out, with a loud warning on stderr and in the report
    report = steward.once(env, runner=docker, sample_only=True)
    assert "BOSS_INSTALL_ID" in report["warning"]
    assert "BOSS_INSTALL_ID" in capsys.readouterr().err


def test_release_rollback_label_can_only_remove_a_recorded_unused_previous_tag(env, monkeypatch):
    """Docker puts labels on image IDs. Other aliases of that ID belong to their owners."""
    prev = "prev-" + env.image_tag
    expected = "boss-community-api:" + prev
    busy = "boss-community-worker:" + prev
    entries = [expected, busy, "boss-community-web:" + prev, "private:archive",
               "boss-community-api:" + env.image_tag]
    (env.install_dir / ".boss-rollback").write_text(
        f"BOSS_IMAGE_TAG={env.image_tag}\nBOSS_PREV_IMAGES=boss-community-api boss-community-worker\n"
    )
    monkeypatch.setattr(steward, "rollback_expired", lambda *_: True)
    monkeypatch.setattr(steward, "docker_df", lambda *_: {})
    label_on_apply = env.install_id
    def run(argv, **_):
        cmd = argv[len(env.docker):]
        if cmd[0] == "ps":
            output = busy
        elif cmd[0] == "images" and "--filter" not in cmd:
            output = "\n".join(entries)
        elif cmd[0] == "images":
            output = ""
            if f"label={steward.ROLLBACK_LABEL}={env.install_id}" in cmd:
                output = "\n".join(json.dumps({"Repository": ref.split(":")[0], "Tag": ref.split(":")[1],
                                              "Size": "1MB"}) for ref in entries)
        elif cmd[:2] == ["image", "inspect"]:
            assert cmd[-1] == expected and steward.ROLLBACK_LABEL in cmd[-2]
            output = label_on_apply
        elif cmd[0] == "rmi":
            assert cmd[1:] == [expected]
            entries.remove(expected)
            output = ""
        else:
            raise AssertionError(cmd)
        return subprocess.CompletedProcess(argv, 0, output, "")
    assert steward.verb_remove_images(env, {}, dry_run=True, runner=run)[0] == [expected]
    label_on_apply = "different-install"
    assert not steward.verb_remove_images(env, {}, dry_run=False, runner=run)[0]
    label_on_apply = env.install_id
    assert steward.verb_remove_images(env, {}, dry_run=False, runner=run)[0] == [expected]
    assert busy in entries and "private:archive" in entries
    assert "BOSS_PREV_EXPIRED_AT" not in (env.install_dir / ".boss-rollback").read_text()


def test_compose_service_snapshot_excludes_oneoff_containers_and_distinguishes_unknown(env):
    def listed(argv, **_):
        assert f"label={steward.COMPOSE_PROJECT_LABEL}={env.compose_project}" in argv
        assert "label=com.docker.compose.oneoff=False" in argv
        return subprocess.CompletedProcess(argv, 0,
            "private-web-1|web|running|Up an hour\nprivate-api-1|api|running|Up 2 minutes (unhealthy)\n"
            "private-db-1|db|exited|Exited (1) 3 seconds ago\n", "")
    rows = steward.compose_service_states(env, listed)
    assert [(r["service"], r["state"]) for r in rows] == [
        ("api", "unhealthy"), ("db", "exited"), ("web", "running")]
    assert steward.compose_service_states(env, lambda *a, **kw: subprocess.CompletedProcess([], 1, "", "")) is None
    assert steward.compose_service_states(env, lambda *a, **kw: subprocess.CompletedProcess([], 0, "", "")) == []

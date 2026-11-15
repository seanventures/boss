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
    """`docker` as the steward drives it: images/volumes with labels; rm records what went."""

    def __init__(self, images, volumes):
        self.images, self.volumes, self.removed = images, volumes, []

    def __call__(self, argv, timeout=120.0):
        a = argv[1:]
        out = ""
        if a[:1] == ["images"]:
            label = next((x.split("=", 2)[2] for x in a if x.startswith("label=")), None)
            rows = [i for i in self.images if label is None or i["labels"].get("boss.install_id") == label]
            out = "\n".join(json.dumps({"Repository": i["repo"], "Tag": i["tag"], "ID": i["id"], "Size": i["size"]}) for i in rows)
        elif a[:2] == ["volume", "ls"]:
            label = next((x.split("=", 2)[2] for x in a if x.startswith("label=")), None)
            rows = [v for v in self.volumes if label is None or v["labels"].get("boss.install_id") == label]
            out = "\n".join(json.dumps({"Name": v["name"], "Size": "1GB"}) for v in rows)
        elif a[:2] == ["volume", "inspect"]:
            name = a[-1]
            v = next(v for v in self.volumes if v["name"] == name)
            out = v["labels"].get("boss.install_id", "") if "Labels" in a[3] else v["created"]
        elif a[:2] == ["image", "inspect"]:
            ref = a[-1]
            i = next((i for i in self.images if f"{i['repo']}:{i['tag']}" == ref or i["id"] == ref), None)
            out = (i or {"labels": {}})["labels"].get("boss.install_id", "")
        elif a[:1] == ["ps"]:
            out = "boss-community-api:v1"
        elif a[:2] == ["volume", "rm"]:
            self.removed.append(("volume", a[-1]))
        elif a[:1] == ["rmi"]:
            self.removed.append(("image", a[-1]))
        elif a[:2] == ["system", "df"]:
            out = "\n".join(json.dumps(r) for r in [
                {"Type": "Images", "Size": "30GB", "Reclaimable": "10GB (33%)"},
                {"Type": "Build Cache", "Size": "18GB", "Reclaimable": "18GB"},
                {"Type": "Local Volumes", "Size": "4GB", "Reclaimable": "4GB"},
                {"Type": "Containers", "Size": "600MB", "Reclaimable": "0B"}])
        return subprocess.CompletedProcess(argv, 0, out, "")


@pytest.fixture()
def docker():
    old = datetime(2026, 11, 1, tzinfo=timezone.utc).isoformat()
    return FakeDocker(
        images=[{"repo": "boss-community-api", "tag": "v1", "id": "sha1", "size": "5GB", "labels": {"boss.install_id": "inst_abc"}},
                {"repo": "boss-community-api", "tag": "prev-v1", "id": "sha2", "size": "5GB", "labels": {"boss.install_id": "inst_abc"}},
                {"repo": "boss-community-api", "tag": "v0", "id": "sha3", "size": "5GB", "labels": {"boss.install_id": "inst_abc"}},
                {"repo": "wordpress", "tag": "latest", "id": "sha4", "size": "1GB", "labels": {}},
                {"repo": "boss-community-api", "tag": "v-other", "id": "sha5", "size": "5GB", "labels": {"boss.install_id": "inst_other"}}],
        volumes=[{"name": "boss_tmp_1", "labels": {"boss.install_id": "inst_abc"}, "created": old},
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

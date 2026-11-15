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
                                  "BOSS_COMMUNITY_IMAGE_TAG=v1\n")
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


def test_helper_label_gate_backups_stay_under_root_and_named(env, keypair, docker, tmp_path):
    priv, pub = keypair
    root = env.backup_root
    now = time.time()
    # twelve old backups of ours, one of another install, one stranger's folder, one symlink out
    for i in range(12):
        d = root / f"boss-backup-2026-10-{i + 1:02d}"
        d.mkdir()
        (d / "boss.dump").write_bytes(b"x" * 1000)
        os.utime(d, (now - (30 - i) * 86400, now - (30 - i) * 86400))
    foreign = root / "boss-backup-foreign"
    foreign.mkdir()
    (foreign / "manifest.json").write_text(json.dumps({"install_id": "inst_other"}))
    os.utime(foreign, (now - 60 * 86400, now - 60 * 86400))
    stranger = root / "my-photos"
    stranger.mkdir()
    (stranger / "a.jpg").write_bytes(b"y" * 5000)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "boss.dump").write_bytes(b"z" * 1000)
    os.symlink(outside, root / "boss-backup-link")
    os.utime(root / "boss-backup-link", (now - 90 * 86400, now - 90 * 86400), follow_symlinks=False)

    args = {"install_id": "inst_abc", "dry_run": True, "keep_newest": 10, "keep_days": 7, "cap_gb": 20}
    dry = steward.perform(env, _job(priv, env, "remove_backups", args), pub, runner=docker)
    assert dry["ok"]
    # keep the newest 10 of ours → the two oldest go; never the foreign, the stranger, the link
    assert dry["items"] == ["boss-backup-2026-10-01", "boss-backup-2026-10-02"]
    real = steward.perform(env, _job(priv, env, "remove_backups", {**args, "dry_run": False, "plan_hash": dry["plan_hash"]}), pub, runner=docker)
    assert real["ok"] and sorted(real["items"]) == ["boss-backup-2026-10-01", "boss-backup-2026-10-02"]
    assert not (root / "boss-backup-2026-10-01").exists()
    assert (root / "boss-backup-2026-10-03").exists() and (root / "boss-backup-2026-10-12").exists()
    assert foreign.exists() and stranger.exists() and (outside / "boss.dump").exists()
    assert (root / "boss-backup-link").is_symlink()


def test_an_item_that_appeared_since_the_dry_run_is_left_alone(env, keypair, docker):
    priv, pub = keypair
    root = env.backup_root
    now = time.time()
    for i in range(3):
        d = root / f"boss-backup-{i}"
        d.mkdir()
        (d / "boss.dump").write_bytes(b"x")
        os.utime(d, (now - (40 - i) * 86400, now - (40 - i) * 86400))
    args = {"install_id": "inst_abc", "dry_run": True, "keep_newest": 1, "keep_days": 7, "cap_gb": 20}
    dry = steward.perform(env, _job(priv, env, "remove_backups", args), pub, runner=docker)
    assert dry["items"] == ["boss-backup-0", "boss-backup-1"]
    late = root / "boss-backup-late-but-old"
    late.mkdir()
    os.utime(late, (now - 50 * 86400, now - 50 * 86400))
    real = steward.perform(env, _job(priv, env, "remove_backups", {**args, "dry_run": False, "plan_hash": dry["plan_hash"]}), pub, runner=docker)
    assert not real["ok"] and "changed since the dry run" in real["error"]
    assert late.exists() and (root / "boss-backup-0").exists()


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

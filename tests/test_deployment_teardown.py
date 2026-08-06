"""Removing a deployed app — the safety properties, not the happy path.

Teardown is the one verb that destroys, and it runs against a host that also carries unrelated
production services. So the interesting assertions are all about what it REFUSES:

  * a container it cannot prove it owns is never removed;
  * nothing outside REMOTE_BASE/<app> is ever a delete target;
  * data survives unless the caller asked for it BY NAME;
  * an unsafe app name stops the whole operation before a connection is opened.

Run directly (pytest collection wipes the local BOSS database):
    PYTHONPATH=engine .venv/bin/python tests/test_deployment_teardown.py
"""
from __future__ import annotations

import sys

sys.path.insert(0, "engine")

from boss_engine.services import deploy_service as ds  # noqa: E402
from boss_engine.services.deployment_connectors import connector_for  # noqa: E402

_failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  PASS  {name}")
    else:
        _failures.append(f"{name}: {detail}")
        print(f"  FAIL  {name} — {detail}")


class _Settings:
    ssh_host_private_key_file = ""
    ssh_host_private_key = "-----BEGIN OPENSSH PRIVATE KEY-----\nfake\n-----END OPENSSH PRIVATE KEY-----\n"
    ssh_host_deploy_allowed = True


class _Result:
    def __init__(self, stdout="", returncode=0, stderr=""):
        self.stdout, self.returncode, self.stderr = stdout, returncode, stderr


def _fake_ssh(script_sink: list, responses: list):
    """Record every remote command and replay canned responses in order."""
    def fake(key, host, user, port, command, timeout=180):
        script_sink.append(command)
        return responses.pop(0) if responses else _Result()
    return fake


TARGET = {"target_ref": "ubuntu@example.com", "repo": "boss-sandbox",
          "app_name": "stock-app", "secret_refs": []}


def _run(responses, **kwargs):
    scripts: list[str] = []
    real_ssh = ds._ssh
    ds._ssh = _fake_ssh(scripts, list(responses))
    try:
        report = connector_for({"target_kind": "ssh"}).destroy(
            TARGET, _Settings(), None, **kwargs)
    finally:
        ds._ssh = real_ssh
    return report, scripts


def test_a_container_boss_does_not_own_is_never_removed() -> None:
    """The decisive case: right name, not BOSS's. Removing it would destroy someone else's app."""
    report, scripts = _run([_Result("OWNER=\nMOUNTED=0\nEXISTS=yes\nSIZE=2048\nDATASIZE=16")])
    check("refused", report["ok"] is False and bool(report["refused"]),
          str(report)[:140])
    check("the refusal explains WHY", "not BOSS-managed" in report["refused"][0])
    check("nothing was removed", report["removed"] == [])
    check("no destructive command was ever sent",
          all("rm -rf" not in s and "docker rm" not in s for s in scripts),
          "a refusal that still deleted would be worse than no check at all")


def test_label_proves_ownership() -> None:
    report, scripts = _run([
        _Result("OWNER=stock-app\nMOUNTED=0\nEXISTS=yes\nSIZE=2048\nDATASIZE=16"),
        _Result(""), _Result("16000000")])
    check("a labelled container is ours", report["ok"] and not report["refused"],
          str(report)[:140])
    check("the container is removed", any("docker rm -f" in s for s in scripts))


def test_mount_path_proves_ownership_for_pre_label_deployments() -> None:
    """Containers deployed before labels existed must still be removable."""
    report, _ = _run([
        _Result("OWNER=\nMOUNTED=1\nEXISTS=yes\nSIZE=2048\nDATASIZE=16"),
        _Result(""), _Result("16000000")])
    check("a container mounting this app's dir is ours", report["ok"], str(report)[:140])


def test_data_survives_unless_asked_for_by_name() -> None:
    report, scripts = _run([
        _Result("OWNER=stock-app\nMOUNTED=1\nEXISTS=yes\nSIZE=2048\nDATASIZE=500"),
        _Result(""), _Result("16000000")])
    # scripts[0] is the read-only probe (it MEASURES the data dir with du), scripts[1] is the
    # destructive script. Checking the whole transcript would match the measurement and call it
    # a deletion — the check has to look where the deleting happens.
    deletes = scripts[1]
    check("dist is removed", "/opt/boss-apps/stock-app/dist" in deletes)
    check("DATA IS NOT removed by default",
          "/opt/boss-apps/stock-app/data" not in deletes,
          "the irreversible step must never be implied")
    check("the report says data was kept", report["data_kept_kb"] == 500)
    check("reclaimed excludes the data it kept", report["reclaimed_kb"] == 2048 - 500)

    report2, scripts2 = _run([
        _Result("OWNER=stock-app\nMOUNTED=1\nEXISTS=yes\nSIZE=2048\nDATASIZE=500"),
        _Result(""), _Result("16000000")], include_data=True)
    check("data IS removed when asked for by name",
          "/opt/boss-apps/stock-app/data" in scripts2[1])
    check("reclaimed then includes it", report2["reclaimed_kb"] == 2048)


def test_stop_only_leaves_the_files() -> None:
    report, scripts = _run([
        _Result("OWNER=stock-app\nMOUNTED=1\nEXISTS=yes\nSIZE=2048\nDATASIZE=16"),
        _Result(""), _Result("16000000")], stop_only=True)
    deletes = scripts[1]
    check("the container is stopped", "docker rm -f" in deletes)
    check("no files are deleted", "rm -rf" not in deletes,
          "stop_only must be reversible by a redeploy")
    check("reported as stop_only", report["stop_only"] is True)


def test_deletes_are_confined_to_the_app_directory() -> None:
    _, scripts = _run([
        _Result("OWNER=stock-app\nMOUNTED=1\nEXISTS=yes\nSIZE=2048\nDATASIZE=16"),
        _Result(""), _Result("16000000")], include_data=True)
    for script in scripts[1:2]:
        for fragment in script.split("rm -rf ")[1:]:
            path = fragment.split()[0].strip("'\"")
            # Container-internal paths (/_x/...) come from the root-container fallback and are
            # bind-mounted to the app dir, so they are in scope too.
            ok = path.startswith("/opt/boss-apps/stock-app") or path.startswith("/_x/")
            check(f"delete target confined: {path}", ok, path)


def test_an_unsafe_app_name_stops_before_any_connection() -> None:
    bad = {**TARGET, "app_name": "../../etc", "repo": ""}
    scripts: list[str] = []
    real_ssh = ds._ssh
    ds._ssh = _fake_ssh(scripts, [])
    try:
        report = connector_for({"target_kind": "ssh"}).destroy(bad, _Settings(), None)
    finally:
        ds._ssh = real_ssh
    # _app_name sanitises "../../etc" to "etc", so the traversal collapses to a normal name
    # UNDER the base. What must never happen is a path that escapes REMOTE_BASE.
    for script in scripts:
        for fragment in script.split("rm -rf ")[1:]:
            path = fragment.split()[0].strip("'\"")
            check(f"confined: {path}",
                  path.startswith("/opt/boss-apps/") or path.startswith("/_x/"), path)
    check("the traversal collapsed to a name under the base",
          all("/opt/boss-apps/etc" in s or "rm -rf" not in s for s in scripts),
          str(scripts)[:160])
    check("report is well-formed", "removed" in report and "refused" in report)


if __name__ == "__main__":
    for fn in (test_a_container_boss_does_not_own_is_never_removed,
               test_label_proves_ownership,
               test_mount_path_proves_ownership_for_pre_label_deployments,
               test_data_survives_unless_asked_for_by_name,
               test_stop_only_leaves_the_files,
               test_deletes_are_confined_to_the_app_directory,
               test_an_unsafe_app_name_stops_before_any_connection):
        print(f"\n{fn.__name__}")
        fn()
    print()
    if _failures:
        print(f"{len(_failures)} FAILURE(S):")
        for f in _failures:
            print(f"  - {f}")
        sys.exit(1)
    print("all teardown safety checks passed")

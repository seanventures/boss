"""Per-target credentials, and the promise that BOSS never keeps the material.

`deployment_targets.secret_refs` was populated on every live target and read by NOTHING —
`_ssh_key_file()` consulted one global setting, so every SSH target necessarily shared one key
and onboarding a second host meant editing `.env` and restarting the API. A binding recorded
per target and ignored per deploy is not a binding.

These tests pin the two properties that make per-target credentials real:
  1. a target's OWN binding is what gets used, and two targets resolve differently;
  2. nothing that resolves a secret ever writes it anywhere BOSS keeps.

Run directly (pytest collection wipes the local BOSS database):
    PYTHONPATH=engine .venv/bin/python tests/test_secret_backends.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, "engine")

from boss_engine.services import secret_backends as sb  # noqa: E402
from boss_engine.services.deployment_connectors import connector_for  # noqa: E402

_failures: list[str] = []
KEY_A = "-----BEGIN OPENSSH PRIVATE KEY-----\nAAAA-key-for-host-A\n-----END OPENSSH PRIVATE KEY-----\n"
KEY_B = "-----BEGIN OPENSSH PRIVATE KEY-----\nBBBB-key-for-host-B\n-----END OPENSSH PRIVATE KEY-----\n"


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  PASS  {name}")
    else:
        _failures.append(f"{name}: {detail}")
        print(f"  FAIL  {name} — {detail}")


class _Settings:
    ssh_host_private_key_file = ""
    ssh_host_private_key = ""
    ssh_host_deploy_allowed = True
    platform_secret_store_url = ""
    platform_secret_store_token = ""
    platform_secret_store_token_file = ""


class _FakeConn:
    """Stands in for a DB connection carrying secret_refs rows."""

    def __init__(self, rows: dict):
        self._rows = rows

    def execute(self, _stmt, params):
        row = self._rows.get(params.get("i"))
        class R:
            def mappings(self_inner):
                class M:
                    def first(self_m):
                        return row
                return M()
        return R()


def test_two_targets_resolve_different_keys() -> None:
    """The whole point: host A and host B can have their own credentials."""
    with tempfile.TemporaryDirectory() as tmp:
        pa, pb = Path(tmp) / "a.pem", Path(tmp) / "b.pem"
        for p, k in ((pa, KEY_A), (pb, KEY_B)):
            p.write_text(k)
            p.chmod(0o600)

        conn = _FakeConn({
            "sref_a": {"ref_kind": "file", "ref_key": str(pa),
                       "storage_backend": "file", "status": "active"},
            "sref_b": {"ref_kind": "file", "ref_key": str(pb),
                       "storage_backend": "file", "status": "active"},
        })
        ta = {"secret_refs": [{"name": "ssh_key", "secret_ref_id": "sref_a"}]}
        tb = {"secret_refs": [{"name": "ssh_key", "secret_ref_id": "sref_b"}]}
        s = _Settings()

        got_a = sb.resolve_target_secret(conn, ta, "ssh_key", s)
        got_b = sb.resolve_target_secret(conn, tb, "ssh_key", s)
        check("target A resolves its own key", got_a == KEY_A)
        check("target B resolves its own key", got_b == KEY_B)
        check("two targets do NOT share one credential", got_a != got_b,
              "this is exactly what forced a single global key before")


def test_unbound_target_falls_back_so_live_deploys_keep_working() -> None:
    conn = _FakeConn({})
    check("no binding resolves to None, not an error",
          sb.resolve_target_secret(conn, {"secret_refs": []}, "ssh_key", _Settings()) is None,
          "None is what lets an un-migrated target fall back to the global setting")


def test_api_string_binding_resolves_secret_ref() -> None:
    """POST /v1/deployments stores secret_refs as list[str] — that shape must resolve.

    Phase 1's first resolver only understood dicts, so an API-onboarded target silently
    fell through to the global key (wrong-secret for every second host).
    """
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "a.pem"
        p.write_text(KEY_A)
        p.chmod(0o600)
        conn = _FakeConn({"sref_a": {"ref_kind": "file", "ref_key": str(p),
                                     "storage_backend": "file", "status": "active"}})
        got = sb.resolve_target_secret(
            conn, {"secret_refs": ["sref_a"]}, "ssh_key", _Settings())
        check("bare sref string resolves", got == KEY_A)


def test_bound_but_unusable_ref_does_not_silently_use_global() -> None:
    """A declared binding that cannot resolve must raise, not return None (global fallback)."""
    target = {"secret_refs": [{"name": "ssh_key", "ref": "BOSS_MISSING_KEY_FILE_XYZ"}]}
    try:
        sb.resolve_target_secret(_FakeConn({}), target, "ssh_key", _Settings())
        check("unset legacy ref refuses global fallback", False, "returned instead of raising")
    except sb.SecretUnavailable as exc:
        check("unset legacy ref refuses global fallback", "refusing" in str(exc).lower()
              or "unset" in str(exc).lower(), str(exc)[:120])


def test_revoked_secret_is_refused() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "k.pem"
        p.write_text(KEY_A)
        p.chmod(0o600)
        conn = _FakeConn({"sref_x": {"ref_kind": "file", "ref_key": str(p),
                                     "storage_backend": "file", "status": "revoked"}})
        target = {"secret_refs": [{"name": "ssh_key", "secret_ref_id": "sref_x"}]}
        try:
            sb.resolve_target_secret(conn, target, "ssh_key", _Settings())
            check("a revoked secret is refused", False, "it resolved anyway")
        except sb.SecretUnavailable as exc:
            check("a revoked secret is refused", "revoked" in str(exc))


def test_world_readable_key_is_refused() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "loose.pem"
        p.write_text(KEY_A)
        p.chmod(0o644)
        try:
            sb.FileBackend().get(str(p))
            check("a world-readable key is refused", False, "it was accepted")
        except sb.SecretUnavailable as exc:
            check("a world-readable key is refused", "readable" in str(exc))


def test_errors_never_carry_secret_material() -> None:
    """An exception is the easiest place for a secret to escape into a log."""
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "k.pem"
        p.write_text(KEY_A)
        p.chmod(0o600)
        conn = _FakeConn({"sref_x": {"ref_kind": "file", "ref_key": str(p),
                                     "storage_backend": "file", "status": "revoked"}})
        try:
            sb.resolve_secret_ref(conn, "sref_x", _Settings())
            msg = ""
        except sb.SecretUnavailable as exc:
            msg = str(exc)
        check("the error names the ref, not the key", "AAAA-key" not in msg, msg[:80])

        missing = ""
        try:
            sb.resolve_secret_ref(_FakeConn({}), "sref_nope", _Settings())
        except sb.SecretUnavailable as exc:
            missing = str(exc)
        check("a missing ref says so plainly", "does not exist" in missing)


def test_connector_declares_what_it_needs() -> None:
    conn = connector_for({"target_kind": "ssh"})
    reqs = conn.required_secrets()
    check("ssh declares exactly one required secret", len(reqs) == 1)
    check("it is an ssh private key",
          reqs[0]["name"] == "ssh_key" and reqs[0]["kind"] == "ssh_private_key")
    check("the requirement explains itself", bool(reqs[0].get("purpose")),
          "an operator reading the onboarding form needs to know why it is asked for")


def test_validate_reports_an_unusable_binding_instead_of_crashing() -> None:
    """A broken credential must be a preflight problem, not a mid-transfer exception."""
    conn = _FakeConn({"sref_gone": {"ref_kind": "file", "ref_key": "/nope/missing.pem",
                                    "storage_backend": "file", "status": "active"}})
    target = {"target_ref": "ubuntu@example.com",
              "domains": ["example.com"],
              "health_checks": [{"kind": "http", "path": "/", "expect": 200}],
              "secret_refs": [{"name": "ssh_key", "secret_ref_id": "sref_gone"}]}
    problems = connector_for({"target_kind": "ssh"})._credential_problems(
        target, _Settings(), conn)
    check("an unresolvable binding is reported", bool(problems), "no problem was raised")
    check("the message says which secret", any("ssh_key" in p for p in problems),
          str(problems)[:120])
    check("the message carries no key material",
          all("BEGIN OPENSSH" not in p for p in problems))


def test_materialised_key_is_owned_and_cleaned_up() -> None:
    """Resolved material is written to a temp file BOSS owns — and therefore may delete."""
    from boss_engine.services import deploy_service as ds

    path = ds._materialise_key(KEY_A)
    try:
        check("written 0600", (os.stat(path).st_mode & 0o077) == 0)
        check("registered as BOSS-owned", path in ds._EPHEMERAL_KEY_FILES,
              "otherwise cleanup would leave key material on disk")
        ds._discard_key_file(path)
        check("cleanup removes it", not os.path.exists(path))
    finally:
        if os.path.exists(path):
            os.unlink(path)


def test_a_named_secret_never_lands_in_the_working_directory() -> None:
    """A generated key once landed in the git repository. Never again.

    `ref_key_for()` produces an env-var-style NAME. The file backend passed it straight to
    `open()`, so the key was written relative to the process's cwd — for the API, the repo. A
    private key one `git add -A` from being committed is a real incident, not a tidiness issue.
    """
    resolved = sb.FileBackend()._resolve("BOSS_DEPTGT_X_SSH_KEY")
    check("a bare name resolves under the secrets dir",
          resolved.startswith(sb.SECRETS_DIR), resolved)
    check("a bare name NEVER resolves into the working directory",
          not resolved.startswith(os.getcwd()), resolved)
    check("an absolute path is still honoured",
          sb.FileBackend()._resolve("/tmp/operator.pem") == "/tmp/operator.pem")
    check("a traversal attempt cannot escape the secrets dir",
          sb.FileBackend()._resolve("../../etc/passwd").startswith(sb.SECRETS_DIR),
          sb.FileBackend()._resolve("../../etc/passwd"))


def test_generated_private_key_is_not_left_on_the_boss_host() -> None:
    """generate_keypair writes to a backend and must leave nothing behind in its temp dir."""
    import glob

    leftovers = glob.glob("/tmp/boss-keygen-*") + glob.glob(
        os.path.join(tempfile.gettempdir(), "boss-keygen-*"))
    check("no keygen scratch directories survive", not leftovers, str(leftovers[:3]))


if __name__ == "__main__":
    for fn in (test_two_targets_resolve_different_keys,
               test_a_named_secret_never_lands_in_the_working_directory,
               test_generated_private_key_is_not_left_on_the_boss_host,
               test_unbound_target_falls_back_so_live_deploys_keep_working,
               test_api_string_binding_resolves_secret_ref,
               test_bound_but_unusable_ref_does_not_silently_use_global,
               test_revoked_secret_is_refused,
               test_world_readable_key_is_refused,
               test_errors_never_carry_secret_material,
               test_connector_declares_what_it_needs,
               test_validate_reports_an_unusable_binding_instead_of_crashing,
               test_materialised_key_is_owned_and_cleaned_up):
        print(f"\n{fn.__name__}")
        fn()
    print()
    if _failures:
        print(f"{len(_failures)} FAILURE(S):")
        for f in _failures:
            print(f"  - {f}")
        sys.exit(1)
    print("all secret-backend checks passed")

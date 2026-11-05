"""The fleet: a VM as a first-class object, and the four steps that make one deployable.

Design: docs/product/DEPLOYMENT_FLEET_AND_APPROVAL_FLOW.md

Before this, a host existed only as a string inside each deployment_target's `target_ref`
("root@192.0.2.202:22"). Ten apps on one box meant ten copies of that string, ten secret
bindings to the SAME key, no way to ask "what runs here?", and a key rotation that meant
editing ten rows. Credentials, architecture and a port budget belong to the machine, not to
each app that happens to sit on it.

`target_ref` is NOT replaced. It is written FROM the host so every existing connector,
`_split_ref` call and test keeps working untouched — host_id is the source of truth, and
target_ref is its projection. One writable copy of the address; the other is derived.

The onboarding path is deliberately four steps, and step 3 is deliberately human:

    register → generate key → INSTALL (operator pastes one line) → verify

BOSS cannot install the first key without already having access, and it must not ask for,
hold, or type a host password to get there. So it hands over an exact command and then proves
the result with a real connection. That is the honest shape of this problem.
"""
from __future__ import annotations

import json
import re
import shlex
import uuid
from typing import Any, Optional

from ..config import Settings
from ..ports import sql
from .core import emit_event

# Two stock-image conditions that both present as "the key does not work" while key auth has
# in fact succeeded. Diagnosed the slow way on the Hetzner fleet; encoded here so nobody has to
# diagnose them twice.
EXPIRED_PASSWORD_MARKERS = (
    "password change required",
    "your password has expired",
    "you are required to change your password",
)


_DIRECT_HOST_WRITE_EDITIONS = frozenset({
    "enterprise", "enterprise_cloud", "enterprise_self_hosted", "cloud",
})
_COMMUNITY_HOST_WRITE_DENIAL = (
    "Direct deployment-host changes are disabled in Community until they use an "
    "approved Resource Plane ChangeSet, budget reservation, and ResourceReceipt."
)


def outward_ssh_allowed(settings: Settings) -> bool:
    """May BOSS open an SSH session at all — to VERIFY a host, or to apply a GOVERNED change?

    Two different questions used to share one switch. BOSS_SSH_HOST_DEPLOY_ALLOWED gates the
    legacy direct writes (key install, docker provisioning, retire, the per-app container
    deploy) and defaults off, so a fresh Community install could not even run `echo` on the VM
    it had just been handed: the Infra panel hid "Add a VM" behind a note telling the operator
    to edit the BOSS host's environment. A read-only probe and a ChangeSet-approved stack
    deployment carry their own governance, so they need only this, which defaults on.
    """
    return (bool(getattr(settings, "ssh_outward_allowed", True))
            or bool(getattr(settings, "ssh_host_deploy_allowed", False)))


def direct_host_writes_allowed(settings: Settings) -> bool:
    """Unknown editions and a disabled outward-SSH switch fail closed."""
    edition = str(getattr(settings, "boss_edition", "community")).strip().lower()
    return (edition in _DIRECT_HOST_WRITE_EDITIONS
            and bool(getattr(settings, "ssh_host_deploy_allowed", False)))


def require_direct_host_write_policy(settings: Settings) -> None:
    """Guard the service itself so callers cannot skip the HTTP route's check."""
    edition = str(getattr(settings, "boss_edition", "community")).strip().lower()
    if edition not in _DIRECT_HOST_WRITE_EDITIONS:
        raise PermissionError(_COMMUNITY_HOST_WRITE_DENIAL)
    if not bool(getattr(settings, "ssh_host_deploy_allowed", False)):
        raise PermissionError(
            "BOSS_SSH_HOST_DEPLOY_ALLOWED is false — direct host changes are disabled."
        )


def _new_id() -> str:
    return f"dh_{uuid.uuid4().hex[:12]}"


def target_ref_for(host: dict) -> str:
    """`user@address:port` — the projection every SSH connector already understands."""
    user = (host.get("ssh_user") or "root").strip()
    port = int(host.get("ssh_port") or 22)
    return f"{user}@{host['address']}:{port}"


# The shell BOSS asks an operator to run, in the four situations a real onboarding hits.
# Written once because the alternative is four drifting copies of an authorized_keys line.
_APPEND = ("mkdir -p ~/.ssh && chmod 700 ~/.ssh && "
           "printf '%s\\n' '{public}' >> ~/.ssh/authorized_keys && "
           "chmod 600 ~/.ssh/authorized_keys && echo BOSS_KEY_INSTALLED")


def install_command_for(host: dict, public_key: Optional[str] = None) -> Optional[str]:
    """The push command for THIS host, with the operator's own identity if one is recorded.

    `admin_identity_file` exists because a laptop with no default SSH identity fails this push
    with "Permission denied (publickey)" — which is indistinguishable from BOSS's key not being
    installed, and sends people to debug the host instead of their own agent. BOSS cannot
    discover the path, so it asks once and then renders it.
    """
    public = (public_key or host.get("key_public") or "").strip()
    if not public:
        return None
    user = (host.get("ssh_user") or "root").strip()
    port = int(host.get("ssh_port") or 22)
    identity = (host.get("admin_identity_file") or "").strip()
    ident = f"-i {shlex.quote(identity)} " if identity else ""
    return (f"ssh {ident}-o StrictHostKeyChecking=accept-new -p {port} "
            f"{user}@{host['address']} \"{_APPEND.format(public=public)}\"")


def install_variants_for(host: dict, public_key: Optional[str] = None) -> dict[str, Any]:
    """Every honest way to get BOSS's key onto this host — one per situation a customer is in.

    Onboarding does not fail because the customer is careless; it fails because the tool
    assumed a situation they are not in. The four that actually occur:

      from_laptop   they can already SSH to the box
      on_host       they can only reach a provider web console (no SSH from anywhere)
      cloud_init    the machine does not exist yet — authorise at creation, zero manual steps
      boss_installs BOSS already holds a working credential and needs no human at all
    """
    public = (public_key or host.get("key_public") or "").strip()
    if not public:
        return {}
    user = (host.get("ssh_user") or "root").strip()
    return {
        "from_laptop": install_command_for(host, public),
        # Pasted into a provider console, where there is no ssh to wrap it in.
        "on_host": _APPEND.format(public=public),
        # The best answer for a host that does not exist yet: no manual step ever happens.
        "cloud_init": ("#cloud-config\n"
                       "users:\n"
                       f"  - name: {user}\n"
                       "    ssh_authorized_keys:\n"
                       f"      - {public}\n"
                       "packages:\n"
                       "  - docker.io\n"),
        "boss_installs": bool(host.get("key_secret_ref")),
    }


def register_host(connection, *, project_id: str, name: str, address: str,
                  ssh_user: str = "root", ssh_port: int = 22, arch: Optional[str] = None,
                  labels: Optional[dict] = None, port_min: int = 8000, port_max: int = 8999,
                  max_apps: Optional[int] = None, notes: Optional[str] = None,
                  admin_identity_file: Optional[str] = None,
                  actor: str = "operator") -> dict[str, Any]:
    """Record a VM. Status `registered` — known, but not yet proven reachable.

    Registering does not touch the machine. Nothing may be scheduled here until `verify_host`
    has actually connected, so a typo in the address surfaces as "not verified" rather than as
    a failed deployment later.
    """
    host_id = _new_id()
    connection.execute(
        sql("INSERT INTO deployment_hosts "
            "(id, project_id, name, address, ssh_user, ssh_port, arch, labels, "
            " port_min, port_max, max_apps, notes, admin_identity_file, created_by) "
            "VALUES (:i, :p, :n, :a, :u, :sp, :ar, CAST(:l AS JSONB), "
            "        :pmin, :pmax, :ma, :no, :aif, :by)"),
        {"i": host_id, "p": project_id, "n": name, "a": address, "u": ssh_user,
         "sp": int(ssh_port), "ar": arch, "l": json.dumps(labels or {}),
         "pmin": int(port_min), "pmax": int(port_max), "ma": max_apps, "no": notes,
         "aif": (admin_identity_file or None), "by": actor})
    return get_host(connection, host_id)


def get_host(connection, host_id: str) -> Optional[dict[str, Any]]:
    row = connection.execute(
        sql("SELECT * FROM deployment_hosts WHERE id = :i"), {"i": host_id}
    ).mappings().first()
    return dict(row) if row else None


#: SHARED HOSTS (2026-11-05). A machine one project registered and verified is the ORGANISATION's:
#: every project of the same org may deploy onto it, as every project may use the org's repos.
#: Imported HelpDesk AI could not deploy anywhere because 2.29.2.202 had been registered under
#: another project; BOSS's own agent found "attach refused across projects" and had no way on.
#: Managing the machine (its key, verify, retire) stays with the project that registered it.
#: A SQL predicate over deployment_hosts (alias optional) with the project bound as `:p`.
def usable_hosts_sql(alias: str = "") -> str:
    a = f"{alias}." if alias else ""
    return (f"({a}project_id = :p OR {a}project_id IN (SELECT p2.id FROM projects p2 "
            "JOIN workspaces w2 ON w2.id = p2.workspace_id WHERE w2.org_id = "
            "(SELECT w1.org_id FROM projects p1 JOIN workspaces w1 ON w1.id = p1.workspace_id "
            "WHERE p1.id = :p)))")


def host_usable_by(connection, host_id: str, project_id: str) -> bool:
    """May `project_id` deploy onto this host (its own, or one its organisation shares)?"""
    return bool(connection.execute(
        sql(f"SELECT 1 FROM deployment_hosts WHERE id = :h AND {usable_hosts_sql()}"),
        {"h": host_id, "p": project_id}).first())


def list_hosts(connection, project_id: str, *, include_retired: bool = False
               ) -> list[dict[str, Any]]:
    """Hosts with the apps riding on each — the map, read from the VM side.

    The app-side view (`deployment_targets`) already existed. This is the direction that was
    missing, and it is the one an operator asks for first: what is on this box?
    """
    clause = "" if include_retired else " AND h.status <> 'retired'"
    rows = connection.execute(
        sql("SELECT h.*, "
            "  COALESCE(t.app_count, 0) AS app_count, "
            "  COALESCE(t.apps, '[]'::jsonb) AS apps "
            "FROM deployment_hosts h "
            "LEFT JOIN ( "
            "  SELECT host_id, COUNT(*) AS app_count, "
            "         jsonb_agg(jsonb_build_object('app_name', app_name, "
            "                                      'environment', environment, "
            "                                      'target_id', id) "
            "                   ORDER BY app_name) AS apps "
            "  FROM deployment_targets WHERE host_id IS NOT NULL AND status = 'active' "
            "  GROUP BY host_id "
            ") t ON t.host_id = h.id "
            f"WHERE {usable_hosts_sql('h')}{clause} "
            "ORDER BY h.created_at DESC"),
        {"p": project_id}).mappings().all()
    hosts = []
    for row in rows:
        host = dict(row)
        # Re-derivable, never re-minted. This is what makes "I lost the command" a non-event
        # instead of a rotation.
        host["install_command"] = install_command_for(host)
        host["install_variants"] = install_variants_for(host)
        hosts.append(host)
    return hosts


def generate_host_key(connection, settings: Settings, *, host_id: str,
                      backend: str = "platform_secret_store",
                      actor: str = "operator") -> dict[str, Any]:
    """Mint this host's keypair, keep the private half, hand back the install line.

    ONE key per host — the whole point of the table. Rotation therefore never touches the apps
    riding on the box: generate, install alongside, verify, drop the old.

    `backend` is a parameter, not a constant. It was hardcoded to the platform secret store,
    which a fully-local run deliberately leaves unconfigured — so the first real attempt died
    inside `backend_for` and surfaced as a bare HTTP 500 with nothing to act on. The caller
    chooses, exactly as `/v1/secrets/keypair` already lets it.
    """
    from .secrets_service import generate_keypair

    host = get_host(connection, host_id)
    if not host:
        raise ValueError(f"no such deployment host: {host_id}")

    minted = generate_keypair(
        connection, settings, project_id=host["project_id"],
        name=f"host/{host['name']}", scope_id=host_id, backend=backend,
        comment=f"boss@{host['name']}", actor=actor)

    public = minted["public_key"]
    connection.execute(
        sql("UPDATE deployment_hosts SET key_secret_ref = :s, key_public = :pub, "
            "updated_at = now() WHERE id = :i"),
        {"s": minted.get("id") or minted.get("secret_ref_id"), "pub": public, "i": host_id})
    user = (host.get("ssh_user") or "root").strip()
    address = host["address"]
    return {
        "host_id": host_id,
        "public_key": public,
        "fingerprint": minted.get("fingerprint"),
        "backend": backend,
        # One line, safe to run twice, creates ~/.ssh with the right modes. sshd silently
        # ignores an authorized_keys with loose permissions, which reads as "the key does not
        # work" — so the chmods are part of the instruction, not an afterthought.
        # PUSHED FROM THE OPERATOR'S MACHINE, not typed on the host.
        #
        # The first version handed over a bare `echo >> authorized_keys` line whose destination
        # lived only in prose. It got pasted into a laptop terminal — which authorises the
        # wrong machine and then fails verify with a message about the key, the one clue that
        # does NOT point at the actual mistake. Wrapping it in `ssh <host>` removes the choice:
        # the command is correct wherever it is pasted, because it names its own destination.
        #
        # It uses the operator's EXISTING access (agent, default key, or a password prompt),
        # which is the same credential that makes any of this possible in the first place.
        "install_command": install_command_for(host, public),
        "install_variants": install_variants_for(host, public),
        # For the case where someone is already sitting on the box (provider console, rescue).
        "install_command_on_host": (
            "mkdir -p ~/.ssh && chmod 700 ~/.ssh && "
            f"printf '%s\\n' '{public}' >> ~/.ssh/authorized_keys && "
            "chmod 600 ~/.ssh/authorized_keys"),
        "install_on": f"{user}@{address}",
        "install_hint": (
            f"Run this FROM YOUR MACHINE — it pushes the key to {address} over the SSH access "
            f"you already have. Expect BOSS_KEY_INSTALLED."),
        "next": f"Run it, then Verify — BOSS will open its own session to {address}.",
    }


# ── Getting a credential onto a host ─────────────────────────────────────────
#
# THE CONSTRAINT, stated once: BOSS cannot place the FIRST credential on a machine it has no
# way into. Nothing can. Every provisioning tool starts from something the customer already
# holds. Pretending otherwise is what produced a "Generate key" button whose only next step
# was a human pasting a line into a terminal — and, on the first real attempt, into the WRONG
# terminal, because a bare shell command reads as "paste me here".
#
# So there are three honest paths, and the product supports all three:
#
#   1. ADOPT   — the customer already has a working key. Upload it; BOSS stores it in the
#                secret backend and uses it. Nothing to install. This is the common case.
#   2. INSTALL — BOSS generates its OWN key and installs it over a credential that already
#                works (usually the adopted one). This is BOSS configuring SSH on the host,
#                and it is what makes the key rotatable without the customer's key.
#   3. MANUAL  — BOSS mints, a human installs. Only for a host where no credential exists at
#                all: a fresh VM reachable solely through a provider console.

def _bind_key(connection, host_id: str, secret_ref_id: str,
              public_key: Optional[str] = None) -> None:
    connection.execute(
        sql("UPDATE deployment_hosts SET key_secret_ref = :s, "
            "key_public = COALESCE(:pub, key_public), updated_at = now() "
            "WHERE id = :i"), {"s": secret_ref_id, "pub": public_key, "i": host_id})


def adopt_host_key(connection, settings: Settings, *, host_id: str, private_key: str,
                   backend: str = "file", actor: str = "operator") -> dict[str, Any]:
    """PATH 1 — take a key the customer already has and make it this host's credential.

    The material is written straight through to the secret backend and never retained by BOSS;
    only the reference and a fingerprint are recorded. No install step exists because the key
    is, by definition, already authorised on the box — so `verify` is the immediate next call
    and it either proves that or says why not.
    """
    from .secrets_service import put_secret

    host = get_host(connection, host_id)
    if not host:
        raise ValueError(f"no such deployment host: {host_id}")

    material = (private_key or "").strip()
    if "PRIVATE KEY" not in material:
        # A public key here is the easy mistake, and storing it would fail much later with a
        # confusing auth error instead of now with an obvious one.
        raise ValueError(
            "that does not look like a PRIVATE key (expected a '-----BEGIN ... PRIVATE KEY-----' "
            "block). Paste the private half; BOSS never needs the public one.")

    stored = put_secret(
        connection, settings, project_id=host["project_id"],
        name=f"host/{host['name']}", value=material + "\n",
        secret_kind="ssh_private_key", backend=backend,
        scope_id=host_id, actor=actor)
    # Adoption clears any pending install line: the customer's key is already on the
    # box, so showing a command to install a different one would be nonsense.
    _bind_key(connection, host_id, stored["secret_ref_id"])
    connection.execute(sql("UPDATE deployment_hosts SET key_public = NULL "
                           "WHERE id = :i"), {"i": host_id})
    return {"host_id": host_id, "adopted": True, "backend": backend,
            "fingerprint": stored.get("fingerprint"),
            "next": "Verify the host — the key should already be authorised on it."}


def install_generated_key(connection, settings: Settings, *, host_id: str,
                          keep_bootstrap_key: bool = True,
                          backend: str = "file",
                          actor: str = "operator") -> dict[str, Any]:
    """PATH 2 — BOSS generates its own key and INSTALLS it, over the credential it already has.

    This is the step that was missing, and it is the one that makes host onboarding a BOSS
    capability rather than an instruction sheet. It needs an existing working credential, which
    is why `adopt` comes first for a host BOSS has never touched.

    Idempotent: appends only when the key is not already present, so re-running never grows
    `authorized_keys`. The append is done with a here-doc over the existing session rather than
    by rewriting the file — a customer's other keys on that host are not BOSS's to reorganise.
    """
    require_direct_host_write_policy(settings)
    from .deploy_service import _discard_key_file, _materialise_key, _ssh, _ssh_key_file
    from .secret_backends import SecretUnavailable, resolve_target_secret
    from .secrets_service import generate_keypair

    host = get_host(connection, host_id)
    if not host:
        raise ValueError(f"no such deployment host: {host_id}")
    if not host.get("key_secret_ref"):
        raise ValueError(
            "this host has no working credential yet. Adopt an existing key first "
            "(POST .../adopt-key), or install BOSS's key by hand from POST .../key.")

    user = (host.get("ssh_user") or "root").strip()
    address = host["address"]
    port = int(host.get("ssh_port") or 22)
    bootstrap_ref = host["key_secret_ref"]

    # The credential BOSS is connecting WITH right now.
    probe_target = {"secret_refs": [{"name": "ssh_key", "secret_ref_id": bootstrap_ref}]}
    ephemeral = None
    try:
        material = resolve_target_secret(connection, probe_target, "ssh_key", settings)
        key = ephemeral = _materialise_key(material) if material else _ssh_key_file(settings)
    except (SecretUnavailable, Exception) as exc:  # noqa: BLE001
        raise ValueError(f"current host credential is unusable: {exc}")

    try:
        minted = generate_keypair(
            connection, settings, project_id=host["project_id"],
            name=f"host/{host['name']}/boss", scope_id=host_id, backend=backend,
            comment=f"boss@{host['name']}", actor=actor)
        public = minted["public_key"]

        # Revoke the key this one REPLACES, while a working session is still open. Appending
        # without revoking left a stale BOSS key on the box after every rotation — invisible
        # to the fleet, and still authorised. Done in the same command so a rotation cannot
        # half-apply: the new key goes in and the old one comes out together.
        superseded = (host.get("key_public") or "").strip()
        revoke = (f"grep -vxF {shlex.quote(superseded)} ~/.ssh/authorized_keys "
                  "> /tmp/.boss_ak && mv /tmp/.boss_ak ~/.ssh/authorized_keys; "
                  if superseded and superseded != public else "")

        # grep -qxF: exact whole-line match, so a second run is a no-op rather than a duplicate.
        script = (
            "set -e; mkdir -p ~/.ssh; chmod 700 ~/.ssh; touch ~/.ssh/authorized_keys; "
            f"grep -qxF {shlex.quote(public)} ~/.ssh/authorized_keys "
            f"|| printf '%s\\n' {shlex.quote(public)} >> ~/.ssh/authorized_keys; "
            f"{revoke}"
            "chmod 600 ~/.ssh/authorized_keys; echo BOSS_KEY_INSTALLED")
        res = _ssh(key, address, user, port, script, timeout=45)
    finally:
        if ephemeral:
            _discard_key_file(ephemeral)

    out = f"{getattr(res, 'stdout', '') or ''}{getattr(res, 'stderr', '') or ''}"
    if "BOSS_KEY_INSTALLED" not in out:
        raise ValueError(f"could not install BOSS's key on {address}: {out.strip()[:300]}")

    # Only now does the host switch to BOSS's own key — after the install is proven, so a
    # failure here leaves the working bootstrap credential in place rather than stranding it.
    _bind_key(connection, host_id, minted.get("id") or minted.get("secret_ref_id"),
              public_key=public)
    emit_event(connection, host["project_id"], "host.key_installed", actor,
               "deployment_host", host_id,
               {"host": host["name"], "address": address,
                "fingerprint": minted.get("fingerprint"),
                "bootstrap_secret_ref": bootstrap_ref,
                "bootstrap_retained": keep_bootstrap_key})
    return {"host_id": host_id, "installed": True, "backend": backend,
            "fingerprint": minted.get("fingerprint"),
            "bootstrap_secret_ref": bootstrap_ref,
            "note": ("BOSS now has its own key on this host. The bootstrap key was left in "
                     "place; it can be removed from the host once you are satisfied."),
            "next": "Verify the host, then Provision it if docker is missing."}


def provision_host(connection, settings: Settings, *, host_id: str,
                   install_docker: bool = True,
                   actor: str = "operator") -> dict[str, Any]:
    """Make a reachable host actually able to RUN a deployment.

    `verify` reports "docker not found"; this is the button that fixes it. Separate from verify
    on purpose — checking is read-only and safe to run at any time, whereas installing packages
    changes someone's machine and should be an explicit act.
    """
    require_direct_host_write_policy(settings)
    from .deploy_service import _discard_key_file, _materialise_key, _ssh, _ssh_key_file
    from .secret_backends import SecretUnavailable, resolve_target_secret

    host = get_host(connection, host_id)
    if not host:
        raise ValueError(f"no such deployment host: {host_id}")
    if not host.get("key_secret_ref"):
        raise ValueError("no credential for this host yet — adopt or install a key first.")

    user = (host.get("ssh_user") or "root").strip()
    address = host["address"]
    port = int(host.get("ssh_port") or 22)

    probe_target = {"secret_refs": [{"name": "ssh_key", "secret_ref_id": host["key_secret_ref"]}]}
    ephemeral = None
    try:
        material = resolve_target_secret(connection, probe_target, "ssh_key", settings)
        key = ephemeral = _materialise_key(material) if material else _ssh_key_file(settings)
    except (SecretUnavailable, Exception) as exc:  # noqa: BLE001
        raise ValueError(f"host credential is unusable: {exc}")

    steps: list[str] = []
    try:
        if install_docker:
            # Distro packages, not the convenience script piped from the internet: an
            # unattended `curl | sh` on someone else's machine is not a thing to do quietly.
            # `sudo -n` so a host without passwordless sudo fails fast instead of hanging on a
            # prompt nobody can answer.
            sudo = "" if user == "root" else "sudo -n "
            script = (
                "set -e; "
                "if command -v docker >/dev/null; then echo DOCKER_PRESENT; else "
                f"  {sudo}env DEBIAN_FRONTEND=noninteractive apt-get update -qq && "
                f"  {sudo}env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "
                "     docker.io >/dev/null 2>&1 && echo DOCKER_INSTALLED || echo DOCKER_FAILED; "
                "fi; "
                f"{sudo}systemctl enable --now docker >/dev/null 2>&1 || true; "
                "command -v docker >/dev/null && docker --version || echo DOCKER_STILL_MISSING")
            res = _ssh(key, address, user, port, script, timeout=300)
            out = f"{getattr(res, 'stdout', '') or ''}{getattr(res, 'stderr', '') or ''}"
            steps.append(out.strip()[-400:])
            if "DOCKER_STILL_MISSING" in out:
                raise ValueError(f"docker could not be installed on {address}: "
                                 f"{out.strip()[-300:]}")
    finally:
        if ephemeral:
            _discard_key_file(ephemeral)

    emit_event(connection, host["project_id"], "host.provisioned", actor,
               "deployment_host", host_id, {"host": host["name"], "steps": steps})
    return {"host_id": host_id, "provisioned": True, "steps": steps,
            "next": "Verify the host — it should now report ready."}


def retire_host(connection, settings: Settings, *, host_id: str,
                revoke_key: bool = True, remove_docker: bool = False,
                purge: bool = False, force: bool = False,
                actor: str = "operator") -> dict[str, Any]:
    """Hand a host back the way BOSS found it, and stop scheduling on it.

    Onboarding that cannot be undone is onboarding nobody wants to try. This is the other half
    of the wizard, and it is also the real operation for decommissioning a box later.

    What it removes and what it deliberately does NOT:

      * BOSS'S OWN KEY, matched EXACTLY against the public half BOSS recorded. Never a rewrite
        of `authorized_keys`, never a "remove keys that look like ours" — the customer's other
        keys on that machine are the thing standing between them and a locked-out server, and
        they are not BOSS's to reorganise.
      * DOCKER STAYS unless explicitly asked for. It is a shared dependency: on a host running
        anything else, removing it to tidy up after ourselves breaks someone else's service.
      * The deploy directory stays. It holds app data, and `teardown`/`purge` on the TARGET is
        the operation that owns that decision.

    Refuses while apps are still pointed here, unless `force` — retiring a host out from under
    a running deployment is a way to discover which apps mattered.
    """
    require_direct_host_write_policy(settings)
    from .deploy_service import _discard_key_file, _materialise_key, _ssh, _ssh_key_file
    from .secret_backends import SecretUnavailable, resolve_target_secret

    host = get_host(connection, host_id)
    if not host:
        raise ValueError(f"no such deployment host: {host_id}")

    attached = connection.execute(
        sql("SELECT app_name FROM deployment_targets "
            "WHERE host_id = :h AND status = 'active' ORDER BY app_name"),
        {"h": host_id}).mappings().all()
    apps = [r["app_name"] for r in attached]
    if apps and not force:
        raise ValueError(
            f"{len(apps)} app(s) still deploy to this host ({', '.join(apps)}). "
            "Move or retire those targets first, or pass force=true to detach them.")

    steps: list[str] = []
    warnings: list[str] = []
    user = (host.get("ssh_user") or "root").strip()
    address = host["address"]
    port = int(host.get("ssh_port") or 22)
    public = (host.get("key_public") or "").strip()

    if revoke_key and host.get("key_secret_ref"):
        ephemeral = None
        try:
            probe = {"secret_refs": [{"name": "ssh_key",
                                      "secret_ref_id": host["key_secret_ref"]}]}
            material = resolve_target_secret(connection, probe, "ssh_key", settings)
            key = ephemeral = _materialise_key(material) if material else _ssh_key_file(settings)

            if public:
                # grep -vxF: drop only the EXACT line. A substring match could take a
                # customer key that happens to share a comment, and rewriting the file
                # wholesale could take all of them.
                ak = '~/.ssh/authorized_keys'
                # Sweep by BOSS's OWN COMMENT, not just the key it currently records.
                # Rotation appended a new line each time without revoking the one it
                # replaced, so a host that had been re-keyed carried orphans that nothing
                # tracked and retire silently left behind. Every key BOSS issues is
                # commented `boss@<host name>`; that marker is what identifies ours.
                marker = f"boss@{host['name']}"
                # End-anchored: ` boss@prod` must not match ` boss@production`.
                marker_re = re.escape(f" {marker}") + r"$"
                script = (
                    'set -e; '
                    f'if [ -f {ak} ]; then '
                    f'  cp {ak} {ak}.boss-retire.bak; '
                    f'  grep -vxF {shlex.quote(public)} {ak}.boss-retire.bak '
                    f'    | grep -vE {shlex.quote(marker_re)} > {ak} || true; '
                    f'  chmod 600 {ak}; echo KEY_REVOKED; '
                    'else echo NO_AUTHORIZED_KEYS; fi')
                res = _ssh(key, address, user, port, script, timeout=30)
                out = f"{getattr(res, 'stdout', '') or ''}{getattr(res, 'stderr', '') or ''}"
                if "KEY_REVOKED" in out:
                    steps.append("removed BOSS's key from authorized_keys "
                                 "(backup left at ~/.ssh/authorized_keys.boss-retire.bak)")
                else:
                    warnings.append(f"could not revoke the key on {address}: {out.strip()[:200]}")
            else:
                # An ADOPTED key belongs to the customer. Removing it would take away access
                # BOSS was only ever borrowing.
                steps.append("key was adopted, not issued by BOSS — left in place on the host")

            if remove_docker:
                sudo = "" if user == "root" else "sudo -n "
                res = _ssh(key, address, user, port,
                           f"{sudo}env DEBIAN_FRONTEND=noninteractive apt-get remove -y -qq "
                           "docker.io >/dev/null 2>&1 && echo DOCKER_REMOVED || echo DOCKER_KEPT",
                           timeout=180)
                out = f"{getattr(res, 'stdout', '') or ''}"
                steps.append("docker removed" if "DOCKER_REMOVED" in out
                             else "docker could not be removed (left in place)")
        except (SecretUnavailable, Exception) as exc:  # noqa: BLE001
            # A box that has already gone away is the NORMAL case for decommissioning. Retiring
            # the record must still succeed, with the leave-behind stated rather than hidden.
            warnings.append(f"could not reach {address} to clean up: "
                            f"{type(exc).__name__}: {exc}"[:200])
        finally:
            if ephemeral:
                _discard_key_file(ephemeral)

    if apps and force:
        connection.execute(
            sql("UPDATE deployment_targets SET host_id = NULL, updated_at = now() "
                "WHERE host_id = :h"), {"h": host_id})
        steps.append(f"detached {len(apps)} target(s): {', '.join(apps)}")

    # EVERY secret scoped to this host, not just the one it currently points at.
    #
    # Retiring only `key_secret_ref` left the installed key — and every key from an earlier
    # rotation — sitting `active` in the store forever. Purging the host then orphaned them
    # completely: live credentials, for a machine that no longer exists, with nothing left
    # pointing at them. Scope is the link that survives, so scope is what to retire by.
    retired = connection.execute(
        sql("UPDATE secret_refs SET status = 'retired', updated_at = now() "
            "WHERE scope_id = :s AND status <> 'retired' RETURNING id"),
        {"s": host_id}).mappings().all()
    if retired:
        steps.append(f"retired {len(retired)} stored credential(s) for this host")

    if purge:
        connection.execute(sql("DELETE FROM deployment_hosts WHERE id = :i"), {"i": host_id})
        steps.append("deleted the host record")
    else:
        connection.execute(
            sql("UPDATE deployment_hosts SET status = 'retired', key_secret_ref = NULL, "
                "key_public = NULL, last_preflight = CAST(:r AS JSONB), updated_at = now() "
                "WHERE id = :i"),
            {"r": json.dumps({"ready": False, "problems": [], "findings": [],
                              "retired_at_steps": steps}), "i": host_id})
        steps.append("host marked retired (kept for the audit trail)")

    emit_event(connection, host["project_id"], "host.retired", actor,
               "deployment_host", host_id,
               {"host": host["name"], "address": address, "purged": purge,
                "revoked_key": revoke_key, "removed_docker": remove_docker,
                "detached_apps": apps if force else [], "warnings": warnings})
    return {"host_id": host_id, "retired": True, "purged": purge,
            "steps": steps, "warnings": warnings,
            "next": ("The host is gone from the fleet. Register it again to walk the wizard "
                     "from the start." if purge else
                     "The host is retired. Nothing schedules on it; the record remains.")}


# Problems `preflight` raises about the APP rather than the machine. A host has no domain and
# no health checks until something is deployed onto it, so these must not decide whether the
# host itself verified.
_APP_SCOPE_MARKERS = (
    "no domain",
    "health_checks",
    "health cannot be observed",
    "observe() would have no verdict",
)


def _is_app_scope(problem: str) -> bool:
    low = (problem or "").lower()
    return any(m in low for m in _APP_SCOPE_MARKERS)


def _classify(problems: list[str]) -> list[dict[str, str]]:
    """Turn raw preflight problems into findings an operator can act on.

    A bare "permission denied" sends people to regenerate a key that was never the issue.
    """
    findings: list[dict[str, str]] = []
    for raw in problems:
        text = str(raw)
        low = text.lower()
        if any(m in low for m in EXPIRED_PASSWORD_MARKERS):
            findings.append({
                "kind": "expired_root_password",
                "detail": text,
                "remedy": ("Key auth SUCCEEDED — PAM then closed the session because this "
                           "image ships the account's password expired. Clear it on the host "
                           "(chage -d today <user>); do not regenerate the key."),
            })
        elif "permission denied" in low or "publickey" in low:
            findings.append({
                "kind": "key_not_installed",
                "detail": text,
                "remedy": ("The key is minted but not on the host yet. Run this host's "
                           "install command FROM YOUR MACHINE — it is shown on the host "
                           "row. Do NOT press Generate key again: that mints a new key "
                           "and invalidates the command you already have."),
            })
        elif "ssh_outward_allowed" in low or "outward ssh" in low:
            findings.append({
                "kind": "outward_ssh_disabled",
                "detail": text,
                "remedy": ("BOSS is configured not to reach out over SSH at all, so no host "
                           "can be verified or deployed to. BOSS_SSH_OUTWARD_ALLOWED=false was "
                           "set on this install; remove it (on a managed install, from the "
                           "config store) and restart. This is a global switch, not a problem "
                           "with this host."),
            })
        elif "bound but unusable" in low or "does not exist" in low:
            # The host row still points at a secret the store cannot produce. Naming the
            # internal store key here ("BOSS_DH_..._HOST_...") told the operator nothing and
            # read like a live fault, when it is really just the reason the wizard is back at
            # step 1. Say what happened and what to do instead.
            findings.append({
                "kind": "credential_lost",
                "detail": text,
                "remedy": ("The key this host was registered with is gone from the secret "
                           "store, so BOSS cannot log in. Nothing is wrong with the machine. "
                           "Start again at Credential: generate a new key (or lend an existing "
                           "one) and push it to the host. Store it in `database` so it survives "
                           "the next restart — keys kept in `file` live on the container's own "
                           "disk and are destroyed on every redeploy, which is what happened "
                           "here."),
            })
        elif "docker" in low:
            findings.append({
                "kind": "docker_missing",
                "detail": text,
                "remedy": "Install Docker on the host; the SSH connector deploys containers.",
            })
        else:
            findings.append({"kind": "other", "detail": text, "remedy": ""})
    return findings


def _connection_probe(connection, settings: Settings, host: dict,
                      probe_target: dict) -> dict[str, Any]:
    """Actually open an SSH session to the box and run one harmless command.

    Without this, `verify` only asked whether a key was BOUND — so a key minted here and
    installed on the WRONG machine still reported "verified". A status that cannot fail is not
    evidence, and this one is the whole basis for calling a host deployable.

    Reuses deploy_service's ssh helpers rather than shelling out again: the key-materialisation
    and cleanup rules there are the ones the deploy path itself obeys, and a second SSH routine
    would eventually disagree about which key it used.
    """
    from .deploy_service import (_discard_key_file, _materialise_key, _ssh, _ssh_key_file)
    from .secret_backends import SecretUnavailable, resolve_target_secret

    user = (host.get("ssh_user") or "root").strip()
    address = host["address"]
    port = int(host.get("ssh_port") or 22)

    ephemeral = None
    try:
        material = resolve_target_secret(connection, probe_target, "ssh_key", settings)
        key = ephemeral = _materialise_key(material) if material else _ssh_key_file(settings)
    except (SecretUnavailable, Exception) as exc:  # noqa: BLE001
        return {"reachable": False, "problems": [f"ssh key unusable: {exc}"]}

    try:
        # Read-only and creates nothing. `docker` is reported, not required: a host can be
        # reachable and simply not have it yet, and conflating the two hides which is wrong.
        res = _ssh(key, address, user, port,
                   "echo BOSS_SSH_OK; command -v docker >/dev/null && echo DOCKER_OK "
                   "|| echo DOCKER_MISSING; uname -m", timeout=25)
    finally:
        if ephemeral:
            _discard_key_file(ephemeral)

    out = f"{getattr(res, 'stdout', '') or ''}{getattr(res, 'stderr', '') or ''}"
    if "BOSS_SSH_OK" not in out:
        detail = (getattr(res, "stderr", "") or getattr(res, "stdout", "") or "").strip()
        return {"reachable": False,
                "problems": [f"ssh to {user}@{address}:{port} failed: {detail[:300]}"]}

    found: dict[str, Any] = {"reachable": True, "problems": []}
    if "DOCKER_MISSING" in out:
        found["problems"].append("docker not found on the host")
    for line in out.splitlines():
        token = line.strip()
        # `uname -m` — the truth about the box, better than whatever was typed at registration.
        if token in ("x86_64", "amd64", "aarch64", "arm64"):
            found["arch_detected"] = "amd64" if token in ("x86_64", "amd64") else "arm64"
    return found


def verify_host(connection, settings: Settings, *, host_id: str) -> dict[str, Any]:
    """Actually connect. `verified` here is evidence, not a checkbox someone ticked.

    Reuses `deployment_connectors.preflight` rather than opening its own SSH session: the
    connector is the thing that has been reasoned about, and a second connection path would
    eventually disagree with the first about what "ready" means.
    """
    from .deployment_connectors import connector_for, preflight

    host = get_host(connection, host_id)
    if not host:
        raise ValueError(f"no such deployment host: {host_id}")

    # A host-shaped target: enough for the connector to try a connection, with no app attached.
    #
    # The binding NAME comes from the connector, never from a literal here. Hardcoding
    # "ssh_private_key" (the secret KIND) where the connector wants "ssh_key" (the binding
    # name) made every verify report "no ssh_key bound to this target" for a host whose key
    # had just been minted and stored correctly.
    probe_conn = connector_for({"target_kind": "ssh"})
    key_names = [r["name"] for r in (probe_conn.required_secrets() if probe_conn else [])] \
        or ["ssh_key"]
    probe = {
        "id": f"probe_{host_id}",
        "project_id": host["project_id"],
        "app_name": f"__preflight__{host['name']}",
        "target_kind": "ssh",
        "target_ref": target_ref_for(host),
        "secret_refs": ([{"name": n, "secret_ref_id": host["key_secret_ref"]}
                         for n in key_names] if host.get("key_secret_ref") else []),
        "required_resources": [],
        "domains": [],
    }
    # VERIFY IS WHERE TRUST IS GIVEN: the key the host shows this authenticated session is
    # recorded (host_key_pin), and every governed connection after this one insists on it.
    from .host_key_pin import pinned

    with pinned(connection, host_id, capture=True):
        try:
            report = preflight(probe, settings, connection)
        except Exception as exc:  # noqa: BLE001 — a failed probe is a finding, not a crash
            report = {"ready": False, "problems": [f"{type(exc).__name__}: {exc}"[:300]]}

        # `preflight` validates a DEPLOY TARGET: it also asks whether the app has a domain and
        # declared health checks. Those are properties of an app, and a host has neither — so
        # reporting them here told an operator their machine was unreachable when the only thing
        # missing was an app that has not been attached yet. Kept in the record, out of the verdict.
        all_problems = [str(p) for p in (report.get("problems") or [])]
        problems = [p for p in all_problems if not _is_app_scope(p)]
        deferred = [p for p in all_problems if _is_app_scope(p)]

        # Credentials resolving is necessary but NOT sufficient. Only a real session decides.
        probe_result: dict[str, Any] = {"reachable": False, "problems": []}
        if probe["secret_refs"] and not problems:
            probe_result = _connection_probe(connection, settings, host, probe)
            problems += [str(p) for p in probe_result.get("problems") or []]

    findings = _classify(problems)
    ready = bool(probe_result.get("reachable")) and not problems

    # `degraded` rather than back to `registered`: a host that was once verified and now is not
    # is a different situation from one that was never set up, and pages differently.
    if ready:
        status = "verified"
    else:
        status = "degraded" if host.get("status") == "verified" else "registered"

    stored = {"ready": ready, "problems": problems, "findings": findings,
              "deferred_to_app": deferred, "reachable": bool(probe_result.get("reachable")),
              "arch_detected": probe_result.get("arch_detected"),
              "connector": report.get("connector")}
    detected = probe_result.get("arch_detected")
    connection.execute(
        sql("UPDATE deployment_hosts SET status = :s, last_preflight_at = now(), "
            "last_preflight = CAST(:r AS JSONB), "
            # The box's own `uname -m` beats what someone typed at registration — and the
            # build step reads arch, so a wrong value fails as a runtime crash later.
            "arch = COALESCE(:arch, arch), updated_at = now() WHERE id = :i"),
        {"s": status, "r": json.dumps(stored), "arch": detected, "i": host_id})

    return {"host_id": host_id, "status": status, **stored}


def attach_target(connection, *, target_id: str, host_id: str) -> dict[str, Any]:
    """Point an app at a host, deriving `target_ref` so the two cannot drift.

    Writing the address into the target by hand is what produced ten copies of one host; the
    only supported way to set it is from the host row.
    """
    host = get_host(connection, host_id)
    if not host:
        raise ValueError(f"no such deployment host: {host_id}")
    if host.get("status") == "retired":
        raise ValueError(f"host {host['name']} is retired; nothing may be scheduled on it")
    target = connection.execute(
        sql("SELECT id, project_id FROM deployment_targets WHERE id = :t"),
        {"t": target_id},
    ).mappings().first()
    if not target:
        raise ValueError(f"no such deployment target: {target_id}")
    if target["project_id"] != host.get("project_id") and not host_usable_by(
            connection, host_id, target["project_id"]):
        raise ValueError(
            "host and target belong to different organisations; attach refused")

    connection.execute(
        sql("UPDATE deployment_targets SET host_id = :h, target_ref = :ref, "
            "updated_at = CURRENT_TIMESTAMP WHERE id = :t"),
        {"h": host_id, "ref": target_ref_for(host), "t": target_id})
    return {"target_id": target_id, "host_id": host_id,
            "target_ref": target_ref_for(host), "host_name": host["name"],
            "arch": host.get("arch")}


def backfill_hosts_from_targets(connection, *, project_id: str,
                                actor: str = "migration") -> dict[str, Any]:
    """Create one host per distinct `target_ref` and re-point existing targets at it.

    Idempotent: safe to re-run, and it never edits a target that already has a host_id. This is
    what lets the fleet table arrive without a flag day — existing targets keep deploying via
    the same derived target_ref they already had.
    """
    rows = connection.execute(
        sql("SELECT DISTINCT target_ref FROM deployment_targets "
            "WHERE project_id = :p AND host_id IS NULL AND target_ref IS NOT NULL "
            "  AND target_ref <> '' AND target_kind IN ('ssh', 'container')"),
        {"p": project_id}).mappings().all()

    from .deployment_connectors import _split_ref

    created, linked = [], 0
    for row in rows:
        ref = row["target_ref"]
        user, address, port = _split_ref(ref)
        if not address or address == "<HOST>":
            continue
        existing = connection.execute(
            sql("SELECT id FROM deployment_hosts WHERE project_id = :p AND address = :a "
                "AND ssh_user = :u AND ssh_port = :sp"),
            {"p": project_id, "a": address, "u": user, "sp": port}).mappings().first()
        if existing:
            host_id = existing["id"]
        else:
            host = register_host(connection, project_id=project_id,
                                 name=address, address=address, ssh_user=user,
                                 ssh_port=port, notes=f"backfilled from {ref}", actor=actor)
            host_id = host["id"]
            created.append({"host_id": host_id, "address": address})
        result = connection.execute(
            sql("UPDATE deployment_targets SET host_id = :h, updated_at = now() "
                "WHERE project_id = :p AND host_id IS NULL AND target_ref = :ref"),
            {"h": host_id, "p": project_id, "ref": ref})
        linked += getattr(result, "rowcount", 0) or 0

    return {"hosts_created": created, "targets_linked": linked}


def _target_id() -> str:
    return f"deptgt_{uuid.uuid4().hex[:20]}"


def _ports_assigned_on(connection, address: str) -> dict[int, str]:
    """Every port BOSS has already promised on this host, whoever owns it.

    Not scoped to the project: a collision with another project's app is still a collision, and
    the machine does not care whose row reserved the socket.
    """
    rows = connection.execute(
        sql("SELECT t.app_name, t.domains FROM deployment_targets t WHERE t.status <> 'retired' "
            + _NEVER_RAN_SQL)
    ).mappings().all()
    taken: dict[int, str] = {}
    for row in rows:
        for domain in (row["domains"] or []):
            head, _, tail = str(domain).rpartition(":")
            if tail.isdigit() and head == address:
                taken[int(tail)] = str(row["app_name"] or "")
    return taken


#: A target whose every deploy FAILED holds no port. Onboarding (deptgt_35ba82e8) was given
#: HelpDesk's :8000 after HelpDesk's target was retired, its only deploy failed, and it kept the
#: port: HelpDesk's next target went to :8001, and every OAuth callback registered for :8000
#: broke. A target with no attempt yet still holds its port (it was just made for a deploy), and
#: so does one with a deploy in flight or one that ever succeeded.
_DEPLOY_WORKED = ("succeeded", "superseded", "healthy")
_DEPLOY_FAILED = ("failed", "blocked", "rejected", "cancelled", "rolled_back", "expired")
_NEVER_RAN_SQL = (
    "AND NOT ("
    " (EXISTS (SELECT 1 FROM deployment_runs r WHERE r.deployment_target_id = t.id)"
    "  OR EXISTS (SELECT 1 FROM stack_deployments d WHERE d.target_id = t.id))"
    " AND NOT EXISTS (SELECT 1 FROM deployment_runs r WHERE r.deployment_target_id = t.id"
    "                 AND r.status NOT IN ('" + "','".join(_DEPLOY_FAILED) + "'))"
    " AND NOT EXISTS (SELECT 1 FROM stack_deployments d WHERE d.target_id = t.id"
    "                 AND d.status NOT IN ('" + "','".join(_DEPLOY_FAILED) + "')))")


def _port_the_app_had(connection, project_id: str, app: str, address: str) -> Optional[int]:
    """The port this app was last given on this host (any of its targets, retired too), so its
    next target keeps the address people, bookmarks and OAuth callbacks already use."""
    from .naming import identity_key

    wanted = identity_key(app)
    for row in connection.execute(
            sql("SELECT app_name, domains FROM deployment_targets WHERE project_id = :p "
                "ORDER BY updated_at DESC NULLS LAST, created_at DESC"),
            {"p": project_id}).mappings().all():
        if identity_key(str(row["app_name"] or "")) != wanted:
            continue
        for domain in (row["domains"] or []):
            head, _, tail = str(domain).rpartition(":")
            if tail.isdigit() and head == address:
                return int(tail)
    return None


def _first_free_port(connection, address: str, *, start: int = 3000,
                     end: Optional[int] = None) -> Optional[int]:
    """A port nothing is listening on AND nothing has been promised.

    Both halves are required. A port that answers nothing right now but belongs to an app
    between deploys is not free, and taking it reproduces the collision one deploy later.
    """
    import socket

    taken = _ports_assigned_on(connection, address)
    port = max(1024, start)
    checked = 0
    stop = min(int(end), 65535) if end else 65535
    while checked < 200 and port <= stop:
        checked += 1
        if port not in taken:
            probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            probe.settimeout(2.0)
            try:
                probe.connect((address, port))
            except OSError:
                return port          # nothing answered, nothing promised
            finally:
                probe.close()
        port += 1
    return None


def create_deployment_target(
    connection, *, project_id: str, app_name: str, environment: str = "staging",
    repo: Optional[str] = None, host_id: Optional[str] = None, port: Optional[int] = None,
    actor: str = "boss",
) -> dict[str, Any]:
    """Give an app somewhere to be deployed to.

    THIS DID NOT EXIST, and its absence is what turned a routine first deployment into an
    incident. An app that has never shipped has no `deployment_target`; the deploy capability
    refuses without one; and the only tool that could create one was `write_db` with SQL the
    model had to compose from a schema it was guessing at. It guessed `code_tasks` (the table is
    `code_task_runs`), the INSERT failed, and the operator was shown a raw psycopg error.

    Reaching for raw SQL was not a lapse in judgement — it was the only route left. So the route
    exists now, and it knows the things the SQL could not: which host this project deploys to,
    which ports are genuinely free, that a bare IP must be probed over http rather than https,
    and that `target_ref` is derived from the host record rather than typed.

    Deliberately narrow, in the same spirit as `deploy_build`:
      * it creates a DESTINATION and never ships anything to it — those are two decisions;
      * an app that already has a target for this environment adopts it rather than growing a
        second one, because two targets for one app is the ambiguity that started all of this;
      * the host must be verified, since a target on an unproven host fails at deploy time,
        far from the decision that caused it.
    """
    from .naming import identity_key, project_name

    display, _blurb, _label = project_name(str(app_name or ""))
    app = display if display and display != "Untitled app" else str(app_name or "").strip()
    if not app:
        return {"ok": False, "error": "app_name is required, and must be the app's name rather "
                                      "than the directive it was built from"}
    environment = str(environment or "staging").strip() or "staging"
    wanted = identity_key(app)

    # ONE APP, ONE TARGET PER ENVIRONMENT. Compared on identity, not on the literal string:
    # "Change Calendar" and "change-calendar" are the same app, and letting both exist recreates
    # the ambiguous-target failure this function was written to end.
    for row in connection.execute(
        sql("SELECT id, app_name, status, host_id, domains FROM deployment_targets "
            "WHERE project_id = :p AND environment = :e"),
        {"p": project_id, "e": environment},
    ).mappings().all():
        # THE SAME APP, WHATEVER NAME IT WAS STORED UNDER.
        #
        # Identity alone could not see a target created before names were normalised, because
        # the name in that row is the whole directive. So this created a SECOND target for
        # Lead-Triage Inbox on the port the first one already had — two active rows, one
        # address, and the console picking whichever it happened to match. Running the stored
        # name through the namer first is what the setup endpoint does; it has to be the same
        # rule in both places or one path keeps producing what the other repairs.
        stored = str(row["app_name"] or "")
        named, _blurb, _label = project_name(stored)
        if identity_key(stored) != wanted and not (
            named and named != "Untitled app" and identity_key(named) == wanted
        ):
            continue
        if stored != app:
            connection.execute(
                sql("UPDATE deployment_targets SET app_name = :a, updated_at = now() "
                    "WHERE id = :i"), {"a": app, "i": row["id"]})
        if row["status"] == "retired":
            connection.execute(
                sql("UPDATE deployment_targets SET status = 'active', updated_at = now() "
                    "WHERE id = :i"), {"i": row["id"]})
        return {"ok": True, "target_id": row["id"], "app_name": app,
                "environment": environment, "adopted_existing": True,
                "revived": row["status"] == "retired",
                "domains": row["domains"] or [],
                "note": f"{app!r} already has a {environment} target; using it rather than "
                        "creating a second one."}

    hosts = [dict(h) for h in list_hosts(connection, project_id)]
    verified = [h for h in hosts if h.get("status") == "verified"]
    if host_id:
        host = get_host(connection, host_id) if host_usable_by(connection, host_id, project_id) else None
        if not host:
            return {"ok": False, "error": f"no such deployment host: {host_id}",
                    "hosts": [{"host_id": h["id"], "name": h["name"],
                               "status": h["status"]} for h in hosts]}
    elif len(verified) == 1:
        host = verified[0]
    else:
        # The host every other app in this project is already on. When a project deploys to one
        # machine, asking which machine is a question with one answer and no reason to ask it.
        used = {r["host_id"] for r in connection.execute(
            sql("SELECT DISTINCT host_id FROM deployment_targets "
                "WHERE project_id = :p AND host_id IS NOT NULL AND status = 'active'"),
            {"p": project_id}).mappings().all() if r["host_id"]}
        host = get_host(connection, next(iter(used))) if len(used) == 1 else None
        if not host and not verified:
            return {"ok": False,
                    "error": ("no verified deployment host in this organisation yet: add one in "
                              "Infra (Add host), verify it, then create the target."),
                    "hosts": [{"host_id": h["id"], "name": h["name"], "address": h["address"],
                               "status": h["status"]} for h in hosts]}
        if not host:
            return {"ok": False,
                    "error": ("this project deploys to more than one host, so which one this "
                              "app belongs on is a real decision. Pass host_id."),
                    "hosts": [{"host_id": h["id"], "name": h["name"], "address": h["address"],
                               "status": h["status"]} for h in hosts]}

    if host.get("status") != "verified":
        return {"ok": False,
                "error": f"host {host['name']!r} is {host.get('status')!r}, not verified. A "
                         "target on an unproven host fails at deploy time, a long way from the "
                         "decision that caused it. Finish onboarding it first."}

    address = str(host["address"])
    taken = _ports_assigned_on(connection, address)
    if port:
        port = int(port)
        if port in taken:
            return {"ok": False,
                    "error": f"port {port} on {address} is already assigned to "
                             f"{taken[port]!r}. Leave port unset and one will be chosen.",
                    "assigned": [{"port": k, "app": v} for k, v in sorted(taken.items())]}
    else:
        # THE APP'S OWN PORT FIRST: the one it had on this host, when no other app holds it now.
        port = _port_the_app_had(connection, project_id, app, address)
        if port is not None and port in taken:
            port = None
    if not port:
        # IN THE HOST'S OWN RANGE (port_min..port_max, "Ports 8000-8999" in Infra): the range is
        # what the operator said apps may use on this machine.
        low = int(host.get("port_min") or 3000)
        high = int(host.get("port_max") or 65535)
        port = _first_free_port(connection, address, start=low, end=high)
        if not port:
            return {"ok": False,
                    "error": f"no free port found on {address} in {low}-{high}"}

    if not repo:
        row = connection.execute(
            sql("SELECT repo FROM deployment_targets WHERE project_id = :p AND repo <> '' "
                "ORDER BY created_at DESC LIMIT 1"), {"p": project_id}).mappings().first()
        repo = str(row["repo"]) if row else ""
    repo = str(repo or "").rsplit("/", 1)[-1]

    # A BARE IP HAS NO CERTIFICATE. Defaulting to https had a healthy app failing its gate with
    # WRONG_VERSION_NUMBER; the destination is known here, so this is a fact rather than a guess.
    looks_like_ip = address.count(".") == 3 and all(p.isdigit() for p in address.split("."))
    scheme = "http" if (looks_like_ip or ":" in address) else "https"

    target_id = _target_id()
    connection.execute(
        sql("INSERT INTO deployment_targets "
            "(id, project_id, app_name, repo, environment, target_kind, target_ref, domains, "
            " health_checks, required_resources, status, created_by, created_at, updated_at) "
            "VALUES (:i, :p, :a, :r, :e, 'ssh', :ref, CAST(:dom AS JSONB), "
            "        CAST(:hc AS JSONB), CAST(:rr AS JSONB), 'active', :by, now(), now())"),
        # THE APP PORT IS DECLARED, not only implied by the domain: the governed host plan
        # refuses a target with no app_port ("has no app port. Set the app's port…"), which
        # stopped the very first auto-created deploy.
        {"i": target_id, "p": project_id, "a": app, "r": repo, "e": environment,
         "rr": json.dumps([{"kind": "app_port", "name": str(port)}]),
         "ref": target_ref_for(host), "dom": json.dumps([f"{address}:{port}"]),
         "hc": json.dumps([{"kind": "http", "scheme": scheme, "path": "/",
                            "expect_status": 200, "port": port}]),
         "by": actor})
    attached = attach_target(connection, target_id=target_id, host_id=host["id"])
    emit_event(connection, project_id, "deployment.target_created", actor,
               "deployment_target", target_id,
               {"app_name": app, "environment": environment, "host": host["name"],
                "domain": f"{address}:{port}"})

    return {"ok": True, "target_id": target_id, "app_name": app, "environment": environment,
            "host": host["name"], "address": address, "port": port,
            "domain": f"{address}:{port}", "target_ref": attached.get("target_ref"),
            "arch": attached.get("arch"), "repo": repo,
            "health_check": f"{scheme}://{address}:{port}/ expecting 200",
            "next": "Nothing has been deployed yet. Deploy the build to it with deploy_app."}

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
#: Imported HelpDesk AI could not deploy anywhere because the shared server had been registered under
#: another project; BOSS's own agent found "attach refused across projects" and had no way on.
#: Managing the machine (its key, verify, retire) stays with the project that registered it.
#: A SQL predicate over deployment_hosts (alias optional) with the project bound as `:p`.
#: Another project's host is shared only once it is VERIFIED (2026-11-07): an unproven machine
#: is its owner's to finish, and a target on it would fail at deploy time.
def usable_hosts_sql(alias: str = "") -> str:
    a = f"{alias}." if alias else ""
    return (f"({a}project_id = :p OR ({a}status = 'verified' AND {a}project_id IN ("
            "SELECT p2.id FROM projects p2 "
            "JOIN workspaces w2 ON w2.id = p2.workspace_id WHERE w2.org_id = "
            "(SELECT w1.org_id FROM projects p1 JOIN workspaces w1 ON w1.id = p1.workspace_id "
            "WHERE p1.id = :p))))")


def host_by_hint_sql(columns: str = "*", *, own_only: bool = False) -> str:
    """THE ONE HOST LOOKUP BY WHAT SOMEONE TYPED (2026-11-18): id, name or address, any case,
    among the hosts this project may use — its own and the verified ones its org shares, as
    list_hosts/match_host see them. Binds :p (project) and :h (the hint). inspect_host,
    run_on_host, describe_deployment_host and the port probe each looked in the project's own
    rows only, so a shared host list_deployment_hosts had just shown was "not registered"."""
    # own_only: run_on_host CHANGES a machine with that host's key, so it stays on the
    # project's own hosts; reads (inspect, describe, probe) see what the org shares.
    scope = "project_id = :p" if own_only else usable_hosts_sql()
    return (f"SELECT {columns} FROM deployment_hosts WHERE {scope} "
            "AND status <> 'retired' AND (lower(id) = lower(:h) OR lower(name) = lower(:h) "
            "OR lower(address) = lower(:h)) ORDER BY (project_id = :p) DESC LIMIT 1")


def host_usable_by(connection, host_id: str, project_id: str) -> bool:
    """May `project_id` deploy onto this host (its own, or a verified one its org shares)?"""
    return bool(connection.execute(
        sql(f"SELECT 1 FROM deployment_hosts WHERE id = :h AND {usable_hosts_sql()}"),
        {"h": host_id, "p": project_id}).first())


class HostNotUsable(ValueError):
    """Why a project may not place an app on a host, said as {headline, action}."""

    def __init__(self, headline: str, action: str, *, status_code: int = 404):
        super().__init__(f"{headline} {action}")
        self.headline = headline
        self.action = action
        self.status_code = status_code

    def to_dict(self) -> dict[str, str]:
        return {"headline": self.headline, "action": self.action}


def usable_host(connection, host_id: str, project_id: str) -> dict[str, Any]:
    """THE ONE RULE for placing an app on a host: create a target, attach one, set up a deploy.

    A project may use its own hosts and the verified hosts its organisation shares
    (`usable_hosts_sql`). Each create/attach path used to ask "is the host in THIS project?" on
    its own, so the onboarding agent offered the shared, verified 192.0.2.202 for staging and the
    stack-target create then refused it ("No such host in this project."). Managing the machine
    (key, verify, retire) stays with the project that registered it.
    """
    host = get_host(connection, str(host_id)) if host_id else None
    if not host or not host_usable_by(connection, host["id"], project_id):
        if host and host.get("project_id") != project_id and host.get("status") != "verified" \
                and _same_org(connection, str(host.get("project_id") or ""), project_id):
            raise HostNotUsable(
                f"Host {host['name']!r} belongs to another project and is not verified yet.",
                "A shared host is usable once its own project verifies it in Infra.",
                status_code=422)
        raise HostNotUsable("No such host in this project or its organisation.",
                            "Add the VM in Infra first, then create the target on it.")
    if host.get("status") == "retired":
        raise HostNotUsable(f"Host {host['name']!r} is retired.",
                            "Pick an active host, or register the VM again.", status_code=422)
    return host


def _same_org(connection, project_a: str, project_b: str) -> bool:
    return bool(connection.execute(
        sql("SELECT 1 FROM projects pa JOIN workspaces wa ON wa.id = pa.workspace_id "
            "JOIN projects pb ON pb.id = :b JOIN workspaces wb ON wb.id = pb.workspace_id "
            "WHERE pa.id = :a AND wa.org_id = wb.org_id"),
        {"a": project_a, "b": project_b}).first())


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


def run_on_host(connection, settings: Settings, host: dict, script: str, *,
                timeout: int = 60):
    """ONE WAY TO RUN A COMMAND ON A HOST WITH ITS OWN KEY: resolve the host's credential,
    materialise it for this one call, run through deploy_service._ssh (the transport every deploy
    uses, so the host-key pin applies), and discard the key file. Provisioning and a server's
    setup (services/add_server.py) both come through here."""
    from .deploy_service import _discard_key_file, _materialise_key, _ssh, _ssh_key_file
    from .secret_backends import SecretUnavailable, resolve_target_secret

    if not host.get("key_secret_ref"):
        raise ValueError("no credential for this host yet — adopt or install a key first.")
    probe_target = {"secret_refs": [{"name": "ssh_key", "secret_ref_id": host["key_secret_ref"]}]}
    ephemeral = None
    try:
        material = resolve_target_secret(connection, probe_target, "ssh_key", settings)
        key = ephemeral = _materialise_key(material) if material else _ssh_key_file(settings)
    except (SecretUnavailable, Exception) as exc:  # noqa: BLE001
        raise ValueError(f"host credential is unusable: {exc}")
    try:
        return _ssh(key, host["address"], (host.get("ssh_user") or "root").strip(),
                    int(host.get("ssh_port") or 22), script, timeout=timeout)
    finally:
        if ephemeral:
            _discard_key_file(ephemeral)


def docker_install_script(host: dict) -> str:
    """Install Docker if it is missing (apt docker.io), start it, and say the version."""
    sudo = "" if (host.get("ssh_user") or "root").strip() == "root" else "sudo -n "
    return (
        "set -e; "
        "if command -v docker >/dev/null; then echo DOCKER_PRESENT; else "
        f"  {sudo}env DEBIAN_FRONTEND=noninteractive apt-get update -qq && "
        f"  {sudo}env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "
        "     docker.io >/dev/null 2>&1 && echo DOCKER_INSTALLED || echo DOCKER_FAILED; "
        "fi; "
        f"{sudo}systemctl enable --now docker >/dev/null 2>&1 || true; "
        "command -v docker >/dev/null && docker --version || echo DOCKER_STILL_MISSING")


def provision_host(connection, settings: Settings, *, host_id: str,
                   install_docker: bool = True,
                   actor: str = "operator") -> dict[str, Any]:
    """Make a reachable host actually able to RUN a deployment.

    `verify` reports "docker not found"; this is the button that fixes it. Separate from verify
    on purpose — checking is read-only and safe to run at any time, whereas installing packages
    changes someone's machine and should be an explicit act.
    """
    require_direct_host_write_policy(settings)
    host = get_host(connection, host_id)
    if not host:
        raise ValueError(f"no such deployment host: {host_id}")
    if not host.get("key_secret_ref"):
        raise ValueError("no credential for this host yet — adopt or install a key first.")

    address = host["address"]
    steps: list[str] = []
    if install_docker:
        res = run_on_host(connection, settings, host, docker_install_script(host), timeout=300)
        out = f"{getattr(res, 'stdout', '') or ''}{getattr(res, 'stderr', '') or ''}"
        steps.append(out.strip()[-400:])
        if "DOCKER_STILL_MISSING" in out:
            raise ValueError(f"docker could not be installed on {address}: "
                             f"{out.strip()[-300:]}")

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
    # WHAT THE MACHINE SAID ABOUT ITSELF STAYS: the capacity read (host_overview, Add a server's
    # read) lives at last_preflight.facts, and a verify that rewrote the record whole dropped it,
    # so the server page went back to "not read yet" after every Re-verify.
    prior = host.get("last_preflight") or {}
    if isinstance(prior, str):
        try:
            prior = json.loads(prior)
        except ValueError:
            prior = {}
    if isinstance(prior, dict) and prior.get("facts"):
        stored["facts"] = prior["facts"]
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
    try:
        usable_host(connection, host_id, target["project_id"])
    except HostNotUsable as refused:
        raise ValueError(f"{refused.headline} {refused.action} (attach refused)") from None

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


#: Where a service commonly answers "I am up", in the order they are tried when nothing declares one.
COMMON_HEALTH_PATHS: tuple[str, ...] = ("/healthz", "/readyz", "/health")
#: Words that mean "this machine" rather than a registered host's name.
_LOCAL_WORDS = frozenset({"local", "locally", "localhost", "this machine", "this laptop",
                          "laptop", "my machine", "my laptop", "this computer"})
_LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1", "0.0.0.0"})


def health_path_from_manifest(manifest_text: Any) -> Optional[str]:
    """The path of the first health check the app's own boss.deploy.yaml declares, or None."""
    from .app_deploy_manifest import ManifestError, load_manifest
    from .preview_service import health_url_path

    if not str(manifest_text or "").strip():
        return None
    try:
        manifest = load_manifest(str(manifest_text))
    except (ManifestError, TypeError, ValueError):
        return None
    for check in manifest.health:
        path = health_url_path(check.url)
        if path:
            return path
    return None


def resolve_health_path(connection, *, project_id: str, repo: str, app_name: str,
                        health_path: Optional[str] = None,
                        manifest_text: Optional[str] = None) -> tuple[str, str]:
    """(path, source) for a NEW target's health check. The app says where it is healthy; "/" is
    the last resort and says so, because an API with no root page fails a "/" check forever.

    Order: the path the caller names, the manifest text it passes, the manifest of the newest
    stack deployment of this repository, the path a sibling target of the same app already
    uses, then "/" (source "default")."""
    from .naming import identity_key

    path = str(health_path or "").strip()
    if path:
        return (path if path.startswith("/") else "/" + path), "given"
    path = health_path_from_manifest(manifest_text)
    if path:
        return path, "manifest"
    owner, _, name = str(repo or "").strip().strip("/").rpartition("/")
    if name:
        # EXACT, owner-scoped: a LIKE on "%/name" treats "_" as a wildcard and matches another
        # owner's repository of the same name.
        try:
            row = connection.execute(
                sql("SELECT manifest_text FROM stack_deployments WHERE project_id = :p "
                    "AND COALESCE(manifest_text, '') <> '' "
                    "AND (CASE WHEN :o <> '' THEN lower(repository) = lower(:full) "
                    "     ELSE lower(split_part(repository, '/', 2)) = lower(:n) END) "
                    "ORDER BY created_at DESC LIMIT 1"),
                {"p": project_id, "o": owner, "full": f"{owner}/{name}", "n": name}
            ).mappings().first()
        except Exception:  # noqa: BLE001 - an unreadable history is "no declaration"
            row = None
        path = health_path_from_manifest(row["manifest_text"]) if row else None
        if path:
            return path, "manifest"
    wanted = identity_key(app_name)
    for row in connection.execute(
            sql("SELECT app_name, health_checks FROM deployment_targets "
                "WHERE project_id = :p AND status <> 'retired' ORDER BY created_at DESC"),
            {"p": project_id}).mappings().all():
        if identity_key(str(row["app_name"] or "")) != wanted:
            continue
        for check in row["health_checks"] or []:
            p = str((check or {}).get("path") or "")
            if p and p != "/":
                return p, "sibling_target"
    return "/", "default"


def valid_health_path(path: Any) -> Optional[str]:
    """The normalised path, or None when it is not a path (a URL, spaces, a query-less nothing)."""
    value = str(path or "").strip()
    if not value or "://" in value or any(ch.isspace() for ch in value) or len(value) > 300:
        return None
    return value if value.startswith("/") else "/" + value


def this_machine_names() -> set[str]:
    """Every address or name that means THIS machine: loopback, host.docker.internal, its
    hostname and the addresses that hostname resolves to (its LAN IP)."""
    import socket

    names = set(_LOOPBACK) | {"host.docker.internal"}
    try:
        host = socket.gethostname().lower()
        names |= {host, host.split(".")[0]}
        for info in socket.getaddrinfo(host, None):
            names.add(str(info[4][0]).lower())
    except OSError:
        pass
    return names


BUILTIN_LOCAL_HOST_NAME = "this machine"


def is_builtin_local_host(host: Optional[dict[str, Any]]) -> bool:
    """True when `host` is BOSS's built-in `'this machine'` local deploy destination."""
    if not host:
        return False
    labels = host.get("labels") or {}
    if isinstance(labels, str):
        try:
            labels = json.loads(labels)
        except ValueError:
            labels = {}
    if isinstance(labels, dict) and labels.get("builtin") and labels.get("local_host"):
        return True
    return (str(host.get("ssh_user") or "").strip().lower() == "local"
            and str(host.get("address") or "").strip().lower() in _LOOPBACK)


def is_local_only_install(connection=None, project_id: Optional[str] = None) -> bool:
    """True for local-only installs (`install_tier() != 'prod'`) with no remote host registered."""
    from . import production

    if production.resolve_install().prod:
        return False
    if connection is not None and project_id:
        for h in list_hosts(connection, project_id):
            if not is_builtin_local_host(h):
                return False
    return True


def ensure_local_host(connection, project_id: str, *, actor: str = "boss",
                      force: bool = False) -> Optional[dict[str, Any]]:
    """Idempotently ensure a verified built-in `'this machine'` host on local-only installs."""
    if not force and not is_local_only_install(connection, project_id):
        return None
    for h in list_hosts(connection, project_id):
        if is_builtin_local_host(h):
            return dict(h)
    existing = connection.execute(
        sql("SELECT id, status FROM deployment_hosts WHERE project_id = :p "
            "AND lower(name) = lower(:n) LIMIT 1"),
        {"p": project_id, "n": BUILTIN_LOCAL_HOST_NAME}).mappings().first()
    if existing:
        if existing["status"] == "retired":
            return None
        return get_host(connection, str(existing["id"]))
    host_id = _new_id()
    labels = {"builtin": True, "local_host": True}
    preflight = {"ready": True, "reachable": True, "problems": [],
                 "local_host": True, "connector": "local"}
    inserted = connection.execute(
        sql("INSERT INTO deployment_hosts "
            "(id, project_id, name, address, ssh_user, ssh_port, labels, "
            " port_min, port_max, status, last_preflight_at, last_preflight, "
            " notes, created_by) "
            "VALUES (:i, :p, :n, '127.0.0.1', 'local', 0, CAST(:l AS JSONB), "
            "        8000, 8999, 'verified', now(), CAST(:pf AS JSONB), :no, :by) "
            "ON CONFLICT (project_id, name) DO NOTHING RETURNING id"),
        {"i": host_id, "p": project_id, "n": BUILTIN_LOCAL_HOST_NAME,
         "l": json.dumps(labels), "pf": json.dumps(preflight),
         "no": "Built-in local machine target for local-only installs (no SSH or Docker required).",
         "by": actor}).scalar()
    if inserted:
        emit_event(connection, project_id, "host.local_ensured", actor,
                   "deployment_host", str(inserted),
                   {"name": BUILTIN_LOCAL_HOST_NAME, "address": "127.0.0.1",
                    "port_min": 8000, "port_max": 8999})
        return get_host(connection, str(inserted))
    row = connection.execute(
        sql("SELECT id FROM deployment_hosts WHERE project_id = :p AND lower(name) = lower(:n)"),
        {"p": project_id, "n": BUILTIN_LOCAL_HOST_NAME}).first()
    return get_host(connection, str(row[0])) if row else None


def ensure_this_machine_target(
    connection, project_id: str, *, app_name: str, repo: str = "",
    environment: str = "staging", port: Optional[int] = None,
    health_path: Optional[str] = None, manifest_text: Optional[str] = None,
    actor: str = "boss",
) -> dict[str, Any]:
    """Ensure `'this machine'` exists and create/adopt the local deployment target for `app_name`."""
    host = ensure_local_host(connection, project_id, actor=actor)
    if host is None:
        return {"ok": False, "error": "this install is not eligible for a built-in local host"}
    return create_deployment_target(
        connection, project_id=project_id, app_name=app_name, environment=environment,
        repo=repo, host_id=str(host["id"]), port=port, actor=actor,
        health_path=health_path, manifest_text=manifest_text)


def match_host(connection, project_id: str, hint: str
               ) -> tuple[Optional[dict[str, Any]], Optional[dict[str, Any]]]:
    """(host, None) when `hint` ("local", a host's id, name or address) names a registered host
    this project may use; (None, refusal) otherwise: what is registered and how to get what was
    asked for. (None, None) for an empty hint."""
    wanted = str(hint or "").strip().lower()
    if not wanted:
        return None, None
    hosts = [dict(h) for h in list_hosts(connection, project_id)]
    local = wanted in _LOCAL_WORDS
    here = this_machine_names() if local else set()
    for h in hosts:
        if wanted in (str(h.get("id") or "").lower(), str(h.get("name") or "").lower(),
                      str(h.get("address") or "").lower()):
            return h, None
        if local and str(h.get("address") or "").lower() in here:
            return h, None
    offer = ("register this machine as a deployment host (Infra > Add host), verify it, then "
             "create the target on it" if local else
             "register that host (Infra > Add host) and verify it, or pick one of the "
             "registered hosts")
    return None, {"ok": False, "no_matching_host": True, "asked_for": hint,
                  "error": f"no host matches {hint!r}: nothing registered for this project is "
                           f"{'this machine' if local else hint!r}. Registered: "
                           f"{', '.join(str(h['name']) for h in hosts) or 'none'}. "
                           "Nothing was created.",
                  "offer": offer,
                  "hosts": [{"host_id": h["id"], "name": h["name"], "address": h["address"],
                             "status": h["status"]} for h in hosts]}


def no_host_matches(connection, project_id: str, hint: str) -> Optional[dict[str, Any]]:
    """The refusal when `hint` names no registered host, else None."""
    return match_host(connection, project_id, hint)[1]


def health_path_findings(target: dict[str, Any], *, probe: Any = None) -> list[dict[str, Any]]:
    """Probe each http health check of a target; a 404 is reported with the common paths that DO
    answer. `probe(url) -> status|None` is injectable; the default is one GET with a short
    timeout. Read-only. Returns [{path, status, warning?, suggest?}]."""
    import urllib.error
    import urllib.request

    def default_probe(url: str) -> Optional[int]:
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:  # noqa: S310
                return int(resp.status)
        except urllib.error.HTTPError as exc:
            return int(exc.code)
        except Exception:  # noqa: BLE001 - unreachable is a result, not an error
            return None

    ask = probe or default_probe
    domains = target.get("domains") or []
    base_host = str(domains[0]).split(":")[0] if domains else ""
    out: list[dict[str, Any]] = []
    for check in target.get("health_checks") or []:
        if not isinstance(check, dict) or str(check.get("kind") or "http") != "http":
            continue
        scheme = str(check.get("scheme") or "http")
        # THE CHECK'S PORT, else the domain's: never an assumed :80/:443.
        port = check.get("port") or (str(domains[0]).rpartition(":")[2]
                                     if ":" in str(domains[0]) else None)
        path = str(check.get("path") or "/")
        if not base_host or not port:
            continue
        origin = f"{scheme}://{base_host}:{port}"
        status = ask(origin + path)
        item: dict[str, Any] = {"path": path, "status": status}
        if status == 404:
            item["warning"] = (f"the health path {path!r} returns 404: the app has no page "
                               "there, so this check can never pass.")
            for candidate in COMMON_HEALTH_PATHS:
                if candidate != path and ask(origin + candidate) == 200:
                    item["suggest"] = candidate
                    item["warning"] += (f" {candidate} answers 200; use "
                                        "update_deployment_target to point the check at it.")
                    break
        out.append(item)
    return out



def _port_holder(connection, address: str, port: int, *, exclude_target_id: str
                 ) -> Optional[dict[str, Any]]:
    """Another live target that already holds address:port (any app, any environment, so staging
    and production of one app never share a port on one host), or None."""
    for row in connection.execute(
            sql("SELECT t.id, t.app_name, t.environment, t.domains FROM deployment_targets t "
                "WHERE t.status <> 'retired' AND t.id <> :x " + _NEVER_RAN_SQL),
            {"x": exclude_target_id}).mappings().all():
        for domain in (row["domains"] or []):
            head, _, tail = str(domain).rpartition(":")
            if tail.isdigit() and head == address and int(tail) == int(port):
                return dict(row)
    return None


_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")


def update_deployment_target(
    connection, *, project_id: str, target_id: str, health_path: Optional[str] = None,
    health_expected_status: Optional[int] = None, port: Optional[int] = None,
    host_id: Optional[str] = None, env: Optional[dict[str, str]] = None,
    actor: str = "boss",
) -> dict[str, Any]:
    """Change a deployment target in place: its health check (path, expected status), port,
    host, and environment variables BY REFERENCE (name -> a secret ref id, never a value).

    EVERYTHING IS CHECKED BEFORE ANYTHING IS WRITTEN: a refusal leaves the row exactly as it
    was, and `deployment.target_updated` is emitted only after every write succeeded. The agent
    reaches this as a proposal the operator accepts; the API's PATCH is the other door."""
    from .secrets_service import bind_secret_to_target

    row = connection.execute(
        sql("SELECT id, app_name, host_id, domains, public_url, health_checks, "
            "required_resources, secret_refs FROM deployment_targets "
            "WHERE id = :i AND project_id = :p FOR UPDATE"),
        {"i": target_id, "p": project_id}).mappings().first()
    if row is None:
        return {"ok": False, "error": f"no deployment target {target_id} in this project"}

    # ---- validate -------------------------------------------------------------------------
    clean_path = None
    if health_path is not None:
        clean_path = valid_health_path(health_path)
        if not clean_path:
            return {"ok": False, "error": "health_path must be a path such as /healthz, "
                                          "not a URL."}
    if health_expected_status is not None and not 100 <= int(health_expected_status) <= 599:
        return {"ok": False, "error": "health_expected_status must be an HTTP status."}
    if host_id and host_id != row["host_id"]:
        try:
            usable_host(connection, host_id, project_id)
        except HostNotUsable as refused:
            return {"ok": False, "error": f"{refused.headline} {refused.action}"}
    effective_host_id = host_id or row["host_id"]
    host = get_host(connection, effective_host_id) if effective_host_id else None
    current_port = None
    for domain in (row["domains"] or []):
        tail = str(domain).rpartition(":")[2]
        if tail.isdigit():
            current_port = int(tail)
            break
    new_port = int(port) if port else current_port
    host_changes = bool(host_id and host_id != row["host_id"])
    if port and host is None:
        return {"ok": False, "error": "the target has no host, so a port cannot be set"}
    if host is not None and new_port and (host_changes or port):
        holder = _port_holder(connection, str(host["address"]), new_port,
                              exclude_target_id=target_id)
        if holder:
            return {"ok": False,
                    "error": f"port {new_port} on {host['address']} is already held by "
                             f"{holder['app_name']!r} ({holder['environment']}). Pick another "
                             "port: an app's staging and production never share one."}
    if env:
        for name, ref in env.items():
            if not _ENV_NAME.match(str(name)):
                return {"ok": False, "error": f"{str(name)[:40]!r} is not an environment "
                                              "variable name."}
            # THE SAME OWNERSHIP RULE bind_secret_to_target enforces; the value is never echoed.
            if not connection.execute(
                    sql("SELECT 1 FROM secret_refs WHERE id = :r AND project_id = :p"),
                    {"r": str(ref or ""), "p": project_id}).first():
                return {"ok": False,
                        "error": f"env {name}: the value is not a secret reference of this "
                                 "project. Pass the id of an existing reference, never a "
                                 "value."}

    # ---- write ----------------------------------------------------------------------------
    changed: dict[str, Any] = {}
    if host_changes:
        attach_target(connection, target_id=target_id, host_id=host_id)
        changed["host_id"] = host_id
    if host is not None and (host_changes or port):
        from urllib.parse import urlsplit, urlunsplit

        # The domain names the host's address and the port; moving either rewrites it, which
        # also frees the old address:port reservation.
        resources = [r for r in (row["required_resources"] or [])
                     if not (isinstance(r, dict) and r.get("kind") == "app_port")]
        if new_port:
            resources.append({"kind": "app_port", "name": str(new_port)})
        old_host = get_host(connection, row["host_id"]) if row.get("host_id") else None
        old_addr = str(old_host["address"]) if old_host else ""
        new_pub = row.get("public_url")
        if new_pub and new_port:
            parts = urlsplit(str(new_pub))
            if parts.scheme and parts.hostname:
                target_host = (str(host["address"])
                               if (host_changes and old_addr and parts.hostname == old_addr)
                               else parts.hostname)
                if parts.port is not None or target_host == str(host["address"]) or (
                        old_addr and parts.hostname == old_addr):
                    host_fmt = f"[{target_host}]" if ":" in target_host else target_host
                    new_pub = urlunsplit((parts.scheme, f"{host_fmt}:{int(new_port)}",
                                          parts.path, parts.query, parts.fragment))
        connection.execute(
            sql("UPDATE deployment_targets SET required_resources = CAST(:r AS JSONB), "
                "domains = CAST(:d AS JSONB), public_url = :pub, updated_at = now() "
                "WHERE id = :i"),
            {"r": json.dumps(resources),
             "d": json.dumps([f"{host['address']}:{new_port}"] if new_port
                             else [str(host["address"])]),
             "pub": new_pub, "i": target_id})
        if new_pub != row.get("public_url"):
            changed["public_url"] = new_pub
        if port:
            changed["port"] = int(port)
        if host_changes and new_port:
            changed["address"] = f"{host['address']}:{new_port}"

    if clean_path is not None or health_expected_status is not None or port:
        checks = [dict(c) for c in (row["health_checks"] or []) if isinstance(c, dict)]
        if not checks:
            checks = [{"kind": "http", "scheme": "http", "path": "/", "expect_status": 200}]
        for check in checks:
            if str(check.get("kind") or "http") != "http":
                continue
            if clean_path is not None:
                check["path"] = clean_path
                changed["health_path"] = clean_path
            if health_expected_status is not None:
                check["expect_status"] = check["expect"] = int(health_expected_status)
                changed["health_expected_status"] = int(health_expected_status)
            if port:
                check["port"] = int(port)
        connection.execute(
            sql("UPDATE deployment_targets SET health_checks = CAST(:h AS JSONB), "
                "updated_at = now() WHERE id = :i"),
            {"h": json.dumps(checks), "i": target_id})

    if env:
        for name, ref in sorted(env.items()):
            bind_secret_to_target(connection, target_id=target_id, name=str(name),
                                  secret_ref_id=str(ref), project_id=project_id, actor=actor)
        changed["env"] = sorted(env)

    if not changed:
        return {"ok": True, "target_id": target_id, "changed": {}, "note": "nothing to change"}
    if "port" in changed or "host_id" in changed or "public_url" in changed:
        from .stack_deploy import target_address

        updated_target = connection.execute(
            sql("SELECT * FROM deployment_targets WHERE id = :i"),
            {"i": target_id}).mappings().first()
        if updated_target and host is not None:
            fresh_open_url = target_address(dict(updated_target), host)
            if fresh_open_url:
                connection.execute(
                    sql("UPDATE stack_deployments SET open_url = :u, updated_at = now() "
                        "WHERE target_id = :t AND status IN "
                        "('planned', 'awaiting_approval', 'running')"),
                    {"u": fresh_open_url, "t": target_id})
    emit_event(connection, project_id, "deployment.target_updated", actor,
               "deployment_target", target_id, {"changed": changed})
    return {"ok": True, "target_id": target_id, "changed": changed,
            "note": "the target is updated; nothing was deployed. Redeploy to use it."}


def suggest_port(connection, host: dict[str, Any], *, project_id: str = "",
                 app_name: str = "") -> Optional[int]:
    """The port a new target on this host should get: the app's own earlier port when nobody
    holds it now, else the first free one in the host's range (port_min..port_max). The same
    choice `create_deployment_target` makes, offered BEFORE the create (the confirm sheets)."""
    address = str(host.get("address") or "")
    if not address:
        return None
    taken = _ports_assigned_on(connection, address)
    if project_id and app_name:
        had = _port_the_app_had(connection, project_id, app_name, address)
        if had and had not in taken:
            return had
    low = int(host.get("port_min") or 3000)
    high = int(host.get("port_max") or 65535)
    return _first_free_port(connection, address, start=low, end=high)


def create_deployment_target(
    connection, *, project_id: str, app_name: str, environment: str = "staging",
    repo: Optional[str] = None, host_id: Optional[str] = None, port: Optional[int] = None,
    actor: str = "boss", host: Optional[str] = None, health_path: Optional[str] = None,
    manifest_text: Optional[str] = None,
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

    if host and not host_id:
        if str(host).strip().lower() in _LOCAL_WORDS and is_local_only_install(connection, project_id):
            ensure_local_host(connection, project_id, actor=actor)
        # ASKED FOR A PLACE THAT IS NOT REGISTERED ("local" staging with only a remote host):
        # say so and offer the path, never quietly build somewhere else. A match (by id, name,
        # address, or "local" for this machine) SELECTS that host.
        matched, refusal = match_host(connection, project_id, host)
        if refusal:
            return refusal
        host_id = str(matched["id"])
    hosts = [dict(h) for h in list_hosts(connection, project_id)]
    if not host_id and not hosts and is_local_only_install(connection, project_id):
        ensured = ensure_local_host(connection, project_id, actor=actor)
        if ensured is not None:
            hosts = [dict(h) for h in list_hosts(connection, project_id)]
    verified = [h for h in hosts if h.get("status") == "verified"]
    if host_id:
        try:
            host = usable_host(connection, host_id, project_id)
        except HostNotUsable as refused:
            return {"ok": False, "error": f"{refused.headline} {refused.action}",
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
    repo_full = str(repo or "").strip()
    repo = repo_full.rsplit("/", 1)[-1]

    # A BARE IP HAS NO CERTIFICATE. Defaulting to https had a healthy app failing its gate with
    # WRONG_VERSION_NUMBER; the destination is known here, so this is a fact rather than a guess.
    looks_like_ip = address.count(".") == 3 and all(p.isdigit() for p in address.split("."))
    scheme = "http" if (looks_like_ip or ":" in address) else "https"

    if health_path and not valid_health_path(health_path):
        return {"ok": False, "error": "health_path must be a path such as /healthz, not a URL."}
    # THE APP SAYS WHERE IT IS HEALTHY. A hard-coded "/" failed every API with no root page.
    check_path, check_source = resolve_health_path(
        connection, project_id=project_id, repo=repo_full, app_name=app,
        health_path=health_path, manifest_text=manifest_text)

    from . import local_repo

    required_resources = [{"kind": "app_port", "name": str(port)}]
    if "/" in repo_full:
        repo_owner = repo_full.split("/", 1)[0]
        if local_repo.is_local_owner(repo_owner):
            required_resources.append({"kind": "repo_owner", "name": repo_owner})

    target_kind = "local" if is_builtin_local_host(host) else "ssh"
    target_id = _target_id()
    connection.execute(
        sql("INSERT INTO deployment_targets "
            "(id, project_id, app_name, repo, environment, target_kind, target_ref, domains, "
            " health_checks, required_resources, status, created_by, created_at, updated_at) "
            "VALUES (:i, :p, :a, :r, :e, :tk, :ref, CAST(:dom AS JSONB), "
            "        CAST(:hc AS JSONB), CAST(:rr AS JSONB), 'active', :by, now(), now())"),
        # THE APP PORT IS DECLARED, not only implied by the domain: the governed host plan
        # refuses a target with no app_port ("has no app port. Set the app's port…"), which
        # stopped the very first auto-created deploy.
        {"i": target_id, "p": project_id, "a": app, "r": repo, "e": environment,
         "tk": target_kind,
         "rr": json.dumps(required_resources),
         "ref": target_ref_for(host), "dom": json.dumps([f"{address}:{port}"]),
         "hc": json.dumps([{"kind": "http", "scheme": scheme, "path": check_path,
                            "expect_status": 200, "port": port}]),
         "by": actor})
    attached = attach_target(connection, target_id=target_id, host_id=host["id"])
    emit_event(connection, project_id, "deployment.target_created", actor,
               "deployment_target", target_id,
               {"app_name": app, "environment": environment, "host": host["name"],
                "domain": f"{address}:{port}", "target_kind": target_kind})

    return {"ok": True, "target_id": target_id, "app_name": app, "environment": environment,
            "host": host["name"], "address": address, "port": port, "target_kind": target_kind,
            "domain": f"{address}:{port}", "target_ref": attached.get("target_ref"),
            "arch": attached.get("arch"), "repo": repo,
            "health_check": f"{scheme}://{address}:{port}{check_path} expecting 200",
            "health_path": check_path, "health_path_source": check_source,
            **({"health_note": "no boss.deploy.yaml health path was found, so the check is "
                               "'/'. If the app has no root page, set the real path with "
                               "update_deployment_target (or pass health_path)."}
               if check_source == "default" else {}),
            "next": "Nothing has been deployed yet. Deploy the build to it with deploy_app."}

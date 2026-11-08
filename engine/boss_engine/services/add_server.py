"""ADD A SERVER: a server that sets itself up (docs/product/wireframes/agentic-factory/
3-add-a-server.html; AUDIT.md §2 "Hosts and capacity", §3 #3).

    one input  → read (the words) + probe (the network, before any credential)
               → the gate: the ONE human step, handing over access (BOSS's key, or a key you have)
               → Agent actions + six boxes → Add and set up
               → the server page: key landed → read → install → verify → ready → watching

ONE PATH, NOT A SECOND ONE. Every step is the existing host onboarding, called in order by the
agent instead of pressed by a person:

    record      deployment_fleet.register_host           (the Host sheet's Add host)
    access      deployment_fleet.generate_host_key       (Generate key)
                deployment_fleet.adopt_host_key          (Use my key)
                deployment_fleet._bind_key               (a stored key, by reference)
    key landed  deployment_fleet.verify_host             (I ran it / Verify: a real SSH session,
                                                          which also pins the host key)
    read        host_overview.CAPACITY_PROBE over        (the server page's capacity read)
                host_folder._ssh_runner
    install     deployment_fleet.provision_host          (Install Docker; same edition policy)
                observability_stack.node_exporter_run    (the monitoring stack's exporter)
                the infra plugin registry                (Caddy: the official.caddy plugin)
    verify      deployment_fleet.verify_host             (Verify)

The person never presses "I ran it": `advance` tries the key on its own (every few seconds while
it waits) and goes on the moment the key works.

APP-INDEPENDENT. Adding a server never creates an app target and never moves an app. A server has
a ROLE (staging, prod or builds), kept on the host's labels; each app picks servers on its own
Project › Deployments page, where a ready server shows up (project_experience/deployments.py).
Production is never picked for anyone.

AFTER SETUP the Infra agent owns the host (`watch_due`): it reads health on a clock, says when the
disk passes the line, and prunes Docker's BUILD CACHE alone when it may (cache, not data — below
the boundary in Where people decide). Anything outside (DNS) or on the list waits for a person.
"""
from __future__ import annotations

import ipaddress
import json
import re
import shlex
import socket
import subprocess
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

from sqlalchemy import text

from ..config import Settings
from .core import emit_event

KINDS = ("ssh", "dials_in", "cloud")
KIND_WORDS = {"ssh": "SSH server", "dials_in": "It dials in", "cloud": "Cloud account"}
ROLES = ("staging", "prod", "builds")
ROLE_WORDS = {"staging": "Staging pool", "prod": "Production", "builds": "Builds"}
ROLE_LINE = {"staging": "any project can deploy staging here; no app is moved",
             "prod": "a production server; each prod deploy still asks a person",
             "builds": "builds and tests run here; no app is moved"}
ACCESS = ("boss_key", "my_key", "stored_key")
BACKENDS = ("database", "file", "platform_secret_store")

#: The server page's diagram, in order (wireframe §2).
PHASES = ("key", "read", "install", "verify", "ready", "watching")
PHASE_WORDS = {"key": "Key landed", "read": "Read", "install": "Install", "verify": "Verify",
               "ready": "Ready", "watching": "Watching"}

#: How often the agent tries the key while it waits (then less often after the first half hour).
KEY_RETRY_S = 6
KEY_RETRY_SLOW_S = 60
STEP_RETRY_S = 20
#: The Infra agent's health read, once a server is watched.
WATCH_EVERY_S = 600
DISK_ALERT_PCT = 80
LOG_KEEP = 60

_IPV4 = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")
_HOST = re.compile(r"^(?=.{1,253}$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*"
                   r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
_USER = re.compile(r"^[a-z_][a-z0-9_.-]{0,31}$", re.I)


class ServerRefused(ValueError):
    """A refusal in words, with the HTTP status the route answers."""

    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.status = status


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: Any) -> Optional[str]:
    if isinstance(value, datetime):
        return (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).isoformat()
    return str(value) if value else None


def _j(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return {}
    return value if value is not None else {}


# ── 1 · the words ──────────────────────────────────────────────────────────────────────────

def _is_address(token: str) -> bool:
    token = token.strip("[]")
    if _IPV4.match(token):
        try:
            ipaddress.ip_address(token)
            return True
        except ValueError:
            return False
    try:
        ipaddress.IPv6Address(token)
        return True
    except ValueError:
        pass
    return bool(_HOST.match(token)) and ("." in token or token == "localhost")


def read_words(raw: str) -> dict[str, Any]:
    """What the one input names: an address, user@host, an ssh line, or words.

    Deterministic and instant (no model): the input is short and the shapes are few. Returns
    {kind, address, user, port, identity_file, role, name_hint, words, said}. A private key pasted
    here is refused before anything else: keys go through the gate, never the input.
    """
    text_ = str(raw or "")
    if "PRIVATE KEY" in text_:
        raise ServerRefused("Never paste a key here: hand it over below, under Let BOSS in.")
    words = " ".join(text_.split())
    low = words.lower()
    out: dict[str, Any] = {"kind": None, "address": None, "user": None, "port": None,
                           "identity_file": None, "role": None, "name_hint": None,
                           "words": words}
    try:
        tokens = shlex.split(text_.replace("\n", " "))
    except ValueError:
        tokens = text_.split()
    # an ssh line: ssh [-p N] [-i file] [-l user] [user@]host
    if tokens[:1] == ["ssh"]:
        i = 1
        while i < len(tokens):
            t = tokens[i]
            if t in ("-p", "-i", "-l", "-o") and i + 1 < len(tokens):
                val = tokens[i + 1]
                if t == "-p" and val.isdigit():
                    out["port"] = int(val)
                elif t == "-i":
                    out["identity_file"] = val
                elif t == "-l":
                    out["user"] = val
                i += 2
                continue
            if not t.startswith("-") and not out["address"]:
                user, _, host = t.rpartition("@")
                if _is_address(host):
                    out["address"] = host.strip("[]")
                    if user:
                        out["user"] = user
                break
            i += 1
    if not out["address"]:
        for t in tokens:
            t = t.strip(",.;")
            m = re.match(r"^(?:([^@\s]+)@)?(\[[0-9a-fA-F:]+\]|[^:\s@]+)(?::(\d{1,5}))?$", t)
            if not m:
                continue
            user, host, port = m.group(1), m.group(2).strip("[]"), m.group(3)
            if not _is_address(host) or (not user and not port and not re.search(r"\d", host)
                                         and host.count(".") < 1):
                continue
            # a sentence's word with a dot ("e.g.") is not a server
            if not user and not port and not _IPV4.match(host) and not re.search(r"\.[a-z]{2,}$", host, re.I):
                continue
            out["address"] = host
            if user:
                out["user"] = user
            if port:
                out["port"] = int(port)
            break
    if out["user"] and not _USER.match(out["user"]):
        out["user"] = None
    if out["port"] is not None and not (0 < int(out["port"]) < 65536):
        out["port"] = None
    # the role, from the words
    if re.search(r"\b(prod|production|live)\b", low):
        out["role"] = "prod"
    elif re.search(r"\b(builds?|ci|runner|test runner)\b", low):
        out["role"] = "builds"
    elif re.search(r"\bstag(e|ing)\b", low):
        out["role"] = "staging"
    # the kind
    if re.search(r"\b(dials? in|dial[- ]in|enrol+|behind (a )?nat|no inbound|hardware agent)\b", low):
        out["kind"] = "dials_in"
    elif not out["address"] and re.search(
            r"\b(aws|amazon|digitalocean|digital ocean|hetzner|gcp|google cloud|azure|linode|"
            r"vultr|cloud account|cloud box|a cloud)\b", low):
        out["kind"] = "cloud"
    elif out["address"]:
        out["kind"] = "ssh"
    named = re.search(r"\b(?:call(?:ed)? it|name(?:d)? it|named)\s+([A-Za-z0-9][\w.-]{0,40})", words)
    if named:
        out["name_hint"] = named.group(1).strip(".")
    return out


def provider_of(words: str) -> Optional[str]:
    low = (words or "").lower()
    for key, pat in (("aws", r"\b(aws|amazon)\b"), ("digitalocean", r"digital ?ocean"),
                     ("hetzner", r"hetzner"), ("gcp", r"\b(gcp|google cloud)\b"),
                     ("azure", r"\bazure\b")):
        if re.search(pat, low):
            return key
    return None


# ── 2 · the network probe (before any credential) ──────────────────────────────────────────

def _tcp(address: str, port: int, timeout: float = 2.5) -> bool:
    try:
        with socket.create_connection((address, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def _keyscan(address: str, port: int, timeout: int = 4) -> str:
    """The host's public key as ssh-keyscan prints it (known_hosts lines); '' when none."""
    try:
        res = subprocess.run(["ssh-keyscan", "-T", str(timeout), "-p", str(int(port)),
                              "-t", "ed25519,ecdsa,rsa", address],
                             capture_output=True, text=True, timeout=timeout + 3, check=False)
        return res.stdout or ""
    except (OSError, subprocess.SubprocessError):
        return ""


def probe(address: str, port: int = 22) -> dict[str, Any]:
    """What the network says before any credential: is the SSH port open, which host key does
    the machine show, and are 80 / 443 free. Read-only; nothing is stored."""
    from .host_key_pin import fingerprint

    if not address or not _is_address(address):
        raise ServerRefused("That is not an address BOSS can reach. Try user@host or an IP.")
    port = int(port or 22)
    open_ = _tcp(address, port)
    key_line, fp, key_type = "", None, None
    if open_:
        key_line = _keyscan(address, port)
        fp = fingerprint(key_line)
        for line in key_line.splitlines():
            parts = line.split()
            if len(parts) >= 3 and not line.startswith("#"):
                key_type = parts[1]
                break
    web = {str(p): ("open" if _tcp(address, p, 1.5) else "free") for p in (80, 443)} if open_ else {}
    if open_ and fp:
        said = f"port {port} open, host key {fp}"
    elif open_:
        said = f"port {port} open, no host key shown yet"
    else:
        said = f"port {port} does not answer from BOSS"
    return {"address": address, "port": port, "port_open": open_, "host_key": fp,
            "host_key_type": key_type, "web": web, "said": said, "at": _now().isoformat()}


# ── the read the sheet makes (words + probe + what is stored) ───────────────────────────────

def _next_name(connection, project_id: str) -> str:
    names = {str(r[0]) for r in connection.execute(text(
        "SELECT name FROM deployment_hosts WHERE project_id = :p"), {"p": project_id})}
    n = 1
    while f"boss-{n}" in names:
        n += 1
    return f"boss-{n}"


#: An SSH private key in the vault (secrets_service stores the kind as private_key and keeps the
#: detail in metadata; older rows say ssh_private_key outright).
_SSH_KEY_SQL = ("status = 'active' AND (secret_kind = 'ssh_private_key' OR (secret_kind = 'private_key' "
                "AND COALESCE(metadata->>'secret_kind_detail', 'ssh_private_key') = 'ssh_private_key'))")


def _key_name(ref_key: str, project_id: str) -> str:
    """BOSS_HELPDESK_DEMO_DEPLOY_KEY_2 → deploy-key-2: the name a person gave it."""
    prefix = "BOSS_" + re.sub(r"[^A-Z0-9]+", "_", project_id.upper()) + "_"
    name = ref_key[len(prefix):] if ref_key.upper().startswith(prefix) else ref_key
    return name.split("/")[-1].lower().replace("_", "-") or ref_key


def stored_keys(connection, project_id: str) -> list[dict[str, Any]]:
    """SSH private keys stored in the vault for this project: names only, never a value."""
    rows = connection.execute(text(
        # The project's own keys (a deploy key someone stored), not each server's BOSS key.
        f"SELECT id, ref_key, metadata FROM secret_refs WHERE project_id = :p AND scope_id = :p "
        f"AND {_SSH_KEY_SQL} ORDER BY created_at DESC LIMIT 8"), {"p": project_id}).mappings().all()
    out = []
    for r in rows:
        meta = _j(r["metadata"]) or {}
        out.append({"id": r["id"], "name": _key_name(str(r["ref_key"] or r["id"]), project_id),
                    "fingerprint": meta.get("fingerprint")})
    return out


def understand(connection, settings: Settings, project_id: str, raw: str, *,
               kind: Optional[str] = None, run_probe: bool = True,
               prober: Optional[Callable[[str, int], dict[str, Any]]] = None) -> dict[str, Any]:
    """The sheet's read: the words, the network probe, a suggested name, the stored keys, the
    plan the agent will follow, and the preconditions (outward SSH, host writes)."""
    from .deployment_fleet import direct_host_writes_allowed, outward_ssh_allowed

    w = read_words(raw)
    k = kind if kind in KINDS else (w["kind"] or "ssh")
    pr = None
    if k == "ssh" and w["address"] and run_probe:
        pr = (prober or probe)(w["address"], int(w["port"] or 22))
    name = w["name_hint"] or _next_name(connection, project_id)
    role = w["role"] or "staging"
    writes = direct_host_writes_allowed(settings)
    return {
        "kind": k, "kind_words": KIND_WORDS[k], "address": w["address"], "user": w["user"] or
        ("root" if w["address"] else None),
        "user_detected": bool(w["user"]), "port": int(w["port"] or 22),
        "identity_file": w["identity_file"], "role": role, "role_words": ROLE_WORDS[role],
        "role_line": ROLE_LINE[role], "name": name, "words": w["words"], "probe": pr,
        "provider": provider_of(w["words"]) if k == "cloud" else None,
        "stored_keys": stored_keys(connection, project_id),
        "plan": plan_lines({"docker": True, "exporter": True, "caddy": True},
                           {"host_health": True}, role),
        "preconditions": {"outward_ssh": outward_ssh_allowed(settings),
                          "host_writes": writes,
                          "host_writes_words": None if writes else (
                              "This edition does not install software on servers: the agent "
                              "reads and verifies, and says what to install.")},
    }


def plan_lines(setup: dict[str, Any], watch: dict[str, Any], role: str) -> list[str]:
    """Then the agent will…: the Agent actions card's steps, from the boxes."""
    lines = ["Read OS, CPU, memory, disk, arch"]
    want = [w for w, on in (("Docker", setup.get("docker")), ("node-exporter", setup.get("exporter")),
                            ("Caddy for TLS", setup.get("caddy"))) if on]
    if want:
        lines.append(f"Install {', '.join(want[:-1]) + ' and ' + want[-1] if len(want) > 1 else want[0]}"
                     " (if missing)")
    lines.append("Verify it can build and run a container")
    lines.append({"staging": "Add it to the staging pool, with its free ports",
                  "prod": "Offer it for production (each prod deploy still asks you)",
                  "builds": "Add it to the build pool"}[role])
    if watch.get("host_health", True):
        lines.append("Start watching host health (disk, Docker, certs)")
    return lines


# ── 3 · Add and set up ─────────────────────────────────────────────────────────────────────

def _log(connection, host_id: str, line: str, *, who: str = "Infra agent") -> None:
    connection.execute(text(
        "UPDATE server_setups SET log = (CASE WHEN jsonb_array_length(log) >= :keep "
        "THEN log - 0 ELSE log END) || jsonb_build_array(jsonb_build_object('at', CAST(:at AS TEXT), "
        "'who', CAST(:who AS TEXT), 'line', CAST(:line AS TEXT))), updated_at = now() "
        "WHERE host_id = :h"),
        {"h": host_id, "at": _now().isoformat(), "who": who, "line": line[:400], "keep": LOG_KEEP})


def _step(connection, host_id: str, key: str, state: str, detail: Optional[str] = None) -> None:
    connection.execute(text(
        "UPDATE server_setups SET steps = jsonb_set(steps, CAST(:path AS TEXT[]), "
        "CAST(:v AS JSONB), true), updated_at = now() WHERE host_id = :h"),
        {"h": host_id, "path": "{" + key + "}",
         "v": json.dumps({"state": state, "detail": detail, "at": _now().isoformat()})})


def create(connection, settings: Settings, *, project_id: str, actor: str,
           spec: dict[str, Any]) -> dict[str, Any]:
    """Record the server, hand over access the way the person chose, and start the setup.

    spec: address, ssh_user, ssh_port, name, arch?, admin_identity_file?, role, port_min?,
    port_max?, max_apps?, access {mode, backend, private_key?, secret_ref_id?},
    setup {docker, exporter, caddy}, watch {host_health, disk_pct}, probe?.
    Nothing is installed here: the agent goes on when the key lands (`advance`).
    """
    from sqlalchemy.exc import IntegrityError

    from .deployment_fleet import (_bind_key, adopt_host_key, generate_host_key, get_host,
                                   register_host)

    address = str(spec.get("address") or "").strip()
    if not address or not _is_address(address):
        raise ServerRefused("Not added: name the machine first (an address, user@host or an ssh line).")
    name = str(spec.get("name") or "").strip() or _next_name(connection, project_id)
    if not re.match(r"^[A-Za-z0-9][\w.-]{0,62}$", name):
        raise ServerRefused("Not added: a name is letters, digits, dots and dashes.")
    user = str(spec.get("ssh_user") or "root").strip()
    if not _USER.match(user):
        raise ServerRefused("Not added: that SSH user is not a valid user name.")
    try:
        port = int(spec.get("ssh_port") or 22)
    except (TypeError, ValueError):
        port = 0
    if not 0 < port < 65536:
        raise ServerRefused("Not added: the SSH port is a number from 1 to 65535.")
    role = str(spec.get("role") or "staging")
    if role not in ROLES:
        raise ServerRefused("Not added: a server's role is staging, prod or builds.")
    access = dict(spec.get("access") or {})
    mode = str(access.get("mode") or "boss_key")
    if mode not in ACCESS:
        raise ServerRefused("Not added: pick one way to let BOSS in.")
    backend = str(access.get("backend") or "database")
    if backend not in BACKENDS:
        raise ServerRefused("Not added: the key is kept in the database, a file or the secret store.")
    private_key = str(access.pop("private_key", "") or "")
    if mode == "my_key" and "PRIVATE KEY" not in private_key:
        raise ServerRefused("Not added: paste the PRIVATE half of a key that already opens this "
                            "server (it starts with -----BEGIN … PRIVATE KEY-----).")
    secret_ref_id = str(access.get("secret_ref_id") or "")
    if mode == "stored_key":
        row = connection.execute(text(
            f"SELECT id FROM secret_refs WHERE id = :s AND project_id = :p AND {_SSH_KEY_SQL}"),
            {"s": secret_ref_id, "p": project_id}).first()
        if not row:
            raise ServerRefused("Not added: pick a stored SSH key from this project's vault.")
    setup = {k: bool((spec.get("setup") or {}).get(k, True)) for k in ("docker", "exporter", "caddy")}
    watch_in = spec.get("watch") or {}
    try:
        disk_pct = int(watch_in.get("disk_pct") or DISK_ALERT_PCT)
    except (TypeError, ValueError):
        disk_pct = DISK_ALERT_PCT
    watch = {"host_health": bool(watch_in.get("host_health", True)),
             "disk_pct": max(50, min(95, disk_pct)),
             "prune_cache": bool(watch_in.get("prune_cache", True))}
    try:
        port_min = int(spec.get("port_min") or 8000)
        port_max = int(spec.get("port_max") or 8999)
    except (TypeError, ValueError):
        raise ServerRefused("Not added: the app port range is two numbers.") from None
    if not (1024 <= port_min <= port_max <= 65535):
        raise ServerRefused("Not added: the app port range runs from 1024 to 65535, low to high.")
    try:
        with connection.begin_nested():
            host = register_host(
                connection, project_id=project_id, name=name, address=address, ssh_user=user,
                ssh_port=port, arch=(str(spec.get("arch") or "").strip() or None),
                labels={"role": role, "added_by": "add_a_server"},
                port_min=port_min, port_max=port_max,
                max_apps=(int(spec["max_apps"]) if str(spec.get("max_apps") or "").isdigit() else None),
                notes=(str(spec.get("notes") or "").strip() or None),
                admin_identity_file=(str(spec.get("admin_identity_file") or "").strip() or None),
                actor=actor)
    except IntegrityError:
        raise ServerRefused(f"Not added: this project already has a server named {name!r}. "
                            "Pick another name in the Name box.", 409) from None
    host_id = host["id"]
    if mode == "boss_key":
        generate_host_key(connection, settings, host_id=host_id, backend=backend, actor=actor)
    elif mode == "my_key":
        adopt_host_key(connection, settings, host_id=host_id, private_key=private_key,
                       backend=backend, actor=actor)
    else:
        _bind_key(connection, host_id, secret_ref_id)
    private_key = ""
    connection.execute(text(
        "INSERT INTO server_setups (host_id, project_id, kind, role, access, setup, watch, owner, "
        "phase, probe, next_attempt_at, created_by) VALUES (:h, :p, 'ssh', :r, :a, CAST(:s AS JSONB), "
        "CAST(:w AS JSONB), 'infra', 'key', CAST(:pr AS JSONB), now(), :by)"),
        {"h": host_id, "p": project_id, "r": role, "a": mode, "s": json.dumps(setup),
         "w": json.dumps(watch), "pr": json.dumps(spec.get("probe") or {}), "by": actor})
    how = {"boss_key": "BOSS made its key; run the one line on the server",
           "my_key": "your key is in the vault (write-only)",
           "stored_key": "a stored key from the vault"}[mode]
    _log(connection, host_id, f"Added {name} ({address}) for {ROLE_WORDS[role].lower()} · {how}",
         who=actor)
    _step(connection, host_id, "key", "now", "waiting for the key" if mode == "boss_key"
          else "trying your key")
    emit_event(connection, project_id, "server.added", actor, "deployment_host", host_id,
               {"host": name, "address": address, "role": role, "access": mode})
    assert get_host(connection, host_id)
    return {"host_id": host_id, "name": name}


# ── 4 · the agent: one step at a time ──────────────────────────────────────────────────────

def _setup_row(connection, host_id: str) -> Optional[dict[str, Any]]:
    row = connection.execute(text("SELECT * FROM server_setups WHERE host_id = :h"),
                             {"h": host_id}).mappings().first()
    if not row:
        return None
    r = dict(row)
    for k in ("setup", "watch", "facts", "steps", "probe"):
        r[k] = _j(r.get(k)) or {}
    r["log"] = _j(r.get("log")) or []
    if not isinstance(r["log"], list):
        r["log"] = []
    return r


def claim(connection, host_id: str, *, force: bool = False) -> bool:
    """Take this setup's next attempt (one at a time across the worker and the page)."""
    got = connection.execute(text(
        "UPDATE server_setups SET next_attempt_at = now() + interval '45 seconds', "
        "attempts = attempts + 1 WHERE host_id = :h AND phase NOT IN ('watching') "
        "AND (:f OR next_attempt_at IS NULL OR next_attempt_at <= now()) RETURNING host_id"),
        {"h": host_id, "f": bool(force)}).first()
    return bool(got)


def _later(connection, host_id: str, seconds: int) -> None:
    connection.execute(text(
        "UPDATE server_setups SET next_attempt_at = now() + make_interval(secs => :s) "
        "WHERE host_id = :h"), {"h": host_id, "s": int(seconds)})


def _phase(connection, host_id: str, phase: str, blocked: Optional[str] = None) -> None:
    connection.execute(text(
        "UPDATE server_setups SET phase = :ph, blocked = :b, updated_at = now() WHERE host_id = :h"),
        {"h": host_id, "ph": phase, "b": blocked})


#: The read the agent runs once the key works: the capacity probe the server page already uses,
#: plus arch, Docker, node-exporter and the ports listening on the machine.
EXTRA_PROBE = (
    "echo \"BOSS_X arch $(uname -m 2>/dev/null)\"; "
    "(docker --version 2>/dev/null | sed 's/^/BOSS_X docker /') || true; "
    "(docker ps --format '{{.Names}}' 2>/dev/null | grep -q '^boss-node-exporter$' "
    "&& echo 'BOSS_X exporter running') || true; "
    "(ss -ltnH 2>/dev/null || netstat -ltn 2>/dev/null) | awk '{print $4}' "
    "| sed -E 's/.*:([0-9]+)$/BOSS_X listen \\1/' | grep '^BOSS_X listen' | head -400; true")


def parse_extra(out: str) -> dict[str, Any]:
    facts: dict[str, Any] = {"listening": []}
    for line in (out or "").splitlines():
        parts = line.strip().split(" ", 2)
        if parts[:1] != ["BOSS_X"] or len(parts) < 3:
            continue
        key, val = parts[1], parts[2].strip()
        if key == "arch":
            facts["arch"] = val[:20]
        elif key == "docker":
            m = re.search(r"version ([0-9][\w.]*)", val)
            facts["docker"] = m.group(1) if m else val[:40]
        elif key == "exporter":
            facts["exporter"] = True
        elif key == "listen" and val.isdigit():
            facts["listening"].append(int(val))
    return facts


def _runner(settings: Settings):
    from .project_experience.host_folder import _ssh_runner

    return _ssh_runner(settings)


def read_machine(connection, settings: Settings, host: dict[str, Any]) -> dict[str, Any]:
    """Ask the machine what it is (read-only): OS, CPUs, memory, disk, arch, Docker, exporter,
    listening ports. Kept on the host record as the server page's capacity too."""
    from .project_experience.host_folder import run_read
    from .project_experience.host_overview import CAPACITY_PROBE, capacity_from, store_capacity

    code, out, err = run_read(host, CAPACITY_PROBE + "; " + EXTRA_PROBE, _runner(settings))
    cap = capacity_from(out)
    if cap is None:
        raise ServerRefused(f"{host['name']} did not answer the read "
                            f"({(err or '').strip()[:160] or f'exit {code}'}).", 502)
    store_capacity(connection, host["id"], cap)
    facts = {**{k: v for k, v in cap.items() if k != "images"}, **parse_extra(out)}
    images = cap.get("images") or []
    facts["exporter"] = bool(facts.get("exporter")) or any("node-exporter" in i for i in images)
    if facts.get("arch"):
        connection.execute(text("UPDATE deployment_hosts SET arch = :a WHERE id = :h"),
                           {"a": facts["arch"], "h": host["id"]})
    return facts


def _caddy_on(connection) -> Optional[bool]:
    """Is the Caddy plugin usable (the infra plugin registry, as Infra › Plugins shows it)?"""
    try:
        from ..platform.registry import Registry

        plugin = next((p for p in Registry.load(connection).plugins if "caddy" in p.id), None)
        return None if plugin is None else bool(plugin.usable)
    except Exception:  # noqa: BLE001 — no registry on this install: unknown, never a blocker
        return None


def _gb(kb: Any) -> Optional[str]:
    try:
        gb = int(kb) / (1024 * 1024)
    except (TypeError, ValueError):
        return None
    return f"{round(gb)} GB" if gb >= 10 else f"{gb:.1f} GB"


def advance(connection, settings: Settings, host_id: str) -> dict[str, Any]:
    """One turn of the agent on this server: from where it is, as far as it can go now.

    Each step is the existing onboarding call (see the module docstring). A step that cannot go
    on says why in words (`blocked`) and is tried again later; nothing waits for "I ran it".
    """
    from .deployment_fleet import (direct_host_writes_allowed, get_host, outward_ssh_allowed,
                                   provision_host, run_on_host, verify_host)

    s = _setup_row(connection, host_id)
    host = get_host(connection, host_id)
    if not s or not host:
        raise ServerRefused("No such server setup.", 404)
    if host.get("status") == "retired":
        _phase(connection, host_id, s["phase"], "The server is retired.")
        return {"phase": s["phase"], "blocked": "retired"}
    if not outward_ssh_allowed(settings):
        why = ("BOSS is configured not to reach out over SSH (BOSS_SSH_OUTWARD_ALLOWED=false), "
               "so it cannot see the key land.")
        _phase(connection, host_id, s["phase"], why)
        _later(connection, host_id, KEY_RETRY_SLOW_S)
        return {"phase": s["phase"], "blocked": why}
    phase = s["phase"]
    for _ in range(len(PHASES)):
        if phase == "key":
            report = verify_host(connection, settings, host_id=host_id)
            if not report.get("reachable"):
                kinds = [f.get("kind") for f in report.get("findings") or []]
                waiting = (not kinds) or kinds == ["key_not_installed"] or "key_not_installed" in kinds
                words = None
                if not waiting:
                    f = (report.get("findings") or [{}])[0]
                    words = (f.get("remedy") or f.get("detail") or "The key did not work.")[:300]
                elif s["access"] != "boss_key":
                    words = ("Your key did not open the server. Check it is the key for "
                             f"{host['ssh_user']}@{host['address']}, or run BOSS's line instead.")
                _phase(connection, host_id, "key", words)
                age = (_now() - (s["created_at"] if s["created_at"].tzinfo else
                                 s["created_at"].replace(tzinfo=timezone.utc))).total_seconds()
                _later(connection, host_id, KEY_RETRY_S if age < 1800 else KEY_RETRY_SLOW_S)
                return {"phase": "key", "blocked": words}
            connection.execute(text(
                "UPDATE server_setups SET key_landed_at = now() WHERE host_id = :h"), {"h": host_id})
            who = "you" if s["access"] == "boss_key" else "your key"
            _step(connection, host_id, "key", "done", who)
            _log(connection, host_id, "Key landed: BOSS opened its own session"
                 + (f" · host key {host.get('host_key_fingerprint')}" if host.get("host_key_fingerprint") else ""))
            phase = "read"
            _phase(connection, host_id, phase)
            host = get_host(connection, host_id) or host
            continue
        if phase == "read":
            _step(connection, host_id, "read", "now", "reading the machine")
            try:
                facts = read_machine(connection, settings, host)
            except Exception as error:  # noqa: BLE001 — said, tried again
                why = str(error)[:300]
                _step(connection, host_id, "read", "now", why)
                _phase(connection, host_id, "read", why)
                _later(connection, host_id, STEP_RETRY_S)
                return {"phase": "read", "blocked": why}
            connection.execute(text(
                "UPDATE server_setups SET facts = CAST(:f AS JSONB) WHERE host_id = :h"),
                {"f": json.dumps(facts), "h": host_id})
            detail = " · ".join(x for x in (facts.get("os"), facts.get("arch"),
                                            f"{facts['cpus']} vCPU" if facts.get("cpus") else None,
                                            _gb(facts.get("mem_kb"))) if x)
            _step(connection, host_id, "read", "done", detail or "read")
            _log(connection, host_id, f"Read the machine: {detail or 'it answered'}")
            s["facts"] = facts
            phase = "install"
            _phase(connection, host_id, phase)
            continue
        if phase == "install":
            facts = s["facts"] or {}
            if s.get("blocked"):
                # it waited for something to appear on the machine: look again before deciding
                try:
                    facts = {**facts, **read_machine(connection, settings, host)}
                except Exception:  # noqa: BLE001 — the last read stands; said below if still missing
                    pass
            want = s["setup"] or {}
            writes = direct_host_writes_allowed(settings)
            done: list[str] = []
            todo: list[str] = []
            if want.get("docker", True):
                if facts.get("docker"):
                    done.append(f"Docker {facts['docker']}")
                elif writes:
                    _step(connection, host_id, "install", "now", "installing Docker")
                    _log(connection, host_id, "Installing Docker from the distribution's packages")
                    try:
                        provision_host(connection, settings, host_id=host_id, actor="agent:infra")
                    except (ValueError, PermissionError) as error:
                        why = f"Docker did not install: {str(error)[:240]}"
                        _step(connection, host_id, "install", "now", why)
                        _phase(connection, host_id, "install", why)
                        _later(connection, host_id, STEP_RETRY_S * 3)
                        return {"phase": "install", "blocked": why}
                    facts = {**facts, **read_machine(connection, settings, host)}
                    done.append(f"Docker {facts.get('docker') or 'installed'}")
                else:
                    todo.append("Docker")
            if want.get("exporter", True):
                if facts.get("exporter"):
                    done.append("node-exporter")
                elif writes and facts.get("docker"):
                    from .observability_stack import node_exporter_run

                    _log(connection, host_id, "Starting node-exporter (the monitoring stack's exporter)")
                    try:
                        res = run_on_host(connection, settings, host,
                                          "docker network create boss-obs >/dev/null 2>&1 || true; "
                                          "docker rm -f boss-node-exporter >/dev/null 2>&1 || true; "
                                          + node_exporter_run(), timeout=180)
                        ok = getattr(res, "returncode", 1) == 0
                    except ValueError:
                        ok = False
                    done.append("node-exporter" if ok else "node-exporter (did not start; health is read over SSH)")
                else:
                    done.append("node-exporter: later (health is read over SSH meanwhile)")
            if want.get("caddy", True):
                on = _caddy_on(connection)
                done.append("Caddy plugin on (TLS per app)" if on is not False
                            else "Caddy plugin is off (Infra › Plugins)")
            if todo:
                why = (f"{' and '.join(todo)} missing, and this edition does not install software on "
                       f"servers. Install it on {host['address']}; the agent sees it and goes on.")
                _step(connection, host_id, "install", "now", why)
                _phase(connection, host_id, "install", why)
                # look again for it on the next turn
                try:
                    s["facts"] = {**facts, **read_machine(connection, settings, host)}
                    connection.execute(text(
                        "UPDATE server_setups SET facts = CAST(:f AS JSONB) WHERE host_id = :h"),
                        {"f": json.dumps(s["facts"]), "h": host_id})
                except Exception:  # noqa: BLE001
                    pass
                _later(connection, host_id, STEP_RETRY_S)
                return {"phase": "install", "blocked": why}
            connection.execute(text(
                "UPDATE server_setups SET facts = CAST(:f AS JSONB) WHERE host_id = :h"),
                {"f": json.dumps(facts), "h": host_id})
            _step(connection, host_id, "install", "done", " · ".join(_short(x) for x in done) or "nothing to install")
            _log(connection, host_id, "Set up: " + (" · ".join(done) or "nothing was missing"))
            phase = "verify"
            _phase(connection, host_id, phase)
            continue
        if phase == "verify":
            _step(connection, host_id, "verify", "now", "build + run")
            report = verify_host(connection, settings, host_id=host_id)
            if report.get("status") != "verified":
                f = (report.get("findings") or [{}])[0]
                why = (f.get("remedy") or f.get("detail") or "Verify did not pass.")[:300]
                _step(connection, host_id, "verify", "now", why)
                _phase(connection, host_id, "verify", why)
                _later(connection, host_id, STEP_RETRY_S)
                return {"phase": "verify", "blocked": why}
            _step(connection, host_id, "verify", "done", "build + run")
            _log(connection, host_id, "Verified: Docker answers and BOSS can run a container")
            phase = "ready"
            _phase(connection, host_id, phase)
            continue
        if phase == "ready":
            free = free_ports(connection, host, s["facts"])
            role = s["role"]
            where = {"staging": "in the staging pool", "prod": "offered for production",
                     "builds": "in the build pool"}[role]
            connection.execute(text("UPDATE server_setups SET ready_at = COALESCE(ready_at, now()) "
                                    "WHERE host_id = :h"), {"h": host_id})
            _step(connection, host_id, "ready", "done", where)
            _log(connection, host_id, f"Ready {where}, {free} free ports; each app picks it on its "
                                      "own Deployments page")
            emit_event(connection, host["project_id"], "server.ready", "agent:infra",
                       "deployment_host", host_id, {"host": host["name"], "role": role})
            if (s["watch"] or {}).get("host_health", True):
                phase = "watching"
                connection.execute(text(
                    "UPDATE server_setups SET watch_next_at = now() + make_interval(secs => :s) "
                    "WHERE host_id = :h"), {"h": host_id, "s": WATCH_EVERY_S})
                _step(connection, host_id, "watching", "done", "disk · Docker · certs")
                _log(connection, host_id, "Watching host health: disk, Docker, certs")
                _phase(connection, host_id, phase)
            else:
                _phase(connection, host_id, "ready")
                connection.execute(text("UPDATE server_setups SET next_attempt_at = NULL "
                                        "WHERE host_id = :h"), {"h": host_id})
            return {"phase": phase, "blocked": None}
        break
    return {"phase": phase, "blocked": None}


_SHORT = (("node-exporter: later", "exporter later"), ("node-exporter (did not start", "exporter failed"),
          ("node-exporter", "exporter"), ("Caddy plugin on", "Caddy on"), ("Caddy plugin is off", "Caddy off"))


def _short(line: str) -> str:
    """A setup line as the diagram's few words (the log keeps the whole sentence)."""
    for start, word in _SHORT:
        if line.startswith(start):
            return word
    return line


def free_ports(connection, host: dict[str, Any], facts: Optional[dict[str, Any]]) -> int:
    from .deployment_fleet import _ports_assigned_on

    lo, hi = int(host.get("port_min") or 8000), int(host.get("port_max") or 8999)
    used = set(_ports_assigned_on(connection, host["address"]).keys())
    used |= {int(p) for p in (facts or {}).get("listening") or [] if str(p).isdigit()}
    return max(0, (hi - lo + 1) - len([p for p in used if lo <= p <= hi]))


# ── 5 · the Infra agent's watch ────────────────────────────────────────────────────────────

def watch_one(connection, settings: Settings, host_id: str) -> dict[str, Any]:
    """Read one watched server's health; act alone below the line (prune build cache), say it."""
    from .deployment_fleet import direct_host_writes_allowed, get_host, run_on_host

    s = _setup_row(connection, host_id)
    host = get_host(connection, host_id)
    if not s or not host or host.get("status") == "retired":
        return {"skipped": True}
    connection.execute(text(
        "UPDATE server_setups SET watch_next_at = now() + make_interval(secs => :s) WHERE host_id = :h"),
        {"h": host_id, "s": WATCH_EVERY_S})
    try:
        facts = read_machine(connection, settings, host)
    except Exception as error:  # noqa: BLE001
        _log(connection, host_id, f"Health read failed: {str(error)[:200]}")
        emit_event(connection, host["project_id"], "server.unreachable", "agent:infra",
                   "deployment_host", host_id, {"host": host["name"]})
        return {"ok": False}
    connection.execute(text("UPDATE server_setups SET facts = facts || CAST(:f AS JSONB) WHERE host_id = :h"),
                       {"f": json.dumps(facts), "h": host_id})
    line = int((s["watch"] or {}).get("disk_pct") or DISK_ALERT_PCT)
    pct = facts.get("disk_pct")
    out: dict[str, Any] = {"ok": True, "disk_pct": pct}
    if pct is not None and pct >= line:
        emit_event(connection, host["project_id"], "server.disk_high", "agent:infra",
                   "deployment_host", host_id, {"host": host["name"], "disk_pct": pct, "line": line})
        _log(connection, host_id, f"Disk {pct}% (line {line}%)")
        if (s["watch"] or {}).get("prune_cache", True) and direct_host_writes_allowed(settings) \
                and facts.get("docker"):
            # BUILD CACHE ONLY: cache, not data — never images in use, volumes or containers.
            try:
                run_on_host(connection, settings, host, "docker builder prune -af >/dev/null 2>&1; true",
                            timeout=300)
                after = read_machine(connection, settings, host)
                _log(connection, host_id, f"Pruned Docker build cache alone: disk {pct}% → "
                                          f"{after.get('disk_pct')}% (cache, not data)")
                emit_event(connection, host["project_id"], "server.cache_pruned", "agent:infra",
                           "deployment_host", host_id,
                           {"host": host["name"], "before": pct, "after": after.get("disk_pct")})
                out["pruned"] = True
            except ValueError as error:
                _log(connection, host_id, f"Could not prune: {str(error)[:200]}")
    return out


def tick(engine, settings: Settings, *, limit: int = 20) -> dict[str, Any]:
    """The worker's clock: advance every setup that is due, then read every watched server
    that is due. Each in its own transaction, so one slow machine never holds the rest. An
    archived project's servers are left alone (projects_archive.live_sql)."""
    from .projects_archive import live_sql

    with engine.begin() as c:
        due = [r[0] for r in c.execute(text(
            "SELECT host_id FROM server_setups WHERE phase NOT IN ('watching') "
            "AND next_attempt_at IS NOT NULL AND next_attempt_at <= now() "
            f"AND {live_sql('server_setups.project_id')} "
            "ORDER BY next_attempt_at LIMIT :n"), {"n": limit})]
        watch = [r[0] for r in c.execute(text(
            "SELECT host_id FROM server_setups WHERE phase = 'watching' "
            "AND (watch_next_at IS NULL OR watch_next_at <= now()) "
            f"AND {live_sql('server_setups.project_id')} ORDER BY watch_next_at "
            "NULLS FIRST LIMIT :n"), {"n": limit})]
    advanced = watched = 0
    for h in due:
        try:
            with engine.begin() as c:
                if claim(c, h):
                    advance(c, settings, h)
                    advanced += 1
        except Exception:  # noqa: BLE001 — one machine never stops the clock
            continue
    for h in watch:
        try:
            with engine.begin() as c:
                watch_one(c, settings, h)
                watched += 1
        except Exception:  # noqa: BLE001
            continue
    return {"advanced": advanced, "watched": watched}


# ── 6 · the server page ────────────────────────────────────────────────────────────────────

def view(connection, settings: Settings, host_id: str) -> dict[str, Any]:
    """The server page's setup: the diagram, the Now line, the gate while the key is awaited,
    the stats (memory, disk, free ports), the log, and who owns it."""
    from .deployment_fleet import get_host, install_variants_for
    from .infra_view import authorize_line

    s = _setup_row(connection, host_id)
    host = get_host(connection, host_id)
    if not s or not host:
        raise ServerRefused("This server was not added through Add a server.", 404)
    phase = s["phase"]
    steps = []
    reached = PHASES.index(phase) if phase in PHASES else 0
    for i, key in enumerate(PHASES):
        st = (s["steps"] or {}).get(key) or {}
        state = st.get("state") or ("done" if i < reached else "now" if i == reached else "todo")
        if i < reached:
            state = "done"
        if key == phase and phase != "watching":
            state = "blocked" if s.get("blocked") else "now"
        if key == "watching" and phase == "watching":
            state = "done"
        if key == "ready" and phase in ("ready", "watching"):
            state = "done"
        detail = st.get("detail")
        if key == "key" and state != "done":
            detail = "waiting for the key" if s["access"] == "boss_key" else "trying your key"
        if key == "key" and state == "done" and s.get("key_landed_at") and s.get("created_at"):
            secs = int((s["key_landed_at"] - s["created_at"]).total_seconds())
            detail = f"{st.get('detail') or 'you'} · {secs // 60}:{secs % 60:02d}"
        if key == "ready" and state == "todo":
            detail = {"staging": "in the staging pool", "prod": "offered for production",
                      "builds": "in the build pool"}[s["role"]]
        if key == "watching" and state == "todo":
            detail = "disk · Docker · certs"
        if key == "verify" and state == "todo":
            detail = "build + run"
        steps.append({"key": key, "label": PHASE_WORDS[key], "state": state, "detail": detail,
                      "at": st.get("at")})
    if s.get("blocked"):
        now = {"text": s["blocked"], "tone": "amber"}
    elif phase == "key":
        now = {"text": ("Waiting for the key: run the one line on the server; the agent sees it land"
                        if s["access"] == "boss_key" else "Trying your key"), "tone": "amber"}
    elif phase == "watching":
        now = {"text": f"Ready · {ROLE_WORDS[s['role']].lower()} · the Infra agent watches disk, Docker and certs",
               "tone": "green"}
    elif phase == "ready":
        now = {"text": f"Ready · {ROLE_WORDS[s['role']].lower()}", "tone": "green"}
    else:
        last = (s["log"] or [{}])[-1].get("line") if s["log"] else None
        now = {"text": last or PHASE_WORDS[phase], "tone": "accent"}
    gate = None
    if phase == "key" and s["access"] == "boss_key" and host.get("key_public"):
        variants = install_variants_for(host)
        gate = {"user": host.get("ssh_user"), "address": host.get("address"),
                "line": authorize_line(host.get("key_public")),
                "from_laptop": variants.get("from_laptop"),
                "cloud_init": variants.get("cloud_init")}
    facts = s["facts"] or {}
    stats = {
        "memory": _gb(facts.get("mem_kb")), "cpus": facts.get("cpus"),
        "arch": facts.get("arch") or host.get("arch"), "os": facts.get("os"),
        "disk_free": (_gb(int(facts["disk_kb"]) * (100 - int(facts.get("disk_pct") or 0)) // 100)
                      if facts.get("disk_kb") else None),
        "disk_pct": facts.get("disk_pct"),
        "alert_pct": int((s["watch"] or {}).get("disk_pct") or DISK_ALERT_PCT),
        "free_ports": free_ports(connection, host, facts) if facts else None,
        "port_range": f"{host.get('port_min')}–{host.get('port_max')}",
        "docker": facts.get("docker"),
    }
    state_word = ("watching" if phase == "watching" else "ready" if phase == "ready"
                  else "needs you" if (phase == "key" and s["access"] == "boss_key")
                  else "setting up")
    return {
        "host_id": host_id, "name": host["name"], "address": host["address"],
        "user": host.get("ssh_user"), "port": host.get("ssh_port"), "kind": s["kind"],
        "role": s["role"], "role_words": ROLE_WORDS[s["role"]], "role_line": ROLE_LINE[s["role"]],
        "access": s["access"], "setup": s["setup"], "watch": s["watch"], "owner": "Infra agent",
        "phase": phase, "state": state_word, "blocked": s.get("blocked"), "steps": steps,
        "now": now, "gate": gate, "stats": stats, "log": (s["log"] or [])[-40:],
        "probe": s["probe"] or {}, "ready": phase in ("ready", "watching"),
        "key_landed_at": _iso(s.get("key_landed_at")), "ready_at": _iso(s.get("ready_at")),
        "created_at": _iso(s.get("created_at")),
    }


# ── 7 · where a ready server shows up: Project › Deployments ───────────────────────────────

def servers_for_project(connection, project_id: str) -> list[dict[str, Any]]:
    """Ready servers this project may use (its own and the org's verified ones), with their role.
    The app picks: staging/builds may be used for staging; prod is a person's choice."""
    from .deployment_fleet import usable_hosts_sql

    rows = connection.execute(text(
        "SELECT h.id, h.name, h.address, h.labels, h.project_id, s.role, s.phase, "
        "  (SELECT count(*) FROM deployment_targets t WHERE t.host_id = h.id AND t.status = 'active' "
        "     AND t.project_id = :p) AS mine "
        f"FROM deployment_hosts h LEFT JOIN server_setups s ON s.host_id = h.id "
        f"WHERE h.status = 'verified' AND {usable_hosts_sql('h')} ORDER BY h.created_at DESC LIMIT 12"),
        {"p": project_id}).mappings().all()
    out = []
    for r in rows:
        labels = _j(r["labels"]) or {}
        role = r["role"] or labels.get("role") or None
        out.append({"host_id": r["id"], "name": r["name"], "address": r["address"],
                    "role": role, "role_words": ROLE_WORDS.get(role or "", "Any use"),
                    "watching": r["phase"] == "watching", "used_here": int(r["mine"] or 0),
                    "shared": r["project_id"] != project_id})
    return out

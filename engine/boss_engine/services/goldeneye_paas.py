"""Goldeneye bare-metal PaaS — BOSS control plane integration.

Goldeneye is a **standalone modular kit** (``packages/goldeneye/``, destined for
its own OSS repo). This service is an *optional* BOSS integration that:

- registers nodes, installs modules, deploys apps
- records every effect as a Receipt
- gates real SSH/host mutation with ``BOSS_SSH_HOST_DEPLOY_ALLOWED``

Kit source of truth: modular ``modules/*/package.yaml`` + assembled
``catalog.yaml``. BOSS never owns module recipes — only fleet truth and policy.
"""

from __future__ import annotations

import json
import shlex
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..config import Settings
from .core import emit_event, make_id
from .receipts import ReceiptService

# Optional injectable SSH runner: (host, user, port, command, timeout) -> dict
# with keys returncode, stdout, stderr. Used by unit tests; production uses
# SSHHostAdapter._run_remote when the deploy gate is open.
SshRunner = Callable[..., dict[str, Any]]

_REMOTE_INSTALL_EDITIONS = frozenset(
    {"cloud", "enterprise", "enterprise_cloud", "enterprise_self_hosted"}
)
_COMMUNITY_INSTALL_DENIAL = (
    "Direct Goldeneye SSH package installation is disabled in Community/local. "
    "Use an approved Resource Plane flow for real host changes."
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _kit_roots() -> list[Path]:
    here = Path(__file__).resolve()
    return [
        here.parents[3] / "packages" / "goldeneye",
        Path.cwd() / "packages" / "goldeneye",
        Path("/opt/goldeneye"),
    ]


def _load_kit_catalog() -> Optional[dict[str, Any]]:
    """Prefer goldeneye_kit Python package; fall back to catalog.yaml paths."""
    for root in _kit_roots():
        py = root / "python"
        if py.is_dir() and str(py) not in sys.path:
            sys.path.insert(0, str(py))
    try:
        from goldeneye_kit.catalog import load_catalog  # type: ignore

        for root in _kit_roots():
            if (root / "modules").is_dir() or (root / "catalog.yaml").is_file():
                return load_catalog(root)
        return load_catalog()
    except Exception:
        pass
    for root in _kit_roots():
        cat = root / "catalog.yaml"
        if cat.is_file():
            try:
                import yaml  # type: ignore

                data = yaml.safe_load(cat.read_text(encoding="utf-8")) or {}
                if data.get("packages"):
                    return data
            except Exception:
                continue
    return None


# Embedded fallback if kit is not on disk (tests / wheel-only).
_EMBEDDED_PACKAGES: list[dict[str, Any]] = [
    {
        "id": "layer0-host",
        "name": "Host baseline",
        "layer": 0,
        "depends_on": [],
        "optional": False,
        "kind": "host",
        "description": "Sysctl, ufw templates, unattended-upgrades, SSH hardening",
    },
    {
        "id": "layer1-runtime",
        "name": "Container runtime",
        "layer": 1,
        "depends_on": ["layer0-host"],
        "optional": False,
        "kind": "runtime",
        "description": "Docker pools, external networks, platform-dns",
    },
    {
        "id": "layer2-edge",
        "name": "Edge plane",
        "layer": 2,
        "depends_on": ["layer1-runtime"],
        "optional": False,
        "kind": "edge",
        "description": "Traefik, registry, hub cache",
    },
    {
        "id": "layer3-control",
        "name": "Control plane hooks",
        "layer": 3,
        "depends_on": ["layer2-edge"],
        "optional": False,
        "kind": "control",
        "description": "ops-agent, health/exposure stubs",
    },
    {
        "id": "layer4-data",
        "name": "Data & backup",
        "layer": 4,
        "depends_on": ["layer1-runtime"],
        "optional": False,
        "kind": "data",
        "description": "pg dumps, restore-drill",
    },
    {
        "id": "layer5-observe",
        "name": "Observability",
        "layer": 5,
        "depends_on": ["layer2-edge"],
        "optional": False,
        "kind": "observe",
        "description": "Logs/portal placeholders",
    },
    {
        "id": "deploy-cli",
        "name": "Deploy CLI",
        "layer": 2,
        "depends_on": ["layer1-runtime"],
        "optional": False,
        "kind": "tool",
        "description": "ge/deploy command surface",
    },
    {
        "id": "minio",
        "name": "Object storage (MinIO)",
        "layer": 6,
        "depends_on": ["layer2-edge"],
        "optional": True,
        "kind": "optional",
        "description": "S3-compatible storage",
    },
    {
        "id": "keycloak",
        "name": "Identity (Keycloak)",
        "layer": 6,
        "depends_on": ["layer2-edge"],
        "optional": True,
        "kind": "optional",
        "description": "OIDC realm factory",
    },
    {
        "id": "llm-proxy",
        "name": "LLM gateway",
        "layer": 6,
        "depends_on": ["layer2-edge"],
        "optional": True,
        "kind": "optional",
        "description": "OpenAI-compatible proxy",
    },
    {
        "id": "plane",
        "name": "Project tracking (Plane)",
        "layer": 6,
        "depends_on": ["layer2-edge"],
        "optional": True,
        "kind": "optional",
        "description": "Issue tracker system of record",
    },
    {
        "id": "meilisearch",
        "name": "Search (Meilisearch)",
        "layer": 6,
        "depends_on": ["layer2-edge"],
        "optional": True,
        "kind": "optional",
        "description": "Full-text search",
    },
]


def load_package_catalog() -> dict[str, Any]:
    data = _load_kit_catalog()
    if data and data.get("packages"):
        return {
            "version": data.get("version", 2),
            "api_version": data.get("api_version", 1),
            "kit_version": data.get("kit_version"),
            "source": data.get("source") or "modules/*/package.yaml",
            "default_bootstrap": data.get("default_bootstrap")
            or [
                "layer0-host",
                "layer1-runtime",
                "layer2-edge",
                "deploy-cli",
            ],
            "items": data["packages"],
            "modular": True,
        }
    return {
        "version": 1,
        "api_version": 1,
        "source": "embedded",
        "default_bootstrap": [
            "layer0-host",
            "layer1-runtime",
            "layer2-edge",
            "deploy-cli",
        ],
        "items": list(_EMBEDDED_PACKAGES),
        "modular": False,
    }


# Idempotent DDL so live dogfood (and ad-hoc test DBs) recover from
# UndefinedTable on goldeneye_nodes instead of 500ing /v1/goldeneye/status.
_GOLDENEYE_DDL = """
CREATE TABLE IF NOT EXISTS goldeneye_nodes (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  display_name TEXT,
  roles JSONB NOT NULL DEFAULT '[]'::jsonb,
  status TEXT NOT NULL DEFAULT 'registered',
  public_ip TEXT,
  tailnet_ip TEXT,
  ssh_host TEXT,
  ssh_user TEXT NOT NULL DEFAULT 'root',
  ssh_port INTEGER NOT NULL DEFAULT 22,
  kit_version TEXT,
  site_config JSONB NOT NULL DEFAULT '{}'::jsonb,
  labels JSONB NOT NULL DEFAULT '{}'::jsonb,
  last_heartbeat_at TIMESTAMPTZ,
  created_by TEXT NOT NULL DEFAULT 'system',
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(project_id, name)
);
CREATE INDEX IF NOT EXISTS idx_goldeneye_nodes_project
  ON goldeneye_nodes(project_id, status, created_at DESC);

CREATE TABLE IF NOT EXISTS goldeneye_package_installs (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  node_id TEXT NOT NULL REFERENCES goldeneye_nodes(id) ON DELETE CASCADE,
  package_id TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'planned',
  layer INTEGER,
  mode TEXT NOT NULL DEFAULT 'simulated',
  receipt_id TEXT REFERENCES receipts(id) ON DELETE SET NULL,
  error JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_by TEXT NOT NULL DEFAULT 'system',
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  completed_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_goldeneye_pkg_installs_node
  ON goldeneye_package_installs(project_id, node_id, package_id, created_at DESC);

CREATE TABLE IF NOT EXISTS goldeneye_apps (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  node_id TEXT NOT NULL REFERENCES goldeneye_nodes(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'planned',
  mode TEXT NOT NULL DEFAULT 'image',
  image_ref TEXT,
  previous_image_ref TEXT,
  compose_ref TEXT,
  domain TEXT,
  public_url TEXT,
  admin_url TEXT,
  receipt_id TEXT REFERENCES receipts(id) ON DELETE SET NULL,
  error JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_by TEXT NOT NULL DEFAULT 'system',
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(project_id, name)
);
CREATE INDEX IF NOT EXISTS idx_goldeneye_apps_project
  ON goldeneye_apps(project_id, status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_goldeneye_apps_node
  ON goldeneye_apps(project_id, node_id, status);

CREATE TABLE IF NOT EXISTS goldeneye_deploys (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  app_id TEXT NOT NULL REFERENCES goldeneye_apps(id) ON DELETE CASCADE,
  node_id TEXT NOT NULL REFERENCES goldeneye_nodes(id) ON DELETE CASCADE,
  action TEXT NOT NULL DEFAULT 'deploy',
  status TEXT NOT NULL DEFAULT 'planned',
  image_ref TEXT,
  previous_image_ref TEXT,
  mode TEXT NOT NULL DEFAULT 'simulated',
  receipt_id TEXT REFERENCES receipts(id) ON DELETE SET NULL,
  error JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_by TEXT NOT NULL DEFAULT 'system',
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  completed_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_goldeneye_deploys_app
  ON goldeneye_deploys(project_id, app_id, created_at DESC);
"""

# Community kit + optional BOSS control plane vs platform-only hosted fleet.
_COMMUNITY_EDITION = {
    "kit": True,
    "control_plane": True,
    "self_hosted_metal": True,
}
_PLATFORM_ONLY = [
    "managed multi-tenant Goldeneye fleet (BOSS Cloud)",
    "hosted metal as a service",
    "Cloud Easy Connect as the node source",
]


def _is_undefined_table(exc: BaseException) -> bool:
    orig = getattr(exc, "orig", None)
    name = type(orig).__name__ if orig is not None else type(exc).__name__
    blob = str(orig or exc).lower()
    return name == "UndefinedTable" or "does not exist" in blob


class GoldeneyePaasService:
    """Project-scoped Goldeneye node/package/app control plane."""

    def __init__(
        self,
        settings: Settings,
        *,
        ssh_runner: Optional[SshRunner] = None,
    ):
        self.settings = settings
        self.receipts = ReceiptService()
        # Injected by tests; production falls back to SSHHostAdapter.
        self.ssh_runner = ssh_runner
        self._tables_ready = False

    def ensure_tables(self, connection: Connection) -> None:
        """Idempotent DDL so missing goldeneye_* relations become empty-200."""
        if self._tables_ready:
            return
        for stmt in _GOLDENEYE_DDL.split(";"):
            s = stmt.strip()
            if s:
                connection.execute(text(s))
        self._tables_ready = True

    # ------------------------------------------------------------------
    # Catalog
    # ------------------------------------------------------------------

    def package_catalog(self) -> dict[str, Any]:
        return load_package_catalog()

    def catalog_bootstrap(self) -> dict[str, Any]:
        """Default module install order for a fresh node (from kit catalog)."""
        catalog = self.package_catalog()
        order = list(
            catalog.get("default_bootstrap")
            or [
                "layer0-host",
                "layer1-runtime",
                "layer2-edge",
                "deploy-cli",
            ]
        )
        return {
            "default_bootstrap": order,
            "count": len(order),
            "kit_version": catalog.get("kit_version"),
            "api_version": catalog.get("api_version") or 1,
            "source": catalog.get("source"),
            "modular": bool(catalog.get("modular", False)),
        }

    def _package_by_id(self, package_id: str) -> dict[str, Any]:
        catalog = self.package_catalog()
        for item in catalog["items"]:
            if str(item.get("id")) == package_id:
                return item
        raise ValueError(f"Unknown package_id: {package_id}")

    # ------------------------------------------------------------------
    # Scope helpers
    # ------------------------------------------------------------------

    def _project_scope(self, connection: Connection, project_id: str) -> dict[str, str]:
        row = connection.execute(
            text(
                """
                SELECT p.id, p.workspace_id, w.org_id
                FROM projects p
                JOIN workspaces w ON w.id = p.workspace_id
                WHERE p.id = :id
                """
            ),
            {"id": project_id},
        ).mappings().first()
        if not row:
            # Fall back to settings defaults when project row is incomplete
            org = getattr(self.settings, "default_org_id", None) or "org_boss"
            ws = getattr(self.settings, "default_workspace_id", None) or "ws_boss"
            exists = connection.execute(
                text("SELECT id FROM projects WHERE id = :id"),
                {"id": project_id},
            ).first()
            if not exists:
                raise ValueError(f"Unknown project_id: {project_id}")
            return {"project_id": project_id, "org_id": org, "workspace_id": ws}
        return {
            "project_id": row["id"],
            "org_id": row["org_id"],
            "workspace_id": row["workspace_id"],
        }

    def _real_host_allowed(self) -> bool:
        return bool(getattr(self.settings, "ssh_host_deploy_allowed", False))

    def _assert_remote_module_install_allowed(self) -> None:
        edition = str(getattr(self.settings, "boss_edition", "community")).strip().lower()
        if edition not in _REMOTE_INSTALL_EDITIONS:
            raise PermissionError(_COMMUNITY_INSTALL_DENIAL)
        if not self._real_host_allowed():
            raise PermissionError("Goldeneye SSH host deployment is disabled.")

    def _actuate_remote_module_install(
        self,
        *,
        node: dict[str, Any],
        package_id: str,
        timeout: int = 600,
    ) -> dict[str, Any]:
        """Run ``ge module install <id>`` on the node via SSH (or injected runner).

        Returns a result dict with at least: ok, returncode, stdout, stderr,
        command, host. Does not raise on remote non-zero exit — callers inspect
        ``ok``. Raises only when the node lacks ssh_host or local SSH setup fails
        before a remote attempt (adapter guard / key missing).
        """
        self._assert_remote_module_install_allowed()
        host = (node.get("ssh_host") or "").strip()
        if not host:
            raise ValueError("node has no ssh_host; cannot actuate real install")
        user = str(node.get("ssh_user") or "root")
        port = int(node.get("ssh_port") or 22)
        # Prefer modular CLI surface; `ge pkg install` is an alias on the kit.
        command = f"ge module install {shlex.quote(package_id)}"

        if self.ssh_runner is not None:
            result = self.ssh_runner(
                host=host,
                user=user,
                port=port,
                command=command,
                timeout=timeout,
            )
            rc = int(result.get("returncode", 1))
            return {
                "ok": rc == 0,
                "returncode": rc,
                "stdout": str(result.get("stdout") or "")[:4000],
                "stderr": str(result.get("stderr") or "")[:4000],
                "command": command,
                "host": host,
                "user": user,
                "port": port,
                "via": "injected_runner",
            }

        from .ssh_host_adapter import SSHHostAdapter

        adapter = SSHHostAdapter(self.settings)
        proc = adapter._run_remote(
            host=host,
            user=user,
            port=port,
            command=command,
            timeout=timeout,
        )
        rc = int(proc.returncode)
        return {
            "ok": rc == 0,
            "returncode": rc,
            "stdout": (proc.stdout or "")[:4000],
            "stderr": (proc.stderr or "")[:4000],
            "command": command,
            "host": host,
            "user": user,
            "port": port,
            "via": "ssh_host_adapter",
        }

    # ------------------------------------------------------------------
    # Nodes
    # ------------------------------------------------------------------

    def register_node(
        self,
        connection: Connection,
        *,
        project_id: str,
        actor: str,
        name: str,
        roles: Optional[list[str]] = None,
        public_ip: Optional[str] = None,
        tailnet_ip: Optional[str] = None,
        ssh_host: Optional[str] = None,
        ssh_user: str = "root",
        ssh_port: int = 22,
        kit_version: Optional[str] = None,
        site_config: Optional[dict[str, Any]] = None,
        labels: Optional[dict[str, Any]] = None,
        display_name: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        name = (name or "").strip().lower().replace(" ", "-")
        if not name or not name.replace("-", "").replace("_", "").isalnum():
            raise ValueError("name must be a short alphanumeric node name")
        self.ensure_tables(connection)
        scope = self._project_scope(connection, project_id)
        roles = roles or ["apps"]
        existing = connection.execute(
            text(
                "SELECT id FROM goldeneye_nodes "
                "WHERE project_id = :project_id AND name = :name"
            ),
            {"project_id": project_id, "name": name},
        ).mappings().first()
        now = _utcnow()
        if existing:
            node_id = existing["id"]
            connection.execute(
                text(
                    """
                    UPDATE goldeneye_nodes SET
                      display_name = COALESCE(:display_name, display_name),
                      roles = CAST(:roles AS jsonb),
                      public_ip = COALESCE(:public_ip, public_ip),
                      tailnet_ip = COALESCE(:tailnet_ip, tailnet_ip),
                      ssh_host = COALESCE(:ssh_host, ssh_host),
                      ssh_user = :ssh_user,
                      ssh_port = :ssh_port,
                      kit_version = COALESCE(:kit_version, kit_version),
                      site_config = CAST(:site_config AS jsonb),
                      labels = CAST(:labels AS jsonb),
                      metadata = CAST(:metadata AS jsonb),
                      updated_at = :now
                    WHERE id = :id
                    """
                ),
                {
                    "id": node_id,
                    "display_name": display_name,
                    "roles": json.dumps(roles),
                    "public_ip": public_ip,
                    "tailnet_ip": tailnet_ip,
                    "ssh_host": ssh_host,
                    "ssh_user": ssh_user or "root",
                    "ssh_port": int(ssh_port or 22),
                    "kit_version": kit_version,
                    "site_config": json.dumps(site_config or {}),
                    "labels": json.dumps(labels or {}),
                    "metadata": json.dumps(metadata or {}),
                    "now": now,
                },
            )
            action = "updated"
        else:
            node_id = make_id("genode")
            connection.execute(
                text(
                    """
                    INSERT INTO goldeneye_nodes (
                      id, project_id, name, display_name, roles, status,
                      public_ip, tailnet_ip, ssh_host, ssh_user, ssh_port,
                      kit_version, site_config, labels, created_by, metadata,
                      created_at, updated_at
                    ) VALUES (
                      :id, :project_id, :name, :display_name, CAST(:roles AS jsonb),
                      'registered', :public_ip, :tailnet_ip, :ssh_host, :ssh_user,
                      :ssh_port, :kit_version, CAST(:site_config AS jsonb),
                      CAST(:labels AS jsonb), :created_by, CAST(:metadata AS jsonb),
                      :now, :now
                    )
                    """
                ),
                {
                    "id": node_id,
                    "project_id": project_id,
                    "name": name,
                    "display_name": display_name or name,
                    "roles": json.dumps(roles),
                    "public_ip": public_ip,
                    "tailnet_ip": tailnet_ip,
                    "ssh_host": ssh_host,
                    "ssh_user": ssh_user or "root",
                    "ssh_port": int(ssh_port or 22),
                    "kit_version": kit_version,
                    "site_config": json.dumps(site_config or {}),
                    "labels": json.dumps(labels or {}),
                    "created_by": actor,
                    "metadata": json.dumps(metadata or {}),
                    "now": now,
                },
            )
            action = "created"

        receipt, _ = self.receipts.create_or_reuse(
            connection,
            scope["org_id"],
            scope["workspace_id"],
            project_id,
            dedupe_key=f"goldeneye.node.register:{project_id}:{name}",
            effect_kind="goldeneye.node.register",
            effect_scope={"node_id": node_id, "name": name, "action": action},
            payload={"roles": roles, "public_ip": public_ip, "ssh_host": ssh_host},
            created_by_job_id=None,
        )
        self.receipts.mark_succeeded(
            connection,
            receipt["id"],
            provider_ref=node_id,
            result={"node_id": node_id, "action": action},
        )
        emit_event(
            connection,
            project_id,
            "goldeneye.node.registered",
            actor,
            "goldeneye_node",
            node_id,
            {"node_id": node_id, "name": name, "action": action},
        )
        return self.get_node(connection, project_id, node_id)

    def list_nodes(
        self, connection: Connection, project_id: str
    ) -> list[dict[str, Any]]:
        self.ensure_tables(connection)
        rows = connection.execute(
            text(
                """
                SELECT * FROM goldeneye_nodes
                WHERE project_id = :project_id
                ORDER BY created_at ASC
                """
            ),
            {"project_id": project_id},
        ).mappings().all()
        return [self._row_node(r) for r in rows]

    def get_node(
        self, connection: Connection, project_id: str, node_id: str
    ) -> dict[str, Any]:
        self.ensure_tables(connection)
        row = connection.execute(
            text(
                "SELECT * FROM goldeneye_nodes "
                "WHERE project_id = :project_id AND id = :id"
            ),
            {"project_id": project_id, "id": node_id},
        ).mappings().first()
        if not row:
            raise ValueError(f"Unknown node_id: {node_id}")
        return self._row_node(row)

    def _resolve_node(
        self,
        connection: Connection,
        project_id: str,
        *,
        node_id: Optional[str] = None,
        node_name: Optional[str] = None,
    ) -> dict[str, Any]:
        if node_id:
            return self.get_node(connection, project_id, node_id)
        if node_name:
            row = connection.execute(
                text(
                    "SELECT * FROM goldeneye_nodes "
                    "WHERE project_id = :project_id AND name = :name"
                ),
                {"project_id": project_id, "name": node_name.strip().lower()},
            ).mappings().first()
            if not row:
                raise ValueError(f"Unknown node name: {node_name}")
            return self._row_node(row)
        raise ValueError("node_id or node_name is required")

    def heartbeat_node(
        self,
        connection: Connection,
        *,
        project_id: str,
        node_id: str,
        status: Optional[str] = None,
        kit_version: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        node = self.get_node(connection, project_id, node_id)
        now = _utcnow()
        if status:
            new_status = status
        elif node["status"] in ("registered", "bootstrapping", "offline", "degraded"):
            # Live heartbeat without explicit status means the node is reachable.
            new_status = "ready"
        else:
            new_status = node["status"]
        connection.execute(
            text(
                """
                UPDATE goldeneye_nodes SET
                  status = :status,
                  kit_version = COALESCE(:kit_version, kit_version),
                  last_heartbeat_at = :now,
                  metadata = CASE
                    WHEN :has_meta THEN CAST(:metadata AS jsonb)
                    ELSE metadata
                  END,
                  updated_at = :now
                WHERE id = :id
                """
            ),
            {
                "id": node_id,
                "status": new_status,
                "kit_version": kit_version,
                "now": now,
                "has_meta": bool(metadata),
                "metadata": json.dumps(metadata or {}),
            },
        )
        return self.get_node(connection, project_id, node_id)

    # ------------------------------------------------------------------
    # Packages
    # ------------------------------------------------------------------

    def install_package(
        self,
        connection: Connection,
        *,
        project_id: str,
        actor: str,
        package_id: str,
        node_id: Optional[str] = None,
        node_name: Optional[str] = None,
        force: bool = False,
        metadata: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        if self._real_host_allowed():
            # Community/local never turns an operator feature flag into an SSH write.
            # Reject before tables, receipts, or host calls change any durable state.
            self._assert_remote_module_install_allowed()
        pkg = self._package_by_id(package_id)
        self.ensure_tables(connection)
        node = self._resolve_node(
            connection, project_id, node_id=node_id, node_name=node_name
        )
        scope = self._project_scope(connection, project_id)

        if not force:
            for dep in pkg.get("depends_on") or []:
                installed = connection.execute(
                    text(
                        """
                        SELECT 1 FROM goldeneye_package_installs
                        WHERE project_id = :project_id AND node_id = :node_id
                          AND package_id = :package_id
                          AND status IN ('installed', 'simulated')
                        LIMIT 1
                        """
                    ),
                    {
                        "project_id": project_id,
                        "node_id": node["id"],
                        "package_id": dep,
                    },
                ).first()
                if not installed:
                    raise ValueError(
                        f"Missing dependency {dep} on node {node['name']} "
                        f"(install it first, or pass force=true)"
                    )

        # Actuation policy:
        # - Gate off (default): simulated record + receipt; no SSH.
        # - Gate on + node.ssh_host: remote `ge module install <id>` via
        #   SSHHostAdapter (or injected ssh_runner); success → installed,
        #   failure → failed with error jsonb.
        # - Gate on without ssh_host: planned (operator must actuate).
        real_allowed = self._real_host_allowed()
        ssh_host = (node.get("ssh_host") or "").strip()
        can_actuate = real_allowed and bool(ssh_host)
        mode = "real" if real_allowed else "simulated"

        install_id = make_id("gepkg")
        now = _utcnow()
        receipt, _ = self.receipts.create_or_reuse(
            connection,
            scope["org_id"],
            scope["workspace_id"],
            project_id,
            dedupe_key=(
                f"goldeneye.pkg.install:{project_id}:{node['id']}:{package_id}:{now.isoformat()}"
            ),
            effect_kind="goldeneye.package.install",
            effect_scope={
                "node_id": node["id"],
                "package_id": package_id,
                "mode": mode,
                "ssh_host": ssh_host or None,
            },
            payload={"force": force, "layer": pkg.get("layer")},
            created_by_job_id=None,
        )

        meta_base: dict[str, Any] = {
            **(metadata or {}),
            "package_name": pkg.get("name"),
            "kind": pkg.get("kind"),
        }
        error_payload: dict[str, Any] = {}
        actuation: Optional[dict[str, Any]] = None
        status: str
        completed_at: Optional[datetime]

        if not real_allowed:
            status = "simulated"
            completed_at = now
            self.receipts.mark_succeeded(
                connection,
                receipt["id"],
                provider_ref=install_id,
                result={
                    "install_id": install_id,
                    "status": status,
                    "mode": mode,
                    "hint": (
                        "BOSS_SSH_HOST_DEPLOY_ALLOWED=false; "
                        "recorded simulated install. On the node run: "
                        f"ge module install {package_id}"
                    ),
                },
            )
        elif not can_actuate:
            # Gate open but node has no ssh_host — plan only.
            status = "planned"
            completed_at = None
            meta_base["hint"] = f"Actuate on node: ge module install {package_id}"
            self.receipts.mark_succeeded(
                connection,
                receipt["id"],
                provider_ref=install_id,
                result={
                    "install_id": install_id,
                    "status": status,
                    "mode": mode,
                    "hint": (
                        "BOSS_SSH_HOST_DEPLOY_ALLOWED=true but node has no "
                        f"ssh_host. Actuate manually: ge module install {package_id}"
                    ),
                },
            )
        else:
            # Real path: attempt remote install via SSH adapter patterns.
            try:
                actuation = self._actuate_remote_module_install(
                    node=node, package_id=package_id
                )
            except Exception as exc:  # noqa: BLE001 — surface as failed install
                actuation = {
                    "ok": False,
                    "returncode": -1,
                    "stdout": "",
                    "stderr": str(exc)[:2000],
                    "command": f"ge module install {package_id}",
                    "host": ssh_host,
                    "via": "exception",
                    "error_type": type(exc).__name__,
                }

            if actuation.get("ok"):
                status = "installed"
                completed_at = now
                meta_base["actuation"] = {
                    "host": actuation.get("host"),
                    "command": actuation.get("command"),
                    "returncode": actuation.get("returncode"),
                    "via": actuation.get("via"),
                }
                self.receipts.mark_succeeded(
                    connection,
                    receipt["id"],
                    provider_ref=install_id,
                    result={
                        "install_id": install_id,
                        "status": status,
                        "mode": mode,
                        "actuation": meta_base["actuation"],
                        "stdout_tail": (actuation.get("stdout") or "")[-500:],
                    },
                )
            else:
                status = "failed"
                completed_at = now
                error_payload = {
                    "message": (
                        f"Remote ge module install {package_id} failed on "
                        f"{ssh_host}"
                    ),
                    "returncode": actuation.get("returncode"),
                    "stderr": (actuation.get("stderr") or "")[:2000],
                    "stdout": (actuation.get("stdout") or "")[:1000],
                    "command": actuation.get("command"),
                    "host": ssh_host,
                    "via": actuation.get("via"),
                    "error_type": actuation.get("error_type"),
                }
                meta_base["actuation"] = {
                    "host": ssh_host,
                    "command": actuation.get("command"),
                    "returncode": actuation.get("returncode"),
                    "via": actuation.get("via"),
                    "failed": True,
                }
                self.receipts.mark_failed(
                    connection, receipt["id"], error=error_payload
                )

        connection.execute(
            text(
                """
                INSERT INTO goldeneye_package_installs (
                  id, project_id, node_id, package_id, status, layer, mode,
                  receipt_id, error, created_by, metadata, created_at, updated_at,
                  completed_at
                ) VALUES (
                  :id, :project_id, :node_id, :package_id, :status, :layer, :mode,
                  :receipt_id, CAST(:error AS jsonb), :created_by,
                  CAST(:metadata AS jsonb), :now, :now, :completed_at
                )
                """
            ),
            {
                "id": install_id,
                "project_id": project_id,
                "node_id": node["id"],
                "package_id": package_id,
                "status": status,
                "layer": pkg.get("layer"),
                "mode": mode,
                "receipt_id": receipt["id"],
                "error": json.dumps(error_payload),
                "created_by": actor,
                "metadata": json.dumps(meta_base),
                "now": now,
                "completed_at": completed_at,
            },
        )

        if node["status"] == "registered" and status in (
            "installed",
            "simulated",
            "planned",
            "installing",
        ):
            connection.execute(
                text(
                    "UPDATE goldeneye_nodes SET status = 'bootstrapping', "
                    "updated_at = :now WHERE id = :id"
                ),
                {"id": node["id"], "now": now},
            )

        emit_event(
            connection,
            project_id,
            "goldeneye.package.install",
            actor,
            "goldeneye_package_install",
            install_id,
            {
                "install_id": install_id,
                "node_id": node["id"],
                "package_id": package_id,
                "status": status,
                "mode": mode,
                "actuated": bool(can_actuate and status == "installed"),
            },
        )
        return self._get_install(connection, project_id, install_id)

    def list_package_installs(
        self,
        connection: Connection,
        project_id: str,
        *,
        node_id: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        self.ensure_tables(connection)
        sql = """
            SELECT * FROM goldeneye_package_installs
            WHERE project_id = :project_id
        """
        params: dict[str, Any] = {"project_id": project_id}
        if node_id:
            sql += " AND node_id = :node_id"
            params["node_id"] = node_id
        sql += " ORDER BY created_at DESC"
        rows = connection.execute(text(sql), params).mappings().all()
        return [self._row_install(r) for r in rows]

    # ------------------------------------------------------------------
    # Apps / deploys
    # ------------------------------------------------------------------

    def deploy_app(
        self,
        connection: Connection,
        *,
        project_id: str,
        actor: str,
        name: str,
        node_id: Optional[str] = None,
        node_name: Optional[str] = None,
        image_ref: Optional[str] = None,
        compose_ref: Optional[str] = None,
        domain: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        name = (name or "").strip().lower().replace(" ", "-")
        if not name:
            raise ValueError("name is required")
        if not image_ref and not compose_ref:
            raise ValueError("image_ref or compose_ref is required")
        self.ensure_tables(connection)
        node = self._resolve_node(
            connection, project_id, node_id=node_id, node_name=node_name
        )
        scope = self._project_scope(connection, project_id)
        mode = "compose" if compose_ref else "image"
        real = self._real_host_allowed()
        deploy_mode = "real" if real else "simulated"
        app_status = "planned" if real else "simulated"

        existing = connection.execute(
            text(
                "SELECT * FROM goldeneye_apps "
                "WHERE project_id = :project_id AND name = :name"
            ),
            {"project_id": project_id, "name": name},
        ).mappings().first()

        now = _utcnow()
        previous_image = None
        if existing:
            app_id = existing["id"]
            previous_image = existing.get("image_ref")
            connection.execute(
                text(
                    """
                    UPDATE goldeneye_apps SET
                      node_id = :node_id,
                      status = :status,
                      mode = :mode,
                      previous_image_ref = :previous_image_ref,
                      image_ref = COALESCE(:image_ref, image_ref),
                      compose_ref = COALESCE(:compose_ref, compose_ref),
                      domain = COALESCE(:domain, domain),
                      public_url = COALESCE(:public_url, public_url),
                      updated_at = :now,
                      metadata = CAST(:metadata AS jsonb)
                    WHERE id = :id
                    """
                ),
                {
                    "id": app_id,
                    "node_id": node["id"],
                    "status": app_status if not real else "deploying",
                    "mode": mode,
                    "previous_image_ref": previous_image,
                    "image_ref": image_ref,
                    "compose_ref": compose_ref,
                    "domain": domain,
                    "public_url": f"https://{domain}" if domain else None,
                    "now": now,
                    "metadata": json.dumps(metadata or {}),
                },
            )
        else:
            app_id = make_id("geapp")
            connection.execute(
                text(
                    """
                    INSERT INTO goldeneye_apps (
                      id, project_id, node_id, name, status, mode, image_ref,
                      previous_image_ref, compose_ref, domain, public_url,
                      created_by, metadata, created_at, updated_at
                    ) VALUES (
                      :id, :project_id, :node_id, :name, :status, :mode, :image_ref,
                      NULL, :compose_ref, :domain, :public_url,
                      :created_by, CAST(:metadata AS jsonb), :now, :now
                    )
                    """
                ),
                {
                    "id": app_id,
                    "project_id": project_id,
                    "node_id": node["id"],
                    "name": name,
                    "status": app_status if not real else "deploying",
                    "mode": mode,
                    "image_ref": image_ref,
                    "compose_ref": compose_ref,
                    "domain": domain,
                    "public_url": f"https://{domain}" if domain else None,
                    "created_by": actor,
                    "metadata": json.dumps(metadata or {}),
                    "now": now,
                },
            )

        deploy_id = make_id("gedeploy")
        receipt, _ = self.receipts.create_or_reuse(
            connection,
            scope["org_id"],
            scope["workspace_id"],
            project_id,
            dedupe_key=(
                f"goldeneye.app.deploy:{project_id}:{name}:{image_ref or compose_ref}:{now.isoformat()}"
            ),
            effect_kind="goldeneye.app.deploy",
            effect_scope={
                "app_id": app_id,
                "node_id": node["id"],
                "name": name,
                "mode": deploy_mode,
            },
            payload={
                "image_ref": image_ref,
                "compose_ref": compose_ref,
                "domain": domain,
            },
            created_by_job_id=None,
        )

        final_status = "simulated" if not real else "planned"
        if not real:
            # Simulated deploys complete as running for honest local modelling
            connection.execute(
                text(
                    "UPDATE goldeneye_apps SET status = 'simulated', "
                    "receipt_id = :rid, updated_at = :now WHERE id = :id"
                ),
                {"id": app_id, "rid": receipt["id"], "now": now},
            )

        connection.execute(
            text(
                """
                INSERT INTO goldeneye_deploys (
                  id, project_id, app_id, node_id, action, status, image_ref,
                  previous_image_ref, mode, receipt_id, created_by, metadata,
                  created_at, completed_at
                ) VALUES (
                  :id, :project_id, :app_id, :node_id, 'deploy', :status, :image_ref,
                  :previous_image_ref, :mode, :receipt_id, :created_by,
                  CAST(:metadata AS jsonb), :now, :completed_at
                )
                """
            ),
            {
                "id": deploy_id,
                "project_id": project_id,
                "app_id": app_id,
                "node_id": node["id"],
                "status": final_status,
                "image_ref": image_ref,
                "previous_image_ref": previous_image,
                "mode": deploy_mode,
                "receipt_id": receipt["id"],
                "created_by": actor,
                "metadata": json.dumps(metadata or {}),
                "now": now,
                "completed_at": now if final_status == "simulated" else None,
            },
        )
        self.receipts.mark_succeeded(
            connection,
            receipt["id"],
            provider_ref=deploy_id,
            result={
                "deploy_id": deploy_id,
                "app_id": app_id,
                "status": final_status,
                "hint": (
                    None
                    if real
                    else "Simulated deploy. On node: "
                    f"ge deploy {name} "
                    + (
                        f"--image {image_ref}"
                        if image_ref
                        else f"--compose {compose_ref}"
                    )
                ),
            },
        )
        emit_event(
            connection,
            project_id,
            "goldeneye.app.deployed",
            actor,
            "goldeneye_app",
            app_id,
            {
                "deploy_id": deploy_id,
                "app_id": app_id,
                "name": name,
                "status": final_status,
            },
        )
        app = self.get_app(connection, project_id, app_id)
        app["latest_deploy_id"] = deploy_id
        return app

    def rollback_app(
        self,
        connection: Connection,
        *,
        project_id: str,
        actor: str,
        name: str,
    ) -> dict[str, Any]:
        app_row = connection.execute(
            text(
                "SELECT * FROM goldeneye_apps "
                "WHERE project_id = :project_id AND name = :name"
            ),
            {"project_id": project_id, "name": name.strip().lower()},
        ).mappings().first()
        if not app_row:
            raise ValueError(f"Unknown app: {name}")
        prev = app_row.get("previous_image_ref")
        if not prev:
            raise ValueError(f"No previous_image_ref for app {name}")
        return self.deploy_app(
            connection,
            project_id=project_id,
            actor=actor,
            name=name,
            node_id=app_row["node_id"],
            image_ref=prev,
            metadata={"rollback": True},
        )

    def list_apps(
        self, connection: Connection, project_id: str
    ) -> list[dict[str, Any]]:
        self.ensure_tables(connection)
        rows = connection.execute(
            text(
                """
                SELECT * FROM goldeneye_apps
                WHERE project_id = :project_id
                ORDER BY updated_at DESC
                """
            ),
            {"project_id": project_id},
        ).mappings().all()
        return [self._row_app(r) for r in rows]

    def get_app(
        self, connection: Connection, project_id: str, app_id: str
    ) -> dict[str, Any]:
        row = connection.execute(
            text(
                "SELECT * FROM goldeneye_apps "
                "WHERE project_id = :project_id AND id = :id"
            ),
            {"project_id": project_id, "id": app_id},
        ).mappings().first()
        if not row:
            raise ValueError(f"Unknown app_id: {app_id}")
        return self._row_app(row)

    def _honest_empty_status(
        self, project_id: str, *, reason: str = "no_nodes_registered"
    ) -> dict[str, Any]:
        catalog = self.package_catalog()
        return {
            "project_id": project_id,
            "product": "goldeneye",
            "status": "not_configured",
            "configured": False,
            "kit": {
                "version": catalog.get("kit_version") or "0.2.0",
                "modular": bool(catalog.get("modular", True)),
                "module_api": catalog.get("api_version") or 1,
                "catalog_source": catalog.get("source"),
                "package_count": len(catalog.get("items") or []),
                "default_bootstrap": catalog.get("default_bootstrap"),
            },
            "real_host_effects": self._real_host_allowed(),
            "nodes": [],
            "node_count": 0,
            "apps": [],
            "app_count": 0,
            "package_installs": [],
            "package_install_count": 0,
            "ready_nodes": 0,
            "host": {"connected": False, "reason": reason},
            "metrics": None,
            "edition": {
                "community": dict(_COMMUNITY_EDITION),
                "platform_only": list(_PLATFORM_ONLY),
            },
        }

    def fleet_status(
        self, connection: Connection, project_id: str
    ) -> dict[str, Any]:
        try:
            self.ensure_tables(connection)
            nodes = self.list_nodes(connection, project_id)
            apps = self.list_apps(connection, project_id)
            installs = self.list_package_installs(connection, project_id)
        except Exception as exc:  # noqa: BLE001 — missing schema must be empty-200
            if _is_undefined_table(exc):
                return self._honest_empty_status(
                    project_id, reason="schema_missing"
                )
            raise
        catalog = self.package_catalog()
        connected = any(
            n.get("last_heartbeat_at")
            and n.get("status") in ("ready", "bootstrapping")
            for n in nodes
        )
        if not nodes:
            status = "not_configured"
        elif connected:
            status = "ready"
        else:
            status = "registered"
        return {
            "project_id": project_id,
            "product": "goldeneye",
            "status": status,
            "configured": bool(nodes),
            "kit": {
                "version": catalog.get("kit_version") or "0.2.0",
                "modular": bool(catalog.get("modular", True)),
                "module_api": catalog.get("api_version") or 1,
                "catalog_source": catalog.get("source"),
                "package_count": len(catalog.get("items") or []),
                "default_bootstrap": catalog.get("default_bootstrap"),
            },
            "real_host_effects": self._real_host_allowed(),
            "nodes": nodes,
            "node_count": len(nodes),
            "apps": apps,
            "app_count": len(apps),
            "package_installs": installs[:50],
            "package_install_count": len(installs),
            "ready_nodes": sum(
                1 for n in nodes if n["status"] in ("ready", "bootstrapping")
            ),
            "host": {
                "connected": bool(connected),
                "reason": (
                    "no_nodes_registered"
                    if not nodes
                    else ("heartbeat_seen" if connected else "no_heartbeat")
                ),
            },
            "metrics": None,
            "edition": {
                "community": dict(_COMMUNITY_EDITION),
                "platform_only": list(_PLATFORM_ONLY),
            },
        }

    # ------------------------------------------------------------------
    # Row mappers
    # ------------------------------------------------------------------

    def _jsonish(self, value: Any, default: Any) -> Any:
        if value is None:
            return default
        if isinstance(value, (dict, list)):
            return value
        if isinstance(value, str):
            try:
                return json.loads(value)
            except Exception:
                return default
        return value

    def _row_node(self, row: Any) -> dict[str, Any]:
        return {
            "id": row["id"],
            "project_id": row["project_id"],
            "name": row["name"],
            "display_name": row.get("display_name"),
            "roles": self._jsonish(row.get("roles"), []),
            "status": row["status"],
            "public_ip": row.get("public_ip"),
            "tailnet_ip": row.get("tailnet_ip"),
            "ssh_host": row.get("ssh_host"),
            "ssh_user": row.get("ssh_user") or "root",
            "ssh_port": row.get("ssh_port") or 22,
            "kit_version": row.get("kit_version"),
            "site_config": self._jsonish(row.get("site_config"), {}),
            "labels": self._jsonish(row.get("labels"), {}),
            "last_heartbeat_at": (
                row["last_heartbeat_at"].isoformat()
                if row.get("last_heartbeat_at")
                else None
            ),
            "created_by": row.get("created_by"),
            "metadata": self._jsonish(row.get("metadata"), {}),
            "created_at": row["created_at"].isoformat() if row.get("created_at") else None,
            "updated_at": row["updated_at"].isoformat() if row.get("updated_at") else None,
        }

    def _row_install(self, row: Any) -> dict[str, Any]:
        return {
            "id": row["id"],
            "project_id": row["project_id"],
            "node_id": row["node_id"],
            "package_id": row["package_id"],
            "status": row["status"],
            "layer": row.get("layer"),
            "mode": row.get("mode"),
            "receipt_id": row.get("receipt_id"),
            "error": self._jsonish(row.get("error"), {}),
            "created_by": row.get("created_by"),
            "metadata": self._jsonish(row.get("metadata"), {}),
            "created_at": row["created_at"].isoformat() if row.get("created_at") else None,
            "updated_at": row["updated_at"].isoformat() if row.get("updated_at") else None,
            "completed_at": (
                row["completed_at"].isoformat() if row.get("completed_at") else None
            ),
        }

    def _row_app(self, row: Any) -> dict[str, Any]:
        return {
            "id": row["id"],
            "project_id": row["project_id"],
            "node_id": row["node_id"],
            "name": row["name"],
            "status": row["status"],
            "mode": row.get("mode"),
            "image_ref": row.get("image_ref"),
            "previous_image_ref": row.get("previous_image_ref"),
            "compose_ref": row.get("compose_ref"),
            "domain": row.get("domain"),
            "public_url": row.get("public_url"),
            "admin_url": row.get("admin_url"),
            "receipt_id": row.get("receipt_id"),
            "error": self._jsonish(row.get("error"), {}),
            "created_by": row.get("created_by"),
            "metadata": self._jsonish(row.get("metadata"), {}),
            "created_at": row["created_at"].isoformat() if row.get("created_at") else None,
            "updated_at": row["updated_at"].isoformat() if row.get("updated_at") else None,
        }

    def _get_install(
        self, connection: Connection, project_id: str, install_id: str
    ) -> dict[str, Any]:
        row = connection.execute(
            text(
                "SELECT * FROM goldeneye_package_installs "
                "WHERE project_id = :project_id AND id = :id"
            ),
            {"project_id": project_id, "id": install_id},
        ).mappings().first()
        if not row:
            raise ValueError(f"Unknown install_id: {install_id}")
        return self._row_install(row)

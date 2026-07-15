from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Optional

from sqlalchemy.engine import Connection

from ..config import Settings
from .feature_catalog import FeatureCatalog


@dataclass(frozen=True)
class Capability:
    id: str
    title: str
    summary: str
    initiator_tool: Optional[str]
    enabled: bool
    reason: Optional[str] = None


@dataclass(frozen=True)
class CapabilityManifest:
    capabilities: tuple[Capability, ...]

    def enabled(self, capability_id: str) -> bool:
        return any(item.id == capability_id and item.enabled for item in self.capabilities)

    def get(self, capability_id: str) -> Optional[Capability]:
        return next((item for item in self.capabilities if item.id == capability_id), None)

    def render_table(self) -> str:
        lines = ["id | capability | initiator | enabled | note", "---|---|---|---|---"]
        for item in self.capabilities:
            lines.append(
                " | ".join(
                    [
                        item.id,
                        item.title,
                        item.initiator_tool or "informational",
                        "yes" if item.enabled else "no",
                        item.reason or item.summary,
                    ]
                )
            )
        return "\n".join(lines)

    def as_dict(self) -> list[dict]:
        return [asdict(item) for item in self.capabilities]


def build_manifest(
    connection: Connection, settings: Settings, project_id: str
) -> CapabilityManifest:
    """Build the truthful, per-turn capability view from the canonical catalog.

    The five orchestration initiators are explicit because they have callable
    tools. The rest of the feature catalog is included as informational context,
    not silently promoted into an actuator.
    """
    model_available = bool(settings.openai_api_key)
    code_target_available = bool(
        settings.target_github_owner and settings.target_github_repo
    )
    core = [
        Capability(
            "code_task",
            "Governed code task",
            "Run a scoped code change in a sandbox and produce a gated PR.",
            "create_code_task",
            code_target_available,
            None if code_target_available else "No target GitHub repository is configured.",
        ),
        Capability(
            "meeting",
            "Decision meeting",
            "Create a cross-functional meeting for an open design question.",
            "create_meeting",
            True,
        ),
        Capability(
            "prd",
            "PRD authoring panel",
            "Convene the governed multi-persona PRD workflow.",
            "create_prd",
            model_available,
            None if model_available else "No model backend is configured for PRD authoring.",
        ),
        Capability(
            "discovery",
            "Evidence discovery",
            "Research and compare an opportunity before committing delivery work.",
            "run_discovery",
            model_available,
            None if model_available else "No model backend is configured for discovery.",
        ),
        Capability(
            "multi_phase_command",
            "Multi-phase command",
            "Propose a dependency-sequenced plan and dispatch only after confirmation.",
            "propose_command",
            True,
        ),
    ]

    known_ids = {item.id for item in core}
    for entry in FeatureCatalog(settings).list_entries(connection, project_id):
        catalog_id = str(entry.get("label") or entry.get("slug") or entry["id"])
        if catalog_id in known_ids:
            continue
        status = str(entry.get("status") or "planned").strip().lower()
        enabled = status in {"complete", "completed", "implemented", "live", "shipped"}
        core.append(
            Capability(
                catalog_id,
                str(entry.get("title") or catalog_id),
                str(entry.get("source") or "Documented in the BOSS feature catalog."),
                None,
                enabled,
                None if enabled else f"Catalog status is {entry.get('status') or 'Planned'}.",
            )
        )
    return CapabilityManifest(tuple(core))

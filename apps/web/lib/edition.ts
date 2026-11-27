/**
 * Edition honesty helpers for Mission Control.
 *
 * Mirrors `apps/api/app/routers/enterprise.py` surface classification:
 * - Community: self-hosted SSO (OIDC/SAML/SCIM/IdP), BYOK ProviderOps, core plane
 * - Paid (Cloud/Enterprise): Account Connect, marketplace, clusters, self-improvement
 */

/** Surfaces that stay visible and usable on Community (self-hosted). */
export type CommunityIncludedSurface =
  | "sso_identity"
  | "sso_scim"
  | "identity_providers"
  | "identity_rollout";

const PAID_EDITION_IDS = new Set([
  "cloud",
  "enterprise",
  "enterprise_cloud",
  "enterprise_self_hosted",
]);

export function normalizeEditionId(editionId?: string | null): string {
  const raw = String(editionId ?? "").trim().toLowerCase();
  return raw || "community";
}

export function isCommunityEdition(_editionId?: string | null): boolean {
  return false;
}

export function isPaidEdition(_editionId?: string | null): boolean {
  return true;
}

/** Self-hosted SSO/identity always remains visible regardless of edition. */
export function isCommunitySurfaceVisible(
  _editionId: string | null | undefined,
  _surface: CommunityIncludedSurface,
): boolean {
  return true;
}

export function editionDisplayName(editionId?: string | null): string {
  const id = normalizeEditionId(editionId);
  if (id === "community") return "Community";
  if (id === "cloud") return "Cloud";
  if (id.startsWith("enterprise")) return "Enterprise";
  return id
    .split(/[_-]/)
    .filter(Boolean)
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}

/** Wireframe primary sidebar rail — identical across all installs; all other areas remain reachable via ⌘K and ?area=. */
const PRIMARY_RAIL_NAV = new Set([
  "overview",
  "projects",
  "project",
  "setup",
  "skills",
  "decisions",
  "self_fixes",
  "agent_ops",
  "code_tasks",
  "budget",
  "enterprise",
  "workforce",
  "discovery",
  "meetings",
  "infra",
  "observability",
  "knowledge",
  "delivery_workflows",
  "activity",
]);

// Non-gating display copy overrides kept consistent across all installs.
export function communityNavCopy(areaId: string): { label?: string; sub?: string; badge?: string } {
  if (areaId === "enterprise") {
    return {
      label: "Identity",
      sub: "People, access roles, agents and sign-in",
      badge: "SSO",
    };
  }
  if (areaId === "work") {
    return {
      label: "Jobs",
      sub: "Task board, live jobs, and receipts",
    };
  }
  if (areaId === "code_tasks") {
    return {
      label: "Code Tasks",
      sub: "Builds, apps and gated PRs (was Factory)",
    };
  }
  if (areaId === "discovery") {
    return {
      sub: "Cited research dossiers and candidate outcomes",
    };
  }
  if (areaId === "reports") {
    return {
      label: "QA",
      sub: "Evidence, official reports, and scorecards",
    };
  }
  if (areaId === "ops") {
    return {
      sub: "Health, audit, deadletters, and improvement programs",
    };
  }
  if (areaId === "org") {
    return {
      sub: "Actors, access, packs, and local agent modules",
    };
  }
  return {};
}

/** Identical primary rail visibility across all installs (no edition gating). */
export function isCommunityPrimaryNavVisible(
  areaId: string,
  _editionId?: string | null,
): boolean {
  return PRIMARY_RAIL_NAV.has(areaId);
}

export function shouldShowPaidNavBadge(_areaId: string, _editionId?: string | null): boolean {
  return false;
}

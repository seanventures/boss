/**
 * Edition honesty helpers for Mission Control.
 *
 * Mirrors `apps/api/app/routers/enterprise.py` surface classification:
 * - Community: self-hosted SSO (OIDC/SAML/SCIM/IdP), BYOK ProviderOps, core plane
 * - Paid (Cloud/Enterprise): Account Connect, marketplace, clusters, self-improvement
 */

/** Surfaces that must not present working CTAs on Community. */
export type PaidSurface =
  | "account_connect"
  | "marketplace"
  | "self_improvement"
  | "enterprise_clusters";

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

const PAID_SURFACE_META: Record<
  PaidSurface,
  { label: string; tier: "Cloud" | "Enterprise" | "Cloud / Enterprise"; summary: string }
> = {
  account_connect: {
    label: "Account Connect",
    tier: "Cloud",
    summary:
      "Hosted org link, team invites, billing portal, and hosted auth sessions require BOSS Cloud Account Connect.",
  },
  marketplace: {
    label: "Hosted marketplace",
    tier: "Cloud",
    summary:
      "Hosted agent marketplace catalog sync is a Cloud feature. Local agent modules remain available on Community.",
  },
  self_improvement: {
    label: "Self-improvement automation",
    tier: "Enterprise",
    summary:
      "Proposal create / canary / staged rollout live on Enterprise. Community keeps improvement programs (trial ledger) only.",
  },
  enterprise_clusters: {
    label: "Enterprise clusters",
    tier: "Enterprise",
    summary:
      "Cluster packs, HA profiles, validation evidence, and Enterprise support cases are Enterprise-only.",
  },
};

export function normalizeEditionId(editionId?: string | null): string {
  const raw = String(editionId ?? "").trim().toLowerCase();
  return raw || "community";
}

export function isCommunityEdition(editionId?: string | null): boolean {
  const id = normalizeEditionId(editionId);
  if (PAID_EDITION_IDS.has(id) || id.startsWith("enterprise")) return false;
  return id === "community" || id === "oss" || id === "local";
}

export function isPaidEdition(editionId?: string | null): boolean {
  return !isCommunityEdition(editionId);
}

/** True when paid CTAs for this surface should render (not Community). */
export function isPaidSurfaceVisible(
  editionId: string | null | undefined,
  surface: PaidSurface,
): boolean {
  void surface;
  return isPaidEdition(editionId);
}

/** Self-hosted SSO/identity always remains visible regardless of edition. */
export function isCommunitySurfaceVisible(
  _editionId: string | null | undefined,
  _surface: CommunityIncludedSurface,
): boolean {
  return true;
}

export function paidSurfaceLabel(surface: PaidSurface): string {
  return PAID_SURFACE_META[surface].label;
}

export function paidSurfaceTier(surface: PaidSurface): string {
  return PAID_SURFACE_META[surface].tier;
}

export function paidSurfaceReason(
  surface: PaidSurface,
  editionId?: string | null,
): string {
  const meta = PAID_SURFACE_META[surface];
  const edition = normalizeEditionId(editionId);
  return (
    `${meta.summary} This instance runs the ${edition} edition — ` +
    `${meta.tier} CTAs are hidden so the console does not look broken.`
  );
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

/**
 * Leftover product chrome that must not appear as a required Community
 * primary-nav area. Routes, ⌘K, and `?area=` deep links stay intact.
 *
 * `enterprise` is NOT hidden: Community self-hosted SSO / IdP packs live there.
 * Only the Enterprise *label* is stripped via `communityNavCopy`.
 *
 * V1 Community home is a small cut (see docs/product/UI_V1_CUT.md). Areas are
 * not deleted — they stay reachable via ⌘K and `?area=`.
 */
const COMMUNITY_HIDDEN_PRIMARY_NAV = new Set(["foundations", "components"]);

const V1_COMMUNITY_PRIMARY_NAV = new Set([
  "overview",
  "activity",
  "work",
  "decisions",
  "reliability",
  "code_tasks",
  "reports",
  "budget",
  "enterprise",
]);

/**
 * Nav chrome adjustments for Community honesty.
 * Enterprise area stays as **SSO** (identity lives there); it is not an
 * Enterprise-product required area. Paid ops/org copy is softened.
 */
export function communityNavCopy(areaId: string): { label?: string; sub?: string; badge?: string } {
  if (areaId === "enterprise") {
    return {
      label: "SSO",
      sub: "Self-hosted identity — OIDC, SAML, SCIM, and IdP packs",
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
      label: "Factory",
      sub: "Governed code tasks and gated PRs",
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

/** False for design-system-as-product lab areas on Community primary nav. */
export function isCommunityPrimaryNavVisible(
  areaId: string,
  editionId?: string | null,
): boolean {
  if (!isCommunityEdition(editionId)) return true;
  if (COMMUNITY_HIDDEN_PRIMARY_NAV.has(areaId)) return false;
  return V1_COMMUNITY_PRIMARY_NAV.has(areaId);
}

export function shouldShowPaidNavBadge(areaId: string, editionId?: string | null): boolean {
  if (!isCommunityEdition(editionId)) return false;
  // Ops hosts self-improvement (paid); org hosts marketplace (paid).
  return areaId === "ops" || areaId === "org";
}

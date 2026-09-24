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

export function isCommunityEdition(editionId?: string | null): boolean {
  const id = normalizeEditionId(editionId);
  if (PAID_EDITION_IDS.has(id) || id.startsWith("enterprise")) return false;
  return id === "community" || id === "oss" || id === "local";
}

export function isPaidEdition(editionId?: string | null): boolean {
  return !isCommunityEdition(editionId);
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

// The Community cut. The full console is defined in ConsoleShell's AREAS; this list is the
// only thing that decides what a Community operator sees in the sidebar, so an area dropped
// here is still present for Enterprise and Cloud — and still reachable by ?area= for everyone.
//
// Not listed, deliberately:
//   activity  — 47,921 events, ~75% a single internal audit-export event. Not empty, unreadable.
//   reliability — incidents: 0 rows.
//   reports/Evidence — drift_reports: 0 rows.
//   work ("Jobs") — NOT empty: 5 open tasks, 196 jobs, 4,287 receipts. Hidden because the
//     screen is plumbing rather than a place decisions get made; Factory and Approvals are
//     where the same work is actually acted on. Worth revisiting if open tasks ever need to be
//     seen on their own.
// Each returns the moment it earns a click.
const V1_COMMUNITY_PRIMARY_NAV = new Set([
  "overview",
  // THE FIRST TEN MINUTES, and the one entry a self-hosted operator needs before any other.
  // Every piece of setup already had an endpoint; what nobody had was the order. A console that
  // hides its own setup behind documentation is a console that gets abandoned during
  // evaluation, which is precisely the audience this edition is for.
  "setup",
  // Skills are how a self-hosted operator adds a capability BOSS does not ship. Hiding that
  // from the edition aimed at self-hosting would hide the point.
  "skills",
  "decisions",
  "code_tasks",
  "budget",
  "enterprise",

  // Workforce and Org Chart. BOSS's pitch is governed AI labour, and until these were here the
  // console could show what the agents produced but never the agents themselves -- who did the
  // work, how well it went, and which of them are running without a registered role at all.
  // That last one is a governance fact an operator should not have to go looking for.
  "workforce",
  "org_chart",

  // The path from an idea to shipped software. Without these the console lists the places
  // work ENDS UP -- approvals, jobs, reports -- and none of the places it starts, so an
  // operator can watch BOSS work but cannot see how to set it going. Discovery and Meetings
  // are the two ways in besides a direct code task; Apps is where the result lives; CI/CD and
  // Monitoring are how it reaches production and whether it is alive.
  //
  // Task Shepherd is deliberately NOT here. Its automatic retries and repairs are worth having
  // on by default and not worth a nav entry: a self-hosted operator wants to know a run
  // recovered, not to watch the reasoning. It stays available to the paid editions and by
  // deep link.
  "discovery",
  "meetings",
  "delivery",   // labelled "CI/CD" in the nav — the id and the label differ
  "monitoring",

  // How work moves through the factory. A self-hosted operator who cannot choose or see the
  // path their work takes is running the product blind, so this is not a paid feature either.
  "delivery_workflows",

  // The log itself, directly under Observability. A dashboard tells you something is wrong;
  // this is where you go next, and until it was here the only way to reach it was a URL you
  // had to already know. It is also the one place the 40-90 minutes of a build are legible —
  // which agent said what, which tool it ran, what came back — so a self-hosted operator with
  // no access to our infrastructure can debug their own builds. That is not a paid feature.
  "activity",
]);

/**
 * Nav chrome adjustments for Community honesty.
 * Enterprise area stays as **SSO** (identity lives there); it is not an
 * Enterprise-product required area. Paid ops/org copy is softened.
 */
export function communityNavCopy(areaId: string): { label?: string; sub?: string; badge?: string } {
  if (areaId === "enterprise") {
    // "Identity", not "SSO": SSO names one protocol, and this area configures who can sign in
    // at all — OIDC, SAML and SCIM provisioning. The badge stays "SSO" because that is the
    // thing Community is being told it INCLUDES, and it is the word somebody scanning for it
    // would look for.
    return {
      label: "Identity",
      sub: "Who can sign in — OIDC, SAML, SCIM, and IdP packs",
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
      sub: "Governed projects and gated PRs",
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

/** Hosted and enterprise edition presentation metadata. */
import { isPaidEdition, normalizeEditionId } from "./edition";

/** Surfaces that must not present working CTAs on Community. */
export type PaidSurface =
  | "account_connect"
  | "marketplace"
  | "self_improvement"
  | "enterprise_clusters";

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

/** True when paid CTAs for this surface should render (not Community). */
export function isPaidSurfaceVisible(
  editionId: string | null | undefined,
  surface: PaidSurface,
): boolean {
  void surface;
  return isPaidEdition(editionId);
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


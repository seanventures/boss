/**
 * Presentational edition honesty chrome for Mission Control.
 * Pure UI — classification lives in `lib/edition.ts`.
 */

import React, { type CSSProperties, type ReactNode } from "react";
import {
  editionDisplayName,
  isCommunityEdition,
  isPaidSurfaceVisible,
  paidSurfaceLabel,
  paidSurfaceReason,
  paidSurfaceTier,
  type PaidSurface,
} from "../lib/edition";

const badgeStyle: CSSProperties = {
  fontSize: 10.5,
  color: "#d29922",
  border: "1px solid #d29922",
  padding: "1px 8px",
  borderRadius: 999,
  whiteSpace: "nowrap",
};

const communityBadgeStyle: CSSProperties = {
  fontSize: 10.5,
  color: "var(--green, #3fb950)",
  border: "1px solid var(--green, #3fb950)",
  padding: "1px 8px",
  borderRadius: 999,
  whiteSpace: "nowrap",
};

export function PaidTierBadge({
  surface,
  label,
}: {
  surface?: PaidSurface;
  label?: string;
}) {
  const text = label ?? (surface ? paidSurfaceTier(surface) : "Cloud / Enterprise");
  return (
    <span style={badgeStyle} data-testid="paid-tier-badge" data-tier={text}>
      {text}
    </span>
  );
}

export function CommunityIncludedBadge({ label = "Included in Community" }: { label?: string }) {
  return (
    <span style={communityBadgeStyle} data-testid="community-included-badge">
      {label}
    </span>
  );
}

export function EditionChip({ editionId }: { editionId?: string | null }) {
  const community = isCommunityEdition(editionId);
  const name = editionDisplayName(editionId);
  return (
    <span
      className="bx-chip hide-sm"
      data-testid="edition-chip"
      data-edition={editionDisplayName(editionId).toLowerCase()}
      title={`BOSS edition: ${name}`}
      style={
        community
          ? undefined
          : { borderColor: "var(--amber, #d29922)", color: "var(--amber, #d29922)" }
      }
    >
      <b>{name}</b>
    </span>
  );
}

/**
 * On Community, replace paid children with an honest empty card.
 * On paid editions, render children unchanged.
 */
export function PaidSurfaceGate({
  editionId,
  surface,
  children,
  title,
}: {
  editionId?: string | null;
  surface: PaidSurface;
  children: ReactNode;
  /** Optional override for the honesty card heading. */
  title?: string;
}) {
  if (isPaidSurfaceVisible(editionId, surface)) {
    return <>{children}</>;
  }

  const heading = title ?? paidSurfaceLabel(surface);
  return (
    <article
      className="stack-card"
      data-testid={`paid-surface-hidden-${surface}`}
      data-edition-gate="hidden"
      data-paid-surface={surface}
    >
      <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 8, flexWrap: "wrap" }}>
        <h3 style={{ margin: 0 }}>{heading}</h3>
        <PaidTierBadge surface={surface} />
      </div>
      <p className="detail">{paidSurfaceReason(surface, editionId)}</p>
      <p className="detail">
        Self-hosted SSO and identity (OIDC, SAML, SCIM, IdP packs) remain available on Community
        in this area — only hosted commercial surfaces are gated.
      </p>
    </article>
  );
}

/** Section heading row with optional Community-included badge. */
export function SurfaceHeading({
  title,
  editionId,
  communityIncluded = false,
  paidSurface,
  children,
}: {
  title: string;
  editionId?: string | null;
  communityIncluded?: boolean;
  paidSurface?: PaidSurface;
  children?: ReactNode;
}) {
  const showCommunity = communityIncluded && isCommunityEdition(editionId);
  const showPaid = paidSurface && isCommunityEdition(editionId);
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
      <h3 style={{ margin: 0 }}>{title}</h3>
      {showCommunity ? <CommunityIncludedBadge /> : null}
      {showPaid && paidSurface ? <PaidTierBadge surface={paidSurface} /> : null}
      {children}
    </div>
  );
}

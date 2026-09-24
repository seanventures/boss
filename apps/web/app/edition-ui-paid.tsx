/** Paid-edition presentation. Excluded from Community source exports. */
import React, { type CSSProperties, type ReactNode } from "react";
import {
  isPaidSurfaceVisible,
  paidSurfaceLabel,
  paidSurfaceReason,
  paidSurfaceTier,
  type PaidSurface,
} from "../lib/edition-paid";

const badgeStyle: CSSProperties = {
  fontSize: 10.5,
  color: "#d29922",
  border: "1px solid #d29922",
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


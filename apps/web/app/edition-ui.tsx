/**
 * Presentational edition honesty chrome for Mission Control.
 * Pure UI — classification lives in `lib/edition.ts`.
 */

import React, { type CSSProperties, type ReactNode } from "react";
import {
  editionDisplayName,
  isCommunityEdition,
} from "../lib/edition";

const communityBadgeStyle: CSSProperties = {
  fontSize: 10.5,
  color: "var(--green, #3fb950)",
  border: "1px solid var(--green, #3fb950)",
  padding: "1px 8px",
  borderRadius: 999,
  whiteSpace: "nowrap",
};

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

/** Section heading row with optional Community-included badge. */
export function SurfaceHeading({
  title,
  editionId,
  communityIncluded = false,
  children,
}: {
  title: string;
  editionId?: string | null;
  communityIncluded?: boolean;
  children?: ReactNode;
}) {
  const showCommunity = communityIncluded && isCommunityEdition(editionId);
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
      <h3 style={{ margin: 0 }}>{title}</h3>
      {showCommunity ? <CommunityIncludedBadge /> : null}
      {children}
    </div>
  );
}

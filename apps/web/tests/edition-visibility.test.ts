import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";

/**
 * U2 Community UI honesty — edition-aware visibility.
 *
 * On BOSS_EDITION=community (edition_id community):
 * - Hide paid CTAs: Account Connect, marketplace, self-improvement, enterprise clusters
 * - Keep self-hosted SSO / identity visible
 */

vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(),
  usePathname: () => "/",
}));

import {
  communityNavCopy,
  editionDisplayName,
  isCommunityEdition,
  isCommunitySurfaceVisible,
  isPaidEdition,
  isPaidSurfaceVisible,
  normalizeEditionId,
  paidSurfaceReason,
  shouldShowPaidNavBadge,
} from "../lib/edition";
import {
  CommunityIncludedBadge,
  EditionChip,
  PaidSurfaceGate,
  PaidTierBadge,
} from "../app/edition-ui";
import ConsoleShell from "../app/ConsoleShell";

describe("edition classification (mirrors API paid surface map)", () => {
  it("treats community / oss / local as unpaid", () => {
    expect(isCommunityEdition("community")).toBe(true);
    expect(isCommunityEdition("COMMUNITY")).toBe(true);
    expect(isCommunityEdition(undefined)).toBe(true);
    expect(isCommunityEdition("")).toBe(true);
    expect(isCommunityEdition("oss")).toBe(true);
    expect(isCommunityEdition("local")).toBe(true);
    expect(isPaidEdition("community")).toBe(false);
  });

  it("treats cloud and enterprise ids as paid", () => {
    expect(isCommunityEdition("cloud")).toBe(false);
    expect(isCommunityEdition("enterprise")).toBe(false);
    expect(isCommunityEdition("enterprise_cloud")).toBe(false);
    expect(isCommunityEdition("enterprise_self_hosted")).toBe(false);
    expect(isPaidEdition("enterprise")).toBe(true);
  });

  it("normalizes empty edition to community", () => {
    expect(normalizeEditionId(null)).toBe("community");
    expect(normalizeEditionId("  Cloud ")).toBe("cloud");
    expect(editionDisplayName("community")).toBe("Community");
    expect(editionDisplayName("enterprise_self_hosted")).toBe("Enterprise");
  });
});

describe("paid vs community surface visibility", () => {
  const paidSurfaces = [
    "account_connect",
    "marketplace",
    "self_improvement",
    "enterprise_clusters",
  ] as const;

  it("hides all paid surfaces on community", () => {
    for (const surface of paidSurfaces) {
      expect(isPaidSurfaceVisible("community", surface)).toBe(false);
      expect(paidSurfaceReason(surface, "community")).toMatch(/community/i);
      expect(paidSurfaceReason(surface, "community")).toMatch(/hidden/i);
    }
  });

  it("shows paid surfaces on cloud and enterprise", () => {
    for (const surface of paidSurfaces) {
      expect(isPaidSurfaceVisible("cloud", surface)).toBe(true);
      expect(isPaidSurfaceVisible("enterprise", surface)).toBe(true);
    }
  });

  it("keeps SSO / identity community surfaces always visible", () => {
    for (const edition of ["community", "cloud", "enterprise"] as const) {
      expect(isCommunitySurfaceVisible(edition, "sso_identity")).toBe(true);
      expect(isCommunitySurfaceVisible(edition, "sso_scim")).toBe(true);
      expect(isCommunitySurfaceVisible(edition, "identity_providers")).toBe(true);
      expect(isCommunitySurfaceVisible(edition, "identity_rollout")).toBe(true);
    }
  });
});

describe("community nav honesty helpers", () => {
  it("relabels Editions nav to emphasize SSO on community", () => {
    const copy = communityNavCopy("enterprise");
    expect(copy.label).toMatch(/SSO/i);
    expect(copy.sub).toMatch(/OIDC|SAML|SCIM/i);
    expect(copy.badge).toBe("SSO");
  });

  it("softens ops/org copy and marks paid nav badges only on community", () => {
    expect(communityNavCopy("ops").sub).toMatch(/deadletters|improvement programs/i);
    expect(shouldShowPaidNavBadge("ops", "community")).toBe(true);
    expect(shouldShowPaidNavBadge("org", "community")).toBe(true);
    expect(shouldShowPaidNavBadge("enterprise", "community")).toBe(false);
    expect(shouldShowPaidNavBadge("ops", "enterprise")).toBe(false);
  });
});

describe("PaidSurfaceGate presentational honesty", () => {
  it("replaces children with honesty card on community", () => {
    const html = renderToStaticMarkup(
      React.createElement(
        PaidSurfaceGate,
        { editionId: "community", surface: "account_connect" },
        React.createElement("button", { type: "button" }, "Link Account Connect"),
      ),
    );
    expect(html).toContain('data-testid="paid-surface-hidden-account_connect"');
    expect(html).toContain("Account Connect");
    expect(html).toMatch(/Cloud/i);
    expect(html).not.toContain("Link Account Connect");
    expect(html).toMatch(/Self-hosted SSO/i);
  });

  it("passes through children on enterprise", () => {
    const html = renderToStaticMarkup(
      React.createElement(
        PaidSurfaceGate,
        { editionId: "enterprise", surface: "marketplace" },
        React.createElement("button", { type: "button" }, "Sync hosted catalog"),
      ),
    );
    expect(html).toContain("Sync hosted catalog");
    expect(html).not.toContain("paid-surface-hidden");
  });

  it("renders marketplace and cluster honesty independently", () => {
    const market = renderToStaticMarkup(
      React.createElement(
        PaidSurfaceGate,
        { editionId: "community", surface: "marketplace" },
        null,
      ),
    );
    const clusters = renderToStaticMarkup(
      React.createElement(
        PaidSurfaceGate,
        { editionId: "community", surface: "enterprise_clusters" },
        null,
      ),
    );
    expect(market).toContain("paid-surface-hidden-marketplace");
    expect(clusters).toContain("paid-surface-hidden-enterprise_clusters");
    expect(clusters).toMatch(/Enterprise/i);
  });
});

describe("edition chrome badges", () => {
  it("renders community edition chip and included badge", () => {
    const chip = renderToStaticMarkup(
      React.createElement(EditionChip, { editionId: "community" }),
    );
    expect(chip).toContain('data-testid="edition-chip"');
    expect(chip).toContain("Community");

    const included = renderToStaticMarkup(React.createElement(CommunityIncludedBadge));
    expect(included).toContain('data-testid="community-included-badge"');
    expect(included).toMatch(/Community/i);

    const paid = renderToStaticMarkup(
      React.createElement(PaidTierBadge, { surface: "self_improvement" }),
    );
    expect(paid).toContain('data-testid="paid-tier-badge"');
    expect(paid).toMatch(/Enterprise/i);
  });
});

describe("ConsoleShell edition-aware nav", () => {
  const baseProps = {
    status: {
      apiReachable: true,
      budgetState: "green",
      deadletters: 0,
      pendingDecisions: 0,
    },
    scope: { project: "boss-dogfood", actor: "ceo" },
    children: React.createElement("div", null, "body"),
  };

  it("shows SSO-labeled nav and keeps enterprise area on community", () => {
    const html = renderToStaticMarkup(
      React.createElement(ConsoleShell, {
        ...baseProps,
        editionId: "community",
      }),
    );
    expect(html).toContain('data-edition="community"');
    expect(html).toContain('data-community="true"');
    expect(html).toContain('data-area-nav="enterprise"');
    expect(html).toContain('data-sso-visible="true"');
    expect(html).toContain('data-testid="nav-sso-badge"');
    expect(html).toMatch(/SSO &amp; Editions|SSO & Editions/);
    expect(html).toContain('data-testid="edition-chip"');
    // Paid badges on ops/org (self-improvement + marketplace homes)
    expect(html).toContain('data-testid="nav-paid-badge-ops"');
    expect(html).toContain('data-testid="nav-paid-badge-org"');
  });

  it("does not badge paid nav on enterprise edition", () => {
    const html = renderToStaticMarkup(
      React.createElement(ConsoleShell, {
        ...baseProps,
        editionId: "enterprise",
      }),
    );
    expect(html).toContain('data-edition="enterprise"');
    expect(html).toContain('data-community="false"');
    expect(html).not.toContain("nav-paid-badge-ops");
    expect(html).not.toContain("nav-sso-badge");
    // Default label when not community-relabeled
    expect(html).toContain("Editions &amp; SSO");
  });
});
